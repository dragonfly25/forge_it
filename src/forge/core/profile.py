"""The personal machine profile `~/.config/forge/profile.yml` (spec §5.3, §6.4).

The profile maps abstract tiers and effort levels to the concrete models of each agent the
user can run. It holds labels only: credentials are never stored, and unknown keys are
rejected at every level so that no credential field can sneak in.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from forge.core.config import DELEGATION_MODES, DelegationMode, parse_delegation_mode
from forge.core.errors import ForgeError
from forge.core.system import config_home, replace_file
from forge.core.team import LOGIN_RE
from forge.core.tiers import EFFORTS, TIERS, Effort, Tier
from forge.core.validate import Fields, load_yaml_mapping

AGENT_ID_PATTERN = r"[a-z0-9][a-z0-9._-]*"
"""Pattern every agent id must match (full match)."""

_TOP_KEYS = ("github", "agents", "default_agent", "worktrees_dir", "delegation")
_AGENT_KEYS = ("id", "tool", "providers", "tiers", "effort")
_SET_USAGE = (
    "use `<agent>.<tier>=<model>`, `<agent>.effort.<level>=<value>`, `default_agent=<id>` or "
    "`delegation=<off|auto|on>`, e.g. `claude-pro-1.strong=opus`"
)


@dataclass(frozen=True)
class AgentProfile:
    """One agent the user can run: a tool, its providers and its tier/effort mappings."""

    id: str
    tool: str
    providers: tuple[str, ...]
    tiers: dict[Tier, str] = field(default_factory=dict)
    effort: dict[Effort, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Profile:
    """The whole personal profile."""

    github: str
    agents: tuple[AgentProfile, ...]
    default_agent: str
    worktrees_dir: Path | None = None
    delegation: DelegationMode | None = None
    """Personal delegation mode (ADR 0017); ``None`` follows the project default."""

    def agent_ids(self) -> list[str]:
        """Return the agent ids in file order."""
        return [a.id for a in self.agents]


def profile_path(env: Mapping[str, str] = os.environ, platform: str | None = None) -> Path:
    """Return the profile location: ``<config home>/forge/profile.yml`` (ADR 0021).

    ``$XDG_CONFIG_HOME/forge/profile.yml`` when that is set and absolute (the XDG spec says
    relative values must be ignored); otherwise ``~/.config/forge/profile.yml`` on macOS and
    Linux and ``%APPDATA%\\forge\\profile.yml`` on Windows
    (:func:`forge.core.system.config_home`).
    """
    return config_home(env, platform) / "forge" / "profile.yml"


def load_profile(path: Path | None = None) -> Profile:
    """Load and validate the profile at ``path`` (default: :func:`profile_path`).

    Raises :class:`ConfigError` when the file is missing or invalid.
    """
    target = path if path is not None else profile_path()
    data = load_yaml_mapping(target, create_hint="forge setup")
    return parse_profile(Fields(data, target))


def parse_profile(f: Fields) -> Profile:
    """Build a :class:`Profile` from the top-level reader of a profile file."""
    f.reject_unknown(_TOP_KEYS)
    github = f.str_("github")
    if LOGIN_RE.fullmatch(github) is None:
        raise f.fail(
            "github",
            f"{github!r} is not a valid GitHub login",
            "write your GitHub login, e.g. `github: vincenzo-s`",
        )
    readers = f.list_of_mappings("agents")
    if not readers:
        raise f.fail(
            "agents",
            "at least one agent is required",
            "add an agent, e.g. `agents: [{id: claude-pro-1, tool: claude-code, "
            "providers: [anthropic], tiers: {standard: sonnet}}]`, or run `forge setup`",
        )
    agents: list[AgentProfile] = []
    seen: set[str] = set()
    for reader in readers:
        agent = _agent(reader)
        if agent.id in seen:
            raise reader.fail(
                "id", f"duplicate agent id {agent.id!r}", "give each agent a unique id"
            )
        seen.add(agent.id)
        agents.append(agent)
    ids = [a.id for a in agents]
    default_agent = f.str_("default_agent")
    if default_agent not in seen:
        raise f.fail(
            "default_agent",
            f"{default_agent!r} is not the id of an agent",
            f"use one of: {', '.join(ids)}",
        )
    worktrees_dir: Path | None = None
    raw_dir = f.str_("worktrees_dir", required=False)
    if raw_dir is not None:
        if not raw_dir.strip():
            raise f.fail(
                "worktrees_dir",
                "must not be empty",
                "write a directory path, or `worktrees_dir: null` for the default",
            )
        worktrees_dir = Path(raw_dir).expanduser()
    return Profile(
        github=github,
        agents=tuple(agents),
        default_agent=default_agent,
        worktrees_dir=worktrees_dir,
        delegation=_delegation(f),
    )


def _delegation(f: Fields) -> DelegationMode | None:
    raw = f.raw("delegation")
    if raw is None:
        return None
    mode = parse_delegation_mode(raw)
    if mode is None:
        modes = ", ".join(DELEGATION_MODES)
        raise f.fail(
            "delegation",
            f"invalid mode {raw!r}; expected one of: {modes}",
            "write it as `delegation: auto`, or remove the key to follow the project default",
        )
    return mode


def _agent(f: Fields) -> AgentProfile:
    f.reject_unknown(_AGENT_KEYS)
    agent_id = f.str_("id", pattern=AGENT_ID_PATTERN)
    tool = f.str_("tool")
    if not tool.strip():
        raise f.fail("tool", "must not be empty", "write it as `tool: claude-code`")
    providers = f.str_list("providers", non_empty=True)
    for i, provider in enumerate(providers):
        if not provider.strip():
            raise f.fail(f"providers[{i}]", "must not be empty", "write a provider, e.g. anthropic")
    tiers = _non_empty_map(f, "tiers", Tier, "opus")
    effort = _non_empty_map(f, "effort", Effort, "high")
    return AgentProfile(
        id=agent_id,
        tool=tool,
        providers=tuple(providers),
        tiers={Tier(k): v for k, v in tiers.items()},
        effort={Effort(k): v for k, v in effort.items()},
    )


def _non_empty_map(
    f: Fields, key: str, key_enum: type[Tier] | type[Effort], example: str
) -> dict[str, str]:
    values = f.str_map(key, key_enum=key_enum)
    for k, v in values.items():
        if not v.strip():
            raise f.fail(f"{key}.{k}", "must not be empty", f"write it as `{k}: {example}`")
    return values


# -- selection --------------------------------------------------------------------------------


def select_agent(profile: Profile, requested: str | None) -> AgentProfile:
    """Return the active agent: ``requested`` (from ``--agent``/``FORGE_AGENT``) or the default.

    Raises :class:`ForgeError` naming the available ids when the id is unknown.
    """
    wanted = requested if requested else profile.default_agent
    for agent in profile.agents:
        if agent.id == wanted:
            return agent
    raise ForgeError(
        f"unknown agent {wanted!r}",
        hint=f"use one of: {', '.join(profile.agent_ids())} (see `forge profile show`)",
    )


# -- writing ----------------------------------------------------------------------------------


def _to_data(profile: Profile) -> dict[str, Any]:
    agents: list[dict[str, Any]] = []
    for a in profile.agents:
        agents.append(
            {
                "id": a.id,
                "tool": a.tool,
                "providers": list(a.providers),
                "tiers": {str(t): a.tiers[t] for t in reversed(TIERS) if t in a.tiers},
                "effort": {str(e): a.effort[e] for e in EFFORTS if e in a.effort},
            }
        )
    data: dict[str, Any] = {
        "github": profile.github,
        "agents": agents,
        "default_agent": profile.default_agent,
        "worktrees_dir": str(profile.worktrees_dir) if profile.worktrees_dir else None,
    }
    if profile.delegation is not None:
        data["delegation"] = profile.delegation
    return data


def profile_to_data(profile: Profile) -> dict[str, Any]:
    """Return ``profile`` as plain data, with keys in spec §6.4 order (what the file holds).

    ``delegation`` is present only when the profile sets it (ADR 0017).
    """
    return _to_data(profile)


def dump_profile(profile: Profile) -> str:
    """Serialize ``profile`` as stable YAML, with keys in spec §6.4 order."""
    return yaml.safe_dump(
        _to_data(profile), sort_keys=False, default_flow_style=False, allow_unicode=True
    )


def save_profile(profile: Profile, path: Path) -> None:
    """Write ``profile`` to ``path`` atomically (temp file + replace), creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".profile-", suffix=".yml.tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(dump_profile(profile))
        replace_file(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# -- forge profile set ------------------------------------------------------------------------


def apply_setting(profile: Profile, setting: str) -> Profile:
    """Return a copy of ``profile`` with one ``forge profile set`` assignment applied.

    Accepted forms: ``<agent>.<tier>=<model>``, ``<agent>.effort.<level>=<value>``,
    ``default_agent=<id>`` and ``delegation=<off|auto|on>``. Raises :class:`ForgeError`
    with a usage hint on bad input.
    """
    key, sep, value = setting.partition("=")
    key, value = key.strip(), value.strip()
    if not sep or not key:
        raise ForgeError(f"invalid setting {setting!r}: expected KEY=VALUE", hint=_SET_USAGE)
    if not value:
        raise ForgeError(f"invalid setting {setting!r}: the value is empty", hint=_SET_USAGE)
    if key == "default_agent":
        if value not in profile.agent_ids():
            raise ForgeError(
                f"unknown agent {value!r}",
                hint=f"use one of: {', '.join(profile.agent_ids())}",
            )
        return dataclasses.replace(profile, default_agent=value)
    if key == "delegation":
        mode = parse_delegation_mode(value)
        if mode is None:
            raise ForgeError(
                f"invalid delegation mode {value!r}; expected one of: "
                f"{', '.join(DELEGATION_MODES)}",
                hint="e.g. `forge profile set delegation=auto`",
            )
        return dataclasses.replace(profile, delegation=mode)

    parts = key.split(".")
    tier_names = [str(t) for t in TIERS]
    effort_names = [str(e) for e in EFFORTS]
    if len(parts) >= 3 and parts[-2] == "effort":
        agent_id, level = ".".join(parts[:-2]), parts[-1]
        if level not in effort_names:
            raise ForgeError(
                f"unknown effort level {level!r}; expected one of: {', '.join(effort_names)}",
                hint=_SET_USAGE,
            )
        agent = _agent_for_setting(profile, agent_id)
        updated = dataclasses.replace(agent, effort={**agent.effort, Effort(level): value})
    elif len(parts) >= 2 and parts[-1] in tier_names:
        agent_id = ".".join(parts[:-1])
        agent = _agent_for_setting(profile, agent_id)
        updated = dataclasses.replace(agent, tiers={**agent.tiers, Tier(parts[-1]): value})
    else:
        raise ForgeError(
            f"invalid setting key {key!r}; the last part must be a tier "
            f"({', '.join(tier_names)}) or effort.<{'|'.join(effort_names)}>",
            hint=_SET_USAGE,
        )
    agents = tuple(updated if a.id == agent.id else a for a in profile.agents)
    return dataclasses.replace(profile, agents=agents)


def _agent_for_setting(profile: Profile, agent_id: str) -> AgentProfile:
    for agent in profile.agents:
        if agent.id == agent_id:
            return agent
    raise ForgeError(
        f"unknown agent {agent_id!r}",
        hint=f"use one of: {', '.join(profile.agent_ids())}; {_SET_USAGE}",
    )


__all__ = [
    "AGENT_ID_PATTERN",
    "AgentProfile",
    "Profile",
    "apply_setting",
    "dump_profile",
    "load_profile",
    "parse_profile",
    "profile_path",
    "profile_to_data",
    "save_profile",
    "select_agent",
]
