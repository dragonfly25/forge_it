"""Tests for :mod:`forge.core.batching` (ADR 0020)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from forge.core.batching import (
    balanced_batches,
    batch_sizes,
    component_key,
    plan_task_key,
    task_key,
    task_waves,
)
from forge.core.graph import GraphCycleError, natural_key
from forge.core.plan import PlanFile, PlanTask
from forge.core.tasks import Size, TaskMetadata, TaskType, Tier


def _ids(n: int) -> list[str]:
    return [f"M1-T{i:02d}" for i in range(1, n + 1)]


def _sizes(batches: list[list[str]]) -> list[int]:
    return [len(b) for b in batches]


# --------------------------------------------------------------------------- sizes


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, []),
        (1, [1]),
        (10, [10]),
        (11, [6, 5]),
        (12, [6, 6]),
        (20, [10, 10]),
        (21, [7, 7, 7]),
        (25, [9, 8, 8]),
        (30, [10, 10, 10]),
        (31, [8, 8, 8, 7]),
    ],
)
def test_size_matrix_max_10(count: int, expected: list[int]) -> None:
    batches = balanced_batches(_ids(count), 10, natural_key)
    assert _sizes(batches) == expected
    assert batch_sizes(count, 10) == expected


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, []),
        (1, [1]),
        (3, [3]),
        (4, [2, 2]),
        (5, [3, 2]),
        (6, [3, 3]),
        (7, [3, 2, 2]),
        (10, [3, 3, 2, 2]),
    ],
)
def test_size_matrix_max_3(count: int, expected: list[int]) -> None:
    components = [f"c{i:02d}" for i in range(count)]
    assert _sizes(balanced_batches(components, 3, component_key)) == expected


def test_max_one_gives_one_item_per_batch() -> None:
    assert balanced_batches(["b", "a", "c"], 1, component_key) == [["a"], ["b"], ["c"]]


@pytest.mark.parametrize("bad", [0, -1])
def test_max_below_one_raises(bad: int) -> None:
    with pytest.raises(ValueError, match="max_per_agent"):
        balanced_batches(["a"], bad, component_key)
    with pytest.raises(ValueError, match="max_per_agent"):
        balanced_batches([], bad, component_key)
    with pytest.raises(ValueError, match="max_per_agent"):
        batch_sizes(3, bad)


def test_negative_count_raises() -> None:
    with pytest.raises(ValueError, match="count"):
        batch_sizes(-1, 10)


@pytest.mark.parametrize("count", [0, 1, 9, 10, 11, 25, 31, 100])
def test_every_batch_is_within_bounds_and_balanced(count: int) -> None:
    sizes = batch_sizes(count, 10)
    assert sum(sizes) == count
    assert all(1 <= s <= 10 for s in sizes)
    if sizes:
        assert max(sizes) - min(sizes) <= 1
        assert sizes == sorted(sizes, reverse=True)


# --------------------------------------------------------------------------- order


def test_groups_are_contiguous_in_key_order() -> None:
    ids = _ids(25)
    shuffled = list(ids)
    random.Random(1).shuffle(shuffled)
    batches = balanced_batches(shuffled, 10, natural_key)
    assert [i for b in batches for i in b] == ids
    assert batches[0] == ids[:9] and batches[1] == ids[9:17] and batches[2] == ids[17:]


def test_natural_id_order() -> None:
    batches = balanced_batches(["M1-T10", "M1-T2", "M1-T1"], 10, natural_key)
    assert batches == [["M1-T1", "M1-T2", "M1-T10"]]


def test_sort_is_stable_for_equal_keys() -> None:
    items = [("x", 1), ("a", 2), ("x", 0), ("a", 1)]
    batches = balanced_batches(items, 10, lambda item: item[0])
    assert batches == [[("a", 2), ("a", 1), ("x", 1), ("x", 0)]]


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 42, 1234])
def test_shuffled_input_gives_identical_output(seed: int) -> None:
    items = [(f"comp-{i % 4}", i % 3, f"M1-T{i}") for i in range(1, 32)]
    keyed = lambda item: task_key(*item)  # noqa: E731
    expected = balanced_batches(items, 10, keyed)
    shuffled = list(items)
    random.Random(seed).shuffle(shuffled)
    assert balanced_batches(shuffled, 10, keyed) == expected
    assert _sizes(expected) == [8, 8, 8, 7]


def test_input_is_not_mutated() -> None:
    items = ["c", "a", "b"]
    balanced_batches(items, 2, component_key)
    assert items == ["c", "a", "b"]


# --------------------------------------------------------------------------- task key


def test_task_key_orders_component_then_wave_then_natural_id() -> None:
    keys = [
        task_key("web", 0, "M1-T01"),
        task_key(None, 2, "M1-T09"),
        task_key("api", 1, "M1-T02"),
        task_key("api", 0, "M1-T10"),
        task_key("api", 0, "M1-T3"),
    ]
    assert sorted(keys) == [
        task_key(None, 2, "M1-T09"),
        task_key("api", 0, "M1-T3"),
        task_key("api", 0, "M1-T10"),
        task_key("api", 1, "M1-T02"),
        task_key("web", 0, "M1-T01"),
    ]


def test_task_without_component_sorts_first() -> None:
    assert task_key(None, 5, "M1-T99") < task_key("a", 0, "M1-T01")
    assert task_key(None, 0, "M1-T01") == task_key("", 0, "M1-T01")


def test_task_waves_treat_external_deps_as_satisfied() -> None:
    waves = task_waves({"M2-T01": ["M1-T05"], "M2-T02": ["M2-T01"], "M2-T03": []})
    assert waves == {"M2-T01": 0, "M2-T02": 1, "M2-T03": 0}


def test_task_waves_raise_on_cycle() -> None:
    with pytest.raises(GraphCycleError):
        task_waves({"M1-T01": ["M1-T02"], "M1-T02": ["M1-T01"]})


def _plan_task(task_id: str, component: str | None, depends_on: tuple[str, ...] = ()) -> PlanTask:
    meta = TaskMetadata(
        id=task_id,
        type=TaskType.IMPL,
        spec="specs/m1/x.md",
        covers=(),
        depends_on=depends_on,
        zones=(),
        size=Size.S,
        tier=Tier.STANDARD,
        done_when=("ok",),
    )
    return PlanTask(meta=meta, title=task_id, description="", component=component)


def test_plan_task_key_groups_components_with_contracts_first() -> None:
    tasks = (
        _plan_task("M1-T01", "web", ("M1-T03",)),
        _plan_task("M1-T02", "api", ("M1-T03",)),
        _plan_task("M1-T03", "api"),
        _plan_task("M1-T04", None),
        _plan_task("M1-T05", "web"),
        _plan_task("M1-T06", "api", ("M1-T02", "M0-T01")),
    )
    plan = PlanFile(milestone="M1", tasks=tasks, path=Path("plans/M1.yml"))
    key = plan_task_key(plan)
    batches = balanced_batches(plan.tasks, 4, key)
    ids = [[t.meta.id for t in b] for b in batches]
    assert ids == [["M1-T04", "M1-T03", "M1-T02"], ["M1-T06", "M1-T05", "M1-T01"]]
    assert key(tasks[5]) == ("api", 2, natural_key("M1-T06"))
