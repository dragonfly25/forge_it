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
from forge.core.errors import PreconditionError
from forge.core.git import Git
from forge.core.github import GitHubClient, GitHubError
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


class FailingGitHub(FakeGitHub):
    """FakeGitHub whose label writes fail for the issues in ``failing``."""

    def __init__(self) -> None:
        super().__init__(repo="acme/shop")
        self.failing: set[int] = set()

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        if number in self.failing:
            raise GitHubError(f"HTTP 502 on issue {number}", status=502)
        super().add_labels(number, labels)


def test_refresh_partial_apply_failure(
    git_world: GitWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FailingGitHub()
    fake.create_label("forge:task", "5319e7")
    for task_id in ("M1-T01", "M1-T02", "M1-T03"):
        fake.create_issue(f"[{task_id}]", body(task_id), labels=["forge:task"])
    fake.failing = {2}
    clone = git_world.clone("alice")
    (clone.cwd / ".forge").mkdir()
    (clone.cwd / ".forge" / "config.yml").write_text(CONFIG)
    monkeypatch.setattr(_context, "github_factory", lambda config: fake)

    result = runner.invoke(app, ["--json", "--repo", str(clone.cwd), "refresh"])
    assert result.exit_code == 1, result.output
    env = json.loads(result.stdout)
    assert env["ok"] is False and env["code"] == 1
    (failure,) = env["data"]["warnings"]
    assert failure.startswith("issue #2 (M1-T02): label update failed") and "re-run" in failure
    assert env["data"]["counts"]["failed"] == 1
    assert statuses(fake) == {1: ["status:ready"], 2: [], 3: ["status:ready"]}
    infos = [m["text"] for m in env["messages"] if m["level"] == "info"]
    assert infos == ["refresh: 2 tasks updated, 1 failed"]

    fake.failing = set()
    env = run_json(clone.cwd)
    assert [c["issue"] for c in env["data"]["changes"]] == [2]
    assert env["data"]["counts"]["failed"] == 0
    assert run_json(clone.cwd)["data"]["changes"] == []


def test_refresh_shallow_clone_exits_5(
    git_world: GitWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = Git(git_world.base / "seed")
    git_world.commit_file(seed, "more.txt", "more\n", "feat: more history")
    git_world.push(seed, "main")
    path = git_world.base / "ci"
    Git(git_world.base).run(
        "clone", "--quiet", "--depth", "1", git_world.remote.as_uri(), str(path)
    )
    (path / ".forge").mkdir()
    (path / ".forge" / "config.yml").write_text(CONFIG)
    monkeypatch.setattr(_context, "github_factory", lambda config: FakeGitHub(repo="acme/shop"))

    result = runner.invoke(app, ["--json", "--repo", str(path), "refresh"])
    assert result.exit_code == 5, result.output
    (message,) = json.loads(result.stdout)["messages"]
    assert "shallow" in message["text"] and "fetch-depth: 0" in message["hint"]


def test_github_client_is_built_lazily_once(
    git_world: GitWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = git_world.clone("erin")
    (clone.cwd / ".forge").mkdir()
    (clone.cwd / ".forge" / "config.yml").write_text(CONFIG)
    calls: list[str] = []

    def broken(config: ForgeConfig) -> GitHubClient:
        calls.append(config.github.repo)
        raise PreconditionError("bad github.repo", hint="fix it")

    monkeypatch.setattr(_context, "github_factory", broken)
    ctx = _context.load_context(GlobalOptions(repo=clone.cwd))
    assert calls == []  # git-only commands never trigger the factory
    assert ctx.git.root().resolve() == clone.cwd.resolve()
    with pytest.raises(PreconditionError):
        _ = ctx.github
    assert calls == ["acme/shop"]

    fake = FakeGitHub(repo="acme/shop")
    monkeypatch.setattr(_context, "github_factory", lambda config: fake)
    ctx = _context.load_context(GlobalOptions(repo=clone.cwd))
    assert ctx.github is fake and ctx.github is fake


# --------------------------------------------------------------------------- --radar

ZONES_YML = """\
zones:
  api: {paths: ["src/api/**"]}
  web: {paths: ["src/web/**"]}
"""


def run_radar_json(repo: Path) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["--json", "--repo", str(repo), "refresh", "--radar"])
    envelope: dict[str, Any] = json.loads(result.stdout)
    return result.exit_code, envelope


def test_refresh_radar_json_envelope(setup: tuple[Path, FakeGitHub], git_world: GitWorld) -> None:
    repo, fake = setup
    (repo / ".forge" / "zones.yml").write_text(ZONES_YML)
    lead = git_world.clone("lead")
    sha = git_world.commit_file(lead, "src/api/a.py", "x\n", "feat: api change")
    git_world.push(lead, "main")

    code, env = run_radar_json(repo)

    assert code == 0, env
    assert env["ok"] is True and env["code"] == 0
    assert set(env["data"]) == {"changes", "warnings", "counts", "radar"}
    radar = env["data"]["radar"]
    (task,) = radar["tasks"]
    assert task["issue"] == 4 and task["id"] == "M1-T04" and task["in_progress"] is True
    assert task["comment"] == "created" and task["applied"] is True
    assert [c["sha"] for c in task["drift"]["commits"]] == [sha]
    assert task["lease"] is None
    assert radar["counts"]["created"] == 1 and radar["counts"]["drift"] == 1
    texts = [m["text"] for m in env["messages"]]
    assert any(t.startswith("radar:") for t in texts)
    (comment,) = fake.list_comments(4)
    assert comment.body.startswith("<!-- forge:radar -->") and sha[:7] in comment.body

    code, env = run_radar_json(repo)
    assert code == 0
    assert env["data"]["radar"]["tasks"][0]["comment"] == "unchanged"
    assert len(fake.list_comments(4)) == 1


def test_refresh_radar_without_zones_warns(setup: tuple[Path, FakeGitHub]) -> None:
    repo, fake = setup

    code, env = run_radar_json(repo)

    assert code == 0, env
    assert env["data"]["radar"]["tasks"] == []
    texts = [m["text"] for m in env["messages"] if m["level"] == "warning"]
    assert any("zones could not be loaded" in t for t in texts)
    assert fake.list_comments(4) == []


def test_refresh_without_radar_has_no_radar_data(setup: tuple[Path, FakeGitHub]) -> None:
    repo, _ = setup
    assert "radar" not in run_json(repo)["data"]


def test_refresh_radar_shallow_clone_exits_5(
    git_world: GitWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = Git(git_world.base / "seed")
    git_world.commit_file(seed, "more.txt", "more\n", "feat: more history")
    git_world.push(seed, "main")
    path = git_world.base / "ci"
    Git(git_world.base).run(
        "clone", "--quiet", "--depth", "1", git_world.remote.as_uri(), str(path)
    )
    (path / ".forge").mkdir()
    (path / ".forge" / "config.yml").write_text(CONFIG)
    (path / ".forge" / "zones.yml").write_text(ZONES_YML)
    monkeypatch.setattr(_context, "github_factory", lambda config: FakeGitHub(repo="acme/shop"))

    code, env = run_radar_json(path)

    assert code == 5, env
    assert "shallow" in env["messages"][0]["text"]
