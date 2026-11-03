"""`forge refresh`: recompute task status labels and unblock dependents (spec §7.2).

With ``--radar`` it then reconciles the radar comment on every open task: zone drift and
stale leases of in-progress tasks (spec §14, ADR 0014).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated

import typer

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
        run_command(opts, lambda: _radar(load_context(opts)))
    else:
        run_command(opts, lambda: run_refresh(load_context(opts)))
