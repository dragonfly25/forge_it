"""End-to-end tests for `forge propose` through the CLI (ADR 0012)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.commands import _context
from forge.core.config import ForgeConfig
from forge.core.github import GitHubClient
from forge.core.propose import PROPOSAL_MARKER
from support.review_world import ReviewWorld

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
"""

PROFILE = """\
github: alice
agents:
  - id: claude
    tool: claude-code
    providers: [anthropic]
    tiers: {strong: opus, standard: sonnet, light: haiku}
default_agent: claude
"""


@pytest.fixture
def world(git_world: GitWorld) -> ReviewWorld:
    return ReviewWorld(git_world)


def install(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, profile: str | None
) -> Path:
    repo = world.alice.cwd
    (repo / ".forge").mkdir(exist_ok=True)
    (repo / ".forge" / "config.yml").write_text(CONFIG)
    xdg = tmp_path / "xdg"
    (xdg / "forge").mkdir(parents=True, exist_ok=True)
    if profile is not None:
        (xdg / "forge" / "profile.yml").write_text(profile)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("FORGE_AGENT", raising=False)
    world.fake.fake_set_user("alice")

    def factory(cfg: ForgeConfig) -> GitHubClient:
        return world.fake

    monkeypatch.setattr(_context, "github_factory", factory)
    return repo


def invoke(repo: Path, tmp_path: Path, title: str, body: str = "Details.\n") -> tuple[int, Any]:
    path = tmp_path / "proposal.md"
    path.write_text(body)
    result = runner.invoke(
        app,
        ["--json", "--repo", str(repo), "propose", "--title", title, "--body-file", str(path)],
    )
    envelope = json.loads(result.stdout)
    assert set(envelope) == {"ok", "code", "data", "messages"}
    return result.exit_code, envelope


def test_propose_from_task_branch(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    n = world.task()
    repo = install(world, monkeypatch, tmp_path, profile=PROFILE)
    world.alice.run("checkout", "--quiet", "-b", f"task/{n}")
    code, env = invoke(repo, tmp_path, "Add caching")
    assert code == 0 and env["data"]["created"] is True
    issue = world.fake.get_issue(env["data"]["issue"]["number"])
    assert issue.labels == ("status:proposed",)
    assert issue.milestone == "M1"
    assert issue.body.endswith(
        f"{PROPOSAL_MARKER}\nProposed by @alice (agent claude) while working on M1-T01 (#{n})\n"
    )


def test_propose_without_profile_or_task(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = install(world, monkeypatch, tmp_path, profile=None)
    code, env = invoke(repo, tmp_path, "Add caching")
    assert code == 0
    issue = world.fake.get_issue(env["data"]["issue"]["number"])
    assert issue.body.endswith(f"{PROPOSAL_MARKER}\nProposed by @alice\n")
    assert issue.milestone is None


def test_propose_is_idempotent(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = install(world, monkeypatch, tmp_path, profile=PROFILE)
    _, first = invoke(repo, tmp_path, "Add caching")
    code, again = invoke(repo, tmp_path, "add CACHING ")
    assert code == 0 and again["data"]["created"] is False
    assert again["data"]["issue"]["number"] == first["data"]["issue"]["number"]
    assert len(world.fake.list_issues(labels=["status:proposed"])) == 1


def test_missing_body_file_is_error(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = install(world, monkeypatch, tmp_path, profile=PROFILE)
    result = runner.invoke(
        app,
        [
            "--json",
            "--repo",
            str(repo),
            "propose",
            "--title",
            "X",
            "--body-file",
            str(tmp_path / "missing.md"),
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["ok"] is False


def test_human_output(world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = install(world, monkeypatch, tmp_path, profile=PROFILE)
    path = tmp_path / "p.md"
    path.write_text("x")
    result = runner.invoke(
        app, ["--repo", str(repo), "propose", "--title", "Idea [x]", "--body-file", str(path)]
    )
    assert result.exit_code == 0
    assert "Proposed #" in result.stdout and "Idea [x]" in result.stdout
