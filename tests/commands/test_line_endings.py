"""A Windows checkout with ``core.autocrlf=true`` (ADR 0021).

Git for Windows checks text files out with CRLF by default. Forge writes LF, so every check
that compares a file on disk with what Forge would write must treat CRLF as equal, and must
not rewrite the file. ``core.autocrlf`` works on every platform, so this runs everywhere.
"""

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


@pytest.fixture
def crlf_clone(tmp_path: Path) -> Path:
    """A project initialised and committed with LF, then cloned with ``core.autocrlf=true``."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git = Git(origin)
    git.run("init", "--quiet", "--initial-branch=main")
    git.run("config", "user.name", "alice")
    git.run("config", "user.email", "alice@example.test")
    git.run("config", "core.autocrlf", "false")
    init_project(origin, VALUES, adapters=["claude", "codex"])
    git.run("add", "--all")
    git.run("commit", "--quiet", "-m", "chore: forge init")
    clone = tmp_path / "clone"
    Git(tmp_path).run("clone", "--quiet", "-c", "core.autocrlf=true", str(origin), str(clone))
    return clone


def forge(repo: Path, *args: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    result = runner.invoke(app, ["--json", "--repo", str(repo), *args])
    return result.exit_code, json.loads(result.stdout)


def test_checkout_really_has_crlf(crlf_clone: Path) -> None:
    for rel in (".forge/prompts/dev.md", "AGENTS.md", "docs/forge/commands.md"):
        assert b"\r\n" in (crlf_clone / rel).read_bytes(), rel
    assert b"\r\n" in next((crlf_clone / ".claude/skills").rglob("SKILL.md")).read_bytes()


def test_adapters_check_sees_no_drift(crlf_clone: Path) -> None:
    code, env = forge(crlf_clone, "adapters", "generate", "--check")
    assert code == 0, env
    assert env["data"]["drift"] == []


def test_adapters_generate_rewrites_nothing(crlf_clone: Path) -> None:
    code, env = forge(crlf_clone, "adapters", "generate")
    assert code == 0, env
    assert env["data"]["written"] == []
    assert not Git(crlf_clone).run("status", "--porcelain").stdout.strip()


def test_docs_check_sees_no_drift(crlf_clone: Path) -> None:
    code, env = forge(crlf_clone, "docs", "--check")
    assert code == 0, env


def test_init_again_finds_everything_unchanged(crlf_clone: Path) -> None:
    report = init_project(crlf_clone, VALUES, adapters=["claude", "codex"])
    assert report.created == []
    assert report.overwritten == []
    assert report.unchanged
    assert not Git(crlf_clone).run("status", "--porcelain").stdout.strip()
