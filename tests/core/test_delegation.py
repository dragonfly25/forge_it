"""Tests for forge.core.delegation: the delegation decision matrix (ADR 0017)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from packaging.specifiers import SpecifierSet

from forge.core.config import (
    DelegationConfig,
    DelegationMode,
    ForgeConfig,
    GitHubConfig,
    ModelsConfig,
    ProjectConfig,
)
from forge.core.delegation import (
    IMPLEMENTER_PATH,
    SUBAGENT,
    Delegation,
    decide,
    delegation_for,
    implementer_model,
    load_implementer,
    rendered_path,
)
from forge.core.presets import load_presets
from forge.core.profile import AgentProfile, Profile
from forge.core.tasks import Size, TaskMetadata, TaskType
from forge.core.tiers import Effort, Tier

IMPLEMENTER_MD = """\
---
name: implementer
description: Implements one brief
tier: standard
effort: medium
tools: [read, edit, shell]
---
Implement exactly the brief.
"""


def run(**overrides: Any) -> Delegation:
    """decide() with a capable claude-code machine and the default policy, S task, 1 zone."""
    args: dict[str, Any] = {
        "size": Size.S,
        "zones": ("api",),
        "policy": DelegationConfig(),
        "profile_mode": None,
        "override": None,
        "tool": "claude-code",
        "template": True,
        "rendered": True,
        "model": "sonnet",
    }
    args.update(overrides)
    return decide(**args)


# --------------------------------------------------------------------------- precedence


def test_project_default_is_auto() -> None:
    d = run()
    assert (d.mode, d.requested, d.source) == ("auto", "auto", "project")
    assert d.delegate is False
    assert d.subagent == SUBAGENT == "forge-implementer"


@pytest.mark.parametrize(
    ("policy_default", "profile_mode", "override", "requested", "source"),
    [
        ("off", None, None, "off", "project"),
        ("on", None, None, "on", "project"),
        ("auto", "on", None, "on", "profile"),
        ("on", "off", None, "off", "profile"),
        ("auto", "off", "on", "on", "invocation"),
        ("off", "on", "auto", "auto", "invocation"),
        ("on", None, "off", "off", "invocation"),
    ],
)
def test_most_specific_wins(
    policy_default: DelegationMode,
    profile_mode: DelegationMode | None,
    override: DelegationMode | None,
    requested: str,
    source: str,
) -> None:
    d = run(
        policy=DelegationConfig(default=policy_default),
        profile_mode=profile_mode,
        override=override,
    )
    assert (d.requested, d.source, d.mode) == (requested, source, requested)
    assert d.warnings == ()


def test_off_reason_names_the_source() -> None:
    assert run(profile_mode="off").reason == "requested by profile"
    assert run(override="off").reason == "requested by --delegation"
    assert run(policy=DelegationConfig(default="off")).reason == "project default"


def test_on_always_delegates_with_model() -> None:
    d = run(override="on")
    assert d.delegate is True
    assert d.reason == "requested by --delegation"
    assert d.model == "sonnet"


def test_off_never_delegates_even_for_big_tasks() -> None:
    d = run(profile_mode="off", size=Size.M, zones=("a", "b", "c"))
    assert d.delegate is False
    assert d.model is None


# --------------------------------------------------------------------------- policy


def test_not_allowed_falls_back_to_project_default_with_warning() -> None:
    policy = DelegationConfig(default="off", allowed=("off", "auto"))
    d = run(policy=policy, profile_mode="on", size=Size.M)
    assert (d.mode, d.requested, d.source) == ("off", "on", "profile")
    assert d.delegate is False
    assert d.reason == "project default"
    assert len(d.warnings) == 1
    assert "'on'" in d.warnings[0]
    assert "forge profile set delegation=" in d.warnings[0]
    assert "delegation.allowed" in d.warnings[0]


def test_not_allowed_override_names_the_flag() -> None:
    policy = DelegationConfig(default="auto", allowed=("off", "auto"))
    d = run(policy=policy, override="on", size=Size.M)
    assert d.mode == "auto"
    assert d.delegate is True  # the project default auto still applies to an M task
    assert d.reason == "size M"
    assert "--delegation" in d.warnings[0]


# --------------------------------------------------------------------------- auto thresholds


@pytest.mark.parametrize(
    ("size", "zones", "delegate", "reason"),
    [
        (Size.S, ("api",), False, "1 zone, size S"),
        (Size.S, (), False, "0 zones, size S"),
        (Size.M, ("api",), True, "size M"),
        (Size.S, ("api", "web"), True, "2 zones"),
        (Size.M, ("api", "web", "db"), True, "size M, 3 zones"),
    ],
)
def test_auto_thresholds(size: Size, zones: tuple[str, ...], delegate: bool, reason: str) -> None:
    d = run(size=size, zones=zones)
    assert d.mode == "auto"
    assert d.delegate is delegate
    assert d.reason == reason
    assert d.model == ("sonnet" if delegate else None)


# --------------------------------------------------------------------------- capability


@pytest.mark.parametrize("mode", ["auto", "on"])
def test_no_profile_forces_off(mode: DelegationMode) -> None:
    d = run(tool=None, override=mode, size=Size.M)
    assert (d.mode, d.requested, d.delegate) == ("off", mode, False)
    assert d.reason == "no profile"
    assert "forge setup" in d.notes[0]


@pytest.mark.parametrize("tool", ["generic", "ollama", "aider"])
def test_tool_without_subagents_forces_off(tool: str) -> None:
    d = run(tool=tool, override="on")
    assert d.mode == "off"
    assert d.reason == f"tool {tool} has no subagents"
    assert d.notes and "claude-code, codex" in d.notes[0]


def test_missing_template_forces_off() -> None:
    d = run(template=False, override="on")
    assert d.mode == "off"
    assert d.reason == ".forge/agents/implementer.md missing"
    assert "forge/templates/project/.forge/agents/implementer.md" in d.notes[0]
    assert "forge init" not in d.notes[0]


def test_invalid_template_says_invalid() -> None:
    d = run(template=False, override="on", template_problem="bad tier")
    assert d.reason == ".forge/agents/implementer.md invalid"
    assert "bad tier" in d.notes[0]


def test_missing_rendered_file_forces_off_with_hint() -> None:
    d = run(rendered=False, override="on")
    assert d.mode == "off"
    assert d.reason == "forge-implementer not rendered"
    assert "forge adapters generate --local" in d.notes[0]


def test_codex_can_delegate() -> None:
    d = run(tool="codex", override="on", model="gpt-6.1-sol")
    assert d.delegate is True
    assert d.model == "gpt-6.1-sol"


def test_capability_not_checked_when_off_by_choice() -> None:
    d = run(tool="generic", profile_mode="off")
    assert d.reason == "requested by profile"
    assert d.notes == ()


def test_inherited_model_is_none() -> None:
    d = run(override="on", model=None)
    assert d.delegate is True
    assert d.model is None
    assert "inherits the session's model" in d.summary()


# --------------------------------------------------------------------------- output


def test_to_data_fields() -> None:
    d = run(size=Size.M)
    assert d.to_data() == {
        "mode": "auto",
        "requested": "auto",
        "source": "project",
        "delegate": True,
        "reason": "size M",
        "subagent": "forge-implementer",
        "model": "sonnet",
        "warnings": [],
        "notes": [],
    }


def test_summary_forms() -> None:
    assert run(size=Size.M).summary() == (
        "auto → delegated (size M) to `forge-implementer` (`sonnet`)"
    )
    assert run().summary() == "auto → not delegated (1 zone, size S)"
    assert run(profile_mode="off").summary() == "off (requested by profile)"
    assert run(tool="generic").summary() == "off (tool generic has no subagents)"


def test_render_lines() -> None:
    lines = run(size=Size.M).render_lines()
    assert lines[0] == "- mode: auto (requested auto by project default)"
    assert lines[1] == "- decision: delegate to `forge-implementer` (`sonnet`): size M"
    off = run(rendered=False).render_lines()
    assert off[1] == "- decision: implement in the main session: forge-implementer not rendered"
    assert off[2].startswith("- note: ")


# --------------------------------------------------------------------------- gathering


def config(min_tier_for: dict[str, Tier] | None = None, **delegation: Any) -> ForgeConfig:
    return ForgeConfig(
        forge_version=SpecifierSet(">=0.1"),
        project=ProjectConfig(name="p"),
        github=GitHubConfig(repo="acme/p"),
        models=ModelsConfig(min_tier_for=min_tier_for or {}),
        delegation=DelegationConfig(**delegation),
    )


CLAUDE = AgentProfile(
    "claude-1",
    "claude-code",
    ("anthropic",),
    {Tier.STRONG: "opus", Tier.STANDARD: "sonnet", Tier.LIGHT: "haiku"},
    {Effort.LOW: "low", Effort.MEDIUM: "medium", Effort.HIGH: "high"},
)
CODEX = AgentProfile(
    "codex-1",
    "codex",
    ("openai",),
    {Tier.STRONG: "gpt-6-astra", Tier.STANDARD: "gpt-6.1-sol"},
    {Effort.MEDIUM: "medium"},
)
GENERIC = AgentProfile("local-1", "ollama", ("local",), {Tier.STANDARD: "llama"})


def profile(*agents: AgentProfile, delegation: DelegationMode | None = None) -> Profile:
    return Profile("me", agents, agents[0].id, delegation=delegation)


def meta(size: Size = Size.M, zones: tuple[str, ...] = ("api",)) -> TaskMetadata:
    return TaskMetadata(
        id="M1-T01",
        spec="docs/spec.md",
        covers=(),
        depends_on=(),
        zones=zones,
        size=size,
        tier=Tier.STANDARD,
        type=TaskType.IMPL,
        done_when=("works",),
    )


def worktree(root: Path, *, template: bool = True, tools: tuple[str, ...] = ()) -> Path:
    if template:
        path = root / IMPLEMENTER_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(IMPLEMENTER_MD, encoding="utf-8")
    for tool in tools:
        rel = rendered_path(tool)
        assert rel is not None
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("rendered\n", encoding="utf-8")
    return root


def test_delegation_for_claude(tmp_path: Path) -> None:
    root = worktree(tmp_path, tools=("claude-code",))
    d = delegation_for(
        meta(), config=config(), profile=profile(CLAUDE), agent=CLAUDE, worktree=root
    )
    assert d.delegate is True
    assert d.model == "sonnet"


def test_delegation_for_min_tier_for_implementer(tmp_path: Path) -> None:
    root = worktree(tmp_path, tools=("claude-code",))
    d = delegation_for(
        meta(),
        config=config({"implementer": Tier.STRONG}),
        profile=profile(CLAUDE),
        agent=CLAUDE,
        worktree=root,
        presets=load_presets(),
    )
    assert d.model == "opus"


def test_delegation_for_codex(tmp_path: Path) -> None:
    root = worktree(tmp_path, tools=("codex",))
    d = delegation_for(meta(), config=config(), profile=profile(CODEX), agent=CODEX, worktree=root)
    assert (d.delegate, d.model) == (True, "gpt-6.1-sol")


def test_delegation_for_profile_mode_and_override(tmp_path: Path) -> None:
    root = worktree(tmp_path, tools=("claude-code",))
    prof = profile(CLAUDE, delegation="off")
    d = delegation_for(meta(), config=config(), profile=prof, agent=CLAUDE, worktree=root)
    assert (d.mode, d.source) == ("off", "profile")
    d = delegation_for(
        meta(Size.S), config=config(), profile=prof, agent=CLAUDE, worktree=root, override="on"
    )
    assert (d.mode, d.source, d.delegate) == ("on", "invocation", True)


def test_delegation_for_project_policy(tmp_path: Path) -> None:
    root = worktree(tmp_path, tools=("claude-code",))
    cfg = config(default="off", allowed=("off",))
    d = delegation_for(
        meta(), config=cfg, profile=profile(CLAUDE), agent=CLAUDE, worktree=root, override="on"
    )
    assert d.mode == "off"
    assert d.warnings


def test_delegation_for_without_profile(tmp_path: Path) -> None:
    root = worktree(tmp_path, tools=("claude-code",))
    d = delegation_for(meta(), config=config(), profile=None, agent=None, worktree=root)
    assert (d.mode, d.reason) == ("off", "no profile")


def test_delegation_for_generic_tool(tmp_path: Path) -> None:
    root = worktree(tmp_path)
    d = delegation_for(
        meta(), config=config(), profile=profile(GENERIC), agent=GENERIC, worktree=root
    )
    assert d.reason == "tool ollama has no subagents"


def test_delegation_for_missing_rendered_file(tmp_path: Path) -> None:
    root = worktree(tmp_path, tools=("codex",))  # codex rendered, claude not
    d = delegation_for(
        meta(), config=config(), profile=profile(CLAUDE, CODEX), agent=CLAUDE, worktree=root
    )
    assert d.reason == "forge-implementer not rendered"
    assert "forge adapters generate --local" in d.notes[0]


def test_delegation_for_missing_template(tmp_path: Path) -> None:
    root = worktree(tmp_path, template=False, tools=("claude-code",))
    d = delegation_for(
        meta(), config=config(), profile=profile(CLAUDE), agent=CLAUDE, worktree=root
    )
    assert d.reason == ".forge/agents/implementer.md missing"


def test_delegation_for_invalid_template_is_a_note(tmp_path: Path) -> None:
    root = worktree(tmp_path, tools=("claude-code",))
    (root / IMPLEMENTER_PATH).write_text("---\nname: implementer\n---\nbody\n", encoding="utf-8")
    definition, problem = load_implementer(root)
    assert definition is None and problem
    d = delegation_for(
        meta(), config=config(), profile=profile(CLAUDE), agent=CLAUDE, worktree=root
    )
    assert d.mode == "off"
    assert d.reason == ".forge/agents/implementer.md invalid"
    assert problem in d.notes[0]


def test_load_implementer_absent(tmp_path: Path) -> None:
    assert load_implementer(tmp_path) == (None, None)


def test_implementer_model_uses_the_rendering_agent(tmp_path: Path) -> None:
    definition, _ = load_implementer(worktree(tmp_path))
    assert definition is not None
    other = AgentProfile("claude-2", "claude-code", ("anthropic",), {Tier.STANDARD: "fable"})
    prof = Profile("me", (other, CLAUDE), "claude-1")
    # the default agent renders claude-code's subagents
    assert implementer_model(definition, prof, "claude-code", config(), None) == "sonnet"
    # no agent for the tool
    assert implementer_model(definition, prof, "codex", config(), None) is None


def test_implementer_model_non_claude_model_inherits(tmp_path: Path) -> None:
    definition, _ = load_implementer(worktree(tmp_path))
    assert definition is not None
    hybrid = AgentProfile("h", "claude-code", ("local",), {Tier.STANDARD: "llama-local"})
    assert implementer_model(definition, profile(hybrid), "claude-code", config(), None) is None


def test_implementer_model_unserved_tier_inherits(tmp_path: Path) -> None:
    definition, _ = load_implementer(worktree(tmp_path))
    assert definition is not None
    light = AgentProfile("l", "claude-code", ("anthropic",), {Tier.LIGHT: "haiku"})
    assert implementer_model(definition, profile(light), "claude-code", config(), None) is None
