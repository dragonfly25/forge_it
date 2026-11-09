"""End-to-end check of delegation (ADR 0017) on a freshly initialised project.

`forge init` in a temporary repository, a profile with a claude-code agent, then
`forge adapters generate --local` renders `forge-implementer` and `forge task show --json` on a
size-M task reports ``delegate: true`` with the rendered model. GitHub is a ``FakeGitHub`` and
the login lookup is replaced: no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from forge.cli import app
from forge.commands import _context
from forge.commands import init as init_cmd
from forge.core.config import ForgeConfig
from forge.core.git import Git
from forge.core.github import GitHubClient
from forge.core.github_fake import FakeGitHub
from forge.core.refresh import ensure_labels
from forge.core.tasks import Size, TaskMetadata, TaskType, upsert_block
from forge.core.tiers import Tier

runner = CliRunner()

PROFILE = """\
github: alice
agents:
  - id: claude-alice
    tool: claude-code
    providers: [anthropic]
    tiers: {strong: opus, standard: sonnet, light: haiku}
    effort: {low: low, medium: medium, high: high}
default_agent: claude-alice
"""


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeGitHub:
    fake = FakeGitHub(repo="acme/shop")
    ensure_labels(fake)
    fake.fake_set_user("alice")

    def factory(config: ForgeConfig) -> GitHubClient:
        assert config.github.repo == "acme/shop"
        return fake

    monkeypatch.setattr(_context, "github_factory", factory)
    monkeypatch.setattr(init_cmd, "login_lookup", lambda: "alice")
    xdg = tmp_path / "xdg"
    (xdg / "forge").mkdir(parents=True)
    (xdg / "forge" / "profile.yml").write_text(PROFILE)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return fake


def forge(repo: Path, *args: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["--json", "--repo", str(repo), *args])
    return result.exit_code, json.loads(result.stdout)


def test_init_render_local_and_task_show_delegates(tmp_path: Path, fake: FakeGitHub) -> None:
    root = tmp_path / "shop"
    root.mkdir()
    git = Git(root)
    git.run("init", "--quiet", "--initial-branch=main")
    # CI runners have no global git identity: set one in the repo so the commit below works.
    git.config_set("user.name", "alice")
    git.config_set("user.email", "alice@example.test")
    git.run("remote", "add", "origin", "https://github.com/acme/shop.git")

    code, out = forge(root, "init")
    assert code == 0, out
    assert (root / ".forge/agents/implementer.md").is_file()

    code, out = forge(root, "adapters", "generate", "--local")
    assert code == 0, out
    rendered = (root / ".claude/agents/forge-implementer.md").read_text()
    front = yaml.safe_load(rendered.split("---\n", 2)[1])
    assert (front["name"], front["model"], front["effort"]) == (
        "forge-implementer",
        "sonnet",
        "medium",
    )

    # Publish the initialised project to a local bare "origin" so commands can fetch offline.
    bare = tmp_path / "origin.git"
    Git(tmp_path).run("init", "--quiet", "--bare", str(bare))
    git.run("add", "-A")
    git.run("commit", "--quiet", "-m", "chore: forge init")
    git.run("remote", "set-url", "origin", str(bare))
    git.run("push", "--quiet", "origin", "main")
    assert not git.is_dirty(include_untracked=True)  # rendered local files are gitignored

    meta = TaskMetadata(
        id="M1-T01",
        spec="specs/m1/x.md",
        covers=("AC-1",),
        depends_on=(),
        zones=("api",),
        size=Size.M,
        tier=Tier.STANDARD,
        type=TaskType.IMPL,
        done_when=("tests pass",),
    )
    issue = fake.create_issue(
        "[M1-T01] task",
        upsert_block("Implement M1-T01.\n", meta),
        labels=["forge:task", "status:ready"],
    )

    code, out = forge(root, "task", "show", str(issue.number))
    assert code == 0, out
    delegation = out["data"]["delegation"]
    assert delegation["delegate"] is True
    assert (delegation["subagent"], delegation["model"]) == ("forge-implementer", "sonnet")
    assert delegation["reason"] == "size M"


def test_invalid_delegation_value_is_a_usage_error(tmp_path: Path, fake: FakeGitHub) -> None:
    for args in (("task", "show"), ("next",)):
        result = runner.invoke(app, ["--repo", str(tmp_path), *args, "--delegation", "sometimes"])
        assert result.exit_code == 1, result.output
        assert "sometimes" in result.output
