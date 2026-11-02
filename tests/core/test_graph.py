"""Tests for forge.core.graph (cycles, waves, ancestry, readiness)."""

from __future__ import annotations

import itertools
import random
import time

import pytest

from forge.core.errors import ForgeError
from forge.core.graph import GraphCycleError, GraphError, TaskGraph, natural_key

LINEAR = {"A": [], "B": ["A"], "C": ["B"]}
DIAMOND = {"A": [], "B": ["A"], "C": ["A"], "D": ["B", "C"]}
DISCONNECTED = {"A": [], "B": ["A"], "X": [], "Y": ["X"], "Z": []}


def _shuffled(deps: dict[str, list[str]], seed: int) -> dict[str, list[str]]:
    rng = random.Random(seed)
    keys = list(deps)
    rng.shuffle(keys)
    out: dict[str, list[str]] = {}
    for key in keys:
        targets = list(deps[key])
        rng.shuffle(targets)
        out[key] = targets
    return out


# --------------------------------------------------------------------- natural sort


def test_natural_key_orders_numbers_numerically() -> None:
    ids = ["M1-T10", "M1-T2", "M10-T1", "M2-T1", "M1-T1"]
    assert sorted(ids, key=natural_key) == ["M1-T1", "M1-T2", "M1-T10", "M2-T1", "M10-T1"]


def test_natural_key_handles_mixed_shapes_and_ties() -> None:
    ids = ["b", "a10", "a2", "10", "2", "T01", "T1"]
    assert sorted(ids, key=natural_key) == ["2", "10", "T01", "T1", "a2", "a10", "b"]


# --------------------------------------------------------------------- acyclic shapes


def test_linear_waves() -> None:
    g = TaskGraph(LINEAR)
    assert g.cycles() == []
    assert not g.has_cycle()
    assert g.waves() == [["A"], ["B"], ["C"]]
    assert g.wave_of() == {"A": 0, "B": 1, "C": 2}


def test_diamond_waves() -> None:
    g = TaskGraph(DIAMOND)
    assert g.waves() == [["A"], ["B", "C"], ["D"]]
    assert g.wave_of()["D"] == 2


def test_wave_is_longest_path() -> None:
    g = TaskGraph({"A": [], "B": ["A"], "C": ["A", "B"]})
    assert g.waves() == [["A"], ["B"], ["C"]]


def test_disconnected_waves() -> None:
    g = TaskGraph(DISCONNECTED)
    assert g.waves() == [["A", "X", "Z"], ["B", "Y"]]


def test_empty_graph() -> None:
    g = TaskGraph({})
    assert g.waves() == []
    assert g.cycles() == []
    assert g.ready(set()) == []


def test_waves_sorted_naturally() -> None:
    g = TaskGraph({"M1-T10": [], "M1-T2": [], "M1-T1": []})
    assert g.waves() == [["M1-T1", "M1-T2", "M1-T10"]]


def test_duplicate_deps_are_deduplicated() -> None:
    g = TaskGraph({"A": [], "B": ["A", "A"]})
    assert g.depends_on("B") == ["A"]
    assert g.waves() == [["A"], ["B"]]


# --------------------------------------------------------------------- cycles


def test_self_loop() -> None:
    g = TaskGraph({"A": ["A"], "B": []})
    assert g.has_cycle()
    assert g.cycles() == [["A"]]


def test_two_node_cycle() -> None:
    g = TaskGraph({"B": ["A"], "A": ["B"]})
    assert g.cycles() == [["A", "B"]]


def test_three_node_cycle_path_order_follows_depends_on() -> None:
    # A depends on C, C depends on B, B depends on A.
    g = TaskGraph({"B": ["A"], "C": ["B"], "A": ["C"]})
    assert g.cycles() == [["A", "C", "B"]]


def test_two_separate_cycles() -> None:
    g = TaskGraph({"A": ["B"], "B": ["A"], "X": ["Y"], "Y": ["Z"], "Z": ["X"], "Q": []})
    assert g.cycles() == [["A", "B"], ["X", "Y", "Z"]]


def test_cycle_plus_acyclic_tail() -> None:
    g = TaskGraph({"R": [], "A": ["R", "C"], "B": ["A"], "C": ["B"], "T1": ["A"], "T2": ["T1"]})
    assert g.cycles() == [["A", "C", "B"]]
    assert g.has_cycle()


def test_overlapping_cycles_in_one_component_reported_once_each() -> None:
    # A->B->A, A->C->A and a self-loop on C, all in one component.
    g = TaskGraph({"A": ["B", "C"], "B": ["A"], "C": ["A", "C"]})
    assert g.cycles() == [["A", "B"], ["A", "C"], ["C"]]


def test_waves_raise_on_cycle() -> None:
    g = TaskGraph({"A": ["B"], "B": ["A"], "C": []})
    with pytest.raises(GraphCycleError) as info:
        g.waves()
    assert info.value.cycles == [["A", "B"]]
    assert "A -> B -> A" in str(info.value)
    assert isinstance(info.value, ForgeError)
    with pytest.raises(GraphCycleError):
        g.wave_of(ignore_unknown=True)


# --------------------------------------------------------------------- external / unknown


def test_external_deps_are_satisfied() -> None:
    g = TaskGraph({"M2-T1": ["M1-T3"], "M2-T2": ["M2-T1"]}, external_done={"M1-T3"})
    assert g.unknown_dependencies() == {}
    assert g.waves() == [["M2-T1"], ["M2-T2"]]
    assert g.ready(set()) == ["M2-T1"]


def test_unknown_deps_raise() -> None:
    g = TaskGraph({"A": ["GHOST", "B"], "B": [], "C": ["NOPE"]})
    assert g.unknown_dependencies() == {"A": ["GHOST"], "C": ["NOPE"]}
    with pytest.raises(GraphError) as info:
        g.waves()
    assert not isinstance(info.value, GraphCycleError)
    assert "GHOST" in str(info.value)
    assert "NOPE" in str(info.value)


def test_unknown_deps_ignored() -> None:
    g = TaskGraph({"A": ["GHOST", "B"], "B": [], "C": ["NOPE"]})
    assert g.waves(ignore_unknown=True) == [["B", "C"], ["A"]]


def test_unknown_deps_keep_task_not_ready() -> None:
    g = TaskGraph({"A": ["GHOST"], "B": []})
    assert g.ready(set()) == ["B"]


# --------------------------------------------------------------------- ancestry


def test_dependents_descendants_ancestors() -> None:
    g = TaskGraph(DIAMOND)
    assert g.dependents("A") == {"B", "C"}
    assert g.dependents("D") == set()
    assert g.descendants("A") == {"B", "C", "D"}
    assert g.descendants("B") == {"D"}
    assert g.ancestors("D") == {"A", "B", "C"}
    assert g.ancestors("A") == set()


def test_ancestry_with_external_and_missing_ids() -> None:
    g = TaskGraph({"B": ["EXT"], "C": ["B"]}, external_done={"EXT"})
    assert g.dependents("EXT") == {"B"}
    assert g.descendants("EXT") == {"B", "C"}
    assert g.ancestors("C") == {"B"}
    assert g.ancestors("NOT-THERE") == set()


def test_ancestry_on_cycle_includes_self() -> None:
    g = TaskGraph({"A": ["B"], "B": ["A"]})
    assert g.ancestors("A") == {"A", "B"}
    assert g.descendants("A") == {"A", "B"}


# --------------------------------------------------------------------- readiness


def test_ready_progresses_as_deps_close() -> None:
    g = TaskGraph(DIAMOND)
    closed: set[str] = set()
    assert g.ready(closed) == ["A"]
    closed.add("A")
    assert g.ready(closed) == ["B", "C"]
    closed.add("B")
    assert g.ready(closed) == ["C"]  # D still blocked by C
    closed.add("C")
    assert g.ready(closed) == ["D"]
    closed.add("D")
    assert g.ready(closed) == []


def test_ready_accepts_closed_ids_outside_graph() -> None:
    g = TaskGraph({"B": ["EARLIER"]})
    assert g.ready(set()) == []
    assert g.ready({"EARLIER"}) == ["B"]


# --------------------------------------------------------------------- determinism / scale


@pytest.mark.parametrize("seed", range(5))
def test_shuffled_input_gives_same_output(seed: int) -> None:
    deps = {
        "M1-T1": [],
        "M1-T2": ["M1-T1"],
        "M1-T10": ["M1-T1"],
        "M1-T3": ["M1-T2", "M1-T10"],
        "M1-T4": [],
        "M1-C1": ["M1-C3"],
        "M1-C2": ["M1-C1", "M1-C3"],
        "M1-C3": ["M1-C2"],
    }
    ref = TaskGraph(deps)
    other = TaskGraph(_shuffled(deps, seed))
    assert other.cycles() == ref.cycles()
    assert ref.cycles() == [["M1-C1", "M1-C3", "M1-C2"], ["M1-C2", "M1-C3"]]
    acyclic = {k: v for k, v in deps.items() if not k.startswith("M1-C")}
    assert TaskGraph(_shuffled(acyclic, seed)).waves() == TaskGraph(acyclic).waves()
    assert TaskGraph(acyclic).waves() == [["M1-T1", "M1-T4"], ["M1-T2", "M1-T10"], ["M1-T3"]]
    assert other.ready(set()) == ref.ready(set())


def test_long_chain_is_iterative() -> None:
    n = 500
    deps = {f"T{i}": ([f"T{i - 1}"] if i else []) for i in range(n)}
    g = TaskGraph(deps)
    assert not g.has_cycle()
    waves = g.waves()
    assert len(waves) == n
    assert waves[-1] == [f"T{n - 1}"]
    assert len(g.ancestors(f"T{n - 1}")) == n - 1
    assert len(g.descendants("T0")) == n - 1


def test_long_cycle_is_iterative() -> None:
    n = 500
    deps = {f"T{i}": [f"T{(i + 1) % n}"] for i in range(n)}
    cycles = TaskGraph(deps).cycles()
    assert len(cycles) == 1
    assert cycles[0][0] == "T0"
    assert len(cycles[0]) == n


# --------------------------------------------------------------------- bounded cycles


def _complete(n: int, prefix: str = "T") -> dict[str, list[str]]:
    ids = [f"{prefix}{i}" for i in range(1, n + 1)]
    return {a: [b for b in ids if b != a] for a in ids}


def _brute_force_cycles(deps: dict[str, list[str]]) -> list[list[str]]:
    """All elementary cycles by permutation, canonical form (small graphs only)."""
    ids = sorted(deps, key=natural_key)
    found: list[list[str]] = []
    for size in range(1, len(ids) + 1):
        for combo in itertools.combinations(ids, size):
            first, rest = combo[0], combo[1:]
            for perm in itertools.permutations(rest):
                cycle = [first, *perm]
                if all(cycle[(i + 1) % size] in deps[cycle[i]] for i in range(size)):
                    found.append(cycle)
    found.sort(key=lambda c: [natural_key(n) for n in c])
    return found


@pytest.mark.parametrize("n", [12, 30])
def test_complete_graph_is_fast(n: int) -> None:
    g = TaskGraph(_complete(n))
    start = time.perf_counter()
    assert g.has_cycle()
    cycles = g.cycles()
    with pytest.raises(GraphCycleError) as info:
        g.waves()
    with pytest.raises(GraphCycleError):
        g.wave_of()
    assert time.perf_counter() - start < 2.0
    assert len(cycles) == 20
    assert len({tuple(c) for c in cycles}) == 20
    for cycle in cycles:
        assert cycle[0] == min(cycle, key=natural_key)
        assert len(set(cycle)) == len(cycle)
        for i, node in enumerate(cycle):
            assert cycle[(i + 1) % len(cycle)] in g.depends_on(node)
    assert cycles == sorted(cycles, key=lambda c: [natural_key(x) for x in c])
    assert info.value.truncated
    assert len(info.value.cycles) == 20


def test_cycles_respects_limit_and_is_deterministic() -> None:
    deps = _complete(6)
    g = TaskGraph(deps)
    assert len(g.cycles(limit=3)) == 3
    assert g.cycles(limit=0) == []
    assert g.cycles(limit=5) == TaskGraph(_shuffled(deps, 1)).cycles(limit=5)
    assert ["T1", "T2"] in g.cycles(limit=1)  # shortest cycle through the smallest id


def test_cycles_exhaustive_matches_brute_force() -> None:
    deps = {"A": ["B", "C"], "B": ["C", "D"], "C": ["A", "D"], "D": ["A", "D"], "E": ["A"]}
    assert TaskGraph(deps).cycles(limit=1000) == _brute_force_cycles(deps)
    k5 = _complete(5)
    assert TaskGraph(k5).cycles(limit=1000) == _brute_force_cycles(k5)


def test_one_representative_per_dense_component() -> None:
    deps = {**_complete(8, "A"), **_complete(8, "B"), "S": ["S"], "Z": []}
    g = TaskGraph(deps)
    cycles = g.cycles(limit=3)
    assert cycles == [["A1", "A2"], ["B1", "B2"], ["S"]]
    assert g.cycle_components() == [
        [f"A{i}" for i in range(1, 9)],
        [f"B{i}" for i in range(1, 9)],
        ["S"],
    ]


def test_self_loops_inside_component_are_represented() -> None:
    deps = {**_complete(6), "T3": [*_complete(6)["T3"], "T3"]}
    cycles = TaskGraph(deps).cycles(limit=2)
    assert cycles == [["T1", "T2"], ["T3"]]


def test_cycle_components() -> None:
    g = TaskGraph({"A": ["B"], "B": ["A"], "C": ["C"], "D": ["A"], "E": []})
    assert g.cycle_components() == [["A", "B"], ["C"]]
    assert TaskGraph(DIAMOND).cycle_components() == []


def test_cycle_error_message_is_truncated() -> None:
    deps = {**_complete(10, "A"), **_complete(10, "B")}
    with pytest.raises(GraphCycleError) as info:
        TaskGraph(deps).waves()
    err = info.value
    assert err.truncated
    assert len(err.cycles) == 20
    assert err.more_components  # components that may hold further cycles
    message = str(err)
    assert "A1 -> A2 -> A1" in message
    assert "B1 -> B2 -> B1" in message
    assert "and more cycles in components {" in message
    assert "remove one depends_on edge" in message


def test_cycle_error_not_truncated_when_exhaustive() -> None:
    with pytest.raises(GraphCycleError) as info:
        TaskGraph({"A": ["B"], "B": ["A"]}).waves()
    assert not info.value.truncated
    assert info.value.more_components == []
    assert "more cycles" not in str(info.value)
