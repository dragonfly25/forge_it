"""`forge task show [X]`: print a task's context (spec §8.1). Read-only."""

from __future__ import annotations

from typing import Annotated

import typer

from forge.commands import _claims
from forge.commands._context import load_context
from forge.commands._output import get_options, run_command
from forge.core.context import TaskContext, render_context
from forge.core.output import Result

task_app = typer.Typer(help="Inspect tasks.", no_args_is_help=True)


@task_app.command("show")
def show(
    ctx: typer.Context,
    task: Annotated[
        str | None,
        typer.Argument(help="M1-T07, #42 or 42 (default: the current task)."),
    ] = None,
) -> None:
    """Print the task context: metadata, body, criteria, zones, dependencies, lease, ADRs."""
    opts = get_options(ctx)
    holder: list[TaskContext] = []

    def run() -> Result:
        app = load_context(opts)
        notes: list[str] = []
        ref: str | int
        if task is None:
            location = _claims.require_current_task(app)
            notes.extend(location.notes)
            ref = location.issue
        else:
            ref = task
        zones = _claims.try_load_zones(app, notes)
        context = _claims.load_task_context(app, ref, zones=zones, now=_claims.clock())
        holder.append(context)
        result = Result(data=context.to_data())
        for note in notes:
            result.info(note)
        for warning in context.warnings:
            result.warn(warning)
        return result

    run_command(opts, run, lambda _result: render_context(holder[0]) if holder else None)
