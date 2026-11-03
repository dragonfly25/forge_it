"""Shared plumbing for the claim commands (`next`, `reclaim`, `heartbeat`, `release-claim`,
`task show`): profile loading, the claim context, the post-claim local steps and the task
context.

The clock and the sleep used by the core are the module attributes :data:`clock` and
:data:`sleep`. Tests replace them with ``monkeypatch.setattr(_claims, "clock", ...)``; they
are looked up at call time, like ``_context.github_factory``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from forge import __version__
from forge.commands._context import AppContext, load_context
from forge.commands._output import GlobalOptions
from forge.core.claim import ClaimContext, Claimed, NextOutcome
from forge.core.config import ForgeConfig, check_cli_version
from forge.core.context import TaskContext, gather_task_context, render_context
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.models import check_profile
from forge.core.output import Level, Result
from forge.core.presets import load_presets
from forge.core.profile import AgentProfile, Profile, load_profile, profile_path, select_agent
from forge.core.team import TEAM_PATH, Team, load_team
from forge.core.workspace import (
    CurrentTask,
    TaskLocation,
    Workspace,
    find_current_task,
    prepare_workspace,
    write_current,
)
from forge.core.zones import ZonesConfig, load_zones


def _utc_now() -> datetime:
    return datetime.now(UTC)


clock: Callable[[], datetime] = _utc_now
"""Clock handed to the core. Tests replace it with ``monkeypatch.setattr``."""

sleep: Callable[[float], None] = time.sleep
"""Sleep used by the post-claim verification wait. Tests replace it."""

PROFILE_HINT = "create ~/.config/forge/profile.yml (forge setup arrives later)"
NO_CURRENT_HINT = (
    "run this inside the worktree of a claimed task (or on its task/<n> branch), "
    "or claim one with `forge next`"
)


# --------------------------------------------------------------------------- profile


def load_profile_and_agent(
    opts: GlobalOptions, recorded_agent: str | None = None
) -> tuple[Profile, AgentProfile]:
    """Load the machine profile and the active agent; exit 5 when either is missing/invalid.

    The agent is ``--agent`` when given, else ``recorded_agent`` (the agent that made the
    current task's claim, from ``current.json``), else the profile's ``default_agent``.
    """
    path = profile_path()
    if not path.is_file():
        raise PreconditionError(f"no Forge profile at {path}", hint=PROFILE_HINT)
    try:
        profile = load_profile(path)
        agent = select_agent(profile, opts.agent or recorded_agent)
    except ForgeError as exc:
        raise PreconditionError(exc.message, hint=exc.hint or PROFILE_HINT) from exc
    return profile, agent


def load_optional_profile(
    opts: GlobalOptions, warnings: list[str]
) -> tuple[Profile | None, AgentProfile | None]:
    """Load the profile and the active agent when a profile exists (read-only commands).

    A missing or invalid profile yields ``(None, None)``; an invalid one adds a warning, and
    so does an ``--agent``/``FORGE_AGENT`` that cannot be used without a profile. An unknown
    agent id in a valid profile is still an error (exit 5), as in the other commands.
    """
    path = profile_path()
    if not path.is_file():
        if opts.agent:
            warnings.append(f"agent {opts.agent!r} ignored: no Forge profile at {path}")
        return None, None
    try:
        profile = load_profile(path)
    except ForgeError as exc:
        warnings.append(f"profile not used: {exc.message}")
        return None, None
    try:
        agent = select_agent(profile, opts.agent)
    except ForgeError as exc:
        raise PreconditionError(exc.message, hint=exc.hint or PROFILE_HINT) from exc
    return profile, agent


def profile_messages(
    result: Result, profile: Profile, agent: AgentProfile, app: AppContext
) -> None:
    """Append the active agent's violations of ``config.models`` (never blocking)."""
    for violation in check_profile(profile, app.config.models, load_presets()):
        if violation.agent_id != agent.id:
            continue
        text = f"profile: agent {violation.agent_id}: {violation.message}"
        if violation.level is Level.INFO:
            result.info(text, violation.fix)
        else:
            result.warn(text, violation.fix)


def claim_context(
    app: AppContext, profile: Profile, agent: AgentProfile, zones: ZonesConfig | None = None
) -> ClaimContext:
    """Build the core :class:`ClaimContext`, with this module's current clock and sleep."""
    return ClaimContext(
        config=app.config,
        git=app.git,
        github=app.github,
        zones=zones if zones is not None else ZonesConfig(zones={}),
        agent=agent,
        profile_login=profile.github,
        clock=lambda: clock(),
        sleep=lambda seconds: sleep(seconds),
    )


# --------------------------------------------------------------------------- project files


def version_problem(config: ForgeConfig, cli_version: str = __version__) -> str | None:
    """Why this CLI does not satisfy ``forge_version`` (ADR 0003), or ``None`` when it does."""
    try:
        ok = check_cli_version(config.forge_version, cli_version)
    except ValueError:
        ok = False
    if ok:
        return None
    return (
        f"forge {cli_version} does not satisfy forge_version "
        f"{str(config.forge_version)!r} in .forge/config.yml"
    )


VERSION_HINT = "install a Forge version matching forge_version, or ask the lead to update it"


def try_load_team(app: AppContext, warnings: list[str]) -> Team | None:
    """Load ``.forge/team.yml`` when it exists; an invalid file becomes a warning."""
    if not (app.root / TEAM_PATH).is_file():
        return None
    try:
        return load_team(app.root)
    except ForgeError as exc:
        warnings.append(f"team.yml not used: {exc.message}")
        return None


# --------------------------------------------------------------------------- current task


def require_current_task(app: AppContext) -> TaskLocation:
    """Return the current task (``current.json`` or a ``task/<n>`` branch); exit 5 if none."""
    location = find_current_task(app.git)
    if location is None:
        raise PreconditionError(f"no current task in {app.root}", hint=NO_CURRENT_HINT)
    return location


def recorded_agent(location: TaskLocation) -> str | None:
    """The agent recorded in ``current.json`` for the current task (``None`` if unknown)."""
    return None if location.current is None else location.current.agent


# --------------------------------------------------------------------------- task context


def load_task_context(
    app: AppContext,
    ref: str | int,
    *,
    zones: ZonesConfig | None,
    now: datetime,
    remote: str = "origin",
) -> TaskContext:
    """Build the task context for ``ref`` from the app's GitHub, git and config (read-only)."""
    return gather_task_context(
        app.github,
        app.git,
        app.config,
        ref,
        repo_root=app.root,
        zones=zones,
        now=now,
        remote=remote,
    )


def try_load_zones(app: AppContext, warnings: list[str]) -> ZonesConfig | None:
    """Load ``.forge/zones.yml`` for display; a problem becomes a warning, never an error."""
    try:
        return load_zones(app.root)
    except ForgeError as exc:
        warnings.append(f"zones not shown: {exc.message}")
        return None


# --------------------------------------------------------------------------- after a claim


@dataclass
class Rendered:
    """Holds what the human renderer needs beyond the JSON data."""

    context: TaskContext | None = None


def _local_tip(app: AppContext, claimed: Claimed, remote: str) -> str:
    """The tip to check out: the fetched remote tip when it contains the claim, else the claim."""
    tip = app.git.rev_parse(f"refs/remotes/{remote}/{claimed.branch}")
    if tip is not None and app.git.is_ancestor(claimed.claim, tip):
        return tip
    return claimed.claim


def finish_claim(
    app: AppContext,
    ctx: ClaimContext,
    profile: Profile,
    outcome: NextOutcome,
    result: Result,
    rendered: Rendered,
    *,
    use_worktree: bool,
) -> None:
    """Post-claim local steps: workspace, ``current.json``, then the task context in ``data``."""
    claimed = outcome.claimed
    assert claimed is not None
    workspace: Workspace = prepare_workspace(
        app.git,
        repo_root=app.root,
        issue_number=claimed.issue,
        claim_tip=_local_tip(app, claimed, ctx.remote),
        remote=ctx.remote,
        use_worktree=use_worktree and app.config.worktrees.enabled,
        worktrees_dir=profile.worktrees_dir,
    )
    current = CurrentTask(
        task=claimed.task_id,
        issue=claimed.issue,
        branch=claimed.branch,
        base=claimed.base,
        claim=claimed.claim,
        agent=claimed.agent,
        claimed_at=claimed.claimed_at,
        worktree=None if workspace.in_place else workspace.path,
    )
    current_file = write_current(workspace.path, current)
    context = load_task_context(
        app, claimed.issue, zones=ctx.zones, now=ctx.now(), remote=ctx.remote
    )
    rendered.context = context
    result.data["workspace"] = workspace.to_data()
    result.data["current"] = {"path": str(current_file), **current.to_data()}
    result.data["context"] = context.to_data()
    for warning in context.warnings:
        result.warn(warning)


def outcome_result(outcome: NextOutcome) -> Result:
    """Turn a :class:`NextOutcome` into a result: its code, data and messages."""
    result = Result(code=outcome.code, data=outcome.to_data())
    for text in outcome.info:
        result.info(text)
    for text in outcome.warnings:
        result.warn(text)
    if outcome.code == ExitCode.NOTHING_AVAILABLE and outcome.reason:
        counts = ", ".join(f"{k}: {v}" for k, v in outcome.excluded_counts().items())
        result.warn(outcome.reason + (f" (excluded: {counts})" if counts else ""))
    return result


def claim_command(
    opts: GlobalOptions,
    run: Callable[[ClaimContext], NextOutcome],
    rendered: Rendered,
    *,
    use_worktree: bool = True,
) -> Result:
    """Shared body of ``forge next`` and ``forge reclaim``."""
    app = load_context(opts)
    profile, agent = load_profile_and_agent(opts)
    ctx = claim_context(app, profile, agent, load_zones(app.root))
    outcome = run(ctx)
    result = outcome_result(outcome)
    profile_messages(result, profile, agent, app)
    claimed = outcome.claimed
    if claimed is not None and not outcome.dry_run:
        try:
            finish_claim(app, ctx, profile, outcome, result, rendered, use_worktree=use_worktree)
        except ForgeError as exc:
            # The claim is held on GitHub: keep its summary in ``data`` so --json callers
            # know which task they hold, and say how to finish the local steps.
            result.code = exc.code
            result.error(exc.message, exc.hint)
            result.warn(
                f"you hold the claim on #{claimed.issue} ({claimed.task_id}); re-run "
                f"`forge next --task {claimed.issue}` to complete the local steps"
            )
    return result


# --------------------------------------------------------------------------- rendering


def render_claim(result: Result, rendered: Rendered) -> str:
    """Human view of ``forge next`` / ``forge reclaim``."""
    data = result.data
    lines: list[str] = []
    claimed = data.get("claimed")
    if data.get("dry_run"):
        own = data.get("resume_own") or []
        if own:
            lines.append(
                "Would first complete your interrupted claim(s): " + ", ".join(f"#{n}" for n in own)
            )
        candidates = data.get("candidates") or []
        lines.append(
            f"Candidates ({len(candidates)}), in claim order:" if candidates else "No candidates."
        )
        for c in candidates:
            extra = " reclaim" if c["reclaim"] else (" resume" if c["resume"] else "")
            lines.append(
                f"  #{c['issue']} {c['id']} [{c['status']}] wave {c['wave']} size {c['size']} "
                f"tier {c['tier']} zones {', '.join(c['zones']) or '-'}{extra}"
            )
    elif claimed is not None:
        how = (
            "Reclaimed" if claimed["reclaimed"] else "Resumed" if claimed["resumed"] else "Claimed"
        )
        lines.append(
            f"{how} #{claimed['issue']} {claimed['id']} on {claimed['branch']} as "
            f"{claimed['login']} (agent {claimed['agent']})"
        )
        workspace = data.get("workspace")
        if workspace:
            where = "in place" if workspace["in_place"] else "worktree"
            lines.append(f"Workspace ({where}): {workspace['path']}")
        if rendered.context is not None:
            lines += ["", render_context(rendered.context)]
    else:
        lines.append(data.get("reason") or "nothing claimable")
    excluded = data.get("excluded") or {}
    if excluded and claimed is None:
        lines.append("Excluded: " + ", ".join(f"{k} {v}" for k, v in excluded.items()))
    return "\n".join(lines)
