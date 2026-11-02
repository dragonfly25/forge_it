"""`forge status [--me] [--milestone M]`: the team snapshot of spec §11.3 (ADR 0010). Read-only.

The report itself is built by :func:`forge.core.report.build_status`; this module loads the
inputs (config, profile, zones, presets), turns warnings into messages and renders the
``data`` payload for humans with rich.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from forge.commands import _claims
from forge.commands._context import find_repo_root, load_context
from forge.commands._output import get_options, run_command
from forge.core.config import CONFIG_PATH
from forge.core.errors import PreconditionError
from forge.core.output import Result
from forge.core.presets import load_presets
from forge.core.report import StatusContext, StatusOptions, build_status

MAX_ZONES = 2
"""Zones shown per task row before ``+N``."""

MAX_DRIFT_SHOWN = 3
"""Drift commits listed in the human view before ``+N more``."""

MAX_LISTED = 6
"""Ids listed inline (wave tasks, uncovered ACs) before ``+N more``."""

CI_STYLE = {"success": "green", "failure": "bold red", "pending": "yellow", "none": "dim"}


def status(
    ctx: typer.Context,
    me: Annotated[
        bool, typer.Option("--me", help="Only my work and the queue (Me and Queue sections).")
    ] = False,
    milestone: Annotated[
        str | None,
        typer.Option(
            "--milestone",
            metavar="M",
            help="Limit Team, Queue and Blocked to milestone M, and show M's progress.",
        ),
    ] = None,
) -> None:
    """Show me, team, queue, blocked, milestone and health (spec §11.3)."""
    opts = get_options(ctx)
    options = StatusOptions(me_only=me, milestone=milestone)

    def run() -> Result:
        root = find_repo_root(opts)
        if not (root / CONFIG_PATH).is_file():
            raise PreconditionError(
                f"{root / CONFIG_PATH}: file not found", hint="run `forge init` to create it"
            )
        app = load_context(opts)
        notes: list[str] = []
        profile, agent = _claims.load_optional_profile(opts, notes)
        zones = _claims.try_load_zones(app, notes)
        report = build_status(
            StatusContext(
                config=app.config,
                git=app.git,
                github=app.github,
                zones=zones,
                presets=load_presets(),
                profile=profile,
                agent=agent,
                clock=lambda: _claims.clock(),
                repo_root=app.root,
            ),
            options,
        )
        data = report.to_data()
        warnings = list(dict.fromkeys([*notes, *report.warnings]))
        data["warnings"] = warnings
        result = Result(data=data)
        for warning in warnings:
            result.warn(warning)
        return result

    run_command(opts, run, lambda result: render_status(result.data))


# --------------------------------------------------------------------------- rendering


def render_status(data: dict[str, Any], *, width: int | None = None) -> str:
    """Human view of the ``forge status`` data: one block per section, in spec order.

    Titles, subjects, branches, paths, labels and logins come from users, so the console
    never interprets strings as rich markup or emoji codes: every style is set through
    :class:`~rich.text.Text` or the ``style`` argument.
    """
    probe = Console()
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        force_terminal=probe.is_terminal,
        no_color=probe.no_color,
        width=width or min(probe.width, 100),
        markup=False,
        emoji=False,
        highlight=False,
        soft_wrap=False,
    )
    now = _parse(data["generated_at"])
    sections: list[str] = data["sections"]
    renderers = {
        "me": _render_me,
        "team": _render_team,
        "queue": _render_queue,
        "blocked": _render_blocked,
        "milestone": _render_milestone,
        "health": _render_health,
    }
    for index, name in enumerate(sections):
        if index:
            console.print()
        renderers[name](console, data.get(name), now)
    return buffer.getvalue()


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def ago(stamp: str | None, now: datetime) -> str:
    """Relative age of an ISO timestamp, e.g. ``3h ago`` (``in 2h`` for future times)."""
    if not stamp:
        return "-"
    seconds = int((now - _parse(stamp)).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        span = f"{seconds // 60}m"
    elif seconds < 48 * 3600:
        span = f"{seconds // 3600}h"
    else:
        span = f"{seconds // 86400}d"
    return f"in {span}" if future else f"{span} ago"


def _heading(console: Console, title: str, extra: str | Text = "") -> None:
    line = Text(title, style="bold")
    if extra:
        line.append("  ")
        line.append(extra if isinstance(extra, Text) else Text(extra))
    console.print(line)


def _none(console: Console, label: str = "") -> None:
    text = Text("  ")
    if label:
        text.append(f"{label}: ")
    text.append("none", style="dim")
    console.print(text)


def _more(items: Sequence[str], limit: int = MAX_LISTED, sep: str = ", ") -> str:
    shown = sep.join(items[:limit])
    rest = len(items) - limit
    return f"{shown}{sep if shown else ''}+{rest} more" if rest > 0 else shown


def _zones(zones: Sequence[str]) -> str:
    if not zones:
        return "-"
    shown = ",".join(zones[:MAX_ZONES])
    rest = len(zones) - MAX_ZONES
    return f"{shown},+{rest}" if rest > 0 else shown


def _who(claim: dict[str, Any] | None) -> str:
    if claim is None:
        return "-"
    return str(claim["owner"]) + (f"/{claim['agent']}" if claim.get("agent") else "")


def _ci(ci: dict[str, Any] | None) -> Text:
    if ci is None:
        return Text("unknown", style="dim")
    text = Text(ci["state"], style=CI_STYLE.get(ci["state"], ""))
    if ci["failing"]:
        text.append(f" ({_more(ci['failing'], 3)})")
    elif ci["pending"]:
        text.append(f" ({_more(ci['pending'], 3)})")
    return text


def _table(*columns: str) -> Table:
    table = Table(box=None, show_edge=False, pad_edge=False, padding=(0, 1), header_style="dim")
    for name in columns:
        if name == "Title":
            table.add_column(name, overflow="ellipsis", no_wrap=True, max_width=26)
        else:
            table.add_column(name, no_wrap=True)
    return table


def _indent(console: Console, table: Table) -> None:
    console.print(Padding(table, (0, 0, 0, 2), expand=False))


def _stale(claim: dict[str, Any] | None) -> Text:
    return Text("STALE", style="bold red") if claim and claim.get("stale") else Text("")


# --------------------------------------------------------------------------- Me


def _render_me(console: Console, me: dict[str, Any] | None, now: datetime) -> None:
    if me is None:
        _heading(console, "Me")
        _none(console)
        return
    who = me["login"] + (f" (agent {me['agent']})" if me.get("agent") else " (no profile)")
    _heading(console, "Me", who)
    current = me.get("current")
    if current is None:
        _none(console, "current task")
    else:
        _render_current(console, current, now)
    others: list[dict[str, Any]] = me.get("others") or []
    if not others:
        if current is None:
            _none(console, "other tasks")
        return
    console.print("  Other tasks")
    table = _table("ID", "#", "Title", "Status", "PR", "CI", "Activity", "")
    for task in others:
        pull = task.get("pull")
        claim = task.get("claim")
        table.add_row(
            task["id"],
            f"#{task['issue']}",
            task["title"],
            task["status"],
            f"#{pull['number']}" if pull else "-",
            _ci(task.get("ci")) if pull else Text("-"),
            ago(claim["last_activity"], now) if claim else "-",
            _stale(claim),
        )
    _indent(console, table)


def _render_current(console: Console, current: dict[str, Any], now: datetime) -> None:
    line = Text("  Current  ")
    line.append(f"{current['id']} #{current['issue']}", style="bold")
    line.append(f" [{current['status']}] {current['title']}")
    console.print(line, overflow="ellipsis", no_wrap=True)
    console.print(f"           branch {current['branch']}")
    console.print(f"           in {current['worktree']}", soft_wrap=True)
    claim = current.get("claim")
    if claim is not None:
        text = Text(
            f"           claim {_who(claim)} · last activity {ago(claim['last_activity'], now)}"
        )
        if claim.get("stale"):
            text.append(" · ")
            text.append_text(_stale(claim))
        else:
            text.append(f" · stale {ago(claim['stale_at'], now)}")
        console.print(text)
    sync = current.get("sync")
    if sync is not None:
        text = Text(f"  Sync     {sync['behind']} behind, {sync['unpushed']} unpushed")
        if sync["pending"]:
            text.append(" · pending sync", style="yellow")
        console.print(text)
    drift = current.get("drift")
    if drift is not None:
        _render_drift(console, drift, now)
    pull = current.get("pull")
    if pull is not None:
        text = Text(f"  PR       #{pull['number']}{' (draft)' if pull['draft'] else ''} · CI ")
        text.append_text(_ci(current.get("ci")))
        console.print(text)


def _render_drift(console: Console, drift: dict[str, Any], now: datetime) -> None:
    total = drift["total"]
    if not total:
        console.print(Text("  Drift    ") + Text("none", style="dim"))
        return
    console.print(Text(f"  Drift    {total} upstream commit(s) touch your zones", style="yellow"))
    commits: list[dict[str, Any]] = drift["commits"]
    for commit in commits[:MAX_DRIFT_SHOWN]:
        console.print(
            f"           {commit['sha'][:7]} {commit['subject']} "
            f"({', '.join(commit['zones'])}) {ago(commit['date'], now)}",
            overflow="ellipsis",
            no_wrap=True,
        )
    rest = total - min(len(commits), MAX_DRIFT_SHOWN)
    if rest > 0:
        console.print(f"           +{rest} more", style="dim")


# --------------------------------------------------------------------------- Team


def _render_team(console: Console, team: dict[str, Any] | None, now: datetime) -> None:
    _heading(console, "Team")
    if team is None:
        _none(console)
        return
    in_progress: list[dict[str, Any]] = team["in_progress"]
    if in_progress:
        console.print(f"  In progress ({len(in_progress)})")
        table = _table("ID", "#", "Title", "Owner", "Zones", "Activity", "")
        for task in in_progress:
            claim = task.get("claim")
            table.add_row(
                task["id"],
                f"#{task['issue']}",
                task["title"],
                _who(claim),
                _zones(task["zones"]),
                ago(claim["last_activity"], now) if claim else "-",
                _stale(claim),
            )
        _indent(console, table)
    else:
        _none(console, "in progress")
    in_review: list[dict[str, Any]] = team["in_review"]
    if in_review:
        console.print(f"  In review ({len(in_review)})")
        table = _table("ID", "#", "Title", "PR", "Author", "")
        for task in in_review:
            pull = task.get("pull") or {}
            table.add_row(
                task["id"],
                f"#{task['issue']}",
                task["title"],
                f"#{pull['number']}" if pull else "-",
                pull.get("author", "-"),
                Text("draft", style="dim") if pull.get("draft") else Text(""),
            )
        _indent(console, table)
    else:
        _none(console, "in review")
    handoffs: list[dict[str, Any]] = team["handoffs"]
    if handoffs:
        console.print(f"  Handoffs waiting ({len(handoffs)})")
        table = _table("ID", "#", "Title", "By", "Note")
        for task in handoffs:
            note = task.get("note")
            table.add_row(
                task["id"],
                f"#{task['issue']}",
                task["title"],
                task.get("by") or "-",
                f"{note['author']}, {ago(note['created_at'], now)}" if note else "no note",
            )
        _indent(console, table)
    else:
        _none(console, "handoffs waiting")


# --------------------------------------------------------------------------- Queue


def _render_queue(console: Console, queue: dict[str, Any] | None, now: datetime) -> None:
    if queue is None:
        _heading(console, "Queue")
        _none(console)
        return
    who = f"next for agent {queue['agent']}" if queue.get("agent") else "no agent (no profile)"
    _heading(console, "Queue", f"{queue['ready']} ready · {who}")
    picks: list[dict[str, Any]] = queue["next"]
    if not picks:
        _none(console, "next")
        return
    table = _table("ID", "#", "Title", "Status", "Wave", "Size", "Zones", "")
    for pick in picks:
        flag = "reclaim" if pick.get("reclaim") else "resume" if pick.get("resume") else ""
        table.add_row(
            pick["id"],
            f"#{pick['issue']}",
            pick["title"],
            pick["status"],
            str(pick["wave"]),
            pick["size"],
            _zones(pick["zones"]),
            Text(flag, style="cyan"),
        )
    _indent(console, table)


# --------------------------------------------------------------------------- Blocked


def _render_blocked(console: Console, blocked: dict[str, Any] | None, now: datetime) -> None:
    _heading(console, "Blocked")
    if blocked is None or not (blocked["by_dependencies"] or blocked["by_conflict"]):
        _none(console)
        return
    for task in blocked["by_dependencies"]:
        waits = ", ".join(f"{d['id']} ({d['status']})" for d in task["waiting_on"])
        console.print(
            f"  {task['id']} #{task['issue']} waits on {waits or '-'}", overflow="ellipsis"
        )
    for task in blocked["by_conflict"]:
        line = Text(f"  {task['id']} #{task['issue']} ")
        line.append("blocked:conflict", style="red")
        line.append(f" {task['title']}")
        console.print(line, overflow="ellipsis", no_wrap=True)


# --------------------------------------------------------------------------- Milestone


def _render_milestone(console: Console, milestone: dict[str, Any] | None, now: datetime) -> None:
    if milestone is None:
        _heading(console, "Milestone")
        _none(console)
        return
    source = {"option": "--milestone", "current_task": "current task", "first_open": "first open"}
    extra = Text(f"{milestone['done']}/{milestone['total']} done")
    extra.append(f" · {source.get(milestone['source'], milestone['source'])}", style="dim")
    if not milestone["open"]:
        extra.append(" · closed", style="dim")
    _heading(console, f"Milestone {milestone['title']}", extra)
    dropped: list[dict[str, Any]] = milestone["dropped"]
    if dropped:
        console.print(f"  Dropped: {_more([t['id'] for t in dropped])}", style="dim")
    proposed: list[str] = milestone.get("proposed") or []
    if proposed:
        console.print(f"  Proposed: {_more(proposed)}", style="dim")
    for wave in milestone["waves"]:
        console.print(
            f"  Wave {wave['wave']}  {wave['done']}/{wave['total']}  "
            f"{_more(wave['tasks'], MAX_LISTED, ' ')}",
            overflow="ellipsis",
            no_wrap=True,
        )
    acs = milestone["acs"]
    covered = len(acs["covered"])
    line = Text(f"  ACs     {covered}/{acs['total']} covered")
    if acs["uncovered"]:
        line.append(" · uncovered: ")
        line.append(_more(acs["uncovered"]), style="yellow")
    console.print(line)
    if acs["missing_specs"]:
        console.print(f"  Missing specs: {_more(acs['missing_specs'])}", style="yellow")


# --------------------------------------------------------------------------- Health


def _check(console: Console, ok: bool, text: str) -> None:
    line = Text("  ")
    line.append("✓" if ok else "!", style="green" if ok else "bold yellow")
    line.append(f" {text}")
    console.print(line, overflow="ellipsis", no_wrap=True)


def _render_health(console: Console, health: dict[str, Any] | None, now: datetime) -> None:
    _heading(console, "Health")
    if health is None:
        _none(console)
        return
    version = health["version"]
    relation = "satisfies" if version["ok"] else "does not satisfy"
    _check(console, version["ok"], f"forge {version['cli']} {relation} {version['required']}")
    if not health["profile"]:
        _check(console, False, "no Forge profile (run `forge setup`)")
    elif not health["violations"]:
        _check(console, True, "profile meets config.models")
    for violation in health["violations"]:
        _check(console, False, f"profile: {violation['agent']}: {violation['message']}")
    if health["presets"]:
        changes = [
            f"{d['agent']} {d['tier']}: {d['current']} → {d['preset']}" for d in health["presets"]
        ]
        _check(console, False, f"newer presets available: {_more(changes, 2)}")
    labels: list[dict[str, Any]] = health["unknown_labels"]
    if labels:
        listed = [f"{u['label']} ({', '.join(f'#{n}' for n in u['issues'])})" for u in labels]
        _check(console, False, f"unknown labels: {_more(listed, 3)}")
    else:
        _check(console, True, "no unknown labels")
    problems: list[dict[str, Any]] = health["without_metadata"]
    if problems:
        listed = [f"#{p['issue']} ({p['error']})" for p in problems]
        _check(console, False, f"issues without metadata: {_more(listed, 3)}")
    else:
        _check(console, True, "every task issue has metadata")
    proposals: list[dict[str, Any]] = health["proposals"]
    if proposals:
        listed = [f"#{p['issue']} {p['task'] or p['title']}" for p in proposals]
        _check(console, False, f"proposals awaiting the lead: {_more(listed, 3)}")
    else:
        _check(console, True, "no proposals awaiting the lead")
