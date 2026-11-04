"""End-to-end tests for `forge review` through the CLI (ADR 0012).

Bob reviews from his clone (config, zones and spec files in his working tree, a profile under
a temporary ``XDG_CONFIG_HOME``) against the shared ``FakeGitHub`` of ``ReviewWorld``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.commands import _context
from forge.commands.review import _ac_summary
from forge.core.config import ForgeConfig
from forge.core.github import GitHubClient
from support.review_world import ReviewWorld

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

STRONG_REVIEWS = CONFIG + "models:\n  min_tier_for:\n    reviewer: strong\n"

ZONES_YML = """\
zones:
  api: {paths: ["src/api/**"]}
  web: {paths: ["src/web/**"]}
"""

PROFILE = """\
github: bob
agents:
  - id: claude
    tool: claude-code
    providers: [anthropic]
    tiers: {strong: opus, standard: sonnet, light: haiku}
  - id: mid
    tool: codex
    providers: [openai]
    tiers: {standard: gpt-x}
default_agent: claude
"""


@pytest.fixture
def world(git_world: GitWorld) -> ReviewWorld:
    return ReviewWorld(git_world)


def install(
    world: ReviewWorld,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    config: str = CONFIG,
    profile: str | None = PROFILE,
) -> Path:
    repo = world.bob.cwd
    (repo / ".forge").mkdir(exist_ok=True)
    (repo / ".forge" / "config.yml").write_text(config)
    (repo / ".forge" / "zones.yml").write_text(ZONES_YML)
    xdg = tmp_path / "xdg"
    (xdg / "forge").mkdir(parents=True, exist_ok=True)
    if profile is not None:
        (xdg / "forge" / "profile.yml").write_text(profile)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("FORGE_AGENT", raising=False)
    world.fake.fake_set_user("bob")

    def factory(cfg: ForgeConfig) -> GitHubClient:
        return world.fake

    monkeypatch.setattr(_context, "github_factory", factory)
    return repo


def invoke(repo: Path, *args: str, json_: bool = True) -> tuple[int, Any]:
    argv = (["--json"] if json_ else []) + ["--repo", str(repo), *args]
    result = runner.invoke(app, argv)
    if not json_:
        return result.exit_code, result.stdout
    envelope = json.loads(result.stdout)
    assert set(envelope) == {"ok", "code", "data", "messages"}
    return result.exit_code, envelope


def warnings_of(env: dict[str, Any]) -> list[str]:
    return [m["text"] for m in env["messages"] if m["level"] == "warning"]


def body_file(tmp_path: Path, text: str = "Looks good.\n") -> str:
    path = tmp_path / "review.md"
    path.write_text(text)
    return str(path)


def test_context_json(world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n", "src/web/b.js": "b\n"})
    repo = install(world, monkeypatch, tmp_path)
    code, env = invoke(repo, "review", str(pr.number))
    assert code == 0 and env["ok"] is True
    data = env["data"]
    assert data["task"]["issue"] == n
    assert data["files"] == ["src/api/a.py", "src/web/b.js"]
    assert data["zones"]["out_of_zone"] == [{"path": "src/web/b.js", "zones": ["web"]}]
    assert data["reviewer"]["agent"] == "claude"


def test_context_human_view(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/[a].py": "a\n"})
    repo = install(world, monkeypatch, tmp_path)
    code, out = invoke(repo, "review", str(pr.number), json_=False)
    assert code == 0
    assert f"PR #{pr.number}" in out and "M1-T01" in out
    assert "src/api/[a].py" in out  # never read as rich markup
    assert "Diff:" in out
    assert "  AC-1  Users can log in.\n" in out
    assert "  AC-2  Users can log out.\n" in out
    assert out.count("AC-1") == 1


@pytest.mark.parametrize(
    "text",
    [
        "- **AC-1** The API contract defines save.",
        "- AC-1: The API contract defines save.",
        "1. `AC-1` The API contract defines save.",
        "### AC-1 The API contract defines save.",
        "**AC-1:** The API contract defines save.\nsecond line",
        "* AC-1 \u2014 The API contract defines save.",
    ],
)
def test_ac_summary_strips_marker_and_id(text: str) -> None:
    assert _ac_summary("AC-1", text) == "The API contract defines save."


def test_ac_summary_keeps_other_text() -> None:
    assert _ac_summary("AC-1", "- AC-10 is different") == "AC-10 is different"
    assert _ac_summary("AC-1", "") == ""


def test_submit_requires_body_file(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = install(world, monkeypatch, tmp_path)
    code, env = invoke(repo, "review", "1", "--submit", "approve")
    assert code == 1 and "--body-file is required" in env["messages"][0]["text"]


def test_body_file_without_submit_is_usage_error(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = install(world, monkeypatch, tmp_path)
    code, _ = invoke(repo, "review", "1", "--body-file", body_file(tmp_path))
    assert code == 1


def test_submit_unknown_value(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = install(world, monkeypatch, tmp_path)
    code, _ = invoke(repo, "review", "1", "--submit", "merge", "--body-file", body_file(tmp_path))
    assert code == 1


def test_submit_approve(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    repo = install(world, monkeypatch, tmp_path)
    code, env = invoke(
        repo, "review", str(pr.number), "--submit", "approve", "--body-file", body_file(tmp_path)
    )
    assert code == 0 and env["data"]["event"] == "APPROVE"
    review = world.fake.list_reviews(pr.number)[0]
    assert review.state == "APPROVED"
    assert "Reviewed with Forge — agent claude (anthropic), model opus" in review.body


def test_approve_own_pr_exits_5(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"}, author="bob")
    repo = install(world, monkeypatch, tmp_path)
    code, _ = invoke(
        repo, "review", str(pr.number), "--submit", "approve", "--body-file", body_file(tmp_path)
    )
    assert code == 5
    code, _ = invoke(
        repo, "review", str(pr.number), "--submit", "comment", "--body-file", body_file(tmp_path)
    )
    assert code == 0


def test_min_tier_refusal_and_force(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    repo = install(world, monkeypatch, tmp_path, config=STRONG_REVIEWS)
    submit = ["--submit", "comment", "--body-file", body_file(tmp_path)]
    code, env = invoke(repo, "--agent", "mid", "review", str(pr.number))
    assert code == 5 and "min_tier_for.reviewer" in env["messages"][0]["text"]
    code, _ = invoke(repo, "--agent", "mid", "review", str(pr.number), *submit)
    assert code == 5
    assert world.fake.list_reviews(pr.number) == []
    code, env = invoke(repo, "--agent", "mid", "review", str(pr.number), "--force")
    assert code == 0 and any("--force" in w for w in warnings_of(env))
    code, env = invoke(repo, "--agent", "mid", "review", str(pr.number), *submit, "--force")
    assert code == 0 and env["data"]["forced"] is True
    assert "tier forced" in world.fake.list_reviews(pr.number)[0].body


def test_no_profile(world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    repo = install(world, monkeypatch, tmp_path, config=STRONG_REVIEWS, profile=None)
    code, env = invoke(repo, "review", str(pr.number))
    assert code == 0 and env["data"]["reviewer"] is None
    assert any("no Forge profile" in w for w in warnings_of(env))
    code, _ = invoke(
        repo, "review", str(pr.number), "--submit", "comment", "--body-file", body_file(tmp_path)
    )
    assert code == 5
    assert world.fake.list_reviews(pr.number) == []


def test_human_view_without_task_says_unzoned_check_only(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pr = world.open_pr("feature/y", {"src/web/b.js": "b\n"})
    repo = install(world, monkeypatch, tmp_path)
    code, out = invoke(repo, "review", str(pr.number), json_=False)
    assert code == 0
    assert "Task: none linked" in out
    assert "Zones: no task: unzoned check only; no unzoned files" in out
    assert "all files in the task's zones" not in out

    other = world.open_pr("feature/z", {"tools/x.sh": "x\n"})
    code, out = invoke(repo, "review", str(other.number), json_=False)
    assert code == 0
    assert "Zones (no task: unzoned check only):" in out and "unzoned: tools/x.sh" in out
    assert "all files in the task's zones" not in out


def test_human_view_with_task_and_clean_zones(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    repo = install(world, monkeypatch, tmp_path)
    code, out = invoke(repo, "review", str(pr.number), json_=False)
    assert code == 0 and "Zones: all files in the task's zones" in out


def test_submit_with_another_gh_login_exits_5(
    world: ReviewWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    repo = install(world, monkeypatch, tmp_path)
    world.fake.fake_set_user("carol")  # gh is authenticated as someone else than the profile
    code, env = invoke(
        repo, "review", str(pr.number), "--submit", "approve", "--body-file", body_file(tmp_path)
    )
    assert code == 5 and "differs from the profile" in env["messages"][0]["text"]
    assert world.fake.list_reviews(pr.number) == []
