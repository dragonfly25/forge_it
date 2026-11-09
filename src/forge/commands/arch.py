"""`forge arch validate <milestone> [--force]` (ADR 0019). Offline: no GitHub, no git.

The validation itself is :func:`forge.core.architecture.validate_architecture`; this module
loads the inputs (config, zones, profile), applies the ``models.min_tier_for.architecture``
check and renders the result.
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
from forge.core.architecture import ARCHITECTURE_ROLE, validate_architecture
from forge.core.config import CONFIG_PATH
from forge.core.errors import ExitCode, PreconditionError
from forge.core.models import check_min_tier
from forge.core.output import Level, Message, Result
from forge.core.zones import load_zones

arch_app = typer.Typer(help="Validate milestone architectures (ADR 0019).", no_args_is_help=True)


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
