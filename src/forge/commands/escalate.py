"""`forge escalate --reason R`: label the current task `blocked:conflict` and mention the
people who can unblock it (ADR 0011).

It needs a current task and GitHub, not a profile or a live claim. A CLI that does not satisfy
``forge_version`` only gets a warning (ADR 0003): escalating is how an agent asks for help.
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
from forge.core.escalation import EscalateContext
from forge.core.escalation import escalate as run_escalate
from forge.core.output import Result
from forge.core.tasks import BLOCKED_CONFLICT


def escalate(
    ctx: typer.Context,
    reason: Annotated[
        str,
        typer.Option(
            "--reason", metavar="R", help="What is blocked and why (quoted in the comment)."
        ),
    ],
) -> None:
    """Escalate the current task: label it blocked:conflict and mention people."""
    opts = get_options(ctx)

    def run() -> Result:
        app = load_context(opts)
        warnings: list[str] = []
        problem = _claims.version_problem(app.config)
        if problem is not None:
            warnings.append(problem)
        team = _claims.try_load_team(app, warnings)
        outcome = run_escalate(EscalateContext(git=app.git, github=app.github, team=team), reason)
        result = Result(data=outcome.to_data())
        for note in outcome.notes:
            result.info(note)
        for warning in warnings:
            result.warn(warning)
        return result

    run_command(opts, run, lambda result: render_escalate(result.data))


def render_escalate(data: dict[str, Any]) -> str:
    """Human view of ``forge escalate``; the reason and logins are printed literally."""
    buffer = io.StringIO()
    probe = Console()
    console = Console(
        file=buffer,
        force_terminal=probe.is_terminal,
        no_color=probe.no_color,
        width=min(probe.width, 100),
        markup=False,
        emoji=False,
        highlight=False,
        soft_wrap=True,
    )
    line = Text("Escalated", style="bold red")
    line.append(f"  {data['id']} #{data['issue']}")
    console.print(line)
    label = "added" if data["label_added"] else "already set"
    console.print(f"  label {BLOCKED_CONFLICT}: {label}")
    if data["comment_created"]:
        console.print(f"  comment posted ({data['comment']})")
    else:
        console.print("  same escalation already posted; no new comment")
    mentioned = data["mentioned"]
    console.print("  mentioned: " + (", ".join(f"@{m}" for m in mentioned) or "nobody"))
    console.print("  reason:")
    for text in str(data["reason"]).splitlines():
        console.print(f"    {text}")
    console.print(
        Text("Next: ", style="bold")
        + Text("wait for an answer on the issue; `forge sync` removes the label once it pushes")
    )
    return buffer.getvalue()
