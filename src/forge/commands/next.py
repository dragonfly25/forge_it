"""`forge next`: select and claim a task, then prepare its workspace (spec §8.1, ADR 0009)."""

from __future__ import annotations

from typing import Annotated

import typer

from forge.commands import _claims
from forge.commands._output import get_options, run_command
from forge.core.claim import NextOptions, claim_next
from forge.core.output import Result


def next_task(
    ctx: typer.Context,
    task: Annotated[
        str | None,
        typer.Option("--task", help="Claim this task only: M1-T07, #42 or 42."),
    ] = None,
    reclaim: Annotated[
        bool, typer.Option("--reclaim", help="Also take over stale claims.")
    ] = False,
    any_tier: Annotated[
        bool, typer.Option("--any-tier", help="Include tasks whose tier the agent cannot serve.")
    ] = False,
    no_worktree: Annotated[
        bool,
        typer.Option("--no-worktree", help="Check the branch out here instead of in a worktree."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the ordered candidates; push nothing.")
    ] = False,
) -> None:
    """Select and claim the next task, create its worktree and print its context."""
    opts = get_options(ctx)
    options = NextOptions(task=task, reclaim=reclaim, any_tier=any_tier, dry_run=dry_run)
    rendered = _claims.Rendered()

    def run() -> Result:
        return _claims.claim_command(
            opts,
            lambda claim_ctx: claim_next(claim_ctx, options),
            rendered,
            use_worktree=not no_worktree,
        )

    run_command(opts, run, lambda result: _claims.render_claim(result, rendered))
