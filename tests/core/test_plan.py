"""Tests for forge.core.plan: roadmap, plan files and the offline validation (ADR 0013).

Each test writes a small project (plans, specs) under ``tmp_path`` and validates it; one test
per error and per warning of ADR 0013, plus the clean case, waves and coverage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.core.config import ModelsConfig
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
        self, milestone: str = "M1", *, zones: ZonesConfig = ZONES, team: Team | None = None
    ) -> PlanValidation:
        return validate_plan(self.root, milestone, zones=zones, team=team)


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
