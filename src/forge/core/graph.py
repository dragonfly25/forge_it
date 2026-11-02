"""Dependency graph of tasks: cycles, waves (spec §3, §8.1, §9.4, §11.3), ancestry and readiness.

The module is pure and generic over string ids. Edges point from a task to the tasks it
depends on (``depends_on``). Every algorithm is iterative so large plans never hit the
recursion limit, and every output is sorted with :func:`natural_key` so it is deterministic
regardless of input order.

Cycle reporting: strongly connected components are found with an iterative Tarjan, which
is all :meth:`TaskGraph.has_cycle` and the wave computation need (linear time). Listing
cycles is bounded by a ``limit``: first one shortest cycle per cyclic component (and every
self-loop), then further *elementary* cycles from Johnson's algorithm with blocking (for each
start node ``s`` in natural order, only nodes greater than ``s`` are visited, so each cycle is
reported once, rotated to start at its smallest id) until the limit is reached. Work stays
proportional to ``limit * (nodes + edges)`` even on dense graphs.
The path order follows ``depends_on``: ``["A", "B"]`` means A depends on B and B depends on A.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from collections.abc import Set as AbstractSet

from forge.core.errors import ForgeError

__all__ = [
    "GraphCycleError",
    "GraphError",
    "TaskGraph",
    "natural_key",
]

_DIGITS = re.compile(r"(\d+)")
_ERROR_CYCLE_LIMIT = 20

NaturalKey = tuple[tuple[str | int, ...], str]


def natural_key(task_id: str) -> NaturalKey:
    """Return a sort key ordering ids naturally (``M1-T2`` before ``M1-T10``).

    Digit runs compare as integers, the rest as text; the raw id breaks ties (``T01``/``T1``).
    """
    parts: list[str | int] = []
    for index, chunk in enumerate(_DIGITS.split(task_id)):
        parts.append(int(chunk) if index % 2 else chunk)
    return (tuple(parts), task_id)


def _sorted(ids: Iterable[str]) -> list[str]:
    return sorted(ids, key=natural_key)


def _format_cycle(cycle: list[str]) -> str:
    return " -> ".join([*cycle, cycle[0]])


class GraphError(ForgeError):
    """The dependency graph cannot be processed (e.g. unknown dependencies)."""


def _format_component(component: list[str]) -> str:
    shown = component if len(component) <= 5 else [*component[:3], f"… +{len(component) - 3}"]
    return "{" + ", ".join(shown) + "}"


class GraphCycleError(GraphError):
    """The dependency graph contains one or more cycles.

    ``cycles`` lists the cycles shown (bounded). When the listing was truncated, ``truncated``
    is true and ``more_components`` names the cyclic components that may hold further cycles.
    """

    def __init__(
        self,
        cycles: list[list[str]],
        more_components: Iterable[list[str]] = (),
    ):
        self.cycles = cycles
        self.more_components: list[list[str]] = [list(c) for c in more_components]
        self.truncated = bool(self.more_components)
        listed = "; ".join(_format_cycle(c) for c in cycles)
        if self.truncated:
            names = ", ".join(_format_component(c) for c in self.more_components)
            listed += f"; … and more cycles in components {names}"
        super().__init__(
            f"dependency cycle(s): {listed}",
            hint="remove one depends_on edge from each cycle listed",
        )


class TaskGraph:
    """Immutable dependency graph built from ``id -> depends_on``.

    ``external_done`` names ids outside the graph that count as satisfied (for example closed
    tasks of earlier milestones). Any other dependency outside the graph is *unknown*.
    """

    def __init__(
        self,
        deps: Mapping[str, Iterable[str]],
        external_done: AbstractSet[str] = frozenset(),
    ) -> None:
        self._deps: dict[str, tuple[str, ...]] = {
            node: tuple(_sorted(set(targets))) for node, targets in deps.items()
        }
        self._nodes: tuple[str, ...] = tuple(_sorted(self._deps))
        self._external: frozenset[str] = frozenset(external_done) - set(self._deps)
        rdeps: dict[str, set[str]] = {}
        for node, targets in self._deps.items():
            for target in targets:
                rdeps.setdefault(target, set()).add(node)
        self._rdeps = rdeps

    # ------------------------------------------------------------------ basics

    @property
    def nodes(self) -> list[str]:
        """All task ids in the graph, naturally sorted."""
        return list(self._nodes)

    def depends_on(self, task_id: str) -> list[str]:
        """Direct dependencies of ``task_id`` (naturally sorted, deduplicated)."""
        return list(self._deps.get(task_id, ()))

    def _internal(self, node: str) -> list[str]:
        return [d for d in self._deps[node] if d in self._deps]

    def unknown_dependencies(self) -> dict[str, list[str]]:
        """Map each task to the deps that are neither in the graph nor in ``external_done``."""
        result: dict[str, list[str]] = {}
        for node in self._nodes:
            unknown = [
                d for d in self._deps[node] if d not in self._deps and d not in self._external
            ]
            if unknown:
                result[node] = unknown
        return result

    # ------------------------------------------------------------------ cycles

    def _sccs(self) -> list[list[str]]:
        """Strongly connected components (iterative Tarjan), each naturally sorted."""
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: set[str] = set()
        stack: list[str] = []
        components: list[list[str]] = []
        counter = 0
        for root in self._nodes:
            if root in index:
                continue
            index[root] = low[root] = counter
            counter += 1
            stack.append(root)
            on_stack.add(root)
            work: list[tuple[str, int]] = [(root, 0)]
            while work:
                node, pos = work[-1]
                succ = self._internal(node)
                if pos < len(succ):
                    work[-1] = (node, pos + 1)
                    nxt = succ[pos]
                    if nxt not in index:
                        index[nxt] = low[nxt] = counter
                        counter += 1
                        stack.append(nxt)
                        on_stack.add(nxt)
                        work.append((nxt, 0))
                    elif nxt in on_stack:
                        low[node] = min(low[node], index[nxt])
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component: list[str] = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    components.append(_sorted(component))
        return components

    def _is_cyclic(self, component: list[str]) -> bool:
        return len(component) > 1 or component[0] in self._deps[component[0]]

    def cycle_components(self) -> list[list[str]]:
        """Components holding a cycle: SCCs with more than one task, plus self-loops.

        Each component is naturally sorted; components are ordered by their smallest id.
        """
        found = [c for c in self._sccs() if self._is_cyclic(c)]
        found.sort(key=lambda c: natural_key(c[0]))
        return found

    def has_cycle(self) -> bool:
        """True when the graph contains at least one cycle (including self-loops)."""
        return any(self._is_cyclic(c) for c in self._sccs())

    def cycles(self, limit: int = 20) -> list[list[str]]:
        """At most ``limit`` elementary cycles, each rotated to start at its smallest id, sorted.

        Every cyclic component (and every self-loop) gets one representative cycle before
        further cycles fill up to ``limit``; if there are more components than ``limit``,
        the first ones in natural order win. Runtime stays bounded on dense graphs.
        """
        return self._bounded_cycles(limit)[0]

    def _shortest_cycle(self, component: list[str]) -> list[str]:
        """Shortest cycle through the smallest id of a non-trivial SCC (BFS)."""
        members = set(component)
        start = component[0]
        parent: dict[str, str] = {}
        queue = [start]
        for node in queue:
            for nxt in self._deps[node]:
                if nxt not in members or nxt == node:
                    continue
                if nxt == start:
                    path = [node]
                    while path[-1] != start:
                        path.append(parent[path[-1]])
                    path.reverse()
                    return path
                if nxt not in parent:
                    parent[nxt] = node
                    queue.append(nxt)
        raise AssertionError(f"component {component} has no cycle")  # pragma: no cover

    def _scc_of(self, start: str, allowed: set[str]) -> set[str]:
        """Nodes of ``allowed`` that are both reachable from and reaching ``start``."""

        def reach(edges: Mapping[str, Iterable[str]]) -> set[str]:
            seen = {start}
            todo = [start]
            while todo:
                node = todo.pop()
                for nxt in edges.get(node, ()):
                    if nxt in allowed and nxt not in seen:
                        seen.add(nxt)
                        todo.append(nxt)
            return seen

        return reach(self._deps) & reach(self._rdeps)

    def _johnson(self, start: str, allowed: set[str]) -> Iterator[list[str]]:
        """Johnson's circuit search from ``start`` inside ``allowed`` (iterative, blocking).

        Yields every elementary cycle of length > 1 through ``start``; self-loops excluded.
        """

        def succ(node: str) -> list[str]:
            return [d for d in self._deps[node] if d in allowed and d != node]

        blocked: set[str] = {start}
        blocked_by: dict[str, set[str]] = {}
        path: list[str] = [start]
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(succ(start)))]
        closed: list[bool] = [False]
        while stack:
            node, successors = stack[-1]
            nxt = next(successors, None)
            if nxt is not None:
                if nxt == start:
                    yield list(path)
                    closed[-1] = True
                elif nxt not in blocked:
                    path.append(nxt)
                    blocked.add(nxt)
                    stack.append((nxt, iter(succ(nxt))))
                    closed.append(False)
                continue
            stack.pop()
            path.pop()
            found = closed.pop()
            if found:
                todo = [node]
                while todo:
                    member = todo.pop()
                    if member in blocked:
                        blocked.discard(member)
                        todo.extend(blocked_by.pop(member, ()))
            else:
                for target in succ(node):
                    blocked_by.setdefault(target, set()).add(node)
            if closed:
                closed[-1] = closed[-1] or found

    def _bounded_cycles(self, limit: int) -> tuple[list[list[str]], list[list[str]]]:
        """Return ``(cycles, incomplete_components)`` with at most ``limit`` cycles.

        ``incomplete_components`` lists the cyclic components whose cycles may not all be
        shown; it is empty when the listing is exhaustive.
        """
        limit = max(limit, 0)
        components = self.cycle_components()
        chosen: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        complete = [False] * len(components)

        def add(cycle: list[str]) -> bool:
            key = tuple(cycle)
            if key in seen:
                return True
            if len(chosen) >= limit:
                return False
            seen.add(key)
            chosen.append(cycle)
            return True

        # Phase 1: one representative per component, then every self-loop.
        overflow = False
        for component in components:
            if len(component) == 1:
                overflow |= not add([component[0]])
            else:
                overflow |= not add(self._shortest_cycle(component))
        for component in components:
            if len(component) > 1:
                for node in component:
                    if node in self._deps[node]:
                        overflow |= not add([node])

        # Phase 2: Johnson's enumeration, component by component, until the limit.
        if not overflow:
            for position, component in enumerate(components):
                stopped = False
                if len(component) > 1:
                    rank = {node: i for i, node in enumerate(component)}
                    for start in component:
                        allowed = self._scc_of(
                            start, {n for n in component if rank[n] >= rank[start]}
                        )
                        if len(allowed) < 2:
                            continue
                        for cycle in self._johnson(start, allowed):
                            if not add(cycle):
                                stopped = True
                                break
                        if stopped:
                            break
                if stopped:
                    break
                complete[position] = True

        chosen.sort(key=lambda c: [natural_key(n) for n in c])
        incomplete = [c for c, done in zip(components, complete, strict=True) if not done]
        return chosen, incomplete

    # ------------------------------------------------------------------ waves

    def wave_of(self, *, ignore_unknown: bool = False) -> dict[str, int]:
        """Map each task to its wave (topological level); see :meth:`waves`."""
        if self.has_cycle():
            raise GraphCycleError(*self._bounded_cycles(_ERROR_CYCLE_LIMIT))
        unknown = self.unknown_dependencies()
        if unknown and not ignore_unknown:
            listed = "; ".join(f"{node} -> {', '.join(deps)}" for node, deps in unknown.items())
            raise GraphError(
                f"unknown dependencies: {listed}",
                hint="fix the depends_on ids or add the missing tasks",
            )
        # Kahn's algorithm over in-graph edges only.
        remaining = {node: len(self._internal(node)) for node in self._nodes}
        level: dict[str, int] = {}
        frontier = [node for node in self._nodes if remaining[node] == 0]
        for node in frontier:
            level[node] = 0
        while frontier:
            next_frontier: list[str] = []
            for node in frontier:
                for dependent in self._rdeps.get(node, ()):
                    level[dependent] = max(level.get(dependent, 0), level[node] + 1)
                    remaining[dependent] -= 1
                    if remaining[dependent] == 0:
                        next_frontier.append(dependent)
            frontier = next_frontier
        return {node: level[node] for node in self._nodes}

    def waves(self, *, ignore_unknown: bool = False) -> list[list[str]]:
        """Topological levels; wave 0 holds tasks with no in-graph dependency.

        Raises :class:`GraphCycleError` on cycles and :class:`GraphError` on unknown
        dependencies unless ``ignore_unknown`` (then unknown deps count as satisfied).
        """
        by_level: dict[int, list[str]] = {}
        for node, lvl in self.wave_of(ignore_unknown=ignore_unknown).items():
            by_level.setdefault(lvl, []).append(node)
        return [_sorted(by_level[lvl]) for lvl in sorted(by_level)]

    # ------------------------------------------------------------------ ancestry

    def dependents(self, task_id: str) -> set[str]:
        """Tasks that directly depend on ``task_id`` (which may be outside the graph)."""
        return set(self._rdeps.get(task_id, ()))

    def descendants(self, task_id: str) -> set[str]:
        """Tasks that transitively depend on ``task_id``; excludes it unless on a cycle."""
        seen: set[str] = set()
        todo = list(self._rdeps.get(task_id, ()))
        while todo:
            node = todo.pop()
            if node in seen:
                continue
            seen.add(node)
            todo.extend(self._rdeps.get(node, ()))
        return seen

    def ancestors(self, task_id: str) -> set[str]:
        """In-graph tasks ``task_id`` transitively depends on; excludes it unless on a cycle."""
        seen: set[str] = set()
        todo = [d for d in self._deps.get(task_id, ()) if d in self._deps]
        while todo:
            node = todo.pop()
            if node in seen:
                continue
            seen.add(node)
            todo.extend(self._internal(node))
        return seen

    # ------------------------------------------------------------------ readiness

    def ready(self, closed: AbstractSet[str]) -> list[str]:
        """Open tasks whose deps are all closed or external, naturally sorted.

        A task is open when it is not in ``closed``. Unknown deps keep a task not ready.
        """
        return [
            node
            for node in self._nodes
            if node not in closed
            and all(d in closed or d in self._external for d in self._deps[node])
        ]
