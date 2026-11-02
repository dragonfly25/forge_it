"""Tests for forge.core.profile (`~/.config/forge/profile.yml`, spec §6.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.core.errors import ConfigError, ForgeError
from forge.core.profile import (
    AgentProfile,
    Profile,
    apply_setting,
    dump_profile,
    load_profile,
    profile_path,
    save_profile,
    select_agent,
)
from forge.core.tiers import Effort, Tier

# Verbatim from spec §6.4.
SPEC_EXAMPLE = """\
github: vincenzo-s
agents:
  - id: claude-pro-1               # free-form label, appears in claims and handoffs
    tool: claude-code
    providers: [anthropic]
    tiers: {strong: opus, standard: sonnet, light: haiku}
    effort: {low: low, medium: medium, high: high}   # abstract → tool-specific value
  - id: openai-team
    tool: codex
    providers: [openai]
    tiers: {standard: gpt-5}
  - id: vincenzo-hybrid            # orchestrator + local model as executor
    tool: claude-code
    providers: [anthropic, local]
    tiers: {strong: opus, standard: sonnet, light: llama-local}
default_agent: claude-pro-1
worktrees_dir: null                # null = <parent-of-repo>/<repo>-worktrees
"""

AGENT = """\
github: vincenzo-s
agents:
  - id: a1
    tool: claude-code
    providers: [anthropic]
    tiers: {standard: sonnet}
{extra_agent}default_agent: a1
{extra_top}"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profile.yml"
    path.write_text(text, encoding="utf-8")
    return path


def make(tmp_path: Path, *, extra_agent: str = "", extra_top: str = "") -> Path:
    return write(
        tmp_path, AGENT.replace("{extra_agent}", extra_agent).replace("{extra_top}", extra_top)
    )


def error(path: Path) -> ConfigError:
    with pytest.raises(ConfigError) as exc:
        load_profile(path)
    return exc.value


def test_spec_example_loads(tmp_path: Path) -> None:
    profile = load_profile(write(tmp_path, SPEC_EXAMPLE))
    assert profile.github == "vincenzo-s"
    assert profile.default_agent == "claude-pro-1"
    assert profile.worktrees_dir is None
    assert [a.id for a in profile.agents] == ["claude-pro-1", "openai-team", "vincenzo-hybrid"]
    first = profile.agents[0]
    assert first.tool == "claude-code"
    assert first.providers == ("anthropic",)
    assert first.tiers == {Tier.STRONG: "opus", Tier.STANDARD: "sonnet", Tier.LIGHT: "haiku"}
    assert first.effort == {Effort.LOW: "low", Effort.MEDIUM: "medium", Effort.HIGH: "high"}
    assert profile.agents[1].effort == {}
    assert profile.agents[2].providers == ("anthropic", "local")


def test_missing_file_suggests_forge_setup(tmp_path: Path) -> None:
    err = error(tmp_path / "nope.yml")
    assert "file not found" in err.problem
    assert err.hint is not None and "forge setup" in err.hint


@pytest.mark.parametrize(
    ("extra_agent", "extra_top", "key", "problem"),
    [
        ("", "token: abc\n", "token", "unknown key"),
        ("    token: abc\n", "", "agents[0].token", "unknown key"),
        (
            "  - id: a2\n    tool: codex\n    providers: [openai]\n    tiers: {strong: ''}\n",
            "",
            "agents[1].tiers.strong",
            "must not be empty",
        ),
        (
            "  - id: a2\n    tool: codex\n    providers: [openai]\n    tiers: {huge: x}\n",
            "",
            "agents[1].tiers.huge",
            "unknown key",
        ),
        (
            "  - id: a2\n    tool: codex\n    providers: [openai]\n    effort: {max: x}\n",
            "",
            "agents[1].effort.max",
            "unknown key",
        ),
        (
            "  - id: a2\n    tool: codex\n    providers: [openai]\n    effort: {high: 3}\n",
            "",
            "agents[1].effort.high",
            "expected a string",
        ),
        ("  - id: a1\n    tool: codex\n    providers: [openai]\n", "", "agents[1].id", "duplicate"),
        (
            "  - id: Bad_Id\n    tool: codex\n    providers: [openai]\n",
            "",
            "agents[1].id",
            "pattern",
        ),
        ("  - id: a2\n    tool: ''\n    providers: [openai]\n", "", "agents[1].tool", "empty"),
        ("  - id: a2\n    providers: [openai]\n", "", "agents[1].tool", "missing"),
        (
            "  - id: a2\n    tool: codex\n    providers: []\n",
            "",
            "agents[1].providers",
            "non-empty",
        ),
        ("  - id: a2\n    tool: codex\n", "", "agents[1].providers", "non-empty"),
        ("", "worktrees_dir: ''\n", "worktrees_dir", "empty"),
    ],
)
def test_validation_errors(
    tmp_path: Path, extra_agent: str, extra_top: str, key: str, problem: str
) -> None:
    err = error(make(tmp_path, extra_agent=extra_agent, extra_top=extra_top))
    assert err.key == key
    assert problem in err.problem


def test_token_key_is_rejected(tmp_path: Path) -> None:
    err = error(make(tmp_path, extra_top="token: ghp_secret\n"))
    assert err.key == "token"
    assert "unknown key" in err.problem


def test_no_agents(tmp_path: Path) -> None:
    err = error(write(tmp_path, "github: vincenzo-s\nagents: []\ndefault_agent: a1\n"))
    assert err.key == "agents"
    assert "at least one agent" in err.problem


def test_default_agent_must_exist(tmp_path: Path) -> None:
    text = (
        AGENT.replace("{extra_agent}", "")
        .replace("{extra_top}", "")
        .replace("default_agent: a1", "default_agent: zz")
    )
    err = error(write(tmp_path, text))
    assert err.key == "default_agent"
    assert err.hint is not None and "a1" in err.hint


def test_github_login_required_and_valid(tmp_path: Path) -> None:
    text = AGENT.replace("{extra_agent}", "").replace("{extra_top}", "")
    assert error(write(tmp_path, text.replace("github: vincenzo-s\n", ""))).key == "github"
    assert error(write(tmp_path, text.replace("vincenzo-s", "-bad-"))).key == "github"


def test_worktrees_dir_expands_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    profile = load_profile(make(tmp_path, extra_top="worktrees_dir: ~/wt\n"))
    assert profile.worktrees_dir == tmp_path / "wt"


# -- profile_path -----------------------------------------------------------------------------


def test_profile_path_uses_xdg(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "HOME": "/home/x"}
    assert profile_path(env) == tmp_path / "cfg" / "forge" / "profile.yml"


@pytest.mark.parametrize("xdg", [None, "", "relative/dir"])
def test_profile_path_defaults_to_home_config(xdg: str | None) -> None:
    env = {"HOME": "/home/x"}
    if xdg is not None:
        env["XDG_CONFIG_HOME"] = xdg
    assert profile_path(env) == Path("/home/x/.config/forge/profile.yml")


def test_load_profile_default_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "forge").mkdir()
    (tmp_path / "forge" / "profile.yml").write_text(SPEC_EXAMPLE, encoding="utf-8")
    assert load_profile().default_agent == "claude-pro-1"


# -- select_agent -----------------------------------------------------------------------------


@pytest.fixture
def spec_profile(tmp_path: Path) -> Profile:
    return load_profile(write(tmp_path, SPEC_EXAMPLE))


def test_select_agent_precedence(spec_profile: Profile) -> None:
    assert select_agent(spec_profile, None).id == "claude-pro-1"
    assert select_agent(spec_profile, "").id == "claude-pro-1"
    assert select_agent(spec_profile, "openai-team").id == "openai-team"


def test_select_agent_unknown_lists_ids(spec_profile: Profile) -> None:
    with pytest.raises(ForgeError) as exc:
        select_agent(spec_profile, "ghost")
    assert "ghost" in exc.value.message
    assert exc.value.hint is not None
    for agent_id in ("claude-pro-1", "openai-team", "vincenzo-hybrid"):
        assert agent_id in exc.value.hint


# -- dump / save ------------------------------------------------------------------------------


def test_dump_load_round_trip(spec_profile: Profile, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "profile.yml"
    save_profile(spec_profile, target)
    assert load_profile(target) == spec_profile
    assert list((tmp_path / "nested" / "dir").iterdir()) == [target]


def test_dump_is_stable_and_ordered(spec_profile: Profile) -> None:
    text = dump_profile(spec_profile)
    assert text == dump_profile(spec_profile)
    keys = [line.split(":")[0] for line in text.splitlines() if not line.startswith((" ", "-"))]
    assert keys == ["github", "agents", "default_agent", "worktrees_dir"]
    assert text.index("strong: opus") < text.index("standard: sonnet") < text.index("light: haiku")
    agent_block = text.split("- id: claude-pro-1")[1]
    order = [agent_block.index(k) for k in ("tool:", "providers:", "tiers:", "effort:")]
    assert order == sorted(order)


def test_round_trip_with_worktrees_dir(tmp_path: Path) -> None:
    profile = Profile(
        github="me",
        agents=(AgentProfile("x", "ollama", ("local",)),),
        default_agent="x",
        worktrees_dir=tmp_path / "wt",
    )
    path = tmp_path / "p.yml"
    save_profile(profile, path)
    assert load_profile(path) == profile


# -- apply_setting ----------------------------------------------------------------------------


def test_apply_tier(spec_profile: Profile) -> None:
    updated = apply_setting(spec_profile, "openai-team.strong=gpt-6-astra")
    assert updated.agents[1].tiers[Tier.STRONG] == "gpt-6-astra"
    assert updated.agents[1].tiers[Tier.STANDARD] == "gpt-5"
    assert Tier.STRONG not in spec_profile.agents[1].tiers  # original unchanged


def test_apply_effort(spec_profile: Profile) -> None:
    updated = apply_setting(spec_profile, "claude-pro-1.effort.high=max")
    assert updated.agents[0].effort[Effort.HIGH] == "max"


def test_apply_default_agent(spec_profile: Profile) -> None:
    assert apply_setting(spec_profile, "default_agent=openai-team").default_agent == "openai-team"


def test_apply_with_dotted_agent_id() -> None:
    profile = Profile(
        "me", (AgentProfile("claude.pro", "claude-code", ("anthropic",)),), "claude.pro"
    )
    assert apply_setting(profile, "claude.pro.light=haiku").agents[0].tiers == {Tier.LIGHT: "haiku"}
    updated = apply_setting(profile, "claude.pro.effort.low=low")
    assert updated.agents[0].effort == {Effort.LOW: "low"}


@pytest.mark.parametrize(
    ("setting", "fragment"),
    [
        ("no-equals", "KEY=VALUE"),
        ("=x", "KEY=VALUE"),
        ("claude-pro-1.strong=", "empty"),
        ("ghost.strong=opus", "unknown agent"),
        ("claude-pro-1.huge=opus", "invalid setting key"),
        ("claude-pro-1.effort.max=max", "unknown effort level"),
        ("default_agent=ghost", "unknown agent"),
        ("strong=opus", "invalid setting key"),
    ],
)
def test_apply_setting_errors(spec_profile: Profile, setting: str, fragment: str) -> None:
    with pytest.raises(ForgeError) as exc:
        apply_setting(spec_profile, setting)
    assert fragment in exc.value.message
    assert exc.value.hint
