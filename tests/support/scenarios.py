"""Shared scenario helpers for the Forge tests (not a test module).

- Claim world (from the Phase 3 claim tests): a shared bare remote plus one ``HookedGit`` clone
  per simulated user, a shared ``FakeGitHub`` seen through one ``UserGitHub`` proxy per user,
  and the constants, config and agent the claim tests use.
- Status scenario (ADR 0010): ``Seeder`` builds issues, ``main`` commits, claims and comments on
  top of a ``World``; ``seed_full`` seeds the full ``forge status`` scenario.

Imported as ``support.scenarios`` (``pythonpath = ["tests"]`` in ``pyproject.toml``).
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forge.core.claim import ClaimContext, NextOptions, NextOutcome, claim_next
from forge.core.config import ForgeConfig, parse_config
from forge.core.context import HANDOFF_MARKER
from forge.core.git import Git, Lease, PushResult
from forge.core.github import CiStatus, GitHubError
from forge.core.github_fake import FakeGitHub
from forge.core.lease import LeaseData, render_lease
from forge.core.presets import Preset
from forge.core.profile import AgentProfile, Profile
from forge.core.refresh import ensure_labels
from forge.core.report import StatusContext
from forge.core.tasks import (
    BLOCKED_CONFLICT,
    TRAILER_AGENT,
    TRAILER_CLAIM,
    TRAILER_HANDOFF,
    TRAILER_TASK,
    Size,
    TaskMetadata,
    TaskType,
    upsert_block,
)
from forge.core.tiers import Tier
from forge.core.validate import Fields
from forge.core.zones import Zone, ZonesConfig

if TYPE_CHECKING:
    from conftest import GitWorld

# --------------------------------------------------------------------------- claim world

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
HOST = "test-host"
PID = 4242  # default pid of a simulated run; runs are dead once they return (World.alive)
WAIT = 20.0  # seconds a test thread waits for its peer before failing (never reached when green)

ZONES = ZonesConfig(
    zones={
        "api": Zone("api", ("src/api/**",)),
        "api-v2": Zone("api-v2", ("src/api/v2/**",)),
        "web": Zone("web", ("src/web/**",)),
        "db": Zone("db", ("src/db/**",)),
        "docs": Zone("docs", ("docs/**",)),
    }
)

AGENT = AgentProfile(
    id="claude",
    tool="claude-code",
    providers=("anthropic",),
    tiers={Tier.STRONG: "opus", Tier.STANDARD: "sonnet", Tier.LIGHT: "haiku"},
)
LIGHT_AGENT = AgentProfile(id="local", tool="ollama", providers=("local",), tiers={Tier.LIGHT: "x"})


# The scenarios pin a fixed CLI version, so a version bump of Forge does not change them.
SCENARIO_CLI_VERSION = "0.1.0"


def make_config(*, max_attempts: int = 5, lease_hours: int = 24, version: str = ">=0.1,<0.2"):
    data = {
        "forge_version": version,
        "project": {"name": "shop", "default_branch": "main"},
        "github": {"repo": "acme/shop"},
        "lease_hours": lease_hours,
        "claim": {"max_attempts": max_attempts},
    }
    return parse_config(Fields(data, "config.yml"))


# --------------------------------------------------------------------------- fixtures


class Clock:
    """A settable clock."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Sleeper:
    """Injected ``sleep``: records calls and runs an optional hook instead of sleeping."""

    def __init__(self, hook: Callable[[], None] | None = None) -> None:
        self.calls: list[float] = []
        self.hook = hook

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.hook is not None:
            self.hook()


class UserGitHub:
    """One user's view of the shared FakeGitHub; every call is atomic under one lock."""

    def __init__(self, fake: FakeGitHub, login: str, lock: threading.RLock) -> None:
        self._fake = fake
        self._login = login
        self._lock = lock
        self.fail: dict[str, int] = {}  # method name -> number of calls that raise
        self.after: dict[str, Callable[[], None]] = {}  # method name -> hook run once after it

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._fake, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            if self.fail.get(name, 0) > 0:
                self.fail[name] -= 1
                raise GitHubError(f"injected failure in {name}", status=502)
            with self._lock:
                self._fake.fake_set_user(self._login)
                result = attr(*args, **kwargs)
            hook = self.after.pop(name, None)
            if hook is not None:
                hook()
            return result

        return call


class HookedGit(Git):
    """A clone whose pushes and fetches can run test hooks (to interleave users)."""

    def __init__(self, cwd: Path) -> None:
        super().__init__(cwd)
        self.before_push: Callable[[str, str], None] | None = None
        self.after_push: Callable[[str, str, PushResult], None] | None = None
        self.after_fetch: Callable[[], None] | None = None
        self.pushes: list[tuple[str, str, bool]] = []  # (src, dst, no_verify)

    def push(  # type: ignore[override]
        self, src: str, dst_ref: str, **kwargs: Any
    ) -> PushResult:
        if self.before_push is not None:
            self.before_push(src, dst_ref)
        result = super().push(src, dst_ref, **kwargs)
        self.pushes.append((src, dst_ref, bool(kwargs.get("no_verify"))))
        if self.after_push is not None:
            self.after_push(src, dst_ref, result)
        return result

    def fetch(self, *args: Any, **kwargs: Any) -> None:
        super().fetch(*args, **kwargs)
        if self.after_fetch is not None:
            self.after_fetch()


@dataclass
class World:
    """Shared remote + FakeGitHub, with one clone and one context per user."""

    git_world: GitWorld
    fake: FakeGitHub = field(default_factory=FakeGitHub)
    lock: threading.RLock = field(default_factory=threading.RLock)
    clones: dict[str, HookedGit] = field(default_factory=dict)
    alive: set[int] = field(default_factory=set)  # pids of simulated runs still running

    def __post_init__(self) -> None:
        ensure_labels(self.fake)

    def git(self, user: str) -> HookedGit:
        if user not in self.clones:
            plain = self.git_world.clone(user)
            self.clones[user] = HookedGit(plain.cwd)
        return self.clones[user]

    def ctx(
        self,
        user: str,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        agent: AgentProfile = AGENT,
        config: ForgeConfig | None = None,
        max_polls: int = 5,
        profile_login: str | None = None,
        pid: int = PID,
        host: str | None = HOST,
        run_id: Callable[[], str] | None = None,
        clone: str | None = None,
    ) -> ClaimContext:
        """``user`` is the GitHub login; ``clone`` (default: ``user``) the local clone."""
        extra: dict[str, Any] = {} if run_id is None else {"run_id": run_id}
        return ClaimContext(
            config=config or make_config(),
            git=self.git(clone or user),
            github=UserGitHub(self.fake, user, self.lock),  # type: ignore[arg-type]
            zones=ZONES,
            agent=agent,
            profile_login=profile_login or user,
            clock=clock or Clock(),
            sleep=sleep or Sleeper(),
            max_polls=max_polls,
            pid=pid,
            host=host,
            pid_running=lambda p: p in self.alive,
            cli_version=SCENARIO_CLI_VERSION,
            **extra,
        )

    def task(
        self,
        task_id: str,
        zones: Sequence[str],
        *,
        deps: Sequence[str] = (),
        size: Size = Size.S,
        tier: Tier = Tier.STANDARD,
        milestone: str | None = None,
        labels: Sequence[str] = (),
        type: TaskType = TaskType.IMPL,
        allow_contract_change: bool = False,
    ) -> int:
        meta = TaskMetadata(
            id=task_id,
            spec="specs/m1/x.md",
            covers=("AC-1",),
            depends_on=tuple(deps),
            zones=tuple(zones),
            size=size,
            tier=tier,
            type=type,
            done_when=("tests pass",),
            allow_contract_change=allow_contract_change,
        )
        if milestone is not None and not any(
            m.title == milestone for m in self.fake.list_milestones()
        ):
            self.fake.create_milestone(milestone)
        issue = self.fake.create_issue(
            f"[{task_id}] task",
            upsert_block("Description.\n", meta),
            labels=["forge:task", "status:ready", *labels],
            milestone=milestone,
        )
        return issue.number

    def remote_tip(self, issue: int) -> str | None:
        return self.git_world.remote_git.rev_parse(f"refs/heads/task/{issue}")

    def push_event(
        self,
        user: str,
        issue: int,
        trailers: dict[str, str],
        when: datetime,
        *,
        parent: str | None = None,
        message: str = "forge: event",
    ) -> str:
        """Push a commit carrying ``trailers`` on top of ``task/<issue>`` (or main)."""
        git = self.git(user)
        ref = f"refs/heads/task/{issue}"
        current = self.remote_tip(issue)
        base = parent or current or self.git_world.remote_git.rev_parse("refs/heads/main")
        assert base is not None
        git.fetch()
        sha = git.commit_tree(git.tree_of(base), [base], message, trailers, date=when)
        assert git.push(sha, ref, lease=Lease(ref, current)).ok
        return sha

    def push_work(self, user: str, issue: int, path: str, when: datetime) -> str:
        """Push a commit that changes ``path`` on ``task/<issue>``."""
        git = self.git(user)
        git.fetch()
        tip = self.remote_tip(issue)
        assert tip is not None
        git.run("checkout", "--quiet", "--detach", tip)
        (git.cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (git.cwd / path).write_text(f"{path}\n")
        git.add([path])
        stamp = f"{int(when.timestamp())} +0000"
        git.run(
            "commit",
            "--quiet",
            "-m",
            "feat: work",
            env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
        )
        sha = git.rev_parse("HEAD")
        assert sha is not None
        ref = f"refs/heads/task/{issue}"
        assert git.push(sha, ref, lease=Lease(ref, tip)).ok
        git.run("checkout", "--quiet", "main")
        return sha

    def claimed_issues(self) -> set[int]:
        """Issues whose remote branch currently ends with a claim."""
        git = self.git_world.remote_git
        result: set[int] = set()
        for ref in git.list_refs("refs/heads/task"):
            number = int(ref.rsplit("/", 1)[1])
            trailers = git.commits(f"{ref}", first_parent=True)[0].trailers
            if TRAILER_CLAIM in trailers:
                result.add(number)
        return result


def claim_of(task_id: str, who: str, agent: str = "claude") -> dict[str, str]:
    return {TRAILER_CLAIM: who, TRAILER_AGENT: agent, TRAILER_TASK: task_id}


RUN_ID = "0123456789abcdef0123456789abcdef"


def run(ctx: ClaimContext, **options: Any) -> NextOutcome:
    return claim_next(ctx, NextOptions(**options))


# --------------------------------------------------------------------------- status scenario

SPEC_M1 = """\
# Core

## Requirements
- AC-1: Pages render.
- AC-2: Pages save.
- AC-3: Pages load fast.
- AC-4: Pages can be shared.
"""
SPEC_M2 = "# Next\n\n- AC-1: Export works.\n- AC-2: Import works.\n"

PRESETS = {
    "claude-code": Preset(
        tool="claude-code",
        provider="anthropic",
        updated=dt.date(2026, 10, 1),
        supports_effort=True,
        tiers={Tier.STRONG: "opus", Tier.STANDARD: "sonnet-5", Tier.LIGHT: "haiku"},
        known_models={Tier.STRONG: ("opus",)},
    )
}


def profile_for(login: str, agent: AgentProfile = AGENT) -> Profile:
    return Profile(github=login, agents=(agent,), default_agent=agent.id)


# --------------------------------------------------------------------------- scenario plumbing


class Seeder:
    """Builds issues, main commits, claims and comments on top of a claim ``World``."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.fake: FakeGitHub = world.fake

    def as_user(self, login: str) -> FakeGitHub:
        self.fake.fake_set_user(login)
        return self.fake

    def milestone(self, title: str) -> None:
        if not any(m.title == title for m in self.fake.list_milestones()):
            self.fake.create_milestone(title)

    def task(
        self,
        task_id: str,
        zones: Sequence[str],
        *,
        deps: Sequence[str] = (),
        covers: Sequence[str] = (),
        spec: str = "specs/m1/core.md",
        size: Size = Size.S,
        milestone: str | None = None,
        labels: Sequence[str] = (),
        closed: str | None = None,
    ) -> int:
        """Create a task issue (``closed``: ``completed`` or ``not_planned``)."""
        meta = TaskMetadata(
            id=task_id,
            spec=spec,
            covers=tuple(covers),
            depends_on=tuple(deps),
            zones=tuple(zones),
            size=size,
            tier=Tier.STANDARD,
            type=TaskType.IMPL,
            done_when=("tests pass",),
        )
        title = milestone if milestone is not None else task_id.partition("-")[0]
        self.milestone(title)
        issue = self.as_user("lead").create_issue(
            f"[{task_id}] task",
            upsert_block("Description.\n", meta),
            labels=["forge:task", "status:ready", *labels],
            milestone=title,
        )
        if closed is not None:
            self.fake.update_issue(issue.number, state="closed", state_reason=closed)
        return issue.number

    def main_commit(self, path: str, content: str, when: datetime, message: str) -> str:
        """Commit ``path`` on ``main`` as ``lead`` with a fixed date and push it."""
        git = self.world.git("lead")
        git.fetch()
        git.run("checkout", "--quiet", "-B", "main", "origin/main")
        (git.cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (git.cwd / path).write_text(content)
        git.add([path])
        stamp = f"{int(when.timestamp())} +0000"
        git.run(
            "commit",
            "--quiet",
            "-m",
            message,
            env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
        )
        result = git.push("refs/heads/main", "refs/heads/main")
        assert result.ok, result.stderr
        sha = git.rev_parse("HEAD")
        assert sha is not None
        return sha

    def claim(self, user: str, issue: int, task_id: str, when: datetime) -> str:
        return self.world.push_event(user, issue, claim_of(task_id, user), when)

    def lease(self, user: str, issue: int, task_id: str, claim: str, heartbeat: datetime) -> None:
        data = LeaseData(task_id, user, "claude", claim, heartbeat, heartbeat)
        self.as_user(user).create_comment(issue, render_lease(data, branch=f"task/{issue}"))

    def checkout_task(self, user: str, issue: int) -> Git:
        """Check out ``task/<issue>`` locally in ``user``'s clone."""
        git = self.world.git(user)
        git.fetch()
        git.run("checkout", "--quiet", "-B", f"task/{issue}", f"origin/task/{issue}")
        return git

    def local_commit(self, git: Git, path: str, when: datetime) -> None:
        (git.cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (git.cwd / path).write_text("local\n")
        git.add([path])
        stamp = f"{int(when.timestamp())} +0000"
        git.run(
            "commit",
            "--quiet",
            "-m",
            "wip: local",
            env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
        )

    def ctx(
        self,
        user: str = "alice",
        *,
        profile: Profile | None = None,
        agent: AgentProfile | None = AGENT,
        zones: ZonesConfig | None = ZONES,
        config: ForgeConfig | None = None,
        cli_version: str = SCENARIO_CLI_VERSION,
        cwd: Path | None = None,
        now: datetime = T0,
        github: Any = None,
    ) -> StatusContext:
        self.fake.fake_set_user(user)
        git = self.world.git(user)
        return StatusContext(
            config=config or make_config(),
            git=git,
            github=github or self.fake,
            zones=zones,
            presets=PRESETS,
            profile=profile if profile is not None or agent is None else profile_for(user),
            agent=agent,
            clock=Clock(now),
            cwd=cwd,
            cli_version=cli_version,
        )


class Ticker:
    """GitHub's clock: starts 7 hours before T0 and advances one second per call."""

    def __init__(self) -> None:
        self.now = T0 - timedelta(hours=7)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def seed_full(s: Seeder) -> dict[str, int]:
    """The ADR 0010 scenario; returns issue numbers by name."""
    s.main_commit("specs/m1/core.md", SPEC_M1, T0 - timedelta(days=10), "docs: m1 spec")
    s.main_commit("specs/m2/next.md", SPEC_M2, T0 - timedelta(days=10), "docs: m2 spec")
    n: dict[str, int] = {}
    n["done"] = s.task("M1-T01", ["docs"], covers=["AC-1"], closed="completed")
    n["dropped"] = s.task("M1-T02", ["docs"], covers=["AC-9"], closed="not_planned")
    n["mine"] = s.task("M1-T03", ["api"], deps=["M1-T01"], covers=["AC-2"])
    n["stale"] = s.task("M1-T04", ["db"], covers=["AC-3"])
    n["blocked"] = s.task("M1-T05", ["web"], deps=["M1-T04", "M2-T01"], covers=["AC-4"])
    n["review"] = s.task("M1-T06", ["web"], covers=["AC-3"])
    n["handoff"] = s.task("M1-T07", ["docs"], covers=["AC-2"])
    n["conflict"] = s.task("M1-T08", ["db"], labels=[BLOCKED_CONFLICT])
    s.fake.create_label("status:wip", "cccccc")
    n["ready1"] = s.task("M1-T09", ["db"], labels=["status:wip"])
    n["ready2"] = s.task(
        "M1-T10", ["docs"], size=Size.M, spec="specs/m1/missing.md", covers=["AC-5"]
    )
    n["ready3"] = s.task("M1-T11", ["api"])
    n["m2a"] = s.task("M2-T01", ["db"], spec="specs/m2/next.md", covers=["AC-1"])
    n["m2b"] = s.task("M2-T02", ["docs"], deps=["M1-T01"], spec="specs/m2/next.md")
    n["proposal"] = s.task("M1-T12", ["web"], labels=["status:proposed"])
    n["nometa"] = (
        s.as_user("lead")
        .create_issue("Loose idea", "Just text, no block.", labels=["forge:task"], milestone="M1")
        .number
    )

    # my current task: claimed 5 h ago, heartbeat 30 min ago, one unpushed local commit
    mine = s.claim("alice", n["mine"], "M1-T03", T0 - timedelta(hours=5))
    s.lease("alice", n["mine"], "M1-T03", mine, T0 - timedelta(minutes=30))
    # bob's claim went stale (30 h without activity, lease_hours = 24)
    s.claim("bob", n["stale"], "M1-T04", T0 - timedelta(hours=30))
    # my other task: claimed, worked on, pull request open, CI failing
    s.claim("alice", n["review"], "M1-T06", T0 - timedelta(hours=20))
    tip = s.world.push_work("alice", n["review"], "src/web/app.js", T0 - timedelta(hours=19))
    pr = s.as_user("alice").create_pull(
        "M1-T06", f"Closes #{n['review']}", f"task/{n['review']}", "main"
    )
    s.fake.fake_set_head_sha(pr.number, tip)
    s.fake.fake_set_ci(tip, CiStatus("failure", 3, ("lint", "test"), ()))
    n["pr"] = pr.number
    # carol handed her task off, with a note
    s.claim("carol", n["handoff"], "M1-T07", T0 - timedelta(hours=10))
    s.world.push_event(
        "carol",
        n["handoff"],
        {TRAILER_HANDOFF: "carol", TRAILER_TASK: "M1-T07"},
        T0 - timedelta(hours=8),
    )
    s.as_user("carol").create_comment(
        n["handoff"], f"{HANDOFF_MARKER}\n## Done\nThe outline.\n## Next\nThe index page."
    )
    # upstream work after my claim: two commits in my zone, one outside it
    s.main_commit("src/api/routes.py", "routes\n", T0 - timedelta(hours=4), "feat: api routes")
    s.main_commit("src/web/app.css", "css\n", T0 - timedelta(hours=3), "style: web")
    s.main_commit("src/api/v2/users.py", "users\n", T0 - timedelta(hours=2), "feat: v2 users")

    alice = s.checkout_task("alice", n["mine"])
    s.local_commit(alice, "src/api/wip.py", T0 - timedelta(hours=1))
    return n


class NoChecksGitHub(FakeGitHub):
    """A GitHub whose token cannot read check runs."""

    def commit_ci(self, sha: str) -> CiStatus:
        raise GitHubError("HTTP 403: Resource not accessible by personal access token", status=403)
