"""End-to-end tests for `forge refresh` through the CLI (git_world clone + FakeGitHub)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.commands import _context
from forge.commands._output import GlobalOptions
from forge.core.config import ForgeConfig
from forge.core.git import Git
from forge.core.github import GitHubClient
from forge.core.github_fake import FakeGitHub
from forge.core.tasks import (
    TRAILER_AGENT,
    TRAILER_CLAIM,
    TRAILER_TASK,
    Size,
    TaskMetadata,
    TaskType,
    upsert_block,
)
from forge.core.tiers import Tier

if TYPE_CHECKING:
    from conftest import GitWorld

runner = CliRunner()

CONFIG = """\
forge_version: ">=0.1"
project:
  name: shop
  default_branch: main
github:
  repo: acme/shop
"""


def body(task_id: str, deps: Sequence[str] = ()) -> str:
    meta = TaskMetadata(
        id=task_id,
        spec="specs/m1/x.md",
        covers=("AC-1",),
        depends_on=tuple(deps),
        zones=("api",),
        size=Size.S,
        tier=Tier.STANDARD,
        type=TaskType.IMPL,
        done_when=("tests pass",),
    )
    return upsert_block(f"Task {task_id}.\n", meta)


@pytest.fixture
def setup(git_world: GitWorld, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, FakeGitHub]:
    """A clone with a config, a fake GitHub with T1 <- T2 <- T3 and a claimed T4."""
    fake = FakeGitHub(repo="acme/shop")
    fake.create_label("forge:task", "5319e7")
    for task_id, deps in (("M1-T01", ()), ("M1-T02", ("M1-T01",)), ("M1-T03", ("M1-T02",))):
        fake.create_issue(f"[{task_id}]", body(task_id, deps), labels=["forge:task"])
    t4 = fake.create_issue("[M1-T04]", body("M1-T04"), labels=["forge:task"])
    assert t4.number == 4

    claimer = git_world.clone("bob")
    trailers = {TRAILER_TASK: "M1-T04", TRAILER_CLAIM: "bob", TRAILER_AGENT: "codex"}
    sha = claimer.commit_tree(claimer.tree_of("HEAD"), ["HEAD"], "chore: claim", trailers)
    assert claimer.push(sha, "refs/heads/task/4").ok

    clone = git_world.clone("alice")
    (clone.cwd / ".forge").mkdir()
    (clone.cwd / ".forge" / "config.yml").write_text(CONFIG)

    def factory(config: ForgeConfig) -> GitHubClient:
        assert config.github.repo == "acme/shop"
        return fake

    monkeypatch.setattr(_context, "github_factory", factory)
    return clone.cwd, fake


def run_json(repo: Path) -> dict[str, Any]:
    result = runner.invoke(app, ["--json", "--repo", str(repo), "refresh"])
    assert result.exit_code == 0, result.output
    envelope: dict[str, Any] = json.loads(result.stdout)
    return envelope


def statuses(fake: FakeGitHub) -> dict[int, list[str]]:
    return {
        i.number: [lb for lb in i.labels if lb.startswith("status:")]
        for i in fake.list_issues(state="all")
    }


def test_refresh_end_to_end(setup: tuple[Path, FakeGitHub]) -> None:
    repo, fake = setup

    env = run_json(repo)
    assert env["ok"] is True and env["code"] == 0
    changes = {c["issue"]: (c["id"], c["from"], c["to"]) for c in env["data"]["changes"]}
    assert changes == {
        1: ("M1-T01", None, "ready"),
        2: ("M1-T02", None, "blocked"),
        3: ("M1-T03", None, "blocked"),
        4: ("M1-T04", None, "in-progress"),
    }
    assert env["data"]["warnings"] == []
    assert env["data"]["counts"]["changed"] == 4
    assert statuses(fake) == {
        1: ["status:ready"],
        2: ["status:blocked"],
        3: ["status:blocked"],
        4: ["status:in-progress"],
    }

    fake.update_issue(1, state="closed")
    env = run_json(repo)
    changes = {c["issue"]: (c["from"], c["to"]) for c in env["data"]["changes"]}
    assert changes == {1: ("ready", "done"), 2: ("blocked", "ready")}

    env = run_json(repo)
    assert env["data"]["changes"] == []
    assert env["data"]["counts"]["changed"] == 0


def test_refresh_warnings_become_messages(setup: tuple[Path, FakeGitHub]) -> None:
    repo, fake = setup
    fake.create_issue("no block", "just text", labels=["forge:task"])
    env = run_json(repo)
    warnings = [m for m in env["messages"] if m["level"] == "warning"]
    assert [m["text"] for m in warnings] == env["data"]["warnings"]
    assert len(warnings) == 1 and warnings[0]["text"].startswith("issue #5:")


def test_refresh_text_mode(setup: tuple[Path, FakeGitHub]) -> None:
    repo, _ = setup
    result = runner.invoke(app, ["--repo", str(repo), "refresh"])
    assert result.exit_code == 0, result.output
    assert "M1-T02" in result.stdout and "blocked" in result.stdout
    assert "4 tasks updated" in result.stderr


def test_refresh_missing_config(git_world: GitWorld) -> None:
    clone = git_world.clone("carol")
    result = runner.invoke(app, ["--json", "--repo", str(clone.cwd), "refresh"])
    assert result.exit_code == 1
    env = json.loads(result.stdout)
    assert env["ok"] is False and env["code"] == 1
    (message,) = env["messages"]
    assert "config.yml" in message["text"] and "file not found" in message["text"]
    assert message["hint"] == "run `forge init` to create it"


def test_refresh_not_a_git_repo(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--json", "--repo", str(tmp_path), "refresh"])
    assert result.exit_code == 5
    env = json.loads(result.stdout)
    assert env["code"] == 5
    assert "not inside a git repository" in env["messages"][0]["text"]
    assert env["messages"][0]["hint"]


def test_refresh_repo_not_a_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--json", "--repo", str(tmp_path / "missing"), "refresh"])
    assert result.exit_code == 5


def test_find_repo_root_from_subdirectory(git_world: GitWorld) -> None:
    clone = git_world.clone("dave")
    sub = clone.cwd / "a" / "b"
    sub.mkdir(parents=True)
    root = _context.find_repo_root(GlobalOptions(repo=sub))
    assert root.resolve() == Git(clone.cwd).root().resolve()
