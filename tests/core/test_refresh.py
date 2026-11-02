"""Tests for forge.core.refresh: the pure plan, apply on FakeGitHub and branch gathering."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from forge.core.config import ForgeConfig, load_config
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.git import Git, Lease
from forge.core.github import GitHubClient, GitHubError, Issue, PullRequest
from forge.core.github_fake import FakeGitHub
from forge.core.refresh import (
    BranchScan,
    RefreshPlan,
    TaskBranch,
    apply_refresh,
    check_full_history,
    ensure_labels,
    fetch_task_refs,
    gather_task_branches,
    plan_refresh,
    run_refresh,
)
from forge.core.tasks import (
    BEGIN_MARKER,
    BLOCKED_CONFLICT,
    END_MARKER,
    LABELS,
    TRAILER_AGENT,
    TRAILER_CLAIM,
    TRAILER_HANDOFF,
    TRAILER_RELEASE,
    TRAILER_TASK,
    BranchEvent,
    BranchState,
    ForgeEvent,
    Size,
    Status,
    TaskMetadata,
    TaskType,
    last_event_from_trailers,
    upsert_block,
)
from forge.core.tiers import Tier

if TYPE_CHECKING:
    from conftest import GitWorld

NOW = datetime(2026, 11, 2, tzinfo=UTC)
MIRRORS = ("forge:task", "size:S", "tier:standard", "type:impl")


def branch(task_id: str, event_trailer: str, *older: Mapping[str, str]) -> TaskBranch:
    """A task branch whose newest commit carries ``event_trailer`` by alice for ``task_id``."""
    newest = {TRAILER_TASK: task_id, event_trailer: "alice"}
    if event_trailer == TRAILER_CLAIM:
        newest[TRAILER_AGENT] = "claude"
    return TaskBranch("abc", (newest, *older))


def claimed(task_id: str) -> TaskBranch:
    return branch(task_id, TRAILER_CLAIM)


def meta(task_id: str, deps: Sequence[str] = ()) -> TaskMetadata:
    return TaskMetadata(
        id=task_id,
        spec="specs/m1/x.md",
        covers=("AC-1",),
        depends_on=tuple(deps),
        zones=("api",),
        size=Size.S,
        tier=Tier.STANDARD,
        type=TaskType.IMPL,
        done_when=("tests pass",),
    )


def body(m: TaskMetadata) -> str:
    return upsert_block("Description.\n", m)


def issue(
    number: int,
    m: TaskMetadata | None,
    *,
    labels: Sequence[str] = (),
    state: str = "open",
    reason: str | None = None,
    raw_body: str | None = None,
) -> Issue:
    return Issue(
        number=number,
        title=f"task {number}",
        body=raw_body if raw_body is not None else (body(m) if m else "plain"),
        state="closed" if state == "closed" else "open",
        state_reason=reason,
        labels=tuple(labels),
        assignees=(),
        milestone=None,
        author="lead",
        url=f"https://example.test/{number}",
        created_at=NOW,
        updated_at=NOW,
        closed_at=NOW if state == "closed" else None,
    )


def with_status(status: Status, *extra: str) -> tuple[str, ...]:
    return (*MIRRORS, status.label, *extra)


def by_issue(plan: RefreshPlan) -> dict[int, tuple[Status | None, Status | None]]:
    return {c.issue: (c.from_status, c.to_status) for c in plan.changes}


# --------------------------------------------------------------------------- plan


def test_deps_closing_flips_blocked_to_ready() -> None:
    t1 = issue(1, meta("M1-T01"), labels=with_status(Status.READY))
    t2 = issue(2, meta("M1-T02", ["M1-T01"]), labels=with_status(Status.BLOCKED))
    assert plan_refresh([t1, t2], {}, set()).changes == ()

    closed = replace(t1, state="closed", state_reason="completed")
    plan = plan_refresh([closed, t2], {}, set())
    assert by_issue(plan) == {
        1: (Status.READY, Status.DONE),
        2: (Status.BLOCKED, Status.READY),
    }
    change = next(c for c in plan.changes if c.issue == 2)
    assert change.added == ("status:ready",)
    assert change.removed == ("status:blocked",)


def test_branch_events() -> None:
    dep_done = issue(1, meta("M1-T01"), labels=with_status(Status.DONE), state="closed")
    issues = [
        dep_done,
        issue(2, meta("M1-T02", ["M1-T01"]), labels=with_status(Status.READY)),
        issue(3, meta("M1-T03"), labels=with_status(Status.IN_PROGRESS)),
        issue(4, meta("M1-T04", ["M1-T01"]), labels=with_status(Status.IN_PROGRESS)),
    ]
    states = {
        2: claimed("M1-T02"),
        3: branch("M1-T03", TRAILER_HANDOFF),
        4: branch("M1-T04", TRAILER_RELEASE),
    }
    plan = plan_refresh(issues, states, set())
    assert by_issue(plan) == {
        2: (Status.READY, Status.IN_PROGRESS),
        3: (Status.IN_PROGRESS, Status.HANDOFF),
        4: (Status.IN_PROGRESS, Status.READY),
    }


def test_open_pr_closed_and_not_planned() -> None:
    issues = [
        issue(1, meta("M1-T01"), labels=with_status(Status.IN_PROGRESS)),
        issue(2, meta("M1-T02"), labels=with_status(Status.IN_REVIEW), state="closed"),
        issue(
            3,
            meta("M1-T03"),
            labels=with_status(Status.READY),
            state="closed",
            reason="not_planned",
        ),
    ]
    plan = plan_refresh(issues, {1: claimed("M1-T01")}, {"task/1", "feature/x"})
    assert by_issue(plan) == {
        1: (Status.IN_PROGRESS, Status.IN_REVIEW),
        2: (Status.IN_REVIEW, Status.DONE),
        3: (Status.READY, None),
    }
    not_planned = next(c for c in plan.changes if c.issue == 3)
    assert not_planned.added == () and not_planned.removed == ("status:ready",)
    assert plan.counts["statuses"] == {"done": 1, "in-review": 1, "none": 1}


def test_proposed_stays_and_mirrors_are_fixed() -> None:
    proposed = issue(1, meta("M1-T01", ["M1-T09"]), labels=("forge:task", "status:proposed"))
    wrong = issue(
        2,
        meta("M1-T02"),
        labels=("forge:task", "size:M", "tier:light", "type:impl", "status:ready", "bug"),
    )
    plan = plan_refresh([proposed, wrong], {}, set())
    first = next(c for c in plan.changes if c.issue == 1)
    assert (first.from_status, first.to_status) == (Status.PROPOSED, Status.PROPOSED)
    assert first.added == ("size:S", "tier:standard", "type:impl") and first.removed == ()
    second = next(c for c in plan.changes if c.issue == 2)
    assert second.added == ("size:S", "tier:standard")
    assert second.removed == ("size:M", "tier:light")
    # proposed is sticky: no warning about the unknown dependency is even computed
    assert plan.warnings == ()


def test_user_labels_and_blocked_conflict_untouched() -> None:
    labels = with_status(Status.READY, BLOCKED_CONFLICT, "bug", "status:custom")
    t = issue(1, meta("M1-T01"), labels=labels)
    assert plan_refresh([t], {}, set()).changes == ()
    plan = plan_refresh([t], {1: claimed("M1-T01")}, set())
    (change,) = plan.changes
    assert change.added == ("status:in-progress",)
    assert change.removed == ("status:ready",)


def test_missing_label_status_and_forge_task_added() -> None:
    t = issue(1, meta("M1-T01"), labels=())
    (change,) = plan_refresh([t], {}, set()).changes
    assert change.from_status is None and change.to_status is Status.READY
    assert set(change.added) == {*MIRRORS, "status:ready"}


def test_invalid_and_missing_blocks_warn_and_skip() -> None:
    bad = f"x\n\n{BEGIN_MARKER}\n```yaml\nid: nope\n```\n{END_MARKER}\n"
    unbalanced = f"x\n{BEGIN_MARKER}\n"
    issues = [
        issue(5, None, labels=("forge:task",)),
        issue(6, None, raw_body=bad, labels=("forge:task",)),
        issue(7, None, raw_body=unbalanced, labels=("forge:task",)),
        issue(8, meta("M1-T08"), labels=with_status(Status.READY)),
    ]
    plan = plan_refresh(issues, {}, set())
    assert plan.changes == ()
    assert len(plan.warnings) == 3
    assert plan.warnings[0].startswith("issue #5:") and "no Forge block" in plan.warnings[0]
    assert plan.warnings[1].startswith("issue #6:") and "skipped" in plan.warnings[1]
    assert plan.warnings[2].startswith("issue #7:")
    assert plan.counts["skipped"] == 3 and plan.counts["tasks"] == 1


def test_duplicate_id_warns_and_lowest_issue_wins() -> None:
    open_dup = issue(3, meta("M1-T01"), labels=with_status(Status.READY))
    closed_first = issue(1, meta("M1-T01"), labels=with_status(Status.DONE), state="closed")
    dependent = issue(4, meta("M1-T04", ["M1-T01"]), labels=with_status(Status.BLOCKED))
    plan = plan_refresh([open_dup, dependent, closed_first], {}, set())
    assert any("duplicate task id M1-T01" in w and "#3" in w for w in plan.warnings)
    assert by_issue(plan) == {4: (Status.BLOCKED, Status.READY)}


def test_unknown_dependency_warns_and_blocks() -> None:
    t = issue(1, meta("M1-T01", ["M9-T99"]), labels=with_status(Status.READY))
    plan = plan_refresh([t], {}, set())
    assert by_issue(plan) == {1: (Status.READY, Status.BLOCKED)}
    assert any("unknown dependency M9-T99" in w for w in plan.warnings)


def test_not_planned_dependency_counts_as_closed_with_warning() -> None:
    dropped = issue(
        1, meta("M1-T01"), labels=with_status(Status.DONE), state="closed", reason="not_planned"
    )
    dependent = issue(2, meta("M1-T02", ["M1-T01"]), labels=with_status(Status.BLOCKED))
    proposed = issue(3, meta("M1-T03", ["M1-T01"]), labels=("forge:task", "status:proposed"))
    closed = issue(4, meta("M1-T04", ["M1-T01"]), labels=with_status(Status.DONE), state="closed")
    plan = plan_refresh([dropped, dependent, proposed, closed], {}, set())
    assert by_issue(plan)[2] == (Status.BLOCKED, Status.READY)
    (warning,) = plan.warnings
    assert warning.startswith("issue #2 (M1-T02): dependency M1-T01 was closed as not planned")
    assert "depends_on" in warning


def test_not_planned_warning_skips_proposed_case_insensitively() -> None:
    dropped = issue(
        1, meta("M1-T01"), labels=with_status(Status.DONE), state="closed", reason="not_planned"
    )
    proposed = issue(2, meta("M1-T02", ["M1-T01"]), labels=("forge:task", "Status:Proposed"))
    plan = plan_refresh([dropped, proposed], {}, set())
    assert plan.warnings == ()
    assert by_issue(plan)[2] == (Status.PROPOSED, Status.PROPOSED)


def test_to_data_shape() -> None:
    t = issue(1, meta("M1-T01"), labels=with_status(Status.BLOCKED))
    data = plan_refresh([t], {}, set()).to_data()
    assert set(data) == {"changes", "warnings", "counts"}
    assert data["changes"] == [
        {
            "issue": 1,
            "id": "M1-T01",
            "from": "blocked",
            "to": "ready",
            "added": ["status:ready"],
            "removed": ["status:blocked"],
            "applied": True,
        }
    ]
    assert data["counts"]["changed"] == 1 and data["counts"]["failed"] == 0


# --------------------------------------------------------------------------- apply


def seeded_fake() -> FakeGitHub:
    fake = FakeGitHub()
    fake.create_label("forge:task", "5319e7")
    fake.create_label("bug", "ff0000")
    fake.create_label("status:blocked", "000000")  # wrong colour: must not be changed
    return fake


def test_ensure_labels_creates_only_missing() -> None:
    fake = seeded_fake()
    created = ensure_labels(fake)
    assert "forge:task" not in created and "status:blocked" not in created
    names = {label.name for label in fake.list_labels()}
    assert names == {spec.name for spec in LABELS} | {"bug"}
    blocked = next(lb for lb in fake.list_labels() if lb.name == "status:blocked")
    assert blocked.color == "000000"
    assert ensure_labels(fake) == []


def test_apply_then_replan_is_idempotent() -> None:
    fake = seeded_fake()
    fake.create_issue("T1", body(meta("M1-T01")), labels=["forge:task"])
    fake.create_issue("T2", body(meta("M1-T02", ["M1-T01"])), labels=["forge:task", "bug"])
    t3 = fake.create_issue("T3", body(meta("M1-T03")), labels=["forge:task", "status:blocked"])
    fake.create_issue("T4", body(meta("M1-T04", ["M1-T03"])), labels=["forge:task"])
    fake.update_issue(t3.number, state="closed")

    plan = plan_refresh(fake.list_issues(state="all"), {1: claimed("M1-T01")}, set())
    assert len(plan.changes) == 4
    apply_refresh(fake, plan)

    labels = {i.number: set(i.labels) for i in fake.list_issues(state="all")}
    assert labels[1] == {*MIRRORS, "status:in-progress"}
    assert labels[2] == {*MIRRORS, "status:blocked", "bug"}
    assert labels[3] == {*MIRRORS, "status:done"}
    assert labels[4] == {*MIRRORS, "status:ready"}

    again = plan_refresh(fake.list_issues(state="all"), {1: claimed("M1-T01")}, set())
    assert again.changes == ()


class FailOnIssue(FakeGitHub):
    """FakeGitHub whose label writes fail for the issues in ``failing``."""

    def __init__(self, failing: set[int]) -> None:
        super().__init__()
        self.failing = failing

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        if number in self.failing:
            raise GitHubError(f"HTTP 502 on issue {number}", status=502)
        super().add_labels(number, labels)


def test_failed_changes_are_marked_and_not_counted_as_changed() -> None:
    fake = FailOnIssue({2})
    fake.create_label("forge:task", "5319e7")
    for task_id in ("M1-T01", "M1-T02", "M1-T03"):
        fake.create_issue(task_id, body(meta(task_id)), labels=["forge:task"])
    plan = plan_refresh(fake.list_issues(state="all"), {}, set())
    failures = apply_refresh(fake, plan)
    assert list(failures) == [2]
    assert failures[2].startswith("issue #2 (M1-T02): label update failed")
    data = plan.to_data(failed=failures.keys())
    assert [(c["issue"], c["applied"]) for c in data["changes"]] == [
        (1, True),
        (2, False),
        (3, True),
    ]
    assert data["counts"]["changed"] == 2 and data["counts"]["failed"] == 1
    assert plan.counts["changed"] == 3  # the plan itself still counts every planned change


# --------------------------------------------------------------------------- gather


def push_event(
    world: GitWorld, git: Git, branch: str, parent: str, trailers: dict[str, str]
) -> str:
    sha = git.commit_tree(git.tree_of(parent), [parent], f"chore: {branch}", trailers)
    result = git.push(sha, f"refs/heads/{branch}")
    assert result.ok, result.stderr
    return sha


def claim_of(task_id: str, who: str = "alice") -> dict[str, str]:
    return {TRAILER_TASK: task_id, TRAILER_CLAIM: who, TRAILER_AGENT: "claude"}


def commit_at(git: Git, parents: Sequence[str], when: str, trailers: Mapping[str, str]) -> str:
    """commit-tree with a fixed committer date, so ``git log`` ordering is deterministic."""
    args = ["commit-tree", git.tree_of(parents[0])]
    for parent in parents:
        args += ["-p", parent]
    message = "chore: event\n\n" + "\n".join(f"{k}: {v}" for k, v in trailers.items()) + "\n"
    env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    return git.run(*args, input=message, env=env).stdout.strip()


def states_of(scan_branches: Mapping[int, TaskBranch], ids: Mapping[int, str]) -> dict[int, object]:
    return {n: scan_branches[n].state(ids[n]) for n in scan_branches}


def test_gather_task_branches(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    main = git_world.initial_sha

    # a claim that reached main (e.g. via a merge commit) must not count for new branches
    merged_claim = push_event(git_world, alice, "main", main, claim_of("M1-T01"))

    t1 = push_event(git_world, alice, "task/1", merged_claim, claim_of("M1-T01"))

    c2 = push_event(git_world, alice, "task/2", merged_claim, claim_of("M1-T02"))
    work = alice.commit_tree(alice.tree_of(c2), [c2], "feat: work")
    t2 = push_event(
        git_world, alice, "task/2", work, {TRAILER_TASK: "M1-T02", TRAILER_HANDOFF: "alice"}
    )

    c3 = push_event(git_world, alice, "task/3", merged_claim, claim_of("M1-T03"))
    t3 = push_event(
        git_world, alice, "task/3", c3, {TRAILER_TASK: "M1-T03", TRAILER_RELEASE: "alice"}
    )

    t4 = alice.commit_tree(alice.tree_of(merged_claim), [merged_claim], "feat: no trailers")
    assert alice.push(t4, "refs/heads/task/4").ok

    for other in ("feature/x", "task/abc", "task/07"):
        assert alice.push(t1, f"refs/heads/{other}").ok

    bob = git_world.clone("bob")
    fetch_task_refs(bob, "main")
    scan = gather_task_branches(bob, "main")

    assert scan.warnings == ()
    ids = {1: "M1-T01", 2: "M1-T02", 3: "M1-T03", 4: "M1-T04"}
    assert states_of(scan.branches, ids) == {
        1: BranchState(True, t1, BranchEvent.CLAIM, "alice", "claude"),
        2: BranchState(True, t2, BranchEvent.HANDOFF, "alice", None),
        3: BranchState(True, t3, BranchEvent.RELEASE, "alice", None),
        4: BranchState(True, t4, None, None, None),
    }


def test_gather_task_branches_none(git_world: GitWorld) -> None:
    bob = git_world.clone("bob")
    assert gather_task_branches(bob, "main") == BranchScan({}, ())


def test_other_task_event_on_branch_is_not_attributed(git_world: GitWorld) -> None:
    """A commit carrying another task's event (e.g. cherry-picked) does not count."""
    alice = git_world.clone("alice")
    c2 = push_event(git_world, alice, "task/2", git_world.initial_sha, claim_of("M1-T02"))
    foreign = {TRAILER_TASK: "M1-T03", TRAILER_RELEASE: "bob"}
    c3 = push_event(git_world, alice, "task/2", c2, foreign)
    push_event(git_world, alice, "task/2", c3, {TRAILER_RELEASE: "carol"})  # no Forge-Task

    bob = git_world.clone("bob")
    fetch_task_refs(bob, "main")
    scan = gather_task_branches(bob, "main")
    state = scan.branches[2].state("M1-T02")
    assert (state.last_event, state.event_author) == (BranchEvent.CLAIM, "alice")
    # through the plan: the task stays in progress, not ready
    t2 = issue(2, meta("M1-T02"), labels=with_status(Status.IN_PROGRESS))
    assert plan_refresh([t2], scan.branches, set()).changes == ()


def test_events_follow_first_parent_only(git_world: GitWorld) -> None:
    """A newer release merged in from a side branch does not override the branch's claim."""
    alice = git_world.clone("alice")
    base = git_world.initial_sha
    claim = commit_at(alice, [base], "2026-01-01T00:00:00Z", claim_of("M1-T02"))
    side = commit_at(
        alice, [base], "2026-01-02T00:00:00Z", {TRAILER_TASK: "M1-T02", TRAILER_RELEASE: "bob"}
    )
    merge = commit_at(alice, [claim, side], "2026-01-03T00:00:00Z", {})
    assert alice.push(merge, "refs/heads/task/2").ok

    # without --first-parent, the side release would be the newest event
    every = [c.trailers for c in alice.commits(f"{base}..{merge}")]
    assert last_event_from_trailers(every, task_id="M1-T02") == ForgeEvent(
        BranchEvent.RELEASE, "bob", None, "M1-T02"
    )

    bob = git_world.clone("bob")
    fetch_task_refs(bob, "main")
    scan = gather_task_branches(bob, "main")
    state = scan.branches[2].state("M1-T02")
    assert (state.last_event, state.event_author) == (BranchEvent.CLAIM, "alice")


def test_branch_without_common_history_warns_and_is_absent(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    orphan = alice.commit_tree(alice.tree_of("HEAD"), [], "orphan", claim_of("M1-T05"))
    assert alice.push(orphan, "refs/heads/task/5").ok
    push_event(git_world, alice, "task/6", git_world.initial_sha, claim_of("M1-T06"))

    bob = git_world.clone("bob")
    fetch_task_refs(bob, "main")
    scan = gather_task_branches(bob, "main")
    assert set(scan.branches) == {6}
    (warning,) = scan.warnings
    assert "task/5" in warning and "treated as absent" in warning

    t5 = issue(5, meta("M1-T05"), labels=with_status(Status.IN_PROGRESS))
    plan = plan_refresh([t5], scan.branches, set(), warnings=scan.warnings)
    assert by_issue(plan) == {5: (Status.IN_PROGRESS, Status.READY)}
    assert plan.warnings[0] == warning and plan.counts["warnings"] == 1


def test_missing_default_branch_ref_is_a_precondition(git_world: GitWorld) -> None:
    bob = git_world.clone("bob")
    with pytest.raises(PreconditionError) as excinfo:
        gather_task_branches(bob, "trunk")
    assert excinfo.value.code == ExitCode.PRECONDITION
    assert "project.default_branch" in (excinfo.value.hint or "")
    assert ".forge/config.yml" in (excinfo.value.hint or "")


def test_fetch_missing_default_branch_on_remote_is_a_precondition(git_world: GitWorld) -> None:
    bob = git_world.clone("bob")
    with pytest.raises(PreconditionError) as excinfo:
        fetch_task_refs(bob, "trunk")
    assert "trunk" in excinfo.value.message
    assert "project.default_branch" in (excinfo.value.hint or "")


def test_fetch_failure_is_wrapped_with_a_hint(git_world: GitWorld, tmp_path: Path) -> None:
    bob = git_world.clone("bob")
    bob.run("remote", "set-url", "origin", str(tmp_path / "gone.git"))
    with pytest.raises(ForgeError) as excinfo:
        fetch_task_refs(bob, "main")
    assert not isinstance(excinfo.value, PreconditionError)
    assert excinfo.value.code == ExitCode.ERROR
    assert "could not fetch from origin" in excinfo.value.message
    assert "reachable" in (excinfo.value.hint or "")


def test_single_branch_clone_sees_task_branches(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = push_event(git_world, alice, "task/1", git_world.initial_sha, claim_of("M1-T01"))
    path = git_world.base / "single"
    Git(git_world.base).run(
        "clone", "--quiet", "--single-branch", "--branch", "main", str(git_world.remote), str(path)
    )
    single = Git(path)
    single.fetch()
    assert gather_task_branches(single, "main").branches == {}

    fetch_task_refs(single, "main")
    scan = gather_task_branches(single, "main")
    assert scan.branches[1].tip == tip
    assert scan.branches[1].state("M1-T01").last_event is BranchEvent.CLAIM


def shallow_clone(world: GitWorld, name: str) -> Git:
    seed = Git(world.base / "seed")
    world.commit_file(seed, "more.txt", "more\n", "feat: more history")
    world.push(seed, "main")
    path = world.base / name
    Git(world.base).run("clone", "--quiet", "--depth", "1", world.remote.as_uri(), str(path))
    return Git(path)


def test_shallow_clone_is_a_precondition(git_world: GitWorld) -> None:
    check_full_history(git_world.clone("full"))
    shallow = shallow_clone(git_world, "ci")
    with pytest.raises(PreconditionError) as excinfo:
        check_full_history(shallow)
    hint = excinfo.value.hint or ""
    assert "fetch-depth: 0" in hint and "git fetch --unshallow" in hint


# --------------------------------------------------------------------------- run


CONFIG = """\
forge_version: ">=0.1"
project:
  name: shop
  default_branch: main
github:
  repo: acme/shop
"""


@dataclass
class Ctx:
    config: ForgeConfig
    git: Git
    github: GitHubClient


def make_ctx(world: GitWorld, user: str, github: GitHubClient) -> Ctx:
    git = world.clone(user)
    (git.cwd / ".forge").mkdir()
    (git.cwd / ".forge" / "config.yml").write_text(CONFIG)
    return Ctx(load_config(git.cwd), git, github)


HookPoint = str
HOOK_POINTS: tuple[HookPoint, ...] = (
    "before_list_issues",
    "after_list_issues",
    "after_list_pulls",
    "before_fetch",
    "after_fetch",
)


class HookedGitHub(FakeGitHub):
    """FakeGitHub that runs ``hooks[point]`` around the reads refresh makes."""

    def __init__(self) -> None:
        super().__init__(repo="acme/shop")
        self.hooks: dict[HookPoint, Callable[[], None]] = {}
        self.create_label("forge:task", "5319e7")

    def fire(self, point: HookPoint) -> None:
        hook = self.hooks.pop(point, None)
        if hook is not None:
            hook()

    def list_issues(self, *args: Any, **kwargs: Any) -> list[Issue]:
        self.fire("before_list_issues")
        issues = super().list_issues(*args, **kwargs)
        self.fire("after_list_issues")
        return issues

    def list_pulls(self, *args: Any, **kwargs: Any) -> list[PullRequest]:
        pulls = super().list_pulls(*args, **kwargs)
        self.fire("after_list_pulls")
        return pulls


class HookedGit(Git):
    def __init__(self, cwd: Path, github: HookedGitHub):
        super().__init__(cwd)
        self.github = github

    def fetch(self, *args: Any, **kwargs: Any) -> None:
        self.github.fire("before_fetch")
        super().fetch(*args, **kwargs)
        self.github.fire("after_fetch")


@pytest.mark.parametrize("point", HOOK_POINTS)
def test_concurrent_claim_label_is_never_removed(git_world: GitWorld, point: HookPoint) -> None:
    """Bob claims T1 (branch + label) at every point of alice's refresh; the label survives."""
    fake = HookedGitHub()
    ctx = make_ctx(git_world, "alice", fake)
    ctx.git = HookedGit(ctx.git.cwd, fake)
    t1 = fake.create_issue("[M1-T01]", body(meta("M1-T01")), labels=["forge:task"])
    fake.create_issue("[M1-T02]", body(meta("M1-T02", ["M1-T01"])), labels=["forge:task"])
    run_refresh(ctx)
    assert set(fake.get_issue(t1.number).labels) == {*MIRRORS, "status:ready"}

    bob = git_world.clone("bob")

    def bob_claims() -> None:
        ref = f"refs/heads/task/{t1.number}"
        base = bob.rev_parse("origin/main")
        assert base is not None
        sha = bob.commit_tree(bob.tree_of(base), [base], "forge: claim", claim_of("M1-T01", "bob"))
        assert bob.push(sha, ref, lease=Lease(ref, None)).ok
        fake.add_labels(t1.number, ["status:in-progress"])
        fake.remove_label(t1.number, "status:ready")

    fake.hooks[point] = bob_claims
    result = run_refresh(ctx)
    assert not fake.hooks, "the hook point was never reached"
    assert result.code == ExitCode.OK
    assert set(fake.get_issue(t1.number).labels) == {*MIRRORS, "status:in-progress"}

    # a re-run converges and changes nothing
    again = run_refresh(ctx)
    assert again.data["changes"] == []


def test_run_refresh_shallow_fails_before_any_github_call(git_world: GitWorld) -> None:
    fake = HookedGitHub()
    ctx = make_ctx(git_world, "alice", fake)
    ctx.git = shallow_clone(git_world, "ci")

    def boom() -> None:
        raise AssertionError("GitHub must not be read on a shallow clone")

    fake.hooks["before_list_issues"] = boom
    with pytest.raises(PreconditionError):
        run_refresh(ctx)
