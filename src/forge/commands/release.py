"""`forge release <milestone> [--version X.Y.Z] [--dry-run]` (spec §11.4, ADR 0015).

The work is :func:`forge.core.release.run_release`. This module loads the inputs (config,
roadmap and team for the soft lead check), enforces the ``forge_version`` pin and renders the
outcome. The first run opens the release pull request; after it is merged, the same command
tags the merge commit, publishes the GitHub release and closes the milestone.
"""

from __future__ import annotations

import io
from typing import Annotated, Any

import typer
from rich.text import Text

from forge.commands import _claims
from forge.commands._context import load_context
from forge.commands._output import get_options, run_command
from forge.commands.plan import plain_console
from forge.core.errors import ForgeError, PreconditionError
from forge.core.output import Result
from forge.core.plan import Roadmap, load_roadmap
from forge.core.release import ReleaseOptions, run_release


def release_cmd(
    ctx: typer.Context,
    milestone: Annotated[str, typer.Argument(help="Milestone id, e.g. M1.")],
    version: Annotated[
        str | None,
        typer.Option(
            "--version",
            metavar="X.Y.Z",
            help="Release version, without the 'v' prefix (required).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Print the changelog section and the next step; write nothing."
        ),
    ] = False,
) -> None:
    """Release a milestone: open the changelog PR, then (once merged) tag, publish and close."""
    opts = get_options(ctx)

    def run() -> Result:
        app = load_context(opts)
        problem = _claims.version_problem(app.config)
        if problem is not None:
            raise PreconditionError(problem, hint=_claims.VERSION_HINT)
        notes: list[str] = []
        team = _claims.try_load_team(app, notes)
        roadmap: Roadmap | None = None
        try:
            roadmap = load_roadmap(app.root)
        except ForgeError as exc:
            notes.append(f"plans/roadmap.yml not used: {exc.message}")
        result = run_release(
            app,
            ReleaseOptions(
                milestone=milestone,
                version=version,
                dry_run=dry_run,
                roadmap=roadmap,
                team=team,
                clock=lambda: _claims.clock(),
            ),
        )
        for note in notes:
            result.warn(note)
        return result

    run_command(opts, run, lambda result: render_release(result.data))


# --------------------------------------------------------------------------- rendering

_STEP_TEXT = {
    "pr_opened": "Release pull request ready",
    "released": "Released",
}


def render_release(data: dict[str, Any]) -> str:
    """Human view of the ``forge release`` data."""
    buffer = io.StringIO()
    console = plain_console(buffer)
    tag = data.get("tag") or data.get("milestone", "")
    step = data.get("step")
    if "open_tasks" in data:
        console.print(Text(f"Open tasks in {data['milestone']}:", style="bold"))
        for issue in data["open_tasks"]:
            console.print(f"  #{issue['number']}  {issue['title']}")
        return buffer.getvalue()
    if step == "dry_run":
        would = _STEP_TEXT.get(str(data.get("would")), str(data.get("would")))
        console.print(Text(f"{tag} (dry run: nothing written) - next: {would}", style="bold"))
    elif step is not None:
        console.print(Text(f"{tag}: {_STEP_TEXT.get(step, step)}", style="bold"))
    pull = data.get("pull")
    if pull:
        state = "created" if pull["created"] else ("updated" if pull["updated"] else "unchanged")
        console.print(f"  pull request #{pull['number']} ({state})  {pull['url']}")
    elif data.get("branch") and step == "dry_run":
        console.print(f"  branch {data['branch']} (pull request not opened yet)")
    if data.get("commit") and data.get("would", step) == "released":
        console.print(f"  tag commit {data['commit'][:12]}")
    release = data.get("release")
    if release:
        console.print(f"  GitHub release: {release['name']}  {release['url']}")
    if data.get("milestone_closed"):
        console.print(f"  milestone {data['milestone']} closed")
    section = data.get("section")
    if section:
        console.print("")
        console.print(section.rstrip("\n"))
    return buffer.getvalue()
