"""End-to-end tests for `forge adapters generate [--check] [--local]` (ADR 0014)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.core.git import Git
from forge.core.init import InitValues, init_project

runner = CliRunner()

VALUES = InitValues(
    project_name="shop",
    repo="acme/shop",
    default_branch="main",
    forge_version=">=0.1,<0.2",
    forge_install_ref="v0.1.0",
    github_login="alice",
)

PROFILE = """\
github: alice
agents:
  - id: claude-pro-1
    tool: claude-code
    providers: [anthropic]
    tiers: {strong: opus, standard: sonnet, light: haiku}
    effort: {low: low, medium: medium, high: high}
  - id: openai-team
    tool: codex
    providers: [openai]
    tiers: {strong: gpt-6-astra, light: gpt-6-luna}
    effort: {low: low, medium: medium, high: high}
default_agent: claude-pro-1
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "shop"
    root.mkdir()
    Git(root).run("init", "--quiet", "--initial-branch=main")
    init_project(root, VALUES, adapters=["claude", "codex"])
    xdg = tmp_path / "xdg"
    (xdg / "forge").mkdir(parents=True)
    (xdg / "forge/profile.yml").write_text(PROFILE)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return root


def invoke(repo: Path, *args: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    result = runner.invoke(app, ["--json", "--repo", str(repo), "adapters", "generate", *args])
    return result.exit_code, json.loads(result.stdout)


def test_check_clean_after_init(repo: Path) -> None:
    code, env = invoke(repo, "--check")
    assert code == 0
    assert env["data"]["drift"] == []


def test_check_detects_drift_and_generate_fixes_it(repo: Path) -> None:
    (repo / ".claude/skills/forge-next/SKILL.md").unlink()
    prompt = repo / ".forge/prompts/status.md"
    prompt.write_text(prompt.read_text() + "\nOne more line.\n")
    code, env = invoke(repo, "--check")
    assert code == 1 and env["ok"] is False
    assert set(env["data"]["drift"]) == {
        "missing: .claude/skills/forge-next/SKILL.md",
        "different: .claude/skills/forge-status/SKILL.md",
        "different: .agents/skills/forge-status/SKILL.md",
    }
    assert (repo / ".claude/skills/forge-next/SKILL.md").exists() is False  # wrote nothing

    code, env = invoke(repo)
    assert code == 0
    assert ".claude/skills/forge-next/SKILL.md" in env["data"]["written"]
    code, _ = invoke(repo, "--check")
    assert code == 0


def test_removed_prompt_removes_generated_skills(repo: Path) -> None:
    (repo / ".forge/prompts/release.md").unlink()
    code, env = invoke(repo, "--check")
    assert code == 1
    assert "extra: .claude/skills/forge-release/SKILL.md" in env["data"]["drift"]
    code, env = invoke(repo)
    assert code == 0
    assert ".agents/skills/forge-release/SKILL.md" in env["data"]["removed"]
    assert not (repo / ".claude/skills/forge-release").exists()


def test_adapters_config_change_is_drift(repo: Path) -> None:
    config = repo / ".forge/config.yml"
    config.write_text(config.read_text().replace("adapters: [generic, claude, codex]", ""))
    config.write_text(config.read_text() + "adapters: [generic, claude]\n")
    code, env = invoke(repo, "--check")
    assert code == 1
    drift = set(env["data"]["drift"])
    assert "different: AGENTS.md" in drift  # it no longer mentions Codex skills
    assert {d for d in drift if d != "different: AGENTS.md"} == {
        f"extra: .agents/skills/forge-{p.stem}/SKILL.md"
        for p in (repo / ".forge/prompts").glob("*.md")
    }


def test_local_renders_gitignored_subagents(repo: Path) -> None:
    code, env = invoke(repo, "--local", "--check")
    assert code == 1
    assert "missing: .claude/agents/forge-explorer.md" in env["data"]["drift"]

    code, env = invoke(repo, "--local")
    assert code == 0, env
    assert ".claude/agents/forge-reviewer.md" in env["data"]["written"]
    assert ".codex/agents/forge-reviewer.toml" in env["data"]["files"]
    text = (repo / ".claude/agents/forge-reviewer.md").read_text()
    assert "model: opus" in text
    status = Git(repo).run("status", "--porcelain", "--untracked-files=all").stdout
    assert "forge-reviewer" not in status

    code, env = invoke(repo, "--local", "--check")
    assert code == 0 and env["data"]["drift"] == []
    # Project-level check is unaffected by local files.
    assert invoke(repo, "--check")[0] == 0


def test_local_refuses_unignored_files(repo: Path) -> None:
    (repo / ".gitignore").write_text(".forge/local/\n")
    code, env = invoke(repo, "--local")
    assert code == 5
    assert ".claude/agents/forge-*.md" in env["messages"][0]["hint"]


def test_local_without_profile_points_to_setup(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "xdg/forge/profile.yml").unlink()
    code, env = invoke(repo, "--local")
    assert code == 1
    assert "forge setup" in env["messages"][0]["hint"]


def test_text_output(repo: Path) -> None:
    result = runner.invoke(app, ["--repo", str(repo), "adapters", "generate", "--check"])
    assert result.exit_code == 0
    assert "up to date" in result.stdout
