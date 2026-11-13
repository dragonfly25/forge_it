"""End-to-end tests for `forge setup` through the CLI (ADR 0014).

External commands (gh, claude, codex, ollama, setup.commands) go through stub runners
installed with ``monkeypatch.setattr(setup_cmd, "env_factory", ...)``. The project is a real
clone with the template `.forge/agents` and `.gitignore`; the profile lives under a temporary
``XDG_CONFIG_HOME``. No network.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from typer.testing import CliRunner

from forge.cli import app
from forge.commands import setup as setup_cmd
from forge.core.git import Git
from forge.core.profile import load_profile
from forge.core.setup import NOT_FOUND, CommandOutput, MachineEnv

if TYPE_CHECKING:
    from conftest import GitWorld

runner = CliRunner()
NOW = datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC)

CONFIG = """\
forge_version: ">=0.1,<0.2"
project: {name: shop, default_branch: main}
github: {repo: acme/shop}
setup:
  commands: ["make deps"]
"""


def meta(when: datetime) -> str:
    return f"HTTP/2.0 200 OK\nDate: {when.strftime('%a, %d %b %Y %H:%M:%S GMT')}\n\n{{}}"


def outputs(**overrides: CommandOutput) -> dict[str, CommandOutput]:
    base = {
        "gh --version": CommandOutput(0, "gh version 2.60.1 (2026-01-01)\n"),
        "gh auth status": CommandOutput(0, "✓ Logged in to github.com account alice (keyring)"),
        "gh api -i /meta": CommandOutput(0, meta(NOW)),
        "claude --version": CommandOutput(0, "2.1.0 (Claude Code)"),
        "codex --version": CommandOutput(0, "codex-cli 0.80.0"),
    }
    base.update(overrides)
    return base


class Machine:
    """Stub machine: command outputs plus a log of setup commands run."""

    def __init__(self, table: dict[str, CommandOutput], git: tuple[int, int, int] = (2, 45, 0)):
        self.table = table
        self.git = git
        self.shell_log: list[str] = []

    def run(self, argv: Sequence[str]) -> CommandOutput:
        return self.table.get(" ".join(argv), CommandOutput(NOT_FOUND, "", "not found"))

    def shell(self, command: str, cwd: Path) -> int:
        self.shell_log.append(command)
        return 0

    def env(self) -> MachineEnv:
        return MachineEnv(
            run=self.run,
            shell=self.shell,
            git_version=lambda: self.git,
            python_version=lambda: (3, 12, 0),
            cli_version="0.1.0",
            now=lambda: NOW,
        )


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home / "forge" / "profile.yml"


def install(monkeypatch: pytest.MonkeyPatch, machine: Machine) -> None:
    monkeypatch.setattr(setup_cmd, "env_factory", machine.env)


def make_project(git_world: GitWorld, config: str = CONFIG) -> Path:
    """A clone with `.forge/config.yml`, the template agents and the template .gitignore."""
    git = git_world.clone("alice")
    root = git.cwd
    template = resources.files("forge") / "templates" / "project"
    (root / ".forge" / "agents").mkdir(parents=True)
    (root / ".forge" / "config.yml").write_text(config)
    for name in ("explorer", "reviewer", "test-writer"):
        source = template / ".forge" / "agents" / f"{name}.md"
        (root / ".forge" / "agents" / f"{name}.md").write_text(source.read_text("utf-8"))
    (root / ".gitignore").write_text((template / ".gitignore").read_text("utf-8"))
    return root


def invoke(args: list[str], input: str | None = None) -> Any:
    return runner.invoke(app, args, input=input)


def envelope(output: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(output)
    return data


# --------------------------------------------------------------------------- --check


def test_check_reports_everything_ok_with_warnings(
    git_world: GitWorld, config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(git_world)
    install(monkeypatch, Machine(outputs()))
    config_home.parent.mkdir(parents=True)
    config_home.write_text(
        "github: alice\nagents:\n  - id: claude-alice\n    tool: claude-code\n"
        "    providers: [anthropic]\n    tiers: {strong: opus, standard: sonnet, light: haiku}\n"
        "default_agent: claude-alice\n"
    )
    result = invoke(["--json", "--repo", str(root), "setup", "--check"])
    assert result.exit_code == 0, result.output
    env = envelope(result.stdout)
    checks = {c["name"]: c for c in env["data"]["checks"]}
    for name in ("git", "gh_installed", "gh_version", "gh_auth", "python", "clock_skew"):
        assert checks[name]["ok"], name
    assert checks["forge_version"]["ok"] and checks["profile"]["ok"]
    assert checks["gitignore"]["ok"]
    # Not rendered yet and rerere not enabled: warnings with a fix, exit 0.
    assert not checks["local_adapters"]["ok"] and checks["local_adapters"]["fix"]
    assert not checks["rerere"]["ok"] and checks["rerere"]["level"] == "warning"


@pytest.mark.parametrize(
    ("machine", "name", "fix_part"),
    [
        (Machine(outputs(), git=(2, 30, 0)), "git", "2.38"),
        (
            Machine({k: v for k, v in outputs().items() if not k.startswith("gh")}),
            "gh_installed",
            "cli.github.com",
        ),
        (
            Machine(outputs(**{"gh --version": CommandOutput(0, "gh version 2.20.0")})),
            "gh_version",
            "upgrade gh",
        ),
        (
            Machine(outputs(**{"gh auth status": CommandOutput(1, "", "not logged in")})),
            "gh_auth",
            "gh auth login",
        ),
    ],
)
def test_check_missing_prerequisite_exits_5(
    machine: Machine,
    name: str,
    fix_part: str,
    config_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home.parent.mkdir(parents=True)
    config_home.write_text(
        "github: alice\nagents:\n  - {id: a, tool: claude-code, providers: [anthropic], "
        "tiers: {strong: opus}}\ndefault_agent: a\n"
    )
    monkeypatch.chdir(tmp_path)  # outside any repository
    install(monkeypatch, machine)
    result = invoke(["--json", "setup", "--check"])
    assert result.exit_code == 5, result.output
    env = envelope(result.stdout)
    failed = [c for c in env["data"]["checks"] if not c["ok"] and c["level"] == "error"]
    assert [c["name"] for c in failed] == [name]
    assert fix_part in failed[0]["fix"]


def test_check_missing_profile_exits_5_with_fix(
    config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, Machine(outputs()))
    result = invoke(["setup", "--check"])
    assert result.exit_code == 5
    assert "profile: no Forge profile" in result.stderr
    assert "Fix: run `forge setup` to create it" in result.stderr


def test_check_python_too_old(
    config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = Machine(outputs())
    monkeypatch.chdir(tmp_path)

    def env() -> MachineEnv:
        base = machine.env()
        return MachineEnv(
            run=base.run,
            shell=base.shell,
            git_version=base.git_version,
            python_version=lambda: (3, 10, 0),
            cli_version="0.1.0",
            now=base.now,
        )

    monkeypatch.setattr(setup_cmd, "env_factory", env)
    result = invoke(["--json", "setup", "--check"])
    assert result.exit_code == 5
    checks = {c["name"]: c for c in envelope(result.stdout)["data"]["checks"]}
    assert not checks["python"]["ok"] and checks["python"]["fix"]


def test_check_version_mismatch_and_clock_skew_are_warnings(
    git_world: GitWorld, config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(git_world, CONFIG.replace(">=0.1,<0.2", ">=0.5,<0.6"))
    late = outputs(**{"gh api -i /meta": CommandOutput(0, meta(NOW + timedelta(minutes=10)))})
    install(monkeypatch, Machine(late))
    config_home.parent.mkdir(parents=True)
    config_home.write_text(
        "github: alice\nagents:\n  - {id: a, tool: claude-code, providers: [anthropic], "
        "tiers: {strong: opus}}\ndefault_agent: a\n"
    )
    result = invoke(["--json", "--repo", str(root), "setup", "--check"])
    assert result.exit_code == 0
    checks = {c["name"]: c for c in envelope(result.stdout)["data"]["checks"]}
    assert checks["forge_version"]["level"] == "warning" and not checks["forge_version"]["ok"]
    assert checks["clock_skew"]["level"] == "warning" and not checks["clock_skew"]["ok"]


def test_check_gitignore_missing_is_required(
    git_world: GitWorld, config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(git_world)
    (root / ".gitignore").write_text(".forge/local/\n")
    install(monkeypatch, Machine(outputs()))
    config_home.parent.mkdir(parents=True)
    config_home.write_text(
        "github: alice\nagents:\n  - {id: a, tool: claude-code, providers: [anthropic], "
        "tiers: {strong: opus}}\ndefault_agent: a\n"
    )
    result = invoke(["--json", "--repo", str(root), "setup", "--check"])
    assert result.exit_code == 5
    checks = {c["name"]: c for c in envelope(result.stdout)["data"]["checks"]}
    assert not checks["gitignore"]["ok"] and ".claude/agents/forge-" in checks["gitignore"]["fix"]


# --------------------------------------------------------------------------- interactive


def test_setup_yes_in_project(
    git_world: GitWorld, config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(git_world)
    machine = Machine(outputs())
    install(monkeypatch, machine)
    result = invoke(["--json", "--repo", str(root), "setup", "--yes"])
    assert result.exit_code == 0, result.output
    data = envelope(result.stdout)["data"]
    profile = load_profile(config_home)
    assert [a.id for a in profile.agents] == ["claude-alice", "codex-alice"]
    rendered = set(data["rendered"])
    assert ".claude/agents/forge-reviewer.md" in rendered
    assert ".codex/agents/forge-reviewer.toml" in rendered
    git = Git(root)
    assert git.config_get("rerere.enabled") == "true"
    assert machine.shell_log == ["make deps"]
    for rel in rendered:
        assert (root / rel).is_file()
        assert git.run("check-ignore", "-q", rel, check=False).returncode == 0
    assert git.dirty_files() == [
        ".forge/agents/explorer.md",
        ".forge/agents/reviewer.md",
        ".forge/agents/test-writer.md",
        ".forge/config.yml",
        ".gitignore",
    ]
    # Re-run: up to date, nothing re-rendered, profile unchanged.
    again = invoke(["--json", "--repo", str(root), "setup", "--check"])
    checks = {c["name"]: c for c in envelope(again.stdout)["data"]["checks"]}
    assert checks["local_adapters"]["ok"] and checks["rerere"]["ok"], checks


def test_setup_yes_renders_into_task_worktrees(
    git_world: GitWorld, config_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = make_project(git_world)
    git = Git(root)
    git.add([".forge", ".gitignore"])
    git.commit("chore: forge files")
    task_wt = tmp_path / "wt" / "task-3"
    git.run("worktree", "add", "--quiet", "-b", "task/3", str(task_wt), "HEAD")
    install(monkeypatch, Machine(outputs()))

    result = invoke(["--json", "--repo", str(root), "setup", "--yes"])

    assert result.exit_code == 0, result.output
    rendered = envelope(result.stdout)["data"]["rendered"]
    for base in (root, task_wt):
        assert (base / ".claude" / "agents" / "forge-reviewer.md").is_file()
        assert (base / ".codex" / "agents" / "forge-reviewer.toml").is_file()
    assert str(task_wt / ".claude" / "agents" / "forge-reviewer.md") in rendered
    assert not Git(task_wt).is_dirty(include_untracked=True)


def test_setup_interactive_accept_defaults(
    git_world: GitWorld, config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(git_world)
    machine = Machine(outputs())
    install(monkeypatch, machine)
    result = invoke(["--repo", str(root), "setup"], input="\n" * 40)
    assert result.exit_code == 0, result.output
    assert "GitHub login [alice]" in result.output
    assert "Run `make deps`?" in result.output
    profile = load_profile(config_home)
    assert profile.default_agent == "claude-alice"
    assert (root / ".claude" / "agents" / "forge-explorer.md").is_file()
    assert machine.shell_log == ["make deps"]


def test_setup_interactive_edit_tier_and_decline_command(
    git_world: GitWorld, config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(git_world)
    only_claude = {k: v for k, v in outputs().items() if not k.startswith("codex")}
    machine = Machine(only_claude)
    install(monkeypatch, machine)
    answers = [
        "",  # GitHub login
        "",  # add claude-code agent
        "",  # agent id
        "fable",  # strong
        "",  # standard
        "-",  # light: none
        "",
        "",
        "",  # efforts
        "off",  # delegation mode (ADR 0017)
        "n",  # run make deps?
    ]
    result = invoke(["--repo", str(root), "setup"], input="\n".join(answers) + "\n")
    assert result.exit_code == 0, result.output
    assert load_profile(config_home).delegation == "off"
    agent = load_profile(config_home).agents[0]
    assert {str(k): v for k, v in agent.tiers.items()} == {"strong": "fable", "standard": "sonnet"}
    assert machine.shell_log == []
    reviewer = (root / ".claude" / "agents" / "forge-reviewer.md").read_text()
    assert "model: fable" in reviewer


def test_setup_local_models_from_ollama(
    config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    table = {k: v for k, v in outputs().items() if not k.startswith(("claude", "codex"))}
    table["ollama list"] = CommandOutput(
        0, "NAME ID SIZE MODIFIED\nllama3.1:8b a 4 GB now\nqwen2.5-coder:7b b 4 GB now\n"
    )
    install(monkeypatch, Machine(table))
    # login, add agent, id, strong (none), standard (by number), light (by name)
    answers = ["", "", "", "", "2", "llama3.1:8b"]
    result = invoke(["--json", "setup"], input="\n".join(answers) + "\n")
    assert result.exit_code == 0, result.output
    agent = load_profile(config_home).agents[0]
    assert agent.tool == "ollama" and agent.providers == ("local",)
    assert {str(k): v for k, v in agent.tiers.items()} == {
        "standard": "qwen2.5-coder:7b",
        "light": "llama3.1:8b",
    }
    # CliRunner echoes the typed answers on stdout; a real terminal echoes them on the TTY.
    env = envelope(result.stdout[result.stdout.index("{\n") :])
    assert any("not rendered" in m["text"] for m in env["messages"])


def test_setup_rerun_existing_profile(
    config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config_home.parent.mkdir(parents=True)
    config_home.write_text(
        yaml.safe_dump(
            {
                "github": "alice",
                "agents": [
                    {
                        "id": "my-claude",
                        "tool": "claude-code",
                        "providers": ["anthropic"],
                        "tiers": {"strong": "fable", "standard": "sonnet"},
                    }
                ],
                "default_agent": "my-claude",
                "worktrees_dir": "/tmp/wt",
            }
        )
    )
    install(monkeypatch, Machine(outputs()))
    result = invoke(["--yes", "setup"])
    assert result.exit_code == 0, result.output
    profile = load_profile(config_home)
    assert [a.id for a in profile.agents] == ["my-claude", "codex-alice"]
    assert profile.agents[0].tiers == load_profile(config_home).agents[0].tiers
    assert {str(k): v for k, v in profile.agents[0].tiers.items()} == {
        "strong": "fable",
        "standard": "sonnet",
    }
    assert profile.default_agent == "my-claude" and profile.worktrees_dir == Path("/tmp/wt")


def test_setup_no_tools_exits_5(
    config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    table = {k: v for k, v in outputs().items() if k.startswith("gh")}
    install(monkeypatch, Machine(table))
    result = invoke(["setup", "--yes"])
    assert result.exit_code == 5
    assert "no agent configured" in result.stderr
    assert not config_home.exists()
