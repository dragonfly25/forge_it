"""End-to-end tests for `forge ship` through the CLI (ADR 0012).

Real bare remote and clones with a shared ``FakeGitHub`` (``support.ship_world``). The project
files (config, zones, team, spec) are committed on ``main`` before any claim; the profile
lives under a temporary ``XDG_CONFIG_HOME``; the clock and the GitHub factory are patched.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.commands import _claims, _context
from forge.core.config import ForgeConfig
from forge.core.github import GitHubClient
from forge.core.pr_body import BEGIN_MARKER
from support.scenarios import T0, Clock
from support.ship_world import ShipWorld

if TYPE_CHECKING:
    from conftest import GitWorld

runner = CliRunner()
ENVELOPE_KEYS = {"ok", "code", "data", "messages"}

CONFIG = """\
forge_version: ">=0.1,<0.2"
project:
  name: shop
  default_branch: main
github:
  repo: acme/shop
lease_hours: 24
checks:
  ok: "exit 0"
review:
  cross_provider: true
  required_approvals: 1
"""

ZONES_YML = """\
zones:
  api:
    paths: ["src/api/**"]
  web:
    paths: ["src/web/**"]
unzoned: {unzoned}
"""

TEAM_YML = """\
members:
  - github: alice
    roles: [dev]
    providers: [anthropic]
  - github: carol
    roles: [lead]
    providers: [anthropic]
  - github: bob
    roles: [dev]
    providers: [openai]
"""

PROFILE = """\
github: alice
agents:
  - id: claude
    tool: claude-code
    providers: [anthropic]
    tiers: {strong: opus, standard: sonnet, light: haiku}
    effort: {low: low, medium: medium, high: high}
default_agent: claude
"""


class Env:
    def __init__(self, sw: ShipWorld, xdg: Path) -> None:
        self.sw = sw
        self.xdg = xdg

    def repo(self) -> Path:
        return self.sw.world.git("alice").cwd

    def invoke(self, *args: str) -> tuple[int, dict[str, Any]]:
        result = runner.invoke(app, ["--json", "--repo", str(self.repo()), *args])
        envelope = json.loads(result.stdout)
        assert set(envelope) == ENVELOPE_KEYS, envelope
        assert envelope["ok"] is (envelope["code"] == 0)
        assert result.exit_code == envelope["code"], result.output
        return result.exit_code, envelope

    def human(self, *args: str) -> Any:
        return runner.invoke(app, ["--repo", str(self.repo()), *args])

    def claim(self) -> int:
        issue = self.sw.task("M1-T01", ("api",))
        self.sw.claim("alice", issue)
        return issue


def make_env(
    git_world: GitWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unzoned: str = "warn"
) -> Env:
    sw = ShipWorld(git_world)
    sw.upstream(
        {
            ".forge/config.yml": CONFIG,
            ".forge/zones.yml": ZONES_YML.format(unzoned=unzoned),
            ".forge/team.yml": TEAM_YML,
        },
        message="chore: forge project",
        link=False,
    )
    xdg = tmp_path / "xdg"
    (xdg / "forge").mkdir(parents=True)
    (xdg / "forge" / "profile.yml").write_text(PROFILE)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    def factory(config: ForgeConfig) -> GitHubClient:
        assert config.github.repo == "acme/shop"
        return sw.github("alice")  # type: ignore[return-value]

    monkeypatch.setattr(_context, "github_factory", factory)
    monkeypatch.setattr(_claims, "clock", Clock(T0 + timedelta(hours=1)))
    return Env(sw, xdg)


@pytest.fixture
def env(git_world: GitWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    return make_env(git_world, monkeypatch, tmp_path)


def test_ship_json_creates_pr_then_updates(env: Env) -> None:
    issue = env.claim()
    env.sw.commit("alice", "src/api/a.py", "a\n")

    code, out = env.invoke("ship", "--draft")

    assert code == 0, out["messages"]
    data = out["data"]
    assert data["outcome"] == "created" and data["issue"] == issue
    assert data["pr"]["created"] is True and data["pr"]["draft"] is True
    assert data["checks"]["skipped"] is True  # the sync ran them on this HEAD
    assert data["sync"]["outcome"] == "pushed"
    assert data["reviewers"]["requested"] == ["bob"]
    assert data["reviewers"]["selected"][0]["rank"] == 1
    assert "status:in-review" in data["labels"]["added"]
    pr = env.sw.pull(issue)
    assert pr.body.startswith(BEGIN_MARKER) and pr.draft
    assert "effort medium → `medium`" in pr.body

    code, again = env.invoke("ship")
    assert code == 0
    assert again["data"]["outcome"] == "updated"
    assert again["data"]["pr"]["body_changed"] is False
    assert again["data"]["reviewers"]["requested"] == []
    assert len(env.sw.pulls(issue)) == 1


def test_ship_human_output(env: Env) -> None:
    issue = env.claim()
    env.sw.commit("alice", "src/api/a.py", "a\n")
    env.sw.commit("alice", "src/web/w.js", "w\n")

    result = env.human("ship")

    assert result.exit_code == 0, result.output
    pr = env.sw.pull(issue)
    assert "Pull request opened" in result.stdout
    assert f"PR #{pr.number}: {pr.url}" in result.stdout
    assert "src/web/w.js  zone web" in result.stdout
    assert "Reviewers requested: @bob" in result.stdout


def test_ship_unzoned_error_exits_2(
    git_world: GitWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = make_env(git_world, monkeypatch, tmp_path, unzoned="error")
    issue = env.claim()
    env.sw.commit("alice", "README.md", "readme\n")

    code, out = env.invoke("ship")

    assert code == 2
    assert out["data"]["outcome"] == "unzoned"
    assert out["data"]["pr"] is None
    assert env.sw.pulls(issue) == []


def test_ship_sync_conflict_exit_2_renders_sync(env: Env) -> None:
    issue = env.claim()
    env.sw.commit("alice", "src/api/a.py", "alice\n")
    env.sw.upstream({"src/api/a.py": "bob\n"}, author="bob")

    result = env.human("ship")

    assert result.exit_code == 2, result.output
    assert "Sync stopped on conflicts" in result.stdout
    assert env.sw.pulls(issue) == []


def test_ship_without_current_task_exits_5(env: Env) -> None:
    code, out = env.invoke("ship")
    assert code == 5
    assert out["messages"][0]["level"] == "error"
