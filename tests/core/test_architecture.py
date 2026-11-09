"""Tests for forge.core.architecture: ``docs/architecture/<M>.md`` and ``overview.md``.

Each test writes a small project under ``tmp_path`` and validates it: one test per error and
per warning of ADR 0019 (for both files where the ADR says so), the clean case, the loaders
and the resolution helpers used by the plan validator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from forge.core.architecture import (
    ArchitectureValidation,
    architecture_path,
    component_zones,
    existing_interfaces,
    interface_producer,
    load_architecture,
    load_overview,
    milestone_interfaces,
    resolve_component,
    resolve_interface,
    validate_architecture,
)
from forge.core.errors import ConfigError
from forge.core.plan import PlanIssue
from forge.core.zones import parse_zones

ZONES = parse_zones(
    {
        "zones": {
            "app/auth": {"paths": ["app/auth/**"]},
            "app/web": {"paths": ["app/web/**"]},
            "contracts/api": {"paths": ["contracts/api/**"], "contract": True},
        }
    },
    ".forge/zones.yml",
)

SPEC = "specs/m1/auth.md"
ARCH = "docs/architecture/M1.md"
OVERVIEW = "docs/architecture/overview.md"

BODY = """\
# M1 architecture

## Context
Login.

## Components
Two components.

## Interfaces
One API.

## Data model
Users.

## Decisions
None.

## Risks
None.

## Open questions
- Token lifetime? RESOLVED: 1h
"""


def component(cid: str = "auth-service", **overrides: Any) -> dict[str, Any]:
    """A valid component; ``None`` overrides delete a key."""
    data: dict[str, Any] = {"id": cid, "zones": ["app/auth"], "summary": "Issues tokens"}
    return _apply(data, overrides)


def interface(iid: str = "IF-auth-api", **overrides: Any) -> dict[str, Any]:
    """A valid interface; ``None`` overrides delete a key."""
    data: dict[str, Any] = {
        "id": iid,
        "zone": "contracts/api",
        "producer": "auth-service",
        "consumers": ["web-app"],
        "summary": "REST endpoints",
    }
    return _apply(data, overrides)


def _apply(data: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def arch_front(**overrides: Any) -> dict[str, Any]:
    """A valid ``<M>.md`` frontmatter for M1."""
    data: dict[str, Any] = {
        "milestone": "M1",
        "status": "ready",
        "specs": [SPEC],
        "components": [component(), component("web-app", zones=["app/web"])],
        "interfaces": [interface()],
    }
    return _apply(data, overrides)


def overview_front(**overrides: Any) -> dict[str, Any]:
    """A valid ``overview.md`` frontmatter that includes M1."""
    data: dict[str, Any] = {
        "milestones": ["M1"],
        "components": [component(), component("web-app", zones=["app/web"])],
        "interfaces": [interface()],
    }
    return _apply(data, overrides)


def document(front: dict[str, Any], body: str = BODY) -> str:
    return "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body


class Project:
    """A repository root on disk with a spec, ``<M>.md`` and ``overview.md``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.write(SPEC, "---\nmilestone: M1\nstatus: ready\n---\n# Auth\n")
        self.arch(arch_front())
        self.overview(overview_front())

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def remove(self, rel: str) -> None:
        (self.root / rel).unlink()

    def arch(self, front: dict[str, Any], body: str = BODY) -> None:
        self.write(ARCH, document(front, body))

    def overview(self, front: dict[str, Any], body: str = "# System\n") -> None:
        self.write(OVERVIEW, document(front, body))

    def validate(self, milestone: str = "M1") -> ArchitectureValidation:
        return validate_architecture(self.root, milestone, zones=ZONES)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)


def codes(issues: tuple[PlanIssue, ...]) -> list[str]:
    return [i.code for i in issues]


def only(issues: tuple[PlanIssue, ...], code: str) -> PlanIssue:
    """The single issue with ``code``; fails when there is not exactly one."""
    found = [i for i in issues if i.code == code]
    assert len(found) == 1, f"expected one {code}, got {codes(issues)}"
    issue = found[0]
    assert issue.task is None
    assert issue.hint
    return issue


# --------------------------------------------------------------------------- clean case


def test_clean_architecture_has_no_issues(project: Project) -> None:
    result = project.validate()
    assert result.ok and not result.errors and not result.warnings
    assert result.architecture is not None and result.overview is not None
    assert result.architecture.status == "ready"
    data = result.to_data()
    assert data["file"] == ARCH
    assert data["components"] == ["auth-service", "web-app"]
    assert data["interfaces"] == ["IF-auth-api"]
    assert data["specs"] == [SPEC]
    assert data["overview"] == {"path": OVERVIEW, "present": True, "listed": True}
    assert data["counts"]["errors"] == 0


def test_draft_with_unresolved_questions_is_allowed(project: Project) -> None:
    project.arch(arch_front(status="draft"), BODY + "- Cache? (open)\n- Which DB?\n")
    result = project.validate()
    assert result.ok
    assert result.architecture is not None
    assert len(result.architecture.unresolved) == 2
    assert result.status == "draft"


def test_issue_shape_matches_plan_issue(project: Project) -> None:
    project.arch(arch_front(components=[component("Bad")]))
    issue = only(project.validate().errors, "invalid_id")
    assert set(issue.to_data()) == {"level", "code", "message", "file", "task", "field", "hint"}
    assert issue.to_data()["task"] is None


# --------------------------------------------------------------------------- errors: <M>.md


def test_invalid_milestone(project: Project) -> None:
    result = project.validate("m1")
    assert codes(result.errors) == ["invalid_milestone"]
    assert result.file == ""
    assert "M1" in (result.errors[0].hint or "")


def test_architecture_missing(project: Project) -> None:
    project.remove(ARCH)
    result = project.validate()
    issue = only(result.errors, "architecture_missing")
    assert issue.file == ARCH and "/forge-architect M1" in (issue.hint or "")
    assert result.architecture is None


def test_missing_architecture_does_not_hide_overview_errors(project: Project) -> None:
    project.remove(ARCH)
    project.overview(overview_front(components=[component(zones=["nowhere"])]))
    result = project.validate()
    assert "architecture_missing" in codes(result.errors)
    assert only(result.errors, "unknown_zone").file == OVERVIEW


@pytest.mark.parametrize(
    "text",
    [
        "# no frontmatter\n",
        "---\nmilestone: M1\n",
        "---\nmilestone: [M1\n---\n",
        "---\n- a\n- b\n---\n",
    ],
    ids=["missing", "unclosed", "yaml", "not-mapping"],
)
def test_frontmatter_invalid(project: Project, text: str) -> None:
    project.write(ARCH, text)
    result = project.validate()
    issue = only(result.errors, "frontmatter_invalid")
    assert issue.file == ARCH
    assert result.architecture is None


@pytest.mark.parametrize(
    ("front", "field"),
    [
        (arch_front(owner="me"), "owner"),
        (arch_front(components=[component(owner="me")]), "components[0].owner"),
        (arch_front(interfaces=[interface(version=2)]), "interfaces[0].version"),
    ],
    ids=["top", "component", "interface"],
)
def test_unknown_field(project: Project, front: dict[str, Any], field: str) -> None:
    project.arch(front)
    result = project.validate()
    issue = only(result.errors, "unknown_field")
    assert (issue.file, issue.field) == (ARCH, field)
    assert "allowed keys" in (issue.hint or "")
    assert result.architecture is None


@pytest.mark.parametrize(
    ("front", "field"),
    [
        (arch_front(specs=None), "specs"),
        (arch_front(status=None), "status"),
        (arch_front(interfaces=None), "interfaces"),
        (arch_front(components=[component(summary=None)]), "components[0].summary"),
        (arch_front(interfaces=[interface(consumers=None)]), "interfaces[0].consumers"),
    ],
    ids=["specs", "status", "interfaces", "component-summary", "interface-consumers"],
)
def test_missing_field(project: Project, front: dict[str, Any], field: str) -> None:
    project.arch(front)
    issue = only(project.validate().errors, "missing_field")
    assert (issue.file, issue.field) == (ARCH, field)
    assert (issue.hint or "").startswith("add `")


def test_null_field_is_missing(project: Project) -> None:
    front = arch_front()
    front["components"] = None
    project.arch(front)
    issue = only(project.validate().errors, "missing_field")
    assert issue.field == "components" and "null" in issue.message


def test_empty_lists_are_allowed(project: Project) -> None:
    project.arch(arch_front(components=[], interfaces=[]))
    result = project.validate()
    assert result.ok, codes(result.errors)


@pytest.mark.parametrize(
    ("front", "field"),
    [
        (arch_front(status="final"), "status"),
        (arch_front(specs=[]), "specs"),
        (arch_front(specs=SPEC), "specs"),
        (arch_front(components={"id": "x"}), "components"),
        (arch_front(components=["auth-service"]), "components[0]"),
        (arch_front(components=[component(zones="app/auth")]), "components[0].zones"),
        (arch_front(interfaces=[interface(producer=3)]), "interfaces[0].producer"),
        (arch_front(milestone=1), "milestone"),
        (arch_front(specs=["../outside.md"]), "specs[0]"),
    ],
    ids=[
        "status",
        "empty-specs",
        "specs-not-list",
        "components-not-list",
        "component-not-mapping",
        "zones-not-list",
        "producer-not-string",
        "milestone-not-string",
        "spec-outside-repo",
    ],
)
def test_invalid_field(project: Project, front: dict[str, Any], field: str) -> None:
    project.arch(front)
    result = project.validate()
    issue = only(result.errors, "invalid_field")
    assert (issue.file, issue.field) == (ARCH, field)
    assert result.architecture is None


def test_milestone_mismatch(project: Project) -> None:
    project.arch(arch_front(milestone="M2"))
    issue = only(project.validate().errors, "milestone_mismatch")
    assert (issue.file, issue.field) == (ARCH, "milestone")
    assert "milestone: M1" in (issue.hint or "")


def test_spec_missing(project: Project) -> None:
    project.arch(arch_front(specs=[SPEC, "specs/m1/nope.md"]))
    issue = only(project.validate().errors, "spec_missing")
    assert (issue.file, issue.field) == (ARCH, "specs[1]")
    assert "specs/m1/nope.md" in issue.message


def test_spec_invalid(project: Project) -> None:
    project.write(SPEC, "# no frontmatter\n")
    issue = only(project.validate().errors, "spec_invalid")
    assert issue.field == "specs[0]"


def test_spec_milestone_mismatch(project: Project) -> None:
    project.write(SPEC, "---\nmilestone: M2\nstatus: ready\n---\n")
    issue = only(project.validate().errors, "spec_milestone_mismatch")
    assert issue.field == "specs[0]" and "M2" in issue.message


def test_spec_path_layout_is_not_checked(project: Project) -> None:
    project.write("specs/M1-auth.md", "---\nmilestone: M1\nstatus: ready\n---\n")
    project.arch(arch_front(specs=["specs/M1-auth.md"]))
    assert project.validate().ok


@pytest.mark.parametrize(
    ("front", "field"),
    [
        (
            arch_front(components=[component("Auth_Service")], interfaces=[]),
            "components[0].id",
        ),
        (
            arch_front(interfaces=[interface("auth-api")]),
            "interfaces[0].id",
        ),
        (
            arch_front(interfaces=[interface("IF-Auth")]),
            "interfaces[0].id",
        ),
    ],
    ids=["component", "interface-prefix", "interface-case"],
)
def test_invalid_id(project: Project, front: dict[str, Any], field: str) -> None:
    project.arch(front)
    issue = only(project.validate().errors, "invalid_id")
    assert (issue.file, issue.field) == (ARCH, field)


@pytest.mark.parametrize("kind", ["components", "interfaces"])
def test_duplicate_id(project: Project, kind: str) -> None:
    front = arch_front()
    front[kind] = [*front[kind], front[kind][0]]
    project.arch(front)
    issue = only(project.validate().errors, "duplicate_id")
    assert issue.file == ARCH and issue.field == f"{kind}[{len(front[kind]) - 1}].id"


@pytest.mark.parametrize(
    ("front", "field"),
    [
        (arch_front(components=[component(zones=["app/nope"]), component("web-app")]), None),
        (arch_front(interfaces=[interface(zone="contracts/nope")]), "interfaces[0].zone"),
    ],
    ids=["component", "interface"],
)
def test_unknown_zone(project: Project, front: dict[str, Any], field: str | None) -> None:
    project.arch(front)
    issue = only(project.validate().errors, "unknown_zone")
    assert issue.file == ARCH
    assert issue.field == (field or "components[0].zones[0]")


def test_interface_zone_not_contract(project: Project) -> None:
    project.arch(arch_front(interfaces=[interface(zone="app/auth")]))
    issue = only(project.validate().errors, "interface_zone_not_contract")
    assert (issue.file, issue.field) == (ARCH, "interfaces[0].zone")


@pytest.mark.parametrize(
    ("iface", "field"),
    [
        (interface(producer="ghost"), "interfaces[0].producer"),
        (interface(consumers=["web-app", "ghost"]), "interfaces[0].consumers[1]"),
    ],
    ids=["producer", "consumer"],
)
def test_unknown_component(project: Project, iface: dict[str, Any], field: str) -> None:
    project.arch(arch_front(interfaces=[iface]))
    issue = only(project.validate().errors, "unknown_component")
    assert (issue.file, issue.field) == (ARCH, field)
    assert "ghost" in issue.message


def test_component_from_overview_resolves(project: Project) -> None:
    project.overview(
        overview_front(components=[*overview_front()["components"], component("billing")])
    )
    project.arch(arch_front(interfaces=[interface(consumers=["billing"])]))
    assert "unknown_component" not in codes(project.validate().errors)


@pytest.mark.parametrize("section", ["Context", "Data model", "Open questions"])
def test_missing_section(project: Project, section: str) -> None:
    body = BODY.replace(f"## {section}\n", "")
    project.arch(arch_front(), body)
    issue = only(project.validate().errors, "missing_section")
    assert (issue.file, issue.field) == (ARCH, section)
    assert issue.hint == f"add `## {section}`"


def test_sections_match_case_insensitively_outside_code(project: Project) -> None:
    project.arch(
        arch_front(), BODY.replace("## Risks", "## RISKS").replace("## Context", "##  context")
    )
    assert project.validate().ok
    fenced = BODY.replace("## Risks\nNone.\n", "```\n## Risks\n```\n")
    project.arch(arch_front(), fenced)
    assert only(project.validate().errors, "missing_section").field == "Risks"
    project.arch(arch_front(), BODY.replace("## Risks", "### Risks"))
    assert only(project.validate().errors, "missing_section").field == "Risks"


def test_open_questions_when_ready(project: Project) -> None:
    project.arch(arch_front(), BODY + "- Which cache?\n")
    issue = only(project.validate().errors, "open_questions")
    assert (issue.file, issue.field) == (ARCH, "Open questions")
    assert "Which cache?" in issue.message


def test_placeholder_question_is_never_resolved(project: Project) -> None:
    body = BODY.replace(
        "- Token lifetime? RESOLVED: 1h\n", "- (none yet, every item marked RESOLVED)\n"
    )
    project.arch(arch_front(), body)
    assert "open_questions" in codes(project.validate().errors)


# --------------------------------------------------------------------------- errors: overview.md


def test_overview_invalid_when_frontmatter_unreadable(project: Project) -> None:
    project.write(OVERVIEW, "# no frontmatter\n")
    result = project.validate()
    issue = only(result.errors, "overview_invalid")
    assert issue.file == OVERVIEW
    assert result.overview is None and result.to_data()["overview"]["present"] is True
    assert "overview_stale" not in codes(result.warnings)


def test_overview_unknown_field(project: Project) -> None:
    project.overview(overview_front(owner="me"))
    issue = only(project.validate().errors, "unknown_field")
    assert (issue.file, issue.field) == (OVERVIEW, "owner")


def test_overview_missing_field(project: Project) -> None:
    project.overview(overview_front(milestones=None))
    issue = only(project.validate().errors, "missing_field")
    assert (issue.file, issue.field) == (OVERVIEW, "milestones")


@pytest.mark.parametrize(
    ("front", "field"),
    [
        (overview_front(milestones=["M1", "v2"]), "milestones[1]"),
        (overview_front(components="all"), "components"),
    ],
    ids=["milestone-item", "components-not-list"],
)
def test_overview_invalid_field(project: Project, front: dict[str, Any], field: str) -> None:
    project.overview(front)
    result = project.validate()
    issue = only(result.errors, "invalid_field")
    assert (issue.file, issue.field) == (OVERVIEW, field)
    assert result.overview is None


def test_overview_invalid_id(project: Project) -> None:
    project.overview(overview_front(interfaces=[interface("api")]))
    issue = only(project.validate().errors, "invalid_id")
    assert (issue.file, issue.field) == (OVERVIEW, "interfaces[0].id")


def test_overview_duplicate_id(project: Project) -> None:
    project.overview(overview_front(components=[component(), component()]))
    errors = project.validate().errors
    issue = only(errors, "duplicate_id")
    assert (issue.file, issue.field) == (OVERVIEW, "components[1].id")


def test_overview_unknown_zone(project: Project) -> None:
    project.overview(overview_front(interfaces=[interface(zone="nope")]))
    issue = only(project.validate().errors, "unknown_zone")
    assert (issue.file, issue.field) == (OVERVIEW, "interfaces[0].zone")


def test_overview_interface_zone_not_contract(project: Project) -> None:
    project.overview(overview_front(interfaces=[interface(zone="app/web")]))
    issue = only(project.validate().errors, "interface_zone_not_contract")
    assert issue.file == OVERVIEW


def test_overview_unknown_component_only_among_its_own(project: Project) -> None:
    # web-app is declared in M1.md but not in overview.md: overview must be self-contained.
    project.overview(overview_front(components=[component()]))
    result = project.validate()
    issue = only(result.errors, "unknown_component")
    assert (issue.file, issue.field) == (OVERVIEW, "interfaces[0].consumers[0]")


# --------------------------------------------------------------------------- warnings


def test_overview_stale_when_missing(project: Project) -> None:
    project.remove(OVERVIEW)
    result = project.validate()
    assert result.ok
    issue = only(result.warnings, "overview_stale")
    assert "missing" in issue.message
    assert result.to_data()["overview"] == {"path": OVERVIEW, "present": False, "listed": False}


def test_overview_stale_when_milestone_not_listed(project: Project) -> None:
    project.overview(overview_front(milestones=[]))
    result = project.validate()
    issue = only(result.warnings, "overview_stale")
    assert issue.field == "milestones" and "M1" in issue.message
    assert result.to_data()["overview"]["listed"] is False


def test_overview_stale_when_ids_are_missing(project: Project) -> None:
    project.overview(overview_front(components=[component()], interfaces=[]))
    result = project.validate()
    issue = only(result.warnings, "overview_stale")
    assert "web-app" in issue.message and "IF-auth-api" in issue.message


def test_component_unused(project: Project) -> None:
    front = arch_front(components=[*arch_front()["components"], component("idle", zones=[])])
    project.arch(front)
    issue = only(project.validate().warnings, "component_unused")
    assert (issue.file, issue.field) == (ARCH, "components[2]")


def test_component_without_zones_but_linked_is_used(project: Project) -> None:
    # Linked only through an interface of overview.md.
    overview = overview_front()
    overview["interfaces"] = [interface(), interface("IF-idle", consumers=["idle"])]
    overview["components"] = [*overview["components"], component("idle", zones=[])]
    project.overview(overview)
    project.arch(arch_front(components=[*arch_front()["components"], component("idle", zones=[])]))
    assert "component_unused" not in codes(project.validate().warnings)


# --------------------------------------------------------------------------- loaders


def test_load_architecture(project: Project) -> None:
    arch = load_architecture(project.root, "M1")
    assert arch.milestone == "M1" and arch.status == "ready" and arch.path == ARCH
    assert arch.specs == (SPEC,)
    assert [c.id for c in arch.components] == ["auth-service", "web-app"]
    assert arch.interface("IF-auth-api") is not None
    assert arch.component("web-app") is not None and arch.component("x") is None
    assert len(arch.sections) == 7 and not arch.unresolved


def test_load_architecture_missing_names_file_and_fix(project: Project) -> None:
    project.remove(ARCH)
    with pytest.raises(ConfigError) as info:
        load_architecture(project.root, "M1")
    assert info.value.file == ARCH and "/forge-architect M1" in (info.value.hint or "")


def test_load_architecture_rejects_wrong_type(project: Project) -> None:
    project.arch(arch_front(components=[component(zones="app/auth")]))
    with pytest.raises(ConfigError) as info:
        load_architecture(project.root, "M1")
    assert info.value.file == ARCH and info.value.key == "components[0].zones"
    assert info.value.hint


def test_load_architecture_rejects_invalid_milestone(project: Project) -> None:
    with pytest.raises(ConfigError):
        load_architecture(project.root, "one")


def test_load_overview(project: Project) -> None:
    overview = load_overview(project.root)
    assert overview is not None and overview.milestones == ("M1",)
    project.remove(OVERVIEW)
    assert load_overview(project.root) is None


def test_load_overview_rejects_bad_frontmatter(project: Project) -> None:
    project.overview(overview_front(extra=1))
    with pytest.raises(ConfigError) as info:
        load_overview(project.root)
    assert info.value.file == OVERVIEW and info.value.key == "extra"


def test_architecture_path() -> None:
    assert architecture_path("M3") == Path("docs/architecture/M3.md")


# --------------------------------------------------------------------------- resolution helpers


def test_resolution_helpers(project: Project) -> None:
    overview = overview_front()
    overview["components"] = [*overview["components"], component("billing", zones=["app/web"])]
    overview["interfaces"] = [
        interface(producer="web-app"),
        interface("IF-billing", producer="billing", consumers=[]),
    ]
    project.overview(overview)
    arch = load_architecture(project.root, "M1")
    over = load_overview(project.root)

    assert list(milestone_interfaces(arch)) == ["IF-auth-api"]
    assert list(existing_interfaces(arch, over)) == ["IF-billing"]
    # <M>.md wins over overview.md.
    assert interface_producer(arch, over, "IF-auth-api") == "auth-service"
    assert interface_producer(arch, over, "IF-billing") == "billing"
    assert interface_producer(arch, over, "IF-none") is None
    assert resolve_interface(None, over, "IF-auth-api") is not None
    assert resolve_component(arch, over, "billing") is not None
    assert resolve_component(arch, None, "billing") is None
    assert component_zones(arch, over, "auth-service") == ("app/auth",)
    assert component_zones(arch, over, "billing") == ("app/web",)
    assert component_zones(arch, over, "ghost") is None
    assert milestone_interfaces(None) == {} and existing_interfaces(None, None) == {}
