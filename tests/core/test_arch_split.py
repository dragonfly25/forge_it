"""Tests for the architecture split of :mod:`forge.core.architecture` (ADR 0020)."""

from __future__ import annotations

import random
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from forge.core.adapters import is_ignored
from forge.core.architecture import (
    ArchSplit,
    arch_part_path,
    arch_split_dir,
    split_architecture,
    write_arch_split,
)
from forge.core.zones import ZonesConfig, parse_zones

if TYPE_CHECKING:
    from conftest import GitWorld

SPEC = "specs/m1/editor.md"
ARCH = "docs/architecture/M1.md"
OVERVIEW = "docs/architecture/overview.md"

SPEC_TEXT = "---\nmilestone: M1\nstatus: ready\n---\n# Editor\n## Requirements\n- **AC-1** x\n"

FULL_BODY = """\
# M1 architecture

## Context
c
## Components
c
## Interfaces
i
## Data model
none
## Decisions
none
## Risks
none
## Open questions
"""

COMPONENTS = [f"c{n:02d}" for n in range(1, 8)]


def zones() -> ZonesConfig:
    data: dict[str, Any] = {f"z-{c}": {"paths": [f"src/{c}/**"]} for c in COMPONENTS}
    data["old"] = {"paths": ["src/old/**"]}
    data["shared"] = {"paths": ["shared/**"], "contract": True}
    return parse_zones({"zones": data}, ".forge/zones.yml")


def component(cid: str) -> dict[str, Any]:
    return {"id": cid, "zones": [f"z-{cid}"], "summary": cid}


def interface(iid: str, producer: str) -> dict[str, Any]:
    return {"id": iid, "zone": "shared", "producer": producer, "consumers": [], "summary": iid}


def front(
    components: list[str] | None = None,
    interfaces: list[dict[str, Any]] | None = None,
    status: str = "draft",
) -> dict[str, Any]:
    return {
        "milestone": "M1",
        "status": status,
        "specs": [SPEC],
        "components": [component(c) for c in (COMPONENTS if components is None else components)],
        "interfaces": [] if interfaces is None else interfaces,
    }


class Project:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.write(SPEC, SPEC_TEXT)

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def arch(self, data: dict[str, Any], body: str = "# M1\n") -> None:
        self.write(ARCH, "---\n" + yaml.safe_dump(data, sort_keys=False) + "---\n" + body)

    def overview(self) -> None:
        data = {
            "milestones": ["M0"],
            "components": [{"id": "old-svc", "zones": ["old"], "summary": "Old"}],
            "interfaces": [],
        }
        self.write(OVERVIEW, "---\n" + yaml.safe_dump(data, sort_keys=False) + "---\n# Map\n")

    def split(
        self, max_per_agent: int = 3, milestone: str = "M1", *, ignored: bool = True
    ) -> ArchSplit:
        return split_architecture(
            self.root,
            milestone,
            zones=zones(),
            max_per_agent=max_per_agent,
            is_ignored=lambda rel: ignored,
        )


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)


def test_groups_seven_components_by_three(project: Project) -> None:
    project.arch(front())
    split = project.split()
    assert split.ok, split.errors
    assert [b.component_ids for b in split.batches] == [
        ("c01", "c02", "c03"),
        ("c04", "c05"),
        ("c06", "c07"),
    ]
    assert [b.index for b in split.batches] == [1, 2, 3]
    assert [b.part_path for b in split.batches] == [
        ".forge/local/arch/M1/part-1.md",
        ".forge/local/arch/M1/part-2.md",
        ".forge/local/arch/M1/part-3.md",
    ]
    assert split.agents == 3 and split.max_per_agent == 3


@pytest.mark.parametrize(
    ("count", "sizes"),
    [(1, [1]), (3, [3]), (4, [2, 2]), (6, [3, 3]), (7, [3, 2, 2])],
)
def test_size_matrix(project: Project, count: int, sizes: list[int]) -> None:
    project.arch(front(COMPONENTS[:count]))
    split = project.split(3)
    assert [len(b.component_ids) for b in split.batches] == sizes


def test_below_threshold_is_one_agent(project: Project) -> None:
    project.arch(front(COMPONENTS[:3]))
    split = project.split(3)
    assert split.agents == 1
    assert split.batches[0].component_ids == ("c01", "c02", "c03")


def test_components_sorted_by_id_not_file_order(project: Project) -> None:
    order = list(COMPONENTS)
    random.Random(7).shuffle(order)
    project.arch(front(order))
    split = project.split()
    assert [c for b in split.batches for c in b.component_ids] == COMPONENTS


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_deterministic_for_any_file_order(tmp_path: Path, seed: int) -> None:
    reference = Project(tmp_path / "a")
    reference.arch(front(interfaces=[interface("IF-b", "c05"), interface("IF-a", "c01")]))
    other = Project(tmp_path / "b")
    comps = list(COMPONENTS)
    ifaces = [interface("IF-a", "c01"), interface("IF-b", "c05")]
    rng = random.Random(seed)
    rng.shuffle(comps)
    rng.shuffle(ifaces)
    other.arch(front(comps, ifaces))
    assert reference.split().to_data() == other.split().to_data()


def test_interfaces_follow_their_producer(project: Project) -> None:
    ifaces = [
        interface("IF-zeta", "c01"),
        interface("IF-alpha", "c01"),
        interface("IF-mid", "c05"),
        interface("IF-last", "c07"),
    ]
    project.arch(front(interfaces=ifaces))
    split = project.split()
    assert [b.interface_ids for b in split.batches] == [
        ("IF-alpha", "IF-zeta"),
        ("IF-mid",),
        ("IF-last",),
    ]
    assert split.unassigned_interfaces == ()


def test_interface_produced_by_an_overview_component_stays_with_the_main_session(
    project: Project,
) -> None:
    project.overview()
    project.arch(front(interfaces=[interface("IF-old", "old-svc"), interface("IF-new", "c02")]))
    split = project.split()
    assert split.ok, split.errors
    assert split.unassigned_interfaces == ("IF-old",)
    assert split.batches[0].interface_ids == ("IF-new",)
    assert all("old-svc" not in b.component_ids for b in split.batches)


def test_missing_sections_are_tolerated(project: Project) -> None:
    project.arch(front(), body="# just a title\n")
    assert project.split().ok


def test_full_body_and_ready_status_split_too(project: Project) -> None:
    project.arch(front(status="ready"), body=FULL_BODY)
    assert project.split().ok


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda d: d["components"][0].update(zones=["nowhere"]), "unknown_zone"),
        (lambda d: d["components"].append(component("c01")), "duplicate_id"),
        (lambda d: d["components"][0].update(id="Bad_Id"), "invalid_id"),
        (lambda d: d.update(owner="me"), "unknown_field"),
        (lambda d: d.update(milestone="M2"), "milestone_mismatch"),
        (lambda d: d["interfaces"].append(interface("IF-x", "ghost")), "unknown_component"),
        (lambda d: d.update(specs=["specs/m1/none.md"]), "spec_missing"),
    ],
)
def test_frontmatter_errors_block(project: Project, mutate: Any, code: str) -> None:
    data = front()
    mutate(data)
    project.arch(data)
    split = project.split()
    assert not split.ok
    assert code in [e.code for e in split.errors]
    assert "missing_section" not in [e.code for e in split.errors]
    assert split.batches == () and split.agents == 0


def test_missing_file(project: Project) -> None:
    split = project.split()
    assert split.missing and not split.ok
    assert [e.code for e in split.errors] == ["architecture_missing"]


def test_invalid_milestone(project: Project) -> None:
    split = project.split(milestone="first")
    assert [e.code for e in split.errors] == ["invalid_milestone"]
    assert not split.missing


def test_max_below_one_raises(project: Project) -> None:
    project.arch(front())
    with pytest.raises(ValueError):
        project.split(0)


def test_write_creates_the_directory_and_removes_stale_parts(project: Project) -> None:
    project.arch(front())
    directory = project.root / arch_split_dir("M1")
    directory.mkdir(parents=True)
    (directory / "part-1.md").write_text("old")
    (directory / "part-9.md").write_text("old")
    (directory / "notes.md").write_text("keep")
    other = project.root / arch_split_dir("M2")
    other.mkdir(parents=True)
    (other / "part-1.md").write_text("other milestone")

    written = write_arch_split(project.root, project.split())
    assert written.removed_parts == (
        ".forge/local/arch/M1/part-1.md",
        ".forge/local/arch/M1/part-9.md",
    )
    assert sorted(p.name for p in directory.iterdir()) == ["notes.md"]
    assert (other / "part-1.md").is_file()
    assert written.to_data()["removed_parts"] == list(written.removed_parts)

    again = write_arch_split(project.root, project.split())
    assert again.removed_parts == ()


def test_write_on_a_fresh_repo(project: Project) -> None:
    project.arch(front())
    written = write_arch_split(project.root, project.split())
    assert written.removed_parts == ()
    assert (project.root / arch_split_dir("M1")).is_dir()
    assert not (project.root / arch_part_path("M1", 1)).exists()


def test_write_refuses_a_split_with_errors(project: Project) -> None:
    with pytest.raises(ValueError):
        write_arch_split(project.root, project.split())
    assert not (project.root / ".forge").exists()


def test_to_data_shape(project: Project) -> None:
    project.arch(front(COMPONENTS[:2], [interface("IF-a", "c02")]))
    data = project.split().to_data()
    assert [w["code"] for w in data.pop("warnings")] == ["overview_stale"]
    assert data == {
        "milestone": "M1",
        "file": ARCH,
        "agents": 1,
        "max_per_agent": 3,
        "batches": [
            {
                "index": 1,
                "component_ids": ["c01", "c02"],
                "interface_ids": ["IF-a"],
                "part_path": ".forge/local/arch/M1/part-1.md",
            }
        ],
        "unassigned_interfaces": [],
        "removed_parts": [],
        "errors": [],
    }


def test_interface_ids_sorted_naturally(project: Project) -> None:
    project.overview()
    ifaces = [
        interface("IF-10", "c01"),
        interface("IF-2", "c01"),
        interface("IF-1", "c01"),
        interface("IF-old-10", "old-svc"),
        interface("IF-old-2", "old-svc"),
    ]
    project.arch(front(interfaces=ifaces))
    split = project.split()
    assert split.ok, split.errors
    assert split.batches[0].interface_ids == ("IF-1", "IF-2", "IF-10")
    assert split.unassigned_interfaces == ("IF-old-2", "IF-old-10")


def test_no_components_gives_zero_agents(project: Project) -> None:
    project.arch(front([]))
    split = project.split()
    assert split.ok, split.errors
    assert split.agents == 0 and split.batches == ()
    assert split.to_data()["agents"] == 0


def test_only_overview_producers_gives_zero_agents(project: Project) -> None:
    project.overview()
    project.arch(front([], [interface("IF-old", "old-svc")]))
    split = project.split()
    assert split.ok, split.errors
    assert split.agents == 0 and split.batches == ()
    assert split.unassigned_interfaces == ("IF-old",)


def test_not_ignored_is_a_precondition(project: Project) -> None:
    project.arch(front())
    split = project.split(ignored=False)
    assert [e.code for e in split.errors] == ["not_ignored"]
    assert split.precondition_failed and not split.missing and not split.ok
    assert split.batches == ()
    assert "add `.forge/local/` to `.gitignore`" in (split.errors[0].hint or "")


def test_not_ignored_comes_before_frontmatter_errors(project: Project) -> None:
    data = front()
    data["owner"] = "me"
    project.arch(data)
    assert [e.code for e in project.split(ignored=False).errors] == ["not_ignored"]


def test_missing_file_comes_before_not_ignored(project: Project) -> None:
    split = project.split(ignored=False)
    assert [e.code for e in split.errors] == ["architecture_missing"]
    assert split.precondition_failed


def test_invalid_milestone_is_not_a_precondition(project: Project) -> None:
    split = project.split(milestone="first", ignored=False)
    assert [e.code for e in split.errors] == ["invalid_milestone"]
    assert not split.precondition_failed


def test_not_ignored_against_a_real_repo(git_world: GitWorld) -> None:
    git = git_world.clone("alice")
    project = Project(git.cwd)
    project.arch(front())

    def check(rel: Path) -> bool:
        return is_ignored(git, rel)

    def run() -> ArchSplit:
        return split_architecture(git.cwd, "M1", zones=zones(), max_per_agent=3, is_ignored=check)

    assert [e.code for e in run().errors] == ["not_ignored"]
    (git.cwd / ".gitignore").write_text(".forge/local/\n")
    assert run().ok
