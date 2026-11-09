"""`forge plan validate <milestone> [--skeleton] [--force]` (spec §9.4, ADR 0013, ADR 0020),
`forge plan split <milestone>` and `forge plan assemble <milestone>` (ADR 0020).

Offline: no GitHub.

The validation itself is :func:`forge.core.plan.validate_plan`; this module loads the inputs
(config, zones, team, profile), applies the ``models.min_tier_for.plan`` check (validate
only) and renders the result. Split and assemble are :mod:`forge.core.plan_split`.
``forge plan apply`` lives in :mod:`forge.commands.plan_apply`.
"""

from __future__ import annotations

import io
import os
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from forge.commands import _claims
from forge.commands._context import AppContext, find_repo_root, load_context
from forge.commands._output import GlobalOptions, get_options, run_command
from forge.core.adapters import is_ignored
from forge.core.config import CONFIG_PATH
from forge.core.errors import ExitCode, PreconditionError
from forge.core.output import Level, Message, Result
from forge.core.plan import check_plan_tier, validate_plan
from forge.core.plan_split import (
    PLANNER,
    assemble_plan,
    split_plan,
    write_plan_assembly,
    write_plan_split,
)
from forge.core.subagents import rendered_subagent_path, subagent_availability
from forge.core.zones import load_zones

plan_app = typer.Typer(
    help="Validate, split, assemble and apply milestone plans (§9, ADR 0020).",
    no_args_is_help=True,
)


def in_ci(env: Mapping[str, str] | None = None) -> bool:
    """True when running in GitHub Actions (``GITHUB_ACTIONS`` set and non-empty)."""
    source = os.environ if env is None else env
    return bool(source.get("GITHUB_ACTIONS"))


def plan_tier_messages(opts: GlobalOptions, app: AppContext, *, force: bool) -> list[Message]:
    """Check ``models.min_tier_for.plan`` for the active agent (ADR 0013, B4).

    Raises :class:`~forge.core.errors.PreconditionError` (exit 5) when the agent cannot serve
    the tier and not ``force``. Returns the messages to add to the result (profile warnings
    included). Shared by ``plan validate`` and ``plan apply``.
    """
    notes: list[str] = []
    ci = in_ci()
    agent = None
    if not ci:
        _, agent = _claims.load_optional_profile(opts, notes)
    messages = [Message(Level.WARNING, n) for n in notes]
    messages.extend(check_plan_tier(app.config.models, agent, force=force, ci=ci))
    return messages


def validate(
    ctx: typer.Context,
    milestone: Annotated[str, typer.Argument(help="Milestone id, e.g. M1.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Validate even if the agent cannot serve models.min_tier_for.plan."
        ),
    ] = False,
    skeleton: Annotated[
        bool,
        typer.Option(
            "--skeleton",
            help="Validate a plan skeleton: description, done_when and tier may be missing.",
        ),
    ] = False,
) -> None:
    """Validate plans/<milestone>.yml against zones, specs and the roadmap (offline)."""
    opts = get_options(ctx)

    def run() -> Result:
        app = load_context(opts)
        messages = plan_tier_messages(opts, app, force=force)
        problem = _claims.version_problem(app.config)
        zones = load_zones(app.root)
        notes: list[str] = []
        team = _claims.try_load_team(app, notes)
        validation = validate_plan(
            app.root,
            milestone,
            zones=zones,
            team=team,
            planning=app.config.planning,
            skeleton=skeleton,
        )
        result = Result(data=validation.to_data())
        if problem is not None:
            result.warn(problem, _claims.VERSION_HINT)
        result.messages.extend(messages)
        result.messages.extend(validation.messages)
        for note in notes:
            result.warn(note)
        if not validation.ok:
            result.code = ExitCode.ERROR
            count = len(validation.errors)
            result.error(
                f"plan {milestone} has {count} error(s)",
                "fix the errors listed above and run `forge plan validate` again",
            )
        return result

    run_command(opts, run, lambda result: render_validation(result.data))


def _require_config(opts: GlobalOptions) -> AppContext:
    """Load the context; a missing ``.forge/config.yml`` is a precondition (exit 5)."""
    root = find_repo_root(opts)
    if not (root / CONFIG_PATH).is_file():
        raise PreconditionError(
            f"{CONFIG_PATH.as_posix()} not found in {root}",
            hint="run `forge init` to create it",
        )
    return load_context(opts)


def split(
    ctx: typer.Context,
    milestone: Annotated[str, typer.Argument(help="Milestone id, e.g. M1.")],
) -> None:
    """Group the tasks of the plan skeleton plans/<milestone>.yml for the planners (offline)."""
    opts = get_options(ctx)

    def run() -> Result:
        app = _require_config(opts)
        problem = _claims.version_problem(app.config)
        zones = load_zones(app.root)
        notes: list[str] = []
        team = _claims.try_load_team(app, notes)
        outcome = split_plan(
            app.root,
            milestone,
            zones=zones,
            planning=app.config.planning,
            team=team,
            is_ignored=lambda rel: is_ignored(app.git, rel),
        )
        if outcome.ok:
            outcome = write_plan_split(app.root, outcome)
        _, agent = _claims.load_optional_profile(opts, notes)
        tool = None if agent is None else agent.tool
        path = rendered_subagent_path(tool, PLANNER)
        rendered = path is not None and (app.root / path).is_file()
        subagent = subagent_availability(PLANNER, tool=tool, rendered=rendered)
        result = Result(data={**outcome.to_data(), "subagent": subagent.to_data()})
        if problem is not None:
            result.warn(problem, _claims.VERSION_HINT)
        result.messages.extend(outcome.messages)
        for note in notes:
            result.warn(note)
        if outcome.precondition_failed:
            result.code = ExitCode.PRECONDITION
            first = outcome.errors[0]
            result.error(f"{first.message} ({first.file})", first.hint)
        elif not outcome.ok:
            result.code = ExitCode.ERROR
            result.error(
                f"plan {milestone} has {len(outcome.errors)} error(s); not split",
                "fix the errors listed above and run `forge plan split` again",
            )
        return result

    run_command(opts, run, lambda result: render_split(result.data))


def assemble(
    ctx: typer.Context,
    milestone: Annotated[str, typer.Argument(help="Milestone id, e.g. M1.")],
) -> None:
    """Merge the planners' fragments into plans/<milestone>.yml (offline, atomic)."""
    opts = get_options(ctx)

    def run() -> Result:
        app = _require_config(opts)
        problem = _claims.version_problem(app.config)
        zones = load_zones(app.root)
        notes: list[str] = []
        team = _claims.try_load_team(app, notes)
        outcome = assemble_plan(
            app.root, milestone, zones=zones, planning=app.config.planning, team=team
        )
        if outcome.ok:
            outcome = write_plan_assembly(app.root, outcome)
        result = Result(data=outcome.to_data())
        if problem is not None:
            result.warn(problem, _claims.VERSION_HINT)
        result.messages.extend(outcome.messages)
        for note in notes:
            result.warn(note)
        if outcome.precondition_failed:
            result.code = ExitCode.PRECONDITION
            first = outcome.errors[0]
            result.error(f"{first.message} ({first.file})", first.hint)
        elif not outcome.ok:
            result.code = ExitCode.ERROR
            result.error(
                f"plan {milestone} has {len(outcome.errors)} error(s); nothing written",
                "fix the errors listed above and run `forge plan assemble` again",
            )
        return result

    run_command(opts, run, lambda result: render_assemble(result.data))


# --------------------------------------------------------------------------- rendering


def plain_console(buffer: io.StringIO, width: int | None = None) -> Console:
    """A console writing to ``buffer`` that never interprets user text (titles, paths) as
    markup or emoji. Shared by ``plan validate`` and ``plan apply``."""
    probe = Console()
    return Console(
        file=buffer,
        force_terminal=probe.is_terminal,
        no_color=probe.no_color,
        width=width or min(probe.width, 100),
        markup=False,
        emoji=False,
        highlight=False,
        soft_wrap=False,
    )


def _where(issue: dict[str, Any]) -> str:
    parts = [p for p in (issue.get("file"), issue.get("task"), issue.get("field")) if p]
    return " · ".join(str(p) for p in parts)


def _issues(console: Console, title: str, issues: Sequence[dict[str, Any]], style: str) -> None:
    console.print(Text(f"{title} ({len(issues)})", style="bold"))
    for issue in issues:
        line = Text("  ")
        line.append(issue["code"], style=style)
        where = _where(issue)
        if where:
            line.append(f"  {where}")
        console.print(line)
        console.print(Text(f"    {issue['message']}"))
        if issue.get("hint"):
            console.print(Text(f"    fix: {issue['hint']}", style="dim"))


def _architecture_line(arch: dict[str, Any] | None) -> Text | None:
    """The one "Architecture" line of the human view (ADR 0019)."""
    if not arch or not arch.get("path"):
        return None
    line = Text("Architecture", style="bold")
    mode = arch.get("mode", "optional")
    if not arch.get("checked"):
        line.append(f"  not checked (planning.architecture: {mode}, no {arch['path']})")
        return line
    if not arch.get("present"):
        line.append(f"  {arch['path']} missing ({mode})")
        return line
    interfaces: dict[str, str | None] = arch.get("interfaces") or {}
    done = sum(1 for owner in interfaces.values() if owner)
    line.append(
        f"  {arch['path']}  {arch.get('status') or '?'}, "
        f"{done}/{len(interfaces)} interface(s) implemented ({mode})"
    )
    return line


def render_validation(data: dict[str, Any], *, width: int | None = None) -> str:
    """Human view of the ``forge plan validate`` data (also used by ``plan apply``).

    Titles, messages and paths come from users, so nothing is read as rich markup.
    """
    buffer = io.StringIO()
    console = plain_console(buffer, width)
    counts = data.get("counts", {})
    head = Text(f"Plan {data['milestone']}", style="bold")
    if data.get("file"):
        head.append(f"  {data['file']}")
    if data.get("skeleton"):
        head.append("  (skeleton)")
    head.append(
        f"  {counts.get('tasks', 0)} task(s), {counts.get('errors', 0)} error(s), "
        f"{counts.get('warnings', 0)} warning(s)"
    )
    console.print(head)
    architecture = _architecture_line(data.get("architecture"))
    if architecture is not None:
        console.print(architecture)
    if data["errors"]:
        console.print()
        _issues(console, "Errors", data["errors"], "bold red")
    if data["warnings"]:
        console.print()
        _issues(console, "Warnings", data["warnings"], "yellow")

    console.print()
    console.print(Text("Waves", style="bold"))
    if data["waves"]:
        for number, wave in enumerate(data["waves"], start=1):
            console.print(Text(f"  {number}: {', '.join(wave)}"))
    else:
        console.print(Text("  none", style="dim"))

    console.print()
    console.print(Text("Coverage", style="bold"))
    coverage: dict[str, list[str]] = data["coverage"]
    if coverage:
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("  AC", no_wrap=True)
        table.add_column("tasks")
        for ac, ids in coverage.items():
            cell = Text(", ".join(ids)) if ids else Text("uncovered", style="yellow")
            table.add_row(Text(f"  {ac}"), cell)
        console.print(table)
    else:
        console.print(Text("  none", style="dim"))
    return buffer.getvalue()


def _ids(console: Console, title: str, ids: Sequence[str]) -> None:
    console.print(Text(title, style="bold"))
    if ids:
        console.print(Text(f"  {', '.join(ids)}"))
    else:
        console.print(Text("  none", style="dim"))


def _batches(console: Console, batches: Sequence[dict[str, Any]]) -> None:
    console.print(Text("Batches", style="bold"))
    for batch in batches:
        line = Text(f"  {batch['index']}: {', '.join(batch['task_ids'])}")
        line.append(f"  -> {batch['part_path']}", style="dim")
        console.print(line)


def render_split(data: dict[str, Any], *, width: int | None = None) -> str:
    """Human view of the ``forge plan split`` data.

    Ids, messages and paths come from users, so nothing is read as rich markup.
    """
    buffer = io.StringIO()
    console = plain_console(buffer, width)
    head = Text(f"Plan split {data['milestone']}", style="bold")
    if data.get("file"):
        head.append(f"  {data['file']}")
    head.append(f"  {data['agents']} agent(s), at most {data['max_per_agent']} task(s) each")
    console.print(head)
    if data["errors"]:
        console.print()
        _issues(console, "Errors", data["errors"], "bold red")
    if data["warnings"]:
        console.print()
        _issues(console, "Warnings", data["warnings"], "yellow")
    if data["batches"]:
        console.print()
        _batches(console, data["batches"])
        console.print(Text(f"  written to {data['batches_path']}", style="dim"))
    if data.get("removed_parts"):
        console.print()
        _ids(console, "Removed stale fragments", data["removed_parts"])
    subagent = data.get("subagent")
    if subagent:
        console.print()
        line = Text("Subagent", style="bold")
        state = "available" if subagent["available"] else "not available"
        line.append(f"  {subagent['name']} {state}: {subagent['reason']}")
        console.print(line)
    return buffer.getvalue()


def render_assemble(data: dict[str, Any], *, width: int | None = None) -> str:
    """Human view of the ``forge plan assemble`` data.

    Ids, messages and paths come from users, so nothing is read as rich markup.
    """
    buffer = io.StringIO()
    console = plain_console(buffer, width)
    counts = data.get("counts", {})
    head = Text(f"Plan assemble {data['milestone']}", style="bold")
    if data.get("file"):
        head.append(f"  {data['file']}")
    head.append(
        f"  {counts.get('batches', 0)} batch(es), {counts.get('tasks', 0)} task(s), "
        f"{counts.get('errors', 0)} error(s), {counts.get('warnings', 0)} warning(s)"
    )
    console.print(head)
    if data["errors"]:
        console.print()
        _issues(console, "Errors", data["errors"], "bold red")
    if data["warnings"]:
        console.print()
        _issues(console, "Warnings", data["warnings"], "yellow")
    console.print()
    if data["written"]:
        state = "written" if data["changed"] else "written (unchanged)"
    else:
        state = "not written"
    console.print(Text(f"Plan {state}", style="bold"))
    return buffer.getvalue()
