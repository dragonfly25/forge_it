"""Tests for forge.core.refresh: the pure plan, apply on FakeGitHub and branch gathering."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from forge.core.git import Git
from forge.core.github import Issue
from forge.core.github_fake import FakeGitHub
from forge.core.refresh import (
    RefreshPlan,
    apply_refresh,
    ensure_labels,
    gather_branch_states,
    plan_refresh,
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
    Size,
    Status,
    TaskMetadata,
    TaskType,
    upsert_block,
)
from forge.core.tiers import Tier

if TYPE_CHECKING:
    from conftest import GitWorld

NOW = datetime(2026, 11, 2, tzinfo=UTC)
MIRRORS = ("forge:task", "size:S", "tier:standard", "type:impl")
CLAIMED = BranchState(True, "abc", BranchEvent.CLAIM, "alice", "claude")
HANDED_OFF = BranchState(True, "abc", BranchEvent.HANDOFF, "alice", None)
RELEASED = BranchState(True, "abc", BranchEvent.RELEASE, "alice", None)


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
    states = {2: CLAIMED, 3: HANDED_OFF, 4: RELEASED}
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
    plan = plan_refresh(issues, {1: CLAIMED}, {"task/1", "feature/x"})
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
    plan = plan_refresh([t], {1: CLAIMED}, set())
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
        }
    ]
    assert data["counts"]["changed"] == 1


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

    plan = plan_refresh(fake.list_issues(state="all"), {1: CLAIMED}, set())
    assert len(plan.changes) == 4
    apply_refresh(fake, plan)

    labels = {i.number: set(i.labels) for i in fake.list_issues(state="all")}
    assert labels[1] == {*MIRRORS, "status:in-progress"}
    assert labels[2] == {*MIRRORS, "status:blocked", "bug"}
    assert labels[3] == {*MIRRORS, "status:done"}
    assert labels[4] == {*MIRRORS, "status:ready"}

    again = plan_refresh(fake.list_issues(state="all"), {1: CLAIMED}, set())
    assert again.changes == ()


# --------------------------------------------------------------------------- gather


def push_event(
    world: GitWorld, git: Git, branch: str, parent: str, trailers: dict[str, str]
) -> str:
    sha = git.commit_tree(git.tree_of(parent), [parent], f"chore: {branch}", trailers)
    result = git.push(sha, f"refs/heads/{branch}")
    assert result.ok, result.stderr
    return sha


def test_gather_branch_states(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    main = git_world.initial_sha
    claim = {TRAILER_TASK: "M1-T01", TRAILER_CLAIM: "alice", TRAILER_AGENT: "claude"}

    # a claim that reached main (e.g. via a merge commit) must not count for new branches
    merged_claim = push_event(git_world, alice, "main", main, claim)

    t1 = push_event(git_world, alice, "task/1", merged_claim, claim)

    c2 = push_event(git_world, alice, "task/2", merged_claim, claim)
    work = alice.commit_tree(alice.tree_of(c2), [c2], "feat: work")
    t2 = push_event(
        git_world, alice, "task/2", work, {TRAILER_TASK: "M1-T02", TRAILER_HANDOFF: "alice"}
    )

    c3 = push_event(git_world, alice, "task/3", merged_claim, claim)
    t3 = push_event(
        git_world, alice, "task/3", c3, {TRAILER_TASK: "M1-T03", TRAILER_RELEASE: "alice"}
    )

    t4 = alice.commit_tree(alice.tree_of(merged_claim), [merged_claim], "feat: no trailers")
    assert alice.push(t4, "refs/heads/task/4").ok

    for other in ("feature/x", "task/abc", "task/07"):
        assert alice.push(t1, f"refs/heads/{other}").ok

    bob = git_world.clone("bob")
    bob.fetch()
    states = gather_branch_states(bob, "main")

    assert states == {
        1: BranchState(True, t1, BranchEvent.CLAIM, "alice", "claude"),
        2: BranchState(True, t2, BranchEvent.HANDOFF, "alice", None),
        3: BranchState(True, t3, BranchEvent.RELEASE, "alice", None),
        4: BranchState(True, t4, None, None, None),
    }


def test_gather_branch_states_none(git_world: GitWorld) -> None:
    bob = git_world.clone("bob")
    assert gather_branch_states(bob, "main") == {}
