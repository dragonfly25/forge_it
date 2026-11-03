"""Task context: everything an agent needs to start on a task (spec §8.1, ``forge task show``).

:func:`build_context` assembles a :class:`TaskContext` from inputs the caller has already
fetched (the issue, its metadata and comments, the other task issues, the zones config); it
only reads local files under the repository root (the spec and the ADRs) and never touches the
network. :func:`render_context` turns it into Markdown-ish text for the terminal and
:meth:`TaskContext.to_data` into the JSON ``data`` payload.

:func:`extract_acceptance_criteria` is a deliberately small extractor; Phase 7's
``core/spec.py`` is expected to reuse or replace it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from forge.core.claim import resolve_task_ref
from forge.core.config import ForgeConfig
from forge.core.errors import ForgeError
from forge.core.git import Git
from forge.core.github import Comment, GitHubClient, Issue
from forge.core.lease import describe_lease, load_task
from forge.core.refresh import TaskRecord, parse_records
from forge.core.tasks import FORGE_TASK_LABEL, TASK_ID_RE, Status, TaskMetadata, split_body
from forge.core.zones import ZonesConfig

HANDOFF_MARKER = "<!-- forge:handoff -->"
"""Marker that starts a handoff note comment (spec §8.3)."""

DECISIONS_DIR = Path("docs/decisions")
"""Directory of the project's ADRs, relative to the repository root."""

UNKNOWN_STATUS = "unknown"

_AC_DEF_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?:(?P<heading>#{1,6})[ \t]+|(?P<bullet>[-*+]|\d{1,9}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?)?"
    r"(?:\*\*|__|\*|_|`)?"
    r"(?P<id>AC-\d+)(?![\w-])"
)
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]")
_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+")


# -- acceptance criteria ---------------------------------------------------------------------


def _indent(line: str) -> int:
    return len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())


def _content_lines(text: str) -> list[str | None]:
    """Return the lines of ``text`` with front matter and fenced code replaced by None."""
    lines = text.splitlines()
    out: list[str | None] = list(lines)
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() in ("---", "..."):
                out[: i + 1] = [None] * (i + 1)
                start = i + 1
                break
    fence: str | None = None
    for i in range(start, len(lines)):
        match = _FENCE_RE.match(lines[i])
        if fence is None:
            if match:
                fence = match.group(1)
                out[i] = None
        else:
            out[i] = None
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
    return out


def _section_end(
    lines: Sequence[str | None], start: int, *, heading: str | None, bullet: bool
) -> int:
    """Index just past the block that starts at ``start``."""
    n = len(lines)
    if heading is not None:
        level = len(heading)
        for j in range(start + 1, n):
            line = lines[j]
            if line is not None:
                match = _HEADING_RE.match(line)
                if match and len(match.group(1)) <= level:
                    return j
        return n
    first = lines[start] or ""
    if bullet:
        base = _indent(first)
        end = start + 1
        j = start + 1
        while j < n:
            line = lines[j]
            if line is None or not line.strip():
                # a blank line continues the item only if indented content follows
                k = j + 1
                while k < n and (lines[k] is None or not (lines[k] or "").strip()):
                    k += 1
                if k < n and _indent(lines[k] or "") > base:
                    j = k
                    continue
                break
            if _indent(line) <= base:
                break
            end = j + 1
            j += 1
        return end
    # paragraph: until a blank line, a heading or a list item
    for j in range(start + 1, n):
        line = lines[j]
        if line is None or not line.strip() or _HEADING_RE.match(line) or _LIST_ITEM_RE.match(line):
            return j
    return n


def extract_acceptance_criteria(text: str, ids: Iterable[str]) -> dict[str, str]:
    """Return ``{ac_id: definition}`` for each id of ``ids`` defined in the Markdown ``text``.

    An AC is *defined* where its id first appears at the start of a line, optionally after a
    heading marker (``### AC-3 …``) or a list marker (``- AC-3: …``, ``1. **AC-3** …``), and
    optionally wrapped in emphasis or backticks. Mentions inside a sentence do not count, nor
    does anything in YAML front matter or fenced code. The definition is:

    - for a heading: the heading and its section, up to the next heading of the same or a
      higher level;
    - for a list item: the item with its indented continuation lines;
    - otherwise: the paragraph.

    Ids that are not found are absent from the result. ``AC-1`` never matches ``AC-10``.
    """
    order = list(ids)
    wanted = set(order)
    found: dict[str, str] = {}
    raw = text.splitlines()
    lines = _content_lines(text)
    for i, line in enumerate(lines):
        if line is None:
            continue
        match = _AC_DEF_RE.match(line)
        if match is None:
            continue
        ac = match.group("id")
        if ac not in wanted or ac in found:
            continue
        end = _section_end(
            lines, i, heading=match.group("heading"), bullet=match.group("bullet") is not None
        )
        found[ac] = "\n".join(ln.rstrip() for ln in raw[i:end]).strip()
    return {ac: found[ac] for ac in order if ac in found}


def defined_acceptance_criteria(text: str) -> list[str]:
    """Return every AC id *defined* in the Markdown ``text``, once each, in order of appearance.

    "Defined" has the meaning of :func:`extract_acceptance_criteria`: the id starts a line
    (optionally after a heading or list marker and emphasis); mentions inside a sentence,
    front matter and fenced code do not count.
    """
    ids: dict[str, None] = {}
    for line in _content_lines(text):
        if line is None:
            continue
        match = _AC_DEF_RE.match(line)
        if match is not None:
            ids.setdefault(match.group("id"), None)
    return list(ids)


# -- handoff notes ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HandoffNote:
    """The newest handoff comment of an issue (spec §8.3); ``body`` is without the marker."""

    author: str
    created_at: datetime
    body: str
    comment_id: int

    def to_data(self) -> dict[str, Any]:
        """Return a JSON-able dict."""
        return {
            "author": self.author,
            "created_at": _ts(self.created_at),
            "body": self.body,
            "comment_id": self.comment_id,
        }


def latest_handoff(comments: Iterable[Comment]) -> HandoffNote | None:
    """Return the newest comment that contains :data:`HANDOFF_MARKER` (ties: highest id)."""
    notes = [c for c in comments if HANDOFF_MARKER in c.body]
    if not notes:
        return None
    newest = max(notes, key=lambda c: (c.created_at, c.id))
    body = newest.body.replace(HANDOFF_MARKER, "", 1).strip()
    return HandoffNote(newest.author, newest.created_at, body, newest.id)


# -- ADRs ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AdrRef:
    """An ADR relevant to the task: its path (relative, POSIX), title and why it matched."""

    path: str
    title: str
    matched: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        """Return a JSON-able dict."""
        return {"path": self.path, "title": self.title, "matched": list(self.matched)}


def _mentions(text: str, term: str) -> bool:
    return re.search(rf"(?<![\w/.-]){re.escape(term)}(?![\w/-])", text) is not None


def find_relevant_adrs(
    repo_root: Path, *, task_id: str, spec_path: str | None, zones: Iterable[str]
) -> list[AdrRef]:
    """Return the ADRs under ``docs/decisions/`` that mention the task id, spec path or a zone.

    The template (``0000-*.md``) is skipped. Matching is literal with word-like boundaries, so
    ``M1-T1`` does not match ``M1-T10``. The title is the first ``# `` heading, or the file
    stem. Results are sorted by file name.
    """
    directory = repo_root / DECISIONS_DIR
    if not directory.is_dir():
        return []
    terms = [task_id, *([spec_path] if spec_path else []), *zones]
    refs: list[AdrRef] = []
    for file in sorted(directory.glob("*.md")):
        if file.name.startswith("0000"):
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        matched = tuple(dict.fromkeys(t for t in terms if _mentions(text, t)))
        if not matched:
            continue
        title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), file.stem)
        refs.append(AdrRef((DECISIONS_DIR / file.name).as_posix(), title, matched))
    return refs


# -- context ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ZoneInfo:
    """A zone of the task with its path globs; ``known`` is False when zones.yml lacks it."""

    name: str
    paths: tuple[str, ...]
    contract: bool
    known: bool

    def to_data(self) -> dict[str, Any]:
        """Return a JSON-able dict."""
        return {
            "name": self.name,
            "paths": list(self.paths),
            "contract": self.contract,
            "known": self.known,
        }


@dataclass(frozen=True)
class DependencySummary:
    """One ``depends_on`` entry. ``issue``/``title``/``state`` are None when it is not found."""

    id: str
    issue: int | None
    title: str | None
    state: str | None
    status: str

    def to_data(self) -> dict[str, Any]:
        """Return a JSON-able dict."""
        return {
            "id": self.id,
            "issue": self.issue,
            "title": self.title,
            "state": self.state,
            "status": self.status,
        }


@dataclass
class TaskContext:
    """Everything ``forge task show`` prints. ``lease`` is filled by the caller (claim/lease)."""

    meta: TaskMetadata
    issue: Issue
    body: str
    acceptance_criteria: dict[str, str]
    zones: list[ZoneInfo]
    dependencies: list[DependencySummary]
    handoff: HandoffNote | None
    adrs: list[AdrRef]
    lease: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def to_data(self) -> dict[str, Any]:
        """Return the JSON-able ``data`` payload."""
        return {
            "task": self.meta.id,
            "metadata": metadata_dict(self.meta),
            "issue": {
                "number": self.issue.number,
                "title": self.issue.title,
                "url": self.issue.url,
                "state": self.issue.state,
                "labels": list(self.issue.labels),
                "assignees": list(self.issue.assignees),
            },
            "body": self.body,
            "spec": {
                "path": self.meta.spec,
                "acceptance_criteria": [
                    {"id": ac, "text": self.acceptance_criteria.get(ac)} for ac in self.meta.covers
                ],
            },
            "zones": [z.to_data() for z in self.zones],
            "dependencies": [d.to_data() for d in self.dependencies],
            "handoff": None if self.handoff is None else self.handoff.to_data(),
            "adrs": [a.to_data() for a in self.adrs],
            "lease": self.lease,
            "warnings": list(self.warnings),
        }


def _ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    return value


def metadata_dict(meta: TaskMetadata) -> dict[str, Any]:
    """Return the metadata block as a JSON-able dict, keys in spec §7.1 order."""
    return {f.name: _plain(getattr(meta, f.name)) for f in fields(meta)}


def _human_body(issue: Issue) -> str:
    before, _block, after = split_body(issue.body, issue_number=issue.number)
    return (before.rstrip() + "\n\n" + after.strip()).strip() if after.strip() else before.strip()


def _spec_criteria(repo_root: Path, meta: TaskMetadata, warnings: list[str]) -> dict[str, str]:
    if not meta.covers:
        return {}
    spec_file = repo_root / meta.spec
    try:
        text = spec_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        warnings.append(f"spec {meta.spec} not found; acceptance criteria not shown")
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        warnings.append(
            f"spec {meta.spec} could not be read ({exc}); acceptance criteria not shown"
        )
        return {}
    found = extract_acceptance_criteria(text, meta.covers)
    for ac in meta.covers:
        if ac not in found:
            warnings.append(f"{ac} is not defined in {meta.spec}")
    return found


def _zone_infos(
    meta: TaskMetadata, zones: ZonesConfig | None, warnings: list[str]
) -> list[ZoneInfo]:
    infos: list[ZoneInfo] = []
    for name in meta.zones:
        zone = None if zones is None else zones.zones.get(name)
        if zone is None:
            if zones is not None:
                warnings.append(f"zone {name!r} is not defined in .forge/zones.yml")
            infos.append(ZoneInfo(name, (), False, False))
        else:
            infos.append(ZoneInfo(name, zone.paths, zone.contract, True))
    if zones is None and meta.zones:
        warnings.append("no zones config loaded; zone paths not shown")
    return infos


def _dependencies(
    meta: TaskMetadata, tasks: Mapping[str, Issue], warnings: list[str]
) -> list[DependencySummary]:
    deps: list[DependencySummary] = []
    for dep_id in meta.depends_on:
        dep = tasks.get(dep_id)
        if dep is None:
            warnings.append(f"dependency {dep_id} not found among the task issues")
            deps.append(DependencySummary(dep_id, None, None, None, UNKNOWN_STATUS))
            continue
        status = Status.from_labels(dep.labels)
        if dep.state == "closed":
            # A closed issue is done whatever its labels say (ADR 0008, rule 1).
            status = None if dep.state_reason == "not_planned" else Status.DONE
            if status is None:
                deps.append(DependencySummary(dep_id, dep.number, dep.title, dep.state, "dropped"))
                continue
        if status is None:
            warnings.append(f"dependency {dep_id} (#{dep.number}) has no status label")
        deps.append(
            DependencySummary(
                dep_id,
                dep.number,
                dep.title,
                dep.state,
                UNKNOWN_STATUS if status is None else status.value,
            )
        )
    return deps


def build_context(
    issue: Issue,
    meta: TaskMetadata,
    *,
    comments: Sequence[Comment],
    tasks: Mapping[str, Issue],
    repo_root: Path,
    zones: ZonesConfig | None,
    lease: dict[str, Any] | None = None,
) -> TaskContext:
    """Assemble the task context from already-fetched inputs (no network access).

    ``tasks`` maps task ids to their issues (the caller resolves duplicate ids). Missing spec
    files, undefined ACs, unknown zones and unknown dependencies become warnings, never errors.
    """
    warnings: list[str] = []
    criteria = _spec_criteria(repo_root, meta, warnings)
    zone_infos = _zone_infos(meta, zones, warnings)
    deps = _dependencies(meta, tasks, warnings)
    adrs = find_relevant_adrs(repo_root, task_id=meta.id, spec_path=meta.spec, zones=meta.zones)
    return TaskContext(
        meta=meta,
        issue=issue,
        body=_human_body(issue),
        acceptance_criteria=criteria,
        zones=zone_infos,
        dependencies=deps,
        handoff=latest_handoff(comments),
        adrs=adrs,
        lease=lease,
        warnings=warnings,
    )


# -- rendering -------------------------------------------------------------------------------


def _render_lease(lease: Mapping[str, Any]) -> list[str]:
    return [f"- {key}: {value}" for key, value in lease.items() if value is not None]


def render_context(ctx: TaskContext) -> str:
    """Render ``ctx`` as Markdown-ish plain text for the terminal."""
    meta, issue = ctx.meta, ctx.issue
    out: list[str] = [f"# #{issue.number} {issue.title}", ""]
    out.append(f"{issue.url}")
    out.append(
        f"State: {issue.state} | Labels: {', '.join(issue.labels) or '-'} | "
        f"Assignees: {', '.join(issue.assignees) or '-'}"
    )
    out += ["", "## Metadata"]
    for key, value in metadata_dict(meta).items():
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value) or "-"
        out.append(f"- {key}: {value}")
    out += ["", "## Description", ctx.body or "(empty)"]
    out += ["", f"## Acceptance criteria ({meta.spec})"]
    if not meta.covers:
        out.append("(none covered)")
    for ac in meta.covers:
        out.append(ctx.acceptance_criteria.get(ac, f"{ac}: (not found in the spec)"))
    out += ["", "## Zones"]
    if not ctx.zones:
        out.append("(none)")
    for zone in ctx.zones:
        paths = ", ".join(zone.paths) if zone.known else "(unknown zone)"
        suffix = " [contract]" if zone.contract else ""
        out.append(f"- {zone.name}{suffix}: {paths}")
    out += ["", "## Dependencies"]
    if not ctx.dependencies:
        out.append("(none)")
    for dep in ctx.dependencies:
        if dep.issue is None:
            out.append(f"- {dep.id}: not found")
        else:
            out.append(f"- {dep.id} #{dep.issue} {dep.title} ({dep.state}, {dep.status})")
    if ctx.lease is not None:
        out += ["", "## Lease", *_render_lease(ctx.lease)]
    if ctx.handoff is not None:
        note = ctx.handoff
        out += ["", f"## Latest handoff note (by {note.author}, {_ts(note.created_at)})", note.body]
    out += ["", "## Relevant ADRs"]
    if not ctx.adrs:
        out.append("(none)")
    for adr in ctx.adrs:
        out.append(f"- {adr.path}: {adr.title}")
    if ctx.warnings:
        out += ["", "## Warnings", *(f"- {w}" for w in ctx.warnings)]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- gathering


def _resolve_any(ref: str, records: Sequence[TaskRecord]) -> int | None:
    """Like ``resolve_task_ref`` (open tasks first), but also finds closed tasks."""
    number = resolve_task_ref(ref, records)  # raises on a malformed reference
    if number is not None:
        return number
    text = ref.strip()
    if TASK_ID_RE.fullmatch(text):
        closed = sorted(r.issue.number for r in records if r.meta.id == text)
        return closed[0] if closed else None
    return int(text.lstrip("#"))


def gather_task_context(
    github: GitHubClient,
    git: Git,
    config: ForgeConfig,
    ref: str | int,
    *,
    repo_root: Path,
    zones: ZonesConfig | None,
    now: datetime,
    remote: str = "origin",
) -> TaskContext:
    """Fetch an issue (``M1-T07``, ``#42``, ``42`` or a number) and build its task context.

    Read-only: it lists GitHub data and fetches the task branches, never pushes or edits.
    """
    warnings: list[str] = []
    issues = github.list_issues(state="all", labels=[FORGE_TASK_LABEL])
    records = parse_records(issues, warnings)
    if isinstance(ref, int):
        number: int | None = ref
    else:
        number = _resolve_any(ref, records)
    if number is None:
        raise ForgeError(
            f"{ref}: no Forge task matches",
            hint="use a task id (M1-T07) or an issue number (#42 or 42)",
        )
    record = load_task(github, number)
    tasks: dict[str, Issue] = {}
    for rec in records:
        tasks.setdefault(rec.meta.id, rec.issue)
    comments = github.list_comments(number)
    lease = describe_lease(git, config, number, record.meta.id, comments, now=now, remote=remote)
    context = build_context(
        record.issue,
        record.meta,
        comments=comments,
        tasks=tasks,
        repo_root=repo_root,
        zones=zones,
        lease=lease,
    )
    return context
