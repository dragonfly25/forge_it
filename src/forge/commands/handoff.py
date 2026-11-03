"""`forge handoff --note-file F`: publish the current task's work and hand it over with a note
(spec §8.3, ADR 0012).

The work is done by :mod:`forge.core.handoff`; this module reads the note, loads the profile
and the agent recorded for the current task, clears ``current.json`` once the handoff is
complete (the worktree is kept) and renders the outcome.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.text import Text

from forge.commands import _claims
from forge.commands._context import load_context
from forge.commands._output import get_options, run_command
from forge.core.errors import ExitCode, ForgeError
from forge.core.handoff import check_note
from forge.core.handoff import handoff as run_handoff
from forge.core.output import Result
from forge.core.workspace import clear_current

MAX_OUTPUT_LINES = 20
"""Lines of a hook's output tail shown in the human view."""

_HEADLINES = {
    "handed_off": "Handed off",
    "wip_rejected": "Handoff not started: a commit hook rejected the `forge: wip` commit",
    "push_rejected": "Handoff stopped: the pre-push hook rejected the push",
    "lease_rejected": "Handoff stopped: the branch moved on origin, nothing was pushed",
    "unpublished_work": "Handed off, but local work of this clone is not part of the handoff",
}


def read_note(path: Path) -> str:
    """Read the note file as UTF-8; :class:`ForgeError` (exit 1) naming the file otherwise."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ForgeError(
            f"note file {path} not found", hint="write the handoff note to a file first"
        ) from None
    except IsADirectoryError:
        raise ForgeError(f"note file {path} is a directory", hint="pass a file") from None
    except UnicodeDecodeError:
        raise ForgeError(
            f"note file {path} is not valid UTF-8", hint="save the note as UTF-8 text"
        ) from None
    except OSError as exc:
        raise ForgeError(
            f"cannot read note file {path}: {exc.strerror or exc}",
            hint="check the path and its permissions",
        ) from None


def handoff(
    ctx: typer.Context,
    note_file: Annotated[
        Path,
        typer.Option(
            "--note-file",
            metavar="F",
            help="Markdown note: Done, Remaining, Decisions, Gotchas, Next step, How to verify.",
        ),
    ],
) -> None:
    """Push the work, post the handoff note, unassign you and set status:handoff."""
    opts = get_options(ctx)

    def run() -> Result:
        note = check_note(read_note(note_file))
        app = load_context(opts)
        location = _claims.require_current_task(app)
        profile, agent = _claims.load_profile_and_agent(opts, _claims.recorded_agent(location))
        claim_ctx = _claims.claim_context(app, profile, agent)
        outcome = run_handoff(claim_ctx, location.issue, note)
        cleared = outcome.code == ExitCode.OK and clear_current(location.workspace)
        result = Result(code=outcome.code, data={**outcome.to_data(), "current_cleared": cleared})
        for text in location.notes:
            result.info(text)
        if outcome.error is not None:
            result.error(outcome.error, outcome.hint)
        for warning in outcome.warnings:
            result.warn(warning)
        return result

    run_command(opts, run, lambda result: render_handoff(result.data))


# --------------------------------------------------------------------------- rendering


def _console(buffer: io.StringIO) -> Console:
    """A console that never interprets user text (notes, paths) as markup or emoji."""
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


def render_handoff(data: dict[str, Any]) -> str:
    """Human view of the ``forge handoff`` data."""
    buffer = io.StringIO()
    console = _console(buffer)
    outcome = data["outcome"]
    ok = outcome == "handed_off"
    line = Text(_HEADLINES.get(outcome, outcome), style="green" if ok else "bold yellow")
    line.append(f"  {data['id']} #{data['issue']} on {data['branch']}")
    console.print(line)
    if ok:
        commit = data["handoff_commit"] or ""
        pushed = (
            "already pushed by a previous run"
            if data["recovered"]
            else f"pushed {data['commits']} commit(s) plus the handoff event"
        )
        console.print(f"  {pushed}; tip {commit[:12]}")
        if data["wip_commit"]:
            console.print(f"  uncommitted changes saved as `forge: wip` {data['wip_commit'][:12]}")
        note = "posted" if data["comment_created"] else "already posted"
        console.print(f"  note {note} (comment {data['comment']})")
        console.print(f"  status: {data['status'] or '-'}; you are unassigned")
        if data["missing_headings"]:
            console.print("  note lacks: " + ", ".join(data["missing_headings"]))
        if data.get("current_cleared"):
            console.print("  current.json cleared; the worktree is kept")
    unpublished = data.get("unpublished") or []
    if unpublished:
        console.print(
            f"  handoff tip {(data['handoff_commit'] or '')[:12]}; not published from here:"
        )
        for item in unpublished:
            console.print(f"    {item}")
        console.print("  current.json kept")
    output = data.get("output")
    if output:
        console.print("Hook output (tail):", style="bold")
        lines = output.rstrip("\n").splitlines()
        if len(lines) > MAX_OUTPUT_LINES:
            console.print(f"    ... {len(lines) - MAX_OUTPUT_LINES} line(s) above", style="dim")
            lines = lines[-MAX_OUTPUT_LINES:]
        for text in lines:
            console.print(f"    {text}")
    return buffer.getvalue()
