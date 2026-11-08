"""`forge task show [X]`: print a task's context (spec §8.1). Read-only."""

from __future__ import annotations

from typing import Annotated

import typer

from forge.commands import _claims
from forge.commands._context import load_context
from forge.commands._output import get_options, run_command
from forge.core.config import DelegationMode
from forge.core.context import TaskContext, render_context
from forge.core.output import Result
from forge.core.workspace import TaskLocation

task_app = typer.Typer(help="Inspect tasks.", no_args_is_help=True)


@task_app.command("show")
def show(
    ctx: typer.Context,
    task: Annotated[
        str | None,
        typer.Argument(help="M1-T07, #42 or 42 (default: the current task)."),
    ] = None,
    delegation: Annotated[
        DelegationMode | None,
        typer.Option(
            "--delegation",
            help=(
                "Delegation mode for this task (saved locally until release or handoff): "
                "off, auto or on."
            ),
        ),
    ] = None,
) -> None:
    """Print the task context: metadata, body, criteria, zones, dependencies, lease, ADRs."""
    opts = get_options(ctx)
    holder: list[TaskContext] = []

    def run() -> Result:
        app = load_context(opts)
        notes: list[str] = []
        ref: str | int
        location: TaskLocation | None
        if task is None:
            location = _claims.require_current_task(app)
            notes.extend(location.notes)
            ref = location.issue
        else:
            ref = task
            location = _claims.try_current_task(app)
        zones = _claims.try_load_zones(app, notes)
        context = _claims.load_task_context(app, ref, zones=zones, now=_claims.clock())
        holder.append(context)
        result = Result()
        for note in notes:
            result.info(note)
        for warning in context.warnings:
            result.warn(warning)
        _claims.show_delegation(app, opts, context, location, delegation, result)
        result.data = context.to_data()
        return result

    run_command(opts, run, lambda _result: render_context(holder[0]) if holder else None)
