"""Tests for forge.core.setup: machine checks, tool detection, the wizard and profile editing.

Every external command goes through stub runners; git behaviour uses real temporary repos.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from forge.core.config import DelegationConfig, ForgeConfig, parse_config
from forge.core.errors import ConfigError, ExitCode, ForgeError, PreconditionError
from forge.core.git import Git, GitError
from forge.core.github_fake import FakeGitHub
from forge.core.output import Level
from forge.core.presets import load_presets
from forge.core.profile import AgentProfile, Profile, load_profile, save_profile
from forge.core.setup import (
    NONE_CHOICE,
    NOT_FOUND,
    Check,
    CommandOutput,
    DefaultsIO,
    DetectedTool,
    MachineEnv,
    SetupTarget,
    ask_delegation,
    blocking,
    build_profile,
    default_agent_id,
    detect_tools,
    edit_profile,
    editor_command,
    machine_checks,
    parse_date_header,
    parse_ollama_list,
    profile_checks,
    project_checks,
    run_checks,
    run_setup,
    run_setup_commands,
    subprocess_runner,
)
from forge.core.tiers import Effort, Tier
from forge.core.validate import Fields

NOW = datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC)
PRESETS = load_presets()

CONFIG = """\
forge_version: ">=0.1,<0.2"
project: {name: shop}
github: {repo: acme/shop}
setup:
  commands: ["make deps", "make hooks"]
models:
  recommended: {strong: opus, standard: sonnet}
  min_tier_for: {reviewer: strong}
"""


def make_config(text: str = CONFIG) -> ForgeConfig:
    return parse_config(Fields(yaml.safe_load(text), ".forge/config.yml"))


# --------------------------------------------------------------------------- stubs


def meta_output(when: datetime) -> str:
    stamp = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
    return f"HTTP/2.0 200 OK\nContent-Type: application/json\nDate: {stamp}\n\n{{}}"


@dataclass
class FakeRunner:
    """Maps an argv (joined by spaces) to its output; unknown commands are "not found"."""

    outputs: dict[str, CommandOutput] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def __call__(self, argv: Sequence[str]) -> CommandOutput:
        key = " ".join(argv)
        self.calls.append(key)
        return self.outputs.get(key, CommandOutput(NOT_FOUND, "", "not found"))


def healthy_outputs(**overrides: CommandOutput) -> dict[str, CommandOutput]:
    outputs = {
        "gh --version": CommandOutput(0, "gh version 2.60.1 (2026-01-01)\n"),
        "gh auth status": CommandOutput(
            0, "github.com\n  ✓ Logged in to github.com account alice (keyring)\n"
        ),
        "gh api -i /meta": CommandOutput(0, meta_output(NOW + timedelta(seconds=20))),
        "claude --version": CommandOutput(0, "2.1.0 (Claude Code)\n"),
        "codex --version": CommandOutput(0, "codex-cli 0.80.0\n"),
    }
    outputs.update(overrides)
    return outputs


def make_env(
    runner: FakeRunner | None = None,
    *,
    git: tuple[int, int, int] | None = (2, 45, 0),
    python: tuple[int, int, int] = (3, 12, 1),
    cli_version: str = "0.1.0",
    shell_codes: dict[str, int] | None = None,
    shell_log: list[str] | None = None,
) -> MachineEnv:
    def git_version() -> tuple[int, int, int]:
        if git is None:
            raise GitError(["--version"], 127, "git: not found")
        return git

    def shell(command: str, cwd: Path) -> int:
        if shell_log is not None:
            shell_log.append(command)
        return (shell_codes or {}).get(command, 0)

    return MachineEnv(
        run=runner if runner is not None else FakeRunner(healthy_outputs()),
        shell=shell,
        git_version=git_version,
        python_version=lambda: python,
        cli_version=cli_version,
        now=lambda: NOW,
    )


@dataclass
class ScriptedIO:
    """Answers prompts that contain a key with the queued answers; the rest get defaults."""

    answers: dict[str, list[str | bool]] = field(default_factory=dict)
    prompts: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    def _take(self, prompt: str) -> str | bool | None:
        self.prompts.append(prompt)
        for key, queue in self.answers.items():
            if key in prompt and queue:
                return queue.pop(0)
        return None

    def ask(self, prompt: str, default: str) -> str:
        answer = self._take(prompt)
        return default if answer is None else str(answer)

    def confirm(self, prompt: str, default: bool = True) -> bool:
        answer = self._take(prompt)
        return default if answer is None else bool(answer)

    def choose(self, prompt: str, choices: Sequence[str], default: str) -> str:
        answer = self._take(prompt)
        if answer is None:
            return default
        assert answer in choices, (answer, choices)
        return str(answer)

    def print(self, text: str) -> None:
        self.lines.append(text)


@dataclass
class FakeAdapters:
    drift: list[str] = field(default_factory=list)
    unignored: list[str] = field(default_factory=list)
    rendered_for: list[Profile] = field(default_factory=list)

    def status(self, profile: Profile) -> list[str]:
        return self.drift

    def not_ignored(self, profile: Profile) -> list[str]:
        return self.unignored

    def render(self, profile: Profile) -> tuple[list[str], list[str]]:
        self.rendered_for.append(profile)
        return [".claude/agents/forge-explorer.md"], ["a render warning"]


CLAUDE = DetectedTool("claude-code", "claude", "2.1.0 (Claude Code)")
CODEX = DetectedTool("codex", "codex", "codex-cli 0.80.0")
OLLAMA = DetectedTool("ollama", "ollama", "", ("llama3.1:8b", "qwen2.5-coder:7b"))


def by_name(checks: Sequence[Check], name: str) -> list[Check]:
    return [c for c in checks if c.name == name]


def one(checks: Sequence[Check], name: str) -> Check:
    found = by_name(checks, name)
    assert len(found) == 1, (name, checks)
    return found[0]


# --------------------------------------------------------------------------- machine checks


def test_machine_checks_all_ok_and_login_read() -> None:
    report = machine_checks(make_env())
    assert [c.name for c in report.checks] == [
        "git",
        "gh_installed",
        "gh_version",
        "gh_auth",
        "clock_skew",
        "python",
    ]
    assert all(c.ok for c in report.checks)
    assert report.login == "alice"


def test_login_read_from_old_gh_format_on_stderr() -> None:
    runner = FakeRunner(
        healthy_outputs(
            **{"gh auth status": CommandOutput(0, "", "✓ Logged in to github.com as bob-2 (oauth)")}
        )
    )
    assert machine_checks(make_env(runner)).login == "bob-2"


def test_git_missing_is_required_with_fix() -> None:
    check = one(machine_checks(make_env(git=None)).checks, "git")
    assert not check.ok and check.level is Level.ERROR
    assert "not installed" in check.message and check.fix and "2.38" in check.fix


def test_git_too_old() -> None:
    check = one(machine_checks(make_env(git=(2, 37, 9))).checks, "git")
    assert not check.ok and check.level is Level.ERROR
    assert "2.37.9" in check.message and check.fix


def test_gh_missing_skips_dependent_checks() -> None:
    runner = FakeRunner({})
    report = machine_checks(make_env(runner))
    check = one(report.checks, "gh_installed")
    assert not check.ok and check.level is Level.ERROR and "cli.github.com" in (check.fix or "")
    assert not by_name(report.checks, "gh_auth")
    assert not by_name(report.checks, "clock_skew")
    assert report.login is None


def test_gh_too_old() -> None:
    runner = FakeRunner(
        healthy_outputs(**{"gh --version": CommandOutput(0, "gh version 2.40.0 (2023-12-01)")})
    )
    check = one(machine_checks(make_env(runner)).checks, "gh_version")
    assert not check.ok and check.level is Level.ERROR
    assert "2.40.0" in check.message and "2.48.0" in check.message and check.fix


def test_gh_version_unreadable() -> None:
    runner = FakeRunner(healthy_outputs(**{"gh --version": CommandOutput(0, "weird")}))
    check = one(machine_checks(make_env(runner)).checks, "gh_version")
    assert not check.ok and check.fix


def test_gh_not_authenticated() -> None:
    runner = FakeRunner(
        healthy_outputs(
            **{"gh auth status": CommandOutput(1, "", "You are not logged into any GitHub hosts")}
        )
    )
    report = machine_checks(make_env(runner))
    check = one(report.checks, "gh_auth")
    assert (
        not check.ok
        and check.level is Level.ERROR
        and check.fix == ("run `gh auth login` (with the repo scope)")
    )
    assert report.login is None


def test_python_too_old() -> None:
    check = one(machine_checks(make_env(python=(3, 10, 4))).checks, "python")
    assert not check.ok and check.level is Level.ERROR and "3.10.4" in check.message
    assert check.fix


def test_clock_skew_warns_above_five_minutes() -> None:
    runner = FakeRunner(
        healthy_outputs(
            **{"gh api -i /meta": CommandOutput(0, meta_output(NOW - timedelta(minutes=6)))}
        )
    )
    check = one(machine_checks(make_env(runner)).checks, "clock_skew")
    assert not check.ok and check.level is Level.WARNING and "360 s" in check.message
    assert check.fix and "NTP" in check.fix


def test_clock_skew_unreadable_is_info() -> None:
    runner = FakeRunner(healthy_outputs(**{"gh api -i /meta": CommandOutput(1, "", "offline")}))
    check = one(machine_checks(make_env(runner)).checks, "clock_skew")
    assert not check.ok and check.level is Level.INFO


def test_clock_skew_goes_through_the_github_client() -> None:
    fake = FakeGitHub(clock=lambda: NOW)
    fake.fake_set_server_skew(timedelta(minutes=10))
    runner = FakeRunner(healthy_outputs())
    env = replace(make_env(runner), github=fake)
    check = one(machine_checks(env).checks, "clock_skew")
    assert not check.ok and check.level is Level.WARNING and "600 s" in check.message
    assert "gh api -i /meta" not in runner.calls  # no direct gh call: the protocol is used
    fake.fake_set_server_skew(timedelta(seconds=30))
    assert one(machine_checks(env).checks, "clock_skew").ok
    fake.fake_set_server_skew(None)
    check = one(machine_checks(env).checks, "clock_skew")
    assert not check.ok and check.level is Level.INFO


def test_parse_date_header() -> None:
    assert parse_date_header(meta_output(NOW)) == NOW
    assert parse_date_header("HTTP/2.0 200 OK\ndate: garbage\n\n") is None
    assert parse_date_header("HTTP/2.0 200 OK\n\nDate: Tue, 03 Nov 2026 10:00:00 GMT") is None


# --------------------------------------------------------------------------- profile checks

GOOD_PROFILE = """\
github: alice
agents:
  - id: claude-alice
    tool: claude-code
    providers: [anthropic]
    tiers: {strong: opus, standard: sonnet, light: haiku}
default_agent: claude-alice
"""


def test_profile_missing(tmp_path: Path) -> None:
    checks, profile = profile_checks(tmp_path / "profile.yml", PRESETS, None)
    assert profile is None
    check = one(checks, "profile")
    assert not check.ok and check.level is Level.ERROR and "forge setup" in (check.fix or "")


def test_profile_invalid(tmp_path: Path) -> None:
    path = tmp_path / "profile.yml"
    path.write_text("github: alice\nagents: []\ndefault_agent: x\n")
    checks, profile = profile_checks(path, PRESETS, None)
    check = one(checks, "profile")
    assert profile is None and not check.ok and "invalid profile" in check.message


def test_profile_valid_without_violations(tmp_path: Path) -> None:
    path = tmp_path / "profile.yml"
    path.write_text(GOOD_PROFILE)
    checks, profile = profile_checks(path, PRESETS, make_config())
    assert profile is not None
    assert all(c.ok for c in checks)
    assert {c.name for c in checks} == {"profile", "profile_constraints", "preset_drift"}


def test_profile_constraint_violations_and_drift(tmp_path: Path) -> None:
    path = tmp_path / "profile.yml"
    path.write_text(
        "github: alice\nagents:\n  - id: hybrid\n    tool: claude-code\n"
        "    providers: [anthropic, local]\n    tiers: {standard: llama-local}\n"
        "default_agent: hybrid\n"
    )
    checks, _ = profile_checks(path, PRESETS, make_config())
    violations = by_name(checks, "profile_constraints")
    assert any("reviewer" in c.message and c.level is Level.WARNING for c in violations)
    drift = one(checks, "preset_drift")
    assert not drift.ok and drift.level is Level.INFO and "sonnet" in drift.message
    assert drift.fix and "forge profile set hybrid.standard=sonnet" in drift.fix


# --------------------------------------------------------------------------- project checks


def test_rerere_check_and_enable(tmp_path: Path) -> None:
    git = Git(tmp_path)
    git.run("init", "--quiet")
    env = make_env()
    check = one(project_checks(SetupTarget(repo_root=tmp_path, git=git), None, env), "rerere")
    assert not check.ok and check.level is Level.WARNING and check.fix
    git.config_set("rerere.enabled", "true")
    assert one(project_checks(SetupTarget(repo_root=tmp_path, git=git), None, env), "rerere").ok


def test_forge_version_mismatch_is_warning() -> None:
    target = SetupTarget(repo_root=Path("."), config=make_config())
    check = one(project_checks(target, None, make_env(cli_version="0.3.0")), "forge_version")
    assert not check.ok and check.level is Level.WARNING and check.fix
    ok = one(project_checks(target, None, make_env(cli_version="0.1.0.dev0")), "forge_version")
    assert ok.ok


def test_config_error_is_reported() -> None:
    err = ConfigError(".forge/config.yml", "project", "required key is missing", "add it")
    target = SetupTarget(repo_root=Path("."), config_error=err)
    check = one(project_checks(target, None, make_env()), "config")
    assert not check.ok and check.level is Level.ERROR and check.fix == "add it"


def test_local_adapter_checks() -> None:
    profile = load_profile_text(GOOD_PROFILE)
    fake = FakeAdapters(drift=["missing .claude/agents/forge-reviewer.md"])
    target = SetupTarget(repo_root=Path("."), config=make_config(), adapters=fake)
    checks = project_checks(target, profile, make_env())
    adapters = one(checks, "local_adapters")
    assert not adapters.ok and "forge-reviewer" in adapters.message and adapters.fix
    assert one(checks, "gitignore").ok
    fake = FakeAdapters(unignored=[".claude/agents/forge-explorer.md"])
    target = SetupTarget(repo_root=Path("."), config=make_config(), adapters=fake)
    checks = project_checks(target, profile, make_env())
    assert one(checks, "local_adapters").ok
    gitignore = one(checks, "gitignore")
    assert (
        not gitignore.ok
        and gitignore.level is Level.ERROR
        and ".gitignore" in (gitignore.fix or "")
    )


def load_profile_text(text: str) -> Profile:
    from forge.core.profile import parse_profile

    return parse_profile(Fields(yaml.safe_load(text), "profile.yml"))


# --------------------------------------------------------------------------- run_checks


def test_run_checks_exit_5_when_required_missing(tmp_path: Path) -> None:
    path = tmp_path / "profile.yml"
    path.write_text(GOOD_PROFILE)
    result = run_checks(make_env(FakeRunner({})), SetupTarget(), path, PRESETS)
    assert result.code == ExitCode.PRECONDITION
    names = [c["name"] for c in result.data["checks"] if not c["ok"]]
    assert names == ["gh_installed"]
    assert set(result.data["checks"][0]) == {"name", "ok", "level", "message", "fix"}
    assert any(m.level is Level.ERROR and m.hint for m in result.messages)


def test_run_checks_warnings_alone_exit_0(tmp_path: Path) -> None:
    path = tmp_path / "profile.yml"
    path.write_text(GOOD_PROFILE)
    runner = FakeRunner(
        healthy_outputs(
            **{"gh api -i /meta": CommandOutput(0, meta_output(NOW + timedelta(hours=1)))}
        )
    )
    result = run_checks(make_env(runner), SetupTarget(), path, PRESETS)
    assert result.code == ExitCode.OK
    assert any(m.level is Level.WARNING for m in result.messages)


def test_run_checks_warns_when_profile_github_differs_from_gh_login(tmp_path: Path) -> None:
    path = tmp_path / "profile.yml"
    path.write_text(GOOD_PROFILE.replace("github: alice", "github: bob"))
    result = run_checks(make_env(), SetupTarget(), path, PRESETS)
    assert result.code == ExitCode.OK
    check = next(c for c in result.data["checks"] if c["name"] == "github_login")
    assert not check["ok"] and check["level"] == "warning"
    assert "'bob'" in check["message"] and "'alice'" in check["message"]
    assert "github: alice" in check["fix"]
    assert any(m.level is Level.WARNING and "github_login" in m.text for m in result.messages)


def test_run_checks_login_match_is_case_insensitive(tmp_path: Path) -> None:
    path = tmp_path / "profile.yml"
    path.write_text(GOOD_PROFILE.replace("github: alice", "github: Alice"))
    result = run_checks(make_env(), SetupTarget(), path, PRESETS)
    check = next(c for c in result.data["checks"] if c["name"] == "github_login")
    assert check["ok"]


def test_run_checks_login_falls_back_to_current_user(tmp_path: Path) -> None:
    path = tmp_path / "profile.yml"
    path.write_text(GOOD_PROFILE)
    runner = FakeRunner(
        healthy_outputs(**{"gh auth status": CommandOutput(0, "github.com\n  logged in\n")})
    )
    env = replace(make_env(runner), github=FakeGitHub(user="carol", clock=lambda: NOW))
    result = run_checks(env, SetupTarget(), path, PRESETS)
    check = next(c for c in result.data["checks"] if c["name"] == "github_login")
    assert not check["ok"] and "'carol'" in check["message"]


def test_run_checks_no_login_check_without_gh(tmp_path: Path) -> None:
    path = tmp_path / "profile.yml"
    path.write_text(GOOD_PROFILE.replace("github: alice", "github: bob"))
    result = run_checks(make_env(FakeRunner({})), SetupTarget(), path, PRESETS)
    assert all(c["name"] != "github_login" for c in result.data["checks"])


def test_run_checks_missing_profile_is_required(tmp_path: Path) -> None:
    result = run_checks(make_env(), SetupTarget(), tmp_path / "none.yml", PRESETS)
    assert result.code == ExitCode.PRECONDITION
    assert blocking([Check("x", False, Level.WARNING, "m")]) == []


# --------------------------------------------------------------------------- detection


def test_parse_ollama_list() -> None:
    text = (
        "NAME                ID              SIZE      MODIFIED\n"
        "llama3.1:8b         42182419e950    4.7 GB    2 days ago\n"
        "qwen2.5-coder:7b    2b0496514337    4.7 GB    3 weeks ago\n\n"
    )
    assert parse_ollama_list(text) == ["llama3.1:8b", "qwen2.5-coder:7b"]
    assert parse_ollama_list("") == []


def test_detect_tools() -> None:
    runner = FakeRunner(
        healthy_outputs(
            **{"ollama list": CommandOutput(0, "NAME ID SIZE MODIFIED\nllama3.1:8b x 4 GB now\n")}
        )
    )
    tools = detect_tools(runner)
    assert [t.tool for t in tools] == ["claude-code", "codex", "ollama"]
    assert tools[0].version == "2.1.0 (Claude Code)"
    assert tools[2].models == ("llama3.1:8b",)
    assert detect_tools(FakeRunner({})) == []


def test_subprocess_runner_missing_command() -> None:
    assert subprocess_runner(["forge-no-such-command-xyz"]).code == NOT_FOUND
    assert subprocess_runner([sys.executable, "-c", "print('hi')"]).out.strip() == "hi"


# --------------------------------------------------------------------------- wizard


def test_wizard_accepts_all_defaults() -> None:
    io = DefaultsIO()
    profile = build_profile(io, tools=[CLAUDE, CODEX], presets=PRESETS, login="Alice")
    assert profile.github == "Alice"
    assert [a.id for a in profile.agents] == ["claude-alice", "codex-alice"]
    claude, codex = profile.agents
    assert claude.tiers == {Tier.STRONG: "opus", Tier.STANDARD: "sonnet", Tier.LIGHT: "haiku"}
    assert claude.effort == {Effort.LOW: "low", Effort.MEDIUM: "medium", Effort.HIGH: "high"}
    assert claude.providers == ("anthropic",)
    assert codex.tiers[Tier.STRONG] == "gpt-6-astra" and codex.providers == ("openai",)
    assert profile.default_agent == "claude-alice"
    assert default_agent_id("codex", "Bob") == "codex-bob"


def test_wizard_edits_a_tier_effort_and_id_and_shows_recommendations() -> None:
    io = ScriptedIO(
        {
            "Agent id for claude-code": ["my-claude"],
            "strong model": ["fable"],
            "light model": ["-"],
            "effort high": ["max"],
            "Add an agent for codex": [False],
        }
    )
    profile = build_profile(
        io,
        tools=[CLAUDE, CODEX],
        presets=PRESETS,
        login="alice",
        recommended={Tier.STRONG: "opus", Tier.STANDARD: "gpt-6.1-sol"},
    )
    (agent,) = profile.agents
    assert agent.id == "my-claude"
    assert agent.tiers == {Tier.STRONG: "fable", Tier.STANDARD: "sonnet"}
    assert agent.effort[Effort.HIGH] == "max"
    strong_prompt = next(p for p in io.prompts if "strong model" in p)
    assert "project recommends opus" in strong_prompt
    standard_prompt = next(p for p in io.prompts if "standard model" in p)
    assert "recommends" not in standard_prompt  # gpt-6.1-sol is not a Claude model


def test_wizard_local_models_from_ollama_list() -> None:
    io = ScriptedIO({"light model (local)": ["qwen2.5-coder:7b"]})
    profile = build_profile(io, tools=[OLLAMA], presets=PRESETS, login="alice")
    (agent,) = profile.agents
    assert agent.id == "ollama-alice" and agent.providers == ("local",)
    # Defaults: no strong local model, the first listed model for standard.
    assert agent.tiers == {Tier.STANDARD: "llama3.1:8b", Tier.LIGHT: "qwen2.5-coder:7b"}
    assert agent.effort == {}  # the ollama preset does not support effort
    assert not any("effort" in p for p in io.prompts)


def test_wizard_ollama_all_none_skips_agent() -> None:
    io = ScriptedIO({"model (local)": [NONE_CHOICE, NONE_CHOICE, NONE_CHOICE]})
    profile = build_profile(io, tools=[CLAUDE, OLLAMA], presets=PRESETS, login="alice")
    assert [a.id for a in profile.agents] == ["claude-alice"]
    assert any("not added" in line for line in io.lines)


def test_wizard_rerun_starts_from_existing_profile() -> None:
    existing = Profile(
        github="alice",
        agents=(
            AgentProfile(
                "claude-pro-1",
                "claude-code",
                ("anthropic", "local"),
                {Tier.STRONG: "opus", Tier.LIGHT: "llama-local"},
                {Effort.HIGH: "high"},
            ),
            AgentProfile("old-gemini", "gemini", ("google",), {Tier.STANDARD: "g-1"}),
        ),
        default_agent="old-gemini",
        worktrees_dir=Path("/tmp/wt"),
    )
    io = ScriptedIO({"Add an agent for codex": [True]})
    profile = build_profile(
        io, tools=[CLAUDE, CODEX], presets=PRESETS, existing=existing, login=None
    )
    assert profile.github == "alice"
    assert [a.id for a in profile.agents] == ["claude-pro-1", "codex-alice", "old-gemini"]
    claude = profile.agents[0]
    # Existing values are kept (no tier added from the preset), drift is shown, not applied.
    assert claude.tiers == {Tier.STRONG: "opus", Tier.LIGHT: "llama-local"}
    assert claude.providers == ("anthropic", "local")
    assert claude.effort[Effort.HIGH] == "high"
    assert any("preset: haiku" in p for p in io.prompts)
    assert profile.default_agent == "old-gemini"
    assert profile.worktrees_dir == Path("/tmp/wt")
    assert any("old-gemini unchanged" in line for line in io.lines)


def test_wizard_rerun_with_defaults_is_stable() -> None:
    first = build_profile(DefaultsIO(), tools=[CLAUDE, OLLAMA], presets=PRESETS, login="alice")
    tools = [CLAUDE, DetectedTool("ollama", "ollama", "", ("other:1b",))]
    second = build_profile(DefaultsIO(), tools=tools, presets=PRESETS, existing=first)
    assert second == first


def test_wizard_rejects_invalid_id_then_accepts() -> None:
    io = ScriptedIO({"Agent id": ["Bad Id", "good-id"]})
    profile = build_profile(io, tools=[CLAUDE], presets=PRESETS, login="alice")
    assert profile.agents[0].id == "good-id"
    assert any("not a valid agent id" in line for line in io.lines)


def test_wizard_gives_up_without_login() -> None:
    with pytest.raises(ForgeError, match="GitHub login"):
        build_profile(DefaultsIO(), tools=[CLAUDE], presets=PRESETS, login=None)


def test_wizard_without_tools_fails() -> None:
    with pytest.raises(PreconditionError, match="no agent configured"):
        build_profile(DefaultsIO(), tools=[], presets=PRESETS, login="alice")


def test_wizard_chooses_default_agent() -> None:
    io = ScriptedIO({"Default agent": ["codex-alice"]})
    profile = build_profile(io, tools=[CLAUDE, CODEX], presets=PRESETS, login="alice")
    assert profile.default_agent == "codex-alice"


# --------------------------------------------------------------------------- setup commands


def test_setup_commands_confirmed_and_stop_on_failure(tmp_path: Path) -> None:
    log: list[str] = []

    def shell(command: str, cwd: Path) -> int:
        log.append(command)
        assert cwd == tmp_path
        return 2 if command == "b" else 0

    io = ScriptedIO({"Run `a`": [False]})
    runs = run_setup_commands(io, ["a", "b", "c"], tmp_path, shell)
    assert [(r.command, r.ran, r.code) for r in runs] == [("a", False, None), ("b", True, 2)]
    assert log == ["b"]


# --------------------------------------------------------------------------- run_setup


def project_target(root: Path, adapters: FakeAdapters) -> SetupTarget:
    git = Git(root)
    git.run("init", "--quiet")
    return SetupTarget(repo_root=root, git=git, config=make_config(), adapters=adapters)


def test_run_setup_yes_writes_profile_and_runs_everything(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    adapters = FakeAdapters()
    target = project_target(repo, adapters)
    log: list[str] = []
    path = tmp_path / "cfg" / "profile.yml"
    result = run_setup(DefaultsIO(), make_env(shell_log=log), target, path, PRESETS)
    assert result.code == ExitCode.OK, result.messages
    saved = load_profile(path)
    assert [a.id for a in saved.agents] == ["claude-alice", "codex-alice"]
    assert "token" not in path.read_text() and "password" not in path.read_text()
    assert target.git is not None and target.git.config_get("rerere.enabled") == "true"
    assert target.git.config_get("rerere.autoupdate") == "true"
    assert adapters.rendered_for == [saved]
    assert result.data["rendered"] == [".claude/agents/forge-explorer.md"]
    assert log == ["make deps", "make hooks"]
    assert [c["code"] for c in result.data["commands"]] == [0, 0]
    assert any(m.text == "a render warning" for m in result.messages)
    assert result.data["rerere"] is True
    assert result.data["violations"] == []


def test_run_setup_does_not_report_what_it_fixed(tmp_path: Path) -> None:
    """rerere is off before setup and enabled by it: no stale rerere warning remains."""
    repo = tmp_path / "repo"
    repo.mkdir()
    target = project_target(repo, FakeAdapters())
    assert target.git is not None and target.git.config_get("rerere.enabled") is None
    result = run_setup(DefaultsIO(), make_env(), target, tmp_path / "p.yml", PRESETS)
    assert result.code == ExitCode.OK, result.messages
    assert not any("rerere" in m.text for m in result.messages), result.messages
    rerere = [c for c in result.data["checks"] if c["name"] == "rerere"]
    assert rerere and rerere[0]["ok"] is True


def test_run_setup_failing_command_exits_1(tmp_path: Path) -> None:
    (tmp_path / "r").mkdir()
    target = project_target(tmp_path / "r", FakeAdapters())
    env = make_env(shell_codes={"make deps": 3})
    result = run_setup(DefaultsIO(), env, target, tmp_path / "p.yml", PRESETS)
    assert result.code == ExitCode.ERROR
    assert [c["command"] for c in result.data["commands"]] == ["make deps"]


def test_run_setup_outside_project_skips_render_and_commands(tmp_path: Path) -> None:
    log: list[str] = []
    result = run_setup(
        DefaultsIO(), make_env(shell_log=log), SetupTarget(), tmp_path / "p.yml", PRESETS
    )
    assert result.code == ExitCode.OK
    assert result.data["rendered"] == [] and result.data["commands"] == []
    assert result.data["rerere"] is False and log == []
    assert any("not rendered" in m.text for m in result.messages)


def test_run_setup_missing_prerequisite_still_writes_profile_exit_5(tmp_path: Path) -> None:
    runner = FakeRunner(
        healthy_outputs(**{"gh auth status": CommandOutput(1, "", "not logged in")})
    )
    path = tmp_path / "p.yml"
    io = ScriptedIO({"GitHub login": ["carol"]})
    result = run_setup(io, make_env(runner), SetupTarget(), path, PRESETS)
    assert result.code == ExitCode.PRECONDITION
    assert load_profile(path).github == "carol"


def test_run_setup_warns_on_violations(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = project_target(repo, FakeAdapters())
    io = ScriptedIO({"strong model": ["-"], "Add an agent for codex": [False]})
    result = run_setup(io, make_env(), target, tmp_path / "p.yml", PRESETS)
    assert any("reviewer" in v["message"] for v in result.data["violations"])
    assert any(m.level is Level.WARNING and "reviewer" in m.text for m in result.messages)


def test_run_setup_replaces_invalid_existing_profile(tmp_path: Path) -> None:
    path = tmp_path / "p.yml"
    path.write_text("github: [oops\n")
    result = run_setup(DefaultsIO(), make_env(), SetupTarget(), path, PRESETS)
    assert result.code == ExitCode.OK
    assert any("invalid" in m.text for m in result.messages)
    assert load_profile(path).github == "alice"


def test_run_setup_rerun_keeps_existing_values(tmp_path: Path) -> None:
    path = tmp_path / "p.yml"
    save_profile(load_profile_text(GOOD_PROFILE.replace("haiku", "claude-haiku-5-5")), path)
    run_setup(DefaultsIO(), make_env(), SetupTarget(), path, PRESETS)
    saved = load_profile(path)
    assert saved.agents[0].tiers[Tier.LIGHT] == "claude-haiku-5-5"
    assert [a.id for a in saved.agents] == ["claude-alice", "codex-alice"]


# --------------------------------------------------------------------------- profile edit


def test_editor_command_order() -> None:
    # ADR 0014: $VISUAL, then $EDITOR, then vi.
    assert editor_command({"EDITOR": "code --wait", "VISUAL": "nano"}) == ["nano"]
    assert editor_command({"EDITOR": "code --wait", "VISUAL": " "}) == ["code", "--wait"]
    assert editor_command({"VISUAL": "nano"}) == ["nano"]
    assert editor_command({"EDITOR": "  "}) == ["vi"]
    assert editor_command({}) == ["vi"]


def fake_editor(new_text: str | None, code: int = 0) -> Callable[[Sequence[str]], int]:
    def run(argv: Sequence[str]) -> int:
        if new_text is not None:
            Path(argv[-1]).write_text(new_text)
        return code

    return run


def test_edit_profile_valid(tmp_path: Path) -> None:
    path = tmp_path / "profile.yml"
    path.write_text(GOOD_PROFILE)
    edited = GOOD_PROFILE.replace("light: haiku", "light: sonnet") + "# my note\n"
    outcome = edit_profile(path, ["ed"], fake_editor(edited))
    assert outcome.changed and outcome.profile.agents[0].tiers[Tier.LIGHT] == "sonnet"
    assert path.read_text() == edited  # comments kept


def test_edit_profile_invalid_keeps_old(tmp_path: Path) -> None:
    path = tmp_path / "profile.yml"
    path.write_text(GOOD_PROFILE)
    with pytest.raises(ForgeError, match="invalid") as info:
        edit_profile(path, ["ed"], fake_editor(GOOD_PROFILE + "api_key: secret\n"))
    assert info.value.code == ExitCode.ERROR
    assert path.read_text() == GOOD_PROFILE


def test_edit_profile_editor_failure_and_no_change(tmp_path: Path) -> None:
    path = tmp_path / "profile.yml"
    path.write_text(GOOD_PROFILE)
    with pytest.raises(ForgeError, match="exited with code 1"):
        edit_profile(path, ["ed"], fake_editor("garbage", code=1))
    assert path.read_text() == GOOD_PROFILE
    assert not edit_profile(path, ["ed"], fake_editor(None)).changed


def test_edit_profile_missing(tmp_path: Path) -> None:
    with pytest.raises(PreconditionError):
        edit_profile(tmp_path / "none.yml", ["ed"], fake_editor(None))


# --------------------------------------------------------------------------- delegation


def test_wizard_yes_keeps_the_project_default_and_writes_nothing() -> None:
    io = DefaultsIO()
    policy = DelegationConfig(default="off")
    profile = build_profile(io, tools=[CLAUDE], presets=PRESETS, login="alice", delegation=policy)
    assert profile.delegation is None
    assert any("project default: off" in line for line in io.lines)


def test_wizard_asks_delegation_with_project_default_preselected() -> None:
    seen: list[tuple[str, list[str], str]] = []

    class Recording(ScriptedIO):
        def choose(self, prompt: str, choices: Sequence[str], default: str) -> str:
            seen.append((prompt, list(choices), default))
            return super().choose(prompt, choices, default)

    io = Recording({"Delegation mode": ["on"]})
    policy = DelegationConfig(default="off", allowed=("off", "on"))
    profile = build_profile(io, tools=[CLAUDE], presets=PRESETS, login="alice", delegation=policy)
    assert seen == [("Delegation mode", ["off", "on"], "off")]
    assert profile.delegation == "on"


def test_wizard_rerun_keeps_an_explicit_delegation() -> None:
    first = build_profile(
        ScriptedIO({"Delegation mode": ["auto"]}),
        tools=[CLAUDE],
        presets=PRESETS,
        login="alice",
    )
    assert first.delegation is None  # equal to the (built-in) project default
    explicit = replace(first, delegation="auto")
    again = build_profile(DefaultsIO(), tools=[CLAUDE], presets=PRESETS, existing=explicit)
    assert again.delegation == "auto"
    off = replace(first, delegation="off")
    assert build_profile(DefaultsIO(), tools=[CLAUDE], presets=PRESETS, existing=off) == off


def test_wizard_keeps_a_stored_mode_the_project_does_not_allow() -> None:
    existing = replace(
        build_profile(DefaultsIO(), tools=[CLAUDE], presets=PRESETS, login="alice"),
        delegation="on",
    )
    policy = DelegationConfig(default="auto", allowed=("off", "auto"))
    io = ScriptedIO()
    profile = build_profile(
        io, tools=[CLAUDE], presets=PRESETS, existing=existing, delegation=policy
    )
    assert profile.delegation == "on"
    assert not any("Delegation mode" in p for p in io.prompts)
    assert any("not allowed by this project" in line for line in io.lines)
    # --yes keeps it too
    again = build_profile(
        DefaultsIO(), tools=[CLAUDE], presets=PRESETS, existing=existing, delegation=policy
    )
    assert again.delegation == "on"


def test_wizard_single_allowed_mode_keeps_the_stored_choice() -> None:
    existing = replace(
        build_profile(DefaultsIO(), tools=[CLAUDE], presets=PRESETS, login="alice"),
        delegation="on",
    )
    policy = DelegationConfig(default="off", allowed=("off",))
    for io in (DefaultsIO(), ScriptedIO()):
        profile = build_profile(
            io, tools=[CLAUDE], presets=PRESETS, existing=existing, delegation=policy
        )
        assert profile.delegation == "on"


def test_wizard_single_allowed_mode_is_not_asked() -> None:
    io = ScriptedIO()
    policy = DelegationConfig(default="off", allowed=("off",))
    profile = build_profile(io, tools=[CLAUDE], presets=PRESETS, login="alice", delegation=policy)
    assert not any("Delegation" in p for p in io.prompts)
    assert profile.delegation is None


def test_wizard_without_subagent_tools_does_not_ask_delegation() -> None:
    io = ScriptedIO()
    profile = build_profile(io, tools=[OLLAMA], presets=PRESETS, login="alice")
    assert not any("Delegation" in p for p in io.prompts)
    assert profile.delegation is None


def test_ask_delegation_returns_choice() -> None:
    io = ScriptedIO({"Delegation mode": ["off"]})
    assert ask_delegation(io, DelegationConfig(), None, tools=["codex"]) == "off"


def test_run_setup_uses_the_project_delegation_policy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = replace(
        project_target(repo, FakeAdapters()),
        config=replace(make_config(), delegation=DelegationConfig(default="on")),
    )
    path = tmp_path / "cfg" / "profile.yml"
    io = ScriptedIO({"Delegation mode": ["off"]})
    result = run_setup(io, make_env(), target, path, PRESETS)
    assert result.code == ExitCode.OK, result.messages
    assert load_profile(path).delegation == "off"
    assert result.data["profile"]["delegation"] == "off"
