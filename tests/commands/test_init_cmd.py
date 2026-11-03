"""End-to-end tests for `forge init` through the CLI (ADR 0014). No network: the GitHub login
lookup is replaced."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.commands import init as init_cmd
from forge.core.git import Git

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init_cmd, "login_lookup", lambda: "alice")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    root.mkdir()
    git = Git(root)
    git.run("init", "--quiet", "--initial-branch=main")
    git.run("remote", "add", "origin", "https://github.com/acme/shop.git")
    return root


def invoke(repo: Path, *args: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    result = runner.invoke(app, ["--json", "--repo", str(repo), "init", *args])
    return result.exit_code, json.loads(result.stdout)


def test_init_json_envelope(repo: Path) -> None:
    code, env = invoke(repo)
    assert code == 0 and env["ok"] is True
    data = env["data"]
    assert data["adapters"] == ["generic", "claude", "codex"]
    assert data["values"]["repo"] == "acme/shop"
    assert data["values"]["github_login"] == "alice"
    assert ".forge/config.yml" in data["created"]
    assert "AGENTS.md" in data["created"]
    assert data["next_steps"]
    assert env["messages"] == []


def test_init_adapters_option(repo: Path) -> None:
    code, env = invoke(repo, "--adapters", "codex")
    assert code == 0
    assert env["data"]["adapters"] == ["generic", "codex"]
    assert not (repo / "CLAUDE.md").exists()
    assert (repo / ".agents/skills/forge-next/SKILL.md").is_file()


def test_init_unknown_adapter_exits_1(repo: Path) -> None:
    code, env = invoke(repo, "--adapters", "claude,cursor")
    assert code == 1
    assert "cursor" in env["messages"][0]["text"]
    assert not (repo / ".forge").exists()


def test_init_conflict_exits_5_then_force(repo: Path) -> None:
    (repo / ".env.example").write_text("MINE=1\n")
    code, env = invoke(repo)
    assert code == 5 and env["ok"] is False
    assert ".env.example" in env["messages"][0]["text"]
    assert not (repo / ".forge").exists()
    code, env = invoke(repo, "--force")
    assert code == 0
    assert ".env.example" in env["data"]["overwritten"]


def test_init_warns_on_undetected_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init_cmd, "login_lookup", lambda: None)
    root = tmp_path / "bare"
    root.mkdir()
    Git(root).run("init", "--quiet", "--initial-branch=main")
    code, env = invoke(root)
    assert code == 0
    texts = " ".join(m["text"] for m in env["messages"])
    assert "repository" in texts and "login" in texts
    assert "<ORG>/<REPO>" in (root / ".forge/config.yml").read_text()


def test_init_text_output(repo: Path) -> None:
    result = runner.invoke(app, ["--repo", str(repo), "init"])
    assert result.exit_code == 0
    assert "Created" in result.stdout and "Next steps" in result.stdout


def test_init_outside_git_exits_5(tmp_path: Path) -> None:
    code, _ = invoke(tmp_path)
    assert code == 5


def test_parse_adapters() -> None:
    assert init_cmd.parse_adapters(None) == ("generic", "claude", "codex")
    assert init_cmd.parse_adapters(" claude , ") == ("claude",)


def test_gh_login_uses_a_repository_less_client(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[object, ...]] = []

    class RecordingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created.append((*args, *kwargs.values()))

        def current_user(self) -> str:
            return "alice"

    monkeypatch.setattr(init_cmd, "GhCliClient", RecordingClient)
    assert init_cmd._gh_login() == "alice"
    assert created == [()]  # no placeholder repository


def test_gh_login_is_none_when_gh_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from forge.core.github import GhCliClient

    def failing(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        return 1, "", "gh: To get started with GitHub CLI, please run:  gh auth login"

    monkeypatch.setattr(init_cmd, "GhCliClient", lambda: GhCliClient(runner=failing))
    assert init_cmd._gh_login() is None
