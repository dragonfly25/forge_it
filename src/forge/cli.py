"""Forge command-line entry point (Typer app ``app``)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from forge import __version__
from forge.commands import escalate as escalate_cmd
from forge.commands import handoff as handoff_cmd
from forge.commands import heartbeat as heartbeat_cmd
from forge.commands import next as next_cmd
from forge.commands import propose as propose_cmd
from forge.commands import reclaim as reclaim_cmd
from forge.commands import refresh as refresh_cmd
from forge.commands import release_claim as release_cmd
from forge.commands import review as review_cmd
from forge.commands import ship as ship_cmd
from forge.commands import status as status_cmd
from forge.commands import sync as sync_cmd
from forge.commands._output import GlobalOptions, run_command
from forge.commands.task import task_app
from forge.core.output import Result


def _usage_error_class(bad_parameter: type[Exception]) -> type[Exception]:
    """Click's ``UsageError``, the direct base class of ``bad_parameter``.

    Typer vendors click and exports only ``BadParameter``. If a Typer release changes that
    hierarchy, fail loudly at import instead of silently letting usage errors exit 2.
    """
    base = bad_parameter.__mro__[1]
    if base.__name__ != "UsageError" or not issubclass(base, Exception):
        raise ImportError(
            f"forge.cli expects {bad_parameter.__qualname__}'s base class to be click's "
            f"UsageError, found {base.__module__}.{base.__qualname__}; this Typer version is "
            "not supported"
        )
    return base


_UsageError: type[Exception] = _usage_error_class(typer.BadParameter)


class ForgeGroup(TyperGroup):
    """Root command group: usage errors exit 1, because exit 2 means "agent action" (§16)."""

    def make_context(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().make_context(*args, **kwargs)
        except _UsageError as exc:
            exc.exit_code = 1  # type: ignore[attr-defined]
            raise

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except _UsageError as exc:
            exc.exit_code = 1  # type: ignore[attr-defined]
            raise


app = typer.Typer(
    cls=ForgeGroup,
    name="forge",
    help="Coordinate several people and AI agents working in parallel on one GitHub repository.",
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", "-V", help="Show the Forge version and exit."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON envelope on stdout (ADR 0004)."),
    ] = False,
    agent: Annotated[
        str | None,
        typer.Option("--agent", envvar="FORGE_AGENT", help="Active agent id from the profile."),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Path to the repository (default: current directory)."),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Print more detail."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Answer yes to confirmations."),
    ] = False,
) -> None:
    """Forge: coordinate people and AI agents on one GitHub repository."""
    opts = GlobalOptions(json=json_output, agent=agent, repo=repo, verbose=verbose, yes=yes)
    ctx.obj = opts
    if version:
        if opts.json:
            run_command(opts, lambda: Result(data={"version": __version__}))
        typer.echo(f"forge {__version__}")
        raise typer.Exit(code=0)
    if ctx.invoked_subcommand is None:
        help_text = ctx.get_help()  # rich help prints itself and returns ""
        if help_text:
            typer.echo(help_text)
        raise typer.Exit(code=0)


app.command("refresh", help="Recompute task status labels and unblock dependents.")(
    refresh_cmd.refresh
)
app.command("next", help="Select and claim a task (spec §8.1).")(next_cmd.next_task)
app.add_typer(task_app, name="task")
app.command("heartbeat", help="Refresh the lease of the current task.")(heartbeat_cmd.heartbeat)
app.command("release-claim", help="Release the current task's claim.")(release_cmd.release_claim)
app.command("reclaim", help="Take over a task whose claim went stale.")(reclaim_cmd.reclaim)
app.command("status", help="Show me, team, queue, blocked, milestone and health (§11.3).")(
    status_cmd.status
)
app.command(
    "sync", help="Rebase the current task on the default branch and resolve conflicts (§10)."
)(sync_cmd.sync)
app.command(
    "escalate", help="Escalate the current task: label blocked:conflict and mention people."
)(escalate_cmd.escalate)
app.command("ship", help="Sync, run checks and open or update the task's pull request (§11.1).")(
    ship_cmd.ship
)
app.command("review", help="Print a pull request's review context, or submit a review (§11.2).")(
    review_cmd.review
)
app.command("handoff", help="Hand the current task off with a note (§8.3).")(handoff_cmd.handoff)
app.command("propose", help="Propose new work as a status:proposed issue.")(propose_cmd.propose)
