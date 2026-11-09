"""`forge plan validate <milestone> [--force]` (spec §9.4, ADR 0013). Offline: no GitHub.

The validation itself is :func:`forge.core.plan.validate_plan`; this module loads the inputs
(config, zones, team, profile), applies the ``models.min_tier_for.plan`` check and renders
the result. ``forge plan apply`` lives in :mod:`forge.commands.plan_apply`.
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
from forge.commands._context import AppContext, load_context
from forge.commands._output import GlobalOptions, get_options, run_command
from forge.core.errors import ExitCode
from forge.core.output import Level, Message, Result
from forge.core.plan import check_plan_tier, validate_plan
from forge.core.zones import load_zones

plan_app = typer.Typer(help="Validate and apply milestone plans (§9).", no_args_is_help=True)


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
            app.root, milestone, zones=zones, team=team, planning=app.config.planning
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
