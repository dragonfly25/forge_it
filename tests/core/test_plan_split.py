"""Tests for :mod:`forge.core.plan_split`: ``forge plan split`` and ``assemble`` (ADR 0020).

The repository is a real clone from the bare-remote fixture, so ``git check-ignore`` runs
against a real ``.gitignore``. Split and assemble share nothing through GitHub (ADR 0020:
no racing-users test is needed); the re-run and interruption cases are covered instead.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from forge.core.adapters import is_ignored
from forge.core.config import PlanningConfig
from forge.core.git import Git
from forge.core.plan import validate_plan
from forge.core.plan_split import (
    PlanAssembly,
    PlanSplit,
    assemble_plan,
    normalize_plan,
    plan_batches_path,
    plan_part_path,
    render_plan,
    skeleton_sha256,
    split_plan,
    write_plan_assembly,
    write_plan_split,
)
from forge.core.zones import ZonesConfig, parse_zones

if TYPE_CHECKING:
    from conftest import GitWorld

PLAN = "plans/M1.yml"
SPEC = "specs/m1/core.md"
SPEC_TEXT = """\
---
milestone: M1
status: ready
---
# Core
## Requirements
- **AC-1** one
- **AC-2** two
## Open questions
"""
ZONES: dict[str, Any] = {
    "zones": {
        "api": {"paths": ["src/api/**"]},
        "web": {"paths": ["src/web/**"]},
        "shared": {"paths": ["shared/**"], "contract": True},
    }
}


def zones() -> ZonesConfig:
    return parse_zones(ZONES, ".forge/zones.yml")


def tid(n: int) -> str:
    return f"M1-T{n:02d}"


def skeleton_task(n: int, **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": tid(n),
        "title": f"Task {n}",
        "type": "impl",
        "spec": SPEC,
        "covers": ["AC-1"] if n % 2 else ["AC-2"],
        "depends_on": [],
        "zones": ["api"] if n % 2 else ["web"],
        "size": "S",
    }
    task.update(extra)
    return task


def skeleton(count: int, deps: dict[int, list[int]] | None = None) -> dict[str, Any]:
    tasks = [skeleton_task(n) for n in range(1, count + 1)]
    for n, on in (deps or {}).items():
        tasks[n - 1]["depends_on"] = [tid(d) for d in on]
    return {"milestone": "M1", "tasks": tasks}


def detail(task_id: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": task_id,
        "tier": "standard",
        "description": f"Build {task_id}.\nIn two lines.\n",
        "done_when": [f"{task_id} works", "tests pass"],
    }
    out.update(extra)
    return out


class Repo:
    """A clone with config-independent planning files and helpers."""

    def __init__(self, git: Git) -> None:
        self.git = git
        self.root = git.cwd
        (self.root / ".gitignore").write_text(".forge/local/\n")
        (self.root / "specs" / "m1").mkdir(parents=True)
        (self.root / SPEC).write_text(SPEC_TEXT)
        (self.root / "plans").mkdir()

    @property
    def plan(self) -> Path:
        return self.root / PLAN

    def write_plan(self, data: dict[str, Any] | str) -> None:
        text = data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False)
        self.plan.write_text(text)

    def split(self, *, max_per_agent: int = 10, write: bool = True) -> PlanSplit:
        outcome = split_plan(
            self.root,
            "M1",
            zones=zones(),
            planning=PlanningConfig(max_tasks_per_agent=max_per_agent),
            is_ignored=lambda rel: is_ignored(self.git, rel),
        )
        if write and outcome.ok:
            outcome = write_plan_split(self.root, outcome)
        return outcome

    def part(self, index: int) -> Path:
        return self.root / plan_part_path("M1", index)

    def write_part(self, index: int, data: dict[str, Any] | str) -> None:
        text = data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False)
        self.part(index).write_text(text)

    def write_fragments(self, split: PlanSplit) -> None:
        for batch in split.batches:
            self.write_part(
                batch.index,
                {
                    "milestone": "M1",
                    "batch": batch.index,
                    "tasks": [detail(t) for t in batch.task_ids],
                },
            )

    def assemble(self, *, write: bool = True) -> PlanAssembly:
        outcome = assemble_plan(self.root, "M1", zones=zones())
        if write and outcome.ok:
            outcome = write_plan_assembly(self.root, outcome)
        return outcome


@pytest.fixture
def repo(git_world: GitWorld) -> Repo:
    return Repo(git_world.clone("lead"))


def codes(outcome: PlanSplit | PlanAssembly) -> list[str]:
    return [e.code for e in outcome.errors]


# --------------------------------------------------------------------------- split


def test_split_single_batch(repo: Repo) -> None:
    repo.write_plan(skeleton(5))
    outcome = repo.split()
    assert outcome.ok, outcome.errors
    assert outcome.agents == 1
    assert [b.task_ids for b in outcome.batches] == [tuple(tid(n) for n in range(1, 6))]
    assert outcome.batches[0].part_path == ".forge/local/plan/M1/part-1.yml"
    text = (repo.root / plan_batches_path("M1")).read_text()
    assert text.endswith("}\n")
    doc = json.loads(text)
    assert doc == {
        "version": 1,
        "milestone": "M1",
        "skeleton_sha256": skeleton_sha256("M1", skeleton(5)),
        "max_per_agent": 10,
        "agents": 1,
        "batches": [
            {
                "index": 1,
                "task_ids": [tid(n) for n in range(1, 6)],
                "part_path": ".forge/local/plan/M1/part-1.yml",
            }
        ],
    }
    assert list(doc) == sorted(doc)
    data = outcome.to_data()
    assert data["batches_path"] == ".forge/local/plan/M1/batches.json"
    assert data["file"] == PLAN and data["removed_parts"] == []


@pytest.mark.parametrize(("count", "sizes"), [(12, [6, 6]), (25, [9, 8, 8]), (21, [7, 7, 7])])
def test_split_balanced_sizes(repo: Repo, count: int, sizes: list[int]) -> None:
    repo.write_plan(skeleton(count))
    outcome = repo.split()
    assert outcome.ok, outcome.errors
    assert [len(b.task_ids) for b in outcome.batches] == sizes
    assert [b.index for b in outcome.batches] == list(range(1, len(sizes) + 1))
    flat = [t for b in outcome.batches for t in b.task_ids]
    assert flat == [tid(n) for n in range(1, count + 1)]


def test_split_orders_by_component_then_wave(repo: Repo) -> None:
    # T01 depends on T03 (wave 1); T02 has component b-svc; others have no component.
    data = skeleton(4, deps={1: [3]})
    data["tasks"][1]["component"] = "b-svc"
    repo.write_plan(data)
    outcome = repo.split(max_per_agent=2)
    assert outcome.ok, outcome.errors
    assert [b.task_ids for b in outcome.batches] == [
        (tid(3), tid(4)),
        (tid(1), tid(2)),
    ]
    # The unchecked architecture fields are a warning, not an error.
    assert "architecture_fields_unchecked" in [w.code for w in outcome.warnings]


def test_split_default_max_without_planning_block(repo: Repo) -> None:
    repo.write_plan(skeleton(11))
    outcome = split_plan(
        repo.root, "M1", zones=zones(), is_ignored=lambda rel: is_ignored(repo.git, rel)
    )
    assert outcome.max_per_agent == 10
    assert [len(b.task_ids) for b in outcome.batches] == [6, 5]


def test_split_is_deterministic(repo: Repo) -> None:
    repo.write_plan(skeleton(25, deps={3: [1], 7: [3]}))
    repo.split()
    first = (repo.root / plan_batches_path("M1")).read_bytes()
    repo.split()
    assert (repo.root / plan_batches_path("M1")).read_bytes() == first


def test_split_groups_do_not_depend_on_file_order(repo: Repo) -> None:
    data = skeleton(12, deps={3: [1], 7: [3]})
    repo.write_plan(data)
    first = [b.task_ids for b in repo.split().batches]
    random.Random(7).shuffle(data["tasks"])
    repo.write_plan(data)
    assert [b.task_ids for b in repo.split().batches] == first


def test_split_removes_stale_parts(repo: Repo) -> None:
    repo.write_plan(skeleton(12))
    directory = repo.root / ".forge" / "local" / "plan" / "M1"
    directory.mkdir(parents=True)
    (directory / "part-1.yml").write_text("old")
    (directory / "part-5.yml").write_text("old")
    (directory / "notes.txt").write_text("kept")
    outcome = repo.split()
    assert outcome.removed_parts == (
        ".forge/local/plan/M1/part-1.yml",
        ".forge/local/plan/M1/part-5.yml",
    )
    assert sorted(p.name for p in directory.iterdir()) == ["batches.json", "notes.txt"]


def test_split_skeleton_without_detail_fields_is_accepted(repo: Repo) -> None:
    repo.write_plan(skeleton(3))
    assert repo.split().ok


def test_split_skeleton_with_invalid_present_detail_field_refuses(repo: Repo) -> None:
    data = skeleton(3)
    data["tasks"][0]["tier"] = "huge"
    repo.write_plan(data)
    outcome = repo.split()
    assert codes(outcome) == ["invalid_field"]
    assert outcome.batches == ()


def test_split_skeleton_error_writes_nothing(repo: Repo) -> None:
    data = skeleton(3)
    data["tasks"][0]["zones"] = ["nowhere"]
    repo.write_plan(data)
    directory = repo.root / ".forge" / "local" / "plan" / "M1"
    directory.mkdir(parents=True)
    (directory / "part-1.yml").write_text("old")
    outcome = repo.split()
    assert codes(outcome) == ["unknown_zone"]
    assert not outcome.precondition_failed
    assert (directory / "part-1.yml").is_file()
    assert not (directory / "batches.json").exists()
    with pytest.raises(ValueError):
        write_plan_split(repo.root, outcome)


def test_split_cycle_refuses(repo: Repo) -> None:
    repo.write_plan(skeleton(3, deps={1: [2], 2: [1]}))
    assert "dependency_cycle" in codes(repo.split())


def test_split_plan_empty(repo: Repo) -> None:
    repo.write_plan({"milestone": "M1", "tasks": []})
    outcome = repo.split()
    assert codes(outcome) == ["plan_empty"]
    assert not outcome.precondition_failed


def test_split_plan_missing(repo: Repo) -> None:
    outcome = repo.split()
    assert codes(outcome) == ["plan_missing"]
    assert outcome.precondition_failed
    assert not (repo.root / ".forge" / "local").exists()


def test_split_invalid_milestone(repo: Repo) -> None:
    outcome = split_plan(repo.root, "first", zones=zones(), is_ignored=lambda rel: True)
    assert codes(outcome) == ["invalid_milestone"]
    assert not outcome.precondition_failed
    assert outcome.to_data()["batches_path"] == ""


def test_split_not_ignored(repo: Repo) -> None:
    (repo.root / ".gitignore").write_text("")
    repo.write_plan(skeleton(3))
    outcome = repo.split()
    assert codes(outcome) == ["not_ignored"]
    assert outcome.precondition_failed
    assert "add `.forge/local/` to `.gitignore`" in (outcome.errors[0].hint or "")
    assert not (repo.root / ".forge" / "local").exists()


def test_split_interrupted_then_rerun_converges(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.write_plan(skeleton(12))
    first = repo.split()
    repo.write_fragments(first)
    original = os.replace

    def crash(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        repo.split()
    monkeypatch.setattr(os, "replace", original)
    directory = repo.root / ".forge" / "local" / "plan" / "M1"
    # No batches.json, no stale fragments, no temporary file: assemble refuses.
    assert sorted(p.name for p in directory.iterdir()) == []
    assert codes(repo.assemble()) == ["split_missing"]
    again = repo.split()
    assert again.ok and again.batches == first.batches


# --------------------------------------------------------------------------- hash


def test_hash_ignores_detail_null_and_key_order() -> None:
    data = skeleton(3)
    base = skeleton_sha256("M1", data)
    reordered = {"tasks": [dict(reversed(list(t.items()))) for t in data["tasks"]]}
    assert skeleton_sha256("M1", reordered) == base
    with_detail = skeleton(3)
    for t in with_detail["tasks"]:
        t.update(detail(t["id"]))
        t["proposal"] = None
    assert skeleton_sha256("M1", with_detail) == base


def test_hash_sees_skeleton_fields() -> None:
    base = skeleton_sha256("M1", skeleton(3))
    for key, value in [
        ("zones", ["web"]),
        ("title", "Other"),
        ("allow_contract_change", True),
        ("proposal", 4),
        ("component", "x"),
    ]:
        data = skeleton(3)
        data["tasks"][0][key] = value
        assert skeleton_sha256("M1", data) != base, key
    swapped = skeleton(3)
    swapped["tasks"].reverse()
    assert skeleton_sha256("M1", swapped) != base
    assert skeleton_sha256("M2", skeleton(3)) != base


# --------------------------------------------------------------------------- rendering


def test_render_plan_format_and_round_trip() -> None:
    data = skeleton(2, deps={2: [1]})
    data["tasks"][0].update(detail(tid(1)))
    data["tasks"][0]["implements"] = ["IF-a"]
    data["tasks"][0]["type"] = "contract"
    data["tasks"][1].update(detail(tid(2), description="One line"))
    data["tasks"][1]["proposal"] = None
    text = render_plan(data)
    assert text == (
        "milestone: M1\n"
        "tasks:\n"
        "  - id: M1-T01\n"
        "    title: Task 1\n"
        "    type: contract\n"
        "    spec: specs/m1/core.md\n"
        "    covers: [AC-1]\n"
        "    depends_on: []\n"
        "    zones: [api]\n"
        "    size: S\n"
        "    tier: standard\n"
        "    description: |\n"
        "      Build M1-T01.\n"
        "      In two lines.\n"
        "    done_when:\n"
        "      - M1-T01 works\n"
        "      - tests pass\n"
        "    implements: [IF-a]\n"
        "  - id: M1-T02\n"
        "    title: Task 2\n"
        "    type: impl\n"
        "    spec: specs/m1/core.md\n"
        "    covers: [AC-2]\n"
        "    depends_on: [M1-T01]\n"
        "    zones: [web]\n"
        "    size: S\n"
        "    tier: standard\n"
        "    description: One line\n"
        "    done_when:\n"
        "      - M1-T02 works\n"
        "      - tests pass\n"
    )
    assert yaml.safe_load(text) == normalize_plan(data)


@pytest.mark.parametrize(
    "value",
    [
        "yes",
        "1.0",
        "a: b",
        "- dash",
        "#hash",
        "trailing space \nnext",
        "unicode é ✓\nline",
        "no final newline\nhere",
        "two final\n\n",
        "  leading indent\nx\n",
        "x" * 300,
        "tab\there\n",
    ],
)
def test_render_plan_round_trips_tricky_strings(value: str) -> None:
    data = skeleton(1)
    data["tasks"][0]["description"] = value
    data["tasks"][0]["done_when"] = [value]
    data["tasks"][0]["title"] = value.replace("\n", " ")
    assert yaml.safe_load(render_plan(data)) == normalize_plan(data)


# --------------------------------------------------------------------------- assemble


@pytest.mark.parametrize(("count", "agents"), [(5, 1), (12, 2), (25, 3)])
def test_assemble_happy_path(repo: Repo, count: int, agents: int) -> None:
    repo.write_plan(skeleton(count, deps={2: [1]}))
    split = repo.split()
    assert split.agents == agents
    repo.write_fragments(split)
    outcome = repo.assemble()
    assert outcome.ok, outcome.errors
    assert outcome.written and outcome.changed
    assert outcome.tasks == tuple(tid(n) for n in range(1, count + 1))
    data = outcome.to_data()
    assert data["counts"] == {
        "batches": agents,
        "tasks": count,
        "errors": 0,
        "warnings": len(outcome.warnings),
    }
    written = yaml.safe_load(repo.plan.read_text())
    assert [t["id"] for t in written["tasks"]] == list(outcome.tasks)
    for task in written["tasks"]:
        assert task["tier"] == "standard"
        assert task["done_when"] == [f"{task['id']} works", "tests pass"]
    full = validate_plan(repo.root, "M1", zones=zones())
    assert full.ok, full.errors
    # No temporary file is left in plans/.
    assert sorted(p.name for p in (repo.root / "plans").iterdir()) == ["M1.yml"]


def test_assemble_rerun_is_unchanged_and_byte_identical(repo: Repo) -> None:
    repo.write_plan(skeleton(12))
    repo.write_fragments(repo.split())
    first = repo.assemble()
    assert first.changed
    before = repo.plan.read_bytes()
    second = repo.assemble()
    assert second.ok and second.written and not second.changed
    assert repo.plan.read_bytes() == before


def test_two_assembles_of_same_input_are_identical(git_world: GitWorld) -> None:
    outputs = []
    for user in ("alice", "bob"):
        other = Repo(git_world.clone(user))
        # Different formatting and comments in the skeleton, same skeleton fields.
        text = yaml.safe_dump(skeleton(25), sort_keys=user == "bob", default_flow_style=None)
        other.write_plan(f"# {user}'s notes\n{text}")
        other.write_fragments(other.split())
        assert other.assemble().ok
        outputs.append(other.plan.read_bytes())
    assert outputs[0] == outputs[1]
    assert b"notes" not in outputs[0]


def test_assemble_overrides_existing_detail(repo: Repo) -> None:
    data = skeleton(3)
    data["tasks"][0].update(tier="light", description="old", done_when=["old"])
    repo.write_plan(data)
    repo.write_fragments(repo.split())
    assert repo.assemble().ok
    task = yaml.safe_load(repo.plan.read_text())["tasks"][0]
    assert task["tier"] == "standard" and task["done_when"] == [f"{tid(1)} works", "tests pass"]


def test_assemble_keeps_skeleton_fields_and_order(repo: Repo) -> None:
    data = skeleton(4, deps={1: [3]})
    data["tasks"][2]["allow_contract_change"] = True
    data["tasks"][2]["zones"] = ["shared"]
    data["tasks"][3]["proposal"] = 12
    repo.write_plan(data)
    repo.write_fragments(repo.split(max_per_agent=2))
    assert repo.assemble().ok
    written = yaml.safe_load(repo.plan.read_text())
    for before, after in zip(data["tasks"], written["tasks"], strict=True):
        assert {k: after[k] for k in before} == before


def test_reformatted_skeleton_is_not_a_change(repo: Repo) -> None:
    data = skeleton(12)
    repo.write_plan(data)
    split = repo.split()
    repo.write_fragments(split)
    # Comments, flow style and key order change; skeleton fields do not.
    shuffled = {"tasks": [dict(sorted(t.items())) for t in data["tasks"]], "milestone": "M1"}
    text = yaml.safe_dump(shuffled, sort_keys=False, default_flow_style=True)
    repo.write_plan("# reasons go in the PR body\n" + text)
    assert repo.assemble().ok


def test_detail_added_to_skeleton_is_not_a_change(repo: Repo) -> None:
    data = skeleton(3)
    repo.write_plan(data)
    repo.write_fragments(repo.split())
    data["tasks"][1]["tier"] = "strong"
    repo.write_plan(data)
    assert repo.assemble().ok


def test_skeleton_changed_after_split(repo: Repo) -> None:
    data = skeleton(12)
    repo.write_plan(data)
    repo.write_fragments(repo.split())
    data["tasks"][4]["zones"] = ["web", "api"]
    repo.write_plan(data)
    before = repo.plan.read_bytes()
    outcome = repo.assemble()
    assert codes(outcome) == ["skeleton_changed"]
    assert not outcome.precondition_failed
    assert "forge plan split M1" in (outcome.errors[0].hint or "")
    assert repo.plan.read_bytes() == before


def test_skeleton_task_added_after_split(repo: Repo) -> None:
    repo.write_plan(skeleton(3))
    repo.write_fragments(repo.split())
    repo.write_plan(skeleton(4))
    assert codes(repo.assemble()) == ["skeleton_changed"]


# --------------------------------------------------------------------------- preconditions


def test_assemble_split_missing(repo: Repo) -> None:
    repo.write_plan(skeleton(3))
    outcome = repo.assemble()
    assert codes(outcome) == ["split_missing"]
    assert outcome.precondition_failed
    assert "forge plan split M1" in (outcome.errors[0].hint or "")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: "not json",
        lambda doc: json.dumps([doc]),
        lambda doc: json.dumps({**doc, "version": 2}),
        lambda doc: json.dumps({**doc, "milestone": "M2"}),
        lambda doc: json.dumps({**doc, "skeleton_sha256": None}),
        lambda doc: json.dumps({**doc, "batches": []}),
        lambda doc: json.dumps(
            {**doc, "batches": [{**doc["batches"][0], "part_path": "../../x.yml"}]}
        ),
        lambda doc: json.dumps({**doc, "batches": [{**doc["batches"][0], "index": 2}]}),
        lambda doc: json.dumps({**doc, "batches": [{**doc["batches"][0], "task_ids": []}]}),
        lambda doc: json.dumps({**doc, "batches": [doc["batches"][0], doc["batches"][0]]}),
    ],
)
def test_assemble_invalid_batches_json(repo: Repo, mutate: Any) -> None:
    repo.write_plan(skeleton(3))
    repo.write_fragments(repo.split())
    path = repo.root / plan_batches_path("M1")
    path.write_text(mutate(json.loads(path.read_text())))
    before = repo.plan.read_bytes()
    outcome = repo.assemble()
    assert codes(outcome) == ["split_missing"]
    assert repo.plan.read_bytes() == before


def test_assemble_plan_missing(repo: Repo) -> None:
    outcome = repo.assemble()
    assert codes(outcome) == ["plan_missing"] and outcome.precondition_failed


def test_assemble_invalid_milestone(repo: Repo) -> None:
    outcome = assemble_plan(repo.root, "m-1", zones=zones())
    assert codes(outcome) == ["invalid_milestone"] and not outcome.precondition_failed


def test_assemble_plan_invalid_yaml(repo: Repo) -> None:
    repo.write_plan(skeleton(3))
    repo.write_fragments(repo.split())
    repo.write_plan("tasks: [unclosed\n")
    assert codes(repo.assemble()) == ["plan_invalid"]
    assert repo.plan.read_text() == "tasks: [unclosed\n"


# --------------------------------------------------------------------------- fragment errors


def _fragment(split: PlanSplit, index: int) -> dict[str, Any]:
    batch = split.batches[index - 1]
    return {"milestone": "M1", "batch": index, "tasks": [detail(t) for t in batch.task_ids]}


def _broken(repo: Repo, index: int, data: dict[str, Any] | str) -> PlanAssembly:
    repo.write_part(index, data)
    before = repo.plan.read_bytes()
    outcome = repo.assemble()
    assert not outcome.ok
    assert not outcome.written and outcome.content is None
    assert repo.plan.read_bytes() == before, "a failing assemble must not touch the plan"
    with pytest.raises(ValueError):
        write_plan_assembly(repo.root, outcome)
    return outcome


@pytest.fixture
def split12(repo: Repo) -> PlanSplit:
    repo.write_plan(skeleton(12))
    split = repo.split()
    repo.write_fragments(split)
    return split


def test_fragment_missing(repo: Repo, split12: PlanSplit) -> None:
    repo.part(2).unlink()
    outcome = repo.assemble()
    assert codes(outcome) == ["fragment_missing"]
    assert outcome.errors[0].file == ".forge/local/plan/M1/part-2.yml"


@pytest.mark.parametrize(
    "text",
    [
        "milestone: [unclosed\n",
        "- a list\n",
        "milestone: M1\nbatch: 1\n",
        "milestone: M1\nbatch: 1\ntasks: []\nextra: 1\n",
        "milestone: M1\nbatch: 1\ntasks: {a: 1}\n",
    ],
)
def test_fragment_invalid(repo: Repo, split12: PlanSplit, text: str) -> None:
    outcome = _broken(repo, 1, text)
    assert "fragment_invalid" in codes(outcome)
    assert all(e.file == ".forge/local/plan/M1/part-1.yml" for e in outcome.errors)


def test_fragment_task_without_id_is_invalid(repo: Repo, split12: PlanSplit) -> None:
    data = _fragment(split12, 1)
    del data["tasks"][0]["id"]
    data["tasks"].append("not a mapping")
    outcome = _broken(repo, 1, data)
    assert codes(outcome) == ["fragment_invalid", "fragment_invalid", "missing_task"]


@pytest.mark.parametrize(("key", "value"), [("milestone", "M2"), ("batch", 2), ("batch", True)])
def test_fragment_mismatch(repo: Repo, split12: PlanSplit, key: str, value: Any) -> None:
    data = _fragment(split12, 1)
    data[key] = value
    outcome = _broken(repo, 1, data)
    assert codes(outcome) == ["fragment_mismatch"]
    assert outcome.errors[0].field == key


def test_fragment_saved_under_wrong_path(repo: Repo, split12: PlanSplit) -> None:
    outcome = _broken(repo, 1, _fragment(split12, 2))
    assert codes(outcome) == ["fragment_mismatch"]


def test_unassigned_task_from_other_batch_and_unknown(repo: Repo, split12: PlanSplit) -> None:
    data = _fragment(split12, 1)
    data["tasks"].append(detail(split12.batches[1].task_ids[0]))
    data["tasks"].append(detail("M1-T99"))
    outcome = _broken(repo, 1, data)
    assert codes(outcome) == ["unassigned_task", "unassigned_task"]
    assert [e.task for e in outcome.errors] == [split12.batches[1].task_ids[0], "M1-T99"]


def test_missing_task(repo: Repo, split12: PlanSplit) -> None:
    data = _fragment(split12, 2)
    dropped = data["tasks"].pop(3)["id"]
    outcome = _broken(repo, 2, data)
    assert codes(outcome) == ["missing_task"]
    assert outcome.errors[0].task == dropped
    assert outcome.errors[0].file == ".forge/local/plan/M1/part-2.yml"


def test_duplicate_id(repo: Repo, split12: PlanSplit) -> None:
    data = _fragment(split12, 1)
    data["tasks"].append(dict(data["tasks"][0]))
    outcome = _broken(repo, 1, data)
    assert codes(outcome) == ["duplicate_id"]


@pytest.mark.parametrize(
    "key", ["zones", "title", "depends_on", "allow_contract_change", "proposal", "component"]
)
def test_skeleton_field(repo: Repo, split12: PlanSplit, key: str) -> None:
    data = _fragment(split12, 1)
    data["tasks"][0][key] = ["api"] if key == "zones" else "x"
    outcome = _broken(repo, 1, data)
    assert codes(outcome) == ["skeleton_field"]
    error = outcome.errors[0]
    assert (error.file, error.task, error.field) == (
        ".forge/local/plan/M1/part-1.yml",
        data["tasks"][0]["id"],
        key,
    )


def test_unknown_field(repo: Repo, split12: PlanSplit) -> None:
    data = _fragment(split12, 1)
    data["tasks"][0]["notes"] = "x"
    outcome = _broken(repo, 1, data)
    assert codes(outcome) == ["unknown_field"]
    assert outcome.errors[0].field == "notes"


@pytest.mark.parametrize("key", ["description", "done_when", "tier"])
def test_missing_field(repo: Repo, split12: PlanSplit, key: str) -> None:
    data = _fragment(split12, 1)
    del data["tasks"][0][key]
    outcome = _broken(repo, 1, data)
    assert codes(outcome) == ["missing_field"]
    assert outcome.errors[0].field == key


@pytest.mark.parametrize(
    ("key", "value", "field"),
    [
        ("tier", "huge", "tier"),
        ("done_when", [], "done_when"),
        ("done_when", "a string", "done_when"),
        ("done_when", ["ok", "  "], "done_when[1]"),
        ("done_when", ["ok", 3], "done_when[1]"),
        ("description", "x\n<!-- forge:begin -->\ny\n", "description"),
        ("description", ["a"], "description"),
    ],
)
def test_invalid_field(repo: Repo, split12: PlanSplit, key: str, value: Any, field: str) -> None:
    data = _fragment(split12, 2)
    data["tasks"][1][key] = value
    outcome = _broken(repo, 2, data)
    assert codes(outcome) == ["invalid_field"]
    assert outcome.errors[0].field == field
    assert outcome.errors[0].task == data["tasks"][1]["id"]


def test_errors_of_every_fragment_are_reported(repo: Repo, split12: PlanSplit) -> None:
    repo.part(1).unlink()
    data = _fragment(split12, 2)
    data["tasks"][0]["tier"] = "huge"
    outcome = _broken(repo, 2, data)
    assert codes(outcome) == ["fragment_missing", "invalid_field"]


# --------------------------------------------------------------------------- full validation


def test_merged_plan_failing_full_validation_writes_nothing(repo: Repo, split12: PlanSplit) -> None:
    # The spec went back to draft after the split: only the full validation sees it.
    (repo.root / SPEC).write_text(SPEC_TEXT.replace("status: ready", "status: draft"))
    before = repo.plan.read_bytes()
    outcome = repo.assemble()
    assert "spec_draft" in codes(outcome)
    assert outcome.content is None and not outcome.written
    assert repo.plan.read_bytes() == before


def test_assemble_crash_during_replace_leaves_plan_intact(
    repo: Repo, split12: PlanSplit, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = repo.plan.read_bytes()
    original = os.replace

    def crash(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        repo.assemble()
    monkeypatch.setattr(os, "replace", original)
    assert repo.plan.read_bytes() == before
    assert sorted(p.name for p in (repo.root / "plans").iterdir()) == ["M1.yml"]
    # The re-run converges on the same bytes as an uninterrupted run.
    assert repo.assemble().ok
    first = repo.plan.read_bytes()
    assert repo.assemble().ok and repo.plan.read_bytes() == first


def test_fragments_and_batches_stay_after_assemble(repo: Repo, split12: PlanSplit) -> None:
    assert repo.assemble().ok
    directory = repo.root / ".forge" / "local" / "plan" / "M1"
    assert sorted(p.name for p in directory.iterdir()) == [
        "batches.json",
        "part-1.yml",
        "part-2.yml",
    ]


def test_render_plan_emits_no_yaml_alias() -> None:
    shared = ["it works", "tests pass"]
    data = {
        "milestone": "M1",
        "tasks": [
            {**skeleton_task(1), **detail(tid(1)), "done_when": shared},
            {**skeleton_task(2), **detail(tid(2)), "done_when": shared},
        ],
    }
    text = render_plan(data)
    assert "&id" not in text and "*id" not in text
    assert yaml.safe_load(text) == normalize_plan(data)


def test_assemble_of_skeleton_and_fragments_with_aliases_emits_none(repo: Repo) -> None:
    repo.write_plan(
        f"""\
milestone: M1
tasks:
  - id: {tid(1)}
    title: Task 1
    type: impl
    spec: {SPEC}
    covers: &covers [AC-1]
    depends_on: []
    zones: &zones [api]
    size: S
  - id: {tid(2)}
    title: Task 2
    type: impl
    spec: {SPEC}
    covers: *covers
    depends_on: []
    zones: *zones
    size: S
"""
    )
    split = repo.split()
    assert split.ok, split.errors
    repo.write_part(
        1,
        f"""\
milestone: M1
batch: 1
tasks:
  - id: {tid(1)}
    tier: standard
    description: Build it.
    done_when: &done [it works, tests pass]
  - id: {tid(2)}
    tier: standard
    description: Build it too.
    done_when: *done
""",
    )
    outcome = repo.assemble()
    assert outcome.ok, outcome.errors
    text = repo.plan.read_text()
    assert "&" not in text and "*" not in text
    tasks = yaml.safe_load(text)["tasks"]
    assert [t["done_when"] for t in tasks] == [["it works", "tests pass"]] * 2
    assert [t["zones"] for t in tasks] == [["api"], ["api"]]
