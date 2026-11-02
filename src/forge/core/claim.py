"""Claiming tasks: selection, compare-and-swap claim, post-claim zone verification, release.

Implements spec §8.1, §8.2 and §8.4 as decided in ADR 0009:

- **read**: open milestones, every ``forge:task`` issue (closed ones resolve dependencies and
  waves), open pull requests, then a fetch of the default branch and every ``task/*`` branch;
- **select**: candidates are ``ready``/``handoff`` tasks (plus stale ``in-progress`` ones
  with ``reclaim``), minus tasks that are in review, carry ``blocked:conflict``, overlap the
  zones of a zone holder (a live claim or an open pull request), or need a tier the agent
  cannot serve. Order: handoff first, milestone (natural order), wave, size, issue number;
- **claim**: an empty claim commit made with ``commit-tree`` and pushed with an explicit
  lease (``None`` for a new branch, the current tip for a resume), skipping user hooks;
- **verify**: the safe post-claim zone verification of ADR 0009, then the GitHub steps.

The worktree and ``.forge/local/current.json`` are created by the caller from
:class:`Claimed`. Every operation is safe to re-run: a claim whose push landed before an
interruption is found again (it names my login and agent, and the lease comment does not
reference it yet) and completed without a second push.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from forge import __version__
from forge.core.config import ForgeConfig, check_cli_version
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.git import Git, Lease
from forge.core.github import GitHubClient
from forge.core.graph import GraphCycleError, TaskGraph, natural_key
from forge.core.lease import (
    ClaimInfo,
    LeaseData,
    check_identity,
    event_commit_index,
    find_claim,
    find_lease,
    format_time,
    is_stale,
    last_activity,
    load_task,
    read_branch,
    stale_at,
    upsert_lease,
)
from forge.core.models import can_serve
from forge.core.profile import AgentProfile
from forge.core.refresh import (
    TaskBranch,
    TaskRecord,
    check_full_history,
    ensure_labels,
    fetch_task_refs,
    gather_task_branches,
    parse_records,
)
from forge.core.tasks import (
    BLOCKED_CONFLICT,
    FORGE_TASK_LABEL,
    TASK_ID_RE,
    TRAILER_AGENT,
    TRAILER_CLAIM,
    TRAILER_RELEASE,
    TRAILER_TASK,
    BranchEvent,
    BranchState,
    LabelDiff,
    Size,
    Status,
    TaskMetadata,
    derive_status,
    desired_labels,
    has_label,
    last_event_from_trailers,
)
from forge.core.zones import ZonesConfig

_ISSUE_REF_RE = re.compile(r"#?([1-9][0-9]*)")


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- context


@dataclass
class ClaimContext:
    """Everything claim, heartbeat and release need. Satisfies ``lease.LeaseContext``.

    ``profile_login`` is the profile's ``github`` field; ``agent`` the active agent.
    ``clock`` and ``sleep`` are injected so tests are deterministic; ``poll_interval`` and
    ``max_polls`` bound the wait of the post-claim verification (2 s x 5 by default).
    """

    config: ForgeConfig
    git: Git
    github: GitHubClient
    zones: ZonesConfig
    agent: AgentProfile
    profile_login: str
    clock: Callable[[], datetime] = _utc_now
    sleep: Callable[[float], None] = time.sleep
    poll_interval: float = 2.0
    max_polls: int = 5
    remote: str = "origin"
    cli_version: str = __version__

    def now(self) -> datetime:
        """Current time from :attr:`clock`, as an aware UTC datetime."""
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("the clock must return timezone-aware datetimes")
        return value.astimezone(UTC)


def check_version(ctx: ClaimContext) -> None:
    """Raise :class:`PreconditionError` (exit 5) when the CLI does not satisfy ``forge_version``."""
    spec = ctx.config.forge_version
    try:
        ok = check_cli_version(spec, ctx.cli_version)
    except ValueError:
        ok = False
    if not ok:
        raise PreconditionError(
            f"forge {ctx.cli_version} does not satisfy forge_version {str(spec)!r} "
            "in .forge/config.yml",
            hint="install a Forge version matching forge_version, or ask the lead to update it",
        )


# --------------------------------------------------------------------------- views


class Reason(StrEnum):
    """Why a task is not a candidate (the keys of ``data.excluded``)."""

    INACTIVE_MILESTONE = "inactive_milestone"
    PROPOSED = "proposed"
    IN_REVIEW = "in_review"
    CONFLICT = "conflict"
    MINE = "mine"
    CLAIMED = "claimed"
    STALE_CLAIM = "stale_claim"
    BLOCKED = "blocked"
    UNKNOWN_ZONE = "unknown_zone"
    TIER = "tier"
    ZONE_OVERLAP = "zone_overlap"
    LOST_RACE = "lost_race"
    YIELDED = "yielded"


@dataclass(frozen=True)
class TaskView:
    """One open task as seen by ``forge next``.

    ``claim`` is set when the branch's last Forge event is a claim. ``activity`` is its last
    activity (the tip date alone when the tip is recent enough to make the claim live
    anyway), and ``stale`` says whether the claim is stale.
    """

    record: TaskRecord
    status: Status | None
    branch: TaskBranch | None
    has_open_pr: bool
    milestone_active: bool
    wave: int
    claim: ClaimInfo | None = None
    activity: datetime | None = None
    stale: bool = False

    @property
    def issue(self) -> int:
        """The issue number."""
        return self.record.issue.number

    @property
    def task_id(self) -> str:
        """The task id (``M1-T07``)."""
        return self.record.meta.id

    @property
    def live(self) -> bool:
        """True when the task holds a live (non-stale) claim."""
        return self.claim is not None and not self.stale


@dataclass(frozen=True)
class Candidate:
    """A claimable task. ``reclaim`` marks a stale claim taken over (``--reclaim``)."""

    view: TaskView
    reclaim: bool = False

    @property
    def resume(self) -> bool:
        """True when the claim continues an existing branch (handoff, release, reclaim)."""
        return self.view.branch is not None

    def to_data(self) -> dict[str, Any]:
        """JSON form used by ``--dry-run``."""
        meta = self.view.record.meta
        return {
            "issue": self.view.issue,
            "id": meta.id,
            "status": None if self.view.status is None else self.view.status.value,
            "milestone": meta.milestone,
            "wave": self.view.wave,
            "size": meta.size.value,
            "tier": meta.tier.value,
            "zones": list(meta.zones),
            "resume": self.resume,
            "reclaim": self.reclaim,
        }


@dataclass(frozen=True)
class Exclusion:
    """A task that is not a candidate, with a machine reason and a human detail."""

    issue: int
    task_id: str
    reason: Reason
    detail: str

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "issue": self.issue,
            "id": self.task_id,
            "reason": self.reason.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Selection:
    """Ordered candidates plus the exclusions."""

    candidates: tuple[Candidate, ...]
    exclusions: tuple[Exclusion, ...]


def order_key(view: TaskView) -> tuple[Any, ...]:
    """ADR 0009 order: handoff first, milestone (natural), wave, size S<M, issue number."""
    meta = view.record.meta
    return (
        0 if view.status is Status.HANDOFF else 1,
        natural_key(meta.milestone),
        view.wave,
        0 if meta.size is Size.S else 1,
        view.issue,
    )


def zones_overlap(zones: ZonesConfig, a: Iterable[str], b: Iterable[str]) -> str | None:
    """Return why two zone lists overlap, or ``None``.

    A shared name always overlaps, even when it is missing from ``zones.yml``; otherwise
    only known zones are compared (:meth:`ZonesConfig.overlap`).
    """
    set_a, set_b = set(a), set(b)
    shared = sorted(set_a & set_b)
    if shared:
        return f"shared zone {shared[0]}"
    known_a = [z for z in set_a if z in zones.zones]
    known_b = [z for z in set_b if z in zones.zones]
    found = zones.overlap(known_a, known_b)
    return None if found is None else found.detail


def _holder_why(view: TaskView) -> str:
    if view.has_open_pr:
        return "open pull request"
    claim = view.claim
    return "claimed" if claim is None else f"claimed by {claim.owner}"


def _claim_detail(view: TaskView, lease_hours: int) -> str:
    claim = view.claim
    assert claim is not None
    who = f"{claim.owner} (agent {claim.agent or 'unknown'})"
    activity = view.activity or claim.tip_date
    when = format_time(stale_at(activity, lease_hours))
    if view.stale:
        return f"the claim by {who} went stale at {when}; take it over with --reclaim"
    return f"claimed by {who}; the claim becomes stale at {when} unless renewed"


def _exclusion(
    view: TaskView,
    *,
    zones: ZonesConfig,
    agent: AgentProfile,
    login: str,
    holders: Sequence[TaskView],
    any_tier: bool,
    reclaim: bool,
    tried: Mapping[int, Reason],
    lease_hours: int,
) -> tuple[Reason, str] | None:
    meta = view.record.meta
    if view.issue in tried:
        reason = tried[view.issue]
        verb = "lost the race for" if reason is Reason.LOST_RACE else "yielded"
        return reason, f"{verb} this task earlier in this run"
    if not view.milestone_active:
        return Reason.INACTIVE_MILESTONE, f"milestone {view.record.issue.milestone} is closed"
    if view.status is Status.PROPOSED:
        return Reason.PROPOSED, "the task is proposed; the lead has not accepted it yet"
    if view.has_open_pr:
        name = TaskMetadata.branch_name(view.issue)
        return Reason.IN_REVIEW, f"an open pull request exists from {name}"
    if has_label(view.record.issue.labels, BLOCKED_CONFLICT):
        return Reason.CONFLICT, f"the issue carries {BLOCKED_CONFLICT}"
    claim = view.claim
    if claim is not None and view.status is Status.IN_PROGRESS:
        if view.live:
            if claim.is_mine(login, agent.id):
                return Reason.MINE, f"you already hold #{view.issue}"
            return Reason.CLAIMED, _claim_detail(view, lease_hours)
        if not reclaim:
            return Reason.STALE_CLAIM, _claim_detail(view, lease_hours)
    elif view.status not in (Status.READY, Status.HANDOFF):
        status = "unknown" if view.status is None else view.status.value
        if view.status is Status.BLOCKED:
            return Reason.BLOCKED, "a dependency is not closed"
        return Reason.BLOCKED, f"status {status} is not claimable"
    unknown = sorted(z for z in meta.zones if z not in zones.zones)
    if unknown:
        return Reason.UNKNOWN_ZONE, f"zones not in .forge/zones.yml: {', '.join(unknown)}"
    if not any_tier and not can_serve(agent, meta.tier):
        return Reason.TIER, (
            f"agent {agent.id} maps no model for tier {meta.tier.value} or above; "
            "use --any-tier to claim it anyway"
        )
    for holder in holders:
        if holder.issue == view.issue:
            continue
        why = zones_overlap(zones, meta.zones, holder.record.meta.zones)
        if why is not None:
            return Reason.ZONE_OVERLAP, (
                f"zones overlap #{holder.issue} ({holder.task_id}, {_holder_why(holder)}): {why}"
            )
    return None


def select_candidates(
    views: Iterable[TaskView],
    *,
    zones: ZonesConfig,
    agent: AgentProfile,
    login: str,
    any_tier: bool = False,
    reclaim: bool = False,
    tried: Mapping[int, Reason] | None = None,
    lease_hours: int = 24,
) -> Selection:
    """Return the ordered candidates and the exclusions among ``views`` (pure).

    Zone holders are the tasks with a live claim or an open pull request (an open PR keeps
    its zones even after the lease goes stale). ``tried`` maps issues already lost or
    yielded in this run to their reason; they are never retried.
    """
    all_views = sorted(views, key=lambda v: v.issue)
    holders = [v for v in all_views if v.live or v.has_open_pr]
    candidates: list[Candidate] = []
    exclusions: list[Exclusion] = []
    for view in all_views:
        excluded = _exclusion(
            view,
            zones=zones,
            agent=agent,
            login=login,
            holders=holders,
            any_tier=any_tier,
            reclaim=reclaim,
            tried=tried or {},
            lease_hours=lease_hours,
        )
        if excluded is not None:
            exclusions.append(Exclusion(view.issue, view.task_id, *excluded))
            continue
        candidates.append(Candidate(view, reclaim=view.status is Status.IN_PROGRESS))
    candidates.sort(key=lambda c: order_key(c.view))
    return Selection(tuple(candidates), tuple(exclusions))


def resolve_task_ref(ref: str, records: Iterable[TaskRecord]) -> int | None:
    """Resolve ``M1-T07``, ``#42`` or ``42`` to an open task's issue number (``None``: none).

    A task id shared by several issues resolves to the lowest issue number. Anything else
    raises :class:`ForgeError`.
    """
    text = ref.strip()
    open_records = [r for r in records if r.issue.state == "open"]
    if TASK_ID_RE.fullmatch(text):
        numbers = sorted(r.issue.number for r in open_records if r.meta.id == text)
        return numbers[0] if numbers else None
    match = _ISSUE_REF_RE.fullmatch(text)
    if match is None:
        raise ForgeError(
            f"invalid task reference {ref!r}",
            hint="use a task id (M1-T07) or an issue number (#42 or 42)",
        )
    number = int(match.group(1))
    return number if any(r.issue.number == number for r in open_records) else None


# --------------------------------------------------------------------------- reading state


class _Leases:
    """Lease comments, read lazily and cached for one run."""

    def __init__(self, github: GitHubClient) -> None:
        self._github = github
        self._cache: dict[int, LeaseData | None] = {}

    def get(self, issue_number: int) -> LeaseData | None:
        if issue_number not in self._cache:
            found = find_lease(self._github.list_comments(issue_number))
            self._cache[issue_number] = None if found is None else found.data
        return self._cache[issue_number]


@dataclass(frozen=True)
class _GitHubState:
    records: tuple[TaskRecord, ...]
    open_heads: frozenset[str]
    open_milestones: frozenset[str]
    deps_closed: Mapping[str, bool]
    waves: Mapping[str, int]


def _read_github(github: GitHubClient, warnings: list[str]) -> _GitHubState:
    milestones = frozenset(m.title for m in github.list_milestones(state="open"))
    issues = github.list_issues(state="all", labels=[FORGE_TASK_LABEL])
    heads = frozenset(pr.head for pr in github.list_pulls(state="open"))
    records = tuple(parse_records(issues, warnings))
    first: dict[str, TaskRecord] = {}
    for rec in records:
        if rec.meta.id in first:
            warnings.append(
                f"issue #{rec.issue.number}: duplicate task id {rec.meta.id} (also on issue "
                f"#{first[rec.meta.id].issue.number}); dependencies on {rec.meta.id} use "
                f"issue #{first[rec.meta.id].issue.number}"
            )
            continue
        first[rec.meta.id] = rec
    deps_closed = {tid: rec.issue.state == "closed" for tid, rec in first.items()}
    graph = TaskGraph({tid: rec.meta.depends_on for tid, rec in first.items()})
    try:
        waves = graph.wave_of(ignore_unknown=True)
    except GraphCycleError as exc:
        warnings.append(f"{exc.message}; every task counts as wave 0 for ordering")
        waves = {}
    return _GitHubState(records, heads, milestones, deps_closed, waves)


@dataclass(frozen=True)
class _Snapshot:
    views: Mapping[int, TaskView]
    default_sha: str


def _fetch_branches(ctx: ClaimContext, warnings: list[str] | None = None) -> dict[int, TaskBranch]:
    default = ctx.config.project.default_branch
    fetch_task_refs(ctx.git, default, ctx.remote)
    scan = gather_task_branches(ctx.git, default, ctx.remote)
    if warnings is not None:
        warnings.extend(scan.warnings)
    return dict(scan.branches)


def _assess(
    ctx: ClaimContext, claim: ClaimInfo, leases: _Leases, now: datetime, *, exact: bool
) -> tuple[datetime, bool]:
    """Return (last activity, stale). The lease is read only when it can matter."""
    hours = ctx.config.lease_hours
    if not exact and not is_stale(claim.tip_date, hours, now):
        return claim.tip_date, False
    activity = last_activity(claim.tip_date, leases.get(claim.issue), claim.sha)
    return activity, is_stale(activity, hours, now)


def _snapshot(
    ctx: ClaimContext,
    gh: _GitHubState,
    leases: _Leases,
    warnings: list[str],
    *,
    exact: Iterable[int] = (),
) -> _Snapshot:
    """Fetch the task branches and build a view of every open task (pre-push snapshot)."""
    branches = _fetch_branches(ctx, warnings)
    default = ctx.config.project.default_branch
    default_sha = ctx.git.rev_parse(f"refs/remotes/{ctx.remote}/{default}")
    if default_sha is None:  # gather_task_branches already checked it; keep mypy honest
        raise PreconditionError(f"{ctx.remote}/{default} not found")
    now = ctx.now()
    exact_set = set(exact)
    views: dict[int, TaskView] = {}
    for rec in gh.records:
        if rec.issue.state != "open":
            continue
        number = rec.issue.number
        branch = branches.get(number)
        has_pr = TaskMetadata.branch_name(number) in gh.open_heads
        derived = derive_status(
            issue=rec.issue,
            meta=rec.meta,
            deps_closed=gh.deps_closed,
            branch=BranchState.absent() if branch is None else branch.state(rec.meta.id),
            has_open_pr=has_pr,
        )
        warnings.extend(derived.warnings)
        claim = None if branch is None else find_claim(number, branch, rec.meta.id)
        activity: datetime | None = None
        stale = False
        if claim is not None:
            activity, stale = _assess(ctx, claim, leases, now, exact=number in exact_set)
        milestone = rec.issue.milestone
        views[number] = TaskView(
            record=rec,
            status=derived.status,
            branch=branch,
            has_open_pr=has_pr,
            milestone_active=milestone is None or milestone in gh.open_milestones,
            wave=gh.waves.get(rec.meta.id, 0),
            claim=claim,
            activity=activity,
            stale=stale,
        )
    return _Snapshot(views, default_sha)


# --------------------------------------------------------------------------- outcome


@dataclass(frozen=True)
class Claimed:
    """A claim that is held and whose GitHub steps are done.

    ``base`` is the merge-base of the claim with the default branch (for a new claim, the
    default branch tip at claim time). ``resumed`` means the claim continued an existing
    branch (handoff, release or reclaim); ``reclaimed`` that it took over a stale claim of
    ``previous_claimant``; ``own`` that the claim already existed and was mine, so nothing
    was pushed in this run.
    """

    record: TaskRecord
    branch: str
    claim: str
    base: str
    claimed_at: datetime
    login: str
    agent: str
    resumed: bool = False
    reclaimed: bool = False
    own: bool = False
    previous_claimant: str | None = None

    @property
    def issue(self) -> int:
        """The issue number."""
        return self.record.issue.number

    @property
    def task_id(self) -> str:
        """The task id."""
        return self.record.meta.id

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "issue": self.issue,
            "id": self.task_id,
            "branch": self.branch,
            "claim": self.claim,
            "base": self.base,
            "claimed_at": format_time(self.claimed_at),
            "login": self.login,
            "agent": self.agent,
            "resumed": self.resumed,
            "reclaimed": self.reclaimed,
            "own": self.own,
            "previous_claimant": self.previous_claimant,
        }


@dataclass
class NextOutcome:
    """Result of :func:`claim_next`. ``code`` is 0 (claimed / dry run) or 3 (nothing)."""

    code: ExitCode
    claimed: Claimed | None = None
    candidates: list[Candidate] = field(default_factory=list)
    exclusions: list[Exclusion] = field(default_factory=list)
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    attempts: int = 0
    dry_run: bool = False
    resume_own: list[int] = field(default_factory=list)

    def excluded_counts(self) -> dict[str, int]:
        """Number of excluded tasks per reason."""
        return dict(sorted(Counter(e.reason.value for e in self.exclusions).items()))

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` for ``forge next`` / ``forge reclaim``."""
        return {
            "claimed": None if self.claimed is None else self.claimed.to_data(),
            "dry_run": self.dry_run,
            "candidates": [c.to_data() for c in self.candidates],
            "resume_own": list(self.resume_own),
            "excluded": self.excluded_counts(),
            "exclusions": [e.to_data() for e in self.exclusions],
            "attempts": self.attempts,
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class NextOptions:
    """Options of ``forge next`` (``reclaim`` alone is ``forge reclaim <n>`` with ``task``)."""

    task: str | None = None
    reclaim: bool = False
    any_tier: bool = False
    dry_run: bool = False


# --------------------------------------------------------------------------- claim + verify


@dataclass(frozen=True)
class _Pushed:
    sha: str
    rollback: str | None  # previous tip to restore on yield; None = delete the branch


def _claim_trailers(login: str, agent: str, task_id: str) -> dict[str, str]:
    return {TRAILER_CLAIM: login, TRAILER_AGENT: agent, TRAILER_TASK: task_id}


def _push_claim(
    ctx: ClaimContext, login: str, candidate: Candidate, default_sha: str
) -> _Pushed | None:
    """Create and push the claim commit. ``None`` when the push lost the race."""
    view = candidate.view
    ref = f"refs/heads/{TaskMetadata.branch_name(view.issue)}"
    tip = None if view.branch is None else view.branch.tip
    parent = tip if tip is not None else default_sha
    sha = ctx.git.commit_tree(
        ctx.git.tree_of(parent),
        [parent],
        f"forge: claim {view.task_id}",
        _claim_trailers(login, ctx.agent.id, view.task_id),
        date=ctx.now(),
    )
    result = ctx.git.push(sha, ref, lease=Lease(ref, tip), remote=ctx.remote, no_verify=True)
    if not result.ok:
        return None
    return _Pushed(sha, tip)


class _Index:
    """Open task records by issue number; unknown issues are fetched once."""

    def __init__(self, github: GitHubClient, records: Iterable[TaskRecord]) -> None:
        self._github = github
        self._records: dict[int, TaskRecord | None] = {r.issue.number: r for r in records}

    def open_record(self, number: int) -> TaskRecord | None:
        if number not in self._records:
            try:
                self._records[number] = load_task(self._github, number)
            except ForgeError:
                self._records[number] = None
        rec = self._records[number]
        return rec if rec is not None and rec.issue.state == "open" else None


def _live_conflicts(
    ctx: ClaimContext,
    record: TaskRecord,
    branches: Mapping[int, TaskBranch],
    index: _Index,
    leases: _Leases,
) -> list[ClaimInfo]:
    """Live claims of other open tasks whose zones overlap ``record``'s."""
    now = ctx.now()
    conflicts: list[ClaimInfo] = []
    for number, branch in sorted(branches.items()):
        if number == record.issue.number:
            continue
        other = index.open_record(number)
        if other is None:
            continue
        claim = find_claim(number, branch, other.meta.id)
        if claim is None:
            continue
        if zones_overlap(ctx.zones, record.meta.zones, other.meta.zones) is None:
            continue
        _, stale = _assess(ctx, claim, leases, now, exact=False)
        if not stale:
            conflicts.append(claim)
    return conflicts


def _still_claimed(branch: TaskBranch | None, claim: ClaimInfo) -> bool:
    if branch is None:
        return False
    current = find_claim(claim.issue, branch, claim.task_id)
    return current is not None and current.sha == claim.sha


def _verify(
    ctx: ClaimContext,
    record: TaskRecord,
    claim_sha: str,
    index: _Index,
    leases: _Leases,
) -> ClaimInfo | None:
    """Post-claim zone verification (ADR 0009, safe variant).

    Returns ``None`` when I keep the claim, else the claim I yield to. Lists the live claims
    again (S2). A conflicting claim that comes first in the order (claim commit date, then
    issue number) makes me yield at once; otherwise I wait for every conflicting claim to go
    away, polling ``max_polls`` times every ``poll_interval`` seconds, and yield if one is
    still live after the last poll.
    """
    mine = (ctx.git.commit_date(claim_sha), record.issue.number)
    conflicts = _live_conflicts(ctx, record, _fetch_branches(ctx), index, leases)
    for other in sorted(conflicts, key=lambda c: c.order_key):
        if other.order_key < mine:
            return other
    pending = list(conflicts)
    polls = 0
    while pending and polls < ctx.max_polls:
        ctx.sleep(ctx.poll_interval)
        polls += 1
        branches = _fetch_branches(ctx)
        pending = [c for c in pending if _still_claimed(branches.get(c.issue), c)]
    return pending[0] if pending else None


def _yield(ctx: ClaimContext, issue_number: int, pushed: _Pushed) -> str | None:
    """Undo my claim with a lease on my claim sha. Returns a warning if that failed."""
    name = TaskMetadata.branch_name(issue_number)
    ref = f"refs/heads/{name}"
    if pushed.rollback is None:
        result = ctx.git.delete_remote_branch(name, pushed.sha, ctx.remote, no_verify=True)
    else:
        result = ctx.git.push(
            pushed.rollback,
            ref,
            lease=Lease(ref, pushed.sha),
            remote=ctx.remote,
            no_verify=True,
        )
    if result.ok:
        return None
    current = ctx.git.remote_branch_sha(name, ctx.remote)
    if current == pushed.rollback:  # absent for a delete, the previous tip for a resume
        return None
    return (
        f"could not roll back the claim on {name}: the remote branch moved to "
        f"{current or 'nothing'} instead of {pushed.rollback or 'deletion'}"
    )


# --------------------------------------------------------------------------- GitHub steps


@dataclass(frozen=True)
class _Previous:
    owner: str
    agent: str | None
    sha: str
    tip_date: datetime


def _previous_claim(ctx: ClaimContext, record: TaskRecord, claim_sha: str) -> _Previous | None:
    """The claim my claim commit was stacked on (a reclaim), read from git history."""
    default_ref = f"refs/remotes/{ctx.remote}/{ctx.config.project.default_branch}"
    base = ctx.git.merge_base(default_ref, claim_sha)
    older = ctx.git.commits(f"{base}..{claim_sha}", first_parent=True)[1:]
    index = event_commit_index(older, record.meta.id)
    if index is None:
        return None
    event = last_event_from_trailers([older[index].trailers], task_id=record.meta.id)
    if event is None or event.event is not BranchEvent.CLAIM:
        return None
    return _Previous(event.author, event.agent, older[index].sha, older[0].date)


def _apply_labels(github: GitHubClient, issue_number: int, diff: LabelDiff) -> None:
    if diff.add:
        ensure_labels(github)
        github.add_labels(issue_number, sorted(diff.add))
    for name in sorted(diff.remove):
        github.remove_label(issue_number, name)


def _reclaim_marker(claim_sha: str) -> str:
    return f"<!-- forge:reclaim {claim_sha} -->"


def _post_claim(
    ctx: ClaimContext,
    login: str,
    record: TaskRecord,
    claim_sha: str,
    *,
    resumed: bool,
    own: bool,
) -> Claimed:
    """Assign, label, reclaim comment, lease comment (ADR 0009 post-claim steps 1-4).

    The lease comment is written **last**: once it references the claim commit the claim
    counts as complete, so an interruption anywhere before is completed by a re-run. Every
    step is idempotent (assignments and labels are diffs, the reclaim comment carries a
    marker with the claim sha, the lease comment is edited in place).
    """
    number = record.issue.number
    github = ctx.github
    previous = _previous_claim(ctx, record, claim_sha)
    issue = github.get_issue(number)
    if not any(a.casefold() == login.casefold() for a in issue.assignees):
        github.add_assignees(number, [login])
    if previous is not None and previous.owner.casefold() != login.casefold():
        github.remove_assignees(number, [previous.owner])
    _apply_labels(github, number, desired_labels(issue.labels, Status.IN_PROGRESS, record.meta))

    comments = github.list_comments(number)
    found = find_lease(comments)
    lease = None if found is None else found.data
    now = ctx.now()
    if previous is not None:
        marker = _reclaim_marker(claim_sha)
        if not any(marker in c.body for c in comments):
            activity = last_activity(previous.tip_date, lease, previous.sha)
            went_stale = format_time(stale_at(activity, ctx.config.lease_hours))
            github.create_comment(
                number,
                f"{marker}\n**Forge reclaim**: {login} (agent {ctx.agent.id}) took over "
                f"`{record.meta.id}` on `{TaskMetadata.branch_name(number)}`. The previous "
                f"claim by @{previous.owner} (agent {previous.agent or 'unknown'}, commit "
                f"`{previous.sha[:12]}`) went stale at {went_stale}.\n",
            )
    claimed_at = (
        lease.claimed_at
        if lease is not None and lease.claim == claim_sha
        else ctx.git.commit_date(claim_sha).astimezone(UTC)
    )
    data = LeaseData(
        task=record.meta.id,
        owner=login,
        agent=ctx.agent.id,
        claim=claim_sha,
        claimed_at=claimed_at,
        heartbeat_at=now,
    )
    upsert_lease(github, number, data, comments=comments)
    default_ref = f"refs/remotes/{ctx.remote}/{ctx.config.project.default_branch}"
    return Claimed(
        record=record,
        branch=TaskMetadata.branch_name(number),
        claim=claim_sha,
        base=ctx.git.merge_base(default_ref, claim_sha),
        claimed_at=claimed_at,
        login=login,
        agent=ctx.agent.id,
        resumed=resumed,
        reclaimed=previous is not None,
        own=own,
        previous_claimant=None if previous is None else previous.owner,
    )


# --------------------------------------------------------------------------- next


def _own_rollback(view: TaskView) -> str | None:
    """For my own pushed claim: the tip to restore on yield (``None``: delete the branch)."""
    claim, branch = view.claim, view.branch
    assert claim is not None and branch is not None
    following = claim.index + 1
    return branch.commits[following].sha if following < len(branch.commits) else None


def claim_next(ctx: ClaimContext, options: NextOptions | None = None) -> NextOutcome:
    """Select and claim a task (``forge next``; ``forge reclaim`` with ``task`` + ``reclaim``).

    Raises :class:`PreconditionError` (exit 5) on a version mismatch, a login that differs
    from the profile, or a shallow clone. Returns code 3 when nothing is claimable, when
    ``claim.max_attempts`` races were lost, or when the requested task is not claimable (the
    reason is in :attr:`NextOutcome.reason`). GitHub and git failures raise.
    """
    opts = options or NextOptions()
    check_version(ctx)
    login = check_identity(ctx.github, ctx.profile_login)
    check_full_history(ctx.git, "claims")

    warnings: list[str] = []
    leases = _Leases(ctx.github)
    gh = _read_github(ctx.github, warnings)

    target: int | None = None
    if opts.task is not None:
        target = resolve_task_ref(opts.task, gh.records)
        if target is None:
            return NextOutcome(
                ExitCode.NOTHING_AVAILABLE,
                reason=f"{opts.task}: no open Forge task matches",
                warnings=_dedupe(warnings),
            )
    # For a targeted task the exact lease matters (the message says when a claim goes stale).
    snap = _snapshot(ctx, gh, leases, warnings, exact=[] if target is None else [target])

    def _mine(view: TaskView) -> bool:
        return view.live and view.claim is not None and view.claim.is_mine(login, ctx.agent.id)

    def _complete(view: TaskView) -> bool:
        lease = leases.get(view.issue)
        claim = view.claim
        assert claim is not None
        return lease is not None and lease.claim == claim.sha and lease.released_at is None

    own = sorted((v for v in snap.views.values() if _mine(v)), key=lambda v: v.issue)
    incomplete = [v.issue for v in own if not _complete(v)]
    outcome = NextOutcome(ExitCode.OK, dry_run=opts.dry_run)
    outcome.info = [f"you already hold #{v.issue} ({v.task_id})" for v in own if _complete(v)]
    index = _Index(ctx.github, gh.records)

    if target is not None and any(v.issue == target for v in own):
        view = snap.views[target]
        if target not in incomplete:
            if opts.dry_run:
                outcome.resume_own = [target]
                return _finish(outcome, warnings)
            assert view.claim is not None
            outcome.claimed = _post_claim(
                ctx, login, view.record, view.claim.sha, resumed=False, own=True
            )
            return _finish(outcome, warnings)
        queue = [target]
    else:
        queue = [] if target is not None else list(incomplete)

    if opts.dry_run:
        selection = _select(ctx, snap, login, opts, {})
        outcome.resume_own = queue
        outcome.candidates = _targeted(selection.candidates, target)
        outcome.exclusions = list(selection.exclusions)
        if not outcome.candidates and not queue:
            outcome.code = ExitCode.NOTHING_AVAILABLE
            outcome.reason = _nothing_reason(selection, target, opts.task)
        return _finish(outcome, warnings)

    tried: dict[int, Reason] = {}
    max_attempts = ctx.config.claim.max_attempts
    while True:
        record: TaskRecord
        pushed: _Pushed
        if queue:
            view = snap.views[queue.pop(0)]
            assert view.claim is not None
            record = view.record
            pushed = _Pushed(view.claim.sha, _own_rollback(view))
            own_claim = True
            if view.claim.tip != view.claim.sha:
                # Work was pushed on top of the claim: it was clearly kept; do not roll back.
                warnings.append(
                    f"#{view.issue}: commits follow your interrupted claim; post-claim "
                    "verification skipped"
                )
                outcome.claimed = _post_claim(
                    ctx,
                    login,
                    record,
                    view.claim.sha,
                    resumed=pushed.rollback is not None,
                    own=True,
                )
                return _finish(outcome, warnings)
        else:
            selection = _select(ctx, snap, login, opts, tried)
            outcome.exclusions = list(selection.exclusions)
            candidates = _targeted(selection.candidates, target)
            if not candidates:
                outcome.code = ExitCode.NOTHING_AVAILABLE
                outcome.reason = _nothing_reason(selection, target, opts.task)
                return _finish(outcome, warnings)
            if outcome.attempts >= max_attempts:
                outcome.code = ExitCode.NOTHING_AVAILABLE
                outcome.reason = (
                    f"gave up after {outcome.attempts} lost race(s) (claim.max_attempts = "
                    f"{max_attempts})"
                )
                return _finish(outcome, warnings)
            candidate = candidates[0]
            record = candidate.view.record
            own_claim = False
            maybe = _push_claim(ctx, login, candidate, snap.default_sha)
            if maybe is None:
                outcome.attempts += 1
                tried[record.issue.number] = Reason.LOST_RACE
                warnings.append(f"#{record.issue.number}: lost the race; trying the next task")
                snap = _snapshot(ctx, gh, leases, warnings)
                continue
            pushed = maybe

        winner = _verify(ctx, record, pushed.sha, index, leases)
        if winner is None:
            outcome.claimed = _post_claim(
                ctx,
                login,
                record,
                pushed.sha,
                resumed=pushed.rollback is not None,
                own=own_claim,
            )
            return _finish(outcome, warnings)
        problem = _yield(ctx, record.issue.number, pushed)
        if problem is not None:
            warnings.append(problem)
        outcome.attempts += 1
        tried[record.issue.number] = Reason.YIELDED
        warnings.append(
            f"#{record.issue.number}: yielded to #{winner.issue} ({winner.task_id}, claimed by "
            f"{winner.owner}), whose zones overlap; trying the next task"
        )
        snap = _snapshot(ctx, gh, leases, warnings)


def _select(
    ctx: ClaimContext,
    snap: _Snapshot,
    login: str,
    opts: NextOptions,
    tried: Mapping[int, Reason],
) -> Selection:
    return select_candidates(
        snap.views.values(),
        zones=ctx.zones,
        agent=ctx.agent,
        login=login,
        any_tier=opts.any_tier,
        reclaim=opts.reclaim,
        tried=tried,
        lease_hours=ctx.config.lease_hours,
    )


def _targeted(candidates: Sequence[Candidate], target: int | None) -> list[Candidate]:
    if target is None:
        return list(candidates)
    return [c for c in candidates if c.view.issue == target]


def _nothing_reason(selection: Selection, target: int | None, ref: str | None) -> str:
    if target is not None:
        for excl in selection.exclusions:
            if excl.issue == target:
                return f"#{target} ({excl.task_id}) is not claimable: {excl.detail}"
        return f"{ref}: not claimable"
    return "nothing claimable"


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _finish(outcome: NextOutcome, warnings: Iterable[str]) -> NextOutcome:
    outcome.warnings = _dedupe(warnings)
    return outcome


def reclaim_task(ctx: ClaimContext, task: str) -> NextOutcome:
    """``forge reclaim <n>``: ``forge next --task <n> --reclaim``.

    A fresh claim held by someone else gives code 3, and the reason says when the claim
    becomes stale.
    """
    return claim_next(ctx, NextOptions(task=task, reclaim=True))


# --------------------------------------------------------------------------- release


@dataclass(frozen=True)
class ReleaseOutcome:
    """Result of :func:`release_claim`."""

    issue: int
    task_id: str
    branch: str
    claim: str
    release_commit: str | None
    deleted: bool
    status: Status | None
    unpushed: int
    recovered: bool
    warnings: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` for ``forge release-claim``."""
        return {
            "issue": self.issue,
            "id": self.task_id,
            "branch": self.branch,
            "claim": self.claim,
            "release_commit": self.release_commit,
            "deleted": self.deleted,
            "status": None if self.status is None else self.status.value,
            "unpushed": self.unpushed,
            "recovered": self.recovered,
            "warnings": list(self.warnings),
        }


def _release_marker(claim_sha: str) -> str:
    return f"<!-- forge:release {claim_sha} -->"


def _non_event_commits(git: Git, default_ref: str, tip: str) -> list[str]:
    """Commits since the merge-base that are not empty Forge event commits."""
    base = git.merge_base(default_ref, tip)
    blockers: list[str] = []
    for commit in git.commits(f"{base}..{tip}"):
        event = last_event_from_trailers([commit.trailers])
        empty = git.rev_parse(f"{commit.sha}^2") is None and git.tree_of(commit.sha) == git.tree_of(
            f"{commit.sha}^"
        )
        if event is None or not empty:
            blockers.append(commit.sha)
    return blockers


def _count_unpushed(git: Git, local_ref: str, remote_tip: str) -> int:
    local = git.rev_parse(local_ref)
    if local is None or local == remote_tip:
        return 0
    return len(git.commits(f"{remote_tip}..{local}"))


def _interrupted_release(
    login: str, agent: str, record: TaskRecord, branch: TaskBranch | None, lease: LeaseData | None
) -> bool:
    """True when a previous release of mine stopped after its git step."""
    if lease is None or lease.released_at is not None or record.issue.state != "open":
        return False
    if lease.owner.casefold() != login.casefold() or lease.agent != agent:
        return False
    if branch is None:
        return True
    event = last_event_from_trailers(branch.trailers, task_id=record.meta.id)
    return (
        event is not None
        and event.event is BranchEvent.RELEASE
        and event.author.casefold() == login.casefold()
    )


def release_claim(
    ctx: ClaimContext,
    issue_number: int,
    *,
    delete_branch: bool = False,
    local_ref: str | None = None,
) -> ReleaseOutcome:
    """Release my claim on ``issue_number`` (``forge release-claim``, ADR 0009).

    My own stale claim is released too (with a warning) while the branch's last event is
    still my claim; a claim of someone else, fresh or stale, is refused (exit 5).

    Pushes an empty ``Forge-Release`` commit on top of the **remote** tip with a lease
    (local commits never pushed are not published; their count is a warning). With
    ``delete_branch`` the branch is deleted with a lease instead, but only when every commit
    since the merge-base is an empty Forge event commit; otherwise it is kept, with a
    warning. Then: unassign me, set the derived status (``ready``/``blocked``), post a
    comment and set ``released_at`` in the lease comment (last, as the completion marker).

    A re-run after an interruption that followed the git step completes the GitHub steps.
    Raises :class:`PreconditionError` (exit 5) when the branch does not end with my claim.
    ``local_ref`` is the local branch to compare with (default ``refs/heads/task/<n>``).
    """
    login = check_identity(ctx.github, ctx.profile_login)
    check_full_history(ctx.git, "claims")
    github = ctx.github
    record = load_task(github, issue_number)
    task_id = record.meta.id
    name = TaskMetadata.branch_name(issue_number)
    ref = f"refs/heads/{name}"
    default_ref = f"refs/remotes/{ctx.remote}/{ctx.config.project.default_branch}"
    branch = read_branch(ctx, issue_number)
    comments = github.list_comments(issue_number)
    found = find_lease(comments)
    lease = None if found is None else found.data
    claim = None if branch is None else find_claim(issue_number, branch, task_id)
    now = ctx.now()
    warnings: list[str] = []

    release_sha: str | None = None
    deleted = False
    unpushed = 0
    recovered = False
    if claim is not None and claim.is_mine(login, ctx.agent.id):
        assert branch is not None
        activity = last_activity(claim.tip_date, lease, claim.sha)
        if is_stale(activity, ctx.config.lease_hours, now):
            # Nobody reclaimed it (their claim would be the last event): release it anyway.
            warnings.append(
                f"your claim on #{issue_number} ({task_id}) had gone stale at "
                f"{format_time(stale_at(activity, ctx.config.lease_hours))}; released"
            )
        claim_sha = claim.sha
        tip = branch.tip
        unpushed = _count_unpushed(ctx.git, local_ref or ref, tip)
        if unpushed:
            warnings.append(
                f"{unpushed} local commit(s) on {name} were never pushed and are not "
                "published by the release"
            )
        if delete_branch:
            blockers = _non_event_commits(ctx.git, default_ref, tip)
            if blockers:
                warnings.append(
                    f"{name} kept: {len(blockers)} commit(s) since the merge-base are not "
                    "empty Forge event commits"
                )
            else:
                result = ctx.git.delete_remote_branch(name, tip, ctx.remote, no_verify=True)
                if not result.ok and ctx.git.remote_branch_sha(name, ctx.remote) is not None:
                    raise ForgeError(
                        f"{name} moved on {ctx.remote} while releasing; nothing was changed",
                        hint="re-run forge release-claim",
                    )
                deleted = True
        if not deleted:
            release_sha = ctx.git.commit_tree(
                ctx.git.tree_of(tip),
                [tip],
                f"forge: release {task_id}",
                {TRAILER_RELEASE: login, TRAILER_TASK: task_id},
                date=now,
            )
            result = ctx.git.push(
                release_sha, ref, lease=Lease(ref, tip), remote=ctx.remote, no_verify=True
            )
            if not result.ok:
                raise ForgeError(
                    f"{name} moved on {ctx.remote} while releasing; nothing was changed",
                    hint="re-run forge release-claim",
                )
    elif _interrupted_release(login, ctx.agent.id, record, branch, lease):
        assert lease is not None
        claim_sha = lease.claim
        recovered = True
        deleted = branch is None
        warnings.append(f"#{issue_number}: completing an interrupted release")
    else:
        raise PreconditionError(
            f"you hold no live claim on #{issue_number} ({task_id})",
            hint="release only a task you claimed with this agent (see forge status --me)",
        )

    # GitHub steps; the lease comment is written last.
    github.remove_assignees(issue_number, [login])
    issues = github.list_issues(state="all", labels=[FORGE_TASK_LABEL])
    first: dict[str, bool] = {}
    for rec in parse_records(issues, []):
        first.setdefault(rec.meta.id, rec.issue.state == "closed")
    if deleted:
        state = BranchState.absent()
    else:
        tip_now = release_sha or (branch.tip if branch is not None else "")
        state = BranchState(True, tip_now, BranchEvent.RELEASE, login, None)
    issue = github.get_issue(issue_number)
    derived = derive_status(
        issue=issue,
        meta=record.meta,
        deps_closed=first,
        branch=state,
        has_open_pr=bool(github.list_pulls(state="open", head=name)),
    )
    warnings.extend(derived.warnings)
    _apply_labels(github, issue_number, desired_labels(issue.labels, derived.status, record.meta))
    marker = _release_marker(claim_sha)
    if not any(marker in c.body for c in comments):
        if deleted:
            kept = "deleted"
        elif release_sha is not None:
            kept = f"kept at `{release_sha[:12]}`"
        else:
            kept = "kept"
        status = "none" if derived.status is None else derived.status.value
        github.create_comment(
            issue_number,
            f"{marker}\n**Forge release**: {login} (agent {ctx.agent.id}) released "
            f"`{task_id}`. Branch `{name}` {kept}. Status: {status}.\n",
        )
    if lease is not None and lease.claim == claim_sha:
        released = LeaseData(
            lease.task,
            lease.owner,
            lease.agent,
            lease.claim,
            lease.claimed_at,
            lease.heartbeat_at,
            now,
        )
    else:
        claimed_at = claim.date if claim is not None else now
        released = LeaseData(task_id, login, ctx.agent.id, claim_sha, claimed_at, now, now)
    upsert_lease(github, issue_number, released, comments=github.list_comments(issue_number))
    return ReleaseOutcome(
        issue=issue_number,
        task_id=task_id,
        branch=name,
        claim=claim_sha,
        release_commit=release_sha,
        deleted=deleted,
        status=derived.status,
        unpushed=unpushed,
        recovered=recovered,
        warnings=tuple(_dedupe(warnings)),
    )
