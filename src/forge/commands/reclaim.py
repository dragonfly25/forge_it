"""`forge reclaim <n>`: take over a stale claim (spec §8.2, ADR 0009)."""

from __future__ import annotations

from typing import Annotated

import typer

from forge.commands import _claims
from forge.commands._output import get_options, run_command
from forge.core.claim import reclaim_task
from forge.core.output import Result


def reclaim(
    ctx: typer.Context,
    task: Annotated[str, typer.Argument(help="The task to take over: 42, #42 or M1-T07.")],
) -> None:
    """Take over a task whose claim went stale, then prepare its workspace."""
    opts = get_options(ctx)
    rendered = _claims.Rendered()

    def run() -> Result:
        return _claims.claim_command(
            opts, lambda claim_ctx: reclaim_task(claim_ctx, task), rendered
        )

    run_command(opts, run, lambda result: _claims.render_claim(result, rendered))
