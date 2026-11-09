"""`forge arch validate <milestone> [--force]` (ADR 0019) and `forge arch split <milestone>`
(ADR 0020). Offline: no GitHub; ``split`` runs one ``git check-ignore``.

The validation itself is :func:`forge.core.architecture.validate_architecture` and the
grouping :func:`forge.core.architecture.split_architecture`; this module loads the inputs
(config, zones, profile), applies the ``models.min_tier_for.architecture`` check (validate
only) and renders the result.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.text import Text

from forge.commands import _claims
from forge.commands._context import AppContext, find_repo_root, load_context
from forge.commands._output import GlobalOptions, get_options, run_command
from forge.commands.plan import in_ci, plain_console
from forge.core.adapters import is_ignored
from forge.core.architecture import (
    ARCH_WRITER,
    ARCHITECTURE_ROLE,
    split_architecture,
    validate_architecture,
    write_arch_split,
)
from forge.core.config import CONFIG_PATH
from forge.core.errors import ExitCode, PreconditionError
from forge.core.models import check_min_tier
from forge.core.output import Level, Message, Result
from forge.core.subagents import rendered_subagent_path, subagent_availability
from forge.core.zones import load_zones

arch_app = typer.Typer(
    help="Validate and split milestone architectures (ADR 0019, ADR 0020).", no_args_is_help=True
)


def arch_tier_messages(opts: GlobalOptions, app: AppContext, *, force: bool) -> list[Message]:
    """Check ``models.min_tier_for.architecture`` for the active agent (ADR 0019).

    Raises :class:`~forge.core.errors.PreconditionError` (exit 5) when the agent cannot serve
    the tier and not ``force``. Returns the messages to add to the result (profile warnings
    included). Without a profile, or in GitHub Actions, only an info message is returned.
    """
    notes: list[str] = []
    ci = in_ci()
    agent = None
    if not ci:
        _, agent = _claims.load_optional_profile(opts, notes)
    messages = [Message(Level.WARNING, n) for n in notes]
    messages.extend(
        check_min_tier(
            app.config.models,
            agent,
            ARCHITECTURE_ROLE,
            force=force,
            ci=ci,
            activity="architecture",
            verb="validate it",
        )
    )
    return messages


def validate(
    ctx: typer.Context,
    milestone: Annotated[str, typer.Argument(help="Milestone id, e.g. M1.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Validate even if the agent cannot serve models.min_tier_for.architecture.",
        ),
    ] = False,
) -> None:
    """Validate docs/architecture/<milestone>.md and overview.md (offline)."""
    opts = get_options(ctx)

    def run() -> Result:
        root = find_repo_root(opts)
        if not (root / CONFIG_PATH).is_file():
            raise PreconditionError(
                f"{CONFIG_PATH.as_posix()} not found in {root}",
                hint="run `forge init` to create it",
            )
        app = load_context(opts)
        messages = arch_tier_messages(opts, app, force=force)
        problem = _claims.version_problem(app.config)
        zones = load_zones(app.root)
        validation = validate_architecture(app.root, milestone, zones=zones)
        result = Result(data=validation.to_data())
        if problem is not None:
            result.warn(problem, _claims.VERSION_HINT)
        result.messages.extend(messages)
        if not validation.ok:
            result.code = ExitCode.ERROR
            count = len(validation.errors)
            result.error(
                f"architecture {milestone} has {count} error(s)",
                "fix the errors listed above and run `forge arch validate` again",
            )
        return result

    run_command(opts, run, lambda result: render_validation(result.data))


def split(
    ctx: typer.Context,
    milestone: Annotated[str, typer.Argument(help="Milestone id, e.g. M1.")],
) -> None:
    """Group the components of docs/architecture/<milestone>.md for the writers (offline)."""
    opts = get_options(ctx)

    def run() -> Result:
        root = find_repo_root(opts)
        if not (root / CONFIG_PATH).is_file():
            raise PreconditionError(
                f"{CONFIG_PATH.as_posix()} not found in {root}",
                hint="run `forge init` to create it",
            )
        app = load_context(opts)
        problem = _claims.version_problem(app.config)
        zones = load_zones(app.root)
        outcome = split_architecture(
            app.root,
            milestone,
            zones=zones,
            max_per_agent=app.config.planning.max_components_per_agent,
            is_ignored=lambda rel: is_ignored(app.git, rel),
        )
        if outcome.ok:
            outcome = write_arch_split(app.root, outcome)
        notes: list[str] = []
        _, agent = _claims.load_optional_profile(opts, notes)
        tool = None if agent is None else agent.tool
        path = rendered_subagent_path(tool, ARCH_WRITER)
        rendered = path is not None and (app.root / path).is_file()
        subagent = subagent_availability(ARCH_WRITER, tool=tool, rendered=rendered)
        result = Result(data={**outcome.to_data(), "subagent": subagent.to_data()})
        if problem is not None:
            result.warn(problem, _claims.VERSION_HINT)
        for note in notes:
            result.warn(note)
        if outcome.missing:
            result.code = ExitCode.PRECONDITION
            result.error(
                f"{outcome.file} not found",
                f"write the frontmatter first (`/forge-architect {milestone}`)",
            )
        elif outcome.precondition_failed:
            result.code = ExitCode.PRECONDITION
            first = outcome.errors[0]
            result.error(f"{first.message} ({first.file})", first.hint)
        elif not outcome.ok:
            result.code = ExitCode.ERROR
            result.error(
                f"architecture {milestone} has {len(outcome.errors)} error(s); not split",
                "fix the errors listed above and run `forge arch split` again",
            )
        return result

    run_command(opts, run, lambda result: render_split(result.data))


arch_app.command(
    "split", help="Group a milestone's components for parallel writers (offline, ADR 0020)."
)(split)


# --------------------------------------------------------------------------- rendering


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


def _ids(console: Console, title: str, ids: Sequence[str]) -> None:
    console.print(Text(title, style="bold"))
    if ids:
        console.print(Text(f"  {', '.join(ids)}"))
    else:
        console.print(Text("  none", style="dim"))


def render_validation(data: dict[str, Any], *, width: int | None = None) -> str:
    """Human view of the ``forge arch validate`` data.

    Ids, messages and paths come from users, so nothing is read as rich markup.
    """
    buffer = io.StringIO()
    console = plain_console(buffer, width)
    counts = data.get("counts", {})
    head = Text(f"Architecture {data['milestone']}", style="bold")
    if data.get("file"):
        head.append(f"  {data['file']}")
    if data.get("status"):
        head.append(f"  status {data['status']}")
    head.append(
        f"  {counts.get('components', 0)} component(s), {counts.get('interfaces', 0)} "
        f"interface(s), {counts.get('errors', 0)} error(s), {counts.get('warnings', 0)} "
        "warning(s)"
    )
    console.print(head)
    if data["errors"]:
        console.print()
        _issues(console, "Errors", data["errors"], "bold red")
    if data["warnings"]:
        console.print()
        _issues(console, "Warnings", data["warnings"], "yellow")

    console.print()
    _ids(console, "Specs", data.get("specs", []))
    _ids(console, "Components", data.get("components", []))
    _ids(console, "Interfaces", data.get("interfaces", []))

    overview = data.get("overview") or {}
    console.print()
    line = Text("Overview", style="bold")
    line.append(f"  {overview.get('path', '')}")
    if not overview.get("present"):
        line.append("  missing", style="yellow")
    elif overview.get("listed"):
        line.append(f"  lists {data['milestone']}")
    else:
        line.append(f"  does not list {data['milestone']}", style="yellow")
    console.print(line)
    return buffer.getvalue()


def render_split(data: dict[str, Any], *, width: int | None = None) -> str:
    """Human view of the ``forge arch split`` data.

    Ids, messages and paths come from users, so nothing is read as rich markup.
    """
    buffer = io.StringIO()
    console = plain_console(buffer, width)
    head = Text(f"Architecture split {data['milestone']}", style="bold")
    if data.get("file"):
        head.append(f"  {data['file']}")
    head.append(f"  {data['agents']} agent(s), at most {data['max_per_agent']} component(s) each")
    console.print(head)
    if data["errors"]:
        console.print()
        _issues(console, "Errors", data["errors"], "bold red")
    if data["warnings"]:
        console.print()
        _issues(console, "Warnings", data["warnings"], "yellow")
    if data["batches"]:
        console.print()
        console.print(Text("Batches", style="bold"))
        for batch in data["batches"]:
            line = Text(f"  {batch['index']}: {', '.join(batch['component_ids'])}")
            if batch["interface_ids"]:
                line.append(f"  interfaces {', '.join(batch['interface_ids'])}")
            line.append(f"  -> {batch['part_path']}", style="dim")
            console.print(line)
        console.print()
        _ids(console, "Unassigned interfaces (main session)", data["unassigned_interfaces"])
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
