"""`forge setup` and `forge profile edit` logic (spec §6.5, §16, §17, ADR 0014).

Everything that touches the machine goes through injected callables, so tests can stub it:

- :data:`CommandRunner` runs ``gh``, ``claude``, ``codex`` and ``ollama``;
- :data:`ShellRunner` runs the project's ``config.setup.commands``;
- :class:`WizardIO` asks the questions of the interactive wizard (the CLI layer implements it
  with typer prompts, :class:`DefaultsIO` accepts every default for ``--yes``);
- :class:`LocalAdapters` renders and inspects the machine-level adapter files.

This module never imports typer or rich, and never stores credentials: the profile it writes
holds labels only.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from forge.core import adapters
from forge.core.config import ForgeConfig, check_cli_version
from forge.core.errors import ConfigError, ExitCode, ForgeError, PreconditionError
from forge.core.git import Git, GitError
from forge.core.github import (
    MIN_GH_VERSION,
    GhCliClient,
    GitHubClient,
    parse_date_header,
    parse_gh_version,
)
from forge.core.models import check_profile, preset_drift, same_alias
from forge.core.output import Level, Result
from forge.core.presets import Preset
from forge.core.profile import (
    AGENT_ID_PATTERN,
    AgentProfile,
    Profile,
    load_profile,
    parse_profile,
    profile_to_data,
    save_profile,
)
from forge.core.team import LOGIN_RE
from forge.core.tiers import EFFORTS, TIERS, Effort, Tier
from forge.core.validate import Fields
from forge.core.workspace import task_worktrees

MIN_GIT_VERSION: tuple[int, int, int] = (2, 38, 0)
"""Oldest git Forge supports (spec §17)."""

MIN_PYTHON_VERSION: tuple[int, int, int] = (3, 11, 0)
"""Oldest Python Forge supports."""

MAX_CLOCK_SKEW_SECONDS = 300
"""Clock skew against GitHub above which setup warns (ADR 0014, Phase 3 known limit)."""

NONE_CHOICE = "none"
"""Choice meaning "leave this tier unmapped" for local models."""

UNSET_ANSWER = "-"
"""Answer that removes a hosted tier mapping in the wizard."""

TOOL_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claude-code", ("claude", "--version")),
    ("codex", ("codex", "--version")),
    ("ollama", ("ollama", "list")),
)
"""Preset tool name and the command that detects it, in proposal order."""

_ID_PREFIX = {"claude-code": "claude", "codex": "codex", "ollama": "ollama"}
_LOGIN_IN_AUTH_RE = re.compile(r"Logged in to \S+ (?:account|as) ([A-Za-z0-9-]+)")
_MAX_ATTEMPTS = 3


# --------------------------------------------------------------------------- runners


@dataclass(frozen=True)
class CommandOutput:
    """Outcome of one external command: exit code, stdout and stderr."""

    code: int
    out: str = ""
    err: str = ""


CommandRunner = Callable[[Sequence[str]], CommandOutput]
"""Runs an argv (``["gh", "auth", "status"]``) and returns its output. Code 127 = not found."""

ShellRunner = Callable[[str, Path], int]
"""Runs one shell command line in a directory and returns its exit code."""

NOT_FOUND = 127


def subprocess_runner(argv: Sequence[str]) -> CommandOutput:
    """Default :data:`CommandRunner`: ``subprocess.run`` with captured text output.

    A missing executable gives code 127; a command still running after 60 s gives code 124.
    """
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=60)
    except FileNotFoundError:
        return CommandOutput(NOT_FOUND, "", f"{argv[0]}: command not found")
    except subprocess.TimeoutExpired:
        return CommandOutput(124, "", f"{argv[0]}: timed out")
    return CommandOutput(proc.returncode, proc.stdout, proc.stderr)


def subprocess_shell(command: str, cwd: Path) -> int:
    """Default :data:`ShellRunner`: run ``command`` with the shell in ``cwd``.

    The child's stdout goes to our stderr when possible, so ``--json`` output stays clean.
    """
    stdout: Any = None
    with contextlib.suppress(AttributeError, ValueError, io.UnsupportedOperation):
        stdout = sys.stderr.fileno()
    return subprocess.run(command, shell=True, cwd=cwd, stdout=stdout, check=False).returncode


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _python_version() -> tuple[int, int, int]:
    return (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)


@dataclass(frozen=True)
class MachineEnv:
    """The machine as setup sees it. Every field can be replaced in tests."""

    run: CommandRunner = subprocess_runner
    shell: ShellRunner = subprocess_shell
    git_version: Callable[[], tuple[int, int, int]] = Git.version
    python_version: Callable[[], tuple[int, int, int]] = _python_version
    cli_version: str = ""
    now: Callable[[], datetime] = _utc_now
    github: GitHubClient | None = None
    """GitHub client for the clock-skew and login checks; ``None`` builds a repository-less
    :class:`GhCliClient` whose ``gh`` calls go through :attr:`run`."""


def machine_github(env: MachineEnv) -> GitHubClient:
    """The GitHub client setup uses: ``env.github``, else a ``GhCliClient`` running ``env.run``."""
    if env.github is not None:
        return env.github

    def runner(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        out = env.run(argv)
        return out.code, out.out, out.err

    return GhCliClient(runner=runner)


# --------------------------------------------------------------------------- adapters hook


class LocalAdapters(Protocol):
    """Machine-level adapter files (rendered subagents) of one Forge project."""

    def status(self, profile: Profile) -> list[str]:
        """Drift messages: rendered files that are missing or out of date."""
        ...

    def not_ignored(self, profile: Profile) -> list[str]:
        """Repository-relative paths that would be rendered but git does not ignore."""
        ...

    def render(self, profile: Profile) -> tuple[list[str], list[str]]:
        """Render and write the files; return ``(written paths, warnings)``."""
        ...


class ProjectAdapters:
    """:class:`LocalAdapters` backed by :mod:`forge.core.adapters` (ADR 0014)."""

    def __init__(
        self,
        repo_root: Path,
        config: ForgeConfig,
        git: Git,
        presets: Mapping[str, Preset] | None = None,
    ):
        self.repo_root = repo_root
        self.config = config
        self.git = git
        self.presets = presets

    def status(self, profile: Profile) -> list[str]:
        """Return :func:`forge.core.adapters.local_status` for ``profile``.

        "Not ignored" messages are left out: :meth:`not_ignored` reports those.
        """
        messages = adapters.local_status(self.repo_root, profile, self.config, self.git)
        return [m for m in messages if not m.startswith("not ignored by git")]

    def not_ignored(self, profile: Profile) -> list[str]:
        """Return the rendered paths that git would not ignore."""
        render = adapters.render_local(self.repo_root, profile, self.config, presets=self.presets)
        return [
            f.path.as_posix() for f in render.files if not adapters.is_ignored(self.git, f.path)
        ]

    def render(self, profile: Profile) -> tuple[list[str], list[str]]:
        """Render with :func:`render_local` and write with :func:`write_local`.

        The main worktree is rendered first (a failure there raises). Then every Forge task
        worktree (``git worktree list`` entries on ``task/<n>``) is re-rendered best effort:
        a failure there is a warning. Paths outside the repository are absolute.
        """
        render = adapters.render_local(self.repo_root, profile, self.config, presets=self.presets)
        written = [
            _relative(self.repo_root, p)
            for p in adapters.write_local(self.repo_root, render, self.git)
        ]
        warnings = list(render.warnings)
        try:
            worktrees = task_worktrees(self.git)
        except GitError as exc:
            return written, [*warnings, f"task worktrees not re-rendered: {exc.message}"]
        main = os.path.realpath(self.repo_root)
        for _issue, path in worktrees:
            if os.path.realpath(path) == main:
                continue
            extra, notes = adapters.write_local_best_effort(
                path, profile, self.config, presets=self.presets
            )
            written += [_relative(self.repo_root, p) for p in extra]
            warnings += [n for n in notes if n not in warnings]
        return written, warnings


def _relative(root: Path, path: Path) -> str:
    if path.is_absolute():
        with contextlib.suppress(ValueError):
            return path.relative_to(root).as_posix()
    return path.as_posix()


# --------------------------------------------------------------------------- target


@dataclass(frozen=True)
class SetupTarget:
    """Where setup runs: the git repository and the Forge project, when there is one.

    ``config`` is ``None`` outside a Forge project or when ``.forge/config.yml`` is invalid
    (then ``config_error`` holds the problem). ``adapters`` is ``None`` outside a project.
    """

    repo_root: Path | None = None
    git: Git | None = None
    config: ForgeConfig | None = None
    config_error: ConfigError | None = None
    adapters: LocalAdapters | None = None

    @property
    def in_project(self) -> bool:
        """True inside a Forge project with a valid config."""
        return self.config is not None and self.repo_root is not None


# --------------------------------------------------------------------------- checks


@dataclass(frozen=True)
class Check:
    """One setup check. ``level`` is the severity when it fails (``error`` = required)."""

    name: str
    ok: bool
    level: Level
    message: str
    fix: str | None = None

    def to_data(self) -> dict[str, Any]:
        """Return ``{name, ok, level, message, fix}``."""
        return {
            "name": self.name,
            "ok": self.ok,
            "level": str(self.level),
            "message": self.message,
            "fix": self.fix,
        }


def _ok(name: str, message: str) -> Check:
    return Check(name, True, Level.INFO, message)


def _fmt(version: tuple[int, int, int]) -> str:
    return ".".join(str(p) for p in version)


@dataclass(frozen=True)
class MachineReport:
    """Machine-wide checks plus the GitHub login read from ``gh auth status`` (if any)."""

    checks: list[Check]
    login: str | None


def machine_checks(env: MachineEnv) -> MachineReport:
    """Check git, gh (installed, version, authenticated), Python and the clock skew."""
    checks: list[Check] = []
    try:
        git_version = env.git_version()
    except (GitError, OSError):
        checks.append(
            Check(
                "git",
                False,
                Level.ERROR,
                "git is not installed or not on PATH",
                f"install git >= {_fmt(MIN_GIT_VERSION)} (https://git-scm.com)",
            )
        )
    else:
        if git_version >= MIN_GIT_VERSION:
            checks.append(_ok("git", f"git {_fmt(git_version)}"))
        else:
            checks.append(
                Check(
                    "git",
                    False,
                    Level.ERROR,
                    f"git {_fmt(git_version)} is too old (need >= {_fmt(MIN_GIT_VERSION)})",
                    f"upgrade git to {_fmt(MIN_GIT_VERSION)} or newer",
                )
            )

    login: str | None = None
    gh = env.run(["gh", "--version"])
    if gh.code == NOT_FOUND:
        checks.append(
            Check(
                "gh_installed",
                False,
                Level.ERROR,
                "the GitHub CLI (gh) is not installed or not on PATH",
                "install it from https://cli.github.com (macOS: `brew install gh`)",
            )
        )
    else:
        checks.append(_ok("gh_installed", "gh is installed"))
        checks.append(_gh_version_check(gh))
        auth = env.run(["gh", "auth", "status"])
        if auth.code == 0:
            match = _LOGIN_IN_AUTH_RE.search(auth.out + "\n" + auth.err)
            login = match.group(1) if match else None
            who = f" as {login}" if login else ""
            checks.append(_ok("gh_auth", f"gh is authenticated{who}"))
        else:
            checks.append(
                Check(
                    "gh_auth",
                    False,
                    Level.ERROR,
                    "gh is not authenticated",
                    "run `gh auth login` (with the repo scope)",
                )
            )
        checks.append(_clock_check(env))

    python = env.python_version()
    if python >= MIN_PYTHON_VERSION:
        checks.append(_ok("python", f"Python {_fmt(python)}"))
    else:
        checks.append(
            Check(
                "python",
                False,
                Level.ERROR,
                f"Python {_fmt(python)} is too old (need >= {_fmt(MIN_PYTHON_VERSION)})",
                "reinstall Forge with `uv tool install --python 3.12 ...` (see the README)",
            )
        )
    return MachineReport(checks, login)


def _gh_version_check(gh: CommandOutput) -> Check:
    try:
        version = parse_gh_version(gh.out)
    except ValueError:
        return Check(
            "gh_version",
            False,
            Level.ERROR,
            f"cannot read the gh version from {gh.out.strip()[:60]!r}",
            f"install gh >= {_fmt(MIN_GH_VERSION)} from https://cli.github.com",
        )
    if version < MIN_GH_VERSION:
        return Check(
            "gh_version",
            False,
            Level.ERROR,
            f"gh {_fmt(version)} is too old (need >= {_fmt(MIN_GH_VERSION)})",
            "upgrade gh (macOS: `brew upgrade gh`; see https://cli.github.com)",
        )
    return _ok("gh_version", f"gh {_fmt(version)}")


def _clock_check(env: MachineEnv) -> Check:
    server = machine_github(env).server_time()
    if server is None:
        return Check(
            "clock_skew",
            False,
            Level.INFO,
            "could not read GitHub's Date header; clock skew not checked",
            "check the network and `gh auth status`, then re-run `forge setup --check`",
        )
    skew = abs((env.now() - server).total_seconds())
    if skew > MAX_CLOCK_SKEW_SECONDS:
        return Check(
            "clock_skew",
            False,
            Level.WARNING,
            f"this machine's clock is {int(skew)} s away from GitHub's; leases may be misjudged",
            "enable automatic time synchronisation (NTP) in the system settings",
        )
    return _ok("clock_skew", f"clock within {int(skew)} s of GitHub")


def profile_checks(
    path: Path, presets: Mapping[str, Preset], config: ForgeConfig | None
) -> tuple[list[Check], Profile | None]:
    """Check the profile at ``path``: valid, constraint violations and preset drift.

    Returns the checks and the profile (``None`` when missing or invalid).
    """
    if not path.is_file():
        return [
            Check(
                "profile",
                False,
                Level.ERROR,
                f"no Forge profile at {path}",
                "run `forge setup` to create it",
            )
        ], None
    try:
        profile = load_profile(path)
    except ForgeError as exc:
        return [
            Check(
                "profile",
                False,
                Level.ERROR,
                f"invalid profile: {exc.message}",
                exc.hint or "fix the file, or run `forge profile edit`",
            )
        ], None
    checks = [_ok("profile", f"profile {path} is valid")]
    if config is not None:
        violations = check_profile(profile, config.models, presets)
        for v in violations:
            checks.append(
                Check(
                    "profile_constraints", False, v.level, f"agent {v.agent_id}: {v.message}", v.fix
                )
            )
        if not violations:
            checks.append(_ok("profile_constraints", "the profile meets config.models"))
    drifts = preset_drift(profile, presets)
    for d in drifts:
        checks.append(
            Check(
                "preset_drift",
                False,
                Level.INFO,
                f"agent {d.agent_id}: {d.tier} uses {d.current!r}; the preset now proposes "
                f"{d.preset!r}",
                f"`forge profile set {d.agent_id}.{d.tier}={d.preset}` or re-run `forge setup`",
            )
        )
    if not drifts:
        checks.append(_ok("preset_drift", "the profile follows the current presets"))
    return checks, profile


def project_checks(target: SetupTarget, profile: Profile | None, env: MachineEnv) -> list[Check]:
    """Checks that need a repository or a Forge project: config, version pin, rerere and the
    machine-level adapter files. Returns nothing outside a git repository."""
    checks: list[Check] = []
    if target.config_error is not None:
        err = target.config_error
        checks.append(Check("config", False, Level.ERROR, err.message, err.hint))
    if target.config is not None and env.cli_version:
        try:
            compatible = check_cli_version(target.config.forge_version, env.cli_version)
        except ValueError:
            compatible = False
        if compatible:
            checks.append(
                _ok("forge_version", f"forge {env.cli_version} satisfies the project's pin")
            )
        else:
            checks.append(
                Check(
                    "forge_version",
                    False,
                    Level.WARNING,
                    f"forge {env.cli_version} does not satisfy forge_version "
                    f"{target.config.forge_version} in .forge/config.yml",
                    "install the pinned version (see docs/forge/onboarding.md); "
                    "`forge next` and `forge ship` refuse to run until then",
                )
            )
    if target.git is not None:
        checks.append(rerere_check(target.git))
    if target.adapters is not None and profile is not None:
        try:
            drift = target.adapters.status(profile)
            not_ignored = target.adapters.not_ignored(profile)
        except ForgeError as exc:
            checks.append(Check("local_adapters", False, Level.WARNING, exc.message, exc.hint))
        else:
            if drift:
                checks.append(
                    Check(
                        "local_adapters",
                        False,
                        Level.WARNING,
                        "machine-level adapter files are out of date: " + "; ".join(drift),
                        "run `forge adapters generate --local` (or `forge setup`)",
                    )
                )
            else:
                checks.append(_ok("local_adapters", "machine-level adapter files are up to date"))
            if not_ignored:
                checks.append(
                    Check(
                        "gitignore",
                        False,
                        Level.ERROR,
                        "git would not ignore: " + ", ".join(not_ignored),
                        "add `.claude/agents/forge-*.md` and `.codex/agents/forge-*.toml` "
                        "to .gitignore",
                    )
                )
            else:
                checks.append(_ok("gitignore", "machine-level adapter files are gitignored"))
    return checks


def rerere_check(git: Git) -> Check:
    """Check that ``rerere.enabled`` is true for the repository."""
    try:
        enabled = (git.config_get("rerere.enabled") or "").strip().lower() == "true"
    except GitError:
        enabled = False
    if enabled:
        return _ok("rerere", "git rerere is enabled")
    return Check(
        "rerere",
        False,
        Level.WARNING,
        "git rerere is not enabled in this repository",
        "run `forge setup`, or `git config rerere.enabled true && "
        "git config rerere.autoupdate true`",
    )


def enable_rerere(git: Git) -> None:
    """Enable rerere (with autoupdate) in the repository's local config."""
    git.config_set("rerere.enabled", "true")
    git.config_set("rerere.autoupdate", "true")


def blocking(checks: Sequence[Check]) -> list[Check]:
    """Return the failed checks of level ``error`` (required prerequisites)."""
    return [c for c in checks if not c.ok and c.level is Level.ERROR]


def run_checks(
    env: MachineEnv, target: SetupTarget, path: Path, presets: Mapping[str, Preset]
) -> Result:
    """``forge setup --check``: every check; exit 5 when a required prerequisite is missing."""
    machine = machine_checks(env)
    prof_checks, profile = profile_checks(path, presets, target.config)
    if profile is not None:
        login_check = github_login_check(profile, machine.login, env)
        if login_check is not None:
            prof_checks.append(login_check)
    checks = machine.checks + prof_checks + project_checks(target, profile, env)
    result = Result(data={"checks": [c.to_data() for c in checks]})
    _add_check_messages(result, checks)
    if blocking(checks):
        result.code = ExitCode.PRECONDITION
    return result


def github_login_check(profile: Profile, login: str | None, env: MachineEnv) -> Check | None:
    """Compare the profile's ``github`` with the login ``gh`` is authenticated as.

    ``login`` is the one read from ``gh auth status``; when it is unknown the GitHub client's
    ``current_user()`` is asked. ``None`` when no login can be read (gh missing or offline).
    """
    if login is None:
        try:
            login = machine_github(env).current_user() or None
        except (ForgeError, OSError, KeyError, TypeError, ValueError):
            login = None
    if login is None:
        return None
    if login.lower() == profile.github.lower():
        return _ok("github_login", f"the profile's github matches the gh login {login}")
    return Check(
        "github_login",
        False,
        Level.WARNING,
        f"the profile's github is {profile.github!r} but gh is logged in as {login!r}; claims "
        "are made as the gh user",
        f"`forge profile edit` and set `github: {login}`, or `gh auth switch` to {profile.github}",
    )


def _add_check_messages(result: Result, checks: Sequence[Check]) -> None:
    for c in checks:
        if c.ok:
            continue
        if c.level is Level.ERROR:
            result.error(f"{c.name}: {c.message}", c.fix)
        elif c.level is Level.WARNING:
            result.warn(f"{c.name}: {c.message}", c.fix)
        else:
            result.info(f"{c.name}: {c.message}", c.fix)


# --------------------------------------------------------------------------- tool detection


@dataclass(frozen=True)
class DetectedTool:
    """An agent tool found on this machine. ``models`` lists local models (ollama only)."""

    tool: str
    command: str
    version: str
    models: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping."""
        return {
            "tool": self.tool,
            "command": self.command,
            "version": self.version,
            "models": list(self.models),
        }


def parse_ollama_list(text: str) -> list[str]:
    """Return the model names in ``ollama list`` output (first column, header skipped)."""
    names: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0].upper() == "NAME":
            continue
        if parts[0] not in names:
            names.append(parts[0])
    return names


def detect_tools(run: CommandRunner) -> list[DetectedTool]:
    """Detect Claude Code, Codex and Ollama with ``run`` (a tool is present when its command
    exits 0)."""
    found: list[DetectedTool] = []
    for tool, argv in TOOL_COMMANDS:
        out = run(list(argv))
        if out.code != 0:
            continue
        if tool == "ollama":
            found.append(DetectedTool(tool, argv[0], "", tuple(parse_ollama_list(out.out))))
        else:
            first = (out.out.strip() or out.err.strip()).splitlines()
            found.append(DetectedTool(tool, argv[0], first[0] if first else ""))
    return found


# --------------------------------------------------------------------------- wizard IO


class WizardIO(Protocol):
    """The questions the wizard asks. Implemented with prompts by the CLI layer."""

    def ask(self, prompt: str, default: str) -> str:
        """Ask for free text; an empty answer means ``default``."""
        ...

    def confirm(self, prompt: str, default: bool = True) -> bool:
        """Ask a yes/no question."""
        ...

    def choose(self, prompt: str, choices: Sequence[str], default: str) -> str:
        """Ask to pick one of ``choices``."""
        ...

    def print(self, text: str) -> None:
        """Show a line of text."""
        ...


@dataclass
class DefaultsIO:
    """:class:`WizardIO` that accepts every default (``--yes``) and records what it shows."""

    printer: Callable[[str], None] | None = None
    lines: list[str] = field(default_factory=list)

    def ask(self, prompt: str, default: str) -> str:
        """Return ``default``."""
        return default

    def confirm(self, prompt: str, default: bool = True) -> bool:
        """Return ``True``: ``--yes`` accepts every proposal."""
        return True

    def choose(self, prompt: str, choices: Sequence[str], default: str) -> str:
        """Return ``default``."""
        return default

    def print(self, text: str) -> None:
        """Record ``text`` and pass it to ``printer`` when set."""
        self.lines.append(text)
        if self.printer is not None:
            self.printer(text)


# --------------------------------------------------------------------------- wizard


def default_agent_id(tool: str, login: str) -> str:
    """Return the proposed agent id ``<tool>-<login>``, lower-cased."""
    prefix = _ID_PREFIX.get(tool, tool)
    return f"{prefix}-{login}".lower()


def _ask_valid(io_: WizardIO, prompt: str, default: str, valid: Callable[[str], str | None]) -> str:
    """Ask until ``valid`` returns ``None`` (no problem); give up after a few attempts."""
    problem = ""
    for _ in range(_MAX_ATTEMPTS):
        answer = io_.ask(prompt, default).strip() or default
        problem = valid(answer) or ""
        if not problem:
            return answer
        io_.print(f"  {problem}")
    raise ForgeError(
        f"no valid answer for {prompt!r}: {problem}",
        hint="re-run `forge setup` and answer the question",
    )


def _login_problem(value: str) -> str | None:
    if LOGIN_RE.fullmatch(value) is None:
        return f"{value!r} is not a valid GitHub login"
    return None


def _id_problem(taken: set[str]) -> Callable[[str], str | None]:
    def check(value: str) -> str | None:
        if re.fullmatch(AGENT_ID_PATTERN, value) is None:
            return f"{value!r} is not a valid agent id (lower case letters, digits, . _ -)"
        if value in taken:
            return f"agent id {value!r} is already used"
        return None

    return check


def _known_models(preset: Preset | None) -> list[str]:
    if preset is None:
        return []
    return [m for models in preset.known_models.values() for m in models] + list(
        preset.tiers.values()
    )


def _recommendation(
    tier: Tier, preset: Preset | None, recommended: Mapping[Tier, str]
) -> str | None:
    rec = recommended.get(tier)
    if rec is None:
        return None
    if any(same_alias(rec, m) for m in _known_models(preset)):
        return rec
    return None


def _edit_agent(
    io_: WizardIO,
    *,
    tool: DetectedTool,
    base: AgentProfile | None,
    proposed_id: str,
    taken: set[str],
    preset: Preset | None,
    recommended: Mapping[Tier, str],
) -> AgentProfile | None:
    """Walk one agent through id, tiers and effort. ``None`` when it ends with no tier."""
    agent_id = _ask_valid(io_, f"Agent id for {tool.tool}", proposed_id, _id_problem(taken))
    tiers: dict[Tier, str] = {}
    if tool.tool == "ollama":
        if not tool.models:
            io_.print("  `ollama list` shows no models: pull one with `ollama pull <model>`")
        for tier in reversed(TIERS):
            current = base.tiers.get(tier) if base else None
            choices = [*tool.models, NONE_CHOICE]
            local_default = NONE_CHOICE
            if current is not None:
                # Re-running keeps the current mapping by default, even if not listed now.
                if current not in tool.models:
                    choices.insert(len(tool.models), current)
                local_default = current
            elif base is None and tier is not Tier.STRONG and tool.models:
                local_default = tool.models[0]
            picked = io_.choose(f"  {tier} model (local)", choices, local_default)
            if picked != NONE_CHOICE:
                tiers[tier] = picked
    else:
        for tier in reversed(TIERS):
            current = base.tiers.get(tier) if base else None
            proposal = preset.tiers.get(tier) if preset else None
            default: str | None = current if base is not None else proposal
            notes: list[str] = []
            if current is not None and proposal is not None and current != proposal:
                notes.append(f"preset: {proposal}")
            rec = _recommendation(tier, preset, recommended)
            if rec is not None:
                notes.append(f"project recommends {rec}")
            suffix = f" ({'; '.join(notes)})" if notes else ""
            hint = f", `{UNSET_ANSWER}` for none" if default else ""
            answer = io_.ask(f"  {tier} model{suffix}{hint}", default or "").strip()
            if answer == UNSET_ANSWER:
                continue
            value = answer or default
            if value:
                tiers[tier] = value
    effort: dict[Effort, str] = {}
    supports_effort = preset.supports_effort if preset is not None else True
    if supports_effort:
        for level in EFFORTS:
            current = base.effort.get(level) if base else None
            proposal = preset.effort.get(level) if preset else None
            effort_default = current if current is not None else proposal
            if effort_default is None:
                continue
            answer = io_.ask(f"  effort {level}", effort_default).strip()
            effort[level] = answer or effort_default
    elif base is not None:
        effort = dict(base.effort)
    if not tiers:
        io_.print(f"  agent {agent_id} has no model for any tier: not added")
        return None
    if base is not None:
        providers = base.providers
    else:
        providers = (preset.provider,) if preset is not None else (tool.tool,)
    return AgentProfile(
        id=agent_id, tool=tool.tool, providers=providers, tiers=tiers, effort=effort
    )


def build_profile(
    io_: WizardIO,
    *,
    tools: Sequence[DetectedTool],
    presets: Mapping[str, Preset],
    recommended: Mapping[Tier, str] | None = None,
    existing: Profile | None = None,
    login: str | None = None,
) -> Profile:
    """Run the profile wizard (ADR 0014, steps 3-4) and return the new profile.

    With an ``existing`` profile every value it holds is the default, agents of tools that
    were not detected are kept unchanged and new tools are offered. Pure: all input comes
    through ``io_``.
    """
    rec = dict(recommended or {})
    default_login = login or (existing.github if existing else "")
    github = _ask_valid(io_, "GitHub login", default_login, _login_problem)
    agents: list[AgentProfile] = []
    taken: set[str] = set()
    detected = {t.tool for t in tools}
    existing_agents = existing.agents if existing else ()
    for tool in tools:
        preset = presets.get(tool.tool)
        bases = [a for a in existing_agents if a.tool == tool.tool]
        label = f"{tool.tool} {tool.version}".strip()
        if bases:
            for base in bases:
                io_.print(f"Agent {base.id} ({label})")
                agent = _edit_agent(
                    io_,
                    tool=tool,
                    base=base,
                    proposed_id=base.id,
                    taken=taken,
                    preset=preset,
                    recommended=rec,
                )
                if agent is not None:
                    agents.append(agent)
                    taken.add(agent.id)
            continue
        if not io_.confirm(f"Add an agent for {label}?", True):
            continue
        io_.print(f"New agent for {label}")
        agent = _edit_agent(
            io_,
            tool=tool,
            base=None,
            proposed_id=default_agent_id(tool.tool, github),
            taken=taken,
            preset=preset,
            recommended=rec,
        )
        if agent is not None:
            agents.append(agent)
            taken.add(agent.id)
    for base in existing_agents:
        if base.tool not in detected and base.id not in taken:
            io_.print(f"Keeping agent {base.id} unchanged ({base.tool} not detected)")
            agents.append(base)
            taken.add(base.id)
    if not agents:
        raise PreconditionError(
            "no agent configured: no supported agent tool was detected or kept",
            hint="install Claude Code (`claude`), Codex (`codex`) or Ollama, then re-run "
            "`forge setup`",
        )
    ids = [a.id for a in agents]
    previous = existing.default_agent if existing else None
    default = previous if previous in ids else ids[0]
    chosen = io_.choose("Default agent", ids, default) if len(ids) > 1 else ids[0]
    return Profile(
        github=github,
        agents=tuple(agents),
        default_agent=chosen,
        worktrees_dir=existing.worktrees_dir if existing else None,
    )


def validate_profile(profile: Profile, path: Path) -> Profile:
    """Round-trip ``profile`` through the file validator (labels only, every rule applied)."""
    data = yaml.safe_load(yaml.safe_dump(profile_to_data(profile)))
    return parse_profile(Fields(data, path))


# --------------------------------------------------------------------------- setup commands


@dataclass(frozen=True)
class CommandRun:
    """One ``config.setup.commands`` entry and what happened to it."""

    command: str
    ran: bool
    code: int | None = None

    def to_data(self) -> dict[str, Any]:
        """Return ``{command, ran, code}``."""
        return {"command": self.command, "ran": self.ran, "code": self.code}


def run_setup_commands(
    io_: WizardIO, commands: Sequence[str], cwd: Path, shell: ShellRunner
) -> list[CommandRun]:
    """Run each command after confirmation; stop at the first failure."""
    runs: list[CommandRun] = []
    for command in commands:
        if not io_.confirm(f"Run `{command}`?", True):
            runs.append(CommandRun(command, False))
            continue
        io_.print(f"$ {command}")
        code = shell(command, cwd)
        runs.append(CommandRun(command, True, code))
        if code != 0:
            break
    return runs


# --------------------------------------------------------------------------- orchestration


def _load_existing(path: Path, io_: WizardIO, result: Result) -> Profile | None:
    if not path.is_file():
        return None
    try:
        return load_profile(path)
    except ForgeError as exc:
        result.warn(f"the existing profile is invalid and will be replaced: {exc.message}")
        io_.print(f"The existing profile is invalid ({exc.message}); starting from presets.")
        return None


def run_setup(
    io_: WizardIO,
    env: MachineEnv,
    target: SetupTarget,
    path: Path,
    presets: Mapping[str, Preset],
) -> Result:
    """Interactive ``forge setup`` (ADR 0014, steps 1-9). Returns the command result.

    The exit code is 5 when a required machine prerequisite is missing (the profile is still
    written), 1 when a setup command fails, else 0.
    """
    result = Result()
    # 1. Checks.
    machine = machine_checks(env)
    for c in machine.checks:
        mark = "ok  " if c.ok else f"{c.level}:"
        io_.print(f"{mark} {c.name}: {c.message}")
        if not c.ok and c.fix:
            io_.print(f"     Fix: {c.fix}")
    project = project_checks(target, None, env)
    _add_check_messages(result, machine.checks + project)
    # 2. Tools.
    tools = detect_tools(env.run)
    names = ", ".join(t.tool for t in tools) or "none"
    io_.print(f"Detected tools: {names}")
    # 3-4. Wizard.
    existing = _load_existing(path, io_, result)
    recommended = target.config.models.recommended if target.config else {}
    profile = build_profile(
        io_,
        tools=tools,
        presets=presets,
        recommended=recommended,
        existing=existing,
        login=machine.login,
    )
    profile = validate_profile(profile, path)
    # 5. Write.
    save_profile(profile, path)
    io_.print(f"Profile written to {path}")
    # 6. Constraints.
    violations = check_profile(profile, target.config.models, presets) if target.config else []
    for v in violations:
        text = f"profile: agent {v.agent_id}: {v.message}"
        io_.print(f"{v.level}: {text}")
        if v.level is Level.INFO:
            result.info(text, v.fix)
        else:
            result.warn(text, v.fix)
    # 7. rerere.
    rerere = False
    if target.git is not None:
        enable_rerere(target.git)
        rerere = True
        io_.print("Enabled git rerere for this repository")
    # 8. Machine-level adapters.
    rendered: list[str] = []
    if target.adapters is not None:
        rendered, warnings = target.adapters.render(profile)
        for w in warnings:
            result.warn(w)
        io_.print(f"Rendered {len(rendered)} machine-level adapter file(s)")
    else:
        result.info("not inside a Forge project: machine-level adapters not rendered")
    # 9. Setup commands.
    runs: list[CommandRun] = []
    if target.config is not None and target.repo_root is not None:
        runs = run_setup_commands(io_, target.config.setup.commands, target.repo_root, env.shell)
    failed = [r for r in runs if r.ran and r.code != 0]
    result.data = {
        "checks": [c.to_data() for c in machine.checks + project],
        "tools": [t.to_data() for t in tools],
        "profile_path": str(path),
        "profile": profile_to_data(profile),
        "violations": [
            {"level": str(v.level), "agent": v.agent_id, "message": v.message, "fix": v.fix}
            for v in violations
        ],
        "rerere": rerere,
        "rendered": rendered,
        "commands": [r.to_data() for r in runs],
    }
    if failed:
        result.error(
            f"setup command failed with exit code {failed[0].code}: {failed[0].command}",
            "fix the problem and re-run `forge setup`",
        )
        result.code = ExitCode.ERROR
    elif blocking(machine.checks + project):
        result.code = ExitCode.PRECONDITION
    return result


# --------------------------------------------------------------------------- profile editing


EditorRunner = Callable[[Sequence[str]], int]
"""Runs the editor argv (attached to the terminal) and returns its exit code."""


def editor_command(env: Mapping[str, str] = os.environ) -> list[str]:
    """Return the editor argv: ``$VISUAL``, else ``$EDITOR``, else ``vi`` (ADR 0014)."""
    for var in ("VISUAL", "EDITOR"):
        value = env.get(var, "").strip()
        if value:
            return shlex.split(value)
    return ["vi"]


def _run_editor(argv: Sequence[str]) -> int:
    try:
        return subprocess.run(list(argv), check=False).returncode
    except FileNotFoundError:
        return NOT_FOUND


@dataclass(frozen=True)
class EditOutcome:
    """Result of ``forge profile edit``: the new profile and whether the file changed."""

    profile: Profile
    changed: bool


def edit_profile(
    path: Path, editor: Sequence[str], run_editor: EditorRunner = _run_editor
) -> EditOutcome:
    """Edit a temporary copy of the profile, validate it and replace the profile if valid.

    Raises :class:`ForgeError` (exit 1) when the editor fails or the edited copy is invalid;
    the profile is then left untouched. A missing profile is a precondition error (exit 5).
    """
    if not path.is_file():
        raise PreconditionError(f"no Forge profile at {path}", hint="run `forge setup`")
    original = path.read_text(encoding="utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix="forge-profile-", suffix=".yml")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(original)
        code = run_editor([*editor, str(tmp)])
        if code != 0:
            raise ForgeError(
                f"the editor `{' '.join(editor)}` exited with code {code}; profile unchanged",
                hint="set $EDITOR to a working editor, e.g. `export EDITOR=nano`",
            )
        edited = tmp.read_text(encoding="utf-8")
        try:
            profile = load_profile(tmp)
        except ForgeError as exc:
            raise ForgeError(
                f"the edited profile is invalid, {path} is unchanged: {exc.message}",
                hint=exc.hint or "run `forge profile edit` again and fix the problem",
            ) from exc
        if edited == original:
            return EditOutcome(profile, False)
        _atomic_write(path, edited)
        return EditOutcome(profile, True)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".profile-", suffix=".yml.tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def update_profile(profile: Profile, path: Path) -> Profile:
    """Validate ``profile`` and save it to ``path`` (used by ``forge profile set``)."""
    checked = validate_profile(profile, path)
    save_profile(checked, path)
    return checked


__all__ = [
    "MAX_CLOCK_SKEW_SECONDS",
    "MIN_GIT_VERSION",
    "MIN_PYTHON_VERSION",
    "NONE_CHOICE",
    "NOT_FOUND",
    "TOOL_COMMANDS",
    "UNSET_ANSWER",
    "Check",
    "CommandOutput",
    "CommandRun",
    "CommandRunner",
    "DefaultsIO",
    "DetectedTool",
    "EditOutcome",
    "EditorRunner",
    "LocalAdapters",
    "MachineEnv",
    "MachineReport",
    "ProjectAdapters",
    "SetupTarget",
    "ShellRunner",
    "WizardIO",
    "blocking",
    "build_profile",
    "default_agent_id",
    "detect_tools",
    "edit_profile",
    "editor_command",
    "enable_rerere",
    "github_login_check",
    "machine_checks",
    "machine_github",
    "parse_date_header",
    "parse_ollama_list",
    "profile_checks",
    "project_checks",
    "rerere_check",
    "run_checks",
    "run_setup",
    "run_setup_commands",
    "subprocess_runner",
    "subprocess_shell",
    "update_profile",
    "validate_profile",
]
