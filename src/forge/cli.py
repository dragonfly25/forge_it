"""Forge command-line entry point (Typer app ``app``)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from forge import __version__
from forge.commands import heartbeat as heartbeat_cmd
from forge.commands import next as next_cmd
from forge.commands import reclaim as reclaim_cmd
from forge.commands import refresh as refresh_cmd
from forge.commands import release_claim as release_cmd
from forge.commands import status as status_cmd
from forge.commands._output import GlobalOptions, run_command
from forge.commands.task import task_app
from forge.core.output import Result

app = typer.Typer(
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
