"""`forge config validate` (ADR 0018). Offline: no GitHub, no profile.

The checks are :func:`forge.core.config_check.check_project`; this module finds the
repository (without loading config.yml, which may be the broken file) and renders the result.
"""

from __future__ import annotations

from typing import Any

import typer

from forge import __version__
from forge.commands._context import find_repo_root
from forge.commands._output import get_options, run_command
from forge.core.config_check import check_project
from forge.core.errors import ExitCode
from forge.core.git import Git
from forge.core.output import Result

config_app = typer.Typer(
    help="Check the project configuration files (ADR 0018).", no_args_is_help=True
)


def validate(ctx: typer.Context) -> None:
    """Validate .forge/config.yml, team.yml and zones.yml together (offline).

    Exit 1 when a file is missing or invalid; warnings alone exit 0.
    """
    opts = get_options(ctx)

    def run() -> Result:
        root = find_repo_root(opts)
        check = check_project(root, Git(root), version=__version__)
        result = Result(data=check.to_data())
        for error in check.errors:
            result.error(error.text, error.fix)
        for warning in check.warnings:
            result.warn(f"{warning.file}: {warning.message}", warning.fix)
        if not check.ok:
            result.code = ExitCode.ERROR
        return result

    run_command(opts, run, lambda result: render_check(result.data))


def render_check(data: dict[str, Any]) -> str:
    """Human view of the ``forge config validate`` data (messages go to stderr)."""
    counts = data["counts"]
    lines = [f"Forge configuration: {counts['errors']} error(s), {counts['warnings']} warning(s)"]
    width = max(len(name) for name in data["files"])
    lines += [f"  {name.ljust(width)}  {status}" for name, status in data["files"].items()]
    leads = data["leads"]
    if leads is not None:
        lines.append(f"Leads: {', '.join(leads) if leads else 'none'}")
    unzoned = data["unzoned"]
    if unzoned is not None:
        lines.append(f"Tracked files: {data['tracked_files']}, unzoned: {unzoned['count']}")
        lines += [f"  {path}" for path in unzoned["sample"]]
        if unzoned["count"] > len(unzoned["sample"]):
            lines.append(f"  ... and {unzoned['count'] - len(unzoned['sample'])} more")
    empty = data["empty_zones"]
    if empty is not None:
        lines.append(f"Zones matching no file: {', '.join(empty) if empty else 'none'}")
    version = data["version"]
    if version["required"] is not None:
        state = "ok" if version["ok"] else "mismatch"
        lines.append(f"Version: forge {version['cli']}, required {version['required']} ({state})")
    return "\n".join(lines)
