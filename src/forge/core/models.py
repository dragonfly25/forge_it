"""Model resolution, tier serving and profile constraint checks (spec §6.5, ADR 0005).

Everything here is pure and deterministic: the CLI never calls an LLM, it only resolves the
abstract ``tier``/``effort`` of a task or subagent to the concrete model of the active agent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from forge.core.config import ModelsConfig
from forge.core.output import Level
from forge.core.presets import Preset
from forge.core.profile import AgentProfile, Profile
from forge.core.tiers import TASK_TIER, TIERS, Effort, Tier


def serving_tier(agent: AgentProfile, tier: Tier) -> Tier | None:
    """Return the lowest tier >= ``tier`` mapped in ``agent``, or ``None`` if there is none."""
    for candidate in TIERS:
        if candidate.rank >= tier.rank and candidate in agent.tiers:
            return candidate
    return None


def can_serve(agent: AgentProfile, tier: Tier) -> bool:
    """True if ``agent`` maps ``tier`` or a stronger tier."""
    return serving_tier(agent, tier) is not None


def model_map(agent: AgentProfile) -> dict[str, str]:
    """Return the agent's tier → model map, strongest first (for PR bodies)."""
    return {str(t): agent.tiers[t] for t in reversed(TIERS) if t in agent.tiers}


@dataclass(frozen=True)
class Note:
    """One explanation of a resolution step.

    ``info`` marks normal adjustments; ``warning`` marks a request that was ignored or could
    not be honoured as asked.
    """

    level: Level
    text: str


@dataclass(frozen=True)
class Resolution:
    """Result of resolving a tier/effort for one agent.

    ``tier`` is the required tier; ``effective_tier`` is the tier whose model is used
    (``None`` when no model can be used). ``served`` is False when the agent maps no tier
    >= ``tier`` (with ``any_tier`` a model may still be chosen).
    """

    tier: Tier
    effective_tier: Tier | None
    model: str | None
    effort: Effort
    effort_value: str | None
    served: bool
    notes: tuple[Note, ...] = ()


def resolve(
    agent: AgentProfile,
    *,
    declared_tier: Tier | Literal["task"],
    declared_effort: Effort = Effort.MEDIUM,
    task_tier: Tier | None = None,
    roles: Sequence[str] = (),
    constraints: ModelsConfig | None = None,
    override_tier: Tier | None = None,
    override_effort: Effort | None = None,
    presets: Mapping[str, Preset] | None = None,
    any_tier: bool = False,
) -> Resolution:
    """Resolve the concrete model and effort value for ``agent`` (ADR 0005, steps 1-6).

    ``presets`` is used only to know whether the tool supports effort; when ``None`` or the
    tool has no preset, effort support is assumed and only the agent's mapping is consulted.
    ``roles`` are the role and/or command names looked up in ``models.min_tier_for`` (e.g.
    ``("reviewer", "plan")``); the highest matching minimum applies.
    """
    notes: list[Note] = []

    def info(text: str) -> None:
        notes.append(Note(Level.INFO, text))

    def warn(text: str) -> None:
        notes.append(Note(Level.WARNING, text))

    # 1. Base tier.
    if declared_tier == TASK_TIER:
        if task_tier is None:
            tier = Tier.STANDARD
            info("tier `task` without a task: using standard")
        else:
            tier = task_tier
    else:
        tier = Tier(declared_tier)

    # 2. Project minimum for the roles or command: the highest matching minimum wins.
    if isinstance(roles, str):
        raise TypeError("roles must be a sequence of names, not a single string")
    if constraints is not None:
        base = tier
        raised_by: str | None = None
        for key in roles:
            minimum = constraints.min_tier_for.get(key)
            if minimum is not None and minimum.rank > tier.rank:
                tier, raised_by = minimum, key
        if raised_by is not None:
            info(f"tier raised from {base} to {tier} by models.min_tier_for.{raised_by}")

    # 3. Manual overrides may only raise.
    if override_tier is not None:
        if override_tier.rank > tier.rank:
            info(f"tier raised from {tier} to {override_tier} by --tier")
            tier = override_tier
        elif override_tier.rank < tier.rank:
            warn(f"--tier {override_tier} ignored: overrides may only raise (tier {tier})")
    effort = declared_effort
    if override_effort is not None:
        if override_effort.rank > effort.rank:
            info(f"effort raised from {effort} to {override_effort} by --effort")
            effort = override_effort
        elif override_effort.rank < effort.rank:
            warn(f"--effort {override_effort} ignored: overrides may only raise (effort {effort})")

    # 4. Serving.
    effective = serving_tier(agent, tier)
    served = effective is not None
    if effective is not None and effective != tier:
        info(f"agent {agent.id} has no {tier} model: using its {effective} model")
    if effective is None:
        if any_tier and agent.tiers:
            effective = max(agent.tiers, key=lambda t: t.rank)
            warn(
                f"agent {agent.id} cannot serve tier {tier}; "
                f"--any-tier uses its highest tier ({effective})"
            )
        else:
            warn(f"agent {agent.id} cannot serve tier {tier}")
    model = agent.tiers[effective] if effective is not None else None

    # 5. Effort.
    preset = presets.get(agent.tool) if presets is not None else None
    effort_value: str | None
    if preset is not None and not preset.supports_effort:
        effort_value = None
        warn(f"effort ignored: tool {agent.tool} does not support it")
    elif effort not in agent.effort:
        effort_value = None
        warn(f"effort ignored: agent {agent.id} has no mapping for {effort}")
    else:
        effort_value = agent.effort[effort]

    # 6. Result.
    return Resolution(
        tier=tier,
        effective_tier=effective,
        model=model,
        effort=effort,
        effort_value=effort_value,
        served=served,
        notes=tuple(notes),
    )


# -- constraint checks ------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """A profile problem relative to project constraints. Never changes anything."""

    level: Level
    agent_id: str
    message: str
    fix: str


def same_alias(a: str, b: str) -> bool:
    """True if two model names refer to the same alias.

    Names match when equal, or when one is a hyphen-separated token of the other
    (``opus`` matches ``claude-opus-5-5``).
    """
    if a == b:
        return True
    return a in b.split("-") or b in a.split("-")


def _verified_strong(presets: Mapping[str, Preset]) -> set[str]:
    known: set[str] = set()
    for preset in presets.values():
        if preset.provider != "local":
            known.update(preset.known_models.get(Tier.STRONG, ()))
    return known


def _tools_knowing(model: str, presets: Mapping[str, Preset]) -> set[str]:
    return {
        p.tool
        for p in presets.values()
        if any(model in models for models in p.known_models.values())
    }


def check_profile(
    profile: Profile, constraints: ModelsConfig, presets: Mapping[str, Preset]
) -> list[Violation]:
    """Return the profile's violations of ``config.models`` (ADR 0005), in agent order."""
    violations: list[Violation] = []
    verified = _verified_strong(presets)
    for agent in profile.agents:
        if constraints.allowed_providers is not None:
            for provider in agent.providers:
                if provider not in constraints.allowed_providers:
                    violations.append(
                        Violation(
                            Level.WARNING,
                            agent.id,
                            f"provider {provider!r} is not in models.allowed_providers "
                            f"({', '.join(constraints.allowed_providers)})",
                            f"remove {provider!r} from the agent's providers, or ask the lead "
                            "to allow it in .forge/config.yml",
                        )
                    )
        # One check per role or command key; resolve() applies the highest matching minimum.
        for key, minimum in constraints.min_tier_for.items():
            if not can_serve(agent, minimum):
                violations.append(
                    Violation(
                        Level.WARNING,
                        agent.id,
                        f"cannot serve {key!r} (models.min_tier_for), which requires tier "
                        f"{minimum} or higher",
                        f"map a model for tier {minimum}: "
                        f"`forge profile set {agent.id}.{minimum}=<model>`",
                    )
                )
        strong = agent.tiers.get(Tier.STRONG)
        if strong is not None and strong not in verified:
            violations.append(
                Violation(
                    Level.WARNING,
                    agent.id,
                    f"strong model {strong!r} is not a known strong model of any hosted preset",
                    "map `strong` to a hosted strong model (e.g. "
                    f"`forge profile set {agent.id}.strong=opus`), or remove the strong tier",
                )
            )
        for tier, recommended in constraints.recommended.items():
            current = agent.tiers.get(tier)
            if current is None or agent.tool not in _tools_knowing(recommended, presets):
                continue
            if not same_alias(current, recommended):
                violations.append(
                    Violation(
                        Level.INFO,
                        agent.id,
                        f"{tier} uses {current!r}; the project recommends {recommended!r}",
                        f"`forge profile set {agent.id}.{tier}={recommended}` to follow it",
                    )
                )
    return violations


@dataclass(frozen=True)
class Drift:
    """A mapped tier whose model differs from the current preset for the agent's tool."""

    agent_id: str
    tier: Tier
    current: str
    preset: str


def preset_drift(profile: Profile, presets: Mapping[str, Preset]) -> list[Drift]:
    """List mapped tiers whose model differs from the agent's tool preset.

    Tiers the agent does not map, and tools without a preset, are not reported.
    """
    drifts: list[Drift] = []
    for agent in profile.agents:
        preset = presets.get(agent.tool)
        if preset is None:
            continue
        for tier in reversed(TIERS):
            current = agent.tiers.get(tier)
            proposed = preset.tiers.get(tier)
            if current is not None and proposed is not None and current != proposed:
                drifts.append(Drift(agent.id, tier, current, proposed))
    return drifts


__all__ = [
    "Drift",
    "Note",
    "Resolution",
    "Violation",
    "can_serve",
    "check_profile",
    "model_map",
    "preset_drift",
    "resolve",
    "same_alias",
    "serving_tier",
]
