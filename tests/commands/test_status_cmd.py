"""End-to-end tests for `forge status` through the CLI (ADR 0010).

The seeded scenario of ``tests/support/scenarios.py`` (``Seeder``, ``seed_full``) runs against
a real bare remote and clones plus a ``FakeGitHub``; here it is driven through the CLI from
alice's clone, with a config and zones file in her working tree, a profile under a temporary
``XDG_CONFIG_HOME`` and the clock injected through ``forge.commands._claims``.
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
from forge.commands.status import _delegation, ago
from forge.core.config import ForgeConfig
from forge.core.errors import PreconditionError
from forge.core.git import Git
from forge.core.github import GitHubClient
from forge.core.github_fake import FakeGitHub
from forge.core.report import SECTIONS
from forge.core.tasks import BLOCKED_CONFLICT
from support.scenarios import T0, NoChecksGitHub, Seeder, Ticker, World, seed_full

if TYPE_CHECKING:
    from conftest import GitWorld

runner = CliRunner()
ENVELOPE_KEYS = {"ok", "code", "data", "messages"}

CONFIG = """\
forge_version: ">=0.1,<0.2"
project:
  name: shop
  default_branch: main
github:
  repo: acme/shop
lease_hours: 24
"""

ZONES_YML = """\
zones:
  api: {paths: ["src/api/**"]}
  api-v2: {paths: ["src/api/v2/**"]}
  web: {paths: ["src/web/**"]}
  db: {paths: ["src/db/**"]}
  docs: {paths: ["docs/**"]}
"""

PROFILE = """\
github: alice
agents:
  - id: claude
    tool: claude-code
    providers: [anthropic]
    tiers: {strong: opus, standard: sonnet, light: haiku}
default_agent: claude
"""


def install(
    seeder: Seeder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, profile: str | None
) -> Path:
    """Point the CLI at alice's clone, the fake GitHub, the fixed clock and a profile."""
    repo = seeder.world.git("alice").cwd
    (repo / ".forge").mkdir(exist_ok=True)
    (repo / ".forge" / "config.yml").write_text(CONFIG)
    (repo / ".forge" / "zones.yml").write_text(ZONES_YML)
    xdg = tmp_path / "xdg"
    (xdg / "forge").mkdir(parents=True)
    if profile is not None:
        (xdg / "forge" / "profile.yml").write_text(profile)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("FORGE_AGENT", raising=False)
    seeder.fake.fake_set_user("alice")

    def factory(config: ForgeConfig) -> GitHubClient:
        assert config.github.repo == "acme/shop"
        return seeder.fake

    monkeypatch.setattr(_context, "github_factory", factory)
    monkeypatch.setattr(_claims, "clock", lambda: T0)
    return repo


@pytest.fixture
def seeder(git_world: GitWorld) -> Seeder:
    return Seeder(World(git_world, fake=FakeGitHub(clock=Ticker())))


@pytest.fixture
def full(seeder: Seeder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The full ADR 0010 scenario, seen from alice's clone with a profile."""
    seed_full(seeder)
    return install(seeder, monkeypatch, tmp_path, profile=PROFILE)


def status_json(repo: Path, *options: str, globals_: tuple[str, ...] = ()) -> tuple[int, Any]:
    result = runner.invoke(app, ["--json", "--repo", str(repo), *globals_, "status", *options])
    envelope = json.loads(result.stdout)
    assert set(envelope) == ENVELOPE_KEYS
    return result.exit_code, envelope


def warnings_of(envelope: dict[str, Any]) -> list[str]:
    return [m["text"] for m in envelope["messages"] if m["level"] == "warning"]


# --------------------------------------------------------------------------- JSON


def test_json_envelope_has_every_section(full: Path) -> None:
    code, env = status_json(full)
    assert code == 0 and env["ok"] is True and env["code"] == 0
    data = env["data"]
    assert data["sections"] == list(SECTIONS)
    assert all(data[name] is not None for name in SECTIONS)
    assert data["me"]["login"] == "alice" and data["me"]["agent"] == "claude"
    assert data["me"]["current"]["id"] == "M1-T03"
    assert data["milestone"]["title"] == "M1"
    assert data["generated_at"] == "2026-11-02T09:00:00Z"
    # warnings are both in data and in the envelope messages, once each
    assert warnings_of(env) == data["warnings"] and data["warnings"]


def test_me_keeps_me_and_queue(full: Path) -> None:
    code, env = status_json(full, "--me")
    assert code == 0
    data = env["data"]
    assert data["sections"] == ["me", "queue"]
    assert data["scope"] == {"me_only": True, "milestone": None}
    for name in ("team", "blocked", "milestone", "health"):
        assert data[name] is None
    assert data["me"] is not None and data["queue"] is not None


def test_milestone_option_scopes_the_report(full: Path) -> None:
    code, env = status_json(full, "--milestone", "M2")
    assert code == 0
    data = env["data"]
    assert data["scope"]["milestone"] == "M2"
    assert data["milestone"]["title"] == "M2" and data["milestone"]["source"] == "option"
    assert data["team"]["in_progress"] == []
    assert data["blocked"]["by_dependencies"] == []
    assert all(pick["milestone"] == "M2" for pick in data["queue"]["next"])


def test_without_profile_still_exits_0_with_a_warning(
    seeder: Seeder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seed_full(seeder)
    repo = install(seeder, monkeypatch, tmp_path, profile=None)
    code, env = status_json(repo)
    assert code == 0 and env["ok"] is True
    data = env["data"]
    assert data["me"]["login_source"] == "gh" and data["me"]["agent"] is None
    assert data["queue"]["next"] == [] and data["health"]["profile"] is False
    assert any("forge setup" in w for w in warnings_of(env))


def test_agent_without_profile_is_ignored_with_a_warning(
    seeder: Seeder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seeder.task("M1-T01", ["api"])
    repo = install(seeder, monkeypatch, tmp_path, profile=None)
    code, env = status_json(repo, globals_=("--agent", "codex"))
    assert code == 0
    assert any("'codex' ignored" in w for w in warnings_of(env))


def test_invalid_profile_is_a_warning(
    seeder: Seeder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seeder.task("M1-T01", ["api"])
    repo = install(seeder, monkeypatch, tmp_path, profile="github: [not, a, login]\n")
    code, env = status_json(repo)
    assert code == 0
    assert env["data"]["me"]["login_source"] == "gh"
    assert any(w.startswith("profile not used:") for w in warnings_of(env))


def test_unknown_agent_exits_5(full: Path) -> None:
    code, env = status_json(full, globals_=("--agent", "nope"))
    assert code == 5 and env["code"] == 5 and env["ok"] is False
    (message,) = env["messages"]
    assert "unknown agent 'nope'" in message["text"] and "claude" in message["hint"]


def test_unknown_agent_from_env_exits_5(full: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_AGENT", "nope")
    code, _ = status_json(full)
    assert code == 5


def test_missing_config_exits_5(git_world: GitWorld) -> None:
    clone = git_world.clone("carol")
    code, env = status_json(clone.cwd)
    assert code == 5 and env["code"] == 5
    (message,) = env["messages"]
    assert "config.yml" in message["text"] and message["hint"] == "run `forge init` to create it"


def test_invalid_config_exits_1(full: Path) -> None:
    (full / ".forge" / "config.yml").write_text("project: [\n")
    code, env = status_json(full)
    assert code == 1 and env["code"] == 1


def test_unreadable_ci_still_exits_0(
    git_world: GitWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seeder = Seeder(World(git_world, fake=NoChecksGitHub(clock=Ticker())))
    number = seeder.task("M1-T01", ["api"])
    seeder.claim("alice", number, "M1-T01", T0 - timedelta(hours=1))
    pr = seeder.as_user("alice").create_pull("M1-T01", "x", f"task/{number}", "main")
    seeder.checkout_task("alice", number)
    repo = install(seeder, monkeypatch, tmp_path, profile=PROFILE)
    code, env = status_json(repo)
    assert code == 0 and env["ok"] is True
    assert env["data"]["me"]["current"]["ci"] is None
    assert any(f"pull request #{pr.number}" in w and "HTTP 403" in w for w in warnings_of(env))
    text = runner.invoke(app, ["--repo", str(repo), "status"])
    assert text.exit_code == 0 and "CI unknown" in text.stdout


class UnauthenticatedGitHub(FakeGitHub):
    """A GitHub whose ``gh`` is logged out: every call raises what ``GhCliClient`` raises."""

    def _logged_out(self) -> PreconditionError:
        return PreconditionError(
            "GitHub CLI is not authenticated: To get started with GitHub CLI, please run:  "
            "gh auth login",
            hint="gh auth login",
        )

    def current_user(self) -> str:
        raise self._logged_out()

    def list_issues(self, *args: Any, **kwargs: Any) -> Any:
        raise self._logged_out()


@pytest.mark.parametrize("profile", [None, PROFILE], ids=["no-profile", "profile"])
def test_gh_not_authenticated_exits_5(
    git_world: GitWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: str | None
) -> None:
    seeder = Seeder(World(git_world, fake=FakeGitHub(clock=Ticker())))
    seeder.task("M1-T01", ["api"])
    repo = install(seeder, monkeypatch, tmp_path, profile=profile)
    logged_out = UnauthenticatedGitHub()
    monkeypatch.setattr(_context, "github_factory", lambda config: logged_out)
    code, env = status_json(repo)
    assert code == 5 and env["code"] == 5 and env["ok"] is False
    (message,) = env["messages"]
    assert "not authenticated" in message["text"] and message["hint"] == "gh auth login"


def test_fetch_failure_exits_1(full: Path) -> None:
    Git(full).run("remote", "set-url", "origin", str(full.parent / "no-such-remote.git"))
    code, env = status_json(full)
    assert code == 1 and env["code"] == 1 and env["ok"] is False
    assert env["data"] is None or env["data"] == {}
    assert any(m["level"] == "error" for m in env["messages"])


def test_zones_problem_is_a_warning(full: Path) -> None:
    (full / ".forge" / "zones.yml").write_text("zones: [\n")
    code, env = status_json(full)
    assert code == 0
    assert any(w.startswith("zones not shown:") for w in warnings_of(env))
    assert env["data"]["me"]["current"]["drift"] is None


# --------------------------------------------------------------------------- human output


def test_human_output_shows_every_section(full: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    result = runner.invoke(app, ["--repo", str(full), "status"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    headings = ["Me ", "Team", "Queue", "Blocked", "Milestone M1", "Health"]
    positions = [out.index(h) for h in headings]
    assert positions == sorted(positions)
    for value in ("M1-T03", "M1-T04", "M1-T05", "M1-T06", "M1-T07", "M1-T08"):
        assert value in out
    assert "STALE" in out
    assert "failure (lint, test)" in out
    assert "3 behind, 1 unpushed · pending sync" in out
    assert "2 upstream commit(s) touch your zones" in out
    assert "1/10 done" in out and "uncovered: AC-2" in out
    assert "Dropped: M1-T02" in out and "Proposed: M1-T12" in out
    assert "30m ago" in out and "30h ago" in out
    assert "! unknown labels: status:wip (#9)" in out
    assert "✓ forge 0.1.0" in out and "satisfies <0.2,>=0.1" in out
    assert "\x1b[" not in out  # no colour when stdout is not a terminal
    assert max(len(line) for line in out.splitlines() if not line.lstrip().startswith("in /")) <= 80
    assert "warning:" in result.stderr


HOSTILE = ("Support [/api] routes :warning:", "[bold red]x", "[link=http://x]y[/link]")


def test_human_output_never_interprets_markup(
    seeder: Seeder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = seeder.fake
    mine = seeder.task("M1-T01", ["api"])
    bobs = seeder.task("M1-T02", ["db"])
    fake.create_label("status:[/api]", "cccccc")
    conflict = seeder.task("M1-T03", ["web"], labels=[BLOCKED_CONFLICT, "status:[/api]"])
    for number, title in zip((mine, bobs, conflict), HOSTILE, strict=True):
        fake.update_issue(number, title=title)
    seeder.as_user("lead").create_issue(f"{HOSTILE[2]} idea", "x", labels=["status:proposed"])
    seeder.claim("alice", mine, "M1-T01", T0 - timedelta(hours=5))
    seeder.claim("bob", bobs, "M1-T02", T0 - timedelta(hours=2))
    seeder.main_commit("src/api/x.py", "x\n", T0 - timedelta(hours=1), "feat: [bold red]x [/api]")
    seeder.checkout_task("alice", mine)
    repo = install(seeder, monkeypatch, tmp_path, profile=PROFILE)

    result = runner.invoke(app, ["--repo", str(repo), "status"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    for text in (*HOSTILE, "feat: [bold red]x [/api]", "status:[/api] (#", f"#{mine}"):
        assert text in out, text
    assert f"{HOSTILE[2]} idea" in out


def test_human_output_me_only(full: Path) -> None:
    result = runner.invoke(app, ["--repo", str(full), "status", "--me"])
    assert result.exit_code == 0
    assert "Me " in result.stdout and "Queue" in result.stdout
    assert "Team" not in result.stdout and "Health" not in result.stdout


def test_empty_sections_print_none(
    seeder: Seeder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = install(seeder, monkeypatch, tmp_path, profile=PROFILE)
    result = runner.invoke(app, ["--repo", str(repo), "status"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "current task: none" in out and "in progress: none" in out
    assert "Blocked\n  none" in out and "Milestone\n  none" in out


def test_ago() -> None:
    assert ago("2026-11-02T08:59:30Z", T0) == "just now"
    assert ago("2026-11-02T06:00:00Z", T0) == "3h ago"
    assert ago("2026-10-29T09:00:00Z", T0) == "4d ago"
    assert ago("2026-11-03T08:30:00Z", T0) == "in 23h"
    assert ago(None, T0) == "-"


def test_human_output_shows_the_delegation_of_the_current_task(full: Path) -> None:
    result = runner.invoke(app, ["--repo", str(full), "status", "--me"])
    assert result.exit_code == 0, result.output
    assert "Delegate off · .forge/agents/implementer.md missing" in result.stdout


@pytest.mark.parametrize(
    ("delegation", "line"),
    [
        (
            {"mode": "auto", "delegate": True, "reason": "size M", "model": "sonnet"},
            "auto → forge-implementer (sonnet) · size M",
        ),
        (
            {"mode": "on", "delegate": True, "reason": "requested by profile", "model": None},
            "on → forge-implementer · requested by profile",
        ),
        (
            {"mode": "auto", "delegate": False, "reason": "1 zone, size S", "model": None},
            "auto → main session · 1 zone, size S",
        ),
        (
            {"mode": "off", "delegate": False, "reason": "requested by profile", "model": None},
            "off · requested by profile",
        ),
    ],
)
def test_delegation_line(delegation: dict[str, Any], line: str) -> None:
    assert _delegation({"subagent": "forge-implementer", **delegation}) == line
