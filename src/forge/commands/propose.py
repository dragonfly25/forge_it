"""`forge propose --title T --body-file F`: create a ``status:proposed`` issue (ADR 0012).

It needs GitHub access; a profile is optional (without one the footer omits the agent). The
current task, when there is one, adds "while working on …" to the footer and gives the
milestone. The work is done by :mod:`forge.core.propose`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from forge.commands import _claims
from forge.commands._context import load_context
from forge.commands._output import get_options, run_command
from forge.core.errors import ForgeError
from forge.core.output import Result
from forge.core.propose import ProposeContext
from forge.core.propose import propose as run_propose
from forge.core.workspace import find_current_task


def propose(
    ctx: typer.Context,
    title: Annotated[str, typer.Option("--title", metavar="T", help="Proposal title.")],
    body_file: Annotated[
        Path, typer.Option("--body-file", metavar="F", help="File with the proposal body.")
    ],
) -> None:
    """Propose new work as a status:proposed issue."""
    opts = get_options(ctx)

    def run() -> Result:
        try:
            body = body_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ForgeError(
                f"cannot read --body-file {body_file}: {exc}", hint="pass a readable UTF-8 file"
            ) from exc
        app = load_context(opts)
        warnings: list[str] = []
        problem = _claims.version_problem(app.config)
        if problem is not None:
            warnings.append(problem)
        profile, agent = _claims.load_optional_profile(opts, warnings)
        location = find_current_task(app.git)
        outcome = run_propose(
            ProposeContext(
                github=app.github,
                login=None if profile is None else profile.github,
                agent=agent,
                current_issue=None if location is None else location.issue,
            ),
            title,
            body,
        )
        for text in warnings:
            outcome.warn(text)
        return outcome

    run_command(opts, run, render_propose)


def render_propose(result: Result) -> str:
    """Human view: the proposal issue, and whether it was created."""
    issue = result.data["issue"]
    verb = "Proposed" if result.data["created"] else "Already proposed"
    return f"{verb} #{issue['number']}: {issue['title']}\n  {issue['url']}"
