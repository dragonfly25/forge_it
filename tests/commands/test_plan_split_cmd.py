"""End-to-end tests for `forge plan split` and `forge plan assemble` through the CLI (ADR 0020).

The repository is a real clone (``git check-ignore`` runs for real); GitHub access is replaced
by a factory that fails, proving both commands are offline. The profile lives under a
temporary ``XDG_CONFIG_HOME``.
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
from forge.commands.plan import render_assemble, render_split
from forge.core.config import ForgeConfig
from forge.core.github import GitHubClient

if TYPE_CHECKING:
    from conftest import GitWorld

runner = CliRunner()

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
"""

SPEC = """\
---
milestone: M1
status: ready
---
# Core
## Requirements
- **AC-1** one
## Open questions
"""

PROFILE_CLAUDE = """\
github: alice
agents:
  - id: claude
    tool: claude-code
    providers: [anthropic]
    tiers: {light: haiku}
default_agent: claude
"""


def _no_github(config: ForgeConfig) -> GitHubClient:
    raise AssertionError("plan split and assemble must not touch GitHub")


def tid(n: int) -> str:
    return f"M1-T{n:02d}"


def skeleton(count: int) -> dict[str, Any]:
    return {
        "milestone": "M1",
        "tasks": [
            {
                "id": tid(n),
                "title": f"Task {n}",
                "type": "impl",
                "spec": "specs/m1/core.md",
                "covers": ["AC-1"],
                "depends_on": [],
                "zones": ["api"],
                "size": "S",
            }
            for n in range(1, count + 1)
        ],
    }


@pytest.fixture
def repo(git_world: GitWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = git_world.clone("alice").cwd
    (root / ".gitignore").write_text(".forge/local/\n")
    (root / ".forge").mkdir()
    (root / ".forge" / "config.yml").write_text(CONFIG)
    (root / ".forge" / "zones.yml").write_text(ZONES)
    (root / "specs" / "m1").mkdir(parents=True)
    (root / "specs" / "m1" / "core.md").write_text(SPEC)
    (root / "plans").mkdir()
    write_plan(root, skeleton(12))
    xdg = tmp_path / "xdg"
    (xdg / "forge").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("FORGE_AGENT", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(_context, "github_factory", _no_github)
    return root


def write_plan(root: Path, data: dict[str, Any]) -> None:
    (root / "plans" / "M1.yml").write_text(yaml.safe_dump(data, sort_keys=False))


def set_profile(text: str) -> None:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "forge" / "profile.yml"
    path.write_text(text)


def invoke(root: Path, *args: str) -> Any:
    return runner.invoke(app, ["--repo", str(root), *args])


def envelope(root: Path, command: str, *args: str) -> tuple[int, dict[str, Any]]:
    result = invoke(root, "--json", "plan", command, *args)
    return result.exit_code, json.loads(result.stdout)


def write_fragments(root: Path, batches: list[dict[str, Any]]) -> None:
    for batch in batches:
        data = {
            "milestone": "M1",
            "batch": batch["index"],
            "tasks": [
                {
                    "id": t,
                    "description": f"Build {t}.\n",
                    "done_when": [f"{t} works"],
                    "tier": "light",
                }
                for t in batch["task_ids"]
            ],
        }
        (root / batch["part_path"]).write_text(yaml.safe_dump(data, sort_keys=False))


# --------------------------------------------------------------------------- split


def test_help_lists_commands_in_workflow_order(repo: Path) -> None:
    out = invoke(repo, "plan", "--help").stdout
    commands = out[out.index("Commands") :]
    positions = [commands.index(f" {name} ") for name in ("validate", "split", "assemble", "apply")]
    assert positions == sorted(positions)


def test_split_json_shape(repo: Path) -> None:
    code, env = envelope(repo, "split", "M1")
    assert code == 0, env
    assert set(env) == {"ok", "code", "data", "messages"}
    data = env["data"]
    assert set(data) == {
        "milestone",
        "file",
        "batches_path",
        "agents",
        "max_per_agent",
        "batches",
        "removed_parts",
        "errors",
        "warnings",
        "subagent",
    }
    assert data["file"] == "plans/M1.yml"
    assert data["batches_path"] == ".forge/local/plan/M1/batches.json"
    assert data["agents"] == 2 and data["max_per_agent"] == 10
    assert data["batches"] == [
        {
            "index": 1,
            "task_ids": [tid(n) for n in range(1, 7)],
            "part_path": ".forge/local/plan/M1/part-1.yml",
        },
        {
            "index": 2,
            "task_ids": [tid(n) for n in range(7, 13)],
            "part_path": ".forge/local/plan/M1/part-2.yml",
        },
    ]
    on_disk = json.loads((repo / data["batches_path"]).read_text())
    assert on_disk["batches"] == data["batches"]


def test_split_max_from_config(repo: Path) -> None:
    (repo / ".forge" / "config.yml").write_text(CONFIG + "planning:\n  max_tasks_per_agent: 4\n")
    code, env = envelope(repo, "split", "M1")
    assert code == 0
    assert env["data"]["max_per_agent"] == 4
    assert [len(b["task_ids"]) for b in env["data"]["batches"]] == [4, 4, 4]


def test_split_no_tier_check(repo: Path) -> None:
    # min_tier_for.plan is strong, the agent serves only light: split still runs.
    set_profile(PROFILE_CLAUDE)
    code, env = envelope(repo, "split", "M1")
    assert code == 0, env


def test_split_subagent_rendered(repo: Path) -> None:
    set_profile(PROFILE_CLAUDE)
    rendered = repo / ".claude" / "agents" / "forge-planner.md"
    rendered.parent.mkdir(parents=True)
    rendered.write_text("---\nname: forge-planner\n---\n")
    _, env = envelope(repo, "split", "M1")
    assert env["data"]["subagent"] == {
        "name": "forge-planner",
        "available": True,
        "reason": "forge-planner is rendered for claude-code",
    }


def test_split_subagent_without_profile(repo: Path) -> None:
    _, env = envelope(repo, "split", "M1")
    assert env["data"]["subagent"]["available"] is False
    assert "forge setup" in env["data"]["subagent"]["reason"]


def test_split_skeleton_error_exit_1(repo: Path) -> None:
    data = skeleton(3)
    data["tasks"][0]["zones"] = ["nowhere"]
    write_plan(repo, data)
    code, env = envelope(repo, "split", "M1")
    assert code == 1
    assert [e["code"] for e in env["data"]["errors"]] == ["unknown_zone"]
    assert env["data"]["batches"] == []
    errors = [m for m in env["messages"] if m["level"] == "error"]
    assert errors[0]["text"] == "plan M1 has 1 error(s); not split"
    assert not (repo / ".forge" / "local").exists()


def test_split_plan_empty_exit_1(repo: Path) -> None:
    write_plan(repo, {"milestone": "M1", "tasks": []})
    code, env = envelope(repo, "split", "M1")
    assert code == 1
    assert [e["code"] for e in env["data"]["errors"]] == ["plan_empty"]


def test_split_invalid_milestone_exit_1(repo: Path) -> None:
    code, env = envelope(repo, "split", "first")
    assert code == 1
    assert [e["code"] for e in env["data"]["errors"]] == ["invalid_milestone"]


def test_split_invalid_zones_exit_1(repo: Path) -> None:
    (repo / ".forge" / "zones.yml").write_text("zones: [not, a, mapping]\n")
    code, env = envelope(repo, "split", "M1")
    assert code == 1
    assert any("zones.yml" in m["text"] for m in env["messages"])


def test_split_plan_missing_exit_5(repo: Path) -> None:
    code, env = envelope(repo, "split", "M7")
    assert code == 5
    assert [e["code"] for e in env["data"]["errors"]] == ["plan_missing"]
    [error] = [m for m in env["messages"] if m["level"] == "error"]
    assert error["text"] == "plan file not found (plans/M7.yml)"
    assert "write the skeleton first" in error["hint"]


def test_split_not_ignored_exit_5(repo: Path) -> None:
    (repo / ".gitignore").write_text("")
    code, env = envelope(repo, "split", "M1")
    assert code == 5
    assert [e["code"] for e in env["data"]["errors"]] == ["not_ignored"]
    [error] = [m for m in env["messages"] if m["level"] == "error"]
    assert ".gitignore" in error["hint"]
    assert not (repo / ".forge" / "local").exists()


def test_split_no_config_exit_5(repo: Path) -> None:
    (repo / ".forge" / "config.yml").unlink()
    code, env = envelope(repo, "split", "M1")
    assert code == 5
    assert ".forge/config.yml" in env["messages"][0]["text"]


def test_split_version_mismatch_is_a_warning(repo: Path) -> None:
    (repo / ".forge" / "config.yml").write_text(CONFIG.replace('">=0.1,<0.2"', '">=99"'))
    code, env = envelope(repo, "split", "M1")
    assert code == 0
    assert any(m["level"] == "warning" for m in env["messages"])


def test_split_removed_parts(repo: Path) -> None:
    directory = repo / ".forge" / "local" / "plan" / "M1"
    directory.mkdir(parents=True)
    (directory / "part-3.yml").write_text("old")
    _, env = envelope(repo, "split", "M1")
    assert env["data"]["removed_parts"] == [".forge/local/plan/M1/part-3.yml"]


def test_split_human_output(repo: Path) -> None:
    result = invoke(repo, "plan", "split", "M1")
    assert result.exit_code == 0
    out = result.stdout
    assert "Plan split M1  plans/M1.yml  2 agent(s), at most 10 task(s) each" in out
    assert "1: M1-T01, M1-T02" in out and "-> .forge/local/plan/M1/part-1.yml" in out
    assert "forge-planner not available" in out


# --------------------------------------------------------------------------- assemble


def test_assemble_json_shape_and_rerun(repo: Path) -> None:
    _, split = envelope(repo, "split", "M1")
    write_fragments(repo, split["data"]["batches"])
    code, env = envelope(repo, "assemble", "M1")
    assert code == 0, env
    data = env["data"]
    assert set(data) == {
        "milestone",
        "file",
        "written",
        "changed",
        "batches",
        "tasks",
        "errors",
        "warnings",
        "counts",
    }
    assert data["written"] is True and data["changed"] is True
    assert data["tasks"] == [tid(n) for n in range(1, 13)]
    assert data["batches"] == split["data"]["batches"]
    first = (repo / "plans" / "M1.yml").read_bytes()
    code, env = envelope(repo, "assemble", "M1")
    assert code == 0
    assert env["data"]["written"] is True and env["data"]["changed"] is False
    assert (repo / "plans" / "M1.yml").read_bytes() == first
    # The assembled plan passes `forge plan validate` (no tier check problem: no profile).
    result = invoke(repo, "--json", "plan", "validate", "M1")
    assert result.exit_code == 0, result.stdout


def test_assemble_split_missing_exit_5(repo: Path) -> None:
    code, env = envelope(repo, "assemble", "M1")
    assert code == 5
    assert [e["code"] for e in env["data"]["errors"]] == ["split_missing"]
    [error] = [m for m in env["messages"] if m["level"] == "error"]
    assert "forge plan split M1" in error["hint"]


def test_assemble_plan_missing_exit_5(repo: Path) -> None:
    code, env = envelope(repo, "assemble", "M3")
    assert code == 5
    assert [e["code"] for e in env["data"]["errors"]] == ["plan_missing"]


def test_assemble_no_config_exit_5(repo: Path) -> None:
    (repo / ".forge" / "config.yml").unlink()
    code, _ = envelope(repo, "assemble", "M1")
    assert code == 5


def test_assemble_fragment_error_exit_1_writes_nothing(repo: Path) -> None:
    _, split = envelope(repo, "split", "M1")
    write_fragments(repo, split["data"]["batches"])
    part = repo / split["data"]["batches"][1]["part_path"]
    data = yaml.safe_load(part.read_text())
    data["tasks"][0]["zones"] = ["web"]
    part.write_text(yaml.safe_dump(data))
    before = (repo / "plans" / "M1.yml").read_bytes()
    code, env = envelope(repo, "assemble", "M1")
    assert code == 1
    assert env["data"]["written"] is False
    assert [e["code"] for e in env["data"]["errors"]] == ["skeleton_field"]
    errors = [m for m in env["messages"] if m["level"] == "error"]
    assert errors[0]["text"] == "plan M1 has 1 error(s); nothing written"
    assert (repo / "plans" / "M1.yml").read_bytes() == before


def test_assemble_skeleton_changed_exit_1(repo: Path) -> None:
    _, split = envelope(repo, "split", "M1")
    write_fragments(repo, split["data"]["batches"])
    write_plan(repo, skeleton(13))
    code, env = envelope(repo, "assemble", "M1")
    assert code == 1
    assert [e["code"] for e in env["data"]["errors"]] == ["skeleton_changed"]


def test_assemble_human_output(repo: Path) -> None:
    _, split = envelope(repo, "split", "M1")
    write_fragments(repo, split["data"]["batches"])
    result = invoke(repo, "plan", "assemble", "M1")
    assert result.exit_code == 0
    assert "Plan assemble M1  plans/M1.yml  2 batch(es), 12 task(s), 0 error(s)" in result.stdout
    assert "Plan written" in result.stdout
    again = invoke(repo, "plan", "assemble", "M1")
    assert "Plan written (unchanged)" in again.stdout


def test_human_output_markup_is_literal() -> None:
    split = {
        "milestone": "M1",
        "file": "[bold]x[/bold]",
        "batches_path": "b",
        "agents": 1,
        "max_per_agent": 10,
        "batches": [{"index": 1, "task_ids": ["[red]t[/red]"], "part_path": "p"}],
        "removed_parts": [],
        "errors": [],
        "warnings": [],
        "subagent": {"name": "forge-planner", "available": True, "reason": "[i]r[/i]"},
    }
    out = render_split(split, width=200)
    assert "[bold]x[/bold]" in out and "[red]t[/red]" in out and "[i]r[/i]" in out
    assembled = {
        "milestone": "M1",
        "file": "[bold]x[/bold]",
        "written": False,
        "changed": False,
        "batches": [],
        "tasks": [],
        "errors": [
            {
                "level": "error",
                "code": "c",
                "message": "[red]m[/red]",
                "file": None,
                "task": None,
                "field": None,
                "hint": None,
            }
        ],
        "warnings": [],
        "counts": {"batches": 0, "tasks": 0, "errors": 1, "warnings": 0},
    }
    out = render_assemble(assembled, width=200)
    assert "[bold]x[/bold]" in out and "[red]m[/red]" in out and "Plan not written" in out
