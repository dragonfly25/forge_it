"""Tests for :mod:`forge.core.config_check` (ADR 0018) against real temporary repositories."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forge.core.config_check import UNZONED_SAMPLE_SIZE, check_project
from forge.core.git import Git
from forge.core.init import LOGIN_PLACEHOLDER, REPO_PLACEHOLDER

if TYPE_CHECKING:
    from conftest import GitWorld

CONFIG = """\
forge_version: ">=0.1,<0.2"
project:
  name: shop
github:
  repo: acme/shop
"""

TEAM = """\
members:
  - github: alice
    roles: [dev, lead]
  - github: bob
    roles: [dev]
"""

ZONES = """\
zones:
  app: {paths: ["src/**"]}
  docs: {paths: ["docs/**", "README.md"]}
"""

FILES = {
    "README.md": "# shop\n",
    "src/main.py": "print()\n",
    "docs/guide.md": "guide\n",
    ".github/workflows/ci.yml": "on: push\n",
    ".claude/skills/x/SKILL.md": "x\n",
    ".agents/skills/x/SKILL.md": "x\n",
    ".codex/config.toml": "x\n",
    "AGENTS.md": "a\n",
    "CLAUDE.md": "c\n",
}


@pytest.fixture
def repo(git_world: GitWorld) -> Git:
    git = git_world.clone("alice")
    write(git, ".forge/config.yml", CONFIG)
    write(git, ".forge/team.yml", TEAM)
    write(git, ".forge/zones.yml", ZONES)
    for path, text in FILES.items():
        write(git, path, text)
    git.add()
    return git


def write(git: Git, rel: str, text: str) -> None:
    target = git.cwd / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def check(git: Git, version: str = "0.1.3") -> dict[str, object]:
    return check_project(git.cwd, git, version=version).to_data()


def codes(data: dict[str, object]) -> list[str]:
    warnings = data["warnings"]
    assert isinstance(warnings, list)
    return [w["code"] for w in warnings]


def test_clean_project(repo: Git) -> None:
    result = check_project(repo.cwd, repo, version="0.1.3")
    assert result.ok
    data = result.to_data()
    assert data == {
        "ok": True,
        "files": {".forge/config.yml": "ok", ".forge/team.yml": "ok", ".forge/zones.yml": "ok"},
        "errors": [],
        "warnings": [],
        "leads": ["alice"],
        "tracked_files": len(FILES) + 3,
        "unzoned": {"count": 0, "sample": []},
        "empty_zones": [],
        "version": {"cli": "0.1.3", "required": "<0.2,>=0.1", "ok": True},
        "counts": {"errors": 0, "warnings": 0},
    }


@pytest.mark.parametrize(
    "rel", [".forge/config.yml", ".forge/team.yml", ".forge/zones.yml"], ids=lambda r: r
)
def test_missing_file_is_an_error(repo: Git, rel: str) -> None:
    (repo.cwd / rel).unlink()
    data = check(repo)
    assert data["ok"] is False
    assert data["files"][rel] == "error"  # type: ignore[index]
    assert data["errors"] == [
        {
            "file": rel,
            "key": None,
            "problem": "file not found",
            "fix": "run `forge init` to create it",
        }
    ]
    others = {k: v for k, v in data["files"].items() if k != rel}  # type: ignore[attr-defined]
    assert set(others.values()) == {"ok"}


def test_invalid_yaml_names_file(repo: Git) -> None:
    write(repo, ".forge/zones.yml", "zones: [unclosed\n")
    data = check(repo)
    [error] = data["errors"]  # type: ignore[misc]
    assert error["file"] == ".forge/zones.yml"
    assert error["problem"].startswith("invalid YAML")
    assert error["fix"]
    assert data["unzoned"] is None and data["empty_zones"] is None


def test_invalid_key_names_key_and_fix(repo: Git) -> None:
    write(repo, ".forge/config.yml", CONFIG.replace("name: shop", "name: shop\n  colour: red"))
    data = check(repo)
    [error] = data["errors"]  # type: ignore[misc]
    assert error["file"] == ".forge/config.yml"
    assert error["key"] == "project.colour"
    assert error["fix"]
    assert data["version"] == {"cli": "0.1.3", "required": None, "ok": None}


def test_repo_placeholder_is_an_error(repo: Git) -> None:
    write(repo, ".forge/config.yml", CONFIG.replace("acme/shop", REPO_PLACEHOLDER))
    data = check(repo)
    [error] = data["errors"]  # type: ignore[misc]
    assert (error["file"], error["key"]) == (".forge/config.yml", "github.repo")
    assert "placeholder" in error["problem"]


def test_login_placeholder_is_an_error(repo: Git) -> None:
    write(repo, ".forge/team.yml", TEAM.replace("github: bob", f'github: "{LOGIN_PLACEHOLDER}"'))
    data = check(repo)
    [error] = data["errors"]  # type: ignore[misc]
    assert error["file"] == ".forge/team.yml"
    assert error["key"] == "members[1].github"
    assert data["leads"] is None


def test_broken_team_still_reports_zones_and_config(repo: Git) -> None:
    write(repo, ".forge/team.yml", "members: 3\n")
    write(repo, "lib/util.py", "x\n")
    repo.add()
    data = check(repo, version="9.0.0")
    assert data["files"] == {
        ".forge/config.yml": "ok",
        ".forge/team.yml": "error",
        ".forge/zones.yml": "ok",
    }
    assert data["unzoned"] == {"count": 1, "sample": ["lib/util.py"]}
    assert codes(data) == ["version_mismatch", "unzoned_files"]
    assert data["leads"] is None


def test_all_three_broken(repo: Git) -> None:
    for rel in (".forge/config.yml", ".forge/team.yml", ".forge/zones.yml"):
        write(repo, rel, "")
    data = check(repo)
    assert [e["file"] for e in data["errors"]] == [  # type: ignore[attr-defined]
        ".forge/config.yml",
        ".forge/team.yml",
        ".forge/zones.yml",
    ]
    assert data["warnings"] == []


def test_no_lead_warning(repo: Git) -> None:
    write(repo, ".forge/team.yml", TEAM.replace("[dev, lead]", "[dev]"))
    data = check(repo)
    assert data["ok"] is True
    assert codes(data) == ["no_lead"]
    assert data["leads"] == []
    [warning] = data["warnings"]  # type: ignore[misc]
    assert warning["file"] == ".forge/team.yml" and "lead" in warning["fix"]


def test_unzoned_files_ignore_forge_files_and_untracked(repo: Git) -> None:
    write(repo, "lib/b.py", "x\n")
    write(repo, "lib/a.py", "x\n")
    write(repo, "Makefile", "x\n")
    write(repo, ".forge/prompts/x.md", "x\n")
    write(repo, "notes/untracked.txt", "x\n")
    repo.add(["lib", "Makefile", ".forge/prompts/x.md"])
    data = check(repo)
    assert data["unzoned"] == {"count": 3, "sample": ["Makefile", "lib/a.py", "lib/b.py"]}
    assert codes(data) == ["unzoned_files"]
    [warning] = data["warnings"]  # type: ignore[misc]
    assert warning["message"].startswith("3 tracked file(s) match no zone")


def test_unzoned_sample_is_capped(repo: Git) -> None:
    for i in range(UNZONED_SAMPLE_SIZE + 5):
        write(repo, f"lib/f{i:02d}.py", "x\n")
    repo.add(["lib"])
    data = check(repo)
    unzoned = data["unzoned"]
    assert isinstance(unzoned, dict)
    assert unzoned["count"] == UNZONED_SAMPLE_SIZE + 5
    assert unzoned["sample"] == [f"lib/f{i:02d}.py" for i in range(UNZONED_SAMPLE_SIZE)]


def test_empty_zone_warning(repo: Git) -> None:
    write(repo, ".forge/zones.yml", ZONES + '  web: {paths: ["scr/web/**"]}\n')
    data = check(repo)
    assert data["empty_zones"] == ["web"]
    assert codes(data) == ["empty_zone"]
    [warning] = data["warnings"]  # type: ignore[misc]
    assert warning["key"] == "zones.web.paths"
    assert "scr/web/**" in warning["message"]


def test_zone_covering_forge_files_is_not_empty(repo: Git) -> None:
    write(repo, ".forge/zones.yml", ZONES + '  ci: {paths: [".github/**"]}\n')
    assert check(repo)["empty_zones"] == []


def test_version_mismatch_is_a_warning(repo: Git) -> None:
    data = check(repo, version="0.2.0")
    assert data["ok"] is True
    assert codes(data) == ["version_mismatch"]
    assert data["version"] == {"cli": "0.2.0", "required": "<0.2,>=0.1", "ok": False}


def test_invalid_cli_version_counts_as_mismatch(repo: Git) -> None:
    assert codes(check(repo, version="not-a-version")) == ["version_mismatch"]


def test_works_before_first_commit(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    git = Git(root)
    git.run("init", "--quiet")
    write(git, ".forge/config.yml", CONFIG)
    write(git, ".forge/team.yml", TEAM)
    write(git, ".forge/zones.yml", ZONES)
    data = check(git)
    assert data["ok"] is True
    assert data["tracked_files"] == 0
    assert data["empty_zones"] == ["app", "docs"]
