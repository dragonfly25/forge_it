"""The data model behind ``forge status`` (spec §11.3, ADR 0010).

:func:`build_status` reads one snapshot and builds the six sections (Me, Team, Queue,
Blocked, Milestone, Health) as a :class:`StatusReport`. It is read-only: it lists GitHub
data and fetches the default branch and the ``task/*`` branches, exactly as ``forge next``
does (:func:`forge.core.claim.read_task_state`), but never pushes or writes to GitHub.
Every task status is derived (ADR 0008), never read from labels, and every claim's last
activity is exact: its lease comment is read (ADR 0009, 0010).

Rendering lives in ``commands/status.py``; :meth:`StatusReport.to_data` gives the JSON
``data`` payload, with a stable key order and UTC ISO 8601 ``Z`` timestamps.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge import __version__
from forge.core.claim import (
    Candidate,
    GitHubState,
    Leases,
    TaskView,
    read_task_state,
    select_candidates,
)
from forge.core.config import ForgeConfig, check_cli_version
from forge.core.context import defined_acceptance_criteria, latest_handoff
from forge.core.errors import ForgeError
from forge.core.git import Git
from forge.core.github import CiStatus, GitHubClient, GitHubError, Issue, PullRequest
from forge.core.graph import GraphCycleError, TaskGraph, natural_key
from forge.core.lease import ClaimInfo, format_time, stale_at
from forge.core.models import Drift, Violation, check_profile, preset_drift
from forge.core.presets import Preset
from forge.core.profile import AgentProfile, Profile
from forge.core.refresh import TaskRecord, check_full_history
from forge.core.tasks import (
    BLOCKED_CONFLICT,
    FORGE_TASK_LABEL,
    LABELS,
    Status,
    TaskBodyError,
    TaskMetadata,
    has_label,
    last_event_from_trailers,
    managed_label_prefixes,
    parse_body,
)
from forge.core.workspace import TaskLocation, find_current_task
from forge.core.zones import ZONES_PATH, ZonesConfig

SECTIONS: tuple[str, ...] = ("me", "team", "queue", "blocked", "milestone", "health")
"""Every section, in report order. ``--me`` keeps :data:`ME_SECTIONS` only."""

ME_SECTIONS: tuple[str, ...] = ("me", "queue")

QUEUE_SIZE = 3
"""How many candidates the Queue shows (spec §11.3)."""

MAX_DRIFT_COMMITS = 10
"""How many drift commits the Me section lists (the total is always given)."""

DROPPED = "dropped"
"""Status shown for a task closed as ``not_planned``."""

UNKNOWN = "unknown"

_LABEL_PREFIXES: tuple[str, ...] = (*managed_label_prefixes, "blocked:")
_CATALOGUE: frozenset[str] = frozenset(spec.name.casefold() for spec in LABELS)
_EXTERNAL = "external:"  # graph node prefix for unsatisfied dependencies outside a milestone


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class StatusOptions:
    """Options of ``forge status``: ``--me`` and ``--milestone M``."""

    me_only: bool = False
    milestone: str | None = None


@dataclass
class StatusContext:
    """Everything :func:`build_status` reads.

    ``profile`` and ``agent`` are ``None`` without a Forge profile (the report degrades:
    no queue picks, a Health warning). ``zones`` is ``None`` when ``.forge/zones.yml`` could
    not be loaded (no drift, every zone counts as unknown). ``presets`` are the shipped
    presets (:func:`forge.core.presets.load_presets`). ``cwd`` locates the current task
    (default: ``git.cwd``); ``repo_root`` is where spec files are read (default: the git
    root). ``clock`` is called once per report.
    """

    config: ForgeConfig
    git: Git
    github: GitHubClient
    zones: ZonesConfig | None
    presets: Mapping[str, Preset]
    profile: Profile | None = None
    agent: AgentProfile | None = None
    clock: Callable[[], datetime] = _utc_now
    remote: str = "origin"
    cwd: Path | None = None
    repo_root: Path | None = None
    cli_version: str = __version__

    def now(self) -> datetime:
        """Current time from :attr:`clock`, as an aware UTC datetime."""
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("the clock must return timezone-aware datetimes")
        return value.astimezone(UTC)


# --------------------------------------------------------------------------- building blocks


def task_milestone(record: TaskRecord) -> str:
    """The task's milestone: the issue's GitHub milestone, else its id prefix (ADR 0009)."""
    return record.issue.milestone or record.meta.milestone


@dataclass(frozen=True)
class TaskRef:
    """A task as every section shows it. ``status`` is derived (``dropped``: not planned)."""

    issue: int
    id: str
    title: str
    milestone: str
    status: str
    zones: tuple[str, ...]
    url: str

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "issue": self.issue,
            "id": self.id,
            "title": self.title,
            "milestone": self.milestone,
            "status": self.status,
            "zones": list(self.zones),
            "url": self.url,
        }


def _closed_status(issue: Issue) -> str:
    return DROPPED if issue.state_reason == "not_planned" else Status.DONE.value


def _ref(record: TaskRecord, status: Status | str | None) -> TaskRef:
    if isinstance(status, Status):
        text = status.value
    elif status is None:
        text = _closed_status(record.issue) if record.issue.state == "closed" else UNKNOWN
    else:
        text = status
    return TaskRef(
        issue=record.issue.number,
        id=record.meta.id,
        title=record.issue.title,
        milestone=task_milestone(record),
        status=text,
        zones=record.meta.zones,
        url=record.issue.url,
    )


def _view_ref(view: TaskView) -> TaskRef:
    return _ref(view.record, view.status)


@dataclass(frozen=True)
class ClaimSummary:
    """A claim on a task branch: owner, agent, claim commit and its (exact) last activity."""

    owner: str
    agent: str | None
    claim: str
    claimed_at: datetime
    last_activity: datetime
    stale: bool
    stale_at: datetime

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "owner": self.owner,
            "agent": self.agent,
            "claim": self.claim,
            "claimed_at": format_time(self.claimed_at),
            "last_activity": format_time(self.last_activity),
            "stale": self.stale,
            "stale_at": format_time(self.stale_at),
        }


def _claim_summary(view: TaskView, lease_hours: int) -> ClaimSummary | None:
    claim: ClaimInfo | None = view.claim
    if claim is None:
        return None
    activity = view.activity or claim.tip_date
    return ClaimSummary(
        owner=claim.owner,
        agent=claim.agent,
        claim=claim.sha,
        claimed_at=claim.date,
        last_activity=activity,
        stale=view.stale,
        stale_at=stale_at(activity, lease_hours),
    )


@dataclass(frozen=True)
class PullInfo:
    """An open pull request from a task branch."""

    number: int
    url: str
    author: str
    draft: bool
    head_sha: str

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "number": self.number,
            "url": self.url,
            "author": self.author,
            "draft": self.draft,
            "head_sha": self.head_sha,
        }


def _pull_info(pr: PullRequest) -> PullInfo:
    return PullInfo(pr.number, pr.url, pr.author, pr.draft, pr.head_sha)


@dataclass(frozen=True)
class CiInfo:
    """The combined CI state of a pull request's head commit."""

    sha: str
    state: str
    total: int
    failing: tuple[str, ...]
    pending: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "sha": self.sha,
            "state": self.state,
            "total": self.total,
            "failing": list(self.failing),
            "pending": list(self.pending),
        }


# --------------------------------------------------------------------------- Me


@dataclass(frozen=True)
class SyncInfo:
    """Pending sync of the current task (ADR 0010).

    ``ref`` is the task branch compared (the local branch when it exists, else the
    remote-tracking one). ``behind`` counts the commits of ``<remote>/<default>`` not in it;
    ``unpushed`` the local task-branch commits not on ``<remote>/task/<n>``.
    """

    ref: str
    behind: int
    unpushed: int

    @property
    def pending(self) -> bool:
        """True when the default branch has commits the task branch lacks."""
        return self.behind > 0

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "ref": self.ref,
            "behind": self.behind,
            "unpushed": self.unpushed,
            "pending": self.pending,
        }


@dataclass(frozen=True)
class DriftCommit:
    """An upstream commit that changes files in the task's zones."""

    sha: str
    date: datetime
    author: str
    subject: str
    zones: tuple[str, ...]
    files: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "sha": self.sha,
            "date": format_time(self.date),
            "author": self.author,
            "subject": self.subject,
            "zones": list(self.zones),
            "files": list(self.files),
        }


@dataclass(frozen=True)
class DriftInfo:
    """Drift of the current task: ``total`` matching commits, the newest ones listed."""

    total: int
    commits: tuple[DriftCommit, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {"total": self.total, "commits": [c.to_data() for c in self.commits]}


@dataclass(frozen=True)
class CurrentTaskInfo:
    """The task the working directory belongs to, with its claim, sync, drift and CI."""

    task: TaskRef
    source: str
    branch: str
    worktree: str
    claim: ClaimSummary | None
    sync: SyncInfo | None
    drift: DriftInfo | None
    pull: PullInfo | None
    ci: CiInfo | None

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            **self.task.to_data(),
            "source": self.source,
            "branch": self.branch,
            "worktree": self.worktree,
            "claim": None if self.claim is None else self.claim.to_data(),
            "sync": None if self.sync is None else self.sync.to_data(),
            "drift": None if self.drift is None else self.drift.to_data(),
            "pull": None if self.pull is None else self.pull.to_data(),
            "ci": None if self.ci is None else self.ci.to_data(),
        }


@dataclass(frozen=True)
class MyTask:
    """Another task of mine: one my login claims, or one with my open pull request."""

    task: TaskRef
    claim: ClaimSummary | None
    pull: PullInfo | None
    ci: CiInfo | None

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            **self.task.to_data(),
            "claim": None if self.claim is None else self.claim.to_data(),
            "pull": None if self.pull is None else self.pull.to_data(),
            "ci": None if self.ci is None else self.ci.to_data(),
        }


@dataclass(frozen=True)
class MeSection:
    """Who I am, my current task and my other claims and pull requests."""

    login: str
    login_source: str
    agent: str | None
    current: CurrentTaskInfo | None
    others: tuple[MyTask, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "login": self.login,
            "login_source": self.login_source,
            "agent": self.agent,
            "current": None if self.current is None else self.current.to_data(),
            "others": [t.to_data() for t in self.others],
        }


# --------------------------------------------------------------------------- Team, Queue, Blocked


@dataclass(frozen=True)
class InProgressTask:
    """A derived ``in-progress`` task with its claim (stale claims stay listed, flagged)."""

    task: TaskRef
    claim: ClaimSummary | None

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            **self.task.to_data(),
            "claim": None if self.claim is None else self.claim.to_data(),
        }


@dataclass(frozen=True)
class ReviewTask:
    """A task with an open pull request from its branch."""

    task: TaskRef
    pull: PullInfo | None

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {**self.task.to_data(), "pull": None if self.pull is None else self.pull.to_data()}


@dataclass(frozen=True)
class HandoffTask:
    """A derived ``handoff`` task: who handed it off and the newest handoff note."""

    task: TaskRef
    by: str | None
    note_author: str | None
    note_at: datetime | None

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        note = (
            None
            if self.note_author is None or self.note_at is None
            else {"author": self.note_author, "created_at": format_time(self.note_at)}
        )
        return {**self.task.to_data(), "by": self.by, "note": note}


@dataclass(frozen=True)
class TeamSection:
    """In progress, in review and handoffs waiting."""

    in_progress: tuple[InProgressTask, ...]
    in_review: tuple[ReviewTask, ...]
    handoffs: tuple[HandoffTask, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "in_progress": [t.to_data() for t in self.in_progress],
            "in_review": [t.to_data() for t in self.in_review],
            "handoffs": [t.to_data() for t in self.handoffs],
        }


@dataclass(frozen=True)
class QueueSection:
    """``ready`` tasks in active milestones (minus ``blocked:conflict``) and the next picks.

    ``agent`` is the agent the candidates were selected for (``None`` without a profile,
    and then ``next`` is empty).
    """

    ready: int
    agent: str | None
    next: tuple[Candidate, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        picks: list[dict[str, Any]] = []
        for candidate in self.next:
            data = candidate.to_data()
            head = {"issue": data.pop("issue"), "id": data.pop("id")}
            picks.append({**head, "title": candidate.view.record.issue.title, **data})
        return {"ready": self.ready, "agent": self.agent, "next": picks}


@dataclass(frozen=True)
class Dependency:
    """A dependency a blocked task waits on. ``issue`` is ``None`` for an unknown id."""

    id: str
    issue: int | None
    status: str

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {"id": self.id, "issue": self.issue, "status": self.status}


@dataclass(frozen=True)
class BlockedTask:
    """A derived ``blocked`` task and the open (or unknown) dependencies it waits on."""

    task: TaskRef
    waiting_on: tuple[Dependency, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {**self.task.to_data(), "waiting_on": [d.to_data() for d in self.waiting_on]}


@dataclass(frozen=True)
class BlockedSection:
    """Tasks blocked by dependencies, and open tasks labeled ``blocked:conflict``."""

    by_dependencies: tuple[BlockedTask, ...]
    by_conflict: tuple[TaskRef, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "by_dependencies": [t.to_data() for t in self.by_dependencies],
            "by_conflict": [t.to_data() for t in self.by_conflict],
        }


# --------------------------------------------------------------------------- Milestone


@dataclass(frozen=True)
class Wave:
    """One dependency wave of a milestone (0 = no unsatisfied dependency)."""

    index: int
    done: int
    total: int
    tasks: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "wave": self.index,
            "done": self.done,
            "total": self.total,
            "tasks": list(self.tasks),
        }


@dataclass(frozen=True)
class AcCoverage:
    """Acceptance criteria of a milestone: covered by done tasks, and the rest.

    ``specs`` are the spec files the milestone's tasks reference; ``missing_specs`` those
    that could not be read.
    """

    covered: tuple[str, ...]
    uncovered: tuple[str, ...]
    specs: tuple[str, ...]
    missing_specs: tuple[str, ...]

    @property
    def total(self) -> int:
        """Number of AC ids."""
        return len(self.covered) + len(self.uncovered)

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "total": self.total,
            "covered": list(self.covered),
            "uncovered": list(self.uncovered),
            "specs": list(self.specs),
            "missing_specs": list(self.missing_specs),
        }


@dataclass(frozen=True)
class MilestoneSection:
    """Progress of one milestone.

    ``source`` says why it was picked: ``option`` (``--milestone``), ``current_task`` or
    ``first_open``. ``open`` is true when it is an open GitHub milestone. ``done``/``total``
    and the waves exclude ``dropped`` tasks (closed as not planned) and ``proposed`` tasks
    (open, awaiting the lead; listed by id); ``statuses`` counts the counted tasks by derived
    status.
    """

    title: str
    source: str
    open: bool
    done: int
    total: int
    statuses: Mapping[str, int]
    dropped: tuple[TaskRef, ...]
    proposed: tuple[str, ...]
    waves: tuple[Wave, ...]
    acs: AcCoverage

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "title": self.title,
            "source": self.source,
            "open": self.open,
            "done": self.done,
            "total": self.total,
            "statuses": dict(self.statuses),
            "dropped": [t.to_data() for t in self.dropped],
            "proposed": list(self.proposed),
            "waves": [w.to_data() for w in self.waves],
            "acs": self.acs.to_data(),
        }


# --------------------------------------------------------------------------- Health


@dataclass(frozen=True)
class VersionHealth:
    """The CLI version against ``forge_version`` (base-version rule of ADR 0003)."""

    cli: str
    required: str
    ok: bool

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {"cli": self.cli, "required": self.required, "ok": self.ok}


@dataclass(frozen=True)
class LabelUse:
    """An unknown managed label and the open task issues carrying it."""

    label: str
    issues: tuple[int, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {"label": self.label, "issues": list(self.issues)}


@dataclass(frozen=True)
class IssueProblem:
    """An open ``forge:task`` issue whose block is missing or invalid."""

    issue: int
    title: str
    url: str
    error: str

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {"issue": self.issue, "title": self.title, "url": self.url, "error": self.error}


@dataclass(frozen=True)
class Proposal:
    """An open issue labeled ``status:proposed``. ``task`` is its task id when it has one."""

    issue: int
    title: str
    author: str
    url: str
    task: str | None
    forge_task: bool

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "issue": self.issue,
            "title": self.title,
            "author": self.author,
            "url": self.url,
            "task": self.task,
            "forge_task": self.forge_task,
        }


@dataclass(frozen=True)
class HealthSection:
    """Version, profile constraints, preset drift, labels, metadata and proposals.

    ``profile`` is ``False`` without a profile; ``violations`` and ``presets`` are then empty.
    """

    version: VersionHealth
    profile: bool
    violations: tuple[Violation, ...]
    presets: tuple[Drift, ...]
    unknown_labels: tuple[LabelUse, ...]
    without_metadata: tuple[IssueProblem, ...]
    proposals: tuple[Proposal, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "version": self.version.to_data(),
            "profile": self.profile,
            "violations": [
                {
                    "level": v.level.value,
                    "agent": v.agent_id,
                    "message": v.message,
                    "fix": v.fix,
                }
                for v in self.violations
            ],
            "presets": [
                {
                    "agent": d.agent_id,
                    "tier": d.tier.value,
                    "current": d.current,
                    "preset": d.preset,
                }
                for d in self.presets
            ],
            "unknown_labels": [u.to_data() for u in self.unknown_labels],
            "without_metadata": [p.to_data() for p in self.without_metadata],
            "proposals": [p.to_data() for p in self.proposals],
        }


# --------------------------------------------------------------------------- report


@dataclass(frozen=True)
class StatusReport:
    """The whole ``forge status`` report.

    ``sections`` names the sections produced; the others are ``None``. ``milestone`` may be
    ``None`` while listed in ``sections``, when no milestone could be picked. ``warnings``
    never change the exit code (ADR 0010).
    """

    generated_at: datetime
    options: StatusOptions
    sections: tuple[str, ...]
    me: MeSection | None = None
    team: TeamSection | None = None
    queue: QueueSection | None = None
    blocked: BlockedSection | None = None
    milestone: MilestoneSection | None = None
    health: HealthSection | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` for ``forge status``; omitted sections are ``null``."""

        def dump(section: Any) -> Any:
            return None if section is None else section.to_data()

        return {
            "generated_at": format_time(self.generated_at),
            "scope": {"me_only": self.options.me_only, "milestone": self.options.milestone},
            "sections": list(self.sections),
            "me": dump(self.me),
            "team": dump(self.team),
            "queue": dump(self.queue),
            "blocked": dump(self.blocked),
            "milestone": dump(self.milestone),
            "health": dump(self.health),
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- builder


@dataclass
class _Run:
    """State shared by the section builders of one report."""

    ctx: StatusContext
    opts: StatusOptions
    now: datetime
    login: str
    gh: GitHubState
    views: Mapping[int, TaskView]
    default_sha: str
    leases: Leases
    warnings: list[str]
    location: TaskLocation | None = None
    _ci: dict[str, CiInfo | None] = field(default_factory=dict)

    @property
    def lease_hours(self) -> int:
        return self.ctx.config.lease_hours

    @property
    def zones(self) -> ZonesConfig:
        return self.ctx.zones if self.ctx.zones is not None else ZonesConfig(zones={})

    def in_scope(self, record: TaskRecord) -> bool:
        return self.opts.milestone is None or task_milestone(record) == self.opts.milestone

    def scoped_views(self) -> list[TaskView]:
        return [v for _, v in sorted(self.views.items()) if self.in_scope(v.record)]

    def pull_for(self, issue_number: int) -> PullRequest | None:
        head = TaskMetadata.branch_name(issue_number)
        pulls = sorted((p for p in self.gh.pulls if p.head == head), key=lambda p: p.number)
        return pulls[0] if pulls else None

    def ci_for(self, pr: PullRequest) -> CiInfo | None:
        """CI of ``pr``'s head commit; a GitHub error becomes a warning and ``None``."""
        if pr.head_sha not in self._ci:
            try:
                status: CiStatus = self.ctx.github.commit_ci(pr.head_sha)
            except GitHubError as exc:
                self.warnings.append(
                    f"pull request #{pr.number}: CI state not available ({exc.message})"
                )
                self._ci[pr.head_sha] = None
            else:
                self._ci[pr.head_sha] = CiInfo(
                    pr.head_sha, status.state, status.total, status.failing, status.pending
                )
        return self._ci[pr.head_sha]

    def record_by_issue(self, number: int) -> TaskRecord | None:
        for rec in self.gh.records:
            if rec.issue.number == number:
                return rec
        return None

    def first_by_id(self) -> dict[str, TaskRecord]:
        first: dict[str, TaskRecord] = {}
        for rec in self.gh.records:
            first.setdefault(rec.meta.id, rec)
        return first


def build_status(ctx: StatusContext, opts: StatusOptions | None = None) -> StatusReport:
    """Build the ``forge status`` report (read-only; ADR 0010).

    Raises :class:`PreconditionError` (exit 5) on a shallow clone, and :class:`ForgeError`
    when fetching fails. A version mismatch, a missing profile, missing spec files, unknown
    zones and unreadable CI states are warnings.
    """
    options = opts or StatusOptions()
    warnings: list[str] = []
    check_full_history(ctx.git, "status")
    version = _version_health(ctx, warnings)
    if ctx.profile is not None:
        login, login_source = ctx.profile.github, "profile"
    else:
        login, login_source = ctx.github.current_user(), "gh"
    location = _locate(ctx, warnings)
    # The report's time is read once, after the fetch, by the shared snapshot code; every
    # section (staleness, ages, generated_at) uses that same instant.
    clock_reads: list[datetime] = []

    def read_clock() -> datetime:
        clock_reads.append(ctx.now())
        return clock_reads[-1]

    state = read_task_state(
        ctx.git,
        ctx.github,
        ctx.config,
        now=read_clock,
        warnings=warnings,
        remote=ctx.remote,
        exact=True,
    )
    now = clock_reads[0] if clock_reads else ctx.now()
    run = _Run(
        ctx=ctx,
        opts=options,
        now=now,
        login=login,
        gh=state.github,
        views=state.snapshot.views,
        default_sha=state.snapshot.default_sha,
        leases=state.leases,
        warnings=warnings,
        location=location,
    )
    sections = ME_SECTIONS if options.me_only else SECTIONS
    me = _me(run, login_source)
    queue = _queue(run)
    team = blocked = milestone = health = None
    if not options.me_only:
        team = _team(run)
        blocked = _blocked(run)
        milestone = _milestone(run, me.current)
        health = _health(run, version)
    return StatusReport(
        generated_at=now,
        options=options,
        sections=sections,
        me=me,
        team=team,
        queue=queue,
        blocked=blocked,
        milestone=milestone,
        health=health,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _version_health(ctx: StatusContext, warnings: list[str]) -> VersionHealth:
    spec = ctx.config.forge_version
    try:
        ok = check_cli_version(spec, ctx.cli_version)
    except ValueError:
        ok = False
    if not ok:
        warnings.append(
            f"forge {ctx.cli_version} does not satisfy forge_version {str(spec)!r} in "
            ".forge/config.yml; install a matching Forge version, or ask the lead to update it"
        )
    return VersionHealth(ctx.cli_version, str(spec), ok)


def _locate(ctx: StatusContext, warnings: list[str]) -> TaskLocation | None:
    try:
        location = find_current_task(ctx.git, ctx.cwd)
    except ForgeError as exc:
        warnings.append(f"current task not shown: {exc.message}")
        return None
    if location is not None:
        warnings.extend(location.notes)
    return location


# --------------------------------------------------------------------------- Me


def _me(run: _Run, login_source: str) -> MeSection:
    current = _current(run)
    agent = run.ctx.agent
    return MeSection(
        login=run.login,
        login_source=login_source,
        agent=None if agent is None else agent.id,
        current=current,
        others=_my_others(run, None if current is None else current.task.issue),
    )


def _current(run: _Run) -> CurrentTaskInfo | None:
    location = run.location
    if location is None:
        return None
    number = location.issue
    view = run.views.get(number)
    base = {
        "source": location.source,
        "branch": location.branch,
        "worktree": str(location.workspace),
    }
    if view is None:
        record = run.record_by_issue(number)
        if record is None:
            run.warnings.append(
                f"the current task #{number} ({location.branch}) is not a Forge task issue"
            )
            return None
        run.warnings.append(f"the current task #{number} ({record.meta.id}) is closed")
        return CurrentTaskInfo(
            task=_ref(record, None),
            claim=None,
            sync=None,
            drift=None,
            pull=None,
            ci=None,
            **base,
        )
    sync, drift = _sync_and_drift(run, view, location.branch)
    pr = run.pull_for(number)
    return CurrentTaskInfo(
        task=_view_ref(view),
        claim=_claim_summary(view, run.lease_hours),
        sync=sync,
        drift=drift,
        pull=None if pr is None else _pull_info(pr),
        ci=None if pr is None else run.ci_for(pr),
        **base,
    )


def _sync_and_drift(
    run: _Run, view: TaskView, branch: str
) -> tuple[SyncInfo | None, DriftInfo | None]:
    git = run.ctx.git
    remote_ref = f"refs/remotes/{run.ctx.remote}/{TaskMetadata.branch_name(view.issue)}"
    local_sha = git.rev_parse(f"refs/heads/{branch}")
    remote_sha = git.rev_parse(remote_ref)
    if local_sha is not None:
        ref, task_sha = branch, local_sha
    elif remote_sha is not None:
        ref, task_sha = remote_ref.removeprefix("refs/remotes/"), remote_sha
    else:
        run.warnings.append(
            f"#{view.issue} ({view.task_id}): neither {branch} nor its remote branch exists; "
            "sync and drift not computed"
        )
        return None, None
    behind = len(git.commits(f"{task_sha}..{run.default_sha}"))
    unpushed = 0
    if local_sha is not None:
        upstream = remote_sha if remote_sha is not None else run.default_sha
        unpushed = len(git.commits(f"{upstream}..{local_sha}"))
    sync = SyncInfo(ref, behind, unpushed)
    return sync, _drift(run, view, task_sha, behind)


def _drift(run: _Run, view: TaskView, task_sha: str, behind: int) -> DriftInfo | None:
    if run.ctx.zones is None:
        run.warnings.append(f"no zones loaded from {ZONES_PATH.as_posix()}; drift not computed")
        return None
    zones = run.zones
    known: set[str] = set()
    for name in view.record.meta.zones:
        if name in zones.zones:
            known.add(name)
        else:
            run.warnings.append(
                f"#{view.issue} ({view.task_id}): zone {name!r} is not defined in "
                f"{ZONES_PATH.as_posix()}; ignored for drift"
            )
    if behind == 0 or not known:
        return DriftInfo(0, ())
    found: list[DriftCommit] = []
    for item in run.ctx.git.commits_with_files(f"{task_sha}..{run.default_sha}"):
        matched_zones: set[str] = set()
        matched_files: list[str] = []
        for path in item.files:
            hit = known.intersection(zones.zones_for_path(path))
            if hit:
                matched_zones |= hit
                matched_files.append(path)
        if matched_zones:
            commit = item.commit
            found.append(
                DriftCommit(
                    sha=commit.sha,
                    date=commit.date,
                    author=commit.author,
                    subject=commit.subject,
                    zones=tuple(sorted(matched_zones)),
                    files=tuple(sorted(matched_files)),
                )
            )
    return DriftInfo(len(found), tuple(found[:MAX_DRIFT_COMMITS]))


def _my_others(run: _Run, current: int | None) -> tuple[MyTask, ...]:
    mine = run.login.casefold()
    numbers: set[int] = set()
    for number, view in run.views.items():
        if view.claim is not None and view.claim.owner.casefold() == mine:
            numbers.add(number)
    for pull in run.gh.pulls:
        if pull.author.casefold() != mine:
            continue
        for number in run.views:
            if pull.head == TaskMetadata.branch_name(number):
                numbers.add(number)
    if current is not None:
        numbers.discard(current)
    result: list[MyTask] = []
    for number in sorted(numbers):
        view = run.views[number]
        pr = run.pull_for(number)
        result.append(
            MyTask(
                task=_view_ref(view),
                claim=_claim_summary(view, run.lease_hours),
                pull=None if pr is None else _pull_info(pr),
                ci=None if pr is None else run.ci_for(pr),
            )
        )
    return tuple(result)


# --------------------------------------------------------------------------- Team


def _team(run: _Run) -> TeamSection:
    in_progress: list[InProgressTask] = []
    in_review: list[ReviewTask] = []
    handoffs: list[HandoffTask] = []
    for view in run.scoped_views():
        if view.status is Status.IN_PROGRESS:
            in_progress.append(
                InProgressTask(_view_ref(view), _claim_summary(view, run.lease_hours))
            )
        if view.has_open_pr:
            pr = run.pull_for(view.issue)
            in_review.append(ReviewTask(_view_ref(view), None if pr is None else _pull_info(pr)))
        if view.status is Status.HANDOFF:
            handoffs.append(_handoff(run, view))
    return TeamSection(tuple(in_progress), tuple(in_review), tuple(handoffs))


def _handoff(run: _Run, view: TaskView) -> HandoffTask:
    by: str | None = None
    if view.branch is not None:
        event = last_event_from_trailers(view.branch.trailers, task_id=view.task_id)
        by = None if event is None else event.author
    note = latest_handoff(run.leases.comments(view.issue))
    return HandoffTask(
        task=_view_ref(view),
        by=by,
        note_author=None if note is None else note.author,
        note_at=None if note is None else note.created_at,
    )


# --------------------------------------------------------------------------- Queue


def _queue(run: _Run) -> QueueSection:
    scoped = run.scoped_views()
    ready = sum(
        1
        for v in scoped
        if v.status is Status.READY
        and v.milestone_active
        and not has_label(v.record.issue.labels, BLOCKED_CONFLICT)
    )
    agent = run.ctx.agent
    if agent is None:
        run.warnings.append(
            "no Forge profile: the queue cannot pick tasks for you; run `forge setup`"
        )
        return QueueSection(ready, None, ())
    if run.ctx.zones is None:
        run.warnings.append(
            "no valid .forge/zones.yml: the queue cannot pick tasks the way `forge next` would"
        )
        return QueueSection(ready, agent.id, ())
    selection = select_candidates(
        run.views.values(),
        zones=run.zones,
        agent=agent,
        login=run.login,
        lease_hours=run.lease_hours,
    )
    picks = [c for c in selection.candidates if run.in_scope(c.view.record)]
    return QueueSection(ready, agent.id, tuple(picks[:QUEUE_SIZE]))


# --------------------------------------------------------------------------- Blocked


def _blocked(run: _Run) -> BlockedSection:
    first = run.first_by_id()
    by_deps: list[BlockedTask] = []
    by_conflict: list[TaskRef] = []
    for view in run.scoped_views():
        if view.status is Status.BLOCKED:
            waiting: list[Dependency] = []
            for dep in view.record.meta.depends_on:
                if run.gh.deps_closed.get(dep) is True:
                    continue
                rec = first.get(dep)
                if rec is None:
                    waiting.append(Dependency(dep, None, UNKNOWN))
                    continue
                dep_view = run.views.get(rec.issue.number)
                status = (
                    UNKNOWN
                    if dep_view is None or dep_view.status is None
                    else (dep_view.status.value)
                )
                waiting.append(Dependency(dep, rec.issue.number, status))
            by_deps.append(BlockedTask(_view_ref(view), tuple(waiting)))
        if has_label(view.record.issue.labels, BLOCKED_CONFLICT):
            by_conflict.append(_view_ref(view))
    return BlockedSection(tuple(by_deps), tuple(by_conflict))


# --------------------------------------------------------------------------- Milestone


def _pick_milestone(run: _Run, current: CurrentTaskInfo | None) -> tuple[str, str] | None:
    if run.opts.milestone is not None:
        return run.opts.milestone, "option"
    if current is not None:
        return current.task.milestone, "current_task"
    with_tasks = {i.milestone for i in run.gh.issues if i.milestone is not None}
    candidates = sorted(run.gh.open_milestones & with_tasks, key=natural_key)
    return (candidates[0], "first_open") if candidates else None


def _milestone(run: _Run, current: CurrentTaskInfo | None) -> MilestoneSection | None:
    picked = _pick_milestone(run, current)
    if picked is None:
        return None
    title, source = picked
    records = [r for r in run.gh.records if task_milestone(r) == title]
    if not records:
        run.warnings.append(f"milestone {title} has no Forge tasks")
    dropped = [
        r for r in records if r.issue.state == "closed" and _closed_status(r.issue) == DROPPED
    ]
    proposed = [r for r in records if _status_of(run, r) == Status.PROPOSED.value]
    excluded = {r.issue.number for r in [*dropped, *proposed]}
    counted = [r for r in records if r.issue.number not in excluded]
    statuses: Counter[str] = Counter()
    done_ids: set[str] = set()
    for rec in counted:
        status = _status_of(run, rec)
        statuses[status] += 1
        if status == Status.DONE.value:
            done_ids.add(rec.meta.id)
    order = [s.value for s in Status] + [UNKNOWN]
    return MilestoneSection(
        title=title,
        source=source,
        open=title in run.gh.open_milestones,
        done=statuses[Status.DONE.value],
        total=len(counted),
        statuses={s: statuses[s] for s in order if statuses[s]},
        dropped=tuple(_ref(r, None) for r in dropped),
        proposed=tuple(r.meta.id for r in proposed),
        waves=_waves(run, title, counted, done_ids),
        acs=_acs(run, records, counted, done_ids),
    )


def _status_of(run: _Run, record: TaskRecord) -> str:
    if record.issue.state == "closed":
        return _closed_status(record.issue)
    view = run.views.get(record.issue.number)
    if view is None or view.status is None:
        return UNKNOWN
    return view.status.value


def _waves(
    run: _Run, title: str, counted: Sequence[TaskRecord], done_ids: set[str]
) -> tuple[Wave, ...]:
    """Dependency waves of the milestone's counted tasks (ADR 0010).

    Edges to other counted tasks of the milestone are kept whatever their state. A
    dependency outside them (another milestone, a dropped task) counts as satisfied when
    that task is closed; an open or unknown one counts as an unsatisfied predecessor, so
    the task cannot be in wave 0.
    """
    tasks: dict[str, TaskRecord] = {}
    for rec in counted:
        tasks.setdefault(rec.meta.id, rec)
    deps: dict[str, list[str]] = {}
    for task_id, rec in tasks.items():
        targets: list[str] = []
        for dep in rec.meta.depends_on:
            if dep in tasks:
                targets.append(dep)
            elif run.gh.deps_closed.get(dep) is not True:
                targets.append(_EXTERNAL + dep)
                deps.setdefault(_EXTERNAL + dep, [])
        deps[task_id] = targets
    try:
        levels = TaskGraph(deps).wave_of(ignore_unknown=True)
    except GraphCycleError as exc:
        run.warnings.append(f"milestone {title}: {exc.message}; waves not shown")
        return ()
    by_level: dict[int, list[str]] = {}
    for task_id in tasks:
        by_level.setdefault(levels[task_id], []).append(task_id)
    waves: list[Wave] = []
    for level in sorted(by_level):
        ids = sorted(by_level[level], key=natural_key)
        waves.append(Wave(level, sum(1 for t in ids if t in done_ids), len(ids), tuple(ids)))
    return tuple(waves)


def _acs(
    run: _Run,
    records: Sequence[TaskRecord],
    counted: Sequence[TaskRecord],
    done_ids: set[str],
) -> AcCoverage:
    root = run.ctx.repo_root if run.ctx.repo_root is not None else run.ctx.git.root()
    users: dict[str, list[str]] = {}
    for rec in sorted(records, key=lambda r: natural_key(r.meta.id)):
        users.setdefault(rec.meta.spec, []).append(rec.meta.id)
    total: set[str] = set()
    missing: list[str] = []
    for spec in sorted(users):
        try:
            text = (root / spec).read_text(encoding="utf-8")
        except FileNotFoundError:
            problem = "not found"
        except (OSError, UnicodeDecodeError) as exc:
            problem = f"could not be read ({exc})"
        else:
            total.update(defined_acceptance_criteria(text))
            continue
        missing.append(spec)
        run.warnings.append(
            f"spec {spec} {problem} (referenced by {', '.join(dict.fromkeys(users[spec]))}); "
            "its acceptance criteria are not counted"
        )
    for rec in records:
        total.update(rec.meta.covers)
    covered = {ac for rec in counted if rec.meta.id in done_ids for ac in rec.meta.covers}
    return AcCoverage(
        covered=tuple(sorted(covered, key=natural_key)),
        uncovered=tuple(sorted(total - covered, key=natural_key)),
        specs=tuple(sorted(users)),
        missing_specs=tuple(missing),
    )


# --------------------------------------------------------------------------- Health


def _health(run: _Run, version: VersionHealth) -> HealthSection:
    ctx = run.ctx
    violations: tuple[Violation, ...] = ()
    drifts: tuple[Drift, ...] = ()
    if ctx.profile is None:
        run.warnings.append(
            "no Forge profile: profile constraints and presets not checked; run `forge setup`"
        )
    else:
        violations = tuple(check_profile(ctx.profile, ctx.config.models, ctx.presets))
        drifts = tuple(preset_drift(ctx.profile, ctx.presets))
    open_tasks = [i for i in run.gh.issues if i.state == "open"]
    return HealthSection(
        version=version,
        profile=ctx.profile is not None,
        violations=violations,
        presets=drifts,
        unknown_labels=_unknown_labels(open_tasks),
        without_metadata=_without_metadata(open_tasks),
        proposals=_proposals(ctx.github),
    )


def _unknown_labels(issues: Iterable[Issue]) -> tuple[LabelUse, ...]:
    found: dict[str, tuple[str, set[int]]] = {}
    for issue in issues:
        for label in issue.labels:
            key = label.casefold()
            if key.startswith(_LABEL_PREFIXES) and key not in _CATALOGUE:
                found.setdefault(key, (label, set()))[1].add(issue.number)
    return tuple(
        LabelUse(name, tuple(sorted(numbers))) for _, (name, numbers) in sorted(found.items())
    )


def _without_metadata(issues: Iterable[Issue]) -> tuple[IssueProblem, ...]:
    problems: list[IssueProblem] = []
    for issue in sorted(issues, key=lambda i: i.number):
        try:
            meta = parse_body(issue.body, issue_number=issue.number, milestone=issue.milestone)
        except TaskBodyError as exc:
            error = exc.reason
        else:
            if meta is not None:
                continue
            error = "no Forge block"
        problems.append(IssueProblem(issue.number, issue.title, issue.url, error))
    return tuple(problems)


def _proposals(github: GitHubClient) -> tuple[Proposal, ...]:
    proposals: list[Proposal] = []
    for issue in sorted(
        github.list_issues(state="open", labels=[Status.PROPOSED.label]), key=lambda i: i.number
    ):
        try:
            meta = parse_body(issue.body, issue_number=issue.number, milestone=issue.milestone)
        except TaskBodyError:
            meta = None
        proposals.append(
            Proposal(
                issue=issue.number,
                title=issue.title,
                author=issue.author,
                url=issue.url,
                task=None if meta is None else meta.id,
                forge_task=has_label(issue.labels, FORGE_TASK_LABEL),
            )
        )
    return tuple(proposals)


__all__ = [
    "ME_SECTIONS",
    "SECTIONS",
    "AcCoverage",
    "BlockedSection",
    "BlockedTask",
    "CiInfo",
    "ClaimSummary",
    "CurrentTaskInfo",
    "Dependency",
    "DriftCommit",
    "DriftInfo",
    "HandoffTask",
    "HealthSection",
    "InProgressTask",
    "IssueProblem",
    "LabelUse",
    "MeSection",
    "MilestoneSection",
    "MyTask",
    "Proposal",
    "PullInfo",
    "QueueSection",
    "ReviewTask",
    "StatusContext",
    "StatusOptions",
    "StatusReport",
    "SyncInfo",
    "TaskRef",
    "TeamSection",
    "VersionHealth",
    "Wave",
    "build_status",
    "task_milestone",
]
