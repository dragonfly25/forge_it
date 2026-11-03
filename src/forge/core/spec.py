"""Milestone specs: ``specs/<milestone>/<feature>.md`` (spec §9.1, ADR 0013).

A spec starts with a YAML frontmatter block between ``---`` lines::

    ---
    milestone: M1
    status: draft            # draft | ready
    owner: vincenzo-s
    ---

``milestone`` and ``status`` are required, ``owner`` is optional and any other key is kept
as a warning. Acceptance criteria use the rule of
:func:`forge.core.context.defined_acceptance_criteria`. Open questions are the list items of
the ``## Open questions`` section; an item is resolved when it contains the upper-case word
``RESOLVED``.

This module is pure apart from :func:`load_spec`, which reads one file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from forge.core.context import defined_acceptance_criteria
from forge.core.errors import ConfigError
from forge.core.validate import Fields, type_name

SPEC_STATUSES: tuple[str, ...] = ("draft", "ready")
"""Allowed values of the frontmatter ``status``."""

MILESTONE_RE = re.compile(r"^M\d+$")
"""Milestone id grammar, e.g. ``M1``. Use with ``fullmatch``."""

_KNOWN_KEYS: tuple[str, ...] = ("milestone", "status", "owner")
_FRONTMATTER_FIX = (
    "start the file with a frontmatter block: `---`, `milestone: M1`, `status: draft`, `---`"
)

_HEADING_RE = re.compile(r"^[ ]{0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t#]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_LIST_ITEM_RE = re.compile(r"^([ \t]*)(?:[-*+]|\d{1,9}[.)])(?:[ \t]+|$)")
_RESOLVED_RE = re.compile(r"(?<![A-Za-z0-9_])RESOLVED(?![A-Za-z0-9_])")
_OPEN_QUESTIONS = "open questions"


@dataclass(frozen=True)
class OpenQuestion:
    """One item of the ``Open questions`` section (with its continuation lines)."""

    text: str
    resolved: bool


@dataclass(frozen=True)
class Spec:
    """A parsed spec file. ``path`` is the path as referenced (relative to the repo root)."""

    path: str
    milestone: str
    status: str
    owner: str | None
    acceptance_criteria: tuple[str, ...]
    open_questions: tuple[OpenQuestion, ...]
    unknown_keys: tuple[str, ...] = ()
    duplicate_acceptance_criteria: tuple[str, ...] = ()
    """AC ids defined more than once in this spec, in order of their second definition."""

    @property
    def unresolved(self) -> tuple[OpenQuestion, ...]:
        """The open questions not marked ``RESOLVED``."""
        return tuple(q for q in self.open_questions if not q.resolved)

    @property
    def is_ready(self) -> bool:
        """True when ``status: ready`` and every open question is resolved."""
        return self.status == "ready" and not self.unresolved


# --------------------------------------------------------------------------- frontmatter


def split_frontmatter(text: str, file: Path | str) -> tuple[dict[str, Any], str]:
    """Return the frontmatter mapping and the Markdown body of a spec.

    Raises :class:`ConfigError` (naming ``file``, the key and the fix) when the block is
    missing, unclosed, invalid YAML or not a mapping.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ConfigError(file, None, "missing YAML frontmatter", _FRONTMATTER_FIX)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        raise ConfigError(
            file, None, "frontmatter is not closed", "add a `---` line after the frontmatter keys"
        )
    raw = "".join(lines[1:end])
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 2}, column {mark.column + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise ConfigError(
            file,
            None,
            f"invalid YAML in frontmatter{where}: {problem}",
            "fix the YAML syntax (check indentation, colons and quotes)",
        ) from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ConfigError(
            file,
            None,
            f"frontmatter must be a mapping, got {type_name(loaded)}",
            "write the frontmatter as `key: value` pairs",
        )
    return {str(k): v for k, v in loaded.items()}, "".join(lines[end + 1 :])


def parse_spec(text: str, path: str, *, file: Path | str | None = None) -> Spec:
    """Parse the content of a spec referenced as ``path``.

    ``file`` names the file in error messages (defaults to ``path``). Raises
    :class:`ConfigError` for a missing or invalid frontmatter or a bad required key.
    """
    where = file if file is not None else path
    data, body = split_frontmatter(text, where)
    f = Fields(data, where)
    milestone = f.str_("milestone")
    if MILESTONE_RE.fullmatch(milestone) is None:
        raise f.fail(
            "milestone", f"invalid milestone {milestone!r}", "use the form `M<n>`, e.g. `M1`"
        )
    if f.raw("status") is None:
        f.str_("status")  # raises the "missing"/"empty" error
    status = f.choice("status", SPEC_STATUSES, default="draft")
    owner = f.str_("owner", required=False)
    unknown = tuple(k for k in data if k not in _KNOWN_KEYS)
    return Spec(
        path=path,
        milestone=milestone,
        status=status,
        owner=owner,
        acceptance_criteria=tuple(defined_acceptance_criteria(body)),
        open_questions=tuple(open_questions(body)),
        unknown_keys=unknown,
        duplicate_acceptance_criteria=duplicate_definitions(body),
    )


def duplicate_definitions(body: str) -> tuple[str, ...]:
    """AC ids *defined* more than once in the Markdown ``body`` (fenced code ignored).

    A definition follows :func:`defined_acceptance_criteria`; each visible line is checked on
    its own, so two definitions of the same id are counted twice.
    """
    seen: set[str] = set()
    dups: dict[str, None] = {}
    for line in _visible_lines(body):
        if line is None:
            continue
        for ac in defined_acceptance_criteria(line):
            if ac in seen:
                dups.setdefault(ac, None)
            seen.add(ac)
    return tuple(dups)


def load_spec(root: Path, path: str) -> Spec:
    """Read and parse the spec at ``path`` (relative to ``root``).

    Raises :class:`ConfigError` when the file is missing, unreadable or invalid.
    """
    full = root / path
    if not full.is_file():
        raise ConfigError(
            path, None, "spec file not found", "create the spec or fix the path that references it"
        )
    try:
        text = full.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(path, None, f"cannot read file: {exc}", "check the file") from exc
    return parse_spec(text, path)


# --------------------------------------------------------------------------- open questions


def _indent(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _visible_lines(text: str) -> list[str | None]:
    """Lines of ``text`` with fenced code blocks replaced by ``None``."""
    out: list[str | None] = []
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                out.append(None)
            else:
                out.append(line)
        else:
            out.append(None)
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
    return out


def _section(lines: list[str | None]) -> list[str | None]:
    """The lines of the first ``Open questions`` section (any level), headings excluded."""
    for i, line in enumerate(lines):
        if line is None:
            continue
        match = _HEADING_RE.match(line)
        if match is None or (match.group(2) or "").strip().casefold() != _OPEN_QUESTIONS:
            continue
        level = len(match.group(1))
        body: list[str | None] = []
        for other in lines[i + 1 :]:
            if other is not None:
                sub = _HEADING_RE.match(other)
                if sub is not None:
                    if len(sub.group(1)) <= level:
                        break
                    body.append(None)  # a nested heading separates items but stays inside
                    continue
            body.append(other)
        return body
    return []


def open_questions(body: str) -> list[OpenQuestion]:
    """Return the items of the ``Open questions`` section of a Markdown ``body``.

    The section runs up to the next heading of the same or a higher level; nested headings
    stay inside it. Each list item at the outer level of a list, together with its indented
    continuation lines and sub-items, is one question. It is resolved when its text contains
    the word ``RESOLVED`` in upper case, unless the whole item is a parenthesized placeholder.
    Fenced code is ignored.
    """
    items: list[list[str]] = []
    base: int | None = None
    current: list[str] | None = None
    for line in _section(_visible_lines(body)):
        if line is None:  # fenced code or a nested heading ends the current item
            current = None
            base = None
            continue
        if not line.strip():
            continue
        match = _LIST_ITEM_RE.match(line)
        indent = _indent(line)
        if match is not None and (base is None or indent <= base):
            base = indent
            current = [line.strip()]
            items.append(current)
        elif current is not None and base is not None and indent > base:
            current.append(line.strip())
        else:
            current = None
            base = None
    return [_question(" ".join(item)) for item in items]


def _question(text: str) -> OpenQuestion:
    """Build the question for an item's text (list marker included).

    A placeholder item, whose whole content is in parentheses like the template's
    ``(must be empty, or every item marked RESOLVED, …)``, is never resolved (ADR 0013).
    """
    content = _LIST_ITEM_RE.sub("", text, count=1).strip()
    placeholder = content.startswith("(") and content.endswith(")")
    resolved = not placeholder and _RESOLVED_RE.search(text) is not None
    return OpenQuestion(text=text, resolved=resolved)
