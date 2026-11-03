"""`forge init [--adapters generic,claude,codex] [--force]` (spec §5.2, ADR 0014).

Scaffolds the project files from the packaged templates and renders the project-level
adapters. The logic is :func:`forge.core.init.init_project`; this module parses options,
looks up the GitHub login (best effort, through ``gh``) and renders the report.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import typer

from forge import __version__
from forge.commands._context import find_repo_root
from forge.commands._output import get_options, run_command
from forge.commands.docs import describe_cli
from forge.core.config import ADAPTERS
from forge.core.errors import ForgeError
from forge.core.git import Git
from forge.core.github import GhCliClient
from forge.core.init import LOGIN_PLACEHOLDER, detect_values, init_project
from forge.core.output import Result


def _gh_login() -> str | None:
    """The authenticated ``gh`` user's login, or ``None`` when ``gh`` is unavailable."""
    try:
        return GhCliClient().current_user() or None
    except (ForgeError, OSError, KeyError, TypeError, ValueError):
        return None


login_lookup: Callable[[], str | None] = _gh_login
"""Looks up the GitHub login for ``{{github_login}}``. Tests replace it."""


def parse_adapters(raw: str | None) -> tuple[str, ...]:
    """Parse ``--adapters`` (comma-separated). Raises :class:`ForgeError` on unknown names."""
    if raw is None:
        return ADAPTERS
    names = [n.strip() for n in raw.split(",") if n.strip()]
    unknown = [n for n in names if n not in ADAPTERS]
    if unknown:
        raise ForgeError(
            f"unknown adapter(s): {', '.join(unknown)}",
            hint=f"use a comma-separated list of: {', '.join(ADAPTERS)}",
        )
    return tuple(names)


def _render(result: Result) -> str:
    data = result.data
    lines = [f"Forge initialised with adapters: {', '.join(data['adapters'])}"]
    for key in ("created", "overwritten", "merged", "unchanged"):
        paths = data[key]
        if paths:
            lines.append(f"{key.capitalize()} ({len(paths)}):")
            lines += [f"  {p}" for p in paths]
    lines.append("Next steps:")
    lines += [f"  {i}. {step}" for i, step in enumerate(data["next_steps"], start=1)]
    return "\n".join(lines)


def init(
    ctx: typer.Context,
    adapters: Annotated[
        str | None,
        typer.Option(
            "--adapters",
            help="Comma-separated adapters (generic is always included). "
            "Default: generic,claude,codex.",
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite existing files that differ.")
    ] = False,
) -> None:
    """Scaffold Forge project files from templates (never commits)."""
    opts = get_options(ctx)

    def run() -> Result:
        chosen = parse_adapters(adapters)
        root = find_repo_root(opts)
        git = Git(root)
        values = detect_values(root, git, version=__version__, github_login=login_lookup())
        report = init_project(root, values, adapters=chosen, force=force, cli=describe_cli())
        result = Result(data=report.to_data())
        if values.repo.startswith("<"):
            result.warn(
                "could not detect the GitHub repository from the `origin` remote",
                "set `github.repo` in .forge/config.yml",
            )
        if values.github_login == LOGIN_PLACEHOLDER:
            result.warn(
                "could not detect your GitHub login (gh unavailable or not authenticated)",
                f"replace {LOGIN_PLACEHOLDER} in .forge/team.yml and plans/roadmap.yml",
            )
        return result

    run_command(opts, run, _render)
