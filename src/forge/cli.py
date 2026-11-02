"""Forge command-line entry point (Typer app ``app``)."""

from __future__ import annotations

from typing import Annotated

import typer

from forge import __version__

app = typer.Typer(
    name="forge",
    help="Coordinate several people and AI agents working in parallel on one GitHub repository.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    """Print the Forge version and exit when ``--version`` is given."""
    if value:
        typer.echo(f"forge {__version__}")
        raise typer.Exit(code=0)


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the Forge version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    # Phase 1: add the global options from spec §16 here
    # (--json, --agent, --repo, --verbose, --yes) and store them on ctx.obj.
) -> None:
    """Forge: coordinate people and AI agents on one GitHub repository."""
