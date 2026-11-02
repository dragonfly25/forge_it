"""`forge heartbeat`: refresh the lease of the current task (spec §8.2, ADR 0009)."""

from __future__ import annotations

import typer

from forge.commands import _claims
from forge.commands._context import load_context
from forge.commands._output import get_options, run_command
from forge.core.lease import heartbeat as run_heartbeat
from forge.core.output import Result


def _render(result: Result) -> str:
    data = result.data
    return (
        f"Lease of #{data['issue']} {data['id']} renewed at {data['heartbeat_at']}; "
        f"stale after {data['stale_at']}"
    )


def heartbeat(ctx: typer.Context) -> None:
    """Refresh the lease of the current task's claim."""
    opts = get_options(ctx)

    def run() -> Result:
        app = load_context(opts)
        location = _claims.require_current_task(app)
        profile, agent = _claims.load_profile_and_agent(opts)
        outcome = run_heartbeat(_claims.claim_context(app, profile, agent), location.issue)
        result = Result(data=outcome.to_data())
        for note in location.notes:
            result.info(note)
        for warning in outcome.warnings:
            result.warn(warning)
        return result

    run_command(opts, run, _render)
