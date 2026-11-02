"""Model presets shipped with Forge (spec §6.5.1, ADR 0005).

Presets live in ``src/forge/presets/<tool>.yml`` and are loaded as package data with
``importlib.resources``. They propose a tier → model mapping per tool and list the models the
preset author considers valid for each tier.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable

import yaml

from forge.core.errors import ConfigError
from forge.core.tiers import TIERS, Effort, Tier
from forge.core.validate import Fields, type_name

_KEYS = ("tool", "provider", "updated", "supports_effort", "tiers", "effort", "known_models")


@dataclass(frozen=True)
class Preset:
    """Default model choices for one tool."""

    tool: str
    provider: str
    updated: dt.date
    supports_effort: bool
    tiers: dict[Tier, str] = field(default_factory=dict)
    effort: dict[Effort, str] = field(default_factory=dict)
    known_models: dict[Tier, tuple[str, ...]] = field(default_factory=dict)


def presets_dir() -> Traversable:
    """Return the package-data directory holding the shipped presets."""
    return resources.files("forge") / "presets"


def load_presets() -> dict[str, Preset]:
    """Load every shipped preset, keyed by tool name.

    Raises :class:`ConfigError` naming the preset file when shipped data is invalid.
    """
    presets: dict[str, Preset] = {}
    for entry in sorted(presets_dir().iterdir(), key=lambda e: e.name):
        if not entry.is_file() or not entry.name.endswith(".yml"):
            continue
        name = f"forge/presets/{entry.name}"
        preset = parse_preset(entry.read_text(encoding="utf-8"), name)
        expected = entry.name.removesuffix(".yml")
        if preset.tool != expected:
            raise ConfigError(
                name,
                "tool",
                f"tool {preset.tool!r} does not match the file name",
                f"use {expected}",
            )
        presets[preset.tool] = preset
    return presets


def parse_preset(text: str, file: str) -> Preset:
    """Parse and validate the YAML ``text`` of one preset (``file`` is used in errors)."""
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(file, None, f"invalid YAML: {exc}", "fix the YAML syntax") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(
            file, None, f"top level must be a mapping, got {type_name(loaded)}", "use key: value"
        )
    f = Fields({str(k): v for k, v in loaded.items()}, file)
    f.reject_unknown(_KEYS)
    tool = f.str_("tool")
    provider = f.str_("provider")
    updated = f.raw("updated")
    if isinstance(updated, str):
        with contextlib.suppress(ValueError):
            updated = dt.date.fromisoformat(updated)
    if not isinstance(updated, dt.date) or isinstance(updated, dt.datetime):
        raise f.fail("updated", "expected a date (YYYY-MM-DD)", "write it as `updated: 2026-11-02`")
    supports_effort = f.raw("supports_effort")
    if not isinstance(supports_effort, bool):
        raise f.fail("supports_effort", "expected true or false", "add `supports_effort: true`")
    tiers = {Tier(k): v for k, v in f.str_map("tiers", key_enum=Tier).items()}
    effort = {Effort(k): v for k, v in f.str_map("effort", key_enum=Effort).items()}
    km = f.mapping("known_models")
    km.reject_unknown(str(t) for t in TIERS)
    known: dict[Tier, tuple[str, ...]] = {}
    for tier in TIERS:
        if km.has(str(tier)):
            known[tier] = tuple(km.str_list(str(tier)))
    return Preset(
        tool=tool,
        provider=provider,
        updated=updated,
        supports_effort=supports_effort,
        tiers=tiers,
        effort=effort,
        known_models=known,
    )


__all__ = ["Preset", "load_presets", "parse_preset", "presets_dir"]
