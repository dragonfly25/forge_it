"""Tests for :mod:`forge.core.subagents` (ADR 0017, ADR 0020)."""

from __future__ import annotations

from pathlib import Path

from forge.core import delegation
from forge.core.subagents import (
    GENERATE_LOCAL,
    SUBAGENT_TOOLS,
    SubagentAvailability,
    rendered_subagent_path,
    subagent_availability,
)


def test_subagent_available_when_rendered_for_claude() -> None:
    result = subagent_availability("planner", tool="claude-code", rendered=True)
    assert result == SubagentAvailability(
        "forge-planner", True, "forge-planner is rendered for claude-code"
    )
    assert result.to_data() == {
        "name": "forge-planner",
        "available": True,
        "reason": "forge-planner is rendered for claude-code",
    }


def test_subagent_unavailable_without_profile() -> None:
    result = subagent_availability("arch-writer", tool=None, rendered=False)
    assert result.name == "forge-arch-writer" and not result.available
    assert "forge setup" in result.reason


def test_subagent_unavailable_for_tool_without_subagents() -> None:
    result = subagent_availability("planner", tool="ollama", rendered=True)
    assert not result.available and "ollama" in result.reason


def test_subagent_unavailable_when_not_rendered() -> None:
    result = subagent_availability("planner", tool="codex", rendered=False)
    assert not result.available
    assert ".codex/agents/forge-planner.toml" in result.reason
    assert "forge adapters generate --local" in result.reason


def test_rendered_subagent_path() -> None:
    assert rendered_subagent_path("claude-code", "planner") == Path(
        ".claude/agents/forge-planner.md"
    )
    assert rendered_subagent_path("codex", "arch-writer") == Path(
        ".codex/agents/forge-arch-writer.toml"
    )
    assert rendered_subagent_path("ollama", "planner") is None
    assert rendered_subagent_path(None, "planner") is None


def test_delegation_shares_the_subagent_constants() -> None:
    assert delegation.SUBAGENT_TOOLS is SUBAGENT_TOOLS
    assert GENERATE_LOCAL == "forge adapters generate --local"
    assert delegation.rendered_path("claude-code") == rendered_subagent_path(
        "claude-code", "implementer"
    )
