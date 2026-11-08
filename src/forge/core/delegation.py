"""Implementation delegation: does the main session delegate the code to ``forge-implementer``?

ADR 0017. :func:`decide` is a pure function of the task (size, zones), the project policy
(``config.delegation``), the personal mode (profile), the per-invocation override
(``--delegation``) and what the machine offers (the active agent's tool, the implementer
template, the rendered ``forge-implementer`` file and its resolved model).

Rules, in order:

1. **Most specific wins**: invocation, then profile, then the project default.
2. **Policy**: a requested mode that ``delegation.allowed`` does not list falls back to the
   project default, with a warning naming the setting to change.
3. **Capability** (only for ``auto`` and ``on``): the mode is forced to ``off``, with an info
   note, without a profile, for a tool without native subagents (not ``claude-code`` or
   ``codex``), without ``.forge/agents/implementer.md``, or when ``forge-implementer`` is not
   rendered for the tool (hint: ``forge adapters generate --local``).
4. **Decision**: ``off`` never delegates, ``on`` always does, ``auto`` delegates when the task
   has ``size: M`` or two or more zones.

:func:`delegation_for` gathers those inputs from the files of a worktree and calls
:func:`decide`. The CLI never calls a model: this is the decision, not what the agent does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from forge.core.adapters import agent_for_tool, claude, codex
from forge.core.adapters._common import resolve_subagent
from forge.core.config import DelegationConfig, DelegationMode, ForgeConfig
from forge.core.errors import ForgeError
from forge.core.presets import Preset
from forge.core.profile import AgentProfile, Profile
from forge.core.prompts import AGENTS_DIR, AgentDef, parse_agent, read_text
from forge.core.tasks import Size, TaskMetadata

Source = Literal["invocation", "profile", "project"]

SUBAGENT = "forge-implementer"
"""Name of the rendered implementer subagent."""

IMPLEMENTER = "implementer"
"""Name of the implementer definition in `.forge/agents/`."""

IMPLEMENTER_PATH = AGENTS_DIR / f"{IMPLEMENTER}.md"
"""The implementer template, relative to the repository root."""

SUBAGENT_TOOLS: tuple[str, ...] = (claude.TOOL, codex.TOOL)
"""Agent tools with native subagents, so the only ones that can delegate."""

AUTO_MIN_ZONES = 2
"""``auto`` delegates a task with at least this many zones (or with ``size: M``)."""

GENERATE_LOCAL = "forge adapters generate --local"

TEMPLATE_HINT = f"forge/templates/project/{IMPLEMENTER_PATH.as_posix()}"
"""Where the implementer template lives inside the installed ``forge`` package."""

_SOURCE_LABEL: dict[Source, str] = {
    "invocation": "--delegation",
    "profile": "profile",
    "project": "project default",
}


@dataclass(frozen=True)
class Delegation:
    """The delegation decision for one task (ADR 0017).

    ``mode`` is the effective mode, ``requested`` the mode asked for and ``source`` where it
    was asked (``invocation``, ``profile`` or ``project``). ``delegate`` is the decision for
    this task and ``reason`` explains it. ``model`` is the implementer's model as the adapters
    render it (``None`` when it is not delegated, or when the rendered subagent inherits the
    main session's model). ``warnings`` and ``notes`` explain fallbacks.
    """

    mode: DelegationMode
    requested: DelegationMode
    source: Source
    delegate: bool
    reason: str
    subagent: str = SUBAGENT
    model: str | None = None
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        """JSON form (the ``delegation`` object of the task context)."""
        return {
            "mode": self.mode,
            "requested": self.requested,
            "source": self.source,
            "delegate": self.delegate,
            "reason": self.reason,
            "subagent": self.subagent,
            "model": self.model,
            "warnings": list(self.warnings),
            "notes": list(self.notes),
        }

    def summary(self) -> str:
        """One line: ``auto → delegated (size M) to `forge-implementer` (`sonnet`)``.

        Used by the PR body (after ``Delegation: ``) and ``forge status``.
        """
        if self.delegate:
            model = f"`{self.model}`" if self.model else "inherits the session's model"
            return f"{self.mode} → delegated ({self.reason}) to `{self.subagent}` ({model})"
        if self.mode == "auto":
            return f"auto → not delegated ({self.reason})"
        return f"{self.mode} ({self.reason})"

    def render_lines(self) -> list[str]:
        """The lines of the ``## Delegation`` block of the human task context."""
        asked = f"requested {self.requested} by {_SOURCE_LABEL[self.source]}"
        lines = [f"- mode: {self.mode} ({asked})"]
        if self.delegate:
            model = f"`{self.model}`" if self.model else "inherits the session's model"
            lines.append(f"- decision: delegate to `{self.subagent}` ({model}): {self.reason}")
        else:
            lines.append(f"- decision: implement in the main session: {self.reason}")
        lines += [f"- warning: {w}" for w in self.warnings]
        lines += [f"- note: {n}" for n in self.notes]
        return lines


def _requested(
    policy: DelegationConfig,
    profile_mode: DelegationMode | None,
    override: DelegationMode | None,
) -> tuple[DelegationMode, Source]:
    if override is not None:
        return override, "invocation"
    if profile_mode is not None:
        return profile_mode, "profile"
    return policy.default, "project"


def _setting(source: Source) -> str:
    if source == "invocation":
        return "pass another `--delegation` value"
    return "change `delegation` in your profile (`forge profile set delegation=<mode>`)"


def _auto_reason(size: Size, zone_count: int) -> tuple[bool, str]:
    big = size is Size.M
    many = zone_count >= AUTO_MIN_ZONES
    zones = f"{zone_count} zone" + ("" if zone_count == 1 else "s")
    if big and many:
        return True, f"size M, {zones}"
    if big:
        return True, "size M"
    if many:
        return True, zones
    return False, f"{zones}, size {size.value}"


def decide(
    *,
    size: Size,
    zones: Sequence[str],
    policy: DelegationConfig,
    profile_mode: DelegationMode | None,
    override: DelegationMode | None,
    tool: str | None,
    template: bool,
    rendered: bool,
    model: str | None,
    template_problem: str | None = None,
) -> Delegation:
    """Decide the delegation mode and whether this task is delegated (ADR 0017). Pure.

    ``tool`` is the active agent's tool, ``None`` when there is no profile. ``template`` says
    whether `.forge/agents/implementer.md` exists and is valid (``template_problem`` says why
    not), ``rendered`` whether the machine-level ``forge-implementer`` file exists for
    ``tool``, and ``model`` is its resolved model.
    """
    requested, source = _requested(policy, profile_mode, override)
    warnings: list[str] = []
    notes: list[str] = []
    mode = requested
    reason_source: Source = source
    if requested not in policy.allowed:
        mode = policy.default
        reason_source = "project"
        warnings.append(
            f"delegation mode {requested!r} (from {_SOURCE_LABEL[source]}) is not allowed by "
            f"the project ({', '.join(policy.allowed)}); using the project default {mode!r}. "
            f"To change it, {_setting(source)}, or ask the lead to add {requested!r} to "
            "`delegation.allowed` in .forge/config.yml"
        )

    def result(effective: DelegationMode, delegate: bool, reason: str) -> Delegation:
        return Delegation(
            mode=effective,
            requested=requested,
            source=source,
            delegate=delegate,
            reason=reason,
            model=model if delegate else None,
            warnings=tuple(warnings),
            notes=tuple(notes),
        )

    if mode == "off":
        reason = (
            "project default"
            if reason_source == "project"
            else (f"requested by {_SOURCE_LABEL[reason_source]}")
        )
        return result("off", False, reason)

    # Capability: delegation needs native subagents and the rendered implementer.
    blocked: str | None = None
    if tool is None:
        blocked = "no profile"
        notes.append(f"delegation {mode} needs a Forge profile: run `forge setup`; using off")
    elif tool not in SUBAGENT_TOOLS:
        blocked = f"tool {tool} has no subagents"
        notes.append(
            f"delegation {mode} needs native subagents ({', '.join(SUBAGENT_TOOLS)}); the "
            f"active agent uses {tool}, so the main session writes the code (off)"
        )
    elif not template:
        state = "invalid" if template_problem else "missing"
        blocked = f"{IMPLEMENTER_PATH.as_posix()} {state}"
        detail = f" ({template_problem})" if template_problem else ""
        notes.append(
            f"delegation {mode} needs a valid {IMPLEMENTER_PATH.as_posix()}{detail}; copy "
            f"{TEMPLATE_HINT} from the installed Forge package; using off"
        )
    elif not rendered:
        blocked = f"{SUBAGENT} not rendered"
        notes.append(
            f"delegation {mode} needs the machine-level {SUBAGENT} subagent for {tool}: run "
            f"`{GENERATE_LOCAL}`; using off"
        )
    if blocked is not None:
        return result("off", False, blocked)

    if mode == "on":
        reason = (
            "project default"
            if reason_source == "project"
            else (f"requested by {_SOURCE_LABEL[reason_source]}")
        )
        return result("on", True, reason)
    delegate, reason = _auto_reason(size, len(zones))
    return result("auto", delegate, reason)


# --------------------------------------------------------------------------- gathering


def load_implementer(repo_root: Path) -> tuple[AgentDef | None, str | None]:
    """Load `.forge/agents/implementer.md` under ``repo_root``.

    Returns ``(definition, None)``, ``(None, None)`` when the file is absent, or
    ``(None, problem)`` when it is invalid. Never raises.
    """
    path = repo_root / IMPLEMENTER_PATH
    if not path.is_file():
        return None, None
    try:
        return parse_agent(read_text(path), IMPLEMENTER_PATH), None
    except ForgeError as exc:
        return None, exc.message


def implementer_model(
    definition: AgentDef,
    profile: Profile,
    tool: str,
    config: ForgeConfig,
    presets: Mapping[str, Preset] | None,
) -> str | None:
    """The model the adapters render for ``forge-implementer`` for ``tool`` (ADR 0014).

    The agent is the one whose models render the tool's subagents (the default agent if it
    uses ``tool``, else the first agent of that tool). ``models.min_tier_for.implementer``
    applies. ``None`` means the rendered subagent inherits the main session's model.
    """
    agent = agent_for_tool(profile, tool)
    if agent is None:
        return None
    if tool == claude.TOOL:
        resolved = resolve_subagent(
            definition,
            agent,
            config,
            presets,
            allowed_efforts=claude.EFFORT_VALUES,
            tool_label="Claude Code",
        )
        if resolved.model is not None and not claude.is_claude_model(resolved.model):
            return None
        return resolved.model
    if tool == codex.TOOL:
        resolved = resolve_subagent(
            definition,
            agent,
            config,
            presets,
            allowed_efforts=codex.EFFORT_VALUES,
            tool_label="Codex",
        )
        return resolved.model
    return None


def rendered_path(tool: str) -> Path | None:
    """Repository-relative path of the rendered ``forge-implementer`` for ``tool``."""
    if tool == claude.TOOL:
        return claude.subagent_path(IMPLEMENTER)
    if tool == codex.TOOL:
        return codex.subagent_path(IMPLEMENTER)
    return None


def delegation_for(
    meta: TaskMetadata,
    *,
    config: ForgeConfig,
    profile: Profile | None,
    agent: AgentProfile | None,
    worktree: Path,
    override: DelegationMode | None = None,
    presets: Mapping[str, Preset] | None = None,
) -> Delegation:
    """Gather the inputs of :func:`decide` from ``worktree`` and decide for ``meta``.

    ``worktree`` is where the agent works: the implementer template and the rendered
    ``forge-implementer`` file are looked up there. Reads files only; never raises for a
    missing or invalid template (it becomes a capability note).
    """
    tool = None if profile is None or agent is None else agent.tool
    definition: AgentDef | None = None
    problem: str | None = None
    rendered = False
    model: str | None = None
    if tool is not None and tool in SUBAGENT_TOOLS:
        definition, problem = load_implementer(worktree)
        rel = rendered_path(tool)
        rendered = rel is not None and (worktree / rel).is_file()
        if definition is not None and profile is not None:
            model = implementer_model(definition, profile, tool, config, presets)
    return decide(
        size=meta.size,
        zones=meta.zones,
        policy=config.delegation,
        profile_mode=None if profile is None else profile.delegation,
        override=override,
        tool=tool,
        template=definition is not None,
        rendered=rendered,
        model=model,
        template_problem=problem,
    )


__all__ = [
    "AUTO_MIN_ZONES",
    "IMPLEMENTER",
    "IMPLEMENTER_PATH",
    "SUBAGENT",
    "SUBAGENT_TOOLS",
    "Delegation",
    "Source",
    "decide",
    "delegation_for",
    "implementer_model",
    "load_implementer",
    "rendered_path",
]
