"""Tests for forge.core.presets (shipped model presets, ADR 0005)."""

from __future__ import annotations

import datetime as dt

import pytest

from forge.core.errors import ConfigError
from forge.core.presets import load_presets, parse_preset, presets_dir
from forge.core.tiers import Effort, Tier


def test_shipped_presets_are_package_data() -> None:
    names = sorted(e.name for e in presets_dir().iterdir() if e.name.endswith(".yml"))
    assert names == ["claude-code.yml", "codex.yml", "ollama.yml"]


def test_shipped_presets_load() -> None:
    presets = load_presets()
    assert set(presets) == {"claude-code", "codex", "ollama"}
    claude = presets["claude-code"]
    assert claude.provider == "anthropic"
    assert claude.supports_effort is True
    assert claude.tiers == {Tier.STRONG: "opus", Tier.STANDARD: "sonnet", Tier.LIGHT: "haiku"}
    assert claude.effort == {Effort.LOW: "low", Effort.MEDIUM: "medium", Effort.HIGH: "high"}
    assert {"opus", "fable", "claude-opus-5-5", "claude-fable-5-1"} <= set(
        claude.known_models[Tier.STRONG]
    )
    assert {"sonnet", "claude-sonnet-5-5"} <= set(claude.known_models[Tier.STANDARD])
    assert {"haiku", "claude-haiku-5-5"} <= set(claude.known_models[Tier.LIGHT])
    codex = presets["codex"]
    assert codex.provider == "openai"
    assert codex.supports_effort is True
    for tier, model in codex.tiers.items():
        assert model in codex.known_models[tier]
    ollama = presets["ollama"]
    assert ollama.provider == "local"
    assert ollama.supports_effort is False
    assert ollama.tiers == {} and ollama.known_models == {}
    for preset in presets.values():
        assert isinstance(preset.updated, dt.date)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("tool: x\nprovider: p\nupdated: 2026-01-01\nsupports_effort: true\nextra: 1\n", "extra"),
        ("tool: x\nprovider: p\nupdated: soon\nsupports_effort: true\n", "updated"),
        ("tool: x\nprovider: p\nupdated: 2026-01-01\nsupports_effort: 1\n", "supports_effort"),
        (
            "tool: x\nprovider: p\nupdated: 2026-01-01\nsupports_effort: true\n"
            "known_models: {huge: [a]}\n",
            "known_models.huge",
        ),
        (
            "tool: x\nprovider: p\nupdated: 2026-01-01\nsupports_effort: true\n"
            "tiers: {strong: 3}\n",
            "tiers.strong",
        ),
    ],
)
def test_invalid_preset_names_key(text: str, key: str) -> None:
    with pytest.raises(ConfigError) as exc:
        parse_preset(text, "forge/presets/x.yml")
    assert exc.value.key == key
    assert exc.value.file == "forge/presets/x.yml"


def test_updated_as_string_is_accepted() -> None:
    preset = parse_preset(
        "tool: x\nprovider: p\nupdated: '2026-01-01'\nsupports_effort: false\n", "x.yml"
    )
    assert preset.updated == dt.date(2026, 1, 1)
