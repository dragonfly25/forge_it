"""`forge review <pr> [--submit approve|request-changes|comment --body-file F] [--force]`
(spec §11.2, ADR 0012).

Without ``--submit`` it prints the review context; with it, it submits the review with a
Forge footer. The work is done by :mod:`forge.core.review`.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.text import Text

from forge.commands import _claims
from forge.commands._context import load_context
from forge.commands._output import get_options, run_command
from forge.core.errors import ForgeError, PreconditionError
from forge.core.output import Result
from forge.core.presets import load_presets
from forge.core.profile import AgentProfile
from forge.core.review import SUBMIT_EVENTS, ReviewContext, review_context, submit_review
from forge.core.zones import ZONES_PATH

MAX_DIFF_LINES = 400
"""Diff lines printed in the human view (the JSON output carries the whole capped diff)."""

_AC_LEAD_RE = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)?")


def _ac_summary(ac: str, text: str) -> str:
    """First line of an AC definition without its heading/list marker and its own id.

    ``- **AC-1** The API ...`` becomes ``The API ...``; text that does not start with the
    id (after the marker and emphasis) is returned with only the marker removed.
    """
    first = text.splitlines()[0] if text else ""
    rest = _AC_LEAD_RE.sub("", first, count=1)
    id_re = re.compile(
        r"^(?P<wrap>\*\*|__|\*|_|`)?"
        + re.escape(ac)
        + r"(?![\w-])[:.]?(?P=wrap)?\s*[:.\u2014\u2013-]?\s*"
    )
    match = id_re.match(rest)
    if match is not None:
        rest = rest[match.end() :]
    return rest.strip()


def review(
    ctx: typer.Context,
    pr: Annotated[int, typer.Argument(help="Pull request number.", min=1)],
    submit: Annotated[
        str | None,
        typer.Option(
            "--submit",
            metavar="approve|request-changes|comment",
            help="Submit a review instead of printing the context.",
        ),
    ] = None,
    body_file: Annotated[
        Path | None,
        typer.Option("--body-file", metavar="F", help="File with the review body (with --submit)."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Review even if the agent cannot serve models.min_tier_for.reviewer."
        ),
    ] = False,
) -> None:
    """Print a pull request's review context, or submit a review (spec §11.2)."""
    opts = get_options(ctx)

    def run() -> Result:
        body = _check_flags(submit, body_file)
        app = load_context(opts)
        warnings: list[str] = []
        problem = _claims.version_problem(app.config)
        agent: AgentProfile | None
        login: str | None
        if submit is not None:
            if problem is not None:
                raise PreconditionError(problem, hint=_claims.VERSION_HINT)
            profile, agent = _claims.load_profile_and_agent(opts)
            login = profile.github
        else:
            if problem is not None:
                warnings.append(problem)
            maybe_profile, agent = _claims.load_optional_profile(opts, warnings)
            login = None if maybe_profile is None else maybe_profile.github
        zones = None
        if (app.root / ZONES_PATH).is_file():
            zones = _claims.try_load_zones(app, warnings)
        review_ctx = ReviewContext(
            config=app.config,
            git=app.git,
            github=app.github,
            repo_root=app.root,
            zones=zones,
            agent=agent,
            presets=load_presets(),
        )
        if submit is not None:
            assert body is not None and login is not None
            result = submit_review(review_ctx, pr, submit, body, login=login, force=force)
        else:
            result = review_context(review_ctx, pr, force=force)
        for text in warnings:
            result.warn(text)
        return result

    run_command(opts, run, lambda result: render_review(result.data))


def _check_flags(submit: str | None, body_file: Path | None) -> str | None:
    """Validate ``--submit``/``--body-file`` (exit 1) and return the body when submitting."""
    if submit is None:
        if body_file is not None:
            raise ForgeError(
                "--body-file is only used with --submit",
                hint="add --submit approve|request-changes|comment",
            )
        return None
    if submit not in SUBMIT_EVENTS:
        raise ForgeError(
            f"unknown --submit value {submit!r}", hint="use one of: " + ", ".join(SUBMIT_EVENTS)
        )
    if body_file is None:
        raise ForgeError(
            "--body-file is required with --submit", hint="write the review to a file first"
        )
    try:
        return body_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ForgeError(
            f"cannot read --body-file {body_file}: {exc}", hint="pass a readable UTF-8 file"
        ) from exc


# --------------------------------------------------------------------------- rendering


def _console(buffer: io.StringIO) -> Console:
    """A console that never interprets user text (titles, paths, diffs) as markup or emoji."""
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


def render_review(data: dict[str, Any]) -> str:
    """Human view of ``forge review``: the submitted review, or the review context."""
    buffer = io.StringIO()
    console = _console(buffer)
    if "event" in data:
        _render_submit(console, data)
    else:
        _render_context(console, data)
    return buffer.getvalue()


def _render_submit(console: Console, data: dict[str, Any]) -> None:
    pr = data["pr"]
    line = Text("Review submitted", style="green")
    line.append(f"  {data['event']} on #{pr['number']} ({pr['url']})")
    console.print(line)
    console.print(f"  {data['footer']}")


def _render_context(console: Console, data: dict[str, Any]) -> None:
    pr = data["pr"]
    head = Text(f"PR #{pr['number']}", style="bold")
    head.append(f"  {pr['title']}")
    console.print(head)
    draft = " (draft)" if pr["draft"] else ""
    console.print(f"  {pr['head']} -> {pr['base']} by {pr['author']}, {pr['state']}{draft}")
    console.print(f"  {pr['url']}")
    task = data["task"]
    if task is None:
        console.print("Task: none linked")
    else:
        console.print(Text(f"Task {task['id']} #{task['issue']}", style="bold"))
        console.print(f"  {task['title']} (linked by {task['linked_by']})")
        meta = task["metadata"]
        console.print(f"  zones: {', '.join(meta['zones']) or '-'}  tier: {meta['tier']}")
        for item in meta["done_when"]:
            console.print(f"  done when: {item}")
        for ac, text in task["acceptance_criteria"].items():
            console.print(Text(f"  {ac}  {_ac_summary(ac, text)}".rstrip()))
    _render_zones(console, data.get("zones"))
    ci = data["ci"]
    console.print("CI: " + ("unavailable" if ci is None else ci["state"]))
    reviews = data["reviews"]
    console.print(f"Reviews ({len(reviews)}):")
    for r in reviews:
        console.print(f"  {r['author']}: {r['state']}")
    files = data["files"]
    console.print(Text(f"Files ({len(files)}):", style="bold"))
    for path in files:
        console.print(f"  {path}")
    diff = data["diff"]
    if diff:
        console.print(Text("Diff:", style="bold"))
        lines = diff.splitlines()
        for text in lines[:MAX_DIFF_LINES]:
            console.print(text)
        rest = len(lines) - MAX_DIFF_LINES
        if rest > 0:
            console.print(f"... {rest} more line(s); use --json for the whole diff", style="dim")
    if data["diff_truncated"]:
        console.print(f"(diff truncated; full size {data['diff_bytes']} bytes)", style="yellow")


def _render_zones(console: Console, zones: dict[str, Any] | None) -> None:
    if zones is None:
        console.print("Zones: not checked")
        return
    out = zones["out_of_zone"]
    unzoned = zones["unzoned"]
    if out is None:
        # no linked task: only files matching no zone at all can be reported
        if not unzoned:
            console.print("Zones: no task: unzoned check only; no unzoned files")
            return
        console.print(Text("Zones (no task: unzoned check only):", style="bold"))
    elif not unzoned and not out:
        console.print("Zones: all files in the task's zones")
        return
    else:
        console.print(Text("Zones:", style="bold"))
    for entry in out or []:
        console.print(f"  out of zone: {entry['path']} ({', '.join(entry['zones'])})")
    for path in unzoned:
        console.print(f"  unzoned: {path}")
