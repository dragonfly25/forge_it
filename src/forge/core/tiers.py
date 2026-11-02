"""Abstract model tiers and effort levels (spec §6.5), shared by config, profile and models."""

from __future__ import annotations

from enum import StrEnum


class Tier(StrEnum):
    """Model strength a task or subagent needs. Ordered: light < standard < strong."""

    LIGHT = "light"
    STANDARD = "standard"
    STRONG = "strong"

    @property
    def rank(self) -> int:
        """Position in the strength order (0 = weakest)."""
        return _TIER_ORDER.index(self)


class Effort(StrEnum):
    """Abstract effort level. Ordered: low < medium < high."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        """Position in the effort order (0 = lowest)."""
        return _EFFORT_ORDER.index(self)


_TIER_ORDER: tuple[Tier, ...] = (Tier.LIGHT, Tier.STANDARD, Tier.STRONG)
_EFFORT_ORDER: tuple[Effort, ...] = (Effort.LOW, Effort.MEDIUM, Effort.HIGH)

TIERS: tuple[Tier, ...] = _TIER_ORDER
EFFORTS: tuple[Effort, ...] = _EFFORT_ORDER
TASK_TIER = "task"
"""Special subagent tier value meaning "inherit the task's tier" (spec §12.1)."""
