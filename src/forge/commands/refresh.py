"""`forge refresh`: recompute task status labels and unblock dependents (spec §7.2)."""

from __future__ import annotations

import typer

from forge.commands._context import load_context
from forge.commands._output import get_options, run_command
from forge.core.refresh import run_refresh


def refresh(ctx: typer.Context) -> None:
    """Recompute status labels from issues, task branches and PRs; unblock dependents."""
    opts = get_options(ctx)
    run_command(opts, lambda: run_refresh(load_context(opts)))
