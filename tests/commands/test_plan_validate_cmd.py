"""End-to-end tests for `forge plan validate` through the CLI (ADR 0013).

The repository is a real clone; GitHub access is replaced by a factory that fails, proving
the command is offline. The profile lives under a temporary ``XDG_CONFIG_HOME``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from typer.testing import CliRunner

from forge.cli import app
from forge.commands import _context
from forge.commands.plan import in_ci, render_validation
from forge.core.config import ForgeConfig
from forge.core.github import GitHubClient

if TYPE_CHECKING:
    from conftest import GitWorld

runner = CliRunner()

NO_ARCH_INFO = {
    "level": "info",
    "text": "no architecture for M1 (planning.architecture: optional)",
    "hint": None,
}

CONFIG = """\
forge_version: ">=0.1,<0.2"
project:
  name: shop
  default_branch: main
github:
  repo: acme/shop
models:
  min_tier_for:
    plan: strong
"""

ZONES = """\
zones:
  api: {paths: ["src/api/**"]}
  web: {paths: ["src/web/**"]}
  shared: {paths: ["shared/**"], contract: true}
"""

TEAM = """\
members:
  - github: alice
    roles: [dev, lead]
  - github: bob
    roles: [lead]
"""

SPEC = """\
---
milestone: M1
status: ready
---
# Editor
## Requirements
- **AC-1** schema
- **AC-2** api
- **AC-3** web
## Open questions
"""

PROFILE_STRONG = """\
github: alice
agents:
  - id: claude
    tool: claude-code
    providers: [anthropic]
    tiers: {strong: opus, standard: sonnet, light: haiku}
default_agent: claude
"""

PROFILE_LIGHT = """\
github: alice
agents:
  - id: small
    tool: ollama
    providers: [local]
    tiers: {light: qwen}
default_agent: small
"""


def _task(n: int, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": f"M1-T{n:02d}",
        "title": f"Task {n}",
        "type": "impl",
        "spec": "specs/m1/editor.md",
        "covers": [],
        "depends_on": [],
        "zones": [],
        "size": "S",
        "tier": "standard",
        "description": "Do it.\n",
        "done_when": ["tests pass"],
    }
    data.update(extra)
    return data


CLEAN = [
    _task(1, type="contract", zones=["shared"], covers=["AC-1"], title="[bold]Schema[/bold]"),
    _task(2, zones=["api"], covers=["AC-2"], depends_on=["M1-T01"]),
    _task(3, zones=["web"], covers=["AC-3"], depends_on=["M1-T01"]),
]


def _no_github(config: ForgeConfig) -> GitHubClient:
    raise AssertionError("plan validate must not touch GitHub")


@pytest.fixture
def repo(git_world: GitWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = git_world.clone("alice").cwd
    (root / ".forge").mkdir()
    (root / ".forge" / "config.yml").write_text(CONFIG)
    (root / ".forge" / "zones.yml").write_text(ZONES)
    (root / ".forge" / "team.yml").write_text(TEAM)
    (root / "specs" / "m1").mkdir(parents=True)
    (root / "specs" / "m1" / "editor.md").write_text(SPEC)
    (root / "plans").mkdir()
    write_plan(root, CLEAN)
    xdg = tmp_path / "xdg"
    (xdg / "forge").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("FORGE_AGENT", raising=False)
    monkeypatch.setattr(_context, "github_factory", _no_github)
    return root


def write_plan(root: Path, tasks: list[dict[str, Any]]) -> None:
    data = {"milestone": "M1", "tasks": tasks}
    (root / "plans" / "M1.yml").write_text(yaml.safe_dump(data, sort_keys=False))


def set_profile(text: str) -> None:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "forge" / "profile.yml"
    path.write_text(text)


def invoke(root: Path, *args: str) -> Any:
    return runner.invoke(app, ["--repo", str(root), *args])


def envelope(root: Path, *args: str) -> tuple[int, dict[str, Any]]:
    result = invoke(root, "--json", "plan", "validate", *args)
    return result.exit_code, json.loads(result.stdout)


# --------------------------------------------------------------------------- JSON


def test_clean_plan_json(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    code, env = envelope(repo, "M1")
    assert code == 0
    assert env["ok"] is True and env["code"] == 0
    data = env["data"]
    assert data["waves"] == [["M1-T01"], ["M1-T02", "M1-T03"]]
    assert data["coverage"] == {"AC-1": ["M1-T01"], "AC-2": ["M1-T02"], "AC-3": ["M1-T03"]}
    assert data["errors"] == [] and data["warnings"] == []
    # No `planning` block and no docs/architecture/M1.md: unchecked, one info (ADR 0019).
    assert env["messages"] == [NO_ARCH_INFO]


def test_errors_exit_1_with_data(repo: Path) -> None:
    tasks = [dict(t) for t in CLEAN]
    tasks[1]["size"] = "L"
    write_plan(repo, tasks)
    code, env = envelope(repo, "M1")
    assert code == 1
    assert env["ok"] is False
    [error] = env["data"]["errors"]
    assert error["code"] == "size_too_large"
    assert (error["file"], error["task"], error["field"]) == ("plans/M1.yml", "M1-T02", "size")
    assert any(m["level"] == "error" for m in env["messages"])


def test_warnings_exit_0(repo: Path) -> None:
    tasks = [dict(t) for t in CLEAN]
    tasks[2]["zones"] = ["api"]
    write_plan(repo, tasks)
    code, env = envelope(repo, "M1")
    assert code == 0
    assert [w["code"] for w in env["data"]["warnings"]] == ["zone_overlap"]


def test_team_parallelism_warning(repo: Path) -> None:
    code, env = envelope(repo, "M1")
    assert code == 0
    # One dev (alice; bob only leads): every wave has enough tasks.
    assert [w["code"] for w in env["data"]["warnings"]] == []
    (repo / ".forge" / "team.yml").write_text(TEAM + "  - github: carol\n    roles: [dev]\n")
    _, env = envelope(repo, "M1")
    # Two devs: wave 1 holds only M1-T01.
    [warning] = env["data"]["warnings"]
    assert warning["code"] == "low_parallelism" and "wave 1" in warning["message"]


def test_missing_plan_file(repo: Path) -> None:
    code, env = envelope(repo, "M7")
    assert code == 1
    assert env["data"]["errors"][0]["code"] == "plan_missing"


def test_version_mismatch_is_a_warning(repo: Path) -> None:
    text = CONFIG.replace('">=0.1,<0.2"', '">=99"')
    (repo / ".forge" / "config.yml").write_text(text)
    code, env = envelope(repo, "M1")
    assert code == 0
    assert any(m["level"] == "warning" and "forge_version" in m["text"] for m in env["messages"])


# --------------------------------------------------------------------------- tier check


def test_tier_refusal_exit_5(repo: Path) -> None:
    set_profile(PROFILE_LIGHT)
    code, env = envelope(repo, "M1")
    assert code == 5
    assert "min_tier_for.plan" in env["messages"][0]["text"]


def test_tier_refusal_forced(repo: Path) -> None:
    set_profile(PROFILE_LIGHT)
    code, env = envelope(repo, "M1", "--force")
    assert code == 0
    assert any(m["level"] == "warning" and "--force" in m["text"] for m in env["messages"])


def test_tier_skipped_in_github_actions(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_profile(PROFILE_LIGHT)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    code, env = envelope(repo, "M1")
    assert code == 0
    [message, arch] = env["messages"]
    assert message["level"] == "info" and "GitHub Actions" in message["text"]
    assert arch == NO_ARCH_INFO


def test_tier_without_profile_is_info(repo: Path) -> None:
    code, env = envelope(repo, "M1")
    assert code == 0
    [message, arch] = env["messages"]
    assert message["level"] == "info" and "no Forge profile" in message["text"]
    assert arch == NO_ARCH_INFO


def test_in_ci() -> None:
    assert in_ci({"GITHUB_ACTIONS": "true"})
    assert not in_ci({"GITHUB_ACTIONS": ""})
    assert not in_ci({})


# --------------------------------------------------------------------------- human output


def test_human_output_clean(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    result = invoke(repo, "plan", "validate", "M1")
    assert result.exit_code == 0
    out = result.stdout
    assert "Plan M1  plans/M1.yml  3 task(s), 0 error(s), 0 warning(s)" in out
    assert "1: M1-T01" in out
    assert "2: M1-T02, M1-T03" in out
    assert "AC-2" in out and "M1-T02" in out


def test_human_output_groups_errors_and_warnings(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    tasks = [dict(t) for t in CLEAN]
    tasks[1]["zones"] = ["api", "nowhere"]
    tasks[2]["covers"] = []
    write_plan(repo, tasks)
    result = invoke(repo, "plan", "validate", "M1")
    assert result.exit_code == 1
    out = result.stdout
    assert "Errors (1)" in out
    assert "unknown_zone  plans/M1.yml · M1-T02 · zones[1]" in out
    assert "fix: add it to .forge/zones.yml" in out
    assert "Warnings (1)" in out
    assert "uncovered_ac  specs/m1/editor.md · AC-3" in out
    assert "uncovered" in out
    assert "error: plan M1 has 1 error(s)" in result.stderr


def test_markup_is_printed_literally() -> None:
    data = {
        "milestone": "M1",
        "file": "plans/M1.yml",
        "counts": {"tasks": 1, "errors": 1, "warnings": 0},
        "errors": [
            {
                "code": "unknown_zone",
                "message": "unknown zone '[red]x[/red]' :smile:",
                "file": "plans/M1.yml",
                "task": "M1-T01",
                "field": "zones[0]",
                "hint": "[bold]fix[/bold]",
            }
        ],
        "warnings": [],
        "waves": [["M1-T01"]],
        "coverage": {"AC-1": []},
    }
    out = render_validation(data, width=100)
    assert "'[red]x[/red]' :smile:" in out
    assert "fix: [bold]fix[/bold]" in out
    assert "uncovered" in out


# --------------------------------------------------------------------------- architecture

ARCH_BODY = """
## Context
Editor.
## Components
Two.
## Interfaces
One.
## Data model
Docs.
## Decisions
None.
## Risks
None.
## Open questions
"""

ARCH_FRONT: dict[str, Any] = {
    "milestone": "M1",
    "status": "ready",
    "specs": ["specs/m1/editor.md"],
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

ARCH_TASKS = [
    {**CLEAN[0], "component": "api-svc", "implements": ["IF-schema"]},
    {**CLEAN[1], "component": "api-svc", "uses": ["IF-schema"]},
    {**CLEAN[2], "component": "web-app", "uses": ["IF-schema"]},
]


def write_architecture(root: Path, front: dict[str, Any] = ARCH_FRONT) -> None:
    path = root / "docs" / "architecture" / "M1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + ARCH_BODY)


def set_mode(root: Path, mode: str) -> None:
    (root / ".forge" / "config.yml").write_text(CONFIG + f"planning:\n  architecture: {mode}\n")


def test_required_without_architecture_exits_1(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    set_mode(repo, "required")
    code, env = envelope(repo, "M1")
    assert code == 1
    assert [e["code"] for e in env["data"]["errors"]] == ["architecture_missing"]
    assert env["data"]["architecture"] == {
        "mode": "required",
        "path": "docs/architecture/M1.md",
        "present": False,
        "status": None,
        "checked": True,
        "interfaces": {},
    }
    assert NO_ARCH_INFO not in env["messages"]
    assert any(m["level"] == "error" for m in env["messages"])


def test_required_with_architecture_exits_0_without_info(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    set_mode(repo, "required")
    write_architecture(repo)
    write_plan(repo, ARCH_TASKS)
    code, env = envelope(repo, "M1")
    assert code == 0, env["data"]["errors"]
    assert env["messages"] == []
    arch = env["data"]["architecture"]
    assert arch["checked"] is True and arch["status"] == "ready"
    assert arch["interfaces"] == {"IF-schema": "M1-T01"}


def test_optional_with_fields_warns_unchecked(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    set_mode(repo, "optional")
    write_plan(repo, ARCH_TASKS)
    code, env = envelope(repo, "M1")
    assert code == 0
    assert [w["code"] for w in env["data"]["warnings"]] == ["architecture_fields_unchecked"]
    assert env["messages"] == [NO_ARCH_INFO]


def test_plan_apply_relays_architecture_errors(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    set_mode(repo, "required")
    result = invoke(repo, "--json", "plan", "apply", "M1", "--dry-run")
    assert result.exit_code == 1
    env = json.loads(result.stdout)
    codes = [e["code"] for e in env["data"]["validation"]["errors"]]
    assert codes == ["architecture_missing"]


def test_plan_apply_surfaces_the_unchecked_info(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    # An invalid plan stops apply before any GitHub access (the fixture forbids it).
    tasks = [dict(t) for t in CLEAN]
    tasks[1]["size"] = "L"
    write_plan(repo, tasks)
    result = invoke(repo, "--json", "plan", "apply", "M1", "--dry-run")
    assert result.exit_code == 1
    env = json.loads(result.stdout)
    assert NO_ARCH_INFO in env["messages"]


def test_human_output_architecture_line(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    out = invoke(repo, "plan", "validate", "M1").stdout
    assert (
        "Architecture  not checked (planning.architecture: optional, "
        "no docs/architecture/M1.md)" in out
    )
    set_mode(repo, "required")
    write_architecture(repo)
    write_plan(repo, ARCH_TASKS)
    out = invoke(repo, "plan", "validate", "M1").stdout
    assert (
        "Architecture  docs/architecture/M1.md  ready, 1/1 interface(s) implemented (required)"
        in out
    )
