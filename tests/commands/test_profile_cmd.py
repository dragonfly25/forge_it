"""End-to-end tests for `forge profile show|set|edit` (spec §6.5.3, ADR 0014).

`set` and `edit` re-render the machine-level subagents in a real clone that carries the
template `.forge/agents` and `.gitignore`. `edit` uses a fake editor (a Python script named
in ``$EDITOR``). The profile lives under a temporary ``XDG_CONFIG_HOME``. No network.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.core.git import Git
from forge.core.profile import load_profile
from forge.core.tiers import Effort, Tier

if TYPE_CHECKING:
    from conftest import GitWorld

runner = CliRunner()

CONFIG = """\
forge_version: ">=0.1,<0.2"
project: {name: shop, default_branch: main}
github: {repo: acme/shop}
"""

PROFILE = """\
github: alice
agents:
  - id: claude-alice
    tool: claude-code
    providers: [anthropic]
    tiers: {strong: opus, standard: sonnet, light: haiku}
    effort: {low: low, medium: medium, high: high}
  - id: local
    tool: ollama
    providers: [local]
    tiers: {standard: llama3.1:8b}
default_agent: claude-alice
"""


@pytest.fixture
def profile_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    path = home / "forge" / "profile.yml"
    path.parent.mkdir(parents=True)
    path.write_text(PROFILE)
    return path


def make_project(git_world: GitWorld) -> Path:
    root = git_world.clone("alice").cwd
    template = resources.files("forge") / "templates" / "project"
    (root / ".forge" / "agents").mkdir(parents=True)
    (root / ".forge" / "config.yml").write_text(CONFIG)
    for name in ("explorer", "reviewer", "test-writer"):
        source = template / ".forge" / "agents" / f"{name}.md"
        (root / ".forge" / "agents" / f"{name}.md").write_text(source.read_text("utf-8"))
    (root / ".gitignore").write_text((template / ".gitignore").read_text("utf-8"))
    return root


def envelope(output: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(output)
    return data


def set_editor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, new_text: str) -> None:
    """Point $EDITOR at a script that overwrites the edited file with ``new_text``."""
    content = tmp_path / "new-profile.yml"
    content.write_text(new_text)
    script = tmp_path / "fake_editor.py"
    script.write_text(f"import shutil, sys\nshutil.copyfile({str(content)!r}, sys.argv[-1])\n")
    monkeypatch.setenv("EDITOR", f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    monkeypatch.delenv("VISUAL", raising=False)


# --------------------------------------------------------------------------- show


def test_show_json_resolved_map(profile_file: Path, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--json", "profile", "show"])
    assert result.exit_code == 0, result.output
    data = envelope(result.stdout)["data"]
    assert data["path"] == str(profile_file)
    assert data["profile"]["default_agent"] == "claude-alice"
    claude = data["resolved"]["claude-alice"]
    assert claude["tiers"]["strong"] == {"model": "opus", "via": "strong"}
    assert claude["effort"] == {"low": "low", "medium": "medium", "high": "high"}
    local = data["resolved"]["local"]
    assert local["tiers"]["light"] == {"model": "llama3.1:8b", "via": "standard"}
    assert local["tiers"]["strong"] == {"model": None, "via": None}
    assert local["effort"] == {"low": None, "medium": None, "high": None}


def test_show_text(profile_file: Path, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["profile", "show"])
    assert result.exit_code == 0
    assert "Resolved models:" in result.stdout
    assert "light    llama3.1:8b  (via standard)" in result.stdout
    assert "strong   -  (cannot serve)" in result.stdout


def test_show_without_profile_exits_5(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    result = runner.invoke(app, ["profile", "show"])
    assert result.exit_code == 5
    assert "forge setup" in result.stderr


# --------------------------------------------------------------------------- set


def test_set_changes_model_and_rerenders(git_world: GitWorld, profile_file: Path) -> None:
    root = make_project(git_world)
    reviewer = root / ".claude" / "agents" / "forge-reviewer.md"
    first = runner.invoke(
        app, ["--json", "--repo", str(root), "profile", "set", "claude-alice.light=haiku"]
    )
    assert first.exit_code == 0, first.output
    assert "model: opus" in reviewer.read_text()

    result = runner.invoke(
        app, ["--json", "--repo", str(root), "profile", "set", "claude-alice.strong=fable"]
    )
    assert result.exit_code == 0, result.output
    data = envelope(result.stdout)["data"]
    assert load_profile(profile_file).agents[0].tiers[Tier.STRONG] == "fable"
    assert data["rendered"] == [".claude/agents/forge-reviewer.md"]
    assert "model: fable" in reviewer.read_text()
    git = Git(root)
    for path in (root / ".claude" / "agents").iterdir():
        rel = path.relative_to(root).as_posix()
        assert git.run("check-ignore", "-q", rel, check=False).returncode == 0, rel
    assert not any(p.startswith(".claude/") for p in git.dirty_files())


def test_set_rerenders_every_task_worktree(
    git_world: GitWorld, profile_file: Path, tmp_path: Path
) -> None:
    root = make_project(git_world)
    git = Git(root)
    git.add([".forge", ".gitignore"])
    git.commit("chore: forge files")
    task_wt = tmp_path / "wt" / "task-7"
    other_wt = tmp_path / "wt" / "feature"
    git.run("worktree", "add", "--quiet", "-b", "task/7", str(task_wt), "HEAD")
    git.run("worktree", "add", "--quiet", "-b", "feature", str(other_wt), "HEAD")

    result = runner.invoke(
        app, ["--json", "--repo", str(root), "profile", "set", "claude-alice.strong=fable"]
    )

    assert result.exit_code == 0, result.output
    rendered = envelope(result.stdout)["data"]["rendered"]
    main_file = root / ".claude" / "agents" / "forge-reviewer.md"
    task_file = task_wt / ".claude" / "agents" / "forge-reviewer.md"
    assert "model: fable" in main_file.read_text()
    assert "model: fable" in task_file.read_text()
    assert ".claude/agents/forge-reviewer.md" in rendered
    assert str(task_file) in rendered
    assert not (other_wt / ".claude").exists()
    assert not Git(task_wt).is_dirty(include_untracked=True)


def test_set_task_worktree_failure_is_a_warning(
    git_world: GitWorld, profile_file: Path, tmp_path: Path
) -> None:
    root = make_project(git_world)
    git = Git(root)
    git.add([".forge"])  # the committed tree has no .gitignore patterns
    git.commit("chore: forge files")
    task_wt = tmp_path / "wt" / "task-8"
    git.run("worktree", "add", "--quiet", "-b", "task/8", str(task_wt), "HEAD")

    result = runner.invoke(
        app, ["--json", "--repo", str(root), "profile", "set", "claude-alice.strong=fable"]
    )

    assert result.exit_code == 0, result.output
    env = envelope(result.stdout)
    assert "model: fable" in (root / ".claude" / "agents" / "forge-reviewer.md").read_text()
    assert not (task_wt / ".claude").exists()
    assert any(m["level"] == "warning" and str(task_wt) in m["text"] for m in env["messages"])


def test_set_effort_and_default_agent(profile_file: Path, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["profile", "set", "claude-alice.effort.high=max"])
    assert result.exit_code == 0, result.output
    assert "not inside a Forge project" in result.stderr
    result = runner.invoke(app, ["profile", "set", "default_agent=local"])
    assert result.exit_code == 0
    profile = load_profile(profile_file)
    assert profile.default_agent == "local"
    assert profile.agents[0].effort[Effort.HIGH] == "max"


def test_set_invalid_keeps_profile(profile_file: Path, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["profile", "set", "nobody.strong=opus"])
    assert result.exit_code == 1
    assert "unknown agent 'nobody'" in result.stderr
    result = runner.invoke(app, ["profile", "set", "claude-alice.huge=opus"])
    assert result.exit_code == 1
    assert profile_file.read_text() == PROFILE


def test_set_refuses_when_not_gitignored(git_world: GitWorld, profile_file: Path) -> None:
    root = make_project(git_world)
    (root / ".gitignore").write_text(".forge/local/\n")
    result = runner.invoke(
        app, ["--repo", str(root), "profile", "set", "claude-alice.strong=fable"]
    )
    assert result.exit_code == 5
    assert "would not ignore" in result.stderr
    assert not (root / ".claude" / "agents").exists()


# --------------------------------------------------------------------------- edit


def test_edit_valid_replaces_and_rerenders(
    git_world: GitWorld, profile_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(git_world)
    edited = PROFILE.replace("strong: opus", "strong: fable") + "# edited by hand\n"
    set_editor(monkeypatch, tmp_path, edited)
    result = runner.invoke(app, ["--json", "--repo", str(root), "profile", "edit"])
    assert result.exit_code == 0, result.output
    data = envelope(result.stdout)["data"]
    assert data["changed"] is True
    assert profile_file.read_text() == edited
    assert "model: fable" in (root / ".claude" / "agents" / "forge-reviewer.md").read_text()


def test_edit_prefers_visual_over_editor(
    git_world: GitWorld, profile_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(git_world)
    edited = PROFILE.replace("strong: opus", "strong: fable")
    set_editor(monkeypatch, tmp_path, edited)
    monkeypatch.setenv("VISUAL", os.environ["EDITOR"])
    monkeypatch.setenv("EDITOR", "false")  # would fail: must not be used
    result = runner.invoke(app, ["--json", "--repo", str(root), "profile", "edit"])
    assert result.exit_code == 0, result.output
    assert profile_file.read_text() == edited


def test_edit_invalid_keeps_old_profile(
    profile_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    set_editor(
        monkeypatch,
        tmp_path,
        PROFILE.replace("default_agent: claude-alice", "default_agent: ghost"),
    )
    result = runner.invoke(app, ["profile", "edit"])
    assert result.exit_code == 1
    assert "invalid" in result.stderr and "ghost" in result.stderr
    assert profile_file.read_text() == PROFILE


def test_edit_without_changes(
    profile_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    set_editor(monkeypatch, tmp_path, PROFILE)
    result = runner.invoke(app, ["--json", "profile", "edit"])
    assert result.exit_code == 0
    env = envelope(result.stdout)
    assert env["data"]["changed"] is False
    assert any(m["text"] == "no changes" for m in env["messages"])
