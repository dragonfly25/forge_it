"""Tests for forge.core.models (model resolution and constraint checks, ADR 0005)."""

from __future__ import annotations

import pytest

from forge.core.config import ModelsConfig
from forge.core.models import (
    Drift,
    Note,
    can_serve,
    check_profile,
    model_map,
    preset_drift,
    resolve,
    same_alias,
    serving_tier,
)
from forge.core.output import Level
from forge.core.presets import Preset, load_presets
from forge.core.profile import AgentProfile, Profile
from forge.core.tiers import Effort, Tier

S, M, L = Tier.STRONG, Tier.STANDARD, Tier.LIGHT
EFFORT = {Effort.LOW: "low", Effort.MEDIUM: "medium", Effort.HIGH: "high"}

FULL = AgentProfile(
    "full", "claude-code", ("anthropic",), {S: "opus", M: "sonnet", L: "haiku"}, EFFORT
)
ONLY_STRONG = AgentProfile("only-strong", "claude-code", ("anthropic",), {S: "opus"}, EFFORT)
ONLY_LIGHT = AgentProfile("only-light", "claude-code", ("anthropic",), {L: "haiku"}, EFFORT)
NO_TIERS = AgentProfile("empty", "claude-code", ("anthropic",))
OLLAMA = AgentProfile("local", "ollama", ("local",), {M: "llama3"}, EFFORT)
# The spec §6.4 agents.
OPENAI_TEAM = AgentProfile("openai-team", "codex", ("openai",), {M: "gpt-5"})
HYBRID = AgentProfile(
    "vincenzo-hybrid",
    "claude-code",
    ("anthropic", "local"),
    {S: "opus", M: "sonnet", L: "llama-local"},
)
CONSTRAINTS = ModelsConfig(
    recommended={S: "opus", M: "sonnet"},
    min_tier_for={"reviewer": S, "plan": S},
    allowed_providers=("anthropic", "openai", "local"),
)


@pytest.fixture(scope="module")
def presets() -> dict[str, Preset]:
    return load_presets()


def profile_of(*agents: AgentProfile) -> Profile:
    return Profile("me", agents, agents[0].id)


# -- serving ----------------------------------------------------------------------------------


def test_serving_tier() -> None:
    assert serving_tier(FULL, M) == M
    assert serving_tier(ONLY_STRONG, L) == S
    assert serving_tier(ONLY_LIGHT, S) is None
    assert can_serve(ONLY_STRONG, M)
    assert not can_serve(ONLY_LIGHT, M)
    assert not can_serve(NO_TIERS, L)


def test_model_map_strongest_first() -> None:
    assert list(model_map(FULL).items()) == [
        ("strong", "opus"),
        ("standard", "sonnet"),
        ("light", "haiku"),
    ]
    assert model_map(OPENAI_TEAM) == {"standard": "gpt-5"}


# -- resolve: precedence matrix ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "tier", "model"),
    [
        ({"declared_tier": "task", "task_tier": L}, L, "haiku"),
        ({"declared_tier": "task", "task_tier": S}, S, "opus"),
        ({"declared_tier": "task"}, M, "sonnet"),
        ({"declared_tier": L}, L, "haiku"),
        ({"declared_tier": L, "roles": ("reviewer",), "constraints": CONSTRAINTS}, S, "opus"),
        ({"declared_tier": L, "roles": ("explorer",), "constraints": CONSTRAINTS}, L, "haiku"),
        ({"declared_tier": L, "roles": ("reviewer",)}, L, "haiku"),  # no constraints given
        ({"declared_tier": L, "override_tier": M}, M, "sonnet"),
        ({"declared_tier": S, "override_tier": L}, S, "opus"),
        ({"declared_tier": "task", "task_tier": L, "override_tier": S}, S, "opus"),
    ],
)
def test_resolve_tier_precedence(kwargs: dict[str, object], tier: Tier, model: str) -> None:
    res = resolve(FULL, **kwargs)  # type: ignore[arg-type]
    assert res.tier == tier
    assert res.effective_tier == tier
    assert res.model == model
    assert res.served


def test_task_without_task_notes_standard() -> None:
    res = resolve(FULL, declared_tier="task")
    assert res.notes == (Note(Level.INFO, "tier `task` without a task: using standard"),)


def test_min_tier_for_note() -> None:
    res = resolve(FULL, declared_tier=L, roles=("plan",), constraints=CONSTRAINTS)
    assert res.tier == S
    assert res.notes == (
        Note(Level.INFO, "tier raised from light to strong by models.min_tier_for.plan"),
    )


@pytest.mark.parametrize("roles", [("explorer", "reviewer"), ("reviewer", "explorer")])
def test_min_tier_for_highest_of_several_roles(roles: tuple[str, str]) -> None:
    constraints = ModelsConfig(min_tier_for={"explorer": M, "reviewer": S})
    res = resolve(FULL, declared_tier=L, roles=roles, constraints=constraints)
    assert res.tier == S
    assert [n.text for n in res.notes] == [
        "tier raised from light to strong by models.min_tier_for.reviewer"
    ]


def test_min_tier_for_role_and_command() -> None:
    constraints = ModelsConfig(min_tier_for={"implementer": M, "plan": S})
    res = resolve(FULL, declared_tier=L, roles=("implementer", "plan"), constraints=constraints)
    assert res.tier == S
    assert "min_tier_for.plan" in res.notes[0].text
    res = resolve(FULL, declared_tier=L, roles=("implementer", "next"), constraints=constraints)
    assert res.tier == M
    assert "min_tier_for.implementer" in res.notes[0].text


def test_min_tier_for_unknown_roles_and_no_constraints() -> None:
    res = resolve(FULL, declared_tier=L, roles=("other",), constraints=CONSTRAINTS)
    assert res.tier == L and res.notes == ()
    res = resolve(FULL, declared_tier=L, roles=("plan",))
    assert res.tier == L and res.notes == ()


def test_min_tier_for_never_lowers() -> None:
    constraints = ModelsConfig(min_tier_for={"explorer": L})
    res = resolve(FULL, declared_tier=S, roles=("explorer",), constraints=constraints)
    assert res.tier == S
    assert res.notes == ()


def test_lower_override_is_ignored_with_note() -> None:
    res = resolve(
        FULL,
        declared_tier=S,
        override_tier=L,
        declared_effort=Effort.HIGH,
        override_effort=Effort.LOW,
    )
    assert res.tier == S
    assert res.effort == Effort.HIGH
    assert all(n.level == Level.WARNING for n in res.notes)
    assert any("--tier light ignored" in n.text for n in res.notes)
    assert any("--effort low ignored" in n.text for n in res.notes)


def test_override_after_min_tier_for_only_raises() -> None:
    res = resolve(
        FULL, declared_tier=L, roles=("reviewer",), constraints=CONSTRAINTS, override_tier=M
    )
    assert res.tier == S
    assert any(n.level == Level.WARNING and "--tier standard ignored" in n.text for n in res.notes)


def test_equal_override_is_silent() -> None:
    res = resolve(FULL, declared_tier=M, override_tier=M, override_effort=Effort.MEDIUM)
    assert res.notes == ()


def test_effort_raised_by_override() -> None:
    res = resolve(FULL, declared_tier=M, declared_effort=Effort.LOW, override_effort=Effort.HIGH)
    assert res.effort == Effort.HIGH
    assert res.effort_value == "high"
    assert any(n.level == Level.INFO and "raised" in n.text for n in res.notes)


def test_only_strong_serves_standard() -> None:
    res = resolve(ONLY_STRONG, declared_tier=M)
    assert res.served
    assert res.tier == M
    assert res.effective_tier == S
    assert res.model == "opus"
    assert res.notes == (
        Note(Level.INFO, "agent only-strong has no standard model: using its strong model"),
    )


def test_only_light_cannot_serve_strong() -> None:
    res = resolve(ONLY_LIGHT, declared_tier=S)
    assert not res.served
    assert res.effective_tier is None
    assert res.model is None
    assert any(n.level == Level.WARNING and "cannot serve" in n.text for n in res.notes)


def test_any_tier_uses_highest_mapped_with_warning() -> None:
    agent = AgentProfile("lm", "claude-code", ("anthropic",), {L: "haiku", M: "sonnet"}, EFFORT)
    res = resolve(agent, declared_tier=S, any_tier=True)
    assert not res.served
    assert res.effective_tier == M
    assert res.model == "sonnet"
    assert any(n.level == Level.WARNING and "--any-tier" in n.text for n in res.notes)
    assert not any(n.text.startswith("warning:") for n in res.notes)


def test_any_tier_without_tiers() -> None:
    res = resolve(NO_TIERS, declared_tier=L, any_tier=True)
    assert res.model is None and not res.served


def test_any_tier_has_no_effect_when_served() -> None:
    assert resolve(FULL, declared_tier=M, any_tier=True) == resolve(FULL, declared_tier=M)


def test_effort_mapped() -> None:
    res = resolve(FULL, declared_tier=M, declared_effort=Effort.HIGH)
    assert res.effort == Effort.HIGH
    assert res.effort_value == "high"


def test_effort_ignored_when_tool_does_not_support_it(presets: dict[str, Preset]) -> None:
    res = resolve(OLLAMA, declared_tier=M, declared_effort=Effort.HIGH, presets=presets)
    assert res.model == "llama3"
    assert res.effort == Effort.HIGH
    assert res.effort_value is None
    assert any(n.level == Level.WARNING and "does not support" in n.text for n in res.notes)


def test_effort_ignored_when_mapping_missing(presets: dict[str, Preset]) -> None:
    res = resolve(OPENAI_TEAM, declared_tier=M, presets=presets)
    assert res.model == "gpt-5"
    assert res.effort_value is None
    assert any(n.level == Level.WARNING and "no mapping for medium" in n.text for n in res.notes)


# -- check_profile ----------------------------------------------------------------------------


def test_spec_profile_has_no_violations(presets: dict[str, Preset]) -> None:
    full = AgentProfile(
        "claude-pro-1", "claude-code", ("anthropic",), {S: "opus", M: "sonnet", L: "haiku"}, EFFORT
    )
    constraints = ModelsConfig(
        recommended=CONSTRAINTS.recommended, allowed_providers=CONSTRAINTS.allowed_providers
    )
    assert check_profile(profile_of(full, OPENAI_TEAM, HYBRID), constraints, presets) == []


def test_disallowed_provider(presets: dict[str, Preset]) -> None:
    agent = AgentProfile("g", "gemini", ("google",), {S: "opus"})
    violations = check_profile(
        profile_of(agent), ModelsConfig(allowed_providers=("anthropic",)), presets
    )
    assert [(v.level, v.agent_id) for v in violations] == [("warning", "g")]
    assert "google" in violations[0].message
    assert violations[0].fix


def test_no_provider_restriction(presets: dict[str, Preset]) -> None:
    agent = AgentProfile("g", "gemini", ("google",), {S: "opus"})
    assert check_profile(profile_of(agent), ModelsConfig(), presets) == []


def test_unservable_min_tier_for_role(presets: dict[str, Preset]) -> None:
    violations = check_profile(
        profile_of(OPENAI_TEAM), ModelsConfig(min_tier_for={"reviewer": S}), presets
    )
    assert len(violations) == 1
    assert violations[0].level is Level.WARNING
    assert "'reviewer' (models.min_tier_for)" in violations[0].message
    assert "openai-team.strong" in violations[0].fix


def test_unverified_strong_for_hybrid_agent(presets: dict[str, Preset]) -> None:
    hybrid = AgentProfile(
        "vincenzo-hybrid", "claude-code", ("anthropic", "local"), {S: "llama-local", M: "sonnet"}
    )
    violations = check_profile(profile_of(hybrid), ModelsConfig(), presets)
    assert len(violations) == 1
    assert violations[0].level == "warning"
    assert "llama-local" in violations[0].message


def test_strong_from_other_hosted_preset_is_verified(presets: dict[str, Preset]) -> None:
    strong = presets["codex"].tiers[S]
    agent = AgentProfile("x", "claude-code", ("anthropic", "openai"), {S: strong})
    assert check_profile(profile_of(agent), ModelsConfig(), presets) == []


def test_recommended_mismatch_is_info(presets: dict[str, Preset]) -> None:
    agent = AgentProfile("c", "claude-code", ("anthropic",), {S: "claude-opus-5-5", M: "haiku"})
    violations = check_profile(
        profile_of(agent), ModelsConfig(recommended={S: "opus", M: "sonnet"}), presets
    )
    assert [(v.level, v.agent_id) for v in violations] == [("info", "c")]
    assert "sonnet" in violations[0].message


def test_recommendation_for_other_tool_is_ignored(presets: dict[str, Preset]) -> None:
    violations = check_profile(
        profile_of(OPENAI_TEAM), ModelsConfig(recommended={M: "sonnet"}), presets
    )
    assert violations == []


def test_same_alias() -> None:
    assert same_alias("opus", "claude-opus-5-5")
    assert same_alias("claude-opus-5-5", "opus")
    assert not same_alias("opus", "claude-sonnet-5-5")


# -- preset_drift -----------------------------------------------------------------------------


def test_preset_drift(presets: dict[str, Preset]) -> None:
    drifts = preset_drift(profile_of(FULL, OPENAI_TEAM, HYBRID, OLLAMA), presets)
    codex_standard = presets["codex"].tiers[M]
    assert drifts == [
        Drift("openai-team", M, "gpt-5", codex_standard),
        Drift("vincenzo-hybrid", L, "llama-local", "haiku"),
    ]


def test_no_drift_for_unknown_tool(presets: dict[str, Preset]) -> None:
    agent = AgentProfile("g", "gemini", ("google",), {S: "x"})
    assert preset_drift(profile_of(agent), presets) == []


def test_roles_as_single_string_is_rejected() -> None:
    with pytest.raises(TypeError):
        resolve(FULL, declared_tier=M, roles="reviewer", constraints=CONSTRAINTS)
