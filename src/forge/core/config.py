"""Project configuration: `.forge/config.yml` (spec §6.1).

Loading validates every key and reports problems as :class:`ConfigError` naming the file,
the key path and a fix. Unknown keys are rejected at every level.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from forge.core.tiers import TIERS, Tier
from forge.core.validate import Fields, load_yaml_mapping, type_name

CONFIG_PATH = Path(".forge/config.yml")
"""Location of the project configuration, relative to the repository root."""

MergeMethod = Literal["squash", "merge", "rebase"]
Strategy = Literal["regenerate", "union"]

MERGE_METHODS: tuple[MergeMethod, ...] = ("squash", "merge", "rebase")
STRATEGIES: tuple[Strategy, ...] = ("regenerate", "union")

DelegationMode = Literal["off", "auto", "on"]
DELEGATION_MODES: tuple[DelegationMode, ...] = ("off", "auto", "on")
"""Implementation delegation modes (ADR 0017), from fewest to most delegation."""

ArchitectureMode = Literal["required", "optional"]
ARCHITECTURE_MODES: tuple[ArchitectureMode, ...] = ("required", "optional")
"""Values of ``planning.architecture`` (ADR 0019)."""

ADAPTERS: tuple[str, ...] = ("generic", "claude", "codex")
"""Known adapter names, in rendering order (ADR 0014). ``generic`` is always enabled."""

_LOGIN = r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}"
_REPO_RE = re.compile(rf"{_LOGIN}/[A-Za-z0-9._-]+")
_VERSION_EXAMPLE = 'forge_version: ">=0.1,<0.2"'


@dataclass(frozen=True)
class ProjectConfig:
    """The `project` section."""

    name: str
    default_branch: str = "main"
    merge_method: MergeMethod = "squash"


@dataclass(frozen=True)
class GitHubConfig:
    """The `github` section. ``repo`` is ``owner/name``."""

    repo: str
    project_number: int | None = None


@dataclass(frozen=True)
class ClaimConfig:
    """The `claim` section."""

    max_attempts: int = 5


@dataclass(frozen=True)
class WorktreesConfig:
    """The `worktrees` section."""

    enabled: bool = True


@dataclass(frozen=True)
class SetupConfig:
    """The `setup` section: shell commands run by `forge setup`."""

    commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolver:
    """A mechanical conflict resolver (spec §10.4)."""

    glob: str
    strategy: Strategy
    command: str | None = None


@dataclass(frozen=True)
class ReviewConfig:
    """The `review` section."""

    cross_provider: bool = True
    required_approvals: int = 1


@dataclass(frozen=True)
class ModelsConfig:
    """Project constraints on model choices (spec §6.5).

    ``allowed_providers`` is ``None`` when providers are not restricted.
    """

    recommended: dict[Tier, str] = field(default_factory=dict)
    min_tier_for: dict[str, Tier] = field(default_factory=dict)
    allowed_providers: tuple[str, ...] | None = None


@dataclass(frozen=True)
class DelegationConfig:
    """The `delegation` section (ADR 0017): the project default and the modes members may use."""

    default: DelegationMode = "auto"
    allowed: tuple[DelegationMode, ...] = DELEGATION_MODES


@dataclass(frozen=True)
class PlanningConfig:
    """The `planning` section (ADR 0019).

    ``architecture`` defaults to ``optional`` when the section or the key is missing, so
    projects created before ADR 0019 keep working. The two ``max_*`` limits are read by
    multi-agent planning (ADR 0020).
    """

    architecture: ArchitectureMode = "optional"
    max_tasks_per_agent: int = 10
    max_components_per_agent: int = 3


@dataclass(frozen=True)
class ForgeConfig:
    """The whole `.forge/config.yml`."""

    forge_version: SpecifierSet
    project: ProjectConfig
    github: GitHubConfig
    lease_hours: int = 24
    claim: ClaimConfig = field(default_factory=ClaimConfig)
    worktrees: WorktreesConfig = field(default_factory=WorktreesConfig)
    setup: SetupConfig = field(default_factory=SetupConfig)
    checks: dict[str, str] = field(default_factory=dict)
    resolvers: tuple[Resolver, ...] = ()
    review: ReviewConfig = field(default_factory=ReviewConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    adapters: tuple[str, ...] = ADAPTERS
    delegation: DelegationConfig = field(default_factory=DelegationConfig)
    planning: PlanningConfig = field(default_factory=PlanningConfig)


_TOP_KEYS = (
    "forge_version",
    "project",
    "github",
    "lease_hours",
    "claim",
    "worktrees",
    "setup",
    "checks",
    "resolvers",
    "review",
    "models",
    "adapters",
    "delegation",
    "planning",
)


def load_config(repo_root: Path) -> ForgeConfig:
    """Load and validate `.forge/config.yml` under ``repo_root``.

    Raises :class:`ConfigError` when the file is missing or invalid.
    """
    path = repo_root / CONFIG_PATH
    data = load_yaml_mapping(path, create_hint="forge init")
    return parse_config(Fields(data, path))


def parse_config(f: Fields) -> ForgeConfig:
    """Build a :class:`ForgeConfig` from the top-level reader of a config file."""
    f.reject_unknown(_TOP_KEYS)
    return ForgeConfig(
        forge_version=_forge_version(f),
        project=_project(f.mapping("project", required=True)),
        github=_github(f.mapping("github", required=True)),
        lease_hours=f.int_("lease_hours", default=24, min=1),
        claim=_claim(f.mapping("claim")),
        worktrees=_worktrees(f.mapping("worktrees")),
        setup=_setup(f.mapping("setup")),
        checks=_checks(f),
        resolvers=tuple(_resolver(r) for r in f.list_of_mappings("resolvers")),
        review=_review(f.mapping("review")),
        models=_models(f.mapping("models")),
        adapters=_adapters(f),
        delegation=_delegation(f.mapping("delegation")),
        planning=_planning(f.mapping("planning")),
    )


def normalize_adapters(names: Iterable[str]) -> tuple[str, ...]:
    """Return ``names`` plus ``generic``, deduplicated, in the canonical :data:`ADAPTERS` order.

    Names must already be known adapter names (see :data:`ADAPTERS`).
    """
    chosen = {"generic", *names}
    return tuple(name for name in ADAPTERS if name in chosen)


def check_cli_version(spec: SpecifierSet, version: str) -> bool:
    """True if CLI ``version`` satisfies ``spec`` (ADR 0003).

    The base version is compared with pre-releases allowed, so ``0.1.0.dev0`` and
    ``0.1.0rc1`` count as ``0.1.0``. Raises ``ValueError`` for an invalid version string.
    """
    base = Version(version).base_version
    return spec.contains(base, prereleases=True)


# -- sections ---------------------------------------------------------------------------------


def _forge_version(f: Fields) -> SpecifierSet:
    raw = f.raw("forge_version")
    if raw is None:
        raise f.fail("forge_version", "required key is missing", f"add `{_VERSION_EXAMPLE}`")
    if not isinstance(raw, str):
        raise f.fail(
            "forge_version",
            f"expected a quoted PEP 440 specifier string, got {type_name(raw)}",
            f"quote it, e.g. `{_VERSION_EXAMPLE}`",
        )
    try:
        spec = SpecifierSet(raw)
    except InvalidSpecifier as exc:
        raise f.fail(
            "forge_version",
            f"invalid version specifier {raw!r}",
            f"use a PEP 440 specifier, e.g. `{_VERSION_EXAMPLE}`",
        ) from exc
    if not raw.strip():
        raise f.fail(
            "forge_version",
            "version specifier is empty",
            f"use a PEP 440 specifier, e.g. `{_VERSION_EXAMPLE}`",
        )
    return spec


def _project(f: Fields) -> ProjectConfig:
    f.reject_unknown(("name", "default_branch", "merge_method"))
    name = _non_empty(f, "name", "my-project")
    default_branch = _non_empty(f, "default_branch", "main", default="main")
    merge_method = cast(MergeMethod, f.choice("merge_method", MERGE_METHODS, default="squash"))
    return ProjectConfig(name=name, default_branch=default_branch, merge_method=merge_method)


def _github(f: Fields) -> GitHubConfig:
    f.reject_unknown(("repo", "project_number"))
    repo = f.str_("repo")
    if "<" in repo or ">" in repo:
        raise f.fail(
            "repo",
            f"placeholder {repo!r} was not replaced",
            "set it to your GitHub repository, e.g. `repo: acme/ai-cms`",
        )
    if _REPO_RE.fullmatch(repo) is None or repo.split("/")[1] in (".", ".."):
        raise f.fail(
            "repo",
            f"invalid repository {repo!r}; expected `owner/name`",
            "write it as `repo: acme/ai-cms`",
        )
    return GitHubConfig(repo=repo, project_number=_optional_int(f, "project_number", min_=1))


def _claim(f: Fields) -> ClaimConfig:
    f.reject_unknown(("max_attempts",))
    return ClaimConfig(max_attempts=f.int_("max_attempts", default=5, min=1))


def _worktrees(f: Fields) -> WorktreesConfig:
    f.reject_unknown(("enabled",))
    return WorktreesConfig(enabled=f.bool_("enabled", default=True))


def _setup(f: Fields) -> SetupConfig:
    f.reject_unknown(("commands",))
    commands = f.str_list("commands")
    for i, cmd in enumerate(commands):
        if not cmd.strip():
            raise f.fail(
                f"commands[{i}]", "command is empty", "remove the item or write a shell command"
            )
    return SetupConfig(commands=tuple(commands))


def _checks(f: Fields) -> dict[str, str]:
    checks = f.str_map("checks")
    for name, cmd in checks.items():
        if not cmd.strip():
            raise f.fail(
                f"checks.{name}",
                "command is empty",
                f'write a shell command, e.g. `{name}: "npm test"`, or remove the check',
            )
    return checks


def _resolver(f: Fields) -> Resolver:
    f.reject_unknown(("glob", "strategy", "command"))
    glob = _non_empty(f, "glob", '"package-lock.json"')
    raw = f.raw("strategy")
    if raw is None:
        raise f.fail("strategy", "required key is missing", "add `strategy: regenerate` or `union`")
    if raw in ("ours", "theirs"):
        raise f.fail(
            "strategy",
            f"strategy {raw!r} is deliberately not offered (spec §10.4)",
            "use `regenerate` (with a `command`) or `union`; see spec §10.4",
        )
    strategy = cast(Strategy, f.choice("strategy", STRATEGIES, default="regenerate"))
    if strategy == "regenerate":
        if f.raw("command") is None:
            raise f.fail(
                "command",
                "required when strategy is `regenerate`",
                'add the command that regenerates the file, e.g. `command: "npm install '
                '--package-lock-only"`',
            )
        command: str | None = _non_empty(f, "command", '"npm install --package-lock-only"')
    else:
        if f.has("command"):
            raise f.fail(
                "command",
                "not allowed when strategy is `union`",
                "remove `command`, or use `strategy: regenerate`",
            )
        command = None
    return Resolver(glob=glob, strategy=strategy, command=command)


def _review(f: Fields) -> ReviewConfig:
    f.reject_unknown(("cross_provider", "required_approvals"))
    return ReviewConfig(
        cross_provider=f.bool_("cross_provider", default=True),
        required_approvals=f.int_("required_approvals", default=1, min=0),
    )


def _models(f: Fields) -> ModelsConfig:
    f.reject_unknown(("recommended", "min_tier_for", "allowed_providers"))
    recommended = {Tier(k): v for k, v in f.str_map("recommended", key_enum=Tier).items()}
    tier_names = [str(t.value) for t in TIERS]
    min_tier_for: dict[str, Tier] = {}
    for name, value in f.str_map("min_tier_for").items():
        if value not in tier_names:
            raise f.fail(
                f"min_tier_for.{name}",
                f"invalid tier {value!r}; expected one of: {', '.join(tier_names)}",
                f"write it as `{name}: strong`",
            )
        min_tier_for[name] = Tier(value)
    allowed: tuple[str, ...] | None = None
    if f.raw("allowed_providers") is not None:
        providers = f.str_list("allowed_providers")
        if not providers:
            raise f.fail(
                "allowed_providers",
                "empty list would forbid every provider",
                "list the allowed providers, e.g. `[anthropic, openai]`, or remove the key",
            )
        allowed = tuple(providers)
    return ModelsConfig(
        recommended=recommended, min_tier_for=min_tier_for, allowed_providers=allowed
    )


def _adapters(f: Fields) -> tuple[str, ...]:
    if f.raw("adapters") is None:
        return ADAPTERS
    names = f.str_list("adapters")
    for i, name in enumerate(names):
        if name not in ADAPTERS:
            raise f.fail(
                f"adapters[{i}]",
                f"unknown adapter {name!r}; known adapters: {', '.join(ADAPTERS)}",
                f"remove it, or use one of: {', '.join(ADAPTERS)}",
            )
    return normalize_adapters(names)


def parse_delegation_mode(value: object) -> DelegationMode | None:
    """Return the delegation mode ``value`` names, or ``None`` when it names none.

    YAML 1.1 reads unquoted ``off``/``on`` as booleans, so ``False`` and ``True`` are
    accepted as ``off`` and ``on``.
    """
    if value is False:
        return "off"
    if value is True:
        return "on"
    if isinstance(value, str):
        for mode in DELEGATION_MODES:
            if value == mode:
                return mode
    return None


def _delegation(f: Fields) -> DelegationConfig:
    f.reject_unknown(("default", "allowed"))
    modes = ", ".join(DELEGATION_MODES)
    allowed: tuple[DelegationMode, ...] = DELEGATION_MODES
    raw_allowed = f.raw("allowed")
    if raw_allowed is not None:
        if not isinstance(raw_allowed, list):
            raise f.fail(
                "allowed",
                f"expected a list of modes, got {type_name(raw_allowed)}",
                f"write it as `allowed: [{modes}]`",
            )
        chosen: list[DelegationMode] = []
        for i, item in enumerate(raw_allowed):
            mode = parse_delegation_mode(item)
            if mode is None:
                raise f.fail(
                    f"allowed[{i}]",
                    f"invalid mode {item!r}; expected one of: {modes}",
                    f"use only {modes}",
                )
            if mode not in chosen:
                chosen.append(mode)
        if not chosen:
            raise f.fail(
                "allowed",
                "empty list would allow no mode",
                f"list at least one mode, e.g. `allowed: [{modes}]`, or remove the key",
            )
        allowed = tuple(m for m in DELEGATION_MODES if m in chosen)
    default: DelegationMode = "auto"
    if f.raw("default") is not None:
        raw_default = f.raw("default")
        parsed = parse_delegation_mode(raw_default)
        if parsed is None:
            raise f.fail(
                "default",
                f"invalid mode {raw_default!r}; expected one of: {modes}",
                "write it as `default: auto`",
            )
        default = parsed
    if default not in allowed:
        where = "" if f.has("default") else " (the built-in default)"
        raise f.fail(
            "default",
            f"default mode {default!r}{where} is not in allowed ({', '.join(allowed)})",
            f"add {default!r} to `delegation.allowed`, or set `delegation.default` to one of: "
            f"{', '.join(allowed)}",
        )
    return DelegationConfig(default=default, allowed=allowed)


def _planning(f: Fields) -> PlanningConfig:
    f.reject_unknown(("architecture", "max_tasks_per_agent", "max_components_per_agent"))
    mode = cast(ArchitectureMode, f.choice("architecture", ARCHITECTURE_MODES, default="optional"))
    return PlanningConfig(
        architecture=mode,
        max_tasks_per_agent=f.int_("max_tasks_per_agent", default=10, min=1),
        max_components_per_agent=f.int_("max_components_per_agent", default=3, min=1),
    )


# -- helpers ----------------------------------------------------------------------------------


def _non_empty(f: Fields, key: str, example: str, *, default: str | None = None) -> str:
    """Return a string that must not be blank (required unless ``default`` is given)."""
    value = f.str_(key, default=default) if default is not None else f.str_(key)
    if not value.strip():
        raise f.fail(key, "must not be empty", f"write it as `{key}: {example}`")
    return value


def _optional_int(f: Fields, key: str, *, min_: int) -> int | None:
    """Return an optional integer (``None`` when absent or null), at least ``min_``."""
    value = f.raw(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise f.fail(
            key,
            f"expected an integer or null, got {type_name(value)}",
            f"write it as `{key}: {min_}` or `{key}: null`",
        )
    if value < min_:
        raise f.fail(key, f"must be >= {min_}, got {value}", f"write it as `{key}: {min_}`")
    return value


__all__ = [
    "ADAPTERS",
    "ARCHITECTURE_MODES",
    "CONFIG_PATH",
    "DELEGATION_MODES",
    "MERGE_METHODS",
    "STRATEGIES",
    "ArchitectureMode",
    "ClaimConfig",
    "DelegationConfig",
    "DelegationMode",
    "ForgeConfig",
    "GitHubConfig",
    "MergeMethod",
    "ModelsConfig",
    "PlanningConfig",
    "ProjectConfig",
    "Resolver",
    "ReviewConfig",
    "SetupConfig",
    "Strategy",
    "WorktreesConfig",
    "check_cli_version",
    "load_config",
    "normalize_adapters",
    "parse_config",
    "parse_delegation_mode",
]
