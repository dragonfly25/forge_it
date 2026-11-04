"""`forge refresh`: recompute task status labels and unblock dependents (spec §7.2).

With ``--radar`` it then reconciles the radar comment on every open task: zone drift and
stale leases of in-progress tasks (spec §14, ADR 0014).
"""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

import typer
from rich.console import Console

from forge.commands._context import AppContext, load_context
from forge.commands._output import get_options, run_command
from forge.core.config import ForgeConfig
from forge.core.errors import ForgeError
from forge.core.git import Git
from forge.core.github import GitHubClient
from forge.core.output import Result
from forge.core.radar import run_refresh_radar
from forge.core.refresh import run_refresh
from forge.core.zones import ZonesConfig, load_zones


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _RadarCtx:
    app: AppContext
    zones: ZonesConfig | None
    clock: Callable[[], datetime] = field(default=_utc_now)

    @property
    def config(self) -> ForgeConfig:
        return self.app.config

    @property
    def git(self) -> Git:
        return self.app.git

    @property
    def github(self) -> GitHubClient:
        return self.app.github


def _radar(app: AppContext) -> Result:
    warning: str | None = None
    try:
        zones: ZonesConfig | None = load_zones(app.root)
    except ForgeError as exc:
        zones = None
        warning = f"zones could not be loaded: {exc.message}"
    result = run_refresh_radar(_RadarCtx(app, zones))
    if warning is not None:
        result.warn(warning, hint="fix .forge/zones.yml, then re-run forge refresh --radar")
    return result


def refresh(
    ctx: typer.Context,
    radar: Annotated[
        bool,
        typer.Option(
            "--radar",
            help="Also comment on in-progress tasks whose zones moved upstream or whose "
            "lease is stale (one comment per issue, kept up to date).",
        ),
    ] = False,
) -> None:
    """Recompute status labels from issues, task branches and PRs; unblock dependents."""
    opts = get_options(ctx)
    if radar:
        run_command(opts, lambda: _radar(load_context(opts)), _render)
    else:
        run_command(opts, lambda: run_refresh(load_context(opts)), _render)


# --------------------------------------------------------------------------- rendering


def _console(buffer: io.StringIO) -> Console:
    """A console that never interprets user text (ids, statuses) as markup or emoji."""
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


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _status(value: str | None) -> str:
    return value if value is not None else "(none)"


def render_refresh(data: dict[str, Any]) -> str:
    """Human view of the ``forge refresh`` data: changed tasks, a summary line and, with
    ``--radar``, the radar section.

    Warning texts are not repeated here: they are already emitted as warning messages.
    """
    buffer = io.StringIO()
    console = _console(buffer)
    changes: list[dict[str, Any]] = data.get("changes") or []
    if changes:
        console.print("Status changes:", style="bold")
        for change in changes:
            line = (
                f"  #{change['issue']} {change['id']}: "
                f"{_status(change['from'])} -> {_status(change['to'])}"
            )
            if not change.get("applied", True):
                line += "  (failed)"
            console.print(line, style=None if change.get("applied", True) else "red")
    counts: dict[str, Any] = data.get("counts") or {}
    summary = (
        f"Refresh: {_plural(int(counts.get('tasks', 0)), 'task')} checked, "
        f"{counts.get('changed', 0)} updated"
    )
    if counts.get("failed"):
        summary += f", {counts['failed']} failed"
    if counts.get("warnings"):
        summary += f", {_plural(int(counts['warnings']), 'warning')}"
    console.print(summary)
    radar = data.get("radar")
    if isinstance(radar, dict):
        _render_radar(console, radar)
    return buffer.getvalue()


def _render_radar(console: Console, radar: dict[str, Any]) -> None:
    reported = [
        t for t in radar.get("tasks") or [] if t["drift"]["total"] or t["lease"] is not None
    ]
    if reported:
        console.print("Radar:", style="bold")
        for task in reported:
            parts = []
            if task["drift"]["total"]:
                total = task["drift"]["total"]
                verb = "touches" if total == 1 else "touch"
                parts.append(f"{_plural(total, 'commit')} upstream {verb} its zones")
            if task["lease"] is not None:
                parts.append("lease stale")
            line = f"  #{task['issue']} {task['id']}: {', '.join(parts)}"
            line += f" (comment {task['comment']}{'' if task['applied'] else ', failed'})"
            console.print(line)
    counts = radar.get("counts") or {}
    console.print(
        f"Radar: {_plural(int(counts.get('in_progress', 0)), 'task')} in progress, "
        f"{counts.get('drift', 0)} with drift, {counts.get('stale', 0)} stale"
    )


def _render(result: Result) -> str:
    return render_refresh(result.data)
