"""Tests for forge.core.claim: selection, CAS claims, post-claim verification, release.

Git runs against real temporary repositories (a bare remote plus one clone per simulated
user, ``git_world``) and GitHub is a shared ``FakeGitHub`` seen through one proxy per user.
Races are made deterministic with hooks on the clones' ``Git`` objects (pre-push, post-push,
post-fetch) and an injected ``sleep``; nothing sleeps for real.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from forge.core.claim import (
    Candidate,
    ClaimContext,
    NextOptions,
    NextOutcome,
    Reason,
    TaskView,
    claim_next,
    order_key,
    reclaim_task,
    release_claim,
    resolve_task_ref,
    select_candidates,
    zones_overlap,
)
from forge.core.config import ForgeConfig, parse_config
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.git import Git, Lease, PushResult
from forge.core.github import GitHubError, Issue
from forge.core.github_fake import FakeGitHub
from forge.core.lease import LEASE_MARKER, find_claim, find_lease, format_time, heartbeat
from forge.core.profile import AgentProfile
from forge.core.refresh import TaskRecord, ensure_labels, gather_task_branches
from forge.core.tasks import (
    BLOCKED_CONFLICT,
    TRAILER_AGENT,
    TRAILER_CLAIM,
    TRAILER_HANDOFF,
    TRAILER_TASK,
    Size,
    Status,
    TaskMetadata,
    TaskType,
    upsert_block,
)
from forge.core.tiers import Tier
from forge.core.validate import Fields
from forge.core.zones import Zone, ZonesConfig

if TYPE_CHECKING:
    from conftest import GitWorld

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
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
                return attr(*args, **kwargs)

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
    ) -> ClaimContext:
        return ClaimContext(
            config=config or make_config(),
            git=self.git(user),
            github=UserGitHub(self.fake, user, self.lock),  # type: ignore[arg-type]
            zones=ZONES,
            agent=agent,
            profile_login=profile_login or user,
            clock=clock or Clock(),
            sleep=sleep or Sleeper(),
            max_polls=max_polls,
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
    ) -> int:
        meta = TaskMetadata(
            id=task_id,
            spec="specs/m1/x.md",
            covers=("AC-1",),
            depends_on=tuple(deps),
            zones=tuple(zones),
            size=size,
            tier=tier,
            type=TaskType.IMPL,
            done_when=("tests pass",),
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


@pytest.fixture
def world(git_world: GitWorld) -> World:
    return World(git_world)


def claim_of(task_id: str, who: str, agent: str = "claude") -> dict[str, str]:
    return {TRAILER_CLAIM: who, TRAILER_AGENT: agent, TRAILER_TASK: task_id}


def run(ctx: ClaimContext, **options: Any) -> NextOutcome:
    return claim_next(ctx, NextOptions(**options))


# --------------------------------------------------------------------------- pure selection


def _record(
    number: int,
    task_id: str,
    zones: Sequence[str] = ("api",),
    *,
    size: Size = Size.S,
    tier: Tier = Tier.STANDARD,
    labels: Sequence[str] = (),
    milestone: str | None = None,
    state: str = "open",
) -> TaskRecord:
    meta = TaskMetadata(
        id=task_id,
        spec="s.md",
        covers=(),
        depends_on=(),
        zones=tuple(zones),
        size=size,
        tier=tier,
        type=TaskType.IMPL,
        done_when=("x",),
    )
    issue = Issue(
        number=number,
        title=task_id,
        body="",
        state="open" if state == "open" else "closed",
        state_reason=None,
        labels=tuple(labels),
        assignees=(),
        milestone=milestone,
        author="lead",
        url="",
        created_at=T0,
        updated_at=T0,
        closed_at=None,
    )
    return TaskRecord(issue, meta)


def view(
    number: int,
    task_id: str,
    zones: Sequence[str] = ("api",),
    *,
    status: Status = Status.READY,
    wave: int = 0,
    open_pr: bool = False,
    active: bool = True,
    **kwargs: Any,
) -> TaskView:
    return TaskView(
        record=_record(number, task_id, zones, **kwargs),
        status=status,
        branch=None,
        has_open_pr=open_pr,
        milestone_active=active,
        wave=wave,
    )


def select(views: Sequence[TaskView], **kwargs: Any) -> tuple[list[int], dict[int, Reason]]:
    sel = select_candidates(views, zones=ZONES, agent=AGENT, login="alice", **kwargs)
    return [c.view.issue for c in sel.candidates], {e.issue: e.reason for e in sel.exclusions}


def test_order_handoff_milestone_wave_size_issue() -> None:
    views = [
        view(1, "M10-T01", ["docs"]),
        view(2, "M2-T01", ["db"], wave=1),
        view(3, "M2-T02", ["web"], size=Size.M),
        view(4, "M2-T03", ["api"]),
        view(5, "M3-T01", ["api-v2"], status=Status.HANDOFF),
        view(6, "M2-T04", ["docs"]),
    ]
    # api-v2 and api globs intersect, and docs is shared: give conflicting views distinct
    # zones by dropping the holders (none hold a claim), so only ordering matters here.
    order, excluded = select(views)
    assert excluded == {}
    assert order == [5, 4, 6, 3, 2, 1]
    assert sorted(order, key=lambda n: order_key(views[n - 1])) == order


def test_exclusion_reasons() -> None:
    views = [
        view(1, "M1-T01", ["api"], status=Status.PROPOSED),
        view(2, "M1-T02", ["web"], status=Status.BLOCKED),
        view(3, "M1-T03", ["db"], labels=[BLOCKED_CONFLICT]),
        view(4, "M1-T04", ["docs"], active=False, milestone="M1"),
        view(5, "M1-T05", ["nope"]),
        view(6, "M1-T06", ["api-v2"], status=Status.IN_REVIEW, open_pr=True),
        view(7, "M1-T07", ["api"]),  # overlaps the open PR of #6 (glob intersection)
        view(8, "M1-T08", ["web"], tier=Tier.STRONG),
    ]
    order, excluded = select(views)
    assert order == [8]
    assert excluded == {
        1: Reason.PROPOSED,
        2: Reason.BLOCKED,
        3: Reason.CONFLICT,
        4: Reason.INACTIVE_MILESTONE,
        5: Reason.UNKNOWN_ZONE,
        6: Reason.IN_REVIEW,
        7: Reason.ZONE_OVERLAP,
    }


def test_tier_filter_and_any_tier() -> None:
    views = [view(1, "M1-T01", ["api"], tier=Tier.STANDARD), view(2, "M1-T02", ["web"])]
    sel = select_candidates(views, zones=ZONES, agent=LIGHT_AGENT, login="alice")
    assert sel.candidates == ()
    assert {e.reason for e in sel.exclusions} == {Reason.TIER}
    assert "--any-tier" in sel.exclusions[0].detail
    sel = select_candidates(views, zones=ZONES, agent=LIGHT_AGENT, login="alice", any_tier=True)
    assert [c.view.issue for c in sel.candidates] == [1, 2]
    light = [view(3, "M1-T03", ["db"], tier=Tier.LIGHT)]
    assert select_candidates(light, zones=ZONES, agent=LIGHT_AGENT, login="x").candidates


def test_zones_overlap() -> None:
    assert zones_overlap(ZONES, ["api"], ["api"]) == "shared zone api"
    assert zones_overlap(ZONES, ["ghost"], ["ghost"]) == "shared zone ghost"
    assert zones_overlap(ZONES, ["api"], ["api-v2"]) is not None
    assert zones_overlap(ZONES, ["api"], ["web"]) is None
    assert zones_overlap(ZONES, ["ghost"], ["web"]) is None


def test_resolve_task_ref() -> None:
    records = [_record(4, "M1-T07"), _record(9, "M1-T07"), _record(5, "M1-T01", state="closed")]
    assert resolve_task_ref("M1-T07", records) == 4
    assert resolve_task_ref("#9", records) == 9
    assert resolve_task_ref(" 9 ", records) == 9
    assert resolve_task_ref("5", records) is None  # closed
    assert resolve_task_ref("M1-T99", records) is None
    with pytest.raises(ForgeError, match="invalid task reference"):
        resolve_task_ref("task-7", records)


def test_candidate_data() -> None:
    data = Candidate(view(3, "M1-T03", ["db"], wave=2)).to_data()
    assert data == {
        "issue": 3,
        "id": "M1-T03",
        "status": "ready",
        "milestone": "M1",
        "wave": 2,
        "size": "S",
        "tier": "standard",
        "zones": ["db"],
        "resume": False,
        "reclaim": False,
    }


# --------------------------------------------------------------------------- basic claim


def test_claim_new_task(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    sleeper = Sleeper()
    ctx = world.ctx("alice", sleep=sleeper)
    outcome = run(ctx)

    assert outcome.code == ExitCode.OK
    claimed = outcome.claimed
    assert claimed is not None
    assert (claimed.issue, claimed.task_id, claimed.branch) == (n, "M1-T01", f"task/{n}")
    assert not (claimed.resumed or claimed.reclaimed or claimed.own)
    main = world.git_world.initial_sha
    assert claimed.base == main
    assert claimed.claimed_at == T0
    assert sleeper.calls == []

    remote = world.git_world.remote_git
    tip = world.remote_tip(n)
    assert tip == claimed.claim
    commit = remote.commits(tip)[0]
    assert commit.subject == "forge: claim M1-T01"
    assert commit.trailers == claim_of("M1-T01", "alice")
    assert commit.date == T0
    assert remote.run("rev-parse", f"{tip}^").stdout.strip() == main
    assert remote.tree_of(tip) == remote.tree_of(main)
    assert (tip, f"refs/heads/task/{n}", True) in ctx.git.pushes  # type: ignore[attr-defined]

    issue = world.fake.get_issue(n)
    assert issue.assignees == ("alice",)
    assert "status:in-progress" in issue.labels and "status:ready" not in issue.labels
    found = find_lease(world.fake.list_comments(n))
    assert found is not None and found.data is not None
    assert found.data.claim == tip and found.data.owner == "alice"
    assert found.data.agent == "claude" and found.data.heartbeat_at == T0
    assert outcome.to_data()["claimed"]["claim"] == tip


def test_nothing_claimable(world: World) -> None:
    world.task("M1-T01", ["api"], deps=["M1-T09"])
    outcome = run(world.ctx("alice"))
    assert outcome.code == ExitCode.NOTHING_AVAILABLE
    assert outcome.reason == "nothing claimable"
    assert outcome.to_data()["excluded"] == {"blocked": 1}
    assert any("unknown dependency M1-T09" in w for w in outcome.warnings)


def test_dry_run_pushes_nothing(world: World) -> None:
    a = world.task("M1-T01", ["api"], size=Size.M)
    b = world.task("M1-T02", ["web"])
    ctx = world.ctx("alice")
    outcome = run(ctx, dry_run=True)
    assert outcome.code == ExitCode.OK and outcome.dry_run
    assert [c.view.issue for c in outcome.candidates] == [b, a]
    assert ctx.git.pushes == []  # type: ignore[attr-defined]
    assert world.remote_tip(a) is None and world.remote_tip(b) is None
    assert world.fake.get_issue(a).assignees == ()


def test_closed_dependency_unblocks_and_wave_orders(world: World) -> None:
    dep = world.task("M1-T01", ["db"])
    world.fake.update_issue(dep, state="closed")
    late = world.task("M1-T02", ["api"], deps=["M1-T01"])  # wave 1
    early = world.task("M1-T03", ["web"], size=Size.M)  # wave 0
    outcome = run(world.ctx("alice"), dry_run=True)
    assert [c.view.issue for c in outcome.candidates] == [early, late]


def test_inactive_milestone_and_cycle_warning(world: World) -> None:
    closed = world.task("M1-T01", ["api"], milestone="M1")
    world.fake.close_milestone(1)
    a = world.task("M2-T01", ["web"], deps=["M2-T02"], milestone="M2")
    world.task("M2-T02", ["db"], deps=["M2-T01"], milestone="M2")
    outcome = run(world.ctx("alice"), dry_run=True)
    reasons = {e.issue: e.reason for e in outcome.exclusions}
    assert reasons[closed] is Reason.INACTIVE_MILESTONE
    assert reasons[a] is Reason.BLOCKED
    assert any("wave 0" in w for w in outcome.warnings)


# --------------------------------------------------------------------------- preconditions


def test_version_mismatch_is_a_precondition(world: World) -> None:
    world.task("M1-T01", ["api"])
    ctx = world.ctx("alice", config=make_config(version=">=9"))
    with pytest.raises(PreconditionError, match="forge_version"):
        run(ctx)
    assert world.claimed_issues() == set()


def test_login_mismatch_is_a_precondition(world: World) -> None:
    world.task("M1-T01", ["api"])
    with pytest.raises(PreconditionError) as excinfo:
        run(world.ctx("alice", profile_login="vincenzo"))
    assert "'alice'" in excinfo.value.message and "'vincenzo'" in excinfo.value.message
    # logins compare case-insensitively, as on GitHub
    assert run(world.ctx("alice", profile_login="Alice")).code == ExitCode.OK


def test_shallow_clone_is_a_precondition(world: World) -> None:
    world.task("M1-T01", ["api"])
    seed = Git(world.git_world.base / "seed")
    world.git_world.commit_file(seed, "b.txt", "b\n", "feat: b")
    world.git_world.push(seed, "main")
    path = world.git_world.base / "shallow"
    Git(world.git_world.base).run(
        "clone", "--quiet", "--depth", "1", world.git_world.remote.as_uri(), str(path)
    )
    ctx = world.ctx("alice")
    ctx.git = Git(path)
    with pytest.raises(PreconditionError, match="shallow"):
        run(ctx)


# --------------------------------------------------------------------------- (a) same-task race


def test_same_task_race_loser_takes_next_candidate(world: World) -> None:
    t1 = world.task("M1-T01", ["api"])
    t2 = world.task("M1-T02", ["web"])
    alice, bob = world.ctx("alice"), world.ctx("bob")
    results: dict[str, NextOutcome] = {}

    def alice_claims_first(src: str, dst: str) -> None:
        bob.git.before_push = None  # type: ignore[attr-defined]
        results["alice"] = run(alice)

    bob.git.before_push = alice_claims_first  # type: ignore[attr-defined]
    results["bob"] = run(bob)

    assert results["alice"].claimed is not None and results["alice"].claimed.issue == t1
    bob_out = results["bob"]
    assert bob_out.code == ExitCode.OK
    assert bob_out.claimed is not None and bob_out.claimed.issue == t2
    assert bob_out.attempts == 1
    assert {e.issue: e.reason for e in bob_out.exclusions}[t1] is Reason.LOST_RACE
    assert any("lost the race" in w for w in bob_out.warnings)
    assert world.fake.get_issue(t1).assignees == ("alice",)
    assert world.fake.get_issue(t2).assignees == ("bob",)
    assert world.claimed_issues() == {t1, t2}


def test_same_task_race_threads(world: World) -> None:
    t1 = world.task("M1-T01", ["api"])
    t2 = world.task("M1-T02", ["web"])
    users = ("alice", "bob")
    ctxs = {u: world.ctx(u) for u in users}
    barrier = threading.Barrier(len(users))
    results: dict[str, NextOutcome] = {}
    errors: list[BaseException] = []

    def once_barrier(git: HookedGit) -> Callable[[str, str], None]:
        def hook(src: str, dst: str) -> None:
            git.before_push = None
            barrier.wait(WAIT)

        return hook

    def attempt(user: str) -> None:
        try:
            results[user] = run(ctxs[user])
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    for u in users:
        git = ctxs[u].git
        assert isinstance(git, HookedGit)
        git.before_push = once_barrier(git)
    threads = [threading.Thread(target=attempt, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT * 2)
    assert not errors, errors
    claimed = sorted(r.claimed.issue for r in results.values() if r.claimed is not None)
    assert claimed == [t1, t2]
    loser = next(r for r in results.values() if r.claimed is not None and r.claimed.issue == t2)
    assert loser.attempts == 1
    winner_login = next(u for u, r in results.items() if r.claimed and r.claimed.issue == t1)
    assert world.fake.get_issue(t1).assignees == (winner_login,)


# --------------------------------------------------------------------------- (b) zone overlap


class Peer:
    """Synchronisation for two users racing on overlapping tasks (threaded scenario)."""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.pushed = threading.Event()
        self.s2 = threading.Event()
        self.done = threading.Event()


def _wait(event: threading.Event, what: str) -> None:
    if not event.wait(WAIT):
        raise AssertionError(f"timed out waiting for {what}")


def _wire(git: HookedGit, me: Peer, other: Peer) -> None:
    state = {"armed": False}

    def before_push(src: str, dst: str) -> None:
        if src == "":  # a yield: the other side must have listed claims (S2) first
            _wait(other.s2, "peer S2")
        elif not me.ready.is_set():  # first claim push: both selected before either pushes
            me.ready.set()
            _wait(other.ready, "peer selection")

    def after_push(src: str, dst: str, result: PushResult) -> None:
        if src and not me.pushed.is_set():
            me.pushed.set()
            _wait(other.pushed, "peer push")  # both pushed before either verifies
            state["armed"] = True

    def after_fetch() -> None:
        if state["armed"]:
            state["armed"] = False
            me.s2.set()

    git.before_push = before_push
    git.after_push = after_push
    git.after_fetch = after_fetch


@pytest.mark.parametrize("alice_first_in_order", [True, False])
def test_overlap_both_see_each_other(world: World, alice_first_in_order: bool) -> None:
    """Both push, then both verify: the later one in order yields, the earlier one waits."""
    t1 = world.task("M1-T01", ["api"])
    t2 = world.task("M1-T02", ["api-v2"])  # globs intersect with api
    # Same clock: the claims tie on date, so the lower issue number comes first.
    alice_task, bob_task = (t1, t2) if alice_first_in_order else (t2, t1)
    peers = {"alice": Peer(), "bob": Peer()}
    sleepers = {
        "alice": Sleeper(lambda: _wait(peers["bob"].done, "bob done")),
        "bob": Sleeper(lambda: _wait(peers["alice"].done, "alice done")),
    }
    ctxs = {u: world.ctx(u, sleep=sleepers[u]) for u in peers}
    _wire(world.git("alice"), peers["alice"], peers["bob"])
    _wire(world.git("bob"), peers["bob"], peers["alice"])
    targets = {"alice": alice_task, "bob": bob_task}
    results: dict[str, NextOutcome] = {}
    errors: list[BaseException] = []

    def go(user: str) -> None:
        try:
            results[user] = run(ctxs[user], task=str(targets[user]))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            peers[user].done.set()

    threads = [threading.Thread(target=go, args=(u,)) for u in peers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT * 2)
    assert not errors, errors

    keeper = "alice" if alice_first_in_order else "bob"
    yielder = "bob" if alice_first_in_order else "alice"
    kept = results[keeper]
    assert kept.code == ExitCode.OK and kept.claimed is not None
    assert kept.claimed.issue == t1
    assert len(sleepers[keeper].calls) == 1  # waited once, then saw the other go away
    lost = results[yielder]
    assert lost.code == ExitCode.NOTHING_AVAILABLE
    assert lost.claimed is None and lost.attempts == 1
    assert "yielded" in (lost.reason or "")
    assert sleepers[yielder].calls == []  # yielded at once
    assert world.remote_tip(t2) is None
    assert world.claimed_issues() == {t1}
    assert world.fake.get_issue(t2).assignees == ()
    assert find_lease(world.fake.list_comments(t2)) is None  # nothing written before verify


def test_overlap_earlier_pusher_kept_later_pusher_first_in_order_waits_then_yields(
    world: World,
) -> None:
    """A verified before B pushed; B comes first in order: B waits for A, A stays, B yields."""
    t1 = world.task("M1-T01", ["api"])  # bob's pick: first in order (lower issue)
    t2 = world.task("M1-T02", ["api"])  # alice targets it
    t3 = world.task("M1-T03", ["web"], size=Size.M)  # bob's fallback
    alice = world.ctx("alice")
    sleeper = Sleeper()
    bob = world.ctx("bob", sleep=sleeper)
    results: dict[str, NextOutcome] = {}

    def alice_runs_completely(src: str, dst: str) -> None:
        world.git("bob").before_push = None
        results["alice"] = run(alice, task="M1-T02")

    world.git("bob").before_push = alice_runs_completely
    results["bob"] = run(bob)

    assert results["alice"].claimed is not None and results["alice"].claimed.issue == t2
    assert sleeper.calls == [2.0] * 5  # polled max_polls times, A never went away
    bob_out = results["bob"]
    assert bob_out.claimed is not None and bob_out.claimed.issue == t3  # moved on
    assert {e.issue: e.reason for e in bob_out.exclusions}[t1] is Reason.YIELDED
    assert any(f"yielded to #{t2}" in w for w in bob_out.warnings)
    assert world.remote_tip(t1) is None
    assert world.claimed_issues() == {t2, t3}


@pytest.mark.parametrize("resume", [False, True])
def test_overlap_earlier_pusher_first_in_order_later_yields_at_once(
    world: World, resume: bool
) -> None:
    """A verified before B pushed and comes first in order: B yields without waiting.

    With ``resume`` B's task is a handoff, so yielding pushes the previous tip back.
    """
    t1 = world.task("M1-T01", ["api"], size=Size.M)  # alice targets it
    t2 = world.task("M1-T02", ["api"])  # bob's pick (size S, or a handoff)
    t3 = world.task("M1-T03", ["web"], size=Size.M, milestone=None)  # bob's fallback
    handoff_tip = None
    if resume:
        world.push_event("carol", t2, claim_of("M1-T02", "carol"), T0 - timedelta(hours=2))
        handoff_tip = world.push_event(
            "carol", t2, {TRAILER_HANDOFF: "carol", TRAILER_TASK: "M1-T02"}, T0 - timedelta(hours=1)
        )
    alice = world.ctx("alice")
    sleeper = Sleeper()
    bob = world.ctx("bob", sleep=sleeper)
    results: dict[str, NextOutcome] = {}

    def alice_runs_completely(src: str, dst: str) -> None:
        world.git("bob").before_push = None
        results["alice"] = run(alice, task="M1-T01")

    world.git("bob").before_push = alice_runs_completely
    results["bob"] = run(bob)

    assert results["alice"].claimed is not None and results["alice"].claimed.issue == t1
    assert sleeper.calls == []
    bob_out = results["bob"]
    assert bob_out.claimed is not None and bob_out.claimed.issue == t3
    assert {e.issue: e.reason for e in bob_out.exclusions}[t2] is Reason.YIELDED
    assert world.remote_tip(t2) == handoff_tip  # deleted (None) or restored
    assert world.claimed_issues() == {t1, t3}


def test_rolled_back_claim_never_reached_github(world: World) -> None:
    t1 = world.task("M1-T01", ["api"])
    t2 = world.task("M1-T02", ["api"])
    alice = world.ctx("alice")
    bob = world.ctx("bob", max_polls=1)

    def alice_first(src: str, dst: str) -> None:
        world.git("bob").before_push = None
        run(alice, task="M1-T01")

    world.git("bob").before_push = alice_first
    out = run(bob, task="M1-T02")
    assert out.code == ExitCode.NOTHING_AVAILABLE
    assert "yielded" in (out.reason or "")
    issue = world.fake.get_issue(t2)
    assert issue.assignees == () and "status:in-progress" not in issue.labels
    assert world.fake.list_comments(t2) == []
    assert world.claimed_issues() == {t1}


# --------------------------------------------------------------------------- holders


def test_open_pr_holds_zones_even_when_stale(world: World) -> None:
    held = world.task("M1-T01", ["api"])
    world.push_event("carol", held, claim_of("M1-T01", "carol"), T0 - timedelta(days=5))
    world.fake.create_pull("M1-T01", f"Closes #{held}", f"task/{held}", "main")
    blocked = world.task("M1-T02", ["api-v2"])
    free = world.task("M1-T03", ["web"], size=Size.M)
    outcome = run(world.ctx("alice"), reclaim=True)
    reasons = {e.issue: e.reason for e in outcome.exclusions}
    assert reasons == {held: Reason.IN_REVIEW, blocked: Reason.ZONE_OVERLAP}
    assert outcome.claimed is not None and outcome.claimed.issue == free


def test_live_claim_holds_zones_stale_does_not(world: World) -> None:
    live = world.task("M1-T01", ["api"])
    world.push_event("carol", live, claim_of("M1-T01", "carol"), T0 - timedelta(hours=1))
    stale = world.task("M1-T02", ["web"])
    world.push_event("dave", stale, claim_of("M1-T02", "dave"), T0 - timedelta(hours=30))
    over_live = world.task("M1-T03", ["api"])
    over_stale = world.task("M1-T04", ["web"])
    outcome = run(world.ctx("alice"), dry_run=True)
    reasons = {e.issue: e.reason for e in outcome.exclusions}
    assert reasons == {
        live: Reason.CLAIMED,
        stale: Reason.STALE_CLAIM,
        over_live: Reason.ZONE_OVERLAP,
    }
    assert [c.view.issue for c in outcome.candidates] == [over_stale]


def test_closed_issue_with_leftover_claim_holds_nothing(world: World) -> None:
    done = world.task("M1-T01", ["api"])
    world.push_event("carol", done, claim_of("M1-T01", "carol"), T0)
    world.fake.update_issue(done, state="closed")
    t2 = world.task("M1-T02", ["api"])
    outcome = run(world.ctx("alice"))
    assert outcome.claimed is not None and outcome.claimed.issue == t2


# --------------------------------------------------------------------------- (c) stale / reclaim


def test_fresh_claim_cannot_be_reclaimed(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    run(world.ctx("carol", clock=Clock(T0)))
    tip = world.remote_tip(n)
    out = reclaim_task(world.ctx("alice", clock=Clock(T0 + timedelta(hours=23))), str(n))
    assert out.code == ExitCode.NOTHING_AVAILABLE
    assert out.reason is not None
    assert "claimed by carol" in out.reason
    assert format_time(T0 + timedelta(hours=24)) in out.reason
    assert world.remote_tip(n) == tip


def test_stale_claim_is_reclaimed(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    carol = run(world.ctx("carol", clock=Clock(T0)))
    assert carol.claimed is not None
    old_tip = world.push_work("carol", n, "src/api/x.py", T0 + timedelta(hours=1))
    later = Clock(T0 + timedelta(hours=26))

    # without --reclaim the stale claim is reported, not taken
    plain = run(world.ctx("alice", clock=later))
    assert plain.code == ExitCode.NOTHING_AVAILABLE
    assert {e.issue: e.reason for e in plain.exclusions} == {n: Reason.STALE_CLAIM}

    out = reclaim_task(world.ctx("alice", clock=later), f"#{n}")
    assert out.code == ExitCode.OK
    claimed = out.claimed
    assert claimed is not None
    assert claimed.reclaimed and claimed.resumed and not claimed.own
    assert claimed.previous_claimant == "carol"
    assert claimed.base == world.git_world.initial_sha
    remote = world.git_world.remote_git
    assert remote.run("rev-parse", f"{claimed.claim}^").stdout.strip() == old_tip
    issue = world.fake.get_issue(n)
    assert issue.assignees == ("alice",)
    bodies = [c.body for c in world.fake.list_comments(n)]
    reclaim = [b for b in bodies if "forge:reclaim" in b]
    assert len(reclaim) == 1
    assert "@carol" in reclaim[0]
    assert format_time(T0 + timedelta(hours=25)) in reclaim[0]
    found = find_lease(world.fake.list_comments(n))
    assert found is not None and found.data is not None
    assert found.data.owner == "alice" and found.data.claim == claimed.claim
    assert sum(1 for b in bodies if b.startswith(LEASE_MARKER)) == 1  # edited in place


def test_heartbeat_extends_activity_only_for_its_claim(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    run(world.ctx("carol", clock=Clock(T0)))
    beat = heartbeat(world.ctx("carol", clock=Clock(T0 + timedelta(hours=20))), n)
    assert beat.stale_at == T0 + timedelta(hours=44)

    at_30h = Clock(T0 + timedelta(hours=30))
    refused = reclaim_task(world.ctx("alice", clock=at_30h), str(n))
    assert refused.code == ExitCode.NOTHING_AVAILABLE
    assert format_time(T0 + timedelta(hours=44)) in (refused.reason or "")

    # carol's claim is replaced by a new claim (from an older parent clock): the old
    # heartbeat (claim sha of the first claim) must not extend the new one.
    tip = world.remote_tip(n)
    assert tip is not None
    world.push_event("dave", n, claim_of("M1-T01", "dave"), T0 + timedelta(hours=1))
    taken = reclaim_task(world.ctx("alice", clock=at_30h), str(n))
    assert taken.code == ExitCode.OK
    assert taken.claimed is not None and taken.claimed.previous_claimant == "dave"


# --------------------------------------------------------------------------- (d) handoff


def test_handoff_is_preferred_and_resumed(world: World) -> None:
    world.task("M1-T01", ["api"])
    later = world.task("M9-T01", ["web"], size=Size.M)
    world.push_event("carol", later, claim_of("M9-T01", "carol"), T0 - timedelta(hours=3))
    tip = world.push_event(
        "carol", later, {TRAILER_HANDOFF: "carol", TRAILER_TASK: "M9-T01"}, T0 - timedelta(hours=1)
    )
    out = run(world.ctx("alice"))
    assert out.claimed is not None and out.claimed.issue == later
    assert out.claimed.resumed and not out.claimed.reclaimed
    remote = world.git_world.remote_git
    assert remote.run("rev-parse", f"{out.claimed.claim}^").stdout.strip() == tip
    assert out.claimed.base == world.git_world.initial_sha


def test_released_branch_is_claimable_again(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    first = run(world.ctx("carol"))
    assert first.claimed is not None
    release_claim(world.ctx("carol"), n)
    out = run(world.ctx("alice"))
    assert out.claimed is not None and out.claimed.issue == n
    assert out.claimed.resumed and not out.claimed.reclaimed


# --------------------------------------------------------------------------- idempotency


def test_interrupted_claim_is_completed_without_a_second_push(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    other = world.task("M1-T02", ["web"], size=Size.M)
    ctx = world.ctx("alice")
    ctx.github.fail["add_assignees"] = 1  # type: ignore[attr-defined]
    with pytest.raises(GitHubError):
        run(ctx)
    tip = world.remote_tip(n)
    assert tip is not None
    assert find_lease(world.fake.list_comments(n)) is None

    again = run(world.ctx("alice"))
    assert again.code == ExitCode.OK
    assert again.claimed is not None
    assert again.claimed.issue == n and again.claimed.own and again.claimed.claim == tip
    assert world.remote_tip(n) == tip  # no second push
    assert world.remote_tip(other) is None
    assert world.fake.get_issue(n).assignees == ("alice",)

    # Complete now: next does not offer it again and moves on to other work.
    third = run(world.ctx("alice"))
    assert third.claimed is not None and third.claimed.issue == other
    assert f"you already hold #{n} (M1-T01)" in third.info
    # next --task on a held task re-runs the post-claim steps idempotently (exit 0)
    comments = len(world.fake.list_comments(n))
    fourth = run(world.ctx("alice"), task="M1-T01")
    assert fourth.code == ExitCode.OK and fourth.claimed is not None and fourth.claimed.own
    assert fourth.claimed.claim == tip
    assert len(world.fake.list_comments(n)) == comments


def test_interrupted_claim_of_another_agent_is_not_mine(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    world.push_event("alice", n, claim_of("M1-T01", "alice", agent="codex"), T0)
    out = run(world.ctx("alice"), task=str(n))
    assert out.code == ExitCode.NOTHING_AVAILABLE
    assert {e.issue: e.reason for e in out.exclusions} == {n: Reason.CLAIMED}


def test_interrupted_claim_is_verified_again_and_yields(world: World) -> None:
    """A claim pushed but never verified (crash) must not survive an earlier overlapping one."""
    held = world.task("M1-T01", ["api"])
    mine = world.task("M1-T02", ["api"])
    run(world.ctx("carol", clock=Clock(T0)), task="M1-T01")
    world.push_event("alice", mine, claim_of("M1-T02", "alice"), T0 + timedelta(minutes=1))
    sleeper = Sleeper()
    out = run(world.ctx("alice", clock=Clock(T0 + timedelta(minutes=2)), sleep=sleeper))
    assert out.code == ExitCode.NOTHING_AVAILABLE
    assert sleeper.calls == []
    assert world.remote_tip(mine) is None
    assert world.claimed_issues() == {held}
    assert out.attempts == 1


def test_max_attempts_exhausted(world: World) -> None:
    t1 = world.task("M1-T01", ["api"])
    world.task("M1-T02", ["web"])
    alice, bob = world.ctx("alice"), world.ctx("bob", config=make_config(max_attempts=1))

    def alice_first(src: str, dst: str) -> None:
        world.git("bob").before_push = None
        run(alice)

    world.git("bob").before_push = alice_first
    out = run(bob)
    assert out.code == ExitCode.NOTHING_AVAILABLE
    assert out.attempts == 1
    assert "max_attempts" in (out.reason or "")
    assert world.claimed_issues() == {t1}


def test_task_not_claimable_gives_reason(world: World) -> None:
    world.task("M1-T01", ["api"], deps=["M1-T05"])
    world.task("M1-T05", ["web"])
    out = run(world.ctx("alice"), task="M1-T01")
    assert out.code == ExitCode.NOTHING_AVAILABLE
    assert out.reason is not None and "M1-T01" in out.reason and "dependency" in out.reason
    missing = run(world.ctx("alice"), task="#99")
    assert missing.code == ExitCode.NOTHING_AVAILABLE
    assert "no open Forge task" in (missing.reason or "")


def test_targeted_lost_race_is_not_retried(world: World) -> None:
    t1 = world.task("M1-T01", ["api"])
    world.task("M1-T02", ["web"])
    alice, bob = world.ctx("alice"), world.ctx("bob")

    def alice_first(src: str, dst: str) -> None:
        world.git("bob").before_push = None
        run(alice, task="M1-T01")

    world.git("bob").before_push = alice_first
    out = run(bob, task="M1-T01")
    assert out.code == ExitCode.NOTHING_AVAILABLE
    assert "lost the race" in (out.reason or "")
    assert world.claimed_issues() == {t1}


# --------------------------------------------------------------------------- release


def _branch_commits(world: World, n: int) -> list[str]:
    remote = world.git_world.remote_git
    return [c.subject for c in remote.commits(f"main..refs/heads/task/{n}")]


def test_release_pushes_event_unassigns_and_relabels(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    run(world.ctx("alice"))
    ctx = world.ctx("alice", clock=Clock(T0 + timedelta(hours=2)))
    out = release_claim(ctx, n)
    assert out.status is Status.READY and not out.deleted and not out.recovered
    assert out.release_commit == world.remote_tip(n)
    assert _branch_commits(world, n) == ["forge: release M1-T01", "forge: claim M1-T01"]
    pushes = ctx.git.pushes  # type: ignore[attr-defined]
    assert pushes[-1][2] is True  # --no-verify
    issue = world.fake.get_issue(n)
    assert issue.assignees == ()
    assert "status:ready" in issue.labels and "status:in-progress" not in issue.labels
    found = find_lease(world.fake.list_comments(n))
    assert found is not None and found.data is not None
    assert found.data.released_at == T0 + timedelta(hours=2)
    assert any("forge:release" in c.body for c in world.fake.list_comments(n))
    # not mine any more: a second release is refused
    with pytest.raises(PreconditionError):
        release_claim(ctx, n)


def test_release_delete_branch(world: World) -> None:
    n = world.task("M1-T01", ["api"], deps=["M1-T02"])
    dep = world.task("M1-T02", ["web"])
    world.fake.update_issue(dep, state="closed")
    run(world.ctx("alice"), task="M1-T01")
    out = release_claim(world.ctx("alice"), n, delete_branch=True)
    assert out.deleted and out.release_commit is None
    assert world.remote_tip(n) is None
    assert out.status is Status.READY


def test_release_delete_branch_keeps_work(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    run(world.ctx("alice"))
    world.push_work("alice", n, "src/api/a.py", T0)
    out = release_claim(world.ctx("alice"), n, delete_branch=True)
    assert not out.deleted
    assert any("kept" in w for w in out.warnings)
    assert _branch_commits(world, n)[0] == "forge: release M1-T01"


def test_release_reports_unpushed_local_commits(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    out = run(world.ctx("alice"))
    assert out.claimed is not None
    git = world.git("alice")
    git.fetch()
    git.checkout(f"task/{n}", create_from=out.claimed.claim)
    world.git_world.commit_file(git, "src/api/local.py", "x\n", "feat: local")
    world.git_world.commit_file(git, "src/api/local2.py", "y\n", "feat: local 2")
    released = release_claim(world.ctx("alice"), n)
    assert released.unpushed == 2
    assert any("2 local commit(s)" in w for w in released.warnings)
    assert _branch_commits(world, n) == ["forge: release M1-T01", "forge: claim M1-T01"]


def test_release_requires_my_live_claim(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    with pytest.raises(PreconditionError, match="no live claim"):
        release_claim(world.ctx("alice"), n)
    run(world.ctx("carol"))
    with pytest.raises(PreconditionError):
        release_claim(world.ctx("alice"), n)
    stale = Clock(T0 + timedelta(hours=25))
    with pytest.raises(PreconditionError, match="stale"):
        release_claim(world.ctx("carol", clock=stale), n)


def test_interrupted_release_is_completed(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    run(world.ctx("alice"))
    ctx = world.ctx("alice")
    ctx.github.fail["create_comment"] = 1  # type: ignore[attr-defined]
    with pytest.raises(GitHubError):
        release_claim(ctx, n)
    tip = world.remote_tip(n)
    again = release_claim(world.ctx("alice"), n)
    assert again.recovered
    assert world.remote_tip(n) == tip  # no second release commit
    assert world.fake.get_issue(n).assignees == ()
    found = find_lease(world.fake.list_comments(n))
    assert found is not None and found.data is not None and found.data.released_at is not None
    assert sum("forge:release" in c.body for c in world.fake.list_comments(n)) == 1


def test_claim_info_from_gathered_branch(world: World) -> None:
    n = world.task("M1-T01", ["api"])
    out = run(world.ctx("alice"))
    assert out.claimed is not None
    work = world.push_work("alice", n, "src/api/z.py", T0 + timedelta(hours=3))
    git = world.git("bob")
    git.fetch(refspecs=["+refs/heads/task/*:refs/remotes/origin/task/*"])
    branch = gather_task_branches(git, "main").branches[n]
    info = find_claim(n, branch, "M1-T01")
    assert info is not None
    assert (info.sha, info.tip, info.index, info.owner) == (out.claimed.claim, work, 1, "alice")
    assert info.tip_date == T0 + timedelta(hours=3) and info.date == T0
    assert find_claim(n, branch, "M1-T02") is None
