"""End-to-end tests for `forge docs [--check] [--output] [--prompts]` (ADR 0015)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from forge.cli import app
from forge.commands.docs import describe_cli
from forge.core import docs as core_docs
from forge.core.git import Git
from forge.core.init import InitValues, init_project

runner = CliRunner()
FRAMEWORK_ROOT = Path(__file__).resolve().parents[2]

VALUES = InitValues(
    project_name="shop",
    repo="acme/shop",
    default_branch="main",
    forge_version=">=0.1,<0.2",
    forge_install_ref="v0.1.0",
    github_login="alice",
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "shop"
    root.mkdir()
    Git(root).run("init", "--quiet", "--initial-branch=main")
    init_project(root, VALUES, adapters=["claude"])
    # No profile is needed: point the config home at an empty directory.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return root


def invoke(repo: Path, *args: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["--json", "--repo", str(repo), "docs", *args])
    return result.exit_code, json.loads(result.stdout)


def all_commands() -> list[tuple[str, ...]]:
    def walk(cmd: Any, path: tuple[str, ...]) -> list[tuple[str, ...]]:
        out = [path]
        for name, sub in getattr(cmd, "commands", {}).items():
            out += walk(sub, (*path, name))
        return out

    return walk(typer.main.get_command(app), ("forge",))[1:]


def test_init_output_is_up_to_date(repo: Path) -> None:
    code, env = invoke(repo, "--check")
    assert code == 0, env
    assert env["data"]["up_to_date"] is True and env["data"]["diff"] == []


def test_generate_then_check_then_drift(repo: Path) -> None:
    target = repo / "docs/forge/commands.md"
    target.unlink()
    code, env = invoke(repo, "--check")
    assert code == 1
    assert env["data"]["diff"] == ["docs/forge/commands.md is missing"]
    assert not target.exists()

    code, env = invoke(repo)
    assert code == 0 and env["data"]["written"] is True
    assert env["data"]["prompts"] == 11
    code, env = invoke(repo)
    assert code == 0 and env["data"]["written"] is False

    text = target.read_text()
    target.write_text(text.replace("forge next", "forge nxt", 1))
    code, env = invoke(repo, "--check")
    assert code == 1
    assert env["ok"] is False
    assert any(line.startswith("+") and "forge next" in line for line in env["data"]["diff"])
    assert target.read_text() != text  # --check writes nothing


def test_check_fails_when_a_prompt_changes(repo: Path) -> None:
    prompt = repo / ".forge/prompts/status.md"
    prompt.write_text(prompt.read_text().replace("description: ", "description: Changed. ", 1))
    code, env = invoke(repo, "--check")
    assert code == 1
    assert any("Changed." in line for line in env["data"]["diff"])


def test_output_is_independent_of_terminal_width(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    texts = []
    for columns in ("20", "300"):
        monkeypatch.setenv("COLUMNS", columns)
        monkeypatch.setenv("TERM", "xterm-256color")
        code, _ = invoke(repo)
        assert code == 0
        texts.append((repo / "docs/forge/commands.md").read_text())
    assert texts[0] == texts[1]
    assert "\x1b" not in texts[0]


def test_every_command_and_option_is_documented(repo: Path) -> None:
    text = core_docs.render_reference(describe_cli(), [])
    root = typer.main.get_command(app)
    for path in all_commands():
        assert f"### `{' '.join(path)}`" in text, path
        cmd: Any = root
        for name in path[1:]:
            cmd = cmd.commands[name]
        section = text.split(f"### `{' '.join(path)}`", 1)[1].split("\n### ", 1)[0]
        for param in cmd.params:
            if param.param_type_name == "option":
                for opt in param.opts:
                    assert f"`{opt}`" in section, (path, opt)
            else:
                assert f"`{param.human_readable_name.upper()}`" in section, (path, param.name)
    for opt in ("--json", "--agent", "--repo", "--verbose", "--yes", "--version"):
        assert f"`{opt}`" in text


def test_prompt_table(repo: Path) -> None:
    invoke(repo)
    text = (repo / "docs/forge/commands.md").read_text()
    table = text.split("## Prompts", 1)[1]
    for name in ("setup", "plan", "next", "dev", "ship", "release"):
        assert f"| `{name}` |" in table


def test_custom_paths_and_human_output(repo: Path) -> None:
    prompts = repo / "my-prompts"
    prompts.mkdir()
    (prompts / "x.md").write_text(
        "---\nname: x\ndescription: X does things\nargs: ''\nroles: [dev]\n---\n## Goal\n"
    )
    result = runner.invoke(
        app, ["--repo", str(repo), "docs", "--prompts", "my-prompts", "--output", "out/ref.md"]
    )
    assert result.exit_code == 0, result.stdout
    assert "Wrote out/ref.md" in result.stdout
    assert "| `x` | X does things |" in (repo / "out/ref.md").read_text()
    result = runner.invoke(
        app,
        [
            "--repo",
            str(repo),
            "docs",
            "--check",
            "--prompts",
            "my-prompts",
            "--output",
            "out/ref.md",
        ],
    )
    assert result.exit_code == 0 and "is up to date" in result.stdout


def test_missing_prompts_dir_exits_5(repo: Path) -> None:
    code, env = invoke(repo, "--prompts", "nope")
    assert code == 5
    message = env["messages"][0]
    assert str(repo / "nope") in message["text"]


def test_global_options_after_subcommand(repo: Path) -> None:
    result = runner.invoke(app, ["--repo", str(repo), "docs", "--check", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["ok"] is True


def test_framework_reference_is_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same command the framework's CI runs (.github/workflows/ci.yml).
    monkeypatch.chdir(FRAMEWORK_ROOT)
    result = runner.invoke(
        app,
        [
            "docs",
            "--check",
            "--prompts",
            "src/forge/templates/project/.forge/prompts",
            "--output",
            "docs/commands.md",
        ],
        env={**os.environ, "COLUMNS": "40"},
    )
    assert result.exit_code == 0, (
        result.stdout + "\nRun: uv run forge docs --prompts "
        "src/forge/templates/project/.forge/prompts --output docs/commands.md"
    )
    ci = (FRAMEWORK_ROOT / ".github/workflows/ci.yml").read_text()
    assert (
        "forge docs --check --prompts src/forge/templates/project/.forge/prompts "
        "--output docs/commands.md" in ci
    )
