"""Forge-managed regions inside human-edited files (ADR 0014).

`AGENTS.md` and `CLAUDE.md` may hold human text. Forge owns only the text between
:data:`BEGIN` and :data:`END`; everything outside the region is kept byte for byte, including
CRLF line endings. A file without the region gets it appended at the end.

All functions work on ``str`` decoded without newline translation (read the file with
``Path.read_bytes().decode("utf-8")``), so ``\\r\\n`` survives a round trip.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from forge.core.errors import ConfigError

BEGIN = "<!-- forge:adapter-begin -->"
END = "<!-- forge:adapter-end -->"

_FIX = (
    f"keep exactly one `{BEGIN}` line followed by one `{END}` line, or delete both markers "
    "and run `forge adapters generate` again"
)


@dataclass(frozen=True)
class Region:
    """Location of the managed region in a text: ``start``/``end`` span both markers."""

    start: int
    end: int
    inner: str


def newline_of(text: str) -> str:
    """Return ``"\\r\\n"`` when ``text`` uses CRLF line endings, else ``"\\n"``."""
    return "\r\n" if "\r\n" in text else "\n"


def find_region(text: str, file: Path | str = "<text>") -> Region | None:
    """Return the managed region of ``text``, or ``None`` when it has none.

    Raises :class:`ConfigError` naming ``file`` when the markers are unbalanced, out of order
    or repeated.
    """
    begins = text.count(BEGIN)
    ends = text.count(END)
    if begins == 0 and ends == 0:
        return None
    if begins != 1 or ends != 1:
        raise ConfigError(
            file,
            None,
            f"expected one managed region, found {begins} begin and {ends} end marker(s)",
            _FIX,
        )
    start = text.index(BEGIN)
    end_at = text.index(END)
    if end_at < start:
        raise ConfigError(
            file, None, "the managed region's end marker comes before its begin", _FIX
        )
    end = end_at + len(END)
    return Region(start=start, end=end, inner=text[start + len(BEGIN) : end_at])


def render_region(inner: str, newline: str = "\n") -> str:
    """Return the full region (both markers) around ``inner``, using ``newline`` line endings.

    ``inner`` is written with ``\\n`` line endings; it always ends up on its own lines.
    """
    body = inner.strip("\n")
    lines = [BEGIN, *body.split("\n"), END] if body else [BEGIN, END]
    return newline.join(lines)


def upsert_region(existing: str | None, inner: str, file: Path | str = "<text>") -> str:
    """Return ``existing`` with its managed region set to ``inner``.

    Text outside the region is preserved byte for byte. A text without a region gets one
    appended at the end, after a blank line. ``None`` (no file yet) gives just the region.
    """
    if existing is None or existing == "":
        return render_region(inner) + "\n"
    newline = newline_of(existing)
    region = find_region(existing, file)
    block = render_region(inner, newline)
    if region is not None:
        return existing[: region.start] + block + existing[region.end :]
    prefix = existing
    if not prefix.endswith("\n"):
        prefix += newline
    return prefix + newline + block + newline


def region_inner(text: str, file: Path | str = "<text>") -> str | None:
    """Return the content of the managed region (without markers), or ``None``."""
    region = find_region(text, file)
    return None if region is None else region.inner


__all__ = [
    "BEGIN",
    "END",
    "Region",
    "find_region",
    "newline_of",
    "region_inner",
    "render_region",
    "upsert_region",
]
