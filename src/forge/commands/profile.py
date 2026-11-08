"""`forge profile show|set|edit` (spec §6.5.3, ADR 0014).

``set`` and ``edit`` validate before saving and re-render the machine-level adapter files when
run inside a Forge project. ``edit`` runs the editor through the module attribute
:data:`editor_runner`, which tests replace with ``monkeypatch``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from forge.commands import setup as setup_cmd
from forge.commands._output import get_options, run_command
from forge.core.errors import PreconditionError
from forge.core.models import resolve
from forge.core.output import Result
from forge.core.presets import Preset, load_presets
from forge.core.profile import (
    Profile,
    apply_setting,
    load_profile,
    profile_path,
    profile_to_data,
)
from forge.core.setup import EditorRunner, edit_profile, editor_command, update_profile
from forge.core.tiers import EFFORTS, TIERS

profile_app = typer.Typer(
    help="Inspect or change the machine profile (§6.5).", no_args_is_help=True
)

editor_runner: EditorRunner | None = None
"""Runs the editor; ``None`` uses the default (subprocess). Tests replace it."""

_SETUP_HINT = "run `forge setup` to create it"


def _load(path: Path) -> Profile:
    if not path.is_file():
        raise PreconditionError(f"no Forge profile at {path}", hint=_SETUP_HINT)
    return load_profile(path)


def resolved_map(profile: Profile, presets: Mapping[str, Preset]) -> dict[str, Any]:
    """Return each agent's resolved tier → model and effort → value map (ADR 0005 rules)."""
    resolved: dict[str, Any] = {}
    for agent in profile.agents:
        tiers: dict[str, Any] = {}
        for tier in reversed(TIERS):
            r = resolve(agent, declared_tier=tier, presets=presets)
            tiers[str(tier)] = {
                "model": r.model,
                "via": str(r.effective_tier) if r.effective_tier else None,
            }
        effort: dict[str, str | None] = {}
        for level in EFFORTS:
            r = resolve(agent, declared_tier=TIERS[0], declared_effort=level, presets=presets)
            effort[str(level)] = r.effort_value
        resolved[agent.id] = {"tiers": tiers, "effort": effort}
    return resolved


def _profile_data(path: Path, profile: Profile, presets: Mapping[str, Preset]) -> dict[str, Any]:
    return {
        "path": str(path),
        "profile": profile_to_data(profile),
        "resolved": resolved_map(profile, presets),
    }


def render_profile(result: Result) -> str | None:
    """Human view: the profile as YAML, then each agent's resolved map."""
    data = result.data
    if "profile" not in data:
        return None
    lines = [f"# {data['path']}", yaml.safe_dump(data["profile"], sort_keys=False).rstrip()]
    lines.append("")
    lines.append("Resolved models:")
    for agent_id, entry in data.get("resolved", {}).items():
        lines.append(f"  {agent_id}:")
        for tier, item in entry["tiers"].items():
            if item["model"] is None:
                lines.append(f"    {tier:<9}-  (cannot serve)")
            elif item["via"] != tier:
                lines.append(f"    {tier:<9}{item['model']}  (via {item['via']})")
            else:
                lines.append(f"    {tier:<9}{item['model']}")
        efforts = ", ".join(
            f"{k}={v if v is not None else '-'}" for k, v in entry["effort"].items()
        )
        lines.append(f"    effort   {efforts}")
    if data.get("rendered"):
        lines.append("")
        lines.append(f"Rendered: {', '.join(data['rendered'])}")
    return "\n".join(lines)


def show(ctx: typer.Context) -> None:
    """Show the profile and each agent's resolved tier → model and effort map."""
    opts = get_options(ctx)

    def run() -> Result:
        path = profile_path()
        profile = _load(path)
        return Result(data=_profile_data(path, profile, load_presets()))

    run_command(opts, run, render_profile)


def set_(
    ctx: typer.Context,
    setting: Annotated[
        str,
        typer.Argument(
            help=(
                "<agent>.<tier>=<model>, <agent>.effort.<level>=<value>, default_agent=<id> "
                "or delegation=<off|auto|on>."
            )
        ),
    ],
) -> None:
    """Change one profile value, save it and re-render the machine-level adapters."""
    opts = get_options(ctx)

    def run() -> Result:
        path = profile_path()
        presets = load_presets()
        profile = update_profile(apply_setting(_load(path), setting), path)
        result = Result()
        rendered = setup_cmd.rerender(opts, profile, result, presets)
        result.data = {**_profile_data(path, profile, presets), "rendered": rendered}
        result.info(f"profile updated: {setting.strip()}")
        return result

    run_command(opts, run, render_profile)


def edit(ctx: typer.Context) -> None:
    """Edit the profile in $VISUAL ($EDITOR, vi); it is replaced only when the copy is valid."""
    opts = get_options(ctx)

    def run() -> Result:
        path = profile_path()
        if not path.is_file():
            raise PreconditionError(f"no Forge profile at {path}", hint=_SETUP_HINT)
        presets = load_presets()
        if editor_runner is None:
            outcome = edit_profile(path, editor_command())
        else:
            outcome = edit_profile(path, editor_command(), editor_runner)
        result = Result()
        rendered: list[str] = []
        if outcome.changed:
            rendered = setup_cmd.rerender(opts, outcome.profile, result, presets)
            result.info(f"profile updated: {path}")
        else:
            result.info("no changes")
        result.data = {
            **_profile_data(path, outcome.profile, presets),
            "changed": outcome.changed,
            "rendered": rendered,
        }
        return result

    run_command(opts, run, render_profile)


__all__ = ["edit", "profile_app", "render_profile", "resolved_map", "set_", "show"]
