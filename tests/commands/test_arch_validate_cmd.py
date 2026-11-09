"""End-to-end tests for `forge arch validate` through the CLI (ADR 0019).

The repository is a real clone; GitHub access is replaced by a factory that fails, proving
the command is offline. The profile lives under a temporary ``XDG_CONFIG_HOME``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.commands import _context
from forge.commands.arch import render_validation
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
    architecture: strong
"""

ZONES = """\
zones:
  api: {paths: ["src/api/**"]}
  web: {paths: ["src/web/**"]}
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
## Open questions
"""

BODY = """\
# M1 architecture

## Context
c
## Components
c
## Interfaces
i
## Data model
No change
## Decisions
none
## Risks
none
## Open questions
"""

ARCH = (
    """\
---
milestone: M1
status: ready
specs: [specs/m1/editor.md]
components:
  - id: api-server
    zones: [api]
    summary: Serves the API
  - id: web-app
    zones: [web]
    summary: The web client
interfaces:
  - id: IF-editor-api
    zone: shared
    producer: api-server
    consumers: [web-app]
    summary: Editor endpoints
---
"""
    + BODY
)

OVERVIEW = """\
---
milestones: [M1]
components:
  - id: api-server
    zones: [api]
    summary: Serves the API
  - id: web-app
    zones: [web]
    summary: The web client
interfaces:
  - id: IF-editor-api
    zone: shared
    producer: api-server
    consumers: [web-app]
    summary: Editor endpoints
---
# System overview
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


def _no_github(config: ForgeConfig) -> GitHubClient:
    raise AssertionError("arch validate must not touch GitHub")


@pytest.fixture
def repo(git_world: GitWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = git_world.clone("alice").cwd
    (root / ".forge").mkdir()
    (root / ".forge" / "config.yml").write_text(CONFIG)
    (root / ".forge" / "zones.yml").write_text(ZONES)
    (root / "specs" / "m1").mkdir(parents=True)
    (root / "specs" / "m1" / "editor.md").write_text(SPEC)
    (root / "docs" / "architecture").mkdir(parents=True)
    (root / "docs" / "architecture" / "M1.md").write_text(ARCH)
    (root / "docs" / "architecture" / "overview.md").write_text(OVERVIEW)
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
    result = invoke(root, "--json", "arch", "validate", *args)
    return result.exit_code, json.loads(result.stdout)


# --------------------------------------------------------------------------- JSON


def test_clean_architecture_json(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    code, env = envelope(repo, "M1")
    assert code == 0
    assert set(env) == {"ok", "code", "data", "messages"}
    assert env["ok"] is True and env["code"] == 0
    data = env["data"]
    assert set(data) == {
        "milestone",
        "file",
        "ok",
        "status",
        "specs",
        "components",
        "interfaces",
        "overview",
        "errors",
        "warnings",
        "counts",
    }
    assert data["milestone"] == "M1"
    assert data["file"] == "docs/architecture/M1.md"
    assert data["ok"] is True and data["status"] == "ready"
    assert data["specs"] == ["specs/m1/editor.md"]
    assert data["components"] == ["api-server", "web-app"]
    assert data["interfaces"] == ["IF-editor-api"]
    assert data["overview"] == {
        "path": "docs/architecture/overview.md",
        "present": True,
        "listed": True,
    }
    assert data["errors"] == [] and data["warnings"] == []
    assert data["counts"] == {"components": 2, "interfaces": 1, "errors": 0, "warnings": 0}
    assert env["messages"] == []


def test_warnings_exit_0(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    (repo / "docs" / "architecture" / "overview.md").unlink()
    code, env = envelope(repo, "M1")
    assert code == 0
    assert env["ok"] is True
    assert [w["code"] for w in env["data"]["warnings"]] == ["overview_stale"]
    assert env["data"]["overview"]["present"] is False
    assert env["messages"] == []


def test_errors_exit_1_with_summary(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    text = ARCH.replace("zones: [web]", "zones: [nowhere]")
    (repo / "docs" / "architecture" / "M1.md").write_text(text)
    code, env = envelope(repo, "M1")
    assert code == 1
    assert env["ok"] is False and env["code"] == 1
    assert env["data"]["ok"] is False
    assert "unknown_zone" in [e["code"] for e in env["data"]["errors"]]
    errors = [m for m in env["messages"] if m["level"] == "error"]
    count = len(env["data"]["errors"])
    assert [m["text"] for m in errors] == [f"architecture M1 has {count} error(s)"]


def test_missing_architecture_exit_1(repo: Path) -> None:
    code, env = envelope(repo, "M7")
    assert code == 1
    assert env["data"]["errors"][0]["code"] == "architecture_missing"


def test_invalid_milestone_exit_1(repo: Path) -> None:
    code, env = envelope(repo, "first")
    assert code == 1
    assert "invalid_milestone" in [e["code"] for e in env["data"]["errors"]]


def test_invalid_zones_exit_1(repo: Path) -> None:
    (repo / ".forge" / "zones.yml").write_text("zones: [not, a, mapping]\n")
    code, env = envelope(repo, "M1")
    assert code == 1
    assert env["ok"] is False
    assert any("zones.yml" in m["text"] for m in env["messages"])


def test_no_config_exit_5(repo: Path) -> None:
    (repo / ".forge" / "config.yml").unlink()
    code, env = envelope(repo, "M1")
    assert code == 5
    assert env["ok"] is False
    assert ".forge/config.yml" in env["messages"][0]["text"]
    assert "forge init" in env["messages"][0]["hint"]


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
    assert env["ok"] is False
    assert "min_tier_for.architecture" in env["messages"][0]["text"]


def test_tier_refusal_forced(repo: Path) -> None:
    set_profile(PROFILE_LIGHT)
    code, env = envelope(repo, "M1", "--force")
    assert code == 0
    assert any(
        m["level"] == "warning" and "--force" in m["text"] and "architecture" in m["text"]
        for m in env["messages"]
    )


def test_tier_skipped_in_github_actions(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_profile(PROFILE_LIGHT)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    code, env = envelope(repo, "M1")
    assert code == 0
    [message] = env["messages"]
    assert message["level"] == "info" and "GitHub Actions" in message["text"]


def test_tier_without_profile_is_info(repo: Path) -> None:
    code, env = envelope(repo, "M1")
    assert code == 0
    [message] = env["messages"]
    assert message["level"] == "info" and "no Forge profile" in message["text"]
    assert "min_tier_for.architecture" in message["text"]


def test_no_tier_configured_no_message(repo: Path) -> None:
    text = CONFIG.replace("models:\n  min_tier_for:\n    architecture: strong\n", "")
    (repo / ".forge" / "config.yml").write_text(text)
    set_profile(PROFILE_LIGHT)
    code, env = envelope(repo, "M1")
    assert code == 0
    assert env["messages"] == []


# --------------------------------------------------------------------------- human output


def test_human_output_clean(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    result = invoke(repo, "arch", "validate", "M1")
    assert result.exit_code == 0
    out = result.stdout
    assert "Architecture M1  docs/architecture/M1.md  status ready" in out
    assert "2 component(s), 1 interface(s), 0 error(s)" in out
    assert "api-server, web-app" in out
    assert "IF-editor-api" in out
    assert "docs/architecture/overview.md  lists M1" in out


def test_human_output_errors(repo: Path) -> None:
    set_profile(PROFILE_STRONG)
    text = ARCH.replace("zones: [web]", "zones: [nowhere]")
    (repo / "docs" / "architecture" / "M1.md").write_text(text)
    result = invoke(repo, "arch", "validate", "M1")
    assert result.exit_code == 1
    assert "Errors (" in result.stdout
    assert "unknown_zone  docs/architecture/M1.md" in result.stdout
    assert "error: architecture M1 has" in result.stderr


def test_markup_is_printed_literally() -> None:
    data = {
        "milestone": "M1",
        "file": "docs/architecture/M1.md",
        "status": "draft",
        "specs": [],
        "components": ["[red]x[/red]"],
        "interfaces": [],
        "overview": {"path": "docs/architecture/overview.md", "present": False, "listed": False},
        "counts": {"components": 1, "interfaces": 0, "errors": 1, "warnings": 0},
        "errors": [
            {
                "code": "unknown_zone",
                "message": "unknown zone '[red]x[/red]' :smile:",
                "file": "docs/architecture/M1.md",
                "task": None,
                "field": "components[0].zones[0]",
                "hint": "[bold]fix[/bold]",
            }
        ],
        "warnings": [],
    }
    out = render_validation(data, width=100)
    assert "[red]x[/red]" in out
    assert ":smile:" in out
    assert "[bold]fix[/bold]" in out
    assert "missing" in out
