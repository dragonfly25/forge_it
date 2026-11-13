"""Tests for `forge config validate` (ADR 0018)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from forge import __version__
from forge.cli import app
from forge.commands import _context
from forge.core.config import ForgeConfig
from forge.core.git import Git
from forge.core.github import GitHubClient

if TYPE_CHECKING:
    from conftest import GitWorld

runner = CliRunner()

CONFIG = """\
forge_version: ">=0.2,<0.3"
project:
  name: shop
github:
  repo: acme/shop
"""

TEAM = """\
members:
  - github: alice
    roles: [dev, lead]
"""

ZONES = """\
zones:
  app: {paths: ["src/**"]}
  docs: {paths: ["README.md"]}
"""


def _no_github(config: ForgeConfig) -> GitHubClient:
    raise AssertionError("config validate must not touch GitHub")


@pytest.fixture
def repo(git_world: GitWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    git = git_world.clone("alice")
    root = git.cwd
    (root / ".forge").mkdir()
    (root / ".forge" / "config.yml").write_text(CONFIG)
    (root / ".forge" / "team.yml").write_text(TEAM)
    (root / ".forge" / "zones.yml").write_text(ZONES)
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("x\n")
    git.add()
    # No profile: XDG points to an empty directory.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("FORGE_AGENT", raising=False)
    monkeypatch.setattr(_context, "github_factory", _no_github)
    return root


def envelope(root: Path, *args: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["--json", "--repo", str(root), "config", "validate", *args])
    return result.exit_code, json.loads(result.stdout)


def test_clean_project_exit_0(repo: Path) -> None:
    code, env = envelope(repo)
    assert code == 0, env
    assert env["ok"] is True and env["code"] == 0
    assert env["messages"] == []
    data = env["data"]
    assert set(data["files"].values()) == {"ok"}
    assert data["leads"] == ["alice"]
    assert data["unzoned"] == {"count": 0, "sample": []}
    assert data["version"]["cli"] == __version__


def test_invalid_config_exit_1_and_other_files_checked(repo: Path) -> None:
    (repo / ".forge" / "config.yml").write_text("forge_version: [\n")
    (repo / "lib.py").write_text("x\n")
    Git(repo).add(["lib.py"])
    code, env = envelope(repo)
    assert code == 1
    assert env["ok"] is False and env["code"] == 1
    data = env["data"]
    assert data["files"][".forge/config.yml"] == "error"
    assert data["files"][".forge/team.yml"] == "ok"
    assert data["unzoned"] == {"count": 1, "sample": ["lib.py"]}
    errors = [m for m in env["messages"] if m["level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["text"].startswith(".forge/config.yml: invalid YAML")
    assert errors[0]["hint"] == data["errors"][0]["fix"]
    warnings = [m for m in env["messages"] if m["level"] == "warning"]
    assert warnings[0]["text"].startswith(".forge/zones.yml: 1 tracked file(s) match no zone")
    assert warnings[0]["hint"]


def test_warnings_only_exit_0(repo: Path) -> None:
    (repo / ".forge" / "team.yml").write_text(TEAM.replace("[dev, lead]", "[dev]"))
    code, env = envelope(repo)
    assert code == 0
    assert [m["level"] for m in env["messages"]] == ["warning"]
    assert env["data"]["warnings"][0]["code"] == "no_lead"


def test_version_mismatch_does_not_block(repo: Path) -> None:
    (repo / ".forge" / "config.yml").write_text(CONFIG.replace(">=0.2,<0.3", ">=9"))
    code, env = envelope(repo)
    assert code == 0
    assert env["data"]["version"]["ok"] is False
    assert env["data"]["warnings"][0]["code"] == "version_mismatch"


def test_missing_files(repo: Path) -> None:
    for name in ("config.yml", "team.yml", "zones.yml"):
        (repo / ".forge" / name).unlink()
    code, env = envelope(repo)
    assert code == 1
    assert [m["hint"] for m in env["messages"]] == ["run `forge init` to create it"] * 3


def test_runs_from_a_subdirectory(repo: Path) -> None:
    code, env = envelope(repo / "src")
    assert code == 0, env


def test_outside_a_repository(tmp_path: Path) -> None:
    code, env = envelope(tmp_path)
    assert code == 5
    assert env["ok"] is False


def test_human_output(repo: Path) -> None:
    (repo / ".forge" / "team.yml").write_text("members: 3\n")
    result = runner.invoke(app, ["--repo", str(repo), "config", "validate"])
    assert result.exit_code == 1
    out = result.stdout
    assert "Forge configuration: 1 error(s), 0 warning(s)" in out
    assert "  .forge/team.yml    error\n" in out
    assert "  .forge/zones.yml   ok\n" in out
    assert "Tracked files: 5, unzoned: 0" in out
    assert "Zones matching no file: none" in out
    assert "Version: forge" in out
    assert "Leads" not in out
    assert "error: .forge/team.yml: members:" in result.stderr
    assert "Fix: " in result.stderr


def test_human_output_lists_unzoned_sample(repo: Path) -> None:
    (repo / "lib").mkdir()
    for i in range(25):
        (repo / "lib" / f"f{i:02d}.py").write_text("x\n")
    Git(repo).add(["lib"])
    result = runner.invoke(app, ["--repo", str(repo), "config", "validate"])
    assert result.exit_code == 0
    assert "unzoned: 25" in result.stdout
    assert "  lib/f00.py" in result.stdout
    assert "  ... and 5 more" in result.stdout
    assert "Leads: alice" in result.stdout


def test_help_mentions_adr() -> None:
    result = runner.invoke(app, ["config", "--help"])
    assert result.exit_code == 0
    assert "ADR 0018" in result.stdout
