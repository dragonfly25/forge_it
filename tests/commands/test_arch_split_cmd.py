"""End-to-end tests for `forge arch split` through the CLI (ADR 0020).

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
from forge.commands.arch import render_split
from forge.core.config import ForgeConfig
from forge.core.github import GitHubClient

if TYPE_CHECKING:
    from conftest import GitWorld

runner = CliRunner()

CONFIG = """\
forge_version: ">=0.2,<0.3"
project:
  name: shop
  default_branch: main
github:
  repo: acme/shop
models:
  min_tier_for:
    architecture: strong
"""

ZONES = """\
zones:
  z-a: {paths: ["src/a/**"]}
  z-b: {paths: ["src/b/**"]}
  z-c: {paths: ["src/c/**"]}
  z-d: {paths: ["src/d/**"]}
  z-e: {paths: ["src/e/**"]}
  old: {paths: ["src/old/**"]}
  shared: {paths: ["shared/**"], contract: true}
"""

SPEC = """\
---
milestone: M1
status: ready
---
# Editor
## Requirements
- **AC-1** api
"""

COMPONENTS = ["svc-a", "svc-b", "svc-c", "svc-d", "svc-e"]


def _front(components: list[str] = COMPONENTS, **extra: Any) -> dict[str, Any]:
    return {
        "milestone": "M1",
        "status": "draft",
        "specs": ["specs/m1/editor.md"],
        "components": [{"id": c, "zones": [f"z-{c[-1]}"], "summary": c} for c in components],
        "interfaces": [
            {
                "id": "IF-a-api",
                "zone": "shared",
                "producer": "svc-a",
                "consumers": ["svc-b"],
                "summary": "A",
            },
            {
                "id": "IF-e-api",
                "zone": "shared",
                "producer": "svc-e",
                "consumers": [],
                "summary": "E",
            },
            {
                "id": "IF-old-api",
                "zone": "shared",
                "producer": "old-svc",
                "consumers": ["svc-a"],
                "summary": "Old",
            },
        ],
        **extra,
    }


OVERVIEW = """\
---
milestones: [M0]
components:
  - id: old-svc
    zones: [old]
    summary: Old service
interfaces: []
---
# System overview
"""

PROFILE_CLAUDE = """\
github: alice
agents:
  - id: claude
    tool: claude-code
    providers: [anthropic]
    tiers: {strong: opus, standard: sonnet, light: haiku}
default_agent: claude
"""

PROFILE_LOCAL = """\
github: alice
agents:
  - id: small
    tool: ollama
    providers: [local]
    tiers: {light: qwen}
default_agent: small
"""


def _no_github(config: ForgeConfig) -> GitHubClient:
    raise AssertionError("arch split must not touch GitHub")


def write_arch(root: Path, front: dict[str, Any], body: str = "# M1 (body pending)\n") -> None:
    text = "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body
    (root / "docs" / "architecture" / "M1.md").write_text(text)


@pytest.fixture
def repo(git_world: GitWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = git_world.clone("alice").cwd
    (root / ".gitignore").write_text(".forge/local/\n")
    (root / ".forge").mkdir()
    (root / ".forge" / "config.yml").write_text(CONFIG)
    (root / ".forge" / "zones.yml").write_text(ZONES)
    (root / "specs" / "m1").mkdir(parents=True)
    (root / "specs" / "m1" / "editor.md").write_text(SPEC)
    (root / "docs" / "architecture").mkdir(parents=True)
    (root / "docs" / "architecture" / "overview.md").write_text(OVERVIEW)
    write_arch(root, _front())
    xdg = tmp_path / "xdg"
    (xdg / "forge").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("FORGE_AGENT", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(_context, "github_factory", _no_github)
    return root


def set_profile(text: str) -> None:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "forge" / "profile.yml"
    path.write_text(text)


def invoke(root: Path, *args: str) -> Any:
    return runner.invoke(app, ["--repo", str(root), *args])


def envelope(root: Path, *args: str) -> tuple[int, dict[str, Any]]:
    result = invoke(root, "--json", "arch", "split", *args)
    return result.exit_code, json.loads(result.stdout)


def set_planning(root: Path, block: str) -> None:
    (root / ".forge" / "config.yml").write_text(CONFIG + block)


# --------------------------------------------------------------------------- happy path


def test_split_json_shape_with_default_max(repo: Path) -> None:
    code, env = envelope(repo, "M1")
    assert code == 0, env
    assert set(env) == {"ok", "code", "data", "messages"}
    data = env["data"]
    assert set(data) == {
        "milestone",
        "file",
        "agents",
        "max_per_agent",
        "batches",
        "unassigned_interfaces",
        "removed_parts",
        "errors",
        "warnings",
        "subagent",
    }
    assert data["milestone"] == "M1"
    assert data["file"] == "docs/architecture/M1.md"
    # No planning block: the default max_components_per_agent is 3; 5 components → 3 + 2.
    assert data["max_per_agent"] == 3 and data["agents"] == 2
    assert data["batches"] == [
        {
            "index": 1,
            "component_ids": ["svc-a", "svc-b", "svc-c"],
            "interface_ids": ["IF-a-api"],
            "part_path": ".forge/local/arch/M1/part-1.md",
        },
        {
            "index": 2,
            "component_ids": ["svc-d", "svc-e"],
            "interface_ids": ["IF-e-api"],
            "part_path": ".forge/local/arch/M1/part-2.md",
        },
    ]
    assert data["unassigned_interfaces"] == ["IF-old-api"]
    assert data["removed_parts"] == [] and data["errors"] == []
    assert (repo / ".forge" / "local" / "arch" / "M1").is_dir()
    assert list((repo / ".forge" / "local" / "arch" / "M1").iterdir()) == []


def test_max_components_per_agent_from_config(repo: Path) -> None:
    set_planning(repo, "planning:\n  max_components_per_agent: 2\n")
    code, env = envelope(repo, "M1")
    assert code == 0
    assert env["data"]["max_per_agent"] == 2
    assert [b["component_ids"] for b in env["data"]["batches"]] == [
        ["svc-a", "svc-b"],
        ["svc-c", "svc-d"],
        ["svc-e"],
    ]
    assert [b["interface_ids"] for b in env["data"]["batches"]] == [
        ["IF-a-api"],
        [],
        ["IF-e-api"],
    ]


def test_single_agent_below_threshold(repo: Path) -> None:
    set_planning(repo, "planning:\n  max_components_per_agent: 5\n")
    code, env = envelope(repo, "M1")
    assert code == 0
    assert env["data"]["agents"] == 1
    assert env["data"]["batches"][0]["component_ids"] == COMPONENTS


def test_missing_sections_are_tolerated(repo: Path) -> None:
    code, env = envelope(repo, "M1")
    assert code == 0
    assert "missing_section" not in [e["code"] for e in env["data"]["errors"]]


def test_removes_stale_parts(repo: Path) -> None:
    directory = repo / ".forge" / "local" / "arch" / "M1"
    directory.mkdir(parents=True)
    (directory / "part-1.md").write_text("old")
    (directory / "part-4.md").write_text("old")
    code, env = envelope(repo, "M1")
    assert code == 0
    assert env["data"]["removed_parts"] == [
        ".forge/local/arch/M1/part-1.md",
        ".forge/local/arch/M1/part-4.md",
    ]
    assert list(directory.iterdir()) == []


def test_deterministic(repo: Path) -> None:
    first = envelope(repo, "M1")
    shuffled = _front(list(reversed(COMPONENTS)))
    shuffled["interfaces"] = list(reversed(shuffled["interfaces"]))
    write_arch(repo, shuffled)
    second = envelope(repo, "M1")
    assert first == second


# --------------------------------------------------------------------------- subagent block


def test_subagent_without_profile(repo: Path) -> None:
    _, env = envelope(repo, "M1")
    subagent = env["data"]["subagent"]
    assert subagent["name"] == "forge-arch-writer"
    assert subagent["available"] is False and "forge setup" in subagent["reason"]


def test_subagent_not_rendered(repo: Path) -> None:
    set_profile(PROFILE_CLAUDE)
    _, env = envelope(repo, "M1")
    subagent = env["data"]["subagent"]
    assert subagent["available"] is False
    assert "forge adapters generate --local" in subagent["reason"]


def test_subagent_rendered_for_claude(repo: Path) -> None:
    set_profile(PROFILE_CLAUDE)
    rendered = repo / ".claude" / "agents" / "forge-arch-writer.md"
    rendered.parent.mkdir(parents=True)
    rendered.write_text("---\nname: forge-arch-writer\n---\n")
    _, env = envelope(repo, "M1")
    assert env["data"]["subagent"] == {
        "name": "forge-arch-writer",
        "available": True,
        "reason": "forge-arch-writer is rendered for claude-code",
    }


def test_subagent_tool_without_subagents(repo: Path) -> None:
    set_profile(PROFILE_LOCAL)
    code, env = envelope(repo, "M1")
    # No tier check for split: a light agent may run it.
    assert code == 0
    assert env["data"]["subagent"]["available"] is False
    assert "ollama" in env["data"]["subagent"]["reason"]


# --------------------------------------------------------------------------- exit codes


def test_frontmatter_error_exit_1_and_nothing_written(repo: Path) -> None:
    front = _front()
    front["components"][0]["zones"] = ["nowhere"]
    write_arch(repo, front)
    code, env = envelope(repo, "M1")
    assert code == 1
    assert env["ok"] is False
    assert [e["code"] for e in env["data"]["errors"]] == ["unknown_zone"]
    assert env["data"]["batches"] == [] and env["data"]["agents"] == 0
    errors = [m for m in env["messages"] if m["level"] == "error"]
    assert errors[0]["text"] == "architecture M1 has 1 error(s); not split"
    assert not (repo / ".forge" / "local" / "arch").exists()


def test_frontmatter_error_keeps_stale_parts(repo: Path) -> None:
    directory = repo / ".forge" / "local" / "arch" / "M1"
    directory.mkdir(parents=True)
    (directory / "part-1.md").write_text("old")
    write_arch(repo, _front(owner="me"))
    code, _ = envelope(repo, "M1")
    assert code == 1
    assert (directory / "part-1.md").is_file()


def test_overview_producer_unknown_without_overview_exit_1(repo: Path) -> None:
    (repo / "docs" / "architecture" / "overview.md").unlink()
    code, env = envelope(repo, "M1")
    assert code == 1
    assert "unknown_component" in [e["code"] for e in env["data"]["errors"]]


def test_invalid_milestone_exit_1(repo: Path) -> None:
    code, env = envelope(repo, "first")
    assert code == 1
    assert [e["code"] for e in env["data"]["errors"]] == ["invalid_milestone"]


def test_invalid_zones_exit_1(repo: Path) -> None:
    (repo / ".forge" / "zones.yml").write_text("zones: [not, a, mapping]\n")
    code, env = envelope(repo, "M1")
    assert code == 1
    assert any("zones.yml" in m["text"] for m in env["messages"])


def test_missing_architecture_exit_5(repo: Path) -> None:
    code, env = envelope(repo, "M7")
    assert code == 5
    assert env["ok"] is False
    assert [e["code"] for e in env["data"]["errors"]] == ["architecture_missing"]
    [error] = [m for m in env["messages"] if m["level"] == "error"]
    assert error["text"] == "docs/architecture/M7.md not found"
    assert "write the frontmatter first" in error["hint"]
    assert not (repo / ".forge" / "local" / "arch").exists()


def test_not_ignored_exit_5(repo: Path) -> None:
    (repo / ".gitignore").write_text("")
    code, env = envelope(repo, "M1")
    assert code == 5
    assert env["ok"] is False
    assert env["code"] == 5
    assert [e["code"] for e in env["data"]["errors"]] == ["not_ignored"]
    assert env["data"]["batches"] == [] and env["data"]["agents"] == 0
    [error] = [m for m in env["messages"] if m["level"] == "error"]
    assert error["text"] == "git would not ignore .forge/local/arch/M1/ (.gitignore)"
    assert ".gitignore" in error["hint"]
    assert not (repo / ".forge" / "local").exists()


def test_no_components_exit_0_with_zero_agents(repo: Path) -> None:
    front = _front([])
    front["interfaces"] = []
    write_arch(repo, front)
    code, env = envelope(repo, "M1")
    assert code == 0, env
    assert env["data"]["agents"] == 0 and env["data"]["batches"] == []


def test_no_config_exit_5(repo: Path) -> None:
    (repo / ".forge" / "config.yml").unlink()
    code, env = envelope(repo, "M1")
    assert code == 5
    assert ".forge/config.yml" in env["messages"][0]["text"]


def test_version_mismatch_is_a_warning(repo: Path) -> None:
    (repo / ".forge" / "config.yml").write_text(CONFIG.replace('">=0.2,<0.3"', '">=99"'))
    code, env = envelope(repo, "M1")
    assert code == 0
    assert any(m["level"] == "warning" for m in env["messages"])


# --------------------------------------------------------------------------- human output


def test_human_output(repo: Path) -> None:
    result = invoke(repo, "arch", "split", "M1")
    assert result.exit_code == 0
    out = result.stdout
    assert "Architecture split M1  docs/architecture/M1.md  2 agent(s)" in out
    assert "1: svc-a, svc-b, svc-c  interfaces IF-a-api  -> .forge/local/arch/M1/part-1.md" in out
    assert "IF-old-api" in out
    assert "forge-arch-writer not available" in out


def test_human_output_markup_is_literal() -> None:
    data = {
        "milestone": "M1",
        "file": "[bold]x[/bold]",
        "agents": 1,
        "max_per_agent": 3,
        "batches": [
            {
                "index": 1,
                "component_ids": ["[red]c[/red]"],
                "interface_ids": [],
                "part_path": "p",
            }
        ],
        "unassigned_interfaces": [],
        "removed_parts": [],
        "errors": [],
        "warnings": [],
        "subagent": {"name": "forge-arch-writer", "available": True, "reason": "[i]r[/i]"},
    }
    out = render_split(data, width=200)
    assert "[bold]x[/bold]" in out and "[red]c[/red]" in out and "[i]r[/i]" in out
