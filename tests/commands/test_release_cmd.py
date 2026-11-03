"""End-to-end tests for `forge release` through the CLI (ADR 0015).

The repository is a real clone of a bare remote; GitHub is a FakeGitHub injected through
``_context.github_factory``; the clock is ``_claims.clock``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.commands import _claims, _context
from forge.core.config import ForgeConfig
from forge.core.github import GitHubClient
from forge.core.tasks import TaskType
from support.release_world import ReleaseWorld

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
"""

ROADMAP = """\
milestones:
  - id: M1
    title: "[red]Editor[/red]"
    lead: lead
"""


@pytest.fixture
def world(git_world: GitWorld, monkeypatch: pytest.MonkeyPatch) -> ReleaseWorld:
    w = ReleaseWorld(git_world)
    w.task(1, "[bold]Login[/bold] form", TaskType.IMPL)
    w.task(2, "Contract", TaskType.CONTRACT)
    root = w.lead.cwd
    (root / ".forge").mkdir()
    (root / ".forge" / "config.yml").write_text(CONFIG)
    (root / "plans").mkdir()
    (root / "plans" / "roadmap.yml").write_text(ROADMAP)

    def factory(config: ForgeConfig) -> GitHubClient:
        assert config.github.repo == "acme/shop"
        return w.fake  # type: ignore[return-value]

    monkeypatch.setattr(_context, "github_factory", factory)
    monkeypatch.setattr(_claims, "clock", lambda: datetime(2026, 11, 3, 12, tzinfo=UTC))
    return w


def invoke(root: Path, *args: str) -> Any:
    return runner.invoke(app, ["--repo", str(root), *args])


def envelope(root: Path, *args: str) -> tuple[int, dict[str, Any]]:
    result = invoke(root, "release", *args, "--json")
    return result.exit_code, json.loads(result.stdout)


def test_json_two_steps(world: ReleaseWorld) -> None:
    root = world.lead.cwd
    code, env = envelope(root, "M1", "--version", "1.2.0")
    assert code == 0, env
    assert env["ok"] is True and env["data"]["step"] == "pr_opened"
    assert env["data"]["pull"]["created"] is True
    assert env["data"]["section"].startswith("## [1.2.0] - 2026-11-03\n")

    world.merge_release()
    code, env = envelope(root, "M1", "--version", "1.2.0")
    assert code == 0, env
    assert env["data"]["step"] == "released"
    assert env["data"]["release"]["name"] == "v1.2.0 — [red]Editor[/red]"
    assert world.git_world.remote_git.rev_parse("refs/tags/v1.2.0") == env["data"]["commit"]
    assert world.fake.list_milestones()[0].state == "closed"

    code, env = envelope(root, "M1", "--version", "1.2.0")  # idempotent re-run
    assert code == 0 and env["data"]["tag_pushed"] is False


def test_missing_version_exit_5_json(world: ReleaseWorld) -> None:
    code, env = envelope(world.lead.cwd, "M1")
    assert code == 5 and env["ok"] is False
    errors = [m for m in env["messages"] if m["level"] == "error"]
    assert errors[0]["text"] == "--version is required"
    assert "0.1.0" in errors[0]["hint"]
    assert env["data"]["latest_tag"] is None


def test_invalid_version_exit_5(world: ReleaseWorld) -> None:
    code, env = envelope(world.lead.cwd, "M1", "--version", "v1.2.0")
    assert code == 5
    assert "'v' prefix" in env["messages"][0]["text"]


def test_open_tasks_exit_5_lists_them(world: ReleaseWorld) -> None:
    number, _ = world.task(3, "Still open", closed=False, merged=False)
    code, env = envelope(world.lead.cwd, "M1", "--version", "1.2.0")
    assert code == 5
    assert [i["number"] for i in env["data"]["open_tasks"]] == [number]
    result = invoke(world.lead.cwd, "release", "M1", "--version", "1.2.0")
    assert result.exit_code == 5
    assert f"#{number}  [M1-T03] Still open" in result.stdout


def test_human_output_is_plain_and_dry_run_writes_nothing(world: ReleaseWorld) -> None:
    result = invoke(world.lead.cwd, "release", "M1", "--version", "1.2.0", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "v1.2.0 (dry run: nothing written) - next: Release pull request ready" in result.stdout
    assert "- [bold]Login[/bold] form (#2)" in result.stdout  # markup printed verbatim
    assert "would create branch release/v1.2.0" in result.stderr
    assert world.git_world.remote_git.rev_parse("refs/heads/release/v1.2.0") is None
    assert not world.fake.list_pulls(state="open")


def test_non_lead_warning(world: ReleaseWorld) -> None:
    world.fake.fake_set_user("mallory")
    code, env = envelope(world.lead.cwd, "M1", "--version", "1.2.0")
    assert code == 0
    assert any(
        m["level"] == "warning" and "mallory is not the lead" in m["text"] for m in env["messages"]
    )


def test_human_output_after_release(world: ReleaseWorld) -> None:
    invoke(world.lead.cwd, "release", "M1", "--version", "1.2.0")
    world.merge_release()
    result = invoke(world.lead.cwd, "release", "M1", "--version", "1.2.0")
    assert result.exit_code == 0, result.output
    assert "v1.2.0: Released" in result.stdout
    assert "GitHub release: v1.2.0 — [red]Editor[/red]" in result.stdout
    assert "milestone M1 closed" in result.stdout


def test_help_lists_the_options() -> None:
    result = runner.invoke(app, ["release", "--help"])
    assert result.exit_code == 0
    assert "--version" in result.stdout and "--dry-run" in result.stdout


def test_pre_push_hook_rejection_exits_2(world: ReleaseWorld) -> None:
    hook = world.lead.cwd / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho 'tests must pass' >&2\nexit 1\n")
    hook.chmod(0o755)
    code, env = envelope(world.lead.cwd, "M1", "--version", "1.2.0")
    assert code == 2 and env["code"] == 2
    assert "pre-push hook" in env["messages"][-1]["text"]
    assert world.git_world.remote_git.rev_parse("refs/heads/release/v1.2.0") is None


def test_unknown_milestone_exits_5(world: ReleaseWorld) -> None:
    code, env = envelope(world.lead.cwd, "M9", "--version", "1.2.0")
    assert code == 5
    assert "milestone 'M9' does not exist" in env["messages"][-1]["text"]


def test_version_of_another_milestone_exits_5(world: ReleaseWorld) -> None:
    envelope(world.lead.cwd, "M1", "--version", "1.2.0")
    world.merge_release()
    envelope(world.lead.cwd, "M1", "--version", "1.2.0")
    world.fake.create_milestone("M2")
    code, env = envelope(world.lead.cwd, "M2", "--version", "1.2.0")
    assert code == 5
    assert "already released for M1" in env["messages"][-1]["text"]
