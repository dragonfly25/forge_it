"""Scenario tests for forge.core.sync (ADR 0011) on real temporary repositories.

Each scenario uses a bare remote plus one clone per simulated user and a shared FakeGitHub
(``support.sync_world``). Checks and resolver commands are tiny local shell commands.
"""

from __future__ import annotations

import json
import stat
import sys
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.escalation import ESCALATION_MARKER
from forge.core.git import Git
from forge.core.github import GitHubError
from forge.core.lease import is_lease_comment
from forge.core.sync import (
    ABORTED_SUFFIX,
    BACKUP_PREFIX,
    MAX_BACKUPS,
    SYNC_MARKER,
    AbortContext,
    Category,
    SyncOptions,
    classify,
    prune_backups,
    read_sync,
    read_sync_log,
    sync,
    sync_abort,
    sync_continue,
)
from forge.core.tasks import BLOCKED_CONFLICT, TRAILER_CLAIM, TRAILER_RUN, TaskType
from forge.core.workspace import read_current
from forge.core.zones import Zone, ZonesConfig
from support.scenarios import T0, Clock, UserGitHub, claim_of
from support.shell import FAIL, PASS, file_contains
from support.sync_world import (
    DEFAULT_RESOLVERS,
    LOCK_COMMAND,
    Recorder,
    SyncWorld,
    status,
    sync_config,
)

if TYPE_CHECKING:
    from conftest import GitWorld


def head(git: Git) -> str:
    sha = git.rev_parse("HEAD")
    assert sha is not None
    return sha


def claim_commit(git: Git, ref: str = "HEAD") -> Any:
    for commit in git.commits(f"origin/main..{ref}", first_parent=True):
        if TRAILER_CLAIM in commit.trailers:
            return commit
    raise AssertionError("no claim commit")


def hook(git: Git, name: str, body: str) -> Path:
    path = git.cwd / ".git" / "hooks" / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def comments_with(world: SyncWorld, issue: int, marker: str) -> list[str]:
    return [c.body for c in world.fake.list_comments(issue) if c.body.startswith(marker)]


class FlakyGitHub(UserGitHub):
    """Raises once on the ``nth`` call (1-based) of ``method``."""

    def __init__(self, base: UserGitHub, method: str, nth: int) -> None:
        super().__init__(base._fake, base._login, base._lock)
        self._method, self._nth, self._calls = method, nth, 0

    def __getattr__(self, name: str) -> Any:
        call = super().__getattr__(name)
        if name != self._method:
            return call

        def flaky(*args: Any, **kwargs: Any) -> Any:
            self._calls += 1
            if self._calls == self._nth:
                raise GitHubError(f"injected failure in {name}", status=502)
            return call(*args, **kwargs)

        return flaky


@pytest.fixture
def sw(git_world: GitWorld) -> SyncWorld:
    return SyncWorld(git_world)


def in_zone_conflict(sw: SyncWorld) -> tuple[int, int, Git]:
    """Task #n (zone api) and upstream both change src/api/routes.py; upstream closes #m."""
    m = sw.task("M1-T09", ["web"])
    sw.upstream({"src/api/routes.py": "base\n"}, link=False, message="chore: base")
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "src/api/routes.py", "task\n")
    sw.upstream({"src/api/routes.py": "upstream\n"}, closes=m, message="feat: routes")
    return n, m, git


# --------------------------------------------------------------------------- 1. clean


def test_clean_rebase_pushes_and_rewrites_the_lease(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    before = claim_commit(git)
    old_lease = sw.lease(n).data
    assert old_lease is not None and old_lease.run == before.trailers[TRAILER_RUN]
    sw.commit("alice", "src/api/a.py", "a\n")
    sw.upstream({"src/web/x.js": "x\n"})
    comments_before = len(sw.fake.list_comments(n))

    out = sync(sw.ctx())

    assert out.code == ExitCode.OK and out.outcome == "pushed", out.messages
    assert out.rebased and out.classification is not None
    assert out.classification.primary is Category.NONE
    tip = head(git)
    assert sw.remote_tip(n) == tip
    assert git.is_ancestor("origin/main", tip)
    after = claim_commit(git)
    assert after.sha != before.sha
    assert after.trailers[TRAILER_RUN] == before.trailers[TRAILER_RUN]
    assert after.date == before.date == T0  # committer date unchanged: claim order kept
    lease = sw.lease(n).data
    assert lease is not None
    assert lease.claim == after.sha and lease.run == old_lease.run
    assert lease.claimed_at == old_lease.claimed_at
    assert lease.heartbeat_at == T0 + timedelta(hours=1)
    assert sum(is_lease_comment(c.body) for c in sw.fake.list_comments(n)) == 1
    assert len(sw.fake.list_comments(n)) == comments_before  # no summary comment
    log = read_sync_log(git.cwd)
    assert len(log) == 1
    assert log[0]["outcome"] == "pushed" and log[0]["conflicts"] == []
    assert log[0]["checks_passed"] is True and log[0]["checked_head"] == tip
    assert log[0]["upstream"] == 1 and log[0]["head"] == tip
    current = read_current(git.cwd)
    assert current is not None and current.claim == after.sha
    assert read_sync(git.cwd) is None
    assert git.list_refs("refs/forge/backup/")  # backup kept


def test_up_to_date_skips_checks_and_push(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    tip = head(git)
    checks = Recorder()
    out = sync(sw.ctx(config=sync_config(checks={"t": PASS}), run_check=checks))
    assert out.code == ExitCode.OK and out.outcome == "up_to_date"
    assert checks.calls == [] and sw.remote_tip(n) == tip and not out.rebased
    assert read_sync_log(git.cwd)[-1]["outcome"] == "up_to_date"
    lease = sw.lease(n).data
    assert lease is not None and lease.heartbeat_at == T0 + timedelta(hours=1)


def test_local_commits_without_upstream_are_checked_and_pushed(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "src/api/a.py", "a\n")
    out = sync(sw.ctx())
    assert out.outcome == "pushed" and not out.rebased
    assert sw.remote_tip(n) == head(git)


# --------------------------------------------------------------------------- 2-3. resolvers


def test_lockfile_conflict_is_regenerated(sw: SyncWorld) -> None:
    sw.upstream({"package-lock.json": "lock v1\n"}, link=False)
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "package-lock.json", "lock task\n")
    sw.upstream({"package-lock.json": "lock upstream\n"})
    commands = Recorder()

    out = sync(sw.ctx(run_command=commands))

    assert out.code == ExitCode.OK and out.outcome == "pushed", out.messages
    assert commands.calls == [(LOCK_COMMAND, git.cwd)]
    # regenerate starts from the upstream side, then runs the command (which appends)
    assert (git.cwd / "package-lock.json").read_text() == "lock upstream\nregenerated\n"
    assert sw.remote_tip(n) == head(git)
    entry = read_sync_log(git.cwd)[-1]
    assert [(c["path"], c["resolution"]) for c in entry["conflicts"]] == [
        ("package-lock.json", "regenerate")
    ]
    summary = comments_with(sw, n, SYNC_MARKER)
    assert len(summary) == 1 and "package-lock.json`: regenerate" in summary[0]


def test_failing_regenerate_leaves_the_path_conflicted(sw: SyncWorld) -> None:
    sw.upstream({"package-lock.json": "lock v1\n"}, link=False)
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "package-lock.json", "lock task\n")
    sw.upstream({"package-lock.json": "lock upstream\n"})
    config = sync_config(
        resolvers=[{"glob": "package-lock.json", "strategy": "regenerate", "command": "exit 3"}]
    )
    # package-lock.json is outside every zone: without a resolver it escalates
    out = sync(sw.ctx(config=config))
    assert out.code == ExitCode.ESCALATION
    assert not git.rebase_in_progress()
    out = sync(sw.ctx(config=config), SyncOptions(force_resolve=True))
    assert out.code == ExitCode.AGENT_ACTION and out.unresolved == ["package-lock.json"]
    assert any("regenerate failed" in m.text and "exited 3" in m.text for m in out.messages)
    sync_abort(sw.ctx(config=config))


def test_changelog_conflict_is_resolved_by_union(sw: SyncWorld) -> None:
    sw.upstream({"CHANGELOG.md": "# Changelog\n\n"}, link=False)
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "CHANGELOG.md", "# Changelog\n\n- task entry\n")
    sw.upstream({"CHANGELOG.md": "# Changelog\n\n- upstream entry\n"})

    out = sync(sw.ctx())

    assert out.code == ExitCode.OK and out.outcome == "pushed", out.messages
    text = (git.cwd / "CHANGELOG.md").read_text()
    assert "- task entry" in text and "- upstream entry" in text and "<<<<" not in text
    remote = sw.world.git_world.remote_git.run("show", f"refs/heads/task/{n}:CHANGELOG.md")
    assert "- task entry" in remote.stdout and "- upstream entry" in remote.stdout
    assert read_sync_log(git.cwd)[-1]["conflicts"][0]["resolution"] == "union"


# --------------------------------------------------------------------------- 4. in-zone


def test_in_zone_conflict_stops_for_the_agent_then_continues(sw: SyncWorld) -> None:
    n, m, git = in_zone_conflict(sw)

    out = sync(sw.ctx())

    assert out.code == ExitCode.AGENT_ACTION and out.outcome == "conflicts"
    assert out.unresolved == ["src/api/routes.py"]
    assert git.rebase_in_progress()
    record = read_sync(git.cwd)
    assert record is not None and record.state.value == "conflicts"
    raw = json.loads((git.cwd / ".forge/local/sync.json").read_text())
    assert raw["classification"]["primary"] == "textual"
    assert raw["conflicts"][0]["path"] == "src/api/routes.py"
    assert raw["conflicts"][0]["zones"] == ["api"] and raw["conflicts"][0]["resolution"] is None
    assert raw["upstream"]["pulls"][0]["author"] == "bob"
    assert raw["upstream"]["pulls"][0]["issues"] == [m]
    assert raw["upstream"]["issues"] == [m]
    assert raw["upstream"]["commits"][0]["files"] == ["src/api/routes.py"]
    assert out.state_file == str(git.cwd / ".forge/local/sync.json")

    # still unmerged: --continue refuses with exit 2
    again = sync_continue(sw.ctx())
    assert again.code == ExitCode.AGENT_ACTION and again.unresolved == ["src/api/routes.py"]

    (git.cwd / "src/api/routes.py").write_text("upstream\ntask\n")
    git.add(["src/api/routes.py"])
    done = sync_continue(sw.ctx())

    assert done.code == ExitCode.OK and done.outcome == "pushed", done.messages
    assert sw.remote_tip(n) == head(git)
    assert not git.rebase_in_progress() and read_sync(git.cwd) is None
    entry = read_sync_log(git.cwd)[-1]
    assert entry["conflicts"][0]["resolution"] == "manual"
    summary = comments_with(sw, n, SYNC_MARKER)
    assert len(summary) == 1 and "src/api/routes.py`: manual" in summary[0]


def test_continue_after_the_agent_finished_the_rebase_with_git(sw: SyncWorld) -> None:
    n, _, git = in_zone_conflict(sw)
    assert sync(sw.ctx()).code == ExitCode.AGENT_ACTION
    (git.cwd / "src/api/routes.py").write_text("both\n")
    git.add(["src/api/routes.py"])
    git.run("rebase", "--continue", env={"GIT_EDITOR": "true"})
    done = sync_continue(sw.ctx())
    assert done.outcome == "pushed" and sw.remote_tip(n) == head(git)
    assert read_sync_log(git.cwd)[-1]["conflicts"][0]["resolution"] == "manual"


def test_continue_restarts_a_rebase_aborted_outside_forge(sw: SyncWorld) -> None:
    n, _, git = in_zone_conflict(sw)
    assert sync(sw.ctx()).code == ExitCode.AGENT_ACTION
    git.rebase_abort()
    again = sync_continue(sw.ctx())
    assert again.code == ExitCode.AGENT_ACTION and git.rebase_in_progress()
    assert again.unresolved == ["src/api/routes.py"]
    # the stale entry of the first attempt was dropped: the path is listed once
    assert [(c.path, c.resolution) for c in again.conflicts] == [("src/api/routes.py", None)]
    record = read_sync(git.cwd)
    assert record is not None
    assert [(c.path, c.resolution) for c in record.conflicts] == [("src/api/routes.py", None)]

    (git.cwd / "src/api/routes.py").write_text("upstream\ntask\n")
    git.add(["src/api/routes.py"])
    done = sync_continue(sw.ctx())

    assert done.outcome == "pushed", done.messages
    entry = read_sync_log(git.cwd)[-1]
    assert [(c["path"], c["resolution"]) for c in entry["conflicts"]] == [
        ("src/api/routes.py", "manual")
    ]
    summary = comments_with(sw, n, SYNC_MARKER)
    assert len(summary) == 1 and summary[0].count("src/api/routes.py") == 1


# --------------------------------------------------------------------------- 5-6. escalation


def out_of_zone_conflict(sw: SyncWorld) -> tuple[int, int, Git]:
    m = sw.task("M1-T09", ["web"])
    sw.upstream({"src/web/app.js": "base\n"}, link=False, message="chore: base")
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "src/api/a.py", "a\n")
    sw.commit("alice", "src/web/app.js", "task\n")
    sw.upstream({"src/web/app.js": "upstream\n"}, closes=m, author="bob")
    return n, m, git


def test_out_of_zone_conflict_escalates(sw: SyncWorld) -> None:
    n, m, git = out_of_zone_conflict(sw)
    tip = head(git)
    before = status(git)

    out = sync(sw.ctx())

    assert out.code == ExitCode.ESCALATION and out.outcome == "escalated"
    assert out.classification is not None
    assert out.classification.primary is Category.OUT_OF_ZONE
    assert not git.rebase_in_progress()
    assert head(git) == tip and status(git) == before
    assert git.current_branch() == f"task/{n}"
    assert read_sync(git.cwd) is None
    assert sw.remote_tip(n) != tip  # nothing pushed
    assert BLOCKED_CONFLICT in sw.labels(n)
    task_comments = comments_with(sw, n, ESCALATION_MARKER)
    assert len(task_comments) == 1
    assert "@bob" in task_comments[0] and "src/web/app.js" in task_comments[0]
    assert "@alice" not in task_comments[0]
    upstream_comments = comments_with(sw, m, ESCALATION_MARKER)
    assert len(upstream_comments) == 1 and "@alice" in upstream_comments[0]
    assert f"#{n}" in upstream_comments[0]
    assert read_sync_log(git.cwd)[-1]["outcome"] == "escalated"
    assert out.escalation is not None and out.escalation.mentioned == ("bob",)

    forced = sync(sw.ctx(), SyncOptions(force_resolve=True))
    assert forced.code == ExitCode.AGENT_ACTION and forced.unresolved == ["src/web/app.js"]
    record = read_sync(git.cwd)
    assert record is not None and record.force_resolve
    (git.cwd / "src/web/app.js").write_text("merged\n")
    git.add(["src/web/app.js"])
    done = sync_continue(sw.ctx())
    assert done.code == ExitCode.OK and done.outcome == "pushed"
    assert BLOCKED_CONFLICT not in sw.labels(n)  # 11: a pushing sync clears the label
    assert read_sync_log(git.cwd)[-1]["force_resolve"] is True


def test_contract_zone_conflict_escalates(sw: SyncWorld) -> None:
    sw.upstream({"contracts/openapi.yml": "v1\n"}, link=False)
    n = sw.task("M1-T01", ["api", "contracts"])
    git = sw.claim("alice", n)
    sw.commit("alice", "contracts/openapi.yml", "v1-task\n")
    sw.upstream({"contracts/openapi.yml": "v1-upstream\n"})
    tip = head(git)
    out = sync(sw.ctx())
    assert out.code == ExitCode.ESCALATION
    assert out.classification is not None
    assert out.classification.primary is Category.SEMANTIC  # in its zones, but a contract
    assert head(git) == tip and not git.rebase_in_progress()
    assert "contract zone contracts" in comments_with(sw, n, ESCALATION_MARKER)[0]


@pytest.mark.parametrize(
    ("kind", "allow"), [(TaskType.CONTRACT, False), (TaskType.IMPL, True)], ids=["type", "flag"]
)
def test_conflict_in_an_owned_contract_zone_is_left_to_the_agent(
    sw: SyncWorld, kind: TaskType, allow: bool
) -> None:
    sw.upstream({"contracts/openapi.yml": "v1\n"}, link=False)
    n = sw.task("M1-T01", ["contracts"], type=kind, allow_contract_change=allow)
    git = sw.claim("alice", n)
    sw.commit("alice", "contracts/openapi.yml", "v1-task\n")
    sw.upstream({"contracts/openapi.yml": "v1-upstream\n"})
    out = sync(sw.ctx())
    assert out.code == ExitCode.AGENT_ACTION
    assert git.rebase_in_progress()
    assert comments_with(sw, n, ESCALATION_MARKER) == []


def test_interrupted_escalation_converges_without_duplicate_comments(sw: SyncWorld) -> None:
    n, m, git = out_of_zone_conflict(sw)
    tip = head(git)
    ctx = sw.ctx()
    ctx.github = FlakyGitHub(sw.github("alice"), "create_comment", 1)  # type: ignore[assignment]
    with pytest.raises(GitHubError):
        sync(ctx)
    assert head(git) == tip and not git.rebase_in_progress() and read_sync(git.cwd) is None
    assert BLOCKED_CONFLICT in sw.labels(n)
    for _ in range(2):  # the re-run completes it; a further run repeats nothing
        out = sync(sw.ctx())
        assert out.code == ExitCode.ESCALATION
        assert head(git) == tip and read_sync(git.cwd) is None
    assert len(comments_with(sw, n, ESCALATION_MARKER)) == 1
    assert len(comments_with(sw, m, ESCALATION_MARKER)) == 1
    assert [e["outcome"] for e in read_sync_log(git.cwd)] == ["escalated", "escalated"]


# --------------------------------------------------------------------------- 7. abort


def test_abort_restores_tip_and_uncommitted_changes(sw: SyncWorld) -> None:
    n, _, git = in_zone_conflict(sw)
    (git.cwd / "src/api/routes.py").write_text("task, edited\n")
    (git.cwd / "src/api/new.py").write_text("untracked\n")
    tip = head(git)
    before = status(git)
    assert " M src/api/routes.py" in before and "?? src/api/new.py" in before

    out = sync(sw.ctx())
    assert out.code == ExitCode.AGENT_ACTION
    assert any("forge: wip" in m.text for m in out.messages)
    assert git.rebase_in_progress()

    aborted = sync_abort(sw.ctx())

    assert aborted.code == ExitCode.OK and aborted.outcome == "aborted"
    assert not git.rebase_in_progress()
    assert head(git) == tip
    assert status(git) == before
    assert (git.cwd / "src/api/routes.py").read_text() == "task, edited\n"
    assert read_sync(git.cwd) is None
    assert git.current_branch() == f"task/{n}"
    assert len(git.list_refs(f"refs/forge/backup/task-{n}/")) == 1  # kept
    with pytest.raises(PreconditionError, match="no sync in progress"):
        sync_abort(sw.ctx())


def test_abort_after_failed_checks(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "src/api/a.py", "broken\n")
    tip = head(git)
    sw.upstream({"src/web/x.js": "x\n"})
    config = sync_config(checks={"lint": file_contains("src/api/a.py", "fixed")})
    assert sync(sw.ctx(config=config)).outcome == "checks_failed"
    assert head(git) != tip  # rebased
    assert sync_abort(sw.ctx(config=config)).code == ExitCode.OK
    assert head(git) == tip and status(git) == ""


# --------------------------------------------------------------------------- 8. checks


def test_failing_check_then_fix_and_continue(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "src/api/a.py", "broken\n")
    sw.upstream({"src/web/x.js": "x\n"})
    config = sync_config(checks={"lint": file_contains("src/api/a.py", "fixed"), "unit": PASS})

    out = sync(sw.ctx(config=config))

    assert out.code == ExitCode.AGENT_ACTION and out.outcome == "checks_failed"
    assert [(c.name, c.exit_code) for c in out.checks] == [("lint", 1), ("unit", 0)]
    record = read_sync(git.cwd)
    assert record is not None and record.state.value == "checks_failed"
    remote_before = sw.remote_tip(n)
    assert remote_before != head(git)

    (git.cwd / "src/api/a.py").write_text("fixed\n")
    with pytest.raises(PreconditionError, match="uncommitted") as info:
        sync_continue(sw.ctx(config=config))
    assert info.value.hint is not None and "commit" in info.value.hint
    git.add(["src/api/a.py"])
    git.commit("fix: lint")

    done = sync_continue(sw.ctx(config=config))

    assert done.code == ExitCode.OK and done.outcome == "pushed", done.messages
    assert sw.remote_tip(n) == head(git)
    entry = read_sync_log(git.cwd)[-1]
    assert entry["checks_passed"] is True and entry["checked_head"] == head(git)
    assert comments_with(sw, n, SYNC_MARKER) == []  # no conflicts were resolved


# --------------------------------------------------------------------------- 9. classification


def zones_cfg() -> ZonesConfig:
    return ZonesConfig(
        zones={
            "api": Zone("api", ("src/api/**",)),
            "web": Zone("web", ("src/web/**",)),
            "contracts": Zone("contracts", ("contracts/**",), contract=True),
            "schema": Zone("schema", ("schema/**",), contract=True),
        }
    )


def test_classify_none() -> None:
    c = classify(["src/web/a.js"], ["src/api/a.py"], zones=zones_cfg(), task_zones=["api"])
    assert c.primary is Category.NONE and c.categories == (Category.NONE,)
    assert c.shared == () and c.to_data()["primary"] == "none"


def test_classify_textual() -> None:
    c = classify(["src/api/a.py"], ["src/api/a.py"], zones=zones_cfg(), task_zones=["api"])
    assert c.primary is Category.TEXTUAL and c.categories == (Category.TEXTUAL,)
    assert c.shared == ("src/api/a.py",)


def test_classify_out_of_zone_wins_over_everything() -> None:
    c = classify(
        ["src/web/a.js", "contracts/api.yml"],
        ["src/web/a.js"],
        zones=zones_cfg(),
        task_zones=["api", "contracts"],
    )
    assert c.categories == (Category.OUT_OF_ZONE, Category.SEMANTIC, Category.TEXTUAL)
    assert c.primary is Category.OUT_OF_ZONE and c.out_of_zone == ("src/web/a.js",)


def test_classify_semantic_via_own_contract_zone() -> None:
    c = classify(
        ["contracts/api.yml"], ["src/api/a.py"], zones=zones_cfg(), task_zones=["api", "contracts"]
    )
    assert c.primary is Category.SEMANTIC and c.semantic == {"contracts": ("contracts/api.yml",)}


def test_classify_semantic_via_depends_on() -> None:
    c = classify(
        ["schema/v2.sql"],
        ["src/api/a.py"],
        zones=zones_cfg(),
        task_zones=["api"],
        dependency_zones=["schema", "web"],
    )
    assert c.primary is Category.SEMANTIC and set(c.semantic) == {"schema"}
    plain = classify(["schema/v2.sql"], ["src/api/a.py"], zones=zones_cfg(), task_zones=["api"])
    assert plain.primary is Category.NONE  # a contract the task does not depend on


def test_classify_semantic_beats_textual() -> None:
    c = classify(
        ["contracts/api.yml", "src/api/a.py"],
        ["src/api/a.py"],
        zones=zones_cfg(),
        task_zones=["api", "contracts"],
    )
    assert c.categories == (Category.SEMANTIC, Category.TEXTUAL)
    assert c.primary is Category.SEMANTIC


def test_sync_reports_semantic_from_a_dependency(sw: SyncWorld) -> None:
    dep = sw.task("M1-T00", ["schema"])
    sw.fake.update_issue(dep, state="closed", state_reason="completed")
    n = sw.task("M1-T01", ["api"], deps=["M1-T00"])
    sw.claim("alice", n)
    sw.commit("alice", "src/api/a.py", "a\n")
    sw.upstream({"schema/v2.sql": "create table x;\n"})
    out = sync(sw.ctx())
    assert out.outcome == "pushed"
    assert out.classification is not None and out.classification.primary is Category.SEMANTIC
    assert out.upstream is not None and out.upstream.pulls[0].author == "bob"


# --------------------------------------------------------------------------- 10. preconditions


def test_no_current_task(sw: SyncWorld) -> None:
    sw.world.git("alice")
    with pytest.raises(PreconditionError, match="no current task"):
        sync(sw.ctx())
    with pytest.raises(PreconditionError, match="no sync in progress"):
        sync_continue(sw.ctx())


def test_claim_not_mine(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    sw.claim("bob", n)
    sw.checkout("alice", n)
    with pytest.raises(PreconditionError, match="belongs to bob"):
        sync(sw.ctx("alice"))


def test_stale_claim_is_refused(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    tip = head(git)
    with pytest.raises(PreconditionError, match="went stale") as info:
        sync(sw.ctx(now=T0 + timedelta(hours=30)))
    assert info.value.hint is not None and f"forge next --task {n}" in info.value.hint
    assert head(git) == tip and git.list_refs("refs/forge/backup/") == {}


def test_lease_in_the_future_is_not_stale(sw: SyncWorld) -> None:
    """Clock skew: a heartbeat written by a machine whose clock runs ahead never stalls sync."""
    n = sw.task("M1-T01", ["api"])
    sw.claim("alice", n, when=T0 + timedelta(hours=5))
    sw.commit("alice", "src/api/a.py", "a\n")
    assert sync(sw.ctx(now=T0)).outcome == "pushed"


def test_incomplete_claim_is_refused(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    sw.world.push_event("alice", n, claim_of("M1-T01", "alice"), T0)
    sw.checkout("alice", n)
    with pytest.raises(PreconditionError, match="incomplete"):
        sync(sw.ctx())


def test_sync_already_in_progress_and_foreign_rebase(sw: SyncWorld) -> None:
    _, _, git = in_zone_conflict(sw)
    assert sync(sw.ctx()).code == ExitCode.AGENT_ACTION
    with pytest.raises(PreconditionError, match="already in progress") as info:
        sync(sw.ctx())
    assert info.value.hint is not None and "--continue" in info.value.hint
    sync_abort(sw.ctx())
    git.run("rebase", "origin/main", check=False, env={"GIT_EDITOR": "true"})
    assert git.rebase_in_progress()
    with pytest.raises(PreconditionError, match="did not start"):
        sync(sw.ctx())


def test_local_branch_missing_remote_commits_is_refused(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.checkout("alice2", n)
    sw.commit("alice2", "src/api/b.py", "b\n")
    sw.push_task("alice2", n)
    with pytest.raises(PreconditionError, match="does not contain") as info:
        sync(sw.ctx())
    hint = info.value.hint
    assert hint is not None
    assert f"`git rebase origin/task/{n}`" in hint and "`git pull --rebase`" in hint
    assert "reset --hard" not in hint and "discard your local commits" in hint
    assert git.list_refs("refs/forge/backup/") == {}


def test_wip_commit_is_created_on_a_dirty_tree(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    (git.cwd / "src/api").mkdir(parents=True)
    (git.cwd / "src/api/wip.py").write_text("wip\n")
    out = sync(sw.ctx())
    assert out.outcome == "pushed" and status(git) == ""
    assert git.commits("HEAD^!")[0].subject == "forge: wip"
    assert sw.remote_tip(n) == head(git)


def test_commit_hook_rejecting_the_wip_commit(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    (git.cwd / "README.md").write_text("changed\n")
    (git.cwd / "new.txt").write_text("new\n")
    before, tip = status(git), head(git)
    hook(git, "pre-commit", "echo 'lint says no' >&2; exit 1")
    out = sync(sw.ctx())
    assert out.code == ExitCode.AGENT_ACTION and out.outcome == "wip_rejected"
    assert out.output is not None and "lint says no" in out.output
    assert head(git) == tip and status(git) == before
    assert read_sync(git.cwd) is None and git.list_refs("refs/forge/backup/") == {}


def test_pre_push_hook_rejection_is_exit_2(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "src/api/a.py", "a\n")
    remote = sw.remote_tip(n)
    path = hook(git, "pre-push", "echo 'tests must pass' >&2; exit 1")
    out = sync(sw.ctx())
    assert out.code == ExitCode.AGENT_ACTION and out.outcome == "push_rejected"
    assert out.output is not None and "tests must pass" in out.output
    assert sw.remote_tip(n) == remote
    path.unlink()
    done = sync_continue(sw.ctx())
    assert done.outcome == "pushed" and sw.remote_tip(n) == head(git)


def test_push_lease_race_one_winner(sw: SyncWorld) -> None:
    """Two clones of the same task race: the one that pushes first wins, the other gets
    exit 5 and can abort back to its exact pre-sync state."""
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "src/api/a.py", "a\n")
    sw.upstream({"src/web/x.js": "x\n"})
    tip = head(git)
    other = sw.checkout("alice2", n)
    sw.commit("alice2", "src/api/b.py", "b\n")
    results: dict[str, Any] = {}

    def race(src: str, dst: str) -> None:
        git.before_push = None
        results["other"] = sync(sw.ctx("alice", clone="alice2"))

    git.before_push = race
    loser = sync(sw.ctx())

    winner = results["other"]
    assert winner.code == ExitCode.OK and winner.outcome == "pushed"
    assert loser.code == ExitCode.PRECONDITION and loser.outcome == "lease_rejected"
    assert sw.remote_tip(n) == head(other)  # exactly one winner
    assert read_sync(git.cwd) is not None  # the loser can still abort
    assert sync_abort(sw.ctx()).code == ExitCode.OK
    assert head(git) == tip


def test_reclaim_during_a_stopped_sync(sw: SyncWorld) -> None:
    """Bob reclaims while alice's sync waits for her: --continue is refused (exit 5)."""
    n, _, _ = in_zone_conflict(sw)
    assert sync(sw.ctx()).code == ExitCode.AGENT_ACTION
    late = T0 + timedelta(hours=30)
    from support.scenarios import run

    taken = run(sw.world.ctx("bob", clock=Clock(late)), task=str(n), reclaim=True)
    assert taken.claimed is not None
    with pytest.raises(PreconditionError, match="belongs to bob"):
        sync_continue(sw.ctx(now=late))
    assert sync_abort(sw.ctx(now=late)).code == ExitCode.OK


def test_interrupted_after_push_converges_with_continue(sw: SyncWorld) -> None:
    """The push lands, then GitHub fails: --continue finishes without a second push."""
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    old = sw.lease(n).data
    sw.commit("alice", "src/api/a.py", "a\n")
    sw.upstream({"src/web/x.js": "x\n"})
    ctx = sw.ctx()
    # list_comments: 1st in the claim check, 2nd in the lease refresh after the push
    ctx.github = FlakyGitHub(sw.github("alice"), "list_comments", 2)  # type: ignore[assignment]
    with pytest.raises(GitHubError):
        sync(ctx)
    assert sw.remote_tip(n) == head(git)
    assert read_sync(git.cwd) is not None
    assert sw.lease(n).data == old  # still names the old sha; the run keeps it valid
    with pytest.raises(PreconditionError, match="already in progress"):
        sync(sw.ctx())
    done = sync_continue(sw.ctx())
    assert done.outcome == "pushed" and sw.remote_tip(n) == head(git)
    lease = sw.lease(n).data
    assert lease is not None and lease.claim == claim_commit(git).sha
    assert len(read_sync_log(git.cwd)) == 1


def test_rerere_replays_an_earlier_resolution(sw: SyncWorld) -> None:
    """A resolution recorded by rerere is reused: the second sync needs no agent."""
    n, _, git = in_zone_conflict(sw)
    failing = sync_config(checks={"gate": FAIL})
    assert sync(sw.ctx(config=failing)).code == ExitCode.AGENT_ACTION
    (git.cwd / "src/api/routes.py").write_text("upstream\ntask\n")
    git.add(["src/api/routes.py"])
    assert sync_continue(sw.ctx(config=failing)).outcome == "checks_failed"
    assert sync_abort(sw.ctx(config=failing)).code == ExitCode.OK

    out = sync(sw.ctx(config=sync_config(checks={"gate": PASS})))

    assert out.code == ExitCode.OK and out.outcome == "pushed", out.messages
    assert (git.cwd / "src/api/routes.py").read_text() == "upstream\ntask\n"
    assert sw.remote_tip(n) == head(git)
    entry = read_sync_log(git.cwd)[-1]
    assert [(c["path"], c["resolution"]) for c in entry["conflicts"]] == [
        ("src/api/routes.py", "rerere")
    ]
    assert out.conflicts[0].resolution == "rerere" and out.conflicts[0].zones == ("api",)
    summary = comments_with(sw, n, SYNC_MARKER)
    assert len(summary) == 1 and "src/api/routes.py`: rerere" in summary[0]


def test_replayed_out_of_zone_rerere_resolution_escalates(sw: SyncWorld) -> None:
    """A resolution recorded during a --force-resolve sync is replayed by rerere in a later
    plain sync: it is recorded as ``rerere`` and escalates (exit 4) instead of being pushed."""
    n, m, git = out_of_zone_conflict(sw)
    tip, before, remote = head(git), status(git), sw.remote_tip(n)
    failing = sync_config(checks={"gate": FAIL})
    forced = sync(sw.ctx(config=failing), SyncOptions(force_resolve=True))
    assert forced.code == ExitCode.AGENT_ACTION and forced.unresolved == ["src/web/app.js"]
    (git.cwd / "src/web/app.js").write_text("merged\n")
    git.add(["src/web/app.js"])
    assert sync_continue(sw.ctx(config=failing)).outcome == "checks_failed"  # rerere recorded
    assert sync_abort(sw.ctx(config=failing)).code == ExitCode.OK
    assert head(git) == tip and BLOCKED_CONFLICT not in sw.labels(n)
    passing = sync_config(checks={"gate": PASS})

    out = sync(sw.ctx(config=passing))

    assert out.code == ExitCode.ESCALATION and out.outcome == "escalated", out.messages
    assert out.unresolved == ["src/web/app.js"]
    assert [(c.path, c.resolution, c.outside) for c in out.conflicts] == [
        ("src/web/app.js", "rerere", True)
    ]
    assert head(git) == tip and status(git) == before and not git.rebase_in_progress()
    assert read_sync(git.cwd) is None and sw.remote_tip(n) == remote
    assert BLOCKED_CONFLICT in sw.labels(n)
    bodies = comments_with(sw, n, ESCALATION_MARKER)
    assert len(bodies) == 1
    assert "`src/web/app.js` (outside the task's zones; rerere replayed" in bodies[0]
    assert len(comments_with(sw, m, ESCALATION_MARKER)) == 1
    entry = read_sync_log(git.cwd)[-1]
    assert entry["outcome"] == "escalated"
    assert [(c["path"], c["resolution"]) for c in entry["conflicts"]] == [
        ("src/web/app.js", "rerere")
    ]

    # with --force-resolve the replayed resolution goes through without the agent
    done = sync(sw.ctx(config=passing), SyncOptions(force_resolve=True))
    assert done.code == ExitCode.OK and done.outcome == "pushed", done.messages
    assert (git.cwd / "src/web/app.js").read_text() == "merged\n"
    assert sw.remote_tip(n) == head(git)
    entry = read_sync_log(git.cwd)[-1]
    assert [(c["path"], c["resolution"]) for c in entry["conflicts"]] == [
        ("src/web/app.js", "rerere")
    ]
    assert BLOCKED_CONFLICT not in sw.labels(n)
    summary = comments_with(sw, n, SYNC_MARKER)
    assert len(summary) == 1 and "src/web/app.js`: rerere" in summary[0]


# --------------------------------------------------------------------------- recoverability


class FailAt(UserGitHub):
    """Raises on the ``k``-th GitHub call (any method, 1-based); records every call."""

    def __init__(self, base: UserGitHub, k: int) -> None:
        super().__init__(base._fake, base._login, base._lock)
        self.k = k
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        call = super().__getattr__(name)
        if not callable(call):
            return call

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            if len(self.calls) == self.k:
                raise GitHubError(f"injected failure in {name}", status=502)
            return call(*args, **kwargs)

        return counted


def recoverable_world(sw: SyncWorld) -> tuple[int, Git]:
    """Every GitHub step of a sync: a dependency (list_issues), an upstream PR closing an
    issue (commit_pulls), a union-resolved conflict (summary comment), a blocked:conflict
    label to clear, and a dirty tree (wip commit)."""
    dep = sw.task("M1-T00", ["schema"])
    sw.fake.update_issue(dep, state="closed", state_reason="completed")
    sw.upstream({"CHANGELOG.md": "# Changelog\n\n"}, link=False)
    n = sw.task("M1-T01", ["api"], deps=["M1-T00"])
    git = sw.claim("alice", n)
    sw.commit("alice", "CHANGELOG.md", "# Changelog\n\n- task entry\n")
    sw.upstream({"CHANGELOG.md": "# Changelog\n\n- upstream entry\n"}, closes=dep)
    sw.fake.add_labels(n, [BLOCKED_CONFLICT])
    (git.cwd / "src/api").mkdir(parents=True)
    (git.cwd / "src/api/wip.py").write_text("wip\n")
    return n, git


def test_a_github_failure_at_any_call_leaves_nothing_or_a_recoverable_sync(
    sw: SyncWorld, git_world: GitWorld, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Fail the k-th GitHub call of a sync, for every k. Either the failure is only a warning
    (upstream lookups), or nothing changed (no wip commit, no backup ref, no sync.json) and a
    re-run pushes, or the push already happened and sync.json lets --continue converge."""
    n, git = recoverable_world(sw)
    probe = FailAt(sw.github("alice"), 0)
    ctx = sw.ctx()
    ctx.github = probe  # type: ignore[assignment]
    assert sync(ctx).outcome == "pushed"
    total = len(probe.calls)
    assert {"list_issues", "commit_pulls", "remove_label", "create_comment"} <= set(probe.calls)
    assert total >= 8

    seen = {"untouched": 0, "continue": 0, "tolerated": 0}
    for k in range(1, total + 1):
        world = SyncWorld(type(git_world)(tmp_path_factory.mktemp(f"k{k}")))
        n, git = recoverable_world(world)
        tip, before, remote = head(git), status(git), world.remote_tip(n)
        flaky = FailAt(world.github("alice"), k)
        ctx = world.ctx()
        ctx.github = flaky  # type: ignore[assignment]
        try:
            out = sync(ctx)
        except GitHubError:
            out = None
        assert len(flaky.calls) >= k
        record = read_sync(git.cwd)
        if out is not None:  # a lookup whose failure is only a warning
            seen["tolerated"] += 1
            assert out.outcome == "pushed" and record is None, k
            if flaky.calls[k - 1] == "update_comment":
                # documented upsert_lease fallback: a new lease comment, the newest wins
                lease = world.lease(n).data
                assert lease is not None and lease.claim == claim_commit(git).sha, k
            else:
                assert any("injected failure" in w for w in out.warnings), (k, out.warnings)
        elif record is None:
            seen["untouched"] += 1
            assert head(git) == tip and status(git) == before, k
            assert git.list_refs(f"{BACKUP_PREFIX}/") == {}, k
            assert world.remote_tip(n) == remote, k
            assert sync(world.ctx()).outcome == "pushed", k
        else:
            # no GitHub call between the wip commit and the push: a sync.json left by a
            # GitHub failure is always past the push, and --continue finishes it
            seen["continue"] += 1
            assert record.wip and world.remote_tip(n) == head(git) != remote, k
            done = sync_continue(world.ctx())
            assert done.outcome == "pushed", (k, done.messages)
            assert world.remote_tip(n) == head(git) and read_sync(git.cwd) is None, k
        final = read_sync_log(git.cwd)[-1]
        assert final["outcome"] == "pushed", k
        assert [(c["path"], c["resolution"]) for c in final["conflicts"]] == [
            ("CHANGELOG.md", "union")
        ], k
        assert BLOCKED_CONFLICT not in world.labels(n), k
        assert len(comments_with(world, n, SYNC_MARKER)) == 1, k
    assert all(seen.values()), seen  # every recovery path was exercised


# --------------------------------------------------------------------------- abort safety


def checks_failed_sync(sw: SyncWorld) -> tuple[int, Git, str, Any]:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "src/api/a.py", "broken\n")
    tip = head(git)
    sw.upstream({"src/web/x.js": "x\n"})
    config = sync_config(checks={"lint": file_contains("src/api/a.py", "fixed")})
    assert sync(sw.ctx(config=config)).outcome == "checks_failed"
    return n, git, tip, config


def test_abort_refuses_a_dirty_tree_after_failed_checks(sw: SyncWorld) -> None:
    n, git, _, config = checks_failed_sync(sw)
    rebased = head(git)
    (git.cwd / "src/api/a.py").write_text("fixed, not committed\n")
    (git.cwd / "src/api/new.py").write_text("untracked\n")
    before = status(git)

    with pytest.raises(PreconditionError, match="uncommitted changes") as info:
        sync_abort(sw.ctx(config=config))

    assert info.value.code == ExitCode.PRECONDITION
    assert info.value.hint is not None and "commit or discard your changes first" in info.value.hint
    assert head(git) == rebased and status(git) == before
    assert (git.cwd / "src/api/a.py").read_text() == "fixed, not committed\n"
    assert read_sync(git.cwd) is not None
    assert len(git.list_refs(f"{BACKUP_PREFIX}/task-{n}/")) == 1


def test_abort_saves_commits_made_after_failed_checks(sw: SyncWorld) -> None:
    n, git, tip, config = checks_failed_sync(sw)
    (git.cwd / "src/api/a.py").write_text("fixed\n")
    git.add(["src/api/a.py"])
    fix = git.commit("fix: lint")
    record = read_sync(git.cwd)
    assert record is not None

    out = sync_abort(sw.ctx(config=config, now=T0 + timedelta(hours=2)))

    assert out.code == ExitCode.OK and out.outcome == "aborted"
    assert out.saved_ref == f"{BACKUP_PREFIX}/task-{n}/20261102T110000Z{ABORTED_SUFFIX}"
    assert git.rev_parse(out.saved_ref) == fix
    assert out.to_data()["saved_ref"] == out.saved_ref
    assert any(out.saved_ref in m.text for m in out.messages)
    assert head(git) == tip and status(git) == ""
    assert set(git.list_refs(f"{BACKUP_PREFIX}/task-{n}/")) == {record.backup_ref, out.saved_ref}


def test_abort_mid_rebase_and_at_the_backup_saves_nothing(sw: SyncWorld) -> None:
    n, _, git = in_zone_conflict(sw)
    assert sync(sw.ctx()).code == ExitCode.AGENT_ACTION
    (git.cwd / "src/api/routes.py").write_text("half resolved\n")  # rebase --abort drops it
    out = sync_abort(AbortContext(git=git, clock=Clock(T0 + timedelta(hours=2))))
    assert out.code == ExitCode.OK and out.saved_ref is None
    assert len(git.list_refs(f"{BACKUP_PREFIX}/task-{n}/")) == 1


# --------------------------------------------------------------------------- backup retention


def fake_backups(git: Git, issue: int, names: list[str]) -> list[str]:
    refs = [f"{BACKUP_PREFIX}/task-{issue}/{name}" for name in names]
    for ref in refs:
        git.update_ref(ref, head(git))
    return refs


def test_sync_keeps_the_newest_backup_refs(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    old = fake_backups(git, n, [f"202001{d:02d}T000000Z" for d in range(1, MAX_BACKUPS + 2)])
    other = fake_backups(git, n + 1, ["20200101T000000Z"])  # another task: untouched
    sw.commit("alice", "src/api/a.py", "a\n")
    sw.upstream({"src/web/x.js": "x\n"})

    out = sync(sw.ctx())

    assert out.outcome == "pushed"
    assert out.backup_ref == f"{BACKUP_PREFIX}/task-{n}/20261102T100000Z"
    assert set(git.list_refs(f"{BACKUP_PREFIX}/task-{n}/")) == {*old[2:], out.backup_ref}
    assert set(git.list_refs(f"{BACKUP_PREFIX}/task-{n + 1}/")) == set(other)


def test_backup_of_an_in_progress_sync_is_never_pruned(sw: SyncWorld) -> None:
    """Clock skew: existing backups look newer than the one of the running sync."""
    n, _, git = in_zone_conflict(sw)
    future = fake_backups(git, n, [f"209901{d:02d}T000000Z" for d in range(1, MAX_BACKUPS + 1)])
    out = sync(sw.ctx())
    assert out.code == ExitCode.AGENT_ACTION and out.backup_ref is not None
    refs = set(git.list_refs(f"{BACKUP_PREFIX}/task-{n}/"))
    assert refs == {out.backup_ref, *future[1:]}
    # a later prune (e.g. from another sync) still keeps the in-progress one
    fake_backups(git, n, ["20991231T000000Z"])
    assert prune_backups(git, n) == [future[1]]
    assert out.backup_ref in git.list_refs(f"{BACKUP_PREFIX}/task-{n}/")
    assert sync_abort(sw.ctx()).code == ExitCode.OK


def test_backup_suffixes_sort_numerically(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    stamp = "20200101T000000Z"
    refs = fake_backups(git, n, [stamp, *(f"{stamp}-{i}" for i in range(2, MAX_BACKUPS + 2))])
    assert prune_backups(git, n) == [refs[0]]  # "-10" and "-11" are newer than "-2"
    assert set(git.list_refs(f"{BACKUP_PREFIX}/task-{n}/")) == set(refs[1:])


def test_interrupted_after_the_wip_commit_is_undone_by_abort(sw: SyncWorld) -> None:
    """A crash right after the preflight (here: in the checks) leaves sync.json (running), so
    --abort takes the wip commit back and restores the uncommitted changes."""
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "src/api/a.py", "a\n")
    (git.cwd / "src/api/a.py").write_text("edited\n")
    (git.cwd / "src/api/new.py").write_text("untracked\n")
    tip, before = head(git), status(git)

    def crash(command: str, cwd: Path) -> Any:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        sync(sw.ctx(config=sync_config(checks={"t": PASS}), run_check=crash))
    record = read_sync(git.cwd)
    assert record is not None and record.state.value == "running" and record.wip
    assert head(git) != tip and git.commits("HEAD^!")[0].subject == "forge: wip"

    assert sync_abort(sw.ctx()).code == ExitCode.OK
    assert head(git) == tip and status(git) == before
    assert (git.cwd / "src/api/a.py").read_text() == "edited\n"


def test_resolver_paths_replayed_by_rerere_go_through_the_resolver_again(sw: SyncWorld) -> None:
    """rerere records the resolvers' results too. On a later sync the resolver still owns
    those paths (outside every zone): they are resolved again, never escalated."""
    sw.upstream({"package-lock.json": "lock v1\n", "CHANGELOG.md": "# Changelog\n\n"}, link=False)
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "package-lock.json", "lock task\n")
    sw.commit("alice", "CHANGELOG.md", "# Changelog\n\n- task entry\n")
    sw.upstream(
        {"package-lock.json": "lock upstream\n", "CHANGELOG.md": "# Changelog\n\n- upstream\n"}
    )
    tip = head(git)
    failing = sync_config(checks={"gate": FAIL}, resolvers=DEFAULT_RESOLVERS)
    first = Recorder()
    assert sync(sw.ctx(config=failing, run_command=first)).outcome == "checks_failed"
    assert first.calls == [(LOCK_COMMAND, git.cwd)]
    assert sync_abort(sw.ctx(config=failing)).code == ExitCode.OK
    assert head(git) == tip

    again = Recorder()
    out = sync(sw.ctx(run_command=again))

    assert out.code == ExitCode.OK and out.outcome == "pushed", out.messages
    assert again.calls == [(LOCK_COMMAND, git.cwd)]  # regenerated again, not replayed
    assert (git.cwd / "package-lock.json").read_text() == "lock upstream\nregenerated\n"
    text = (git.cwd / "CHANGELOG.md").read_text()
    assert "- task entry" in text and "- upstream" in text and "<<<<" not in text
    entry = read_sync_log(git.cwd)[-1]
    assert [(c["path"], c["resolution"]) for c in entry["conflicts"]] == [
        ("package-lock.json", "regenerate"),
        ("CHANGELOG.md", "union"),
    ]
    assert comments_with(sw, n, ESCALATION_MARKER) == []
    assert BLOCKED_CONFLICT not in sw.labels(n)


def test_resolver_path_replayed_by_rerere_never_escalates_without_its_conflict(
    sw: SyncWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback: when git cannot re-create the conflict, the replayed resolution of a resolver
    path is kept (recorded as rerere) and does not escalate."""
    sw.upstream({"CHANGELOG.md": "# Changelog\n\n"}, link=False)
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "CHANGELOG.md", "# Changelog\n\n- task entry\n")
    sw.upstream({"CHANGELOG.md": "# Changelog\n\n- upstream\n"})
    failing = sync_config(checks={"gate": FAIL}, resolvers=DEFAULT_RESOLVERS)
    assert sync(sw.ctx(config=failing)).outcome == "checks_failed"
    assert sync_abort(sw.ctx(config=failing)).code == ExitCode.OK
    monkeypatch.setattr(Git, "restore_conflict", lambda self, path: False)

    out = sync(sw.ctx())

    assert out.code == ExitCode.OK and out.outcome == "pushed", out.messages
    entry = read_sync_log(git.cwd)[-1]
    assert [(c["path"], c["resolution"], c["outside"]) for c in entry["conflicts"]] == [
        ("CHANGELOG.md", "rerere", True)
    ]
    assert comments_with(sw, n, ESCALATION_MARKER) == []


# --------------------------------------------------------------------------- restarted rebases


def test_abort_after_a_restarted_rebase_saves_the_new_commits(sw: SyncWorld) -> None:
    """The rebase is aborted outside Forge, a commit X is made, --continue restarts the
    rebase from X and stops: --abort must not drop X silently."""
    n, _, git = in_zone_conflict(sw)
    tip = head(git)
    assert sync(sw.ctx()).code == ExitCode.AGENT_ACTION
    git.rebase_abort()
    x = sw.commit("alice", "src/api/x.py", "x\n", "feat: x")
    again = sync_continue(sw.ctx())
    assert again.code == ExitCode.AGENT_ACTION and git.rebase_in_progress()

    out = sync_abort(sw.ctx(now=T0 + timedelta(hours=2)))

    assert out.code == ExitCode.OK and out.outcome == "aborted"
    assert out.saved_ref == f"{BACKUP_PREFIX}/task-{n}/20261102T110000Z{ABORTED_SUFFIX}"
    assert git.rev_parse(out.saved_ref) == x
    assert head(git) == tip and not git.is_ancestor(x, "HEAD")
    saved_msgs = [m for m in out.messages if out.saved_ref in m.text]
    assert len(saved_msgs) == 1 and saved_msgs[0].hint is not None
    assert f"{out.to_data()['backup_ref']}..{out.saved_ref}" in saved_msgs[0].hint


def test_escalation_after_a_restarted_rebase_saves_the_new_commits(sw: SyncWorld) -> None:
    """Same restart, but the new commit X conflicts outside the zones: the escalation saves
    X, reports it, and its comment no longer claims the branch is unchanged."""
    m = sw.task("M1-T09", ["web"])
    sw.upstream(
        {"src/api/routes.py": "base\n", "src/web/app.js": "base\n"}, link=False, message="base"
    )
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    sw.commit("alice", "src/api/routes.py", "task\n")
    tip = head(git)
    sw.upstream({"src/api/routes.py": "up\n", "src/web/app.js": "up\n"}, closes=m)
    assert sync(sw.ctx()).code == ExitCode.AGENT_ACTION  # in-zone stop on routes.py
    git.rebase_abort()
    x = sw.commit("alice", "src/web/app.js", "task\n", "feat: touch web")
    assert sync_continue(sw.ctx()).code == ExitCode.AGENT_ACTION  # restarted from X
    (git.cwd / "src/api/routes.py").write_text("up\ntask\n")
    git.add(["src/api/routes.py"])

    out = sync_continue(sw.ctx(now=T0 + timedelta(hours=2)))

    assert out.code == ExitCode.ESCALATION and out.outcome == "escalated", out.messages
    assert out.unresolved == ["src/web/app.js"]
    assert out.saved_ref == f"{BACKUP_PREFIX}/task-{n}/20261102T110000Z{ABORTED_SUFFIX}"
    assert git.rev_parse(out.saved_ref) == x
    assert out.to_data()["saved_ref"] == out.saved_ref
    assert head(git) == tip and not git.rebase_in_progress() and read_sync(git.cwd) is None
    assert any(out.saved_ref in w for w in out.warnings)
    bodies = comments_with(sw, n, ESCALATION_MARKER)
    assert len(bodies) == 1
    assert "branch is unchanged" not in bodies[0]
    assert "reset to its state before the sync" in bodies[0] and out.saved_ref in bodies[0]


def test_aborted_refs_are_never_pruned(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    names = ["20190101T000000Z-aborted", "20190101T000000Z-aborted-2", "20190102T000000Z-aborted"]
    aborted = fake_backups(git, n, names)
    regular = fake_backups(git, n, [f"202001{d:02d}T000000Z" for d in range(1, MAX_BACKUPS + 2)])
    assert prune_backups(git, n) == [regular[0]]  # 11 regular refs: one goes, aborted ones stay
    assert set(git.list_refs(f"{BACKUP_PREFIX}/task-{n}/")) == {*aborted, *regular[1:]}


def test_interrupted_abort_after_the_last_reset_converges(
    sw: SyncWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--abort killed after `reset --mixed original_tip` but before sync.json is removed: the
    re-run sees the wip changes back in place and only removes sync.json."""
    import forge.core.sync as sync_module

    _, _, git = in_zone_conflict(sw)
    (git.cwd / "src/api/routes.py").write_text("task, edited\n")
    (git.cwd / "src/api/new.py").write_text("untracked\n")
    tip, before = head(git), status(git)
    assert sync(sw.ctx()).code == ExitCode.AGENT_ACTION
    real = sync_module.remove_sync

    def killed(root: Path) -> None:
        monkeypatch.setattr(sync_module, "remove_sync", real)
        raise KeyboardInterrupt

    monkeypatch.setattr(sync_module, "remove_sync", killed)
    with pytest.raises(KeyboardInterrupt):
        sync_abort(sw.ctx())
    assert read_sync(git.cwd) is not None and head(git) == tip and status(git) == before

    out = sync_abort(sw.ctx())

    assert out.code == ExitCode.OK and out.outcome == "aborted" and out.saved_ref is None
    assert read_sync(git.cwd) is None and head(git) == tip and status(git) == before
    assert (git.cwd / "src/api/routes.py").read_text() == "task, edited\n"


@pytest.mark.skipif(
    sys.platform == "win32", reason="chmod(0) does not make a file unreadable on Windows"
)
def test_failing_git_add_is_not_reported_as_a_hook_rejection(sw: SyncWorld) -> None:
    n = sw.task("M1-T01", ["api"])
    git = sw.claim("alice", n)
    (git.cwd / "README.md").write_text("changed\n")
    secret = git.cwd / "unreadable.txt"
    secret.write_text("x\n")
    secret.chmod(0)
    tip, before = head(git), status(git)
    try:
        with pytest.raises(ForgeError) as info:
            sync(sw.ctx())
        assert type(info.value) is ForgeError and info.value.code == ExitCode.ERROR
        assert "`git add -A` failed" in info.value.message and "hook" not in info.value.message
        assert "unreadable.txt" in info.value.message
        assert head(git) == tip and status(git) == before
        assert read_sync(git.cwd) is None and git.list_refs(f"{BACKUP_PREFIX}/") == {}
    finally:
        secret.chmod(0o644)


def test_abort_mid_rebase_warns_about_discarded_resolutions(sw: SyncWorld) -> None:
    _, _, git = in_zone_conflict(sw)
    assert sync(sw.ctx()).code == ExitCode.AGENT_ACTION
    (git.cwd / "src/api/routes.py").write_text("upstream\ntask\n")
    git.add(["src/api/routes.py"])  # a staged resolution in progress

    out = sync_abort(sw.ctx())

    assert out.code == ExitCode.OK
    assert out.warnings == [
        "the rebase abort discarded the uncommitted changes of the stopped rebase "
        "(conflict resolutions in progress) in: src/api/routes.py"
    ]
    assert (git.cwd / "src/api/routes.py").read_text() == "task\n"


def test_abort_without_a_rebase_has_no_discard_warning(sw: SyncWorld) -> None:
    _, git, tip, config = checks_failed_sync(sw)
    out = sync_abort(sw.ctx(config=config))
    assert out.code == ExitCode.OK and out.warnings == [] and head(git) == tip
