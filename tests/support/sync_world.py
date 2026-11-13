"""Scenario helpers for the sync and escalation tests (not a test module).

A :class:`SyncWorld` wraps the claim ``World`` of :mod:`support.scenarios`: a bare remote,
one clone per simulated user and a shared ``FakeGitHub``. Users claim tasks through the real
``forge next`` core, check out ``task/<n>`` in place with a ``current.json``, commit, and
``lead`` lands upstream commits on ``main`` (optionally through a pull request linked with
``fake_link_commit``, as a squash merge would be).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forge.core.config import ForgeConfig, parse_config
from forge.core.git import Git, Lease
from forge.core.github_fake import FakeGitHub
from forge.core.lease import FoundLease, find_lease
from forge.core.sync import CommandRun, SyncContext, run_shell
from forge.core.tasks import TaskType
from forge.core.team import Member, Team
from forge.core.validate import Fields
from forge.core.workspace import CurrentTask, write_current
from forge.core.zones import Zone, ZonesConfig
from support.scenarios import AGENT, T0, Clock, HookedGit, UserGitHub, World, run
from support.shell import append_line

if TYPE_CHECKING:
    from conftest import GitWorld

SYNC_ZONES = ZonesConfig(
    zones={
        "api": Zone("api", ("src/api/**",)),
        "web": Zone("web", ("src/web/**",)),
        "contracts": Zone("contracts", ("contracts/**",), contract=True),
        "schema": Zone("schema", ("schema/**",), contract=True),
        "docs": Zone("docs", ("docs/**",)),
    }
)

LOCK_COMMAND = append_line("package-lock.json", "regenerated")
"""Appends a line, so a test can tell which side ``regenerate`` started from."""


def sync_config(
    *,
    checks: Mapping[str, str] | None = None,
    resolvers: Sequence[Mapping[str, str]] | None = None,
    lease_hours: int = 24,
) -> ForgeConfig:
    data: dict[str, Any] = {
        "forge_version": ">=0.2,<0.3",
        "project": {"name": "shop", "default_branch": "main"},
        "github": {"repo": "acme/shop"},
        "lease_hours": lease_hours,
    }
    if checks is not None:
        data["checks"] = dict(checks)
    if resolvers is not None:
        data["resolvers"] = [dict(r) for r in resolvers]
    return parse_config(Fields(data, "config.yml"))


DEFAULT_RESOLVERS = (
    {"glob": "package-lock.json", "strategy": "regenerate", "command": LOCK_COMMAND},
    {"glob": "CHANGELOG.md", "strategy": "union"},
)


class Recorder:
    """A shell runner that records its calls and delegates to the real shell."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def __call__(self, command: str, cwd: Path) -> CommandRun:
        self.calls.append((command, cwd))
        return run_shell(command, cwd)


class SyncWorld:
    """Claimed tasks checked out in place, upstream commits and sync contexts."""

    def __init__(self, git_world: GitWorld) -> None:
        self.world = World(git_world)
        self.fake: FakeGitHub = self.world.fake
        self._pr_heads = 0

    # ---- tasks and claims

    def task(
        self,
        task_id: str,
        zones: Sequence[str],
        *,
        deps: Sequence[str] = (),
        type: TaskType = TaskType.IMPL,
        allow_contract_change: bool = False,
    ) -> int:
        return self.world.task(
            task_id, zones, deps=deps, type=type, allow_contract_change=allow_contract_change
        )

    def claim(self, user: str, issue: int, *, when: datetime = T0) -> HookedGit:
        """Claim ``issue`` as ``user`` (real ``forge next``) and check it out in place."""
        ctx = replace(self.world.ctx(user, clock=Clock(when)), zones=SYNC_ZONES)
        outcome = run(ctx, task=str(issue))
        claimed = outcome.claimed
        assert claimed is not None, outcome
        git = self.world.git(user)
        git.fetch()
        git.run("checkout", "--quiet", "-B", f"task/{issue}", f"origin/task/{issue}")
        write_current(
            git.cwd,
            CurrentTask(
                task=claimed.task_id,
                issue=issue,
                branch=claimed.branch,
                base=claimed.base,
                claim=claimed.claim,
                agent=claimed.agent,
                claimed_at=claimed.claimed_at,
                worktree=None,
            ),
        )
        return git

    def checkout(self, clone: str, issue: int) -> HookedGit:
        """Check out ``origin/task/<issue>`` as ``task/<issue>`` in ``clone`` (no current.json)."""
        git = self.world.git(clone)
        git.fetch()
        git.run("checkout", "--quiet", "-B", f"task/{issue}", f"origin/task/{issue}")
        return git

    # ---- commits

    def commit(self, user: str, path: str, content: str, message: str = "feat: work") -> str:
        """Commit ``path`` on the branch checked out in ``user``'s clone (not pushed)."""
        git = self.world.git(user)
        target = git.cwd / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git.add([path])
        return git.commit(message)

    def push_task(self, user: str, issue: int) -> str:
        """Fast-forward ``origin/task/<issue>`` to ``user``'s local branch (plain push)."""
        git = self.world.git(user)
        result = git.push(f"refs/heads/task/{issue}", f"refs/heads/task/{issue}")
        assert result.ok, result.stderr
        sha = git.rev_parse(f"refs/heads/task/{issue}")
        assert sha is not None
        return sha

    def upstream(
        self,
        files: Mapping[str, str],
        *,
        author: str = "bob",
        closes: int | None = None,
        message: str = "feat: upstream change",
        link: bool = True,
    ) -> tuple[str, int | None]:
        """Land a commit on ``main`` (as ``lead``'s clone); return (sha, pull request number).

        With ``link`` a pull request by ``author`` (body ``Closes #closes``) is created and
        linked to the commit, as GitHub links a squash merge.
        """
        git = self.world.git("lead")
        git.fetch()
        git.run("checkout", "--quiet", "-B", "main", "origin/main")
        for path, content in files.items():
            target = git.cwd / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git.add(list(files))
        sha = git.commit(message)
        result = git.push("refs/heads/main", "refs/heads/main")
        assert result.ok, result.stderr
        if not link:
            return sha, None
        self._pr_heads += 1
        self.fake.fake_set_user(author)
        body = f"Closes #{closes}" if closes is not None else "No issue."
        pr = self.fake.create_pull(message, body, f"feature/{self._pr_heads}", "main")
        self.fake.fake_link_commit(sha, pr.number)
        return sha, pr.number

    # ---- contexts and probes

    def ctx(
        self,
        user: str = "alice",
        *,
        config: ForgeConfig | None = None,
        now: datetime = T0 + timedelta(hours=1),
        clock: Callable[[], datetime] | None = None,
        run_check: Callable[[str, Path], CommandRun] | None = None,
        run_command: Callable[[str, Path], CommandRun] | None = None,
        team: Team | None = None,
        profile_login: str | None = None,
        clone: str | None = None,
    ) -> SyncContext:
        """A sync context for login ``user`` working in clone ``clone`` (default: ``user``)."""
        extra: dict[str, Any] = {}
        if run_check is not None:
            extra["run_check"] = run_check
        if run_command is not None:
            extra["run_command"] = run_command
        return SyncContext(
            config=config or sync_config(resolvers=DEFAULT_RESOLVERS),
            git=self.world.git(clone or user),
            github=UserGitHub(self.fake, user, self.world.lock),  # type: ignore[arg-type]
            zones=SYNC_ZONES,
            agent=AGENT,
            profile_login=profile_login or user,
            team=team,
            clock=clock or Clock(now),
            **extra,
        )

    def github(self, user: str) -> UserGitHub:
        return UserGitHub(self.fake, user, self.world.lock)

    def remote_tip(self, issue: int) -> str | None:
        return self.world.remote_tip(issue)

    def lease(self, issue: int) -> FoundLease:
        found = find_lease(self.fake.list_comments(issue))
        assert found is not None and found.data is not None
        return found

    def labels(self, issue: int) -> tuple[str, ...]:
        return self.fake.get_issue(issue).labels


def status(git: Git) -> str:
    """``git status --porcelain`` (untracked files included)."""
    return git.run("status", "--porcelain", "--untracked-files=all").stdout


def team_with_leads(*leads: str) -> Team:
    return Team(
        members=tuple(
            Member(github=login, name=login, roles=frozenset({"lead"})) for login in leads
        )
    )


def rebase_remote_task(world: World, user: str, issue: int, path: str = "src/web/up.js") -> str:
    """Land a commit on ``main``, then rebase ``task/<issue>`` onto it and push it with a lease,
    as a sync does before it rewrites the lease. Returns the new remote tip."""
    lead = world.git("lead")
    lead.fetch()
    lead.run("checkout", "--quiet", "-B", "main", "origin/main")
    (lead.cwd / path).parent.mkdir(parents=True, exist_ok=True)
    (lead.cwd / path).write_text("upstream\n")
    lead.add([path])
    lead.commit("feat: upstream")
    assert lead.push("refs/heads/main", "refs/heads/main").ok
    git = world.git(user)
    git.fetch()
    old = world.remote_tip(issue)
    assert old is not None
    git.run("checkout", "--quiet", "-B", f"task/{issue}", old)
    git.run("rebase", "--quiet", "--keep-empty", "--committer-date-is-author-date", "origin/main")
    new = git.rev_parse("HEAD")
    assert new is not None and new != old
    ref = f"refs/heads/task/{issue}"
    assert git.push(new, ref, lease=Lease(ref, old)).ok
    git.run("checkout", "--quiet", "main")
    return new
