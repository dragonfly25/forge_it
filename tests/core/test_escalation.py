"""Tests for forge.core.escalation: upstream context helpers and `forge escalate` (ADR 0011)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from forge.core.errors import ExitCode, PreconditionError
from forge.core.escalation import (
    ESCALATION_MARKER,
    EscalateContext,
    closing_issues,
    escalate,
    mention_list,
    pull_issues,
)
from forge.core.github import PullRequest
from forge.core.sync import sync
from forge.core.tasks import BLOCKED_CONFLICT
from forge.core.workspace import clear_current
from support.sync_world import SyncWorld, team_with_leads

if TYPE_CHECKING:
    from conftest import GitWorld

T = datetime(2026, 11, 3, tzinfo=UTC)


def pr(body: str, head: str = "feature/x") -> PullRequest:
    return PullRequest(
        1, "t", body, head, "abc", "main", "closed", True, T, None, False, "bob", (), (), (), "u"
    )


def test_closing_keywords() -> None:
    text = "Fixes #12, closes: #3 and Resolved #7. Mentions #9. fix #12 again. prefix#4"
    assert closing_issues(text) == [12, 3, 7]
    assert closing_issues("") == []


def test_pull_issues_include_a_task_head() -> None:
    assert pull_issues(pr("Closes #5", head="task/8")) == [5, 8]
    assert pull_issues(pr("Closes #8", head="task/8")) == [8]
    assert pull_issues(pr("nothing", head="task/08")) == []


def test_mention_list_dedupes_and_excludes() -> None:
    assert mention_list(["bob", "Bob", "", "alice", "carol"], exclude=["ALICE"]) == ["bob", "carol"]


def escalation_comments(sw: SyncWorld, issue: int) -> list[str]:
    return [c.body for c in sw.fake.list_comments(issue) if c.body.startswith(ESCALATION_MARKER)]


def stopped_sync(sw: SyncWorld) -> tuple[int, int]:
    """Alice's sync of #n stops on an in-zone conflict with bob's PR (closing #m)."""
    m = sw.task("M1-T09", ["web"])
    sw.upstream({"src/api/routes.py": "base\n"}, link=False)
    n = sw.task("M1-T01", ["api"])
    sw.claim("alice", n)
    sw.commit("alice", "src/api/routes.py", "task\n")
    sw.upstream({"src/api/routes.py": "upstream\n"}, closes=m, author="bob")
    assert sync(sw.ctx()).code == ExitCode.AGENT_ACTION
    return n, m


def test_escalate_labels_comments_and_mentions(git_world: GitWorld) -> None:
    sw = SyncWorld(git_world)
    n, _ = stopped_sync(sw)
    git = sw.world.git("alice")
    ctx = EscalateContext(git, sw.github("alice"), team_with_leads("lead1", "alice"))  # type: ignore[arg-type]

    out = escalate(ctx, "the routes contract changed under me")

    assert out.issue == n and out.task_id == "M1-T01"
    assert out.label_added and BLOCKED_CONFLICT in sw.labels(n)
    assert out.mentioned == ("bob", "lead1") and out.source == "sync"
    bodies = escalation_comments(sw, n)
    assert len(bodies) == 1
    body = bodies[0]
    assert "> the routes contract changed under me" in body
    assert "@bob" in body and "@lead1" in body and "@alice" not in body
    assert out.to_data()["comment_created"] is True

    again = escalate(ctx, "the routes contract changed under me")  # re-run: nothing new
    assert not again.label_added and not again.comment_created
    assert len(escalation_comments(sw, n)) == 1
    other = escalate(ctx, "also the schema")
    assert other.comment_created and len(escalation_comments(sw, n)) == 2


def test_escalate_finds_the_task_from_sync_json_mid_rebase(git_world: GitWorld) -> None:
    sw = SyncWorld(git_world)
    n, _ = stopped_sync(sw)
    git = sw.world.git("alice")
    assert git.rebase_in_progress() and git.current_branch() is None
    clear_current(git.cwd)
    out = escalate(EscalateContext(git, sw.github("alice")), "need help")  # type: ignore[arg-type]
    assert out.issue == n and out.mentioned == ("bob",)


def test_escalate_uses_the_last_sync_log_entry(git_world: GitWorld) -> None:
    sw = SyncWorld(git_world)
    m = sw.task("M1-T09", ["web"])
    sw.upstream({"src/web/app.js": "base\n"}, link=False)
    n = sw.task("M1-T01", ["api"])
    sw.claim("alice", n)
    sw.commit("alice", "src/web/app.js", "task\n")
    sw.upstream({"src/web/app.js": "upstream\n"}, closes=m, author="carol")
    assert sync(sw.ctx()).code == ExitCode.ESCALATION  # logged, no sync.json left
    sw.fake.remove_label(n, BLOCKED_CONFLICT)
    git = sw.world.git("alice")
    out = escalate(EscalateContext(git, sw.github("alice")), "still blocked")  # type: ignore[arg-type]
    assert out.source == "sync-log" and out.mentioned == ("carol",) and out.label_added


def test_escalate_needs_no_live_claim_but_a_task_and_a_reason(git_world: GitWorld) -> None:
    sw = SyncWorld(git_world)
    n = sw.task("M1-T01", ["api"])
    sw.claim("bob", n)
    git = sw.checkout("alice", n)  # bob's claim, alice escalates; no sync ever ran
    ctx = EscalateContext(git, sw.github("alice"), team_with_leads("lead1"))  # type: ignore[arg-type]
    out = escalate(ctx, "blocked by #3")
    assert out.mentioned == ("lead1",) and out.source is None
    with pytest.raises(PreconditionError, match="reason"):
        escalate(ctx, "  ")
    git.run("checkout", "--quiet", "main")
    with pytest.raises(PreconditionError, match="no current task"):
        escalate(ctx, "x")


def test_no_self_mention_on_upstream_issues_of_mine(git_world: GitWorld) -> None:
    """Upstream issues closed by my own PR, or assigned to me, get no comment mentioning me;
    the others do. The task comment mentions the other PR authors only."""
    sw = SyncWorld(git_world)
    mine_by_pr = sw.task("M1-T07", ["web"])
    mine_by_assignee = sw.task("M1-T08", ["web"])
    theirs = sw.task("M1-T09", ["web"])
    sw.fake.add_assignees(mine_by_assignee, ["Alice"])
    sw.upstream(
        {"src/web/a.js": "base\n", "src/web/b.js": "base\n", "src/web/c.js": "base\n"}, link=False
    )
    n = sw.task("M1-T01", ["api"])
    sw.claim("alice", n)
    for name in ("a", "b", "c"):
        sw.commit("alice", f"src/web/{name}.js", "task\n")
    sw.upstream({"src/web/a.js": "up\n"}, closes=mine_by_pr, author="alice")
    sw.upstream({"src/web/b.js": "up\n"}, closes=mine_by_assignee, author="carol")
    sw.upstream({"src/web/c.js": "up\n"}, closes=theirs, author="bob")

    out = sync(sw.ctx())

    assert out.code == ExitCode.ESCALATION and out.escalation is not None
    assert set(out.escalation.mentioned) == {"bob", "carol"}
    assert escalation_comments(sw, mine_by_pr) == []
    assert escalation_comments(sw, mine_by_assignee) == []
    bodies = escalation_comments(sw, theirs)
    assert len(bodies) == 1 and "@alice" in bodies[0] and f"#{n}" in bodies[0]
    assert dict(out.escalation.upstream_comments) == {theirs: True}
    assert sorted(out.escalation.upstream_skipped) == sorted([mine_by_pr, mine_by_assignee])
    assert sorted(out.escalation.to_data()["upstream_skipped"]) == sorted(
        [mine_by_pr, mine_by_assignee]
    )
    task_body = escalation_comments(sw, n)[0]
    assert "@alice" not in task_body and "@bob" in task_body and "@carol" in task_body
