"""Task model (spec §7, ADR 0008): the issue metadata block, labels and the derived status.

This module is pure: no git, no GitHub, no filesystem. Callers gather the state (issues,
branches, pull requests) and feed it in.

The metadata block sits between two marker lines and holds exactly one fenced YAML block::

    <!-- forge:begin -->
    ```yaml
    id: M1-T07
    ...
    ```
    <!-- forge:end -->

Forge only ever rewrites the text between the markers; everything else in the issue body is
preserved byte for byte.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

import yaml

from forge.core.errors import ConfigError, ForgeError
from forge.core.github import Issue, Label
from forge.core.tiers import Tier
from forge.core.validate import Fields, type_name

# --------------------------------------------------------------------------- constants

BEGIN_MARKER = "<!-- forge:begin -->"
END_MARKER = "<!-- forge:end -->"

TRAILER_TASK = "Forge-Task"
TRAILER_CLAIM = "Forge-Claim"
TRAILER_AGENT = "Forge-Agent"
TRAILER_HANDOFF = "Forge-Handoff"
TRAILER_RELEASE = "Forge-Release"

TASK_ID_RE = re.compile(r"^M(\d+)-T(\d+)$")
"""Task id grammar, e.g. ``M1-T07``. Use with ``fullmatch``."""
AC_ID_RE = re.compile(r"^AC-\d+$")
"""Acceptance criterion id grammar, e.g. ``AC-3``. Use with ``fullmatch``."""

BLOCKED_CONFLICT = "blocked:conflict"
FORGE_TASK_LABEL = "forge:task"

_FENCE_OPEN_RE = re.compile(r"^```yaml\s*$")
_FENCE_CLOSE_RE = re.compile(r"^```\s*$")

_KEYS: tuple[str, ...] = (
    "id",
    "spec",
    "covers",
    "depends_on",
    "zones",
    "size",
    "tier",
    "type",
    "done_when",
    "allow_contract_change",
)


# --------------------------------------------------------------------------- metadata


class TaskType(StrEnum):
    """Kind of work a task does (spec §7.1)."""

    CONTRACT = "contract"
    IMPL = "impl"
    TEST = "test"
    CHORE = "chore"


class Size(StrEnum):
    """Task size. ``L`` does not exist: such tasks must be split (spec §9.4)."""

    S = "S"
    M = "M"


@dataclass(frozen=True)
class TaskMetadata:
    """The content of a task's metadata block, in spec §7.1 order."""

    id: str
    spec: str
    covers: tuple[str, ...]
    depends_on: tuple[str, ...]
    zones: tuple[str, ...]
    size: Size
    tier: Tier
    type: TaskType
    done_when: tuple[str, ...]
    allow_contract_change: bool = False

    @property
    def milestone(self) -> str:
        """Milestone prefix of the id (``M1`` for ``M1-T07``)."""
        return self.id.partition("-")[0]

    @property
    def number(self) -> int:
        """Task number within the milestone (``7`` for ``M1-T07``)."""
        return int(self.id.partition("-T")[2])

    @staticmethod
    def branch_name(issue_number: int) -> str:
        """Name of the task branch for ``issue_number`` (spec §7.3), e.g. ``task/42``."""
        return f"task/{issue_number}"


class TaskBodyError(ForgeError):
    """An issue body has a malformed Forge block. ``reason`` is the problem without location."""

    def __init__(self, reason: str, *, issue_number: int | None = None, hint: str | None = None):
        self.issue_number = issue_number
        self.reason = reason
        super().__init__(f"{_where(issue_number)}: {reason}", hint=hint)


def _where(issue_number: int | None) -> str:
    return "forge block" if issue_number is None else f"issue #{issue_number}: forge block"


# --------------------------------------------------------------------------- body splitting


@dataclass(frozen=True)
class _Span:
    """Offsets of a block: ``start`` of the begin line, ``inner_start``/``inner_end`` of the
    text between the marker lines, ``end`` just after the end marker (before its newline)."""

    start: int
    inner_start: int
    inner_end: int
    end: int


def _lines_with_offsets(text: str) -> list[tuple[int, str, int]]:
    """Return ``(start, content_without_terminator, end_including_terminator)`` per line.

    Only ``\n`` ends a line (a preceding ``\r`` is stripped from the content), unlike
    :meth:`str.splitlines`, so that other Unicode separators stay inside their line.
    """
    out: list[tuple[int, str, int]] = []
    pos = 0
    for raw in text.split("\n"):
        end = pos + len(raw)
        if end < len(text):
            end += 1  # the "\n" terminator
        elif not raw:
            break  # text ends with "\n" (or is empty): no extra empty line
        out.append((pos, raw.removesuffix("\r"), end))
        pos = end
    return out


def _indent_width(line: str) -> int:
    """Width of the leading whitespace of ``line``, with tabs advancing to a multiple of 4."""
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _is_marker(line: str, marker: str) -> bool:
    """True when ``line`` is ``marker`` alone, indented by at most 3 columns.

    Four or more columns make an indented code block in Markdown, so such a line only shows
    the marker as an example.
    """
    return line.strip() == marker and _indent_width(line) < 4


_FENCE_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")
_LIST_MARKER_RE = re.compile(r"^([-*+]|[0-9]{1,9}[.)])( {1,4})(?=`{3}|~{3})")


@dataclass(frozen=True)
class _Fence:
    """An open fenced code block: its fence ``run`` and the ``indent`` of its content.

    ``indent`` is 0 for a top-level fence and the list item's content column for a fence
    opened right after a list marker (e.g. ``- ```` or ``1. ~~~``).
    """

    run: str
    indent: int

    @property
    def closer(self) -> str:
        """A line that closes this fence: the same run, indented to the content column."""
        return " " * self.indent + self.run


def _fence_open(line: str) -> _Fence | None:
    """Return the fence opened by ``line``, or ``None``.

    Besides a top-level fence (indented by at most 3 columns), the common CommonMark case of
    a fence opened right after a list marker (``-``, ``*``, ``+``, ``1.`` or ``1)``) and one
    to four spaces is recognised; its content and closer are indented to the list item's
    content column. Nested list markers and block quotes are not tracked.
    """
    indent = _indent_width(line)
    if indent >= 4:
        return None
    rest = line.lstrip(" \t")
    content_indent = 0
    item = _LIST_MARKER_RE.match(rest)
    if item is not None:
        content_indent = indent + len(item.group(0))
        rest = rest[len(item.group(0)) :]
    match = _FENCE_RE.match(rest)
    if match is None:
        return None
    run, info = match.groups()
    if run[0] == "`" and "`" in info:
        return None  # CommonMark: a backtick fence's info string cannot contain backticks
    return _Fence(run=run, indent=content_indent)


def _fence_closes(line: str, fence: _Fence) -> bool:
    """True when ``line`` closes ``fence``: the same character, at least as long, alone."""
    if _indent_width(line) - fence.indent >= 4:
        return False
    stripped = line.strip()
    return len(stripped) >= len(fence.run) and set(stripped) == {fence.run[0]}


def _leaves_list_item(line: str, fence: _Fence) -> bool:
    """True when a non-blank ``line`` is indented less than a list-item fence's content.

    Such a line ends the list item, and with it the fenced code block (CommonMark).
    """
    return fence.indent > 0 and bool(line.strip()) and _indent_width(line) < fence.indent


@dataclass(frozen=True)
class _Scan:
    """Marker lines found outside code blocks, plus the fence still open at the end."""

    begins: list[tuple[int, str, int]]
    ends: list[tuple[int, str, int]]
    open_fence: _Fence | None
    swallowed_begin: bool
    """A begin marker sat inside a fence with no end marker after it in that fence: likely a
    real block hidden by a fence left open above it (the block's own closing fence then
    closed it, leaving the end marker visible)."""


def _scan(body: str) -> _Scan:
    """Find the marker lines of ``body``, ignoring markers quoted inside Markdown code blocks.

    Outside the block, fenced code blocks (backticks or tildes, any length >= 3, top-level or
    right after a list marker) are tracked and markers inside them are ignored. Between a
    begin marker and its end marker, fences are not tracked: the block's own ```yaml fence is
    checked by :func:`_extract_yaml`. ``open_fence`` is the fence left unclosed at the end of
    the body, if any.
    """
    begins: list[tuple[int, str, int]] = []
    ends: list[tuple[int, str, int]] = []
    fence: _Fence | None = None
    in_block = False
    pending_begin = False  # a begin marker seen inside the current fence, no end after it
    swallowed = False
    for line in _lines_with_offsets(body):
        content = line[1]
        if in_block:
            if _is_marker(content, END_MARKER):
                ends.append(line)
                in_block = False
            elif _is_marker(content, BEGIN_MARKER):
                begins.append(line)
            continue
        if fence is not None:
            closes = _fence_closes(content, fence)
            if closes or _leaves_list_item(content, fence):
                swallowed |= pending_begin
                pending_begin = False
                fence = None
                if closes:
                    continue
                # The list item ended: handle the line as ordinary text below.
            else:
                if _is_marker(content, BEGIN_MARKER):
                    pending_begin = True
                elif _is_marker(content, END_MARKER):
                    pending_begin = False
                continue
        if _is_marker(content, BEGIN_MARKER):
            begins.append(line)
            in_block = True
        elif _is_marker(content, END_MARKER):
            ends.append(line)
        else:
            fence = _fence_open(content)
    return _Scan(begins, ends, fence, swallowed)


def _find_span(body: str, issue_number: int | None) -> _Span | None:
    """Locate the Forge block (see :func:`_scan`); ``None`` when there are no markers."""
    scan = _scan(body)
    begins, ends = scan.begins, scan.ends
    if not begins and not ends:
        return None
    fix_one = f"keep exactly one `{BEGIN_MARKER}` … `{END_MARKER}` block in the issue body"
    if len(begins) > 1 or len(ends) > 1:
        raise TaskBodyError(
            "found more than one Forge block", issue_number=issue_number, hint=fix_one
        )
    if not ends:
        raise TaskBodyError(
            f"`{BEGIN_MARKER}` without `{END_MARKER}`",
            issue_number=issue_number,
            hint=f"add `{END_MARKER}` on its own line after the YAML block",
        )
    if not begins:
        hint = f"add `{BEGIN_MARKER}` on its own line before the YAML block"
        if scan.swallowed_begin:
            hint = (
                f"`{BEGIN_MARKER}` seems to be inside a code block: a code fence (``` or ~~~) "
                f"above it is probably not closed; close it, or {hint[0].lower()}{hint[1:]}"
            )
        raise TaskBodyError(
            f"`{END_MARKER}` without `{BEGIN_MARKER}`", issue_number=issue_number, hint=hint
        )
    b_start, _, b_end = begins[0]
    e_start, e_content, _ = ends[0]
    if e_start < b_start:
        raise TaskBodyError(
            f"`{END_MARKER}` comes before `{BEGIN_MARKER}`",
            issue_number=issue_number,
            hint=fix_one,
        )
    return _Span(start=b_start, inner_start=b_end, inner_end=e_start, end=e_start + len(e_content))


def split_body(body: str, *, issue_number: int | None = None) -> tuple[str, str | None, str]:
    """Split ``body`` into ``(before, block_text, after)``.

    ``block_text`` is the text between the marker lines (``None`` when there is no block, in
    which case ``before`` is the whole body and ``after`` is empty). ``before`` ends where the
    begin marker line starts and ``after`` starts right after the end marker (with its line
    terminator), so ``before + after`` is the human part of the body. Raises
    :class:`TaskBodyError` on unbalanced or repeated markers.
    """
    span = _find_span(body, issue_number)
    if span is None:
        return body, None, ""
    return body[: span.start], body[span.inner_start : span.inner_end], body[span.end :]


def _extract_yaml(block_text: str, issue_number: int | None) -> str:
    """Return the YAML inside the single fenced block of ``block_text``."""
    lines = [line for _, line, _ in _lines_with_offsets(block_text)]
    fix = "put exactly one ```yaml … ``` block between the markers, and nothing else"
    opens = [i for i, line in enumerate(lines) if _FENCE_OPEN_RE.match(line.strip())]
    if not opens:
        raise TaskBodyError(
            "no ```yaml fenced block between the markers", issue_number=issue_number, hint=fix
        )
    first = opens[0]
    close = next(
        (i for i in range(first + 1, len(lines)) if _FENCE_CLOSE_RE.match(lines[i].strip())), None
    )
    if close is None:
        raise TaskBodyError(
            "the ```yaml fenced block is not closed", issue_number=issue_number, hint=fix
        )
    outside = [line for i, line in enumerate(lines) if (i < first or i > close) and line.strip()]
    if outside:
        raise TaskBodyError(
            f"unexpected text inside the Forge block: {outside[0].strip()[:60]!r}",
            issue_number=issue_number,
            hint=fix,
        )
    return "\n".join(lines[first + 1 : close])


# --------------------------------------------------------------------------- parsing


def parse_body(
    body: str, *, issue_number: int | None = None, milestone: str | None = None
) -> TaskMetadata | None:
    """Parse the Forge block of an issue body.

    Returns ``None`` when the body has no markers (not a Forge task). Raises
    :class:`TaskBodyError` for a malformed block or invalid fields. ``milestone`` is the
    issue's GitHub milestone title; when given, the id's milestone prefix must match it.
    """
    _, block_text, _ = split_body(body, issue_number=issue_number)
    if block_text is None:
        return None
    text = _extract_yaml(block_text, issue_number)
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise TaskBodyError(
            f"invalid YAML{where}: {problem}",
            issue_number=issue_number,
            hint="fix the YAML syntax (check indentation, colons and quotes)",
        ) from exc
    if not isinstance(loaded, dict):
        raise TaskBodyError(
            f"the YAML block must be a mapping, got {type_name(loaded)}",
            issue_number=issue_number,
            hint="write the metadata as `key: value` pairs (see spec §7.1)",
        )
    data = {str(k): v for k, v in loaded.items()}
    try:
        return _parse_fields(Fields(data, _where(issue_number)), milestone)
    except ConfigError as exc:
        reason = exc.problem if exc.key is None else f"{exc.key}: {exc.problem}"
        raise TaskBodyError(reason, issue_number=issue_number, hint=exc.hint) from exc


def _required_choice(f: Fields, key: str, values: Sequence[str]) -> str:
    f.str_(key)  # raises "missing" / wrong-type errors
    return f.choice(key, values, default=values[0])


def _parse_fields(f: Fields, milestone: str | None) -> TaskMetadata:
    f.reject_unknown(_KEYS)

    task_id = f.str_("id")
    if TASK_ID_RE.fullmatch(task_id) is None:
        raise f.fail(
            "id", f"invalid task id {task_id!r}", "use the form `M<n>-T<n>`, e.g. `M1-T07`"
        )
    prefix = task_id.partition("-")[0]
    if milestone is not None and prefix != milestone:
        raise f.fail(
            "id",
            f"id {task_id!r} belongs to milestone {prefix} but the issue is in milestone "
            f"{milestone}",
            f"move the issue to milestone {prefix} or fix the id",
        )

    spec = f.str_("spec")
    path = PurePosixPath(spec)
    if not spec.strip() or path.is_absolute() or ".." in path.parts or "\\" in spec:
        raise f.fail(
            "spec",
            f"{spec!r} is not a relative path",
            "write a path relative to the repo root, e.g. `specs/m1/editor.md`",
        )

    covers = f.str_list("covers")
    for i, ac in enumerate(covers):
        if AC_ID_RE.fullmatch(ac) is None:
            raise f.fail(f"covers[{i}]", f"invalid acceptance criterion id {ac!r}", "use `AC-<n>`")

    depends_on = f.str_list("depends_on")
    seen: set[str] = set()
    for i, dep in enumerate(depends_on):
        key = f"depends_on[{i}]"
        if TASK_ID_RE.fullmatch(dep) is None:
            raise f.fail(key, f"invalid task id {dep!r}", "use the form `M<n>-T<n>`")
        if dep == task_id:
            raise f.fail(key, "a task cannot depend on itself", f"remove {dep} from depends_on")
        if dep in seen:
            raise f.fail(key, f"duplicate dependency {dep}", "list each dependency once")
        seen.add(dep)

    zones = f.str_list("zones")
    for i, zone in enumerate(zones):
        if not zone.strip():
            raise f.fail(f"zones[{i}]", "empty zone name", "remove it or name a zone")

    raw_size = f.raw("size")
    if isinstance(raw_size, str) and raw_size.strip().upper() == "L":
        raise f.fail("size", "size L is too large", "split the task into S or M tasks (§9.4)")
    size = Size(_required_choice(f, "size", [s.value for s in Size]))
    tier = Tier(_required_choice(f, "tier", [t.value for t in Tier]))
    task_type = TaskType(_required_choice(f, "type", [t.value for t in TaskType]))

    done_when = f.str_list("done_when", non_empty=True)
    for i, item in enumerate(done_when):
        if not item.strip():
            raise f.fail(f"done_when[{i}]", "empty item", "describe a verifiable condition")

    return TaskMetadata(
        id=task_id,
        spec=spec,
        covers=tuple(covers),
        depends_on=tuple(depends_on),
        zones=tuple(zones),
        size=size,
        tier=tier,
        type=task_type,
        done_when=tuple(done_when),
        allow_contract_change=f.bool_("allow_contract_change", default=False),
    )


# --------------------------------------------------------------------------- rendering


def _same(doc: str, expected: Any) -> bool:
    try:
        return bool(yaml.safe_load(doc) == expected)
    except yaml.YAMLError:
        return False


def _scalar(value: str, *, context: str) -> str:
    """Render ``value`` plain when YAML reads it back unchanged, else double-quoted."""
    if value and "\n" not in value and "\r" not in value and value == value.strip():
        if context == "flow":
            ok = _same(f"k: [{value}]", {"k": [value]})
        elif context == "block":
            ok = _same(f"k:\n  - {value}\n", {"k": [value]})
        else:
            ok = _same(f"k: {value}", {"k": value})
        if ok:
            return value
    return json.dumps(value, ensure_ascii=False)


def render_metadata(meta: TaskMetadata) -> str:
    """Render ``meta`` as the fenced YAML block (no markers), ending with a newline.

    Keys come in spec §7.1 order; ``allow_contract_change`` is written only when true.
    """

    def flow(items: Sequence[str]) -> str:
        return "[" + ", ".join(_scalar(x, context="flow") for x in items) + "]"

    lines = [
        "```yaml",
        f"id: {meta.id}",
        f"spec: {_scalar(meta.spec, context='plain')}",
        f"covers: {flow(meta.covers)}",
        f"depends_on: {flow(meta.depends_on)}",
        f"zones: {flow(meta.zones)}",
        f"size: {meta.size.value}",
        f"tier: {meta.tier.value}",
        f"type: {meta.type.value}",
        "done_when:",
        *(f"  - {_scalar(item, context='block')}" for item in meta.done_when),
    ]
    if meta.allow_contract_change:
        lines.append("allow_contract_change: true")
    lines.append("```")
    return "\n".join(lines) + "\n"


def _render_block(meta: TaskMetadata, newline: str) -> str:
    """Begin marker through end marker (no trailing newline), using ``newline``."""
    inner = render_metadata(meta).rstrip("\n").split("\n")
    return newline.join([BEGIN_MARKER, *inner, END_MARKER])


def upsert_block(body: str, meta: TaskMetadata, *, issue_number: int | None = None) -> str:
    """Return ``body`` with its Forge block replaced by (or appended with) ``meta``.

    Text outside the markers is preserved byte for byte. A missing block is appended after a
    blank line. The line terminator of the inserted block follows the body (``\\r\\n`` if the
    body uses it). An existing block whose YAML is invalid is replaced, but one that holds
    anything besides whitespace and a single ```yaml fence is not: rather than destroy human
    notes, :class:`TaskBodyError` is raised. It is also raised on unbalanced or repeated
    markers.

    When the body ends inside an unclosed code fence, a matching closing fence line is added
    first (the only change to the human text); otherwise the appended block would be quoted
    by that fence and the result would not parse back.
    """
    newline = "\r\n" if "\r\n" in body else "\n"
    block = _render_block(meta, newline)
    span = _find_span(body, issue_number)
    if span is not None:
        inner = body[span.inner_start : span.inner_end]
        if inner.strip():
            try:
                _extract_yaml(inner, issue_number)
            except TaskBodyError as exc:
                raise TaskBodyError(
                    f"refusing to overwrite the block: {exc.reason}",
                    issue_number=issue_number,
                    hint="move your notes outside the forge markers; only one ```yaml block "
                    "may sit between them",
                ) from exc
        return body[: span.start] + block + body[span.end :]
    open_fence = _scan(body).open_fence
    if open_fence is not None:
        if not body.endswith("\n"):
            body += newline
        body += open_fence.closer + newline
    if not body:
        return block + newline
    if body.endswith(newline + newline):
        sep = ""
    elif body.endswith(newline):
        sep = newline
    else:
        sep = newline + newline
    return body + sep + block + newline


# --------------------------------------------------------------------------- labels


class Status(StrEnum):
    """Workflow status of a task, mirrored as a ``status:*`` label (spec §7.2)."""

    PROPOSED = "proposed"
    READY = "ready"
    BLOCKED = "blocked"
    IN_PROGRESS = "in-progress"
    HANDOFF = "handoff"
    IN_REVIEW = "in-review"
    DONE = "done"

    @property
    def label(self) -> str:
        """The label name, e.g. ``status:in-progress``."""
        return f"status:{self.value}"

    @classmethod
    def from_labels(cls, labels: Iterable[str]) -> Status | None:
        """Return the status of the first recognised ``status:*`` label, or ``None``.

        Label names are compared case-insensitively, as on GitHub.
        """
        by_label = {s.label.casefold(): s for s in cls}
        for name in labels:
            status = by_label.get(name.casefold())
            if status is not None:
                return status
        return None


LabelSpec = Label
"""A label Forge owns: ``name``, ``color`` (six hex digits) and ``description``."""

_STATUS_LABELS: dict[Status, tuple[str, str]] = {
    Status.PROPOSED: ("d4c5f9", "Forge: proposed by an agent, awaiting the lead"),
    Status.READY: ("0e8a16", "Forge: ready to be claimed"),
    Status.BLOCKED: ("b60205", "Forge: waiting on open dependencies"),
    Status.IN_PROGRESS: ("fbca04", "Forge: claimed and being worked on"),
    Status.HANDOFF: ("f9d0c4", "Forge: handed off, waiting for someone to resume"),
    Status.IN_REVIEW: ("1d76db", "Forge: pull request open"),
    Status.DONE: ("5319e7", "Forge: merged and closed"),
}

LABELS: tuple[LabelSpec, ...] = (
    LabelSpec(FORGE_TASK_LABEL, "5319e7", "Forge: task managed by Forge"),
    *(LabelSpec(s.label, color, desc) for s, (color, desc) in _STATUS_LABELS.items()),
    LabelSpec("size:S", "c2e0c6", "Forge: small task"),
    LabelSpec("size:M", "bfd4f2", "Forge: medium task"),
    LabelSpec("tier:strong", "5319e7", "Forge: needs a strong model"),
    LabelSpec("tier:standard", "1d76db", "Forge: needs a standard model"),
    LabelSpec("tier:light", "c5def5", "Forge: a light model is enough"),
    LabelSpec("type:contract", "d93f0b", "Forge: shared contract (types, API, schema)"),
    LabelSpec("type:impl", "0052cc", "Forge: implementation"),
    LabelSpec("type:test", "0e8a16", "Forge: tests"),
    LabelSpec("type:chore", "ededed", "Forge: chore"),
    LabelSpec(BLOCKED_CONFLICT, "b60205", "Forge: sync conflict escalated to a human"),
)
"""Every label Forge owns (ADR 0008)."""

managed_label_prefixes: tuple[str, ...] = ("status:", "size:", "tier:", "type:")
"""Prefixes of the label families Forge mirrors. Only catalogue names are ever changed."""

_MIRRORED: frozenset[str] = frozenset(
    spec.name
    for spec in LABELS
    if spec.name.startswith(managed_label_prefixes) or spec.name == FORGE_TASK_LABEL
)
_STATUS_NAMES: frozenset[str] = frozenset(s.label.casefold() for s in Status)
_META_NAMES: frozenset[str] = (
    frozenset(n.casefold() for n in _MIRRORED) - _STATUS_NAMES - {FORGE_TASK_LABEL.casefold()}
)


def has_label(labels: Iterable[str], name: str) -> bool:
    """True when ``labels`` contains ``name``, compared case-insensitively as on GitHub."""
    wanted = name.casefold()
    return any(label.casefold() == wanted for label in labels)


def metadata_labels(meta: TaskMetadata) -> set[str]:
    """Labels mirroring the metadata: ``forge:task`` plus size, tier and type."""
    return {
        FORGE_TASK_LABEL,
        f"size:{meta.size.value}",
        f"tier:{meta.tier.value}",
        f"type:{meta.type.value}",
    }


@dataclass(frozen=True)
class LabelDiff:
    """Labels to add to and remove from an issue."""

    add: frozenset[str]
    remove: frozenset[str]

    @property
    def empty(self) -> bool:
        """True when nothing changes."""
        return not self.add and not self.remove


def desired_labels(
    current: Iterable[str], status: Status | None, meta: TaskMetadata | None
) -> LabelDiff:
    """Compute the label changes that align an issue with ``status`` and ``meta``.

    Only catalogue labels of the ``status:*``, ``size:*``, ``tier:*`` and ``type:*`` families
    and ``forge:task`` are touched; ``blocked:conflict`` and user labels never are. The result
    leaves at most one status label. ``status=None`` removes every status label. With
    ``meta=None`` the size/tier/type mirrors and ``forge:task`` are left alone.

    Names are compared case-insensitively, as on GitHub: ``add`` uses the catalogue spelling
    and ``remove`` the label's current spelling (e.g. ``Status:Ready``).
    """
    have = list(dict.fromkeys(current))
    have_folded = {name.casefold() for name in have}
    want = {status.label} if status is not None else set()
    managed = set(_STATUS_NAMES)
    if meta is not None:
        want |= metadata_labels(meta)
        managed |= _META_NAMES
    want_folded = {name.casefold() for name in want}
    add = {name for name in want if name.casefold() not in have_folded}
    remove = {
        name for name in have if name.casefold() in managed and name.casefold() not in want_folded
    }
    return LabelDiff(add=frozenset(add), remove=frozenset(remove))


# --------------------------------------------------------------------------- branch events


class BranchEvent(StrEnum):
    """The Forge event recorded by a commit trailer on a task branch (ADR 0008)."""

    CLAIM = "claim"
    HANDOFF = "handoff"
    RELEASE = "release"


_EVENT_TRAILERS: dict[str, BranchEvent] = {
    TRAILER_CLAIM.lower(): BranchEvent.CLAIM,
    TRAILER_HANDOFF.lower(): BranchEvent.HANDOFF,
    TRAILER_RELEASE.lower(): BranchEvent.RELEASE,
}


@dataclass(frozen=True)
class ForgeEvent:
    """The last Forge event of a branch: who triggered it and, for a claim, with which agent."""

    event: BranchEvent
    author: str
    agent: str | None
    task: str | None


@dataclass(frozen=True)
class BranchState:
    """Remote state of ``task/<n>``: whether it exists, its tip and its last Forge event."""

    exists: bool
    tip: str | None = None
    last_event: BranchEvent | None = None
    event_author: str | None = None
    event_agent: str | None = None

    @classmethod
    def absent(cls) -> BranchState:
        """A branch that does not exist."""
        return cls(exists=False)

    @classmethod
    def from_event(cls, tip: str, event: ForgeEvent | None) -> BranchState:
        """An existing branch at ``tip`` whose last Forge event is ``event``."""
        if event is None:
            return cls(exists=True, tip=tip)
        return cls(True, tip, event.event, event.author, event.agent)


def last_event_from_trailers(
    commits: Sequence[Mapping[str, str]], *, task_id: str | None = None
) -> ForgeEvent | None:
    """Return the newest Forge event among ``commits`` (trailer dicts, newest first).

    Trailer keys are matched case-insensitively. Commits without an event trailer are
    skipped, and so are malformed commits carrying more than one event trailer. When
    ``task_id`` is given, commits whose ``Forge-Task`` value is missing or differs from it
    (case-sensitively) are skipped too.
    """
    for trailers in commits:
        lowered = {k.strip().lower(): v.strip() for k, v in trailers.items()}
        if task_id is not None and lowered.get(TRAILER_TASK.lower()) != task_id:
            continue
        found = [(ev, lowered[key]) for key, ev in _EVENT_TRAILERS.items() if key in lowered]
        if len(found) != 1:
            continue
        event, author = found[0]
        agent = lowered.get(TRAILER_AGENT.lower()) or None
        task = lowered.get(TRAILER_TASK.lower()) or None
        return ForgeEvent(event, author, agent, task)
    return None


# --------------------------------------------------------------------------- derived status


@dataclass(frozen=True)
class Derived:
    """Result of :func:`derive_status`: the status (``None`` = no status label) and warnings."""

    status: Status | None
    warnings: tuple[str, ...] = ()


def derive_status(
    *,
    issue: Issue,
    meta: TaskMetadata | None,
    deps_closed: Mapping[str, bool],
    branch: BranchState,
    has_open_pr: bool,
) -> Derived:
    """Derive a task's status from the observed state (ADR 0008, rules 1-7; first match wins).

    1. closed → ``done`` (``None`` when ``state_reason`` is ``not_planned``);
    2. ``status:proposed`` label → ``proposed`` (sticky);
    3. open PR from ``task/<n>`` → ``in-review``;
    4. branch exists, last event claim → ``in-progress``;
    5. branch exists, last event handoff → ``handoff``;
    6. a dependency not closed → ``blocked`` (ids missing from ``deps_closed`` count as not
       closed, with a warning);
    7. otherwise → ``ready``.
    """
    if issue.state == "closed":
        return Derived(None if issue.state_reason == "not_planned" else Status.DONE)
    if has_label(issue.labels, Status.PROPOSED.label):
        return Derived(Status.PROPOSED)
    if has_open_pr:
        return Derived(Status.IN_REVIEW)
    if branch.exists and branch.last_event is BranchEvent.CLAIM:
        return Derived(Status.IN_PROGRESS)
    if branch.exists and branch.last_event is BranchEvent.HANDOFF:
        return Derived(Status.HANDOFF)

    warnings: list[str] = []
    who = f"issue #{issue.number}" + (f" ({meta.id})" if meta is not None else "")
    if branch.exists and branch.last_event is None:
        warnings.append(
            f"{who}: branch {TaskMetadata.branch_name(issue.number)} has no Forge event; "
            "it does not count as a claim"
        )
    blocked = False
    for dep in meta.depends_on if meta is not None else ():
        closed = deps_closed.get(dep)
        if closed is None:
            warnings.append(f"{who}: unknown dependency {dep}; treated as not closed")
            blocked = True
        elif not closed:
            blocked = True
    return Derived(Status.BLOCKED if blocked else Status.READY, tuple(warnings))
