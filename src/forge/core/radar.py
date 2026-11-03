"""`forge refresh --radar`: zone drift and stale-lease comments on in-progress tasks.

Spec §14, ADR 0014 ("Radar"). After the normal refresh, the radar reads one snapshot of the
open tasks (:func:`forge.core.claim.read_task_state`, exact staleness, ADR 0009/0010). For
each in-progress task (derived status) it computes:

- **zone drift**: the commits on ``origin/<default>`` since the merge-base with
  ``origin/task/<n>`` that touch the task's zones, merges excluded
  (:func:`forge.core.report.zone_drift`, the drift rule of ADR 0010);
- **stale lease**: whether the claim's last activity is older than ``lease_hours``.

Forge keeps at most one comment per issue, starting with :data:`RADAR_MARKER`. It is created
when there is something to report, updated only when its content changes and deleted when
nothing is left to report (including on open tasks that are no longer in progress).
Duplicate radar comments are deleted, keeping the oldest. A second run with no change makes
no GitHub write. The comment never mentions anyone: logins are in code spans.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from forge.core.claim import TaskView, read_task_state
from forge.core.config import ForgeConfig
from forge.core.errors import ExitCode
from forge.core.git import Git
from forge.core.github import Comment, GitHubClient, GitHubError
from forge.core.lease import format_time, stale_at
from forge.core.output import Result
from forge.core.refresh import check_full_history, run_refresh
from forge.core.report import MAX_DRIFT_COMMITS, DriftCommit, zone_drift
from forge.core.tasks import Status
from forge.core.zones import ZONES_PATH, ZonesConfig

RADAR_MARKER = "<!-- forge:radar -->"
"""First line of every radar comment; Forge owns the comments that start with it."""

Action = Literal["created", "updated", "deleted", "unchanged", "none"]


class RadarContext(Protocol):
    """What :func:`run_radar` needs. ``zones`` is ``None`` when ``zones.yml`` did not load."""

    @property
    def config(self) -> ForgeConfig:
        """The project configuration."""
        ...

    @property
    def git(self) -> Git:
        """Git access to the repository."""
        ...

    @property
    def github(self) -> GitHubClient:
        """GitHub access to ``config.github.repo``."""
        ...

    @property
    def zones(self) -> ZonesConfig | None:
        """The parsed ``.forge/zones.yml``, or ``None`` when it could not be loaded."""
        ...

    @property
    def clock(self) -> Callable[[], datetime]:
        """Returns the current time (timezone-aware)."""
        ...


@dataclass(frozen=True)
class StaleLease:
    """A stale claim: owner, agent, last activity and when it became stale."""

    owner: str
    agent: str | None
    last_activity: datetime
    stale_at: datetime

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "owner": self.owner,
            "agent": self.agent,
            "last_activity": format_time(self.last_activity),
            "stale_at": format_time(self.stale_at),
        }


@dataclass(frozen=True)
class RadarFinding:
    """What the radar reports on one in-progress task (``drift`` newest first)."""

    issue: int
    task_id: str
    default_branch: str
    drift: tuple[DriftCommit, ...] = ()
    lease: StaleLease | None = None

    @property
    def empty(self) -> bool:
        """True when there is nothing to report."""
        return not self.drift and self.lease is None

    def body(self) -> str | None:
        """The radar comment body, or ``None`` when there is nothing to report."""
        if self.empty:
            return None
        return render_radar(self)


@dataclass(frozen=True)
class RadarEntry:
    """The radar outcome on one issue: finding (if in progress) and comment action."""

    issue: int
    task_id: str
    in_progress: bool
    finding: RadarFinding | None
    action: Action
    applied: bool = True

    def to_data(self) -> dict[str, Any]:
        """JSON form: ``{"issue", "id", "in_progress", "drift", "lease", "comment", "applied"}``."""
        finding = self.finding
        drift = finding.drift if finding is not None else ()
        lease = finding.lease if finding is not None else None
        return {
            "issue": self.issue,
            "id": self.task_id,
            "in_progress": self.in_progress,
            "drift": {
                "total": len(drift),
                "commits": [c.to_data() for c in drift[:MAX_DRIFT_COMMITS]],
            },
            "lease": None if lease is None else lease.to_data(),
            "comment": self.action,
            "applied": self.applied,
        }


@dataclass
class RadarOutcome:
    """Result of :func:`run_radar`: one entry per issue reported, warnings and failures."""

    entries: list[RadarEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    writes: int = 0

    def to_data(self) -> dict[str, Any]:
        """JSON form: ``{"tasks": [...], "counts": {...}}``."""
        counts: dict[str, int] = {
            "in_progress": sum(1 for e in self.entries if e.in_progress),
            "drift": sum(1 for e in self.entries if e.finding is not None and e.finding.drift),
            "stale": sum(1 for e in self.entries if e.finding is not None and e.finding.lease),
        }
        for action in ("created", "updated", "deleted", "unchanged"):
            counts[action] = sum(1 for e in self.entries if e.action == action and e.applied)
        counts["failed"] = sum(1 for e in self.entries if not e.applied)
        return {"tasks": [e.to_data() for e in self.entries], "counts": counts}


# --------------------------------------------------------------------------- rendering


def code_span(text: str) -> str:
    """Render ``text`` as a Markdown code span, whatever backticks it contains.

    Inside a code span GitHub creates no ``@`` mention and no ``#`` reference.
    """
    text = " ".join(text.splitlines())
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") or not text.strip() else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def render_radar(finding: RadarFinding) -> str:
    """Render the radar comment of ``finding`` (deterministic: no current time in it)."""
    lines = [RADAR_MARKER, f"**Forge radar** for {code_span(finding.task_id)}"]
    if finding.drift:
        total = len(finding.drift)
        zones = sorted({z for c in finding.drift for z in c.zones})
        lines += [
            "",
            f"**Sync recommended:** {_plural(total, 'commit')} on "
            f"{code_span(finding.default_branch)} touch this task's zones "
            f"({', '.join(code_span(z) for z in zones)}). Run `forge sync`.",
            "",
        ]
        for commit in finding.drift[:MAX_DRIFT_COMMITS]:
            lines.append(
                f"- {code_span(commit.sha[:7])} {code_span(commit.subject)} by "
                f"{code_span(commit.author)} ({', '.join(code_span(z) for z in commit.zones)})"
            )
        if total > MAX_DRIFT_COMMITS:
            lines.append(f"- ... and {_plural(total - MAX_DRIFT_COMMITS, 'more commit')}")
    if finding.lease is not None:
        lease = finding.lease
        agent = "" if lease.agent is None else f" (agent {code_span(lease.agent)})"
        lines += [
            "",
            f"**Stale lease:** the claim by {code_span(lease.owner)}{agent} has had no "
            f"activity since {format_time(lease.last_activity)}; its lease expired at "
            f"{format_time(lease.stale_at)}. Reclaimable with "
            f"{code_span(f'forge reclaim {finding.issue}')}.",
        ]
    return "\n".join(lines) + "\n"


def is_radar_comment(body: str) -> bool:
    """True when ``body`` is a radar comment (starts with :data:`RADAR_MARKER`)."""
    return body.lstrip().startswith(RADAR_MARKER)


def _same(a: str, b: str) -> bool:
    return a.replace("\r\n", "\n").strip() == b.replace("\r\n", "\n").strip()


# --------------------------------------------------------------------------- core


def _finding(
    ctx: RadarContext, view: TaskView, zones: ZonesConfig, default_sha: str, warnings: list[str]
) -> RadarFinding:
    config = ctx.config
    for name in view.record.meta.zones:
        if name not in zones.zones:
            warnings.append(
                f"#{view.issue} ({view.task_id}): zone {name!r} is not defined in "
                f"{ZONES_PATH.as_posix()}; ignored for drift"
            )
    drift: tuple[DriftCommit, ...] = ()
    if view.branch is not None:
        drift = tuple(
            zone_drift(ctx.git, zones, view.record.meta.zones, view.branch.tip, default_sha)
        )
    lease: StaleLease | None = None
    if view.claim is not None and view.stale:
        activity = view.activity or view.claim.tip_date
        lease = StaleLease(
            owner=view.claim.owner,
            agent=view.claim.agent,
            last_activity=activity,
            stale_at=stale_at(activity, config.lease_hours),
        )
    return RadarFinding(
        issue=view.issue,
        task_id=view.task_id,
        default_branch=config.project.default_branch,
        drift=drift,
        lease=lease,
    )


def _reconcile(
    github: GitHubClient, issue: int, comments: Sequence[Comment], desired: str | None
) -> tuple[Action, int]:
    """Bring the radar comments of ``issue`` to ``desired``; return (action, writes)."""
    radar = sorted((c for c in comments if is_radar_comment(c.body)), key=lambda c: c.id)
    writes = 0
    action: Action
    if desired is None:
        for comment in radar:
            github.delete_comment(comment.id)
            writes += 1
        return ("deleted" if radar else "none"), writes
    if not radar:
        github.create_comment(issue, desired)
        return "created", 1
    keep, extra = radar[0], radar[1:]
    if _same(keep.body, desired):
        action = "unchanged"
    else:
        github.update_comment(keep.id, desired)
        writes += 1
        action = "updated"
    for comment in extra:
        github.delete_comment(comment.id)
        writes += 1
    if extra and action == "unchanged":
        action = "updated"
    return action, writes


def run_radar(ctx: RadarContext) -> RadarOutcome:
    """Read one task snapshot and reconcile the radar comment of every open task.

    Read-only on git (fetch only). Without zones (``ctx.zones is None``) nothing is written:
    drift cannot be computed and existing comments are left as they are, with a warning.
    A :class:`GitHubError` on one issue is recorded in ``failures``; the others proceed.
    """
    outcome = RadarOutcome()
    if ctx.zones is None:
        outcome.warnings.append(
            f"no zones loaded from {ZONES_PATH.as_posix()}; radar skipped, comments unchanged"
        )
        return outcome
    zones = ctx.zones
    check_full_history(ctx.git, "the radar")
    state = read_task_state(
        ctx.git, ctx.github, ctx.config, now=ctx.clock, warnings=outcome.warnings, exact=True
    )
    default_sha = state.snapshot.default_sha
    for number in sorted(state.snapshot.views):
        view = state.snapshot.views[number]
        in_progress = view.status is Status.IN_PROGRESS
        finding = _finding(ctx, view, zones, default_sha, outcome.warnings) if in_progress else None
        desired = None if finding is None else finding.body()
        try:
            comments = state.leases.comments(number)
            action, writes = _reconcile(ctx.github, number, comments, desired)
        except GitHubError as exc:
            outcome.failures.append(
                f"issue #{number} ({view.task_id}): radar comment update failed: "
                f"{exc.message}; re-run forge refresh --radar"
            )
            outcome.entries.append(
                RadarEntry(number, view.task_id, in_progress, finding, "none", applied=False)
            )
            continue
        outcome.writes += writes
        if in_progress or action != "none":
            outcome.entries.append(RadarEntry(number, view.task_id, in_progress, finding, action))
    return outcome


def run_refresh_radar(ctx: RadarContext) -> Result:
    """``forge refresh --radar``: :func:`run_refresh`, then :func:`run_radar`.

    ``data`` is the refresh data plus ``"radar"`` (:meth:`RadarOutcome.to_data`). Radar
    warnings and failures become warning messages; the exit code is 1 when any label or
    comment update failed.
    """
    result = run_refresh(ctx)
    outcome = run_radar(ctx)
    result.data["radar"] = outcome.to_data()
    for text in [*outcome.warnings, *outcome.failures]:
        result.warn(text)
    counts = result.data["radar"]["counts"]
    summary = (
        f"radar: {_plural(counts['in_progress'], 'task')} in progress, "
        f"{counts['drift']} with drift, {counts['stale']} stale; comments "
        f"{counts['created']} created, {counts['updated']} updated, "
        f"{counts['deleted']} deleted"
    )
    if outcome.failures:
        result.code = ExitCode.ERROR
        summary += f", {counts['failed']} failed"
    result.info(summary)
    return result
