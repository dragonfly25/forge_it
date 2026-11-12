"""Path normalisation for snapshot and golden comparisons on every platform (ADR 0021).

Snapshots store temporary directories as a token (``<tmp>``, ``<base>``) followed by
``/``-separated parts. On Windows the same path has backslashes and may be printed by git
with forward slashes, so every form of the base is replaced, and the separators after the
token are turned into ``/``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def path_forms(bases: Iterable[Path]) -> list[str]:
    """Every spelling of ``bases`` (as given, resolved, native and with ``/``), longest first."""
    forms = set()
    for base in bases:
        for path in (base, base.resolve()):
            forms.update({str(path), path.as_posix()})
    return sorted(forms, key=len, reverse=True)


def replace_paths(value: Any, bases: Iterable[Path], token: str) -> Any:
    """``value`` (JSON-like) with the ``bases`` in every string replaced by ``token``."""
    forms = path_forms(bases)

    def fix(text: str) -> str:
        for form in forms:
            text = text.replace(form, token)
        if token in text and os.sep == "\\":
            text = text.replace("\\", "/")
        return text

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            return fix(item)
        if isinstance(item, dict):
            return {walk(k): walk(v) for k, v in item.items()}
        if isinstance(item, list | tuple):
            return [walk(v) for v in item]
        return item

    return walk(value)
