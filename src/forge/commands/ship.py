"""`forge ship [--draft]`: sync, run the checks and open or update the task's pull request
(spec §11.1, ADR 0012).

The work is done by :mod:`forge.core.ship`; this module loads the inputs (config, profile,
zones, team, presets), enforces the ``forge_version`` pin (ADR 0003) and renders the outcome.
Git runs in the current worktree (the cwd or ``--repo``).
"""

from __future__ import annotations

import io
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.text import Text

from forge.commands import _claims
from forge.commands._context import load_context
from forge.commands._output import get_options, run_command
from forge.commands.sync import render_sync
from forge.core.errors import PreconditionError
from forge.core.output import Result
from forge.core.presets import load_presets
from forge.core.ship import ShipContext, ShipOptions
from forge.core.ship import ship as run_ship
from forge.core.sync import SyncContext, load_sync_zones
from forge.core.workspace import find_current_task

MAX_OUTPUT_LINES = 20
"""Lines of a failing check's output tail shown in the human view."""

_HEADLINES = {
    "created": "Pull request opened",
    "updated": "Pull request updated",
    "sync_stopped": "Ship stopped at the sync",
    "checks_failed": "Ship stopped: failing checks",
    "unzoned": "Ship stopped: unzoned files (`unzoned: error`)",
}


def ship(
    ctx: typer.Context,
    draft: Annotated[
        bool,
        typer.Option("--draft", help="Open the pull request as a draft (on creation only)."),
    ] = False,
) -> None:
    """Sync, run the checks and open or update the task's pull request (spec §11.1)."""
    opts = get_options(ctx)

    def run() -> Result:
        app = load_context(opts)
        warnings: list[str] = []
        problem = _claims.version_problem(app.config)
        if problem is not None:
            raise PreconditionError(problem, hint=_claims.VERSION_HINT)
        location = find_current_task(app.git)
        recorded = None if location is None else _claims.recorded_agent(location)
        profile, agent = _claims.load_profile_and_agent(opts, recorded)
        zones = load_sync_zones(app.root)
        team = _claims.try_load_team(app, warnings)
        sync_ctx = SyncContext(
            config=app.config,
            git=app.git,
            github=app.github,
            zones=zones,
            agent=agent,
            profile_login=profile.github,
            team=team,
            clock=lambda: _claims.clock(),
        )
        outcome = run_ship(ShipContext(sync=sync_ctx, presets=load_presets()), ShipOptions(draft))
        result = outcome.to_result()
        for text in warnings:
            result.warn(text)
        return result

    run_command(opts, run, lambda result: render_ship(result.data))


# --------------------------------------------------------------------------- rendering


def _console(buffer: io.StringIO) -> Console:
    """A console that never interprets user text (paths, titles) as markup or emoji."""
    probe = Console()
    return Console(
        file=buffer,
        force_terminal=probe.is_terminal,
        no_color=probe.no_color,
        width=min(probe.width, 100),
        markup=False,
        emoji=False,
        highlight=False,
        soft_wrap=True,
    )


def render_ship(data: dict[str, Any]) -> str:
    """Human view of the ``forge ship`` data: outcome, checks, out-of-zone files, the pull
    request and the reviewers; a stopped sync is rendered as ``forge sync`` renders it."""
    outcome = data.get("outcome")
    if outcome is None:
        return ""
    if outcome == "sync_stopped" and data.get("sync"):
        return render_sync(data["sync"])
    buffer = io.StringIO()
    console = _console(buffer)
    style = "green" if outcome in ("created", "updated") else "bold yellow"
    line = Text(_HEADLINES.get(outcome, outcome), style=style)
    line.append(f"  {data['id']} #{data['issue']} on {data['branch']}")
    console.print(line)
    pr = data.get("pr")
    if pr:
        draft = " (draft)" if pr["draft"] else ""
        console.print(f"  PR #{pr['number']}{draft}: {pr['url']}")
    _render_checks(console, data["checks"])
    files = data.get("out_of_zone") or []
    if files:
        console.print(Text("Out-of-zone changes:", style="bold"))
        for item in files:
            where = "unzoned" if item["unzoned"] else "zone " + ", ".join(item["zones"])
            console.print(f"  {item['path']}  {where}")
    reviewers = data.get("reviewers")
    if reviewers is not None:
        requested = reviewers.get("requested") or []
        if requested:
            console.print(
                "Reviewers requested: "
                + ", ".join(
                    f"@{c['login']} ({c['reason']})"
                    for c in reviewers["selected"]
                    if c["login"] in requested
                )
            )
        elif reviewers["existing"]:
            console.print("Reviewers: " + ", ".join(f"@{x}" for x in reviewers["existing"]))
    return buffer.getvalue()


def _render_checks(console: Console, checks: dict[str, Any]) -> None:
    if checks["skipped"]:
        console.print("Checks: skipped (the sync passed them on this HEAD)")
        return
    runs: list[dict[str, Any]] = checks["runs"]
    if not runs:
        return
    failed = [c for c in runs if c["exit_code"] != 0]
    console.print(f"Checks: {len(runs) - len(failed)}/{len(runs)} passed")
    for check in failed:
        line = Text("  ")
        line.append(check["name"], style="bold red")
        line.append(f" (exit {check['exit_code']}): {check['command']}")
        console.print(line)
        lines = check["output"].rstrip("\n").splitlines()[-MAX_OUTPUT_LINES:]
        for text in lines:
            console.print(f"    {text}")
