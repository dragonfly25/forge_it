"""Tests for forge.core.handoff (spec §8.3, ADR 0012).

Git runs against real temporary repositories (a bare remote plus one clone per simulated
user) and GitHub is a shared ``FakeGitHub`` seen through one proxy per user
(``support.sync_world``). Tasks are claimed through the real ``forge next`` core and checked
out in place. Races are interleaved deterministically with hooks on the clones' pushes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forge.core.claim import ClaimContext
from forge.core.context import HANDOFF_MARKER, latest_handoff
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.git import Git, GitError, PushResult
from forge.core.github import GitHubError
from forge.core.handoff import (
    MAX_COMMENT_CHARS,
    REQUIRED_HEADINGS,
    HandoffOutcome,
    handoff,
    missing_headings,
    render_handoff_comment,
)
from forge.core.lease import LeaseData, find_claim, heartbeat, render_lease
from forge.core.profile import AgentProfile
from forge.core.refresh import gather_task_branches
from forge.core.tasks import (
    TRAILER_AGENT,
    TRAILER_HANDOFF,
    TRAILER_RUN,
    TRAILER_TASK,
    Size,
    Status,
    derive_status,
)
from support.scenarios import AGENT, T0, Clock, HookedGit, run
from support.ship_world import ShipWorld
from support.sync_world import SyncWorld, status

if TYPE_CHECKING:
    from conftest import GitWorld

NOTE = """\
## Done
- the API answers

## Remaining
- pagination

## Decisions
- cursor-based pages

## Gotchas
- the fixture is slow

## Next step
- write the pagination test

## How to verify
- uv run pytest -q
"""

LATER = T0 + timedelta(hours=2)


@pytest.fixture
def sw(git_world: GitWorld) -> SyncWorld:
    return SyncWorld(git_world)


def hctx(
    sw: SyncWorld, user: str = "alice", *, now: datetime = LATER, clone: str | None = None
) -> ClaimContext:
    """A claim context for login ``user`` working in clone ``clone`` (default: ``user``)."""
    return sw.world.ctx(user, clock=Clock(now), clone=clone)


def claimed(
    sw: SyncWorld, task_id: str = "M1-T01", zones: tuple[str, ...] = ("api",)
) -> tuple[int, HookedGit]:
    issue = sw.task(task_id, zones)
    git = sw.claim("alice", issue)
    return issue, git


def remote(sw: SyncWorld) -> Git:
    return sw.world.git_world.remote_git


def head(git: Git) -> str:
    sha = git.rev_parse("HEAD")
    assert sha is not None
    return sha


def handoff_comments(sw: SyncWorld, issue: int) -> list[str]:
    return [c.body for c in sw.fake.list_comments(issue) if HANDOFF_MARKER in c.body]


def lease_data(sw: SyncWorld, issue: int) -> LeaseData:
    found = sw.lease(issue)
    assert found.data is not None
    return found.data


def derived(sw: SyncWorld, issue: int, task_id: str) -> Status | None:
    """The ADR 0008 derived status, from the remote branch as anyone fetching sees it."""
    git = sw.world.git("observer")
    git.fetch()
    branch = gather_task_branches(git, "main").branches.get(issue)
    assert branch is not None
    return derive_status(
        issue=sw.fake.get_issue(issue),
        meta=None,
        deps_closed={},
        branch=branch.state(task_id),
        has_open_pr=False,
    ).status


def install_hook(git: Git, name: str, script: str) -> None:
    hooks = Path(git.run("rev-parse", "--git-path", "hooks").stdout.strip())
    if not hooks.is_absolute():
        hooks = git.cwd / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    path = hooks / name
    path.write_text("#!/bin/sh\n" + script + "\n")
    path.chmod(0o755)


def status_labels(sw: SyncWorld, issue: int) -> list[str]:
    return sorted(label for label in sw.labels(issue) if label.startswith("status:"))


# --------------------------------------------------------------------------- the note


def test_missing_headings_is_case_insensitive_and_ignores_code() -> None:
    assert missing_headings(NOTE) == []
    note = (
        "# DONE\n**Remaining:**\n### decisions taken\n## Gotchas\n"
        "```\n## Next step\n```\n## How to run/verify\n"
    )
    assert missing_headings(note) == ["Next step"]
    assert missing_headings("just text, no headings") == list(REQUIRED_HEADINGS)
    # a word that merely starts like a heading does not count
    assert "Done" in missing_headings("## Doneness\n")


def test_comment_shape() -> None:
    body = render_handoff_comment(
        "\n## Done\nx\n\n", login="alice", agent="claude", at=T0, branch="task/3", tip="a" * 40
    )
    lines = body.splitlines()
    assert lines[0] == HANDOFF_MARKER
    assert lines[1] == "**Forge handoff** by alice (agent claude) at 2026-11-02T09:00:00Z"
    assert lines[2] == f"Branch `task/3`, tip `{'a' * 40}`"
    assert lines[3:] == ["", "## Done", "x"]


# --------------------------------------------------------------------------- the happy path


def test_handoff_pushes_work_comments_unassigns_and_labels(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    remote_before = sw.remote_tip(n)
    local = sw.commit("alice", "src/api/a.py", "a\n", "feat: local work")
    ctx = hctx(sw)

    out = handoff(ctx, n, NOTE)

    assert out.code is ExitCode.OK and out.outcome == "handed_off" and not out.recovered
    tip = sw.remote_tip(n)
    assert tip == out.handoff_commit and head(git) == tip  # the local branch follows
    assert status(git) == ""
    assert remote(sw).rev_parse(f"{tip}^") == local  # one push carried the work too
    assert out.commits == 1 and out.wip_commit is None
    assert remote(sw).is_ancestor(remote_before or "", tip or "")
    # the event commit: empty, with the four trailers, dated by the injected clock
    event = remote(sw).commits(f"{tip}^!")[0]
    assert event.subject == "forge: handoff M1-T01"
    assert event.trailers[TRAILER_HANDOFF] == "alice"
    assert event.trailers[TRAILER_AGENT] == "claude"
    assert event.trailers[TRAILER_TASK] == "M1-T01"
    assert len(event.trailers[TRAILER_RUN]) == 32
    assert event.date == LATER
    assert remote(sw).tree_of(tip or "") == remote(sw).tree_of(local)
    # pushed with hooks enabled and an explicit lease
    pushes = git.pushes
    assert pushes[-1] == (tip, f"refs/heads/task/{n}", False)
    # the comment
    comments = handoff_comments(sw, n)
    assert len(comments) == 1
    assert comments[0].startswith(
        f"{HANDOFF_MARKER}\n**Forge handoff** by alice (agent claude) at 2026-11-02T11:00:00Z\n"
        f"Branch `task/{n}`, tip `{tip}`\n\n## Done"
    )
    note = latest_handoff(sw.fake.list_comments(n))
    assert note is not None and "## How to verify" in note.body
    assert out.comment_id == note.comment_id and out.comment_created
    # issue: unassigned, status:handoff only
    issue = sw.fake.get_issue(n)
    assert issue.assignees == ()
    assert status_labels(sw, n) == ["status:handoff"]
    assert out.status is Status.HANDOFF
    # lease: released_at set, the rest kept
    lease = lease_data(sw, n)
    assert lease.released_at == LATER and lease.owner == "alice" and lease.claimed_at == T0
    # the derived status, as anyone fetching sees it
    assert derived(sw, n, "M1-T01") is Status.HANDOFF
    assert out.missing_headings == () and out.warnings == ()


def test_dirty_tree_is_saved_as_a_wip_commit_in_the_same_push(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    (git.cwd / "src" / "api" / "a.py").write_text("changed\n")
    (git.cwd / "src" / "api" / "new.py").write_text("untracked\n")

    out = handoff(hctx(sw), n, NOTE)

    assert out.code is ExitCode.OK
    tip = sw.remote_tip(n)
    assert out.wip_commit is not None and remote(sw).rev_parse(f"{tip}^") == out.wip_commit
    assert remote(sw).commits(f"{out.wip_commit}^!")[0].subject == "forge: wip"
    assert out.commits == 2
    assert status(git) == ""
    shown = remote(sw).run("show", f"{tip}:src/api/new.py").stdout
    assert shown == "untracked\n"
    assert len(git.pushes) == 2  # the claim push, then exactly one handoff push


def test_handoff_with_nothing_new_still_pushes_the_event(sw: SyncWorld) -> None:
    n, _git = claimed(sw)
    before = sw.remote_tip(n)
    out = handoff(hctx(sw), n, NOTE)
    assert out.commits == 0
    assert remote(sw).rev_parse(f"{sw.remote_tip(n)}^") == before
    assert derived(sw, n, "M1-T01") is Status.HANDOFF


def test_missing_headings_are_only_a_warning(sw: SyncWorld) -> None:
    n, _git = claimed(sw)
    out = handoff(hctx(sw), n, "## Done\nhalf of it\n")
    assert out.code is ExitCode.OK
    assert out.missing_headings == (
        "Remaining",
        "Decisions",
        "Gotchas",
        "Next step",
        "How to verify",
    )
    assert any("lacks the §8.3 heading(s): Remaining" in w for w in out.warnings)
    assert len(handoff_comments(sw, n)) == 1


def test_my_own_stale_claim_can_be_handed_off(sw: SyncWorld) -> None:
    n, _git = claimed(sw)
    out = handoff(hctx(sw, now=T0 + timedelta(hours=30)), n, NOTE)
    assert out.code is ExitCode.OK
    assert any("had gone stale at" in w for w in out.warnings)
    assert derived(sw, n, "M1-T01") is Status.HANDOFF


def test_clock_behind_the_claim_still_hands_off(sw: SyncWorld) -> None:
    """Clock skew: the event commit is dated before the claim. Events are ordered by the
    first-parent history, never by date, so the handoff is still the last event."""
    n, _git = claimed(sw)
    out = handoff(hctx(sw, now=T0 - timedelta(hours=3)), n, NOTE)
    assert out.code is ExitCode.OK
    assert derived(sw, n, "M1-T01") is Status.HANDOFF
    assert lease_data(sw, n).released_at == T0 - timedelta(hours=3)
    taken = run(sw.world.ctx("bob", clock=Clock(LATER)), task=str(n))
    assert taken.claimed is not None and taken.claimed.resumed


# --------------------------------------------------------------------------- refusals


def test_empty_note_is_an_error_before_anything_changes(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    (git.cwd / "src").mkdir(exist_ok=True)
    (git.cwd / "src" / "x.py").write_text("x\n")
    before = sw.remote_tip(n)
    with pytest.raises(ForgeError, match="note is empty") as info:
        handoff(hctx(sw), n, " \n\t\n")
    assert info.value.code is ExitCode.ERROR
    assert sw.remote_tip(n) == before and "?? src/x.py" in status(git)


def test_oversized_note_is_refused_before_anything_changes(sw: SyncWorld) -> None:
    n, _git = claimed(sw)
    before = sw.remote_tip(n)
    with pytest.raises(ForgeError, match="too long"):
        handoff(hctx(sw), n, "## Done\n" + "x" * MAX_COMMENT_CHARS)
    assert sw.remote_tip(n) == before


def test_someone_elses_claim_is_refused(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ("api",))
    sw.claim("bob", n)
    sw.checkout("alice", n)
    before = sw.remote_tip(n)
    with pytest.raises(PreconditionError, match="belongs to bob"):
        handoff(hctx(sw), n, NOTE)
    # stale or not
    with pytest.raises(PreconditionError, match="belongs to bob"):
        handoff(hctx(sw, now=T0 + timedelta(hours=30)), n, NOTE)
    assert sw.remote_tip(n) == before
    assert handoff_comments(sw, n) == []


def test_another_agent_of_mine_is_refused(sw: SyncWorld) -> None:
    n, _git = claimed(sw)
    codex = AgentProfile(id="codex", tool="codex", providers=("openai",), tiers=AGENT.tiers)
    other = sw.world.ctx("alice", clock=Clock(LATER), agent=codex)
    with pytest.raises(PreconditionError, match="not to alice with agent codex"):
        handoff(other, n, NOTE)


def test_task_without_a_branch_is_refused(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ("api",))
    with pytest.raises(PreconditionError, match="does not exist"):
        handoff(hctx(sw), n, NOTE)


def test_incomplete_claim_is_refused(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    found = sw.lease(n)
    assert found.data is not None
    other = LeaseData(found.data.task, "alice", "claude", "f" * 40, T0, T0, run="0" * 32)
    sw.fake.update_comment(found.comment.id, render_lease(other, branch=f"task/{n}"))
    before = sw.remote_tip(n)
    with pytest.raises(PreconditionError, match="incomplete") as info:
        handoff(hctx(sw), n, NOTE)
    assert info.value.hint is not None and f"forge next --task {n}" in info.value.hint
    assert sw.remote_tip(n) == before and head(git) == before


def test_diverged_local_branch_is_refused(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    sw.commit("alice", "src/api/mine.py", "mine\n")
    # the same login pushed from another clone meanwhile
    pushed = sw.world.push_work("alice-laptop", n, "src/api/laptop.py", T0 + timedelta(minutes=5))
    with pytest.raises(PreconditionError, match="does not contain origin/task") as info:
        handoff(hctx(sw), n, NOTE)
    assert info.value.code is ExitCode.PRECONDITION
    assert sw.remote_tip(n) == pushed
    assert handoff_comments(sw, n) == []
    assert status(git) == ""


def test_sync_in_progress_is_refused(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    state = git.cwd / ".forge" / "local" / "sync.json"
    state.write_text("{}")
    with pytest.raises(PreconditionError, match="a sync is in progress"):
        handoff(hctx(sw), n, NOTE)


def test_branch_not_checked_out_is_refused(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    git.run("checkout", "--quiet", "main")
    with pytest.raises(PreconditionError, match="is not checked out"):
        handoff(hctx(sw), n, NOTE)


# --------------------------------------------------------------------------- hooks


def test_commit_hook_rejecting_the_wip_commit_is_exit_2(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    (git.cwd / "src").mkdir(exist_ok=True)
    (git.cwd / "src" / "x.py").write_text("x\n")
    install_hook(git, "pre-commit", "echo 'lint: x.py is ugly' >&2; exit 1")
    before = sw.remote_tip(n)

    out = handoff(hctx(sw), n, NOTE)

    assert out.code is ExitCode.AGENT_ACTION and out.outcome == "wip_rejected"
    assert out.output is not None and "x.py is ugly" in out.output
    assert sw.remote_tip(n) == before and head(git) == before
    assert status(git) == "?? src/x.py\n"  # unstaged again
    assert handoff_comments(sw, n) == []
    assert lease_data(sw, n).released_at is None


def test_pre_push_hook_rejection_is_exit_2_and_undoes_the_wip(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    work = sw.commit("alice", "src/api/a.py", "a\n")
    (git.cwd / "src" / "api" / "a.py").write_text("dirty\n")
    install_hook(git, "pre-push", "echo 'tests failed' >&2; exit 1")
    before = sw.remote_tip(n)

    out = handoff(hctx(sw), n, NOTE)

    assert out.code is ExitCode.AGENT_ACTION and out.outcome == "push_rejected"
    assert out.output is not None and "tests failed" in out.output
    assert out.claim is not None and out.error is not None and "wip commit was undone" in out.error
    assert sw.remote_tip(n) == before
    assert head(git) == work and status(git) == " M src/api/a.py\n"
    assert handoff_comments(sw, n) == []
    assert sw.fake.get_issue(n).assignees == ("alice",)
    assert status_labels(sw, n) == ["status:in-progress"]
    # fixed: the re-run goes through
    install_hook(git, "pre-push", "exit 0")
    again = handoff(hctx(sw), n, NOTE)
    assert again.code is ExitCode.OK and again.wip_commit is not None


# --------------------------------------------------------------------------- races


def test_lease_rejected_when_someone_reclaims_first(sw: SyncWorld) -> None:
    """Alice's stale claim: bob reclaims it between alice's fetch and alice's push.
    Exactly one winner (bob); alice gets exit 5 and nothing of hers is published."""
    n, git = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    (git.cwd / "src" / "api" / "b.py").write_text("dirty\n")
    original = head(git)
    late = T0 + timedelta(hours=30)

    def bob_reclaims(src: str, dst: str) -> None:
        git.before_push = None
        taken = run(sw.world.ctx("bob", clock=Clock(late)), task=str(n), reclaim=True)
        assert taken.claimed is not None and taken.claimed.reclaimed

    git.before_push = bob_reclaims
    out = handoff(hctx(sw, now=late), n, NOTE)

    assert out.code is ExitCode.PRECONDITION and out.outcome == "lease_rejected"
    assert out.error is not None and "moved on origin" in out.error
    assert head(git) == original and status(git) == "?? src/api/b.py\n"  # wip undone
    # bob is the only owner
    tip = sw.remote_tip(n)
    branch = gather_task_branches(remote_clone(sw), "main").branches[n]
    claim = find_claim(n, branch, "M1-T01")
    assert claim is not None and claim.owner == "bob" and claim.tip == tip
    assert lease_data(sw, n).owner == "bob" and lease_data(sw, n).released_at is None
    assert sw.fake.get_issue(n).assignees == ("bob",)
    assert status_labels(sw, n) == ["status:in-progress"]
    assert handoff_comments(sw, n) == []


def remote_clone(sw: SyncWorld) -> Git:
    git = sw.world.git("observer")
    git.fetch()
    return git


def test_same_identity_on_two_clones_one_handoff_wins(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    laptop = sw.checkout("alice-laptop", n)
    results: list[HandoffOutcome] = []

    def laptop_hands_off(src: str, dst: str) -> None:
        git.before_push = None
        results.append(handoff(hctx(sw, clone="alice-laptop"), n, NOTE))

    git.before_push = laptop_hands_off
    out = handoff(hctx(sw), n, NOTE)

    assert results[0].code is ExitCode.OK
    assert out.code is ExitCode.PRECONDITION and out.outcome == "lease_rejected"
    tip = sw.remote_tip(n)
    assert tip == results[0].handoff_commit == head(laptop)
    events = [c for c in remote(sw).commits(f"main..{tip}") if TRAILER_HANDOFF in c.trailers]
    assert len(events) == 1
    assert len(handoff_comments(sw, n)) == 1
    # the loser's re-run sees the winner's handoff as its own and completes nothing twice
    again = handoff(hctx(sw), n, NOTE)
    assert again.recovered and again.handoff_commit == tip
    assert sw.remote_tip(n) == tip and len(handoff_comments(sw, n)) == 1
    assert head(git) == tip  # fast-forwarded: nothing local was lost


def test_someone_resumes_between_my_push_and_my_github_steps(sw: SyncWorld) -> None:
    """Bob claims the handed-off task before alice's labels and lease are written. Alice's
    run must not overwrite bob's labels or lease: bob's claim stays complete."""
    n, git = claimed(sw)

    def bob_resumes(src: str, dst: str, result: PushResult) -> None:
        git.after_push = None
        assert result.ok
        taken = run(sw.world.ctx("bob", clock=Clock(LATER)), task=str(n))
        assert taken.claimed is not None and taken.claimed.resumed

    git.after_push = bob_resumes
    out = handoff(hctx(sw), n, NOTE)

    assert out.code is ExitCode.OK and out.status is None
    assert any("someone resumed the task" in w for w in out.warnings)
    assert len(handoff_comments(sw, n)) == 1
    issue = sw.fake.get_issue(n)
    assert issue.assignees == ("bob",)
    assert status_labels(sw, n) == ["status:in-progress"]
    lease = lease_data(sw, n)
    assert lease.owner == "bob" and lease.released_at is None
    beat = heartbeat(sw.world.ctx("bob", clock=Clock(LATER + timedelta(minutes=1))), n)
    assert beat.claim == lease.claim


# --------------------------------------------------------------------------- idempotency


@pytest.mark.parametrize("step", ["create_comment", "remove_assignees", "list_pulls", "add_labels"])
def test_interrupted_after_the_push_rerun_completes_without_a_second_event(
    sw: SyncWorld, step: str
) -> None:
    n, git = claimed(sw)
    claim_tip = sw.remote_tip(n)
    sw.commit("alice", "src/api/a.py", "a\n")
    ctx = hctx(sw)

    def interrupt_the_github_steps(src: str, dst: str, result: PushResult) -> None:
        git.after_push = None
        assert result.ok
        ctx.github.fail[step] = 1  # type: ignore[attr-defined]

    git.after_push = interrupt_the_github_steps  # the next `step` call after the push fails
    with pytest.raises(GitHubError):
        handoff(ctx, n, NOTE)
    tip = sw.remote_tip(n)
    assert tip is not None and tip != claim_tip  # the push landed before the failure
    assert lease_data(sw, n).released_at is None  # not complete yet

    again = handoff(hctx(sw, now=LATER + timedelta(minutes=5)), n, NOTE)

    assert again.code is ExitCode.OK and again.recovered
    assert again.handoff_commit == tip and sw.remote_tip(n) == tip
    assert any("completing an interrupted handoff" in w for w in again.warnings)
    assert len(handoff_comments(sw, n)) == 1
    assert sw.fake.get_issue(n).assignees == ()
    assert status_labels(sw, n) == ["status:handoff"]
    assert lease_data(sw, n).released_at == LATER + timedelta(minutes=5)
    assert head(git) == tip


def test_interrupted_before_the_local_branch_moved(sw: SyncWorld) -> None:
    """Killed right after the push: the local branch still points below the event commit."""
    n, git = claimed(sw)
    work = sw.commit("alice", "src/api/a.py", "a\n")
    ctx = hctx(sw)
    ctx.github.fail["create_comment"] = 1  # type: ignore[attr-defined]
    with pytest.raises(GitHubError):
        handoff(ctx, n, NOTE)
    git.update_ref(f"refs/heads/task/{n}", work)

    again = handoff(hctx(sw), n, NOTE)

    assert again.recovered and head(git) == sw.remote_tip(n) and status(git) == ""


def test_rerun_after_completion_keeps_released_at_and_posts_nothing(sw: SyncWorld) -> None:
    n, _git = claimed(sw)
    first = handoff(hctx(sw), n, NOTE)
    comments = len(sw.fake.list_comments(n))

    again = handoff(hctx(sw, now=LATER + timedelta(hours=1)), n, NOTE)

    assert again.recovered and not again.comment_created
    assert again.comment_id == first.comment_id
    assert again.handoff_commit == first.handoff_commit == sw.remote_tip(n)
    assert len(sw.fake.list_comments(n)) == comments
    assert lease_data(sw, n).released_at == LATER


def test_rerun_with_new_local_commits_is_exit_2_listing_them(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    first = handoff(hctx(sw), n, NOTE)
    late = sw.commit("alice", "src/api/late.py", "late\n", "feat: late work")
    again = handoff(hctx(sw), n, NOTE)
    assert again.recovered
    assert again.code is ExitCode.AGENT_ACTION and again.outcome == "unpublished_work"
    assert again.unpublished == (f"commit {late[:12]} feat: late work",)
    assert again.error is not None and f"commit {late[:12]} feat: late work" in again.error
    assert again.hint is not None and f"forge next --task {n}" in again.hint
    assert again.to_data()["unpublished"] == [f"commit {late[:12]} feat: late work"]
    # nothing local was touched, nothing was pushed, the GitHub steps are complete
    assert head(git) == late and sw.remote_tip(n) == first.handoff_commit
    assert len(handoff_comments(sw, n)) == 1
    assert lease_data(sw, n).released_at == LATER


def test_rerun_after_someone_resumed_is_refused(sw: SyncWorld) -> None:
    n, _git = claimed(sw)
    handoff(hctx(sw), n, NOTE)
    run(sw.world.ctx("bob", clock=Clock(LATER)), task=str(n))
    with pytest.raises(PreconditionError, match="belongs to bob"):
        handoff(hctx(sw), n, NOTE)


# --------------------------------------------------------------------------- next


def test_next_prefers_the_handed_off_task(sw: SyncWorld) -> None:
    sw.task("M1-T02", ("web",))  # earlier milestone order would win without the handoff
    later = sw.world.task("M9-T01", ["api"], size=Size.M)
    sw.claim("alice", later)
    handoff(hctx(sw), later, NOTE)

    out = run(sw.world.ctx("bob", clock=Clock(LATER)))

    assert out.claimed is not None and out.claimed.issue == later
    assert out.claimed.resumed and not out.claimed.reclaimed
    parent = remote(sw).rev_parse(f"{out.claimed.claim}^")
    branch = gather_task_branches(remote_clone(sw), "main").branches[later]
    assert parent == branch.commits[1].sha
    assert branch.commits[1].trailers[TRAILER_HANDOFF] == "alice"


# --------------------------------------------------------------------------- open pull request


def close_pull(sw: SyncWorld, number: int) -> None:
    """Close a pull request without merging it."""
    sw.fake.fake_close_pull(number)


def test_open_pr_refuses_the_handoff_until_it_is_closed(git_world: GitWorld) -> None:
    sw = ShipWorld(git_world)
    n = sw.task("M1-T01", ("api",))
    git = sw.claim("alice", n)
    sw.commit("alice", "src/api/a.py", "a\n")
    shipped = sw.ship()
    assert shipped.code is ExitCode.OK and shipped.pr is not None
    assert Status.IN_REVIEW.label in sw.labels(n)
    (git.cwd / "src" / "api" / "a.py").write_text("addressing review\n")
    tip, local = sw.remote_tip(n), head(git)
    comments, labels = len(sw.fake.list_comments(n)), sw.labels(n)
    pushes = len(git.pushes)

    with pytest.raises(PreconditionError) as info:
        handoff(hctx(sw), n, NOTE)

    error = info.value
    assert error.code is ExitCode.PRECONDITION
    assert f"open pull request (#{shipped.pr.number})" in error.message
    assert error.hint == (
        "the task is in review: address review comments on the PR yourself, or close the PR, "
        "then hand off"
    )
    # nothing changed: no commit, no push, no comment, no label, no lease write
    assert sw.remote_tip(n) == tip and head(git) == local and len(git.pushes) == pushes
    assert status(git) == " M src/api/a.py\n"
    assert len(sw.fake.list_comments(n)) == comments and sw.labels(n) == labels
    assert sw.fake.get_issue(n).assignees == ("alice",)
    assert lease_data(sw, n).released_at is None

    close_pull(sw, shipped.pr.number)
    out = handoff(hctx(sw), n, NOTE)

    assert out.code is ExitCode.OK and out.status is Status.HANDOFF
    assert status_labels(sw, n) == ["status:handoff"]
    assert derived(sw, n, "M1-T01") is Status.HANDOFF
    taken = run(sw.world.ctx("bob", clock=Clock(LATER)))
    assert taken.claimed is not None and taken.claimed.issue == n
    assert taken.claimed.resumed


# --------------------------------------------------------------------------- git state refusals


@pytest.mark.parametrize("operation", ["merge", "cherry-pick", "revert"])
def test_merge_cherry_pick_or_revert_in_progress_is_refused(sw: SyncWorld, operation: str) -> None:
    n, git = claimed(sw)
    base = head(git)
    git.run("checkout", "--quiet", "-b", "side")
    side = sw.commit("alice", "src/api/c.py", "side\n", "feat: side")
    git.run("checkout", "--quiet", f"task/{n}")
    mine = sw.commit("alice", "src/api/c.py", "mine\n", "feat: mine")
    if operation == "merge":
        args = ["merge", "--no-edit", "side"]
    elif operation == "cherry-pick":
        args = ["cherry-pick", side]
    else:
        sw.commit("alice", "src/api/c.py", "later\n", "feat: later")
        args = ["revert", "--no-edit", mine]
    assert git.run(*args, check=False).returncode != 0  # stopped on a conflict
    before_status = status(git)
    before_head = head(git)

    with pytest.raises(PreconditionError, match=f"a {operation} is in progress") as info:
        handoff(hctx(sw), n, NOTE)

    assert info.value.hint is not None and f"git {operation} --abort" in info.value.hint
    assert head(git) == before_head and status(git) == before_status
    assert git.operation_in_progress() == operation
    assert sw.remote_tip(n) == base  # nothing was pushed
    assert handoff_comments(sw, n) == []


# --------------------------------------------------------------------------- push failures


def test_plain_push_failure_undoes_the_wip_and_reraises(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    work = sw.commit("alice", "src/api/a.py", "a\n")
    (git.cwd / "src" / "api" / "a.py").write_text("dirty\n")
    (git.cwd / "src" / "api" / "new.py").write_text("new\n")
    before = sw.remote_tip(n)

    def network_down(src: str, dst: str) -> None:
        git.before_push = None
        raise GitError(["push"], 128, "fatal: unable to access remote: network is down")

    git.before_push = network_down
    with pytest.raises(GitError, match="network is down"):
        handoff(hctx(sw), n, NOTE)

    assert head(git) == work  # the wip commit was undone
    assert status(git) == " M src/api/a.py\n?? src/api/new.py\n"
    assert sw.remote_tip(n) == before and handoff_comments(sw, n) == []
    # the network is back: the re-run goes through and saves the changes again
    again = handoff(hctx(sw), n, NOTE)
    assert again.code is ExitCode.OK and not again.recovered and again.wip_commit is not None
    assert status(git) == "" and head(git) == sw.remote_tip(n)


def test_failure_reported_after_the_push_landed_converges_on_rerun(sw: SyncWorld) -> None:
    """The push landed but the run failed right after (e.g. interrupted): the wip commit is
    undone, so the files hold exactly the handoff's content. The re-run recognizes them as
    published, aligns the branch and completes with exit 0."""
    n, git = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    (git.cwd / "src" / "api" / "a.py").write_text("dirty\n")

    def interrupted(src: str, dst: str, result: PushResult) -> None:
        git.after_push = None
        assert result.ok
        raise GitError(["push"], 1, "interrupted after the push")

    git.after_push = interrupted
    with pytest.raises(GitError):
        handoff(hctx(sw), n, NOTE)
    tip = sw.remote_tip(n)
    assert tip is not None and status(git) == " M src/api/a.py\n"

    again = handoff(hctx(sw), n, NOTE)

    assert again.code is ExitCode.OK and again.recovered and again.unpublished == ()
    assert head(git) == tip and status(git) == ""
    assert (git.cwd / "src" / "api" / "a.py").read_text() == "dirty\n"
    assert len(handoff_comments(sw, n)) == 1


# --------------------------------------------------------------------------- unpublished work


def test_losing_clone_with_local_work_is_exit_2_on_rerun(sw: SyncWorld) -> None:
    """Same identity on two clones: the laptop's handoff wins the race. The desktop had its
    own commit and changes; its re-run completes nothing twice and must not claim success."""
    n, git = claimed(sw)
    laptop = sw.checkout("alice-laptop", n)
    mine = sw.commit("alice", "src/api/desk.py", "desk\n", "feat: desk work")
    (git.cwd / "src" / "api" / "draft.py").write_text("draft\n")
    results: list[HandoffOutcome] = []

    def laptop_hands_off(src: str, dst: str) -> None:
        git.before_push = None
        results.append(handoff(hctx(sw, clone="alice-laptop"), n, NOTE))

    git.before_push = laptop_hands_off
    lost = handoff(hctx(sw), n, NOTE)

    assert results[0].code is ExitCode.OK  # exactly one winner
    assert lost.code is ExitCode.PRECONDITION and lost.outcome == "lease_rejected"
    tip = sw.remote_tip(n)
    assert tip == results[0].handoff_commit == head(laptop)
    assert head(git) == mine and status(git) == "?? src/api/draft.py\n"  # wip undone

    again = handoff(hctx(sw), n, NOTE)

    assert again.recovered and again.handoff_commit == tip
    assert again.code is ExitCode.AGENT_ACTION and again.outcome == "unpublished_work"
    assert again.unpublished == (
        f"commit {mine[:12]} feat: desk work",
        "uncommitted change src/api/draft.py",
    )
    assert again.error is not None and "src/api/draft.py" in again.error
    # nothing local was moved or lost, nothing pushed or posted twice
    assert head(git) == mine and status(git) == "?? src/api/draft.py\n"
    assert sw.remote_tip(n) == tip and len(handoff_comments(sw, n)) == 1
    events = [c for c in remote(sw).commits(f"main..{tip}") if TRAILER_HANDOFF in c.trailers]
    assert len(events) == 1


def test_rerun_with_changes_after_an_interruption_is_exit_2(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    ctx = hctx(sw)
    ctx.github.fail["create_comment"] = 1  # type: ignore[attr-defined]
    with pytest.raises(GitHubError):
        handoff(ctx, n, NOTE)
    tip = sw.remote_tip(n)
    assert head(git) == tip
    (git.cwd / "src" / "api").mkdir(parents=True, exist_ok=True)
    (git.cwd / "src" / "api" / "after.py").write_text("after\n")

    again = handoff(hctx(sw), n, NOTE)

    assert again.code is ExitCode.AGENT_ACTION and again.outcome == "unpublished_work"
    assert again.unpublished == ("uncommitted change src/api/after.py",)
    assert len(handoff_comments(sw, n)) == 1 and lease_data(sw, n).released_at == LATER
    # once the change is set aside, the re-run is a success
    git.run("stash", "--quiet", "--include-untracked")
    final = handoff(hctx(sw), n, NOTE)
    assert final.code is ExitCode.OK and final.outcome == "handed_off"


# --------------------------------------------------------------------------- recovery path


def interrupted_handoff(sw: SyncWorld, n: int) -> str:
    """Run a handoff that fails right after its push; return the pushed handoff tip."""
    ctx = hctx(sw)
    ctx.github.fail["create_comment"] = 1  # type: ignore[attr-defined]
    with pytest.raises(GitHubError):
        handoff(ctx, n, NOTE)
    tip = sw.remote_tip(n)
    assert tip is not None
    return tip


def test_rerun_on_a_detached_head_reports_an_unchecked_working_tree(sw: SyncWorld) -> None:
    """A stopped `git pull --rebase` (or a manual checkout) left work on a detached HEAD: the
    re-run cannot compare it with the handoff, so it must not report success."""
    n, git = claimed(sw)
    tip = interrupted_handoff(sw, n)
    git.run("checkout", "--quiet", "--detach")
    detached = sw.commit("alice", "src/api/detached.py", "d\n", "feat: on a detached head")

    again = handoff(hctx(sw), n, NOTE)

    assert again.recovered
    assert again.code is ExitCode.AGENT_ACTION and again.outcome == "unpublished_work"
    assert len(again.unpublished) == 1
    entry = again.unpublished[0]
    assert entry.startswith("unchecked working tree: task/")
    assert f"HEAD is detached at {detached[:12]}" in entry
    assert again.hint is not None and f"git switch task/{n}" in again.hint
    # nothing moved, the GitHub steps are complete and posted once
    assert head(git) == detached and git.current_branch() is None
    assert sw.remote_tip(n) == tip and len(handoff_comments(sw, n)) == 1
    assert lease_data(sw, n).released_at == LATER

    # back on the branch, with the detached work set aside: the re-run succeeds
    git.run("checkout", "--quiet", f"task/{n}")
    final = handoff(hctx(sw), n, NOTE)
    assert final.code is ExitCode.OK and final.unpublished == ()


@pytest.mark.parametrize("operation", ["sync", "rebase", "merge"])
def test_rerun_refuses_an_operation_in_progress(sw: SyncWorld, operation: str) -> None:
    n, git = claimed(sw)
    tip = interrupted_handoff(sw, n)
    assert head(git) == tip
    if operation == "sync":
        (git.cwd / ".forge" / "local" / "sync.json").write_text("{}")
    else:
        git.run("checkout", "--quiet", "-b", "side", f"{tip}~1")
        sw.commit("alice", "src/api/c.py", "side\n", "feat: side")
        git.run("checkout", "--quiet", f"task/{n}")
        sw.commit("alice", "src/api/c.py", "mine\n", "feat: mine")
        args = ["rebase", "side"] if operation == "rebase" else ["merge", "--no-edit", "side"]
        assert git.run(*args, check=False).returncode != 0  # stopped on a conflict
    comments = len(sw.fake.list_comments(n))

    with pytest.raises(PreconditionError, match=f"a {operation} is in progress") as info:
        handoff(hctx(sw), n, NOTE)

    assert info.value.code is ExitCode.PRECONDITION
    assert len(sw.fake.list_comments(n)) == comments  # no GitHub step ran
    assert lease_data(sw, n).released_at is None
    assert sw.remote_tip(n) == tip


def test_mixed_reset_keeps_staged_content_that_is_neither_head_nor_tip(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    work = sw.commit("alice", "src/api/a.py", "a\n")
    (git.cwd / "src" / "api" / "a.py").write_text("dirty\n")

    def interrupted(src: str, dst: str, result: PushResult) -> None:
        git.after_push = None
        raise GitError(["push"], 1, "interrupted after the push")

    git.after_push = interrupted
    with pytest.raises(GitError):
        handoff(hctx(sw), n, NOTE)
    tip = sw.remote_tip(n)
    assert head(git) == work  # the wip commit was undone; the files hold the tip's content
    # stage a version that is neither HEAD's nor the tip's, then restore the file
    (git.cwd / "src" / "api" / "a.py").write_text("staged only\n")
    git.add(["src/api/a.py"])
    (git.cwd / "src" / "api" / "a.py").write_text("dirty\n")

    again = handoff(hctx(sw), n, NOTE)

    assert again.code is ExitCode.OK and again.recovered
    assert any("staged content" in w for w in again.warnings)
    assert head(git) == work and sw.remote_tip(n) == tip  # no reset
    assert git.run("show", ":src/api/a.py").stdout == "staged only\n"
    assert (git.cwd / "src" / "api" / "a.py").read_text() == "dirty\n"


# --------------------------------------------------------------------------- publish edge cases


def test_lease_rejection_undoes_the_wip_even_if_the_remote_query_fails(sw: SyncWorld) -> None:
    n, git = claimed(sw)
    work = sw.commit("alice", "src/api/a.py", "a\n")
    (git.cwd / "src" / "api" / "b.py").write_text("dirty\n")
    late = T0 + timedelta(hours=30)

    def bob_reclaims_then_network_drops(src: str, dst: str) -> None:
        git.before_push = None
        taken = run(sw.world.ctx("bob", clock=Clock(late)), task=str(n), reclaim=True)
        assert taken.claimed is not None

        def unreachable(branch: str, remote: str = "origin") -> str | None:
            raise GitError(["ls-remote"], 128, "fatal: unable to access remote")

        git.remote_branch_sha = unreachable  # type: ignore[method-assign]

    git.before_push = bob_reclaims_then_network_drops
    out = handoff(hctx(sw, now=late), n, NOTE)

    assert out.code is ExitCode.PRECONDITION and out.outcome == "lease_rejected"
    assert (
        out.error is not None and "-> unknown" in out.error and "wip commit was undone" in out.error
    )
    assert head(git) == work and status(git) == "?? src/api/b.py\n"
    assert handoff_comments(sw, n) == []


def test_commit_made_during_the_push_is_kept_and_reported(sw: SyncWorld) -> None:
    """The local branch moves while the pre-push hook runs (a commit in the same worktree):
    the compare-and-swap leaves it alone and the run reports it as unpublished."""
    n, git = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    concurrent: list[str] = []

    def commit_meanwhile(src: str, dst: str, result: PushResult) -> None:
        git.after_push = None
        assert result.ok
        concurrent.append(sw.commit("alice", "src/api/late.py", "late\n", "feat: meanwhile"))

    git.after_push = commit_meanwhile
    out = handoff(hctx(sw), n, NOTE)

    late = concurrent[0]
    assert out.code is ExitCode.AGENT_ACTION and out.outcome == "unpublished_work"
    assert not out.recovered
    assert out.unpublished == (f"commit {late[:12]} feat: meanwhile",)
    assert any("moved while the handoff was pushed" in w for w in out.warnings)
    assert head(git) == late and status(git) == ""  # nothing lost
    tip = sw.remote_tip(n)
    assert tip == out.handoff_commit and remote(sw).rev_parse(late) is None
    assert len(handoff_comments(sw, n)) == 1 and out.status is Status.HANDOFF
