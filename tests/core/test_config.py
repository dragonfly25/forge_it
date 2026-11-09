"""Tests for forge.core.config (`.forge/config.yml`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

from forge.core.config import (
    ADAPTERS,
    CONFIG_PATH,
    ClaimConfig,
    DelegationConfig,
    ModelsConfig,
    PlanningConfig,
    Resolver,
    ReviewConfig,
    SetupConfig,
    WorktreesConfig,
    check_cli_version,
    load_config,
    normalize_adapters,
    parse_delegation_mode,
)
from forge.core.errors import ConfigError
from forge.core.tiers import Tier

# Verbatim from spec §6.1 (repo replaced); keep line lengths as in the spec.
SPEC_EXAMPLE = """\
forge_version: ">=0.1,<0.2"        # PEP 440 specifier the CLI must satisfy
project:
  name: ai-cms
  default_branch: main
  merge_method: squash             # squash recommended: claim commits disappear
github:
  repo: acme/ai-cms
  project_number: null             # GitHub Projects v2 board (optional, §15)
lease_hours: 24
claim:
  max_attempts: 5
worktrees:
  enabled: true
setup:
  commands:                        # run by `forge setup` after machine checks
    - cp -n .env.example .env.local || true
    - docker compose pull
checks:                            # run by `forge sync --continue` and `forge ship`
  lint: "npm run lint"
  typecheck: "npm run typecheck"
  test: "npm test"
resolvers:                         # deterministic conflict resolvers (§10.4)
  - glob: "package-lock.json"
    strategy: regenerate
    command: "npm install --package-lock-only"
  - glob: "CHANGELOG.md"
    strategy: union
review:
  cross_provider: true             # prefer a reviewer whose agent uses a different provider
  required_approvals: 1
models:                            # project constraints on model choices (decided by the lead)
  recommended: {strong: opus, standard: sonnet}   # shown as suggestions during setup
  min_tier_for: {reviewer: strong, plan: strong}  # subagents/commands that must not run below a tier
  allowed_providers: [anthropic, openai, local]   # warn when a profile uses others
"""  # noqa: E501

MINIMAL = """\
forge_version: ">=0.1,<0.2"
project:
  name: ai-cms
github:
  repo: acme/ai-cms
"""


def write(root: Path, text: str) -> Path:
    path = root / CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def config_error(root: Path, text: str) -> ConfigError:
    path = write(root, text)
    with pytest.raises(ConfigError) as info:
        load_config(root)
    err = info.value
    assert err.file == str(path)
    assert err.hint
    return err


# -- valid files ------------------------------------------------------------------------------


def test_spec_example_loads(tmp_path: Path) -> None:
    write(tmp_path, SPEC_EXAMPLE)
    cfg = load_config(tmp_path)
    assert cfg.forge_version == SpecifierSet(">=0.1,<0.2")
    assert cfg.project.name == "ai-cms"
    assert cfg.project.default_branch == "main"
    assert cfg.project.merge_method == "squash"
    assert cfg.github.repo == "acme/ai-cms"
    assert cfg.github.project_number is None
    assert cfg.lease_hours == 24
    assert cfg.claim == ClaimConfig(max_attempts=5)
    assert cfg.worktrees == WorktreesConfig(enabled=True)
    assert cfg.setup == SetupConfig(
        commands=("cp -n .env.example .env.local || true", "docker compose pull")
    )
    assert list(cfg.checks.items()) == [
        ("lint", "npm run lint"),
        ("typecheck", "npm run typecheck"),
        ("test", "npm test"),
    ]
    assert cfg.resolvers == (
        Resolver("package-lock.json", "regenerate", "npm install --package-lock-only"),
        Resolver("CHANGELOG.md", "union", None),
    )
    assert cfg.review == ReviewConfig(cross_provider=True, required_approvals=1)
    assert cfg.models == ModelsConfig(
        recommended={Tier.STRONG: "opus", Tier.STANDARD: "sonnet"},
        min_tier_for={"reviewer": Tier.STRONG, "plan": Tier.STRONG},
        allowed_providers=("anthropic", "openai", "local"),
    )


def test_defaults_when_optional_sections_absent(tmp_path: Path) -> None:
    write(tmp_path, MINIMAL)
    cfg = load_config(tmp_path)
    assert cfg.project.default_branch == "main"
    assert cfg.project.merge_method == "squash"
    assert cfg.github.project_number is None
    assert cfg.lease_hours == 24
    assert cfg.claim.max_attempts == 5
    assert cfg.worktrees.enabled is True
    assert cfg.setup.commands == ()
    assert cfg.checks == {}
    assert cfg.resolvers == ()
    assert cfg.review == ReviewConfig(cross_provider=True, required_approvals=1)
    assert cfg.models == ModelsConfig({}, {}, None)


def test_project_number_and_other_values(tmp_path: Path) -> None:
    write(
        tmp_path,
        MINIMAL.replace("repo: acme/ai-cms", "repo: acme/ai-cms\n  project_number: 3")
        + "review: {required_approvals: 0, cross_provider: false}\n",
    )
    cfg = load_config(tmp_path)
    assert cfg.github.project_number == 3
    assert cfg.review == ReviewConfig(cross_provider=False, required_approvals=0)


def test_config_is_frozen(tmp_path: Path) -> None:
    write(tmp_path, MINIMAL)
    cfg = load_config(tmp_path)
    with pytest.raises(AttributeError):
        cfg.lease_hours = 1  # type: ignore[misc]


# -- file-level errors ------------------------------------------------------------------------


def test_missing_file_hints_forge_init(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_config(tmp_path)
    assert info.value.file == str(tmp_path / CONFIG_PATH)
    assert info.value.hint is not None and "forge init" in info.value.hint


# -- per-rule errors --------------------------------------------------------------------------


def _drop(text: str, line: str) -> str:
    assert line in text
    return text.replace(line, "")


@pytest.mark.parametrize(
    ("text", "key", "fix_part"),
    [
        (_drop(MINIMAL, 'forge_version: ">=0.1,<0.2"\n'), "forge_version", ">=0.1,<0.2"),
        (
            MINIMAL.replace('">=0.1,<0.2"', '"not a spec"'),
            "forge_version",
            ">=0.1,<0.2",
        ),
        (MINIMAL.replace('">=0.1,<0.2"', '""'), "forge_version", ">=0.1,<0.2"),
        (MINIMAL.replace('">=0.1,<0.2"', "0.1"), "forge_version", ">=0.1,<0.2"),
        (_drop(MINIMAL, "project:\n  name: ai-cms\n"), "project", "project"),
        (MINIMAL.replace("name: ai-cms", "default_branch: main"), "project.name", "name"),
        (MINIMAL.replace("name: ai-cms", 'name: ""'), "project.name", "name"),
        (
            MINIMAL.replace("name: ai-cms", "name: ai-cms\n  merge_method: fast"),
            "project.merge_method",
            "merge_method",
        ),
        (_drop(MINIMAL, "github:\n  repo: acme/ai-cms\n"), "github", "github"),
        (
            MINIMAL.replace("repo: acme/ai-cms", "repo: <ORG>/<REPO>"),
            "github.repo",
            "set it to your GitHub repository",
        ),
        (
            MINIMAL.replace("repo: acme/ai-cms", "repo: acme/<REPO>"),
            "github.repo",
            "set it to your GitHub repository",
        ),
        (MINIMAL.replace("repo: acme/ai-cms", "repo: acme"), "github.repo", "acme/ai-cms"),
        (MINIMAL.replace("repo: acme/ai-cms", "repo: -acme/x"), "github.repo", "acme/ai-cms"),
        (MINIMAL.replace("repo: acme/ai-cms", "repo: a/b/c"), "github.repo", "acme/ai-cms"),
        (
            MINIMAL.replace("repo: acme/ai-cms", "repo: acme/ai-cms\n  project_number: 0"),
            "github.project_number",
            "project_number",
        ),
        (
            MINIMAL.replace("repo: acme/ai-cms", "repo: acme/ai-cms\n  project_number: x"),
            "github.project_number",
            "project_number",
        ),
        (MINIMAL + "lease_hours: 0\n", "lease_hours", "lease_hours: 1"),
        (MINIMAL + "lease_hours: abc\n", "lease_hours", "lease_hours"),
        (MINIMAL + "claim: {max_attempts: 0}\n", "claim.max_attempts", "max_attempts: 1"),
        (MINIMAL + "worktrees: {enabled: 1}\n", "worktrees.enabled", "true"),
        (MINIMAL + "setup: {commands: [ok, 3]}\n", "setup.commands[1]", "quote"),
        (MINIMAL + "setup: {commands: [ok, ' ']}\n", "setup.commands[1]", "shell command"),
        (MINIMAL + "checks: {lint: ''}\n", "checks.lint", "shell command"),
        (MINIMAL + "checks: {lint: 3}\n", "checks.lint", "lint"),
        (MINIMAL + "checks: [lint]\n", "checks", "checks"),
        (
            MINIMAL
            + "resolvers:\n  - {glob: a, strategy: union}\n  - {glob: b, strategy: regenerate}\n",
            "resolvers[1].command",
            "command",
        ),
        (
            MINIMAL + "resolvers:\n  - {glob: a, strategy: union, command: x}\n",
            "resolvers[0].command",
            "remove `command`",
        ),
        (
            MINIMAL + "resolvers:\n  - {glob: a, strategy: ours}\n",
            "resolvers[0].strategy",
            "§10.4",
        ),
        (
            MINIMAL + "resolvers:\n  - {glob: a, strategy: theirs}\n",
            "resolvers[0].strategy",
            "§10.4",
        ),
        (
            MINIMAL + "resolvers:\n  - {glob: a, strategy: magic}\n",
            "resolvers[0].strategy",
            "strategy",
        ),
        (MINIMAL + "resolvers:\n  - {glob: a}\n", "resolvers[0].strategy", "union"),
        (MINIMAL + "resolvers:\n  - {strategy: union}\n", "resolvers[0].glob", "glob"),
        (
            MINIMAL + "resolvers:\n  - {glob: a, strategy: regenerate, command: ''}\n",
            "resolvers[0].command",
            "command",
        ),
        (MINIMAL + "resolvers:\n  - just-a-string\n", "resolvers[0]", "key: value"),
        (
            MINIMAL + "review: {required_approvals: -1}\n",
            "review.required_approvals",
            "required_approvals: 0",
        ),
        (MINIMAL + "review: {cross_provider: yes-please}\n", "review.cross_provider", "true"),
        (MINIMAL + "models: {recommended: {huge: x}}\n", "models.recommended.huge", "strong"),
        (
            MINIMAL + "models: {min_tier_for: {reviewer: mega}}\n",
            "models.min_tier_for.reviewer",
            "strong",
        ),
        (MINIMAL + "models: {allowed_providers: []}\n", "models.allowed_providers", "remove"),
        (MINIMAL + "models: {allowed_providers: anthropic}\n", "models.allowed_providers", "["),
    ],
)
def test_validation_rule(tmp_path: Path, text: str, key: str, fix_part: str) -> None:
    err = config_error(tmp_path, text)
    assert err.key == key
    assert err.hint is not None and fix_part in err.hint
    assert str(err).startswith(f"{tmp_path / CONFIG_PATH}: {key}: ")


@pytest.mark.parametrize(
    ("text", "key", "suggestion"),
    [
        (MINIMAL + "lease_hour: 3\n", "lease_hour", "lease_hours"),
        (MINIMAL + "extra: 1\n", "extra", "remove it"),
        (MINIMAL.replace("name: ai-cms", "name: ai-cms\n  nmae: x"), "project.nmae", "name"),
        (MINIMAL.replace("repo: acme/ai-cms", "repo: acme/ai-cms\n  owner: x"), "github.owner", ""),
        (MINIMAL + "claim: {attempts: 3}\n", "claim.attempts", "max_attempts"),
        (MINIMAL + "worktrees: {enable: true}\n", "worktrees.enable", "enabled"),
        (MINIMAL + "setup: {command: [x]}\n", "setup.command", "commands"),
        (
            MINIMAL + "resolvers:\n  - {glob: a, strategy: union, globs: b}\n",
            "resolvers[0].globs",
            "glob",
        ),
        (MINIMAL + "review: {approvals: 1}\n", "review.approvals", ""),
        (MINIMAL + "models: {recomended: {}}\n", "models.recomended", "recommended"),
    ],
)
def test_unknown_keys_rejected_at_every_level(
    tmp_path: Path, text: str, key: str, suggestion: str
) -> None:
    err = config_error(tmp_path, text)
    assert err.key == key
    assert err.problem == "unknown key"
    assert err.hint is not None and suggestion in err.hint


# -- check_cli_version ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.1.0", True),
        ("0.1.0.dev0", True),
        ("0.1.0rc1", True),
        ("0.2.0.dev0", False),
        ("0.0.9", False),
    ],
)
def test_check_cli_version(version: str, expected: bool) -> None:
    assert check_cli_version(SpecifierSet(">=0.1,<0.2"), version) is expected


def test_check_cli_version_rejects_invalid_version() -> None:
    with pytest.raises(ValueError):
        check_cli_version(SpecifierSet(">=0.1"), "not-a-version")


# -- adapters (ADR 0014) ------------------------------------------------------------------------


def test_adapters_default_to_all_known(tmp_path: Path) -> None:
    write(tmp_path, MINIMAL)
    assert load_config(tmp_path).adapters == ADAPTERS == ("generic", "claude", "codex")


def test_adapters_always_include_generic_in_canonical_order(tmp_path: Path) -> None:
    write(tmp_path, MINIMAL + "adapters: [codex, claude, codex]\n")
    assert load_config(tmp_path).adapters == ("generic", "claude", "codex")
    write(tmp_path, MINIMAL + "adapters: []\n")
    assert load_config(tmp_path).adapters == ("generic",)


def test_unknown_adapter_names_file_key_and_fix(tmp_path: Path) -> None:
    err = config_error(tmp_path, MINIMAL + "adapters: [generic, cursor]\n")
    assert err.file.endswith("config.yml")
    assert err.key == "adapters[1]"
    assert "cursor" in err.problem
    assert "generic, claude, codex" in str(err.hint)


def test_normalize_adapters() -> None:
    assert normalize_adapters(["claude"]) == ("generic", "claude")
    assert normalize_adapters([]) == ("generic",)


# -- delegation (ADR 0017) --------------------------------------------------------------------


def test_delegation_defaults_when_absent(tmp_path: Path) -> None:
    write(tmp_path, MINIMAL)
    config = load_config(tmp_path)
    assert config.delegation == DelegationConfig(default="auto", allowed=("off", "auto", "on"))


@pytest.mark.parametrize(
    ("section", "default", "allowed"),
    [
        ("delegation: {}\n", "auto", ("off", "auto", "on")),
        ("delegation:\n  default: off\n", "off", ("off", "auto", "on")),
        ("delegation:\n  default: 'on'\n", "on", ("off", "auto", "on")),
        ("delegation:\n  default: off\n  allowed: [off]\n", "off", ("off",)),
        # unquoted YAML booleans are read as off/on; order is canonical, duplicates collapse
        ("delegation:\n  allowed: [on, auto, auto]\n", "auto", ("auto", "on")),
        ('delegation:\n  default: "off"\n  allowed: ["off", "on"]\n', "off", ("off", "on")),
    ],
)
def test_delegation_section(
    tmp_path: Path, section: str, default: str, allowed: tuple[str, ...]
) -> None:
    write(tmp_path, MINIMAL + section)
    config = load_config(tmp_path)
    assert config.delegation.default == default
    assert config.delegation.allowed == allowed


@pytest.mark.parametrize(
    ("section", "key", "fix_part"),
    [
        ("delegation: {default: sometimes}\n", "delegation.default", "default: auto"),
        ("delegation: {default: 3}\n", "delegation.default", "default: auto"),
        ("delegation: {allowed: auto}\n", "delegation.allowed", "allowed: [off, auto, on]"),
        ("delegation: {allowed: [auto, maybe]}\n", "delegation.allowed[1]", "off, auto, on"),
        ("delegation: {allowed: []}\n", "delegation.allowed", "at least one mode"),
        ("delegation: {default: on, allowed: [off, auto]}\n", "delegation.default", "allowed"),
        ("delegation: {allowed: [off]}\n", "delegation.default", "delegation.default"),
        ("delegation: {mode: auto}\n", "delegation", "mode"),
        ("delegation: auto\n", "delegation", "delegation"),
    ],
)
def test_delegation_errors_name_key_and_fix(
    tmp_path: Path, section: str, key: str, fix_part: str
) -> None:
    err = config_error(tmp_path, MINIMAL + section)
    assert err.key is not None and err.key.startswith(key)
    assert fix_part in (err.hint or "") or fix_part in err.message


def test_parse_delegation_mode() -> None:
    assert parse_delegation_mode("auto") == "auto"
    assert parse_delegation_mode(False) == "off"
    assert parse_delegation_mode(True) == "on"
    assert parse_delegation_mode("never") is None
    assert parse_delegation_mode(None) is None


# -- planning (ADR 0019) ----------------------------------------------------------------------


def test_planning_defaults_to_optional_when_absent(tmp_path: Path) -> None:
    write(tmp_path, MINIMAL)
    assert load_config(tmp_path).planning == PlanningConfig(
        architecture="optional", max_tasks_per_agent=10, max_components_per_agent=3
    )


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ("planning: {}\n", PlanningConfig()),
        ("planning:\n  max_tasks_per_agent: 4\n", PlanningConfig(max_tasks_per_agent=4)),
        ("planning:\n  architecture: required\n", PlanningConfig(architecture="required")),
        ("planning:\n  architecture: optional\n", PlanningConfig(architecture="optional")),
        (
            "planning:\n  architecture: required\n  max_tasks_per_agent: 1\n"
            "  max_components_per_agent: 1\n",
            PlanningConfig("required", 1, 1),
        ),
    ],
    ids=["empty-block", "missing-architecture-key", "required", "optional", "all-keys"],
)
def test_planning_section(tmp_path: Path, section: str, expected: PlanningConfig) -> None:
    write(tmp_path, MINIMAL + section)
    assert load_config(tmp_path).planning == expected


@pytest.mark.parametrize(
    ("section", "key", "fix_part"),
    [
        ("planning: {architecture: mandatory}\n", "planning.architecture", "architecture:"),
        ("planning: {architecture: true}\n", "planning.architecture", "architecture:"),
        ("planning: {max_tasks_per_agent: 0}\n", "planning.max_tasks_per_agent", "1"),
        ("planning: {max_tasks_per_agent: ten}\n", "planning.max_tasks_per_agent", "10"),
        ("planning: {max_components_per_agent: 0}\n", "planning.max_components_per_agent", "1"),
        ("planning: {max_components_per_agent: 2.5}\n", "planning.max_components_per_agent", "3"),
        ("planning: {architecure: required}\n", "planning.architecure", "architecture"),
        ("planning: required\n", "planning", "mapping"),
    ],
    ids=[
        "bad-mode",
        "bool-mode",
        "tasks-zero",
        "tasks-string",
        "components-zero",
        "components-float",
        "unknown-key",
        "not-mapping",
    ],
)
def test_planning_errors_name_key_and_fix(
    tmp_path: Path, section: str, key: str, fix_part: str
) -> None:
    err = config_error(tmp_path, MINIMAL + section)
    assert err.key == key
    assert fix_part in (err.hint or "") or fix_part in err.message
