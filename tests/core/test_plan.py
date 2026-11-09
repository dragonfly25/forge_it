"""Tests for forge.core.plan: roadmap, plan files and the offline validation (ADR 0013).

Each test writes a small project (plans, specs) under ``tmp_path`` and validates it; one test
per error and per warning of ADR 0013, plus the clean case, waves and coverage, and one test
per architecture error and warning of ADR 0019 with the required / optional / absent modes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.core.config import ModelsConfig, PlanningConfig
from forge.core.errors import ConfigError, ExitCode, PreconditionError
from forge.core.output import Level
from forge.core.plan import (
    PlanValidation,
    check_plan_tier,
    load_roadmap,
    validate_plan,
)
from forge.core.profile import AgentProfile
from forge.core.team import Member, Team
from forge.core.tiers import Tier
from forge.core.zones import ZonesConfig, parse_zones

ZONES = parse_zones(
    {
        "zones": {
            "api": {"paths": ["src/api/**"]},
            "api-v2": {"paths": ["src/api/v2/**"]},
            "web": {"paths": ["src/web/**"]},
            "db": {"paths": ["src/db/**"]},
            "shared": {"paths": ["shared/**"], "contract": True},
        },
        "conflicts": [["web", "db"]],
    },
    ".forge/zones.yml",
)

SPEC = "specs/m1/editor.md"


def spec_text(
    *,
    milestone: str = "M1",
    status: str = "ready",
    acs: tuple[str, ...] = ("AC-1", "AC-2", "AC-3"),
    questions: str = "- Storage? RESOLVED: postgres\n",
    extra: str = "",
) -> str:
    """A spec file with the given frontmatter, ACs and open questions."""
    reqs = "".join(f"- **{ac}** something.\n" for ac in acs)
    return (
        f"---\nmilestone: {milestone}\nstatus: {status}\n{extra}---\n# Editor\n"
        f"## Requirements\n{reqs}## Open questions\n{questions}"
    )


def task(n: int, **overrides: Any) -> dict[str, Any]:
    """A valid plan task ``M1-T0<n>``; ``overrides`` replace fields (``None`` deletes one)."""
    data: dict[str, Any] = {
        "id": f"M1-T{n:02d}",
        "title": f"Task {n}",
        "type": "impl",
        "spec": SPEC,
        "covers": [],
        "depends_on": [],
        "zones": [],
        "size": "S",
        "tier": "standard",
        "description": f"Do thing {n}.\n",
        "done_when": ["tests pass"],
    }
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def clean_tasks() -> list[dict[str, Any]]:
    """Contract-first plan: T01 (shared) then T02 (api) and T03 (web) in parallel."""
    return [
        task(1, type="contract", zones=["shared"], covers=["AC-1"]),
        task(2, zones=["api"], covers=["AC-2"], depends_on=["M1-T01"]),
        task(3, zones=["web"], covers=["AC-3"], depends_on=["M1-T01"]),
    ]


class Project:
    """A repository root on disk with plans and specs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.write(SPEC, spec_text())

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def plan(self, tasks: list[dict[str, Any]], milestone: str = "M1", **top: Any) -> None:
        data: dict[str, Any] = {"milestone": milestone, "tasks": tasks, **top}
        self.write(f"plans/{milestone}.yml", yaml.safe_dump(data, sort_keys=False))

    def validate(
        self,
        milestone: str = "M1",
        *,
        zones: ZonesConfig = ZONES,
        team: Team | None = None,
        planning: PlanningConfig | None = None,
    ) -> PlanValidation:
        return validate_plan(self.root, milestone, zones=zones, team=team, planning=planning)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)


def codes(issues: tuple[Any, ...]) -> list[str]:
    return [i.code for i in issues]


def only(issues: tuple[Any, ...], code: str) -> Any:
    found = [i for i in issues if i.code == code]
    assert len(found) == 1, issues
    return found[0]


# --------------------------------------------------------------------------- clean plan


def test_clean_plan_is_ok_with_waves_and_coverage(project: Project) -> None:
    project.plan(clean_tasks())
    result = project.validate()
    assert result.ok, result.errors
    assert result.warnings == ()
    assert result.waves == (("M1-T01",), ("M1-T02", "M1-T03"))
    assert dict(result.coverage) == {
        "AC-1": ("M1-T01",),
        "AC-2": ("M1-T02",),
        "AC-3": ("M1-T03",),
    }
    assert result.plan is not None
    first = result.plan.tasks[0]
    assert first.title == "Task 1"
    assert first.description == "Do thing 1.\n"
    assert first.meta.milestone == "M1"
    assert first.meta.type.value == "contract"
    assert result.plan.find("M1-T02") is not None
    assert result.roadmap is None


def test_to_data_shape(project: Project) -> None:
    project.plan(clean_tasks())
    data = project.validate().to_data()
    assert data["ok"] is True
    assert data["file"] == "plans/M1.yml"
    assert data["tasks"] == ["M1-T01", "M1-T02", "M1-T03"]
    assert data["specs"] == [SPEC]
    assert data["waves"] == [["M1-T01"], ["M1-T02", "M1-T03"]]
    assert data["coverage"]["AC-2"] == ["M1-T02"]
    assert data["errors"] == [] and data["warnings"] == []
    assert data["roadmap"] == {"present": False, "milestone": None}
    assert data["counts"] == {"tasks": 3, "errors": 0, "warnings": 0}


def test_empty_task_list_is_valid_but_uncovered(project: Project) -> None:
    project.plan([])
    result = project.validate()
    assert result.ok
    assert result.waves == ()
    # No task references a spec and there is no roadmap: no milestone specs, no ACs.
    assert dict(result.coverage) == {}


# --------------------------------------------------------------------------- file errors


def test_invalid_milestone_argument(project: Project) -> None:
    result = project.validate("../etc")
    assert codes(result.errors) == ["invalid_milestone"]
    assert result.plan is None


def test_plan_missing(project: Project) -> None:
    result = project.validate()
    issue = only(result.errors, "plan_missing")
    assert issue.file == "plans/M1.yml"
    assert result.plan is None


@pytest.mark.parametrize("text", ["milestone: [M1\n", "- a\n", ""])
def test_plan_invalid_yaml(project: Project, text: str) -> None:
    project.write("plans/M1.yml", text)
    result = project.validate()
    issue = only(result.errors, "plan_invalid")
    assert issue.file == "plans/M1.yml" and issue.hint
    assert result.plan is None


def test_unknown_top_level_field(project: Project) -> None:
    project.plan(clean_tasks(), owner="alice")
    issue = only(project.validate().errors, "unknown_field")
    assert issue.field == "owner" and issue.task is None


def test_missing_top_level_field(project: Project) -> None:
    project.write("plans/M1.yml", "milestone: M1\n")
    issue = only(project.validate().errors, "missing_field")
    assert issue.field == "tasks"
    assert "tasks:" in issue.hint


def test_milestone_mismatch(project: Project) -> None:
    project.plan(clean_tasks())
    text = (project.root / "plans/M1.yml").read_text().replace("milestone: M1", "milestone: M2")
    project.write("plans/M1.yml", text)
    issue = only(project.validate().errors, "milestone_mismatch")
    assert issue.field == "milestone" and issue.file == "plans/M1.yml"


def test_tasks_not_a_list(project: Project) -> None:
    project.write("plans/M1.yml", "milestone: M1\ntasks: nope\n")
    issue = only(project.validate().errors, "invalid_field")
    assert issue.field == "tasks"


# --------------------------------------------------------------------------- task fields


def test_unknown_task_field_with_suggestion(project: Project) -> None:
    project.plan([task(1, zone=["api"])])
    issue = only(project.validate().errors, "unknown_field")
    assert (issue.task, issue.field, issue.file) == ("M1-T01", "zone", "plans/M1.yml")
    assert "zones" in issue.hint


@pytest.mark.parametrize(
    "missing",
    [
        "id",
        "title",
        "type",
        "spec",
        "covers",
        "depends_on",
        "zones",
        "size",
        "tier",
        "description",
        "done_when",
    ],
)
def test_missing_required_task_field(project: Project, missing: str) -> None:
    project.plan([task(1, **{missing: None})])
    result = project.validate()
    issue = only(result.errors, "missing_field")
    assert issue.field.endswith(missing)
    if missing == "id":
        assert issue.task is None and issue.field == "tasks[0].id"
    else:
        assert issue.task == "M1-T01"


def test_allow_contract_change_is_optional(project: Project) -> None:
    project.plan([task(1, covers=["AC-1", "AC-2", "AC-3"])])
    assert project.validate().ok


@pytest.mark.parametrize("bad_id", ["T1", "M1-1", "M2-T01", "m1-T01"])
def test_invalid_task_id(project: Project, bad_id: str) -> None:
    project.plan([task(1, id=bad_id)])
    issue = only(project.validate().errors, "invalid_id")
    assert issue.field == "id" and "M1-T<n>" in issue.hint


def test_non_string_id(project: Project) -> None:
    project.plan([task(1, id=7)])
    issue = only(project.validate().errors, "invalid_id")
    assert issue.field == "tasks[0].id"


def test_duplicate_id(project: Project) -> None:
    project.plan([task(1), task(1, title="again")])
    issue = only(project.validate().errors, "duplicate_id")
    assert issue.task == "M1-T01"


@pytest.mark.parametrize("size", ["L", "XL", "l"])
def test_size_l_must_be_split(project: Project, size: str) -> None:
    project.plan([task(1, size=size)])
    issue = only(project.validate().errors, "size_too_large")
    assert issue.field == "size" and "split the task" in issue.hint


def test_invalid_field_value(project: Project) -> None:
    project.plan([task(1, tier="huge")])
    issue = only(project.validate().errors, "invalid_field")
    assert issue.task == "M1-T01" and issue.field == "tier"


@pytest.mark.parametrize(
    ("key", "value", "field"),
    [("title", "  ", "title"), ("description", 3, "description"), ("size", 2, "size")],
)
def test_invalid_title_description_size_type(
    project: Project, key: str, value: Any, field: str
) -> None:
    project.plan([task(1, **{key: value})])
    issue = only(project.validate().errors, "invalid_field")
    assert issue.field == field


def test_description_with_a_forge_marker_line_is_an_error(project: Project) -> None:
    project.plan([task(1, description="intro\n<!-- forge:begin -->\nmore\n")])
    issue = only(project.validate().errors, "invalid_field")
    assert issue.field == "description" and "<!-- forge:begin -->" in issue.message


def test_several_problems_in_one_task_are_all_reported(project: Project) -> None:
    project.plan([task(1, size="L", extra=1, tier=None)])
    assert sorted(codes(project.validate().errors)) == [
        "missing_field",
        "size_too_large",
        "unknown_field",
    ]


def test_broken_task_does_not_cascade_to_dependents(project: Project) -> None:
    project.plan([task(1, size="L"), task(2, depends_on=["M1-T01"])])
    assert codes(project.validate().errors) == ["size_too_large"]


# --------------------------------------------------------------------------- zones and graph


def test_unknown_zone(project: Project) -> None:
    project.plan([task(1, zones=["api", "mobile"])])
    issue = only(project.validate().errors, "unknown_zone")
    assert (issue.task, issue.field) == ("M1-T01", "zones[1]")
    assert ".forge/zones.yml" in issue.hint


def test_unknown_dependency(project: Project) -> None:
    project.plan([task(1, depends_on=["M1-T09"])])
    issue = only(project.validate().errors, "unknown_dependency")
    assert (issue.task, issue.field) == ("M1-T01", "depends_on[0]")


def test_dependency_cycle(project: Project) -> None:
    project.plan(
        [
            task(1, depends_on=["M1-T03"]),
            task(2, depends_on=["M1-T01"]),
            task(3, depends_on=["M1-T02"]),
        ]
    )
    result = project.validate()
    issue = only(result.errors, "dependency_cycle")
    assert "M1-T01 -> M1-T03 -> M1-T02 -> M1-T01" in issue.message
    assert result.waves == ()


def test_cross_milestone_dependency_resolves_and_is_satisfied(project: Project) -> None:
    project.write("plans/M0.yml", yaml.safe_dump({"milestone": "M0", "tasks": [{"id": "M0-T01"}]}))
    tasks = clean_tasks()
    tasks[0]["depends_on"] = ["M0-T01"]
    project.plan(tasks)
    result = project.validate()
    assert result.ok, result.errors
    assert result.waves == (("M1-T01",), ("M1-T02", "M1-T03"))


def test_cross_milestone_dependency_unknown(project: Project) -> None:
    project.write("plans/M0.yml", "milestone: M0\ntasks: []\n")
    project.write("plans/M5.yml", "not: [valid\n")
    project.plan([task(1, depends_on=["M0-T07"])])
    issue = only(project.validate().errors, "unknown_dependency")
    assert "plans/M0.yml" in issue.hint


def test_same_milestone_ids_in_other_files_do_not_count(project: Project) -> None:
    project.write("plans/old.yml", yaml.safe_dump({"tasks": [{"id": "M1-T09"}]}))
    project.plan([task(1, depends_on=["M1-T09"])])
    assert "unknown_dependency" in codes(project.validate().errors)


# --------------------------------------------------------------------------- specs


def test_spec_missing(project: Project) -> None:
    project.plan([task(1, spec="specs/m1/none.md")])
    issue = only(project.validate().errors, "spec_missing")
    assert issue.file == "specs/m1/none.md" and "M1-T01" in issue.message


def test_spec_invalid_frontmatter(project: Project) -> None:
    project.write(SPEC, "# no frontmatter\n- AC-1 x\n")
    project.plan([task(1)])
    issue = only(project.validate().errors, "spec_invalid")
    assert issue.file == SPEC and issue.hint


def test_spec_milestone_mismatch(project: Project) -> None:
    project.write(SPEC, spec_text(milestone="M2"))
    project.plan(clean_tasks())
    issue = only(project.validate().errors, "spec_milestone_mismatch")
    assert issue.field == "milestone"


def test_spec_draft(project: Project) -> None:
    project.write(SPEC, spec_text(status="draft"))
    project.plan(clean_tasks())
    issue = only(project.validate().errors, "spec_draft")
    assert issue.file == SPEC and issue.field == "status"


def test_spec_unresolved_open_questions(project: Project) -> None:
    project.write(SPEC, spec_text(questions="- Storage? RESOLVED\n- Auth provider?\n"))
    project.plan(clean_tasks())
    issue = only(project.validate().errors, "spec_open_questions")
    assert "Auth provider?" in issue.message


def test_spec_template_placeholder_is_unresolved(project: Project) -> None:
    placeholder = "- (must be empty, or every item marked RESOLVED, for status: ready)\n"
    project.write(SPEC, spec_text(questions=placeholder))
    project.plan(clean_tasks())
    only(project.validate().errors, "spec_open_questions")


def test_spec_errors_reported_once_per_spec(project: Project) -> None:
    project.write(SPEC, spec_text(status="draft"))
    project.plan(clean_tasks())
    issue = only(project.validate().errors, "spec_draft")
    assert "M1-T01, M1-T02, M1-T03" in issue.message


def test_spec_unknown_key_is_a_warning(project: Project) -> None:
    project.write(SPEC, spec_text(extra="reviewers: [bob]\n"))
    project.plan(clean_tasks())
    result = project.validate()
    assert result.ok
    issue = only(result.warnings, "spec_unknown_key")
    assert (issue.file, issue.field) == (SPEC, "reviewers")


# --------------------------------------------------------------------------- ACs


def test_unknown_ac(project: Project) -> None:
    tasks = clean_tasks()
    tasks[1]["covers"] = ["AC-2", "AC-7"]
    project.plan(tasks)
    issue = only(project.validate().errors, "unknown_ac")
    assert (issue.task, issue.field) == ("M1-T02", "covers[1]")


def test_duplicate_ac_across_milestone_specs(project: Project) -> None:
    project.write("specs/m1/other.md", spec_text(acs=("AC-3", "AC-4")))
    tasks = clean_tasks()
    tasks.append(task(4, spec="specs/m1/other.md", covers=["AC-4"], zones=["db"]))
    project.plan(tasks)
    issue = only(project.validate().errors, "duplicate_ac")
    assert issue.field == "AC-3"
    assert SPEC in issue.message and "specs/m1/other.md" in issue.message


def test_uncovered_ac_warning(project: Project) -> None:
    tasks = clean_tasks()
    tasks[2]["covers"] = []
    project.plan(tasks)
    result = project.validate()
    assert result.ok
    issue = only(result.warnings, "uncovered_ac")
    assert (issue.file, issue.field) == (SPEC, "AC-3")
    assert result.coverage["AC-3"] == ()


# --------------------------------------------------------------------------- contract zones


def test_contract_zone_needs_contract_type(project: Project) -> None:
    tasks = clean_tasks()
    tasks[1]["zones"] = ["api", "shared"]
    project.plan(tasks)
    issue = only(project.validate().errors, "contract_zone")
    assert issue.task == "M1-T02" and "shared" in issue.message


def test_contract_zone_allowed_with_flag(project: Project) -> None:
    tasks = clean_tasks()
    tasks[1]["zones"] = ["api", "shared"]
    tasks[1]["allow_contract_change"] = True
    project.plan(tasks)
    assert "contract_zone" not in codes(project.validate().errors)


# --------------------------------------------------------------------------- wave warnings


@pytest.mark.parametrize(
    ("zones_a", "zones_b", "why"),
    [
        (["api"], ["api"], "shared zone api"),
        (["web"], ["db"], "declared conflict"),
        (["api"], ["api-v2"], "globs intersect"),
    ],
)
def test_zone_overlap_in_same_wave(
    project: Project, zones_a: list[str], zones_b: list[str], why: str
) -> None:
    project.plan(
        [
            task(1, zones=zones_a, covers=["AC-1", "AC-2"]),
            task(2, zones=zones_b, covers=["AC-3"]),
        ]
    )
    result = project.validate()
    assert result.ok
    issue = only(result.warnings, "zone_overlap")
    assert why in issue.message and "dependency" in issue.hint


def test_overlap_in_different_waves_is_fine(project: Project) -> None:
    project.plan(
        [
            task(1, zones=["api"], covers=["AC-1", "AC-2"]),
            task(2, zones=["api"], covers=["AC-3"], depends_on=["M1-T01"]),
        ]
    )
    assert project.validate().warnings == ()


def _team(devs: int, leads: int = 0) -> Team:
    members = [Member(f"dev{i}", f"dev{i}", frozenset({"dev"})) for i in range(devs)]
    members += [Member(f"lead{i}", f"lead{i}", frozenset({"lead"})) for i in range(leads)]
    return Team(members=tuple(members))


def test_low_parallelism_warning(project: Project) -> None:
    project.plan(clean_tasks())
    result = project.validate(team=_team(2, leads=3))
    issue = only(result.warnings, "low_parallelism")
    assert "wave 1 has 1 task(s) for 2" in issue.message


def test_low_parallelism_skipped_without_team(project: Project) -> None:
    project.plan(clean_tasks())
    assert project.validate(team=None).warnings == ()


# --------------------------------------------------------------------------- roadmap


ROADMAP = """\
milestones:
  - id: M0
    title: Foundations
  - id: M1
    title: Editor base
    goal: edit pages
    demo: create and save a page
    lead: alice
    specs: [specs/m1/editor.md, specs/m1/extra.md]
"""


def test_roadmap_missing_is_fine(project: Project) -> None:
    project.plan(clean_tasks())
    assert load_roadmap(project.root) is None
    assert project.validate().ok


def test_roadmap_specs_count_as_milestone_specs(project: Project) -> None:
    project.write("plans/roadmap.yml", ROADMAP)
    project.write("specs/m1/extra.md", spec_text(acs=("AC-9",)))
    project.plan(clean_tasks())
    result = project.validate()
    assert result.ok, result.errors
    assert result.roadmap is not None
    entry = result.roadmap.find("M1")
    assert entry is not None and entry.goal == "edit pages"
    assert result.coverage["AC-9"] == ()
    only(result.warnings, "uncovered_ac")
    assert result.to_data()["roadmap"]["milestone"]["title"] == "Editor base"


def test_roadmap_spec_missing(project: Project) -> None:
    project.write("plans/roadmap.yml", ROADMAP)
    project.plan(clean_tasks())
    issue = only(project.validate().errors, "spec_missing")
    assert issue.file == "specs/m1/extra.md" and "roadmap" in issue.message


def test_roadmap_without_milestone(project: Project) -> None:
    project.write("plans/roadmap.yml", "milestones:\n  - id: M0\n    title: Foundations\n")
    project.plan(clean_tasks())
    issue = only(project.validate().errors, "roadmap_missing_milestone")
    assert issue.file == "plans/roadmap.yml"


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("milestones:\n  - id: M1\n    title: x\n    owner: bob\n", "milestones[0].owner"),
        ("milestones:\n  - id: X1\n    title: x\n", "milestones[0].id"),
        ("milestones:\n  - id: M1\n", "milestones[0].title"),
        ("milestones:\n  - {id: M1, title: a}\n  - {id: M1, title: b}\n", "milestones[1].id"),
        ("milestones:\n  - {id: M1, title: a, specs: [/abs.md]}\n", "milestones[0].specs[0]"),
        ("phases: []\n", "phases"),
        ("other: 1\nmilestones: []\n", "other"),
    ],
)
def test_roadmap_invalid(project: Project, text: str, key: str) -> None:
    project.write("plans/roadmap.yml", text)
    with pytest.raises(ConfigError) as info:
        load_roadmap(project.root)
    assert info.value.key == key
    assert info.value.file == "plans/roadmap.yml"
    project.plan(clean_tasks())
    issue = only(project.validate().errors, "roadmap_invalid")
    assert issue.field == key and issue.hint


# --------------------------------------------------------------------------- tier check


STRONG = AgentProfile("opus", "claude-code", ("anthropic",), {Tier.STRONG: "opus"})
LIGHT = AgentProfile("small", "ollama", ("local",), {Tier.LIGHT: "qwen"})
NEEDS_STRONG = ModelsConfig(min_tier_for={"plan": Tier.STRONG})


def test_tier_no_minimum_is_silent() -> None:
    assert check_plan_tier(ModelsConfig(), LIGHT) == []


def test_tier_served() -> None:
    assert check_plan_tier(NEEDS_STRONG, STRONG) == []


def test_tier_refused_with_exit_5() -> None:
    with pytest.raises(PreconditionError) as info:
        check_plan_tier(NEEDS_STRONG, LIGHT)
    assert info.value.code == ExitCode.PRECONDITION
    assert "--force" in (info.value.hint or "")


def test_tier_forced_is_a_warning() -> None:
    [message] = check_plan_tier(NEEDS_STRONG, LIGHT, force=True)
    assert message.level == Level.WARNING and "--force" in message.text


@pytest.mark.parametrize(
    ("agent", "ci", "why"), [(None, False, "no Forge profile"), (LIGHT, True, "GitHub Actions")]
)
def test_tier_not_checked_without_profile_or_in_ci(
    agent: AgentProfile | None, ci: bool, why: str
) -> None:
    [message] = check_plan_tier(NEEDS_STRONG, agent, ci=ci)
    assert message.level == Level.INFO and why in message.text


# --------------------------------------------------------------------------- review fixes


def test_ac_defined_twice_in_one_spec(project: Project) -> None:
    project.write(SPEC, spec_text(acs=("AC-1", "AC-2", "AC-3", "AC-2")))
    project.plan(clean_tasks())
    issue = only(project.validate().errors, "duplicate_ac")
    assert (issue.file, issue.field) == (SPEC, "AC-2")
    assert "more than once" in issue.message


def test_ac_quoted_in_code_is_not_a_second_definition(project: Project) -> None:
    project.write(SPEC, spec_text() + "```\n- **AC-1** example\n```\n")
    project.plan(clean_tasks())
    assert "duplicate_ac" not in codes(project.validate().errors)


@pytest.mark.parametrize("title", ["Line one\nline two", "a\r\nb"])
def test_multiline_title_is_rejected(project: Project, title: str) -> None:
    tasks = clean_tasks()
    tasks[0]["title"] = title
    project.plan(tasks)
    issue = only(project.validate().errors, "invalid_field")
    assert (issue.task, issue.field) == ("M1-T01", "title")


def test_title_longer_than_github_allows_is_rejected(project: Project) -> None:
    tasks = clean_tasks()
    prefix = len("[M1-T01] ")
    tasks[0]["title"] = "x" * (256 - prefix)  # exactly 256 with the prefix: allowed
    tasks[1]["title"] = "x" * (257 - prefix)
    project.plan(tasks)
    issue = only(project.validate().errors, "invalid_field")
    assert (issue.task, issue.field) == ("M1-T02", "title")
    assert "256" in issue.message


def test_proposal_field(project: Project) -> None:
    tasks = clean_tasks()
    tasks[0]["proposal"] = 42
    project.plan(tasks)
    result = project.validate()
    assert result.ok, result.errors
    assert result.plan is not None
    assert [t.proposal for t in result.plan.tasks] == [42, None, None]


@pytest.mark.parametrize("value", [0, -3, "42", True, 1.5])
def test_proposal_must_be_a_positive_issue_number(project: Project, value: Any) -> None:
    tasks = clean_tasks()
    tasks[0]["proposal"] = value
    project.plan(tasks)
    issue = only(project.validate().errors, "invalid_field")
    assert (issue.task, issue.field) == ("M1-T01", "proposal")


def test_proposal_adopted_twice_is_an_error(project: Project) -> None:
    tasks = clean_tasks()
    tasks[0]["proposal"] = 7
    tasks[2]["proposal"] = 7
    project.plan(tasks)
    issue = only(project.validate().errors, "duplicate_proposal")
    assert (issue.task, issue.field) == ("M1-T03", "proposal")


# --------------------------------------------------------------------------- architecture

ARCH = "docs/architecture/M1.md"
OVERVIEW = "docs/architecture/overview.md"
REQUIRED = PlanningConfig(architecture="required")
OPTIONAL = PlanningConfig(architecture="optional")
NO_ARCH_INFO = "no architecture for M1 (planning.architecture: optional)"

ARCH_BODY = """\
# M1 architecture
## Context
x
## Components
x
## Interfaces
x
## Data model
x
## Decisions
x
## Risks
x
## Open questions
"""


def arch_front(**overrides: Any) -> dict[str, Any]:
    """A valid M1 architecture: api-svc produces IF-schema (zone shared) for web-app."""
    data: dict[str, Any] = {
        "milestone": "M1",
        "status": "ready",
        "specs": [SPEC],
        "components": [
            {"id": "api-svc", "zones": ["api"], "summary": "API"},
            {"id": "web-app", "zones": ["web"], "summary": "Web"},
        ],
        "interfaces": [
            {
                "id": "IF-schema",
                "zone": "shared",
                "producer": "api-svc",
                "consumers": ["web-app"],
                "summary": "Schema",
            }
        ],
    }
    data.update(overrides)
    return data


def overview_front() -> dict[str, Any]:
    """An overview with an existing component (db-svc) and interface (IF-legacy)."""
    return {
        "milestones": ["M0"],
        "components": [{"id": "db-svc", "zones": ["db"], "summary": "DB"}],
        "interfaces": [
            {
                "id": "IF-legacy",
                "zone": "shared",
                "producer": "db-svc",
                "consumers": [],
                "summary": "Legacy",
            }
        ],
    }


def write_arch(project: Project, front: dict[str, Any] | None = None) -> None:
    text = yaml.safe_dump(arch_front() if front is None else front, sort_keys=False)
    project.write(ARCH, f"---\n{text}---\n{ARCH_BODY}")


def write_overview(project: Project) -> None:
    text = yaml.safe_dump(overview_front(), sort_keys=False)
    project.write(OVERVIEW, f"---\n{text}---\n# System\n")


def arch_tasks() -> list[dict[str, Any]]:
    """The clean plan with its architecture fields: T01 implements IF-schema, T02/T03 use it."""
    t1, t2, t3 = clean_tasks()
    t1.update(component="api-svc", implements=["IF-schema"])
    t2.update(component="api-svc", uses=["IF-schema"])
    t3.update(component="web-app", uses=["IF-schema"])
    return [t1, t2, t3]


def checked(project: Project, tasks: list[dict[str, Any]], **arch: Any) -> PlanValidation:
    """Write the architecture and ``tasks`` and validate in required mode."""
    write_arch(project, arch_front(**arch))
    project.plan(tasks)
    return project.validate(planning=REQUIRED)


def test_architecture_checked_clean_plan(project: Project) -> None:
    result = checked(project, arch_tasks())
    assert result.ok, result.errors
    assert result.warnings == ()
    assert result.messages == ()
    assert result.architecture.to_data() == {
        "mode": "required",
        "path": ARCH,
        "present": True,
        "status": "ready",
        "checked": True,
        "interfaces": {"IF-schema": "M1-T01"},
    }
    assert result.to_data()["architecture"]["interfaces"] == {"IF-schema": "M1-T01"}
    assert result.plan is not None
    first = result.plan.tasks[0]
    assert (first.component, first.implements, first.uses) == ("api-svc", ("IF-schema",), ())


def test_required_without_architecture_fails(project: Project) -> None:
    project.plan(clean_tasks())
    result = project.validate(planning=REQUIRED)
    # No other architecture check runs: no missing component errors.
    assert codes(result.errors) == ["architecture_missing"]
    issue = result.errors[0]
    assert issue.file == ARCH and "/forge-architect M1" in issue.hint
    assert result.messages == ()
    assert result.architecture.checked and not result.architecture.present


def test_planning_key_absent_without_architecture_passes(project: Project) -> None:
    project.plan(clean_tasks())
    result = project.validate()  # no `planning`: the default is optional
    assert result.ok and result.warnings == ()
    assert [(m.level, m.text) for m in result.messages] == [(Level.INFO, NO_ARCH_INFO)]
    assert result.architecture.to_data() == {
        "mode": "optional",
        "path": ARCH,
        "present": False,
        "status": None,
        "checked": False,
        "interfaces": {},
    }
    assert PlanningConfig().architecture == "optional"


def test_optional_without_architecture_passes_with_info(project: Project) -> None:
    project.plan(clean_tasks())
    result = project.validate(planning=OPTIONAL)
    assert result.ok and result.warnings == ()
    assert [m.text for m in result.messages] == [NO_ARCH_INFO]


@pytest.mark.parametrize("text", [None, "milestone: [M1\n"])
def test_optional_info_emitted_when_plan_does_not_load(project: Project, text: str | None) -> None:
    if text is not None:
        project.write("plans/M1.yml", text)
    result = project.validate(planning=OPTIONAL)
    assert result.plan is None and not result.ok
    assert [(m.level, m.text) for m in result.messages] == [(Level.INFO, NO_ARCH_INFO)]
    assert not result.architecture.checked


def test_required_without_loadable_plan_has_no_info(project: Project) -> None:
    project.write("plans/M1.yml", "milestone: [M1\n")
    result = project.validate(planning=REQUIRED)
    assert result.plan is None and result.messages == ()


def test_optional_with_architecture_is_checked(project: Project) -> None:
    write_arch(project)
    project.plan(clean_tasks())  # no component: an error once the file exists
    result = project.validate(planning=OPTIONAL)
    assert result.architecture.checked
    assert result.messages == ()
    assert {i.code for i in result.errors} >= {"missing_field"}


def test_architecture_fields_unchecked_warning(project: Project) -> None:
    project.plan(arch_tasks())
    result = project.validate(planning=OPTIONAL)
    assert result.ok
    issue = only(result.warnings, "architecture_fields_unchecked")
    assert "M1-T01, M1-T02, M1-T03" in issue.message
    assert [m.text for m in result.messages] == [NO_ARCH_INFO]


def test_architecture_invalid_relays_errors_with_code_prefix(project: Project) -> None:
    components = arch_front()["components"]
    components[0]["zones"] = ["nowhere"]
    result = checked(project, arch_tasks(), components=components)
    issue = only(result.errors, "architecture_invalid")
    assert issue.message.startswith("[unknown_zone] ")
    assert issue.file == ARCH and issue.field is not None and issue.hint
    assert issue.task is None


def test_architecture_warnings_are_not_relayed(project: Project) -> None:
    # No overview.md: `forge arch validate` warns overview_stale; plan validate does not.
    result = checked(project, arch_tasks())
    assert result.warnings == ()


def test_architecture_unreadable_frontmatter_skips_task_checks(project: Project) -> None:
    front = arch_front()
    del front["interfaces"]
    write_arch(project, front)
    tasks = arch_tasks()
    del tasks[0]["component"]  # would be missing_field if the task checks ran
    project.plan(tasks)
    result = project.validate(planning=REQUIRED)
    assert codes(result.errors) == ["architecture_invalid"]
    assert result.errors[0].message.startswith("[missing_field] ")
    assert result.architecture.interfaces == {}


def test_architecture_draft_still_runs_task_checks(project: Project) -> None:
    tasks = arch_tasks()
    tasks[2]["component"] = "ghost"
    result = checked(project, tasks, status="draft")
    issue = only(result.errors, "architecture_draft")
    assert (issue.file, issue.field) == (ARCH, "status")
    assert "unknown_component" in codes(result.errors)


def test_component_required_when_checked(project: Project) -> None:
    tasks = arch_tasks()
    del tasks[1]["component"]
    result = checked(project, tasks)
    issue = only(result.errors, "missing_field")
    assert (issue.task, issue.field) == ("M1-T02", "component")


def test_unknown_component(project: Project) -> None:
    tasks = arch_tasks()
    tasks[2]["component"] = "ghost"
    issue = only(checked(project, tasks).errors, "unknown_component")
    assert (issue.task, issue.field) == ("M1-T03", "component")


def test_component_and_interface_from_overview(project: Project) -> None:
    write_overview(project)
    tasks = arch_tasks()
    tasks.append(
        task(4, zones=["db"], component="db-svc", uses=["IF-legacy"], depends_on=["M1-T01"])
    )
    result = checked(project, tasks)
    assert result.ok, result.errors
    # IF-legacy is existing: not implementable, exempt from uses_without_dependency.
    assert result.architecture.interfaces == {"IF-schema": "M1-T01"}


def test_unknown_interface_in_implements(project: Project) -> None:
    write_overview(project)
    tasks = arch_tasks()
    tasks[0]["implements"] = ["IF-schema", "IF-legacy"]  # IF-legacy is only in overview.md
    issue = only(checked(project, tasks).errors, "unknown_interface")
    assert (issue.task, issue.field) == ("M1-T01", "implements[1]")
    assert "declare it in" in issue.hint


def test_unknown_interface_in_uses(project: Project) -> None:
    tasks = arch_tasks()
    tasks[2]["uses"] = ["IF-schema", "IF-ghost"]
    issue = only(checked(project, tasks).errors, "unknown_interface")
    assert (issue.task, issue.field) == ("M1-T03", "uses[1]")


def test_interface_unimplemented(project: Project) -> None:
    interfaces = arch_front()["interfaces"]
    interfaces.append({**interfaces[0], "id": "IF-events"})
    result = checked(project, arch_tasks(), interfaces=interfaces)
    issue = only(result.errors, "interface_unimplemented")
    assert issue.field == "IF-events" and issue.file == "plans/M1.yml"
    assert result.architecture.interfaces == {"IF-schema": "M1-T01", "IF-events": None}


def test_interface_multiply_implemented(project: Project) -> None:
    tasks = arch_tasks()
    tasks.append(
        task(4, type="contract", zones=["shared"], component="api-svc", implements=["IF-schema"])
    )
    issue = only(checked(project, tasks).errors, "interface_multiply_implemented")
    assert "M1-T01, M1-T04" in issue.message and issue.field == "IF-schema"


def test_contract_without_interface(project: Project) -> None:
    tasks = arch_tasks()
    del tasks[0]["implements"]
    result = checked(project, tasks)
    issue = only(result.errors, "contract_without_interface")
    assert (issue.task, issue.field) == ("M1-T01", "implements")


def test_uses_without_dependency(project: Project) -> None:
    tasks = arch_tasks()
    tasks[2]["depends_on"] = []
    issue = only(checked(project, tasks).errors, "uses_without_dependency")
    assert (issue.task, issue.field) == ("M1-T03", "uses[0]")
    assert "M1-T01" in issue.hint


def test_uses_with_transitive_dependency_is_fine(project: Project) -> None:
    tasks = arch_tasks()
    tasks[2]["depends_on"] = ["M1-T02"]  # T03 -> T02 -> T01
    tasks[2]["covers"] = ["AC-3"]
    result = checked(project, tasks)
    assert "uses_without_dependency" not in codes(result.errors)


def test_uses_by_its_own_implementer_is_exempt(project: Project) -> None:
    tasks = arch_tasks()
    tasks[0]["uses"] = ["IF-schema"]
    assert "uses_without_dependency" not in codes(checked(project, tasks).errors)


def test_uses_without_dependency_skipped_on_cycle_or_no_implementer(project: Project) -> None:
    tasks = arch_tasks()
    tasks[0]["depends_on"] = ["M1-T02"]  # cycle T01 <-> T02
    tasks[2]["depends_on"] = []
    result = checked(project, tasks)
    assert "dependency_cycle" in codes(result.errors)
    assert "uses_without_dependency" not in codes(result.errors)

    tasks = arch_tasks()
    del tasks[0]["implements"]
    tasks[2]["depends_on"] = []
    result = checked(project, tasks)
    assert "interface_unimplemented" in codes(result.errors)
    assert "uses_without_dependency" not in codes(result.errors)


def test_zone_outside_component(project: Project) -> None:
    tasks = arch_tasks()
    tasks[2]["zones"] = ["web", "db"]
    result = checked(project, tasks)
    assert result.ok
    issue = only(result.warnings, "zone_outside_component")
    assert (issue.task, issue.field) == ("M1-T03", "zones[1]")


def test_zone_of_used_interface_does_not_count(project: Project) -> None:
    tasks = arch_tasks()
    tasks[2].update(zones=["web", "shared"], allow_contract_change=True)
    issue = only(checked(project, tasks).warnings, "zone_outside_component")
    assert (issue.task, issue.field) == ("M1-T03", "zones[1]")


def test_zone_of_implemented_interface_counts(project: Project) -> None:
    # T01 touches `shared`, which is not a zone of api-svc but the zone of IF-schema.
    result = checked(project, arch_tasks())
    assert "zone_outside_component" not in codes(result.warnings)


def test_implementer_not_producer(project: Project) -> None:
    tasks = arch_tasks()
    tasks[0]["component"] = "web-app"
    result = checked(project, tasks)
    assert result.ok
    issue = only(result.warnings, "implementer_not_producer")
    assert (issue.task, issue.field) == ("M1-T01", "component")
    assert "api-svc" in issue.message


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"component": 5}, "component"),
        ({"component": "Bad_Id"}, "component"),
        ({"uses": "IF-schema"}, "uses"),
        ({"uses": ["if-schema"]}, "uses[0]"),
        ({"type": "contract", "zones": ["shared"], "implements": [3]}, "implements[0]"),
        ({"implements": ["IF-schema"]}, "implements"),  # not a contract task
    ],
)
def test_architecture_fields_format(
    project: Project, overrides: dict[str, Any], field: str
) -> None:
    project.plan([task(1, **overrides)])
    # Format rules apply in unchecked mode too.
    result = project.validate(planning=OPTIONAL)
    issue = only(result.errors, "invalid_field")
    assert (issue.task, issue.field) == ("M1-T01", field)


# --------------------------------------------------------------------------- skeleton (ADR 0020)

DETAIL = ("description", "done_when", "tier")


def skeleton_tasks() -> list[dict[str, Any]]:
    """The clean plan without any detail field."""
    return [{k: v for k, v in t.items() if k not in DETAIL} for t in clean_tasks()]


def test_detail_key_constants() -> None:
    from forge.core.plan import DETAIL_TASK_KEYS, SKELETON_TASK_KEYS, TASK_KEYS

    assert DETAIL_TASK_KEYS == DETAIL
    assert set(SKELETON_TASK_KEYS) | set(DETAIL_TASK_KEYS) == set(TASK_KEYS)
    assert SKELETON_TASK_KEYS == (
        "id",
        "title",
        "type",
        "spec",
        "covers",
        "depends_on",
        "zones",
        "size",
        "allow_contract_change",
        "proposal",
        "component",
        "implements",
        "uses",
    )


def test_skeleton_without_detail_fields_is_ok(project: Project) -> None:
    project.plan(skeleton_tasks())
    result = validate_plan(project.root, "M1", zones=ZONES, skeleton=True)
    assert result.ok, result.errors
    assert result.skeleton is True
    data = result.to_data()
    assert data["skeleton"] is True
    assert data["tasks"] == ["M1-T01", "M1-T02", "M1-T03"]
    assert data["waves"] == [["M1-T01"], ["M1-T02", "M1-T03"]]
    assert data["coverage"]["AC-2"] == ["M1-T02"]


def test_without_flag_the_detail_fields_are_required(project: Project) -> None:
    project.plan(skeleton_tasks())
    result = project.validate()
    assert result.skeleton is False and result.to_data()["skeleton"] is False
    missing = sorted((i.task, i.field) for i in result.errors if i.code == "missing_field")
    assert missing == sorted((f"M1-T0{n}", k) for n in (1, 2, 3) for k in DETAIL)


@pytest.mark.parametrize("key", DETAIL)
def test_skeleton_skips_each_missing_detail_field(project: Project, key: str) -> None:
    project.plan([task(1, type="contract", zones=["shared"], **{key: None})])
    assert validate_plan(project.root, "M1", zones=ZONES, skeleton=True).errors == ()


@pytest.mark.parametrize("key", DETAIL)
def test_skeleton_tolerates_null_detail_field(project: Project, key: str) -> None:
    data = task(1, type="contract", zones=["shared"])
    data[key] = None
    project.plan([data])
    assert validate_plan(project.root, "M1", zones=ZONES, skeleton=True).errors == ()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tier", "huge"),
        ("done_when", []),
        ("done_when", [""]),
        ("description", 42),
        ("description", "intro\n<!-- forge:begin -->\nmore\n"),
    ],
)
def test_skeleton_still_checks_present_detail_fields(
    project: Project, key: str, value: Any
) -> None:
    project.plan([task(1, type="contract", zones=["shared"], **{key: value})])
    result = validate_plan(project.root, "M1", zones=ZONES, skeleton=True)
    issue = only(result.errors, "invalid_field")
    assert issue.task == "M1-T01" and key in (issue.field or "")


@pytest.mark.parametrize("key", ["title", "type", "spec", "covers", "depends_on", "zones", "size"])
def test_skeleton_still_requires_skeleton_fields(project: Project, key: str) -> None:
    tasks = skeleton_tasks()
    del tasks[0][key]
    project.plan(tasks)
    result = validate_plan(project.root, "M1", zones=ZONES, skeleton=True)
    issue = only(result.errors, "missing_field")
    assert (issue.task, issue.field) == ("M1-T01", key)


def test_skeleton_runs_every_other_check(project: Project) -> None:
    tasks = skeleton_tasks()
    tasks[0]["owner"] = "alice"  # unknown_field
    tasks[1]["zones"] = ["nowhere"]  # unknown_zone
    tasks[2]["zones"] = ["shared"]  # contract_zone
    tasks[2]["depends_on"] = ["M1-T09"]  # unknown_dependency
    tasks.append({**skeleton_tasks()[1], "id": "M1-T04", "size": "L"})  # size_too_large
    tasks.append({**skeleton_tasks()[1], "id": "M1-T02"})  # duplicate_id
    project.plan(tasks)
    result = validate_plan(project.root, "M1", zones=ZONES, skeleton=True)
    assert set(codes(result.errors)) >= {
        "unknown_field",
        "unknown_zone",
        "contract_zone",
        "unknown_dependency",
        "size_too_large",
        "duplicate_id",
    }
    assert "missing_field" not in codes(result.errors)


def test_skeleton_still_detects_cycles(project: Project) -> None:
    tasks = skeleton_tasks()
    tasks[0]["depends_on"] = ["M1-T02"]
    project.plan(tasks)
    result = validate_plan(project.root, "M1", zones=ZONES, skeleton=True)
    assert "dependency_cycle" in codes(result.errors)
    assert result.waves == ()


def test_skeleton_still_warns(project: Project) -> None:
    tasks = skeleton_tasks()
    tasks[2]["zones"] = ["api"]
    project.plan(tasks)
    result = validate_plan(project.root, "M1", zones=ZONES, skeleton=True)
    assert result.ok and codes(result.warnings) == ["zone_overlap"]


def test_skeleton_runs_architecture_checks(project: Project) -> None:
    write_arch(project)
    tasks = [{k: v for k, v in t.items() if k not in DETAIL} for t in arch_tasks()]
    project.plan(tasks)
    ok = validate_plan(project.root, "M1", zones=ZONES, planning=REQUIRED, skeleton=True)
    assert ok.ok, ok.errors
    assert ok.architecture.interfaces == {"IF-schema": "M1-T01"}

    tasks[2]["component"] = "ghost"
    project.plan(tasks)
    bad = validate_plan(project.root, "M1", zones=ZONES, planning=REQUIRED, skeleton=True)
    assert "missing_field" not in codes(bad.errors)
    assert bad.errors and all(i.code != "missing_field" for i in bad.errors)


def test_skeleton_cannot_pass_a_draft_architecture(project: Project) -> None:
    write_arch(project, arch_front(status="draft"))
    tasks = [{k: v for k, v in t.items() if k not in DETAIL} for t in arch_tasks()]
    project.plan(tasks)
    result = validate_plan(project.root, "M1", zones=ZONES, planning=REQUIRED, skeleton=True)
    only(result.errors, "architecture_draft")


def test_skeleton_required_architecture_missing(project: Project) -> None:
    project.plan(skeleton_tasks())
    result = validate_plan(project.root, "M1", zones=ZONES, planning=REQUIRED, skeleton=True)
    assert codes(result.errors) == ["architecture_missing"]


def test_skeleton_plan_missing_and_invalid_milestone(project: Project) -> None:
    assert codes(validate_plan(project.root, "M7", zones=ZONES, skeleton=True).errors) == [
        "plan_missing"
    ]
    bad = validate_plan(project.root, "first", zones=ZONES, skeleton=True)
    assert codes(bad.errors) == ["invalid_milestone"] and bad.skeleton is True


def test_skeleton_on_a_full_plan_matches_the_full_validation(project: Project) -> None:
    project.plan(clean_tasks())
    full = project.validate().to_data()
    skel = validate_plan(project.root, "M1", zones=ZONES, skeleton=True).to_data()
    assert {**skel, "skeleton": False} == full
