"""`forge setup [--check] [--yes]` (spec §6.5, §16, ADR 0014).

The logic lives in :mod:`forge.core.setup`; this module builds the target (repository and
Forge project, when there is one), implements the wizard's IO with typer prompts and renders
the result.

The machine environment is built by the module attribute :data:`env_factory`. Tests replace
it with ``monkeypatch.setattr(setup_cmd, "env_factory", ...)``; it is looked up at call time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated

import typer

from forge import __version__
from forge.commands._context import find_repo_root
from forge.commands._output import GlobalOptions, get_options, run_command
from forge.core.config import CONFIG_PATH, load_config
from forge.core.errors import ConfigError, PreconditionError
from forge.core.git import Git
from forge.core.output import Result
from forge.core.presets import Preset, load_presets
from forge.core.profile import Profile, profile_path
from forge.core.setup import (
    DefaultsIO,
    MachineEnv,
    ProjectAdapters,
    SetupTarget,
    run_checks,
    run_setup,
)


def _default_env() -> MachineEnv:
    return MachineEnv(cli_version=__version__)


env_factory: Callable[[], MachineEnv] = _default_env
"""Builds the :class:`MachineEnv` used by setup. Tests replace it with ``monkeypatch``."""


class TyperIO:
    """:class:`~forge.core.setup.WizardIO` backed by typer prompts.

    With ``err=True`` (``--json``) prompts and lines go to stderr, keeping stdout for JSON.
    """

    def __init__(self, err: bool = False):
        self.err = err

    def ask(self, prompt: str, default: str) -> str:
        """Prompt for text; an empty answer returns ``default``."""
        answer: str = typer.prompt(
            prompt, default=default, show_default=bool(default), err=self.err
        )
        return answer

    def confirm(self, prompt: str, default: bool = True) -> bool:
        """Prompt for yes/no."""
        return bool(typer.confirm(prompt, default=default, err=self.err))

    def choose(self, prompt: str, choices: Sequence[str], default: str) -> str:
        """Prompt for one of ``choices`` (by name or 1-based number) until the answer is valid."""
        options = list(choices)
        listing = ", ".join(f"{i}) {c}" for i, c in enumerate(options, start=1))
        while True:
            answer: str = typer.prompt(f"{prompt} [{listing}]", default=default, err=self.err)
            answer = answer.strip()
            if answer in options:
                return answer
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1]
            typer.echo(f"  choose one of: {', '.join(options)}", err=self.err)

    def print(self, text: str) -> None:
        """Print one line."""
        typer.echo(text, err=self.err)


def build_target(opts: GlobalOptions, presets: Mapping[str, Preset] | None = None) -> SetupTarget:
    """Return the repository and Forge project setup works on (empty outside a repository).

    ``--repo`` must name a repository; without it, running outside one is fine.
    """
    try:
        root = find_repo_root(opts)
    except PreconditionError:
        if opts.repo is not None:
            raise
        return SetupTarget()
    git = Git(root)
    if not (root / CONFIG_PATH).is_file():
        return SetupTarget(repo_root=root, git=git)
    try:
        config = load_config(root)
    except ConfigError as exc:
        return SetupTarget(repo_root=root, git=git, config_error=exc)
    adapters = ProjectAdapters(root, config, git, presets)
    return SetupTarget(repo_root=root, git=git, config=config, adapters=adapters)


def rerender(
    opts: GlobalOptions, profile: Profile, result: Result, presets: Mapping[str, Preset]
) -> list[str]:
    """Re-render the machine-level adapter files after a profile change (§6.5.3).

    Outside a Forge project nothing is rendered and an info line says so.
    """
    target = build_target(opts, presets)
    if target.adapters is None:
        why = (
            "the project config is invalid" if target.config_error else "not inside a Forge project"
        )
        result.info(f"{why}: machine-level adapters not rendered")
        return []
    written, warnings = target.adapters.render(profile)
    for warning in warnings:
        result.warn(warning)
    return written


def render_checks(result: Result) -> str | None:
    """Human view of ``setup --check``: one line per check."""
    checks = result.data.get("checks")
    if not isinstance(checks, list):
        return None
    lines = []
    for c in checks:
        mark = "ok" if c["ok"] else c["level"]
        lines.append(f"{mark:<8}{c['name']}: {c['message']}")
    return "\n".join(lines)


def render_setup(result: Result) -> str | None:
    """Human summary of an interactive setup run."""
    data = result.data
    if "profile_path" not in data:
        return None
    lines = [f"Profile: {data['profile_path']}"]
    profile = data.get("profile") or {}
    agents = [a.get("id") for a in profile.get("agents", [])]
    lines.append(f"Agents: {', '.join(agents)} (default {profile.get('default_agent')})")
    if data.get("rendered"):
        lines.append(f"Rendered: {', '.join(data['rendered'])}")
    for run in data.get("commands", []):
        status = f"exit {run['code']}" if run["ran"] else "skipped"
        lines.append(f"Command `{run['command']}`: {status}")
    return "\n".join(lines)


def setup(
    ctx: typer.Context,
    check: Annotated[
        bool, typer.Option("--check", help="Only run the checks; exit 5 if a prerequisite fails.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Accept every default without prompting.")
    ] = False,
) -> None:
    """Check this machine and create or update the profile (§6.5)."""
    opts = get_options(ctx)
    accept_all = yes or opts.yes

    def run() -> Result:
        presets = load_presets()
        target = build_target(opts, presets)
        env = env_factory()
        path: Path = profile_path()
        if check:
            return run_checks(env, target, path, presets)
        if accept_all:
            printer: Callable[[str], None] = lambda text: typer.echo(text, err=opts.json)  # noqa: E731
            return run_setup(DefaultsIO(printer=printer), env, target, path, presets)
        return run_setup(TyperIO(err=opts.json), env, target, path, presets)

    run_command(opts, run, render_checks if check else render_setup)
