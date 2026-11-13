"""End-to-end tests for `forge sync` and `forge escalate` through the CLI (ADR 0011).

Real bare remote and clones with a shared ``FakeGitHub`` (``support.sync_world``). The project
files (config, zones, team) are committed on ``main`` before any claim; the profile lives under
a temporary ``XDG_CONFIG_HOME``; the clock and the GitHub factory are patched.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.commands import _claims, _context
from forge.core.config import ForgeConfig
from forge.core.escalation import ESCALATION_MARKER
from forge.core.git import Git
from forge.core.github import GitHubClient
from forge.core.sync import read_sync
from forge.core.tasks import BLOCKED_CONFLICT
from support.scenarios import T0, Clock
from support.sync_world import SyncWorld, status

if TYPE_CHECKING:
    from conftest import GitWorld

runner = CliRunner()
ENVELOPE_KEYS = {"ok", "code", "data", "messages"}

CONFIG = """\
forge_version: "{version}"
project:
  name: shop
  default_branch: main
github:
  repo: acme/shop
lease_hours: 24
checks:
  ok: "exit 0"
"""

ZONES_YML = """\
zones:
  api:
    paths: ["src/api/**"]
  web:
    paths: ["src/web/**"]
  contracts:
    paths: ["contracts/**"]
    contract: true
"""

TEAM_YML = """\
members:
  - github: carol
    roles: [lead]
  - github: alice
    roles: [dev]
"""


def profile_yaml(login: str) -> str:
    return (
        f"github: {login}\n"
        "agents:\n"
        "  - id: claude\n"
        "    tool: claude-code\n"
        "    providers: [anthropic]\n"
        "    tiers: {strong: opus, standard: sonnet, light: haiku}\n"
        "default_agent: claude\n"
    )


class Env:
    """A :class:`SyncWorld` with the Forge project files on ``main`` and CLI plumbing."""

    def __init__(self, sw: SyncWorld, xdg: Path) -> None:
        self.sw = sw
        self.xdg = xdg
        self.user = "alice"

    def repo(self, user: str = "alice") -> Path:
        return self.sw.world.git(user).cwd

    def git(self, user: str = "alice") -> Git:
        return self.sw.world.git(user)

    def profile(self, login: str) -> None:
        path = self.xdg / "forge" / "profile.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(profile_yaml(login))

    def invoke(
        self, *args: str, user: str = "alice", profile: bool = True
    ) -> tuple[int, dict[str, Any]]:
        """Run ``forge --json --repo <user's clone> <args>``; return (exit code, envelope)."""
        self.user = user
        if profile:
            self.profile(user)
        result = runner.invoke(app, ["--json", "--repo", str(self.repo(user)), *args])
        envelope = json.loads(result.stdout)
        assert set(envelope) == ENVELOPE_KEYS, envelope
        assert envelope["ok"] is (envelope["code"] == 0)
        assert isinstance(envelope["data"], dict)
        for message in envelope["messages"]:
            assert set(message) == {"level", "text", "hint"}
        assert result.exit_code == envelope["code"], result.output
        return result.exit_code, envelope

    def human(self, *args: str, user: str = "alice") -> Any:
        self.user = user
        self.profile(user)
        return runner.invoke(app, ["--repo", str(self.repo(user)), *args])

    def claim(self, task_id: str = "M1-T01", zones: tuple[str, ...] = ("api",)) -> int:
        issue = self.sw.task(task_id, zones)
        self.sw.claim("alice", issue)
        return issue


@pytest.fixture
def env(git_world: GitWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    sw = SyncWorld(git_world)
    sw.upstream(
        {
            ".forge/config.yml": CONFIG.format(version=">=0.1,<0.2"),
            ".forge/zones.yml": ZONES_YML,
            ".forge/team.yml": TEAM_YML,
        },
        message="chore: forge project",
        link=False,
    )
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    environment = Env(sw, xdg)

    def factory(config: ForgeConfig) -> GitHubClient:
        assert config.github.repo == "acme/shop"
        return sw.github(environment.user)  # type: ignore[return-value]

    monkeypatch.setattr(_context, "github_factory", factory)
    monkeypatch.setattr(_claims, "clock", Clock(T0 + timedelta(hours=1)))
    return environment


def head(git: Git) -> str:
    sha = git.rev_parse("HEAD")
    assert sha is not None
    return sha


def conflict(env: Env, path: str = "src/api/a.py", subject: str = "feat: upstream api") -> int:
    """Alice's task and an upstream PR by bob change the same file."""
    issue = env.claim()
    env.sw.commit("alice", path, "alice\n", "feat: alice api")
    env.sw.upstream({path: "bob\n"}, author="bob", message=subject)
    return issue


# --------------------------------------------------------------------------- sync


def test_clean_sync_pushes_with_json_envelope(env: Env) -> None:
    issue = env.claim()
    env.sw.commit("alice", "src/api/a.py", "alice\n")
    env.sw.upstream({"src/web/w.js": "bob\n"}, author="bob")

    code, out = env.invoke("sync")

    assert code == 0
    data = out["data"]
    assert data["outcome"] == "pushed" and data["issue"] == issue and data["rebased"] is True
    assert data["classification"]["primary"] == "none"
    assert data["checks"][0]["name"] == "ok" and data["checks"][0]["exit_code"] == 0
    assert env.sw.remote_tip(issue) == head(env.git())
    assert read_sync(env.repo()) is None


def test_conflict_stops_with_hint_then_continue_pushes(env: Env) -> None:
    issue = conflict(env)

    result = env.human("sync")

    assert result.exit_code == 2, result.output
    stdout = result.stdout
    assert "Sync stopped on conflicts" in stdout
    assert "Classification: textual" in stdout
    assert "PR #2 by bob" in stdout
    assert "src/api/a.py  zone api  open" in stdout
    assert "Next: resolve the files" in stdout and "forge sync --continue" in stdout
    assert read_sync(env.repo()) is not None

    code, out = env.invoke("sync", "--continue")
    assert code == 2 and out["data"]["unresolved"] == ["src/api/a.py"]

    (env.repo() / "src/api/a.py").write_text("alice and bob\n")
    env.git().add(["src/api/a.py"])
    code, out = env.invoke("sync", "--continue")

    assert code == 0, out["messages"]
    assert out["data"]["outcome"] == "pushed"
    assert out["data"]["conflicts"][0]["resolution"] == "manual"
    assert env.sw.remote_tip(issue) == head(env.git())
    assert env.git().current_branch() == f"task/{issue}"


def test_abort_restores_the_pre_sync_state(env: Env) -> None:
    conflict(env)
    before = head(env.git())
    assert env.invoke("sync")[0] == 2

    code, out = env.invoke("sync", "--abort")

    assert code == 0
    assert out["data"]["outcome"] == "aborted"
    assert head(env.git()) == before
    assert not env.git().rebase_in_progress()
    assert read_sync(env.repo()) is None
    assert status(env.git()) == ""
    human = env.human("sync", "--abort")
    assert human.exit_code == 5  # nothing to abort any more


def test_abort_needs_no_profile_and_no_github(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    conflict(env)
    before = head(env.git())
    assert env.invoke("sync")[0] == 2
    (env.xdg / "forge" / "profile.yml").unlink()

    def no_github(config: ForgeConfig) -> GitHubClient:
        raise AssertionError("--abort must not touch GitHub")

    monkeypatch.setattr(_context, "github_factory", no_github)

    code, out = env.invoke("sync", "--abort", profile=False)

    assert code == 0, out["messages"]
    assert out["data"]["outcome"] == "aborted" and out["data"]["saved_ref"] is None
    assert head(env.git()) == before and not env.git().rebase_in_progress()
    assert read_sync(env.repo()) is None
    assert [m["text"] for m in out["messages"] if m["level"] == "warning"] == [
        "the rebase abort discarded the uncommitted changes of the stopped rebase "
        "(conflict resolutions in progress) in: src/api/a.py"
    ]


def test_abort_works_with_conflict_markers_in_the_config(env: Env) -> None:
    """Mid-rebase, .forge/config.yml and zones.yml hold conflict markers: --abort still
    restores the pre-sync state and only warns that the config was not loaded."""
    issue = env.claim()
    for name in ("config.yml", "zones.yml"):
        path = env.repo() / ".forge" / name
        path.write_text(path.read_text() + "# alice\n")
    env.git().add([".forge/config.yml", ".forge/zones.yml"])
    env.git().commit("chore: alice config")
    before = head(env.git())
    env.sw.upstream(
        {
            ".forge/config.yml": CONFIG.format(version=">=0.1,<0.2") + "# bob\n",
            ".forge/zones.yml": ZONES_YML + "# bob\n",
        },
        author="bob",
    )
    code, out = env.invoke("sync", "--force-resolve")
    assert code == 2, out["messages"]
    assert sorted(out["data"]["unresolved"]) == [".forge/config.yml", ".forge/zones.yml"]
    assert "<<<<<<<" in (env.repo() / ".forge/config.yml").read_text()

    code, out = env.invoke("sync", "--abort", profile=False)

    assert code == 0, out["messages"]
    assert out["data"]["outcome"] == "aborted" and out["data"]["issue"] == issue
    warnings = [m["text"] for m in out["messages"] if m["level"] == "warning"]
    assert len(warnings) == 2
    assert warnings[0].endswith("in progress) in: .forge/config.yml, .forge/zones.yml")
    assert warnings[1].startswith("config not loaded")
    assert head(env.git()) == before and not env.git().rebase_in_progress()
    assert "<<<<<<<" not in (env.repo() / ".forge/config.yml").read_text()
    assert status(env.git()) == "" and read_sync(env.repo()) is None


def test_out_of_zone_conflict_escalates(env: Env) -> None:
    issue = env.claim()
    env.sw.commit("alice", "src/web/w.js", "alice\n", "feat: touch web")
    env.sw.upstream({"src/web/w.js": "bob\n"}, author="bob")
    before = head(env.git())

    code, out = env.invoke("sync")

    assert code == 4
    data = out["data"]
    assert data["outcome"] == "escalated"
    assert data["classification"]["primary"] == "out_of_zone"
    assert data["escalation"]["mentioned"] == ["bob"]
    assert BLOCKED_CONFLICT in env.sw.labels(issue)
    assert head(env.git()) == before

    human = env.human("sync")
    assert human.exit_code == 4
    assert "Sync escalated" in human.stdout
    assert "outside the task's zones" in human.stdout
    assert "wait for @bob" in human.stdout


def test_force_resolve_leaves_out_of_zone_conflicts_to_the_agent(env: Env) -> None:
    issue = env.claim()
    env.sw.commit("alice", "src/web/w.js", "alice\n", "feat: touch web")
    env.sw.upstream({"src/web/w.js": "bob\n"}, author="bob")

    code, out = env.invoke("sync", "--force-resolve")

    assert code == 2
    assert out["data"]["outcome"] == "conflicts"
    assert out["data"]["unresolved"] == ["src/web/w.js"]
    assert BLOCKED_CONFLICT not in env.sw.labels(issue)


def test_sync_without_profile_is_code_5(env: Env) -> None:
    env.claim()

    code, out = env.invoke("sync", profile=False)

    assert code == 5
    assert "no Forge profile" in out["messages"][0]["text"]


def test_sync_refuses_a_cli_outside_forge_version(env: Env) -> None:
    issue = env.claim()
    tip = env.sw.remote_tip(issue)
    (env.repo() / ".forge/config.yml").write_text(CONFIG.format(version=">=9"))

    code, out = env.invoke("sync")

    assert code == 5
    assert "does not satisfy forge_version" in out["messages"][0]["text"]
    assert out["messages"][0]["hint"]
    # nothing happened: no wip commit, no sync state, nothing pushed
    assert "config.yml" in status(env.git())
    assert read_sync(env.repo()) is None
    assert env.sw.remote_tip(issue) == tip


@pytest.mark.parametrize(
    "flags",
    [("--continue", "--abort"), ("--continue", "--force-resolve"), ("--abort", "--force-resolve")],
)
def test_exclusive_flags_are_a_usage_error(env: Env, flags: tuple[str, ...]) -> None:
    env.claim()

    code, out = env.invoke("sync", *flags)

    assert code == 1
    assert "cannot be used" in out["messages"][0]["text"]


def test_markup_in_paths_and_subjects_is_printed_literally(env: Env) -> None:
    path = "src/api/[bold]x[/bold].py"
    conflict(env, path=path, subject="feat: [red]boom[/red] :smile:")

    result = env.human("sync")

    assert result.exit_code == 2, result.output
    assert path in result.stdout
    assert "[red]boom[/red] :smile:" in result.stdout


# --------------------------------------------------------------------------- escalate


def test_escalate_labels_comments_and_mentions(env: Env) -> None:
    issue = conflict(env)
    assert env.invoke("sync")[0] == 2

    code, out = env.invoke("escalate", "--reason", "bob's [bold]API[/bold] change breaks mine")

    assert code == 0
    data = out["data"]
    assert data["issue"] == issue and data["label_added"] is True
    assert data["mentioned"] == ["bob", "carol"]
    assert data["upstream_source"] == "sync"
    assert BLOCKED_CONFLICT in env.sw.labels(issue)
    bodies = [c.body for c in env.sw.fake.list_comments(issue)]
    assert any(b.startswith(ESCALATION_MARKER) and "@bob @carol" in b for b in bodies)

    human = env.human("escalate", "--reason", "bob's [bold]API[/bold] change breaks mine")
    assert human.exit_code == 0
    assert "[bold]API[/bold]" in human.stdout
    assert "same escalation already posted" in human.stdout


def test_escalate_without_current_task_is_code_5(env: Env) -> None:
    code, out = env.invoke("escalate", "--reason", "help", user="bob")

    assert code == 5
    assert "no current task" in out["messages"][0]["text"]


def test_escalate_needs_no_profile_and_only_warns_on_version(env: Env) -> None:
    issue = env.claim()
    (env.repo() / ".forge/config.yml").write_text(CONFIG.format(version=">=9"))
    (env.repo() / ".forge/team.yml").write_text("members: nope\n")

    code, out = env.invoke("escalate", "--reason", "blocked", profile=False)

    assert code == 0
    assert BLOCKED_CONFLICT in env.sw.labels(issue)
    warnings = [m["text"] for m in out["messages"] if m["level"] == "warning"]
    assert any("does not satisfy forge_version" in w for w in warnings)
    assert any("team.yml not used" in w for w in warnings)
    assert out["data"]["mentioned"] == []
