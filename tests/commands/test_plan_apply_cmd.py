"""End-to-end tests for `forge plan apply` through the CLI (ADR 0013).

The repository is a real clone of a bare remote; GitHub is a FakeGitHub injected through
``_context.github_factory``. The profile lives under a temporary ``XDG_CONFIG_HOME``.
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
from forge.core.config import ForgeConfig
from forge.core.errors import PreconditionError
from forge.core.github import GitHubClient
from forge.core.github_fake import FakeGitHub
from forge.core.propose import ProposeContext, propose
from support.plan_world import RecordingGitHub

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
# Editor
## Requirements
- **AC-1** api
- **AC-2** web
## Open questions
"""

ROADMAP = """\
milestones:
  - id: M1
    title: "[red]Editor[/red]"
    goal: save a page
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
        "covers": [f"AC-{n}"],
        "depends_on": [],
        "zones": ["api" if n == 1 else "web"],
        "size": "S",
        "tier": "standard",
        "description": f"Do task {n}.\n",
        "done_when": ["tests pass"],
    }
    data.update(extra)
    return data


PLAN = [_task(1, title="[bold]Schema[/bold]"), _task(2, depends_on=["M1-T01"])]


def write_plan(root: Path, tasks: list[dict[str, Any]]) -> None:
    data = {"milestone": "M1", "tasks": tasks}
    (root / "plans" / "M1.yml").write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub(repo="acme/shop")


@pytest.fixture
def repo(
    git_world: GitWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub
) -> Path:
    root = git_world.clone("alice").cwd
    (root / ".forge").mkdir()
    (root / ".forge" / "config.yml").write_text(CONFIG)
    (root / ".forge" / "zones.yml").write_text(ZONES)
    (root / "specs" / "m1").mkdir(parents=True)
    (root / "specs" / "m1" / "editor.md").write_text(SPEC)
    (root / "plans").mkdir()
    (root / "plans" / "roadmap.yml").write_text(ROADMAP)
    write_plan(root, PLAN)
    xdg = tmp_path / "xdg"
    (xdg / "forge").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("FORGE_AGENT", raising=False)
    set_profile(PROFILE_STRONG)
    use_github(monkeypatch, fake)
    return root


def use_github(monkeypatch: pytest.MonkeyPatch, github: Any) -> None:
    def factory(config: ForgeConfig) -> GitHubClient:
        assert config.github.repo == "acme/shop"
        return github  # type: ignore[no-any-return]

    monkeypatch.setattr(_context, "github_factory", factory)


def set_profile(text: str) -> None:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "forge" / "profile.yml"
    path.write_text(text)


def invoke(root: Path, *args: str) -> Any:
    return runner.invoke(app, ["--repo", str(root), *args])


def envelope(root: Path, *args: str) -> tuple[int, dict[str, Any]]:
    result = invoke(root, "--json", "plan", "apply", *args)
    return result.exit_code, json.loads(result.stdout)


# --------------------------------------------------------------------------- JSON


def test_apply_json_then_idempotent(
    repo: Path, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, env = envelope(repo, "M1")
    assert code == 0, env
    assert env["ok"] is True and env["code"] == 0
    data = env["data"]
    assert data["applied"] is True and data["dry_run"] is False
    assert data["file"] == "plans/M1.yml"
    assert [(t["id"], t["action"]) for t in data["tasks"]] == [
        ("M1-T01", "create"),
        ("M1-T02", "create"),
    ]
    assert data["milestone_action"]["description"] == "[red]Editor[/red]: save a page"
    issues = fake.list_issues()
    assert [i.title for i in issues] == ["[M1-T01] [bold]Schema[/bold]", "[M1-T02] Task 2"]
    assert "status:ready" in issues[0].labels and "status:blocked" in issues[1].labels

    guard = RecordingGitHub(fake, read_only=True)
    use_github(monkeypatch, guard)
    code, env = envelope(repo, "M1")
    assert code == 0
    assert guard.writes == []
    assert env["data"]["counts"]["writes"] == 0
    assert any("already up to date" in m["text"] for m in env["messages"])


def test_dry_run_writes_nothing(
    repo: Path, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = RecordingGitHub(fake, read_only=True)
    use_github(monkeypatch, guard)
    code, env = envelope(repo, "M1", "--dry-run")
    assert code == 0
    assert guard.writes == []
    assert env["data"]["dry_run"] is True and env["data"]["applied"] is False
    assert [c["id"] for c in env["data"]["creates"]] == ["M1-T01", "M1-T02"]
    assert env["data"]["counts"]["writes"] > 0


def test_validation_errors_exit_1_nothing_written(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_github(config: ForgeConfig) -> GitHubClient:
        raise AssertionError("an invalid plan must not touch GitHub")

    monkeypatch.setattr(_context, "github_factory", no_github)
    write_plan(repo, [_task(1, size="L")])
    code, env = envelope(repo, "M1")
    assert code == 1
    assert env["ok"] is False
    assert env["data"]["applied"] is False
    assert env["data"]["validation"]["errors"][0]["code"] == "size_too_large"
    assert any(m["level"] == "error" for m in env["messages"])


def test_tier_refusal_exit_5_unless_force(repo: Path, fake: FakeGitHub) -> None:
    set_profile(PROFILE_LIGHT)
    code, env = envelope(repo, "M1")
    assert code == 5
    assert "min_tier_for.plan" in env["messages"][0]["text"]
    assert fake.list_issues(state="all") == []
    code, env = envelope(repo, "M1", "--force")
    assert code == 0
    assert any("--force" in m["text"] for m in env["messages"])
    assert len(fake.list_issues(state="all")) == 2


def test_version_mismatch_exit_5(repo: Path, fake: FakeGitHub) -> None:
    (repo / ".forge" / "config.yml").write_text(CONFIG.replace(">=0.2,<0.3", ">=9"))
    code, env = envelope(repo, "M1")
    assert code == 5
    assert "forge_version" in env["messages"][0]["text"]
    assert fake.list_issues(state="all") == []


def test_gh_not_authenticated_exit_5(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unauthenticated(config: ForgeConfig) -> GitHubClient:
        raise PreconditionError("gh is not authenticated", hint="run `gh auth login`")

    monkeypatch.setattr(_context, "github_factory", unauthenticated)
    code, env = envelope(repo, "M1")
    assert code == 5
    assert env["messages"][0]["hint"] == "run `gh auth login`"


# --------------------------------------------------------------------------- human


def test_human_output_is_plain_text(repo: Path, fake: FakeGitHub) -> None:
    result = invoke(repo, "plan", "apply", "M1", "--dry-run")
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "Plan apply M1 (dry run: nothing written)" in out
    assert "[red]Editor[/red]: save a page" in out  # markup is never interpreted
    assert "M1-T01  new  create" in out and "-> ready" in out
    assert "M1-T02  new  create" in out and "-> blocked" in out
    assert "nothing was written" in result.output


def test_human_output_after_apply_shows_removed_tasks(repo: Path, fake: FakeGitHub) -> None:
    assert invoke(repo, "plan", "apply", "M1").exit_code == 0
    write_plan(repo, [_task(1, title="Renamed", covers=["AC-1", "AC-2"])])
    result = invoke(repo, "plan", "apply", "M1")
    assert result.exit_code == 0, result.output
    assert "M1-T01  #1  update (title, metadata)" in result.stdout
    assert "Removed from the plan:" in result.stdout
    assert "M1-T02  #2  close as not planned" in result.stdout


def test_human_validation_failure(repo: Path) -> None:
    write_plan(repo, [_task(1, size="L")])
    result = invoke(repo, "plan", "apply", "M1")
    assert result.exit_code == 1
    assert "nothing was written" in result.output


def test_proposal_adoption_through_the_cli(repo: Path, fake: FakeGitHub) -> None:
    number = propose(ProposeContext(github=fake, login="bob"), "Cache", "Idea.\n").data["issue"][
        "number"
    ]
    write_plan(repo, [*PLAN, _task(3, zones=["api"], covers=[], proposal=number)])
    result = invoke(repo, "plan", "apply", "M1")
    assert result.exit_code == 0, result.output
    assert f"M1-T03  #{number}  adopt" in result.stdout
    assert fake.get_issue(number).title == "[M1-T03] Task 3"
    write_plan(repo, [*PLAN, _task(3, zones=["api"], covers=[], proposal=99)])
    code, env = envelope(repo, "M1")
    assert code == 1  # the task already has its issue: #99 cannot be adopted
    assert "proposal #99" in env["messages"][0]["text"]
