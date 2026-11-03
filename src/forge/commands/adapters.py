"""`forge adapters generate [--check] [--local]` (spec §13, ADR 0014).

- plain: rewrite the project-level adapter files, remove stale generated ``forge-*`` skills;
- ``--check``: write nothing, exit 1 listing missing/different/extra files (CI);
- ``--local``: render the machine-level subagents from the current profile (gitignored).
"""

from __future__ import annotations

from typing import Annotated

import typer

from forge.commands._context import load_context
from forge.commands._output import get_options, run_command
from forge.core import adapters as core_adapters
from forge.core.errors import ExitCode
from forge.core.output import Result
from forge.core.profile import load_profile

adapters_app = typer.Typer(
    help="Render tool-native files from .forge/ (§13).", no_args_is_help=True
)


def _render(result: Result) -> str:
    data = result.data
    lines: list[str] = []
    if "drift" in data:
        drift = data["drift"]
        if not drift:
            return f"{data['scope'].capitalize()} adapter files are up to date."
        lines.append(f"{len(drift)} {data['scope']} adapter file(s) out of date:")
        lines += [f"  {d}" for d in drift]
        return "\n".join(lines)
    for key in ("written", "removed"):
        paths = data.get(key, [])
        if paths:
            lines.append(f"{key.capitalize()} ({len(paths)}):")
            lines += [f"  {p}" for p in paths]
    if not lines:
        lines.append(f"{data['scope'].capitalize()} adapter files already up to date.")
    return "\n".join(lines)


def generate(
    ctx: typer.Context,
    check: Annotated[
        bool, typer.Option("--check", help="Write nothing; exit 1 when files are out of date.")
    ] = False,
    local: Annotated[
        bool,
        typer.Option("--local", help="Render machine-level subagents from your profile."),
    ] = False,
) -> None:
    """Regenerate adapter files from .forge/ (project level, or --local machine level)."""
    opts = get_options(ctx)

    def run() -> Result:
        app = load_context(opts)
        if local:
            profile = load_profile()
            if check:
                messages = core_adapters.local_status(app.root, profile, app.config, app.git)
                result = Result(data={"scope": "local", "drift": messages})
            else:
                render = core_adapters.render_local(app.root, profile, app.config)
                changed = core_adapters.write_local(app.root, render, app.git)
                result = Result(
                    data={
                        "scope": "local",
                        "files": [f.path.as_posix() for f in render.files],
                        "written": [p.as_posix() for p in changed],
                    }
                )
                for warning in render.warnings:
                    result.warn(warning)
                return result
            if messages:
                result.code = ExitCode.ERROR
                result.error(
                    "machine-level adapter files are out of date",
                    "run `forge adapters generate --local`",
                )
            return result
        project = core_adapters.load_project(app.root, app.config)
        if check:
            drift = core_adapters.check(app.root, project)
            result = Result(
                data={
                    "scope": "project",
                    "drift": [f"{d.kind}: {d.path.as_posix()}" for d in drift],
                    "files": [d.to_data() for d in drift],
                }
            )
            if drift:
                result.code = ExitCode.ERROR
                result.error(
                    "project-level adapter files are out of date",
                    "run `forge adapters generate` and commit the result",
                )
            return result
        report = core_adapters.generate(app.root, project)
        return Result(data={"scope": "project", **report.to_data()})

    run_command(opts, run, _render)
