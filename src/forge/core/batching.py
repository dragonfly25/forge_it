"""Balanced batches for multi-agent planning (ADR 0020).

A pure, deterministic module: no I/O, no git, no GitHub.

* :func:`balanced_batches` cuts a list of items into ``ceil(N / max)`` contiguous groups of
  balanced size after a stable sort by a key (12 → 6+6, 21 → 7+7+7, 25 → 9+8+8).
* :func:`task_key` / :func:`plan_task_key` give the task order of ``forge plan split``:
  component, then wave (topological level), then natural id order.
* :func:`component_key` gives the component order of ``forge arch split``.

Subagent availability lives in :mod:`forge.core.subagents`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from forge.core.graph import NaturalKey, TaskGraph, natural_key

if TYPE_CHECKING:
    from forge.core.plan import PlanFile, PlanTask

__all__ = [
    "SupportsLessThan",
    "TaskKey",
    "balanced_batches",
    "batch_sizes",
    "component_key",
    "plan_task_key",
    "task_key",
    "task_waves",
]


class SupportsLessThan(Protocol):
    """Anything ``sorted`` can order: it supports ``<``."""

    def __lt__(self, other: Any, /) -> bool: ...


T = TypeVar("T")

TaskKey = tuple[str, int, NaturalKey]
"""Sort key of a task: ``(component or "", wave, natural_key(id))``."""


# --------------------------------------------------------------------------- batches


def batch_sizes(count: int, max_per_agent: int) -> list[int]:
    """Sizes of the groups :func:`balanced_batches` makes for ``count`` items.

    ``[]`` for 0 items; one group when ``count <= max_per_agent``; otherwise
    ``k = ceil(count / max_per_agent)`` groups of ``count // k`` items, the first
    ``count % k`` of them with one more. Raises :class:`ValueError` when ``max_per_agent < 1``
    or ``count < 0``.
    """
    if max_per_agent < 1:
        raise ValueError(f"max_per_agent must be >= 1, got {max_per_agent}")
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    if count == 0:
        return []
    groups = 1 if count <= max_per_agent else -(-count // max_per_agent)
    base, extra = divmod(count, groups)
    return [base + 1 if index < extra else base for index in range(groups)]


def balanced_batches(
    items: Sequence[T], max_per_agent: int, key: Callable[[T], SupportsLessThan]
) -> list[list[T]]:
    """Split ``items`` into balanced, contiguous groups of at most ``max_per_agent`` items.

    The items are sorted by ``key`` (a stable sort), then cut into the groups of
    :func:`batch_sizes`. With a total order on ``key`` the result does not depend on the
    input order. ``max_per_agent < 1`` raises :class:`ValueError` (a programming guard: the
    config loader already rejects such values). No items gives ``[]``.
    """
    sizes = batch_sizes(len(items), max_per_agent)
    ordered = sorted(items, key=key)
    batches: list[list[T]] = []
    start = 0
    for size in sizes:
        batches.append(ordered[start : start + size])
        start += size
    return batches


# --------------------------------------------------------------------------- keys


def task_key(component: str | None, wave: int, task_id: str) -> TaskKey:
    """Sort key of one task for ``forge plan split``.

    A task without a ``component`` sorts first (unchecked architecture, ADR 0019); within a
    component, lower waves first (contracts before the work that depends on them); then the
    natural id order (``M1-T2`` before ``M1-T10``). Task ids are unique, so the order is total.
    """
    return (component or "", wave, natural_key(task_id))


def task_waves(deps: Mapping[str, Iterable[str]]) -> dict[str, int]:
    """Wave (topological level, from 0) of each task of ``deps`` (``id -> depends_on``).

    Dependencies outside ``deps`` (other milestones) count as satisfied, as in ADR 0013.
    Raises :class:`~forge.core.graph.GraphCycleError` on a cycle: validate the plan first.
    """
    return TaskGraph(deps).wave_of(ignore_unknown=True)


def plan_task_key(plan: PlanFile) -> Callable[[PlanTask], TaskKey]:
    """Return the :func:`task_key` function for the tasks of ``plan``.

    The waves are computed once over the whole plan (:func:`task_waves`). Use it with
    :func:`balanced_batches`::

        balanced_batches(plan.tasks, max_tasks_per_agent, plan_task_key(plan))

    Raises :class:`~forge.core.graph.GraphCycleError` when the plan has a dependency cycle.
    """
    waves = task_waves({t.meta.id: t.meta.depends_on for t in plan.tasks})

    def key(task: PlanTask) -> TaskKey:
        return task_key(task.component, waves.get(task.meta.id, 0), task.meta.id)

    return key


def component_key(component_id: str) -> str:
    """Sort key of a component for ``forge arch split``: its id."""
    return component_id
