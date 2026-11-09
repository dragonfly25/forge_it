"""Native subagents: which tools have them, where they are rendered, can one be spawned.

Shared by implementation delegation (ADR 0017, ``forge-implementer``) and multi-agent
planning (ADR 0020, ``forge-planner`` and ``forge-arch-writer``).

* :data:`SUBAGENT_TOOLS` lists the agent tools with native subagents.
* :func:`rendered_subagent_path` gives where an adapter renders a definition of
  ``.forge/agents/``.
* :func:`subagent_availability` applies the capability rule of ADR 0017 to a planning
  subagent; the caller supplies the file check, so the function is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.core.adapters import claude, codex

SUBAGENT_PREFIX = "forge-"
"""Prefix of the rendered subagent names (``planner`` → ``forge-planner``)."""

SUBAGENT_TOOLS: tuple[str, ...] = (claude.TOOL, codex.TOOL)
"""Agent tools with native subagents (ADR 0017)."""

GENERATE_LOCAL = "forge adapters generate --local"
"""The command that renders the subagent files of the active agent."""


@dataclass(frozen=True)
class SubagentAvailability:
    """Whether a planning subagent can be spawned (the ``subagent`` block, ADR 0020)."""

    name: str
    available: bool
    reason: str

    def to_data(self) -> dict[str, Any]:
        """JSON view: ``{"name", "available", "reason"}``."""
        return {"name": self.name, "available": self.available, "reason": self.reason}


def rendered_subagent_path(tool: str | None, definition: str) -> Path | None:
    """Repository-relative path of the rendered subagent ``definition`` for ``tool``.

    ``definition`` is the name in ``.forge/agents/`` (``implementer``, ``planner``,
    ``arch-writer``). ``None`` for a tool without native subagents, or without a tool.
    """
    if tool == claude.TOOL:
        return claude.subagent_path(definition)
    if tool == codex.TOOL:
        return codex.subagent_path(definition)
    return None


def subagent_availability(
    definition: str, *, tool: str | None, rendered: bool
) -> SubagentAvailability:
    """Apply the capability rule of ADR 0017 to the subagent ``definition``. Pure.

    ``tool`` is the active agent's tool (``None`` without a profile) and ``rendered`` says
    whether :func:`rendered_subagent_path` exists in the repository. The subagent is available
    when the tool is ``claude-code`` or ``codex``, a profile exists and the file is rendered.
    """
    name = f"{SUBAGENT_PREFIX}{definition}"
    if tool is None:
        return SubagentAvailability(name, False, "no Forge profile: run `forge setup`")
    if tool not in SUBAGENT_TOOLS:
        return SubagentAvailability(
            name,
            False,
            f"tool {tool} has no native subagents ({', '.join(SUBAGENT_TOOLS)} do)",
        )
    if not rendered:
        path = rendered_subagent_path(tool, definition)
        where = "" if path is None else f" ({path.as_posix()})"
        return SubagentAvailability(
            name, False, f"{name} is not rendered for {tool}{where}: run `{GENERATE_LOCAL}`"
        )
    return SubagentAvailability(name, True, f"{name} is rendered for {tool}")


__all__ = [
    "GENERATE_LOCAL",
    "SUBAGENT_PREFIX",
    "SUBAGENT_TOOLS",
    "SubagentAvailability",
    "rendered_subagent_path",
    "subagent_availability",
]
