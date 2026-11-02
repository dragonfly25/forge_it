"""Rendering of command results (ADR 0004): JSON envelope or human text, plus error handling."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.pretty import Pretty

from forge.core.errors import ForgeError
from forge.core.output import Level, Result, dumps, error_result


@dataclass(frozen=True)
class GlobalOptions:
    """Global CLI options (spec §16), parsed in the root callback and stored on ``ctx.obj``."""

    json: bool = False
    agent: str | None = None
    repo: Path | None = None
    verbose: bool = False
    yes: bool = False


_PREFIX = {Level.INFO: "info: ", Level.WARNING: "warning: ", Level.ERROR: "error: "}


def get_options(ctx: typer.Context) -> GlobalOptions:
    """Return the :class:`GlobalOptions` stored on the root context (defaults if absent)."""
    obj = ctx.find_root().obj
    return obj if isinstance(obj, GlobalOptions) else GlobalOptions()


Renderer = Callable[[Result], str | None]
"""Human rendering of a successful command's data; ``None`` falls back to the default view."""


def emit(result: Result, opts: GlobalOptions, render: Renderer | None = None) -> None:
    """Render ``result``: one JSON document on stdout, or text (data stdout, messages stderr).

    In text mode ``render`` (when given) turns the result into the text printed on stdout;
    it is not called for results that carry no data (e.g. errors).
    """
    if opts.json:
        typer.echo(dumps(result))
        return
    text = render(result) if render is not None and result.data else None
    if text is not None:
        typer.echo(text.rstrip("\n"))
    elif result.data:
        Console(highlight=False).print(Pretty(result.data, expand_all=False))
    for message in result.messages:
        typer.echo(f"{_PREFIX[message.level]}{message.text}", err=True)
        if message.hint:
            typer.echo(f"Fix: {message.hint}", err=True)


def run_command(
    opts: GlobalOptions, fn: Callable[[], Result], render: Renderer | None = None
) -> NoReturn:
    """Call ``fn``, render its result (or the :class:`ForgeError` it raises) and exit.

    ``render`` is the optional human renderer passed to :func:`emit`. Exceptions that are not
    ``ForgeError`` propagate unchanged.
    """
    try:
        result = fn()
    except ForgeError as exc:
        result = error_result(exc)
    emit(result, opts, render)
    raise typer.Exit(code=int(result.code))
