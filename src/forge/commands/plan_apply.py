"""`forge plan apply <milestone> [--dry-run] [--force]` (spec §9.5, ADR 0013).

The work is :func:`forge.core.plan_apply.plan_apply`; this module loads the inputs (config,
zones, team, profile), enforces the ``forge_version`` pin and the
``models.min_tier_for.plan`` check (shared with ``forge plan validate``), validates the plan
and renders the outcome.
"""

from __future__ import annotations

import io
from typing import Annotated, Any

import typer
from rich.text import Text

from forge.commands import _claims
from forge.commands._context import load_context
from forge.commands._output import get_options, run_command
from forge.commands.plan import plain_console, plan_tier_messages, render_validation
from forge.core.errors import PreconditionError
from forge.core.output import Result
from forge.core.plan import validate_plan
from forge.core.plan_apply import plan_apply
from forge.core.zones import load_zones


def apply(
    ctx: typer.Context,
    milestone: Annotated[str, typer.Argument(help="Milestone id, e.g. M1.")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print what would change on GitHub; write nothing."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Apply even if the agent cannot serve models.min_tier_for.plan."
        ),
    ] = False,
) -> None:
    """Create or update the milestone's task issues from plans/<milestone>.yml (§9.5)."""
    opts = get_options(ctx)

    def run() -> Result:
        app = load_context(opts)
        problem = _claims.version_problem(app.config)
        if problem is not None:
            raise PreconditionError(problem, hint=_claims.VERSION_HINT)
        messages = plan_tier_messages(opts, app, force=force)
        zones = load_zones(app.root)
        notes: list[str] = []
        team = _claims.try_load_team(app, notes)
        validation = validate_plan(
            app.root, milestone, zones=zones, team=team, planning=app.config.planning
        )
        result = plan_apply(app, validation, dry_run=dry_run)
        prefix = [*messages, *validation.messages]
        result.messages[:0] = prefix
        for note in notes:
            result.warn(note)
        return result

    run_command(opts, run, lambda result: render_apply(result.data))


# --------------------------------------------------------------------------- rendering


def _issue(number: int | None) -> str:
    return "new" if number is None else f"#{number}"


def render_apply(data: dict[str, Any]) -> str:
    """Human view of the ``forge plan apply`` data: what changes on GitHub, task by task."""
    if "validation" in data:
        return render_validation(data["validation"])
    if "tasks" not in data:
        return ""
    buffer = io.StringIO()
    console = plain_console(buffer)
    mode = " (dry run: nothing written)" if data.get("dry_run") else ""
    console.print(Text(f"Plan apply {data['milestone']}{mode}", style="bold"))
    console.print(f"  {data['file']}")
    labels = data.get("labels_created") or []
    if labels:
        console.print(f"Labels to create: {len(labels)}")
    ms = data.get("milestone_action")
    if ms is not None:
        console.print(f"Milestone {ms['title']}: {ms['action']} ({ms['description'] or '-'})")

    to_status = {c["issue"]: c["to"] for c in data.get("label_changes", [])}
    created = {c["id"]: c["status"] for c in data.get("creates", [])}
    console.print(Text("Tasks:", style="bold"))
    for task in data["tasks"]:
        line = Text(f"  {task['id']}  {_issue(task['issue'])}  ")
        action = task["action"]
        line.append(action, style="green" if action in ("create", "adopt", "update") else "")
        if task["fields"]:
            line.append(f" ({', '.join(task['fields'])})")
        if task["started"]:
            line.append(f"  started: {task['started']}")
        if task["pending"]:
            line.append(f"  not applied: {', '.join(task['pending'])}", style="yellow")
        if task["reason"]:
            line.append(f"  {task['reason']}", style="yellow")
        status = created.get(task["id"]) if action == "create" else to_status.get(task["issue"])
        if status:
            line.append(f"  -> {status}")
        console.print(line)

    for title, items in (
        ("Removed from the plan:", data["removed"]),
        ("Duplicates:", data["duplicates"]),
    ):
        if not items:
            continue
        console.print(Text(title, style="bold"))
        for item in items:
            text = {"close": "close as not planned", "keep": "left open", "skip": "left alone"}
            extra = f" (duplicate of #{item['duplicate_of']})" if item["duplicate_of"] else ""
            started = f", started: {item['started']}" if item["started"] else ""
            console.print(
                f"  {item['id']}  #{item['issue']}  {text[item['action']]}{extra}{started}"
            )

    others = [c for c in data.get("label_changes", []) if c["from"] != c["to"]]
    if others:
        console.print(Text("Status changes:", style="bold"))
        for change in others:
            console.print(
                f"  #{change['issue']} {change['id']}: {change['from'] or '-'} -> "
                f"{change['to'] or '-'}"
            )
    comments = data.get("comments") or []
    if comments:
        console.print(f"Comments: {len(comments)}")
    counts = data["counts"]
    console.print(f"GitHub writes: {counts['writes']}")
    return buffer.getvalue()
