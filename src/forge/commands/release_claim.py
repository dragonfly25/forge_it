"""`forge release-claim`: give the current task back (spec §8.4, ADR 0009)."""

from __future__ import annotations

from typing import Annotated

import typer

from forge.commands import _claims
from forge.commands._context import load_context
from forge.commands._output import get_options, run_command
from forge.core.claim import release_claim as run_release
from forge.core.output import Result
from forge.core.workspace import clear_current


def _render(result: Result) -> str:
    data = result.data
    branch = f"{data['branch']} deleted" if data["deleted"] else f"{data['branch']} kept"
    return f"Released #{data['issue']} {data['id']}; {branch}; status {data['status'] or '-'}"


def release_claim(
    ctx: typer.Context,
    delete_branch: Annotated[
        bool,
        typer.Option(
            "--delete-branch",
            help="Delete the task branch when it holds only Forge event commits.",
        ),
    ] = False,
) -> None:
    """Release the current task's claim: unassign, reset its status, comment."""
    opts = get_options(ctx)

    def run() -> Result:
        app = load_context(opts)
        location = _claims.require_current_task(app)
        profile, agent = _claims.load_profile_and_agent(opts)
        claim_ctx = _claims.claim_context(app, profile, agent)
        outcome = run_release(claim_ctx, location.issue, delete_branch=delete_branch)
        cleared = clear_current(location.workspace)
        result = Result(data={**outcome.to_data(), "current_cleared": cleared})
        for note in location.notes:
            result.info(note)
        for warning in outcome.warnings:
            result.warn(warning)
        return result

    run_command(opts, run, _render)
