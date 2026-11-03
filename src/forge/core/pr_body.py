"""The Forge block of a task pull request body (spec §11.1, §6.5.7, ADR 0012). Pure.

Forge owns the text between :data:`BEGIN_MARKER` and :data:`END_MARKER`; everything outside
belongs to humans and is kept byte for byte. On update, a checklist item that was ticked keeps
its tick when its text is unchanged, so ship never unticks what a reviewer checked.

Block sections, in order: header (``Closes #n``, task id, spec path), acceptance criteria,
done when, out-of-zone changes, conflict resolutions (from ``sync-log.json``) and the agent
(id, tool, providers, resolved tier → model map).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.core.sync import BACKUP_PREFIX

BEGIN_MARKER = "<!-- forge:pr-begin -->"
END_MARKER = "<!-- forge:pr-end -->"
TEMPLATE_PATH = Path(".github/pull_request_template.md")
"""The repository's PR template, appended on creation only (read from the working tree)."""

MAX_AC_CHARS = 300
"""Longest acceptance-criterion text shown on one checklist line."""

_CHECKBOX_RE = re.compile(r"^(?P<lead>[ \t]*[-*+][ \t]+)\[(?P<mark>[ xX])\][ \t]+(?P<text>.*?)\s*$")
_AC_LEAD_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+|(?:[-*+]|\d{1,9}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?)?"
    r"(?P<open>\*\*|__|\*|_|`)?(?P<id>AC-\d+)(?P=open)?(?![\w-])"
    r"[ \t]*(?:[:.\-]|\u2014|\u2013)?[ \t]*"
)


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class OutOfZoneFile:
    """A changed file outside the task's zones; ``zones`` empty means unzoned."""

    path: str
    zones: tuple[str, ...] = ()

    @property
    def unzoned(self) -> bool:
        """True when the file matches no zone at all."""
        return not self.zones

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {"path": self.path, "zones": list(self.zones), "unzoned": self.unzoned}


@dataclass(frozen=True)
class UpstreamLink:
    """An upstream pull request a sync rebased onto."""

    number: int
    author: str | None = None


@dataclass(frozen=True)
class SyncResolutions:
    """One completed sync of the branch that had conflicts or ran with ``--force-resolve``.

    ``conflicts`` are ``(path, resolution)`` pairs: a resolver strategy (``regenerate``,
    ``union``), ``rerere`` or ``manual``.
    """

    at: str
    onto: str
    force_resolve: bool
    conflicts: tuple[tuple[str, str], ...]
    pulls: tuple[UpstreamLink, ...] = ()


@dataclass(frozen=True)
class AgentInfo:
    """The author's agent and the model resolved for the task's tier (spec §6.5.7)."""

    id: str
    tool: str
    providers: tuple[str, ...]
    tier: str
    effective_tier: str | None
    model: str | None
    effort: str
    effort_value: str | None
    tier_map: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PrBlock:
    """Everything the Forge block shows. ``criteria`` pairs each ``covers`` id with its text
    from the spec (``None`` when not found)."""

    issue: int
    task_id: str
    spec: str
    criteria: tuple[tuple[str, str | None], ...]
    done_when: tuple[str, ...]
    out_of_zone: tuple[OutOfZoneFile, ...]
    resolutions: tuple[SyncResolutions, ...]
    agent: AgentInfo


# --------------------------------------------------------------------------- inputs


def ac_text(ac_id: str, definition: str) -> str:
    """One-line text of an AC definition (``extract_acceptance_criteria`` output).

    The leading heading or list marker and the id itself are dropped, whitespace is collapsed
    and the result is capped at :data:`MAX_AC_CHARS`.
    """
    text = definition.strip()
    match = _AC_LEAD_RE.match(text)
    if match is not None and match.group("id") == ac_id:
        text = text[match.end() :]
    text = " ".join(text.split())
    if len(text) > MAX_AC_CHARS:
        text = text[: MAX_AC_CHARS - 1].rstrip() + "…"
    return text


def resolutions_from_log(entries: Iterable[Mapping[str, Any]], issue: int) -> list[SyncResolutions]:
    """The syncs of ``task/<issue>`` worth listing in the PR, oldest first.

    An entry counts when its id is a backup ref of this task, it was pushed, and it had
    conflicts or ran with ``--force-resolve``. Conflicts repeated across rebase stops are
    listed once per (path, resolution). Malformed entries are skipped.
    """
    prefix = f"{BACKUP_PREFIX}/task-{issue}/"
    found: list[SyncResolutions] = []
    for entry in entries:
        if not str(entry.get("id", "")).startswith(prefix) or entry.get("outcome") != "pushed":
            continue
        pairs: dict[tuple[str, str], None] = {}
        for conflict in entry.get("conflicts") or []:
            if not isinstance(conflict, Mapping) or "path" not in conflict:
                continue
            pairs[(str(conflict["path"]), str(conflict.get("resolution") or "unresolved"))] = None
        force = bool(entry.get("force_resolve"))
        if not pairs and not force:
            continue
        pulls: list[UpstreamLink] = []
        context = entry.get("upstream_context")
        for pr in (context.get("pulls") if isinstance(context, Mapping) else None) or []:
            try:
                pulls.append(UpstreamLink(int(pr["number"]), pr.get("author")))
            except (KeyError, TypeError, ValueError):
                continue
        found.append(
            SyncResolutions(
                at=str(entry.get("at", "")),
                onto=str(entry.get("onto", "")),
                force_resolve=force,
                conflicts=tuple(pairs),
                pulls=tuple(pulls),
            )
        )
    return found


# --------------------------------------------------------------------------- rendering


def _resolution(name: str) -> str:
    if name in ("manual", "rerere", "unresolved"):
        return name
    return f"resolver `{name}`"


def render_block(block: PrBlock) -> str:
    """The Forge block, markers included, ``\\n`` line endings, no trailing newline."""
    lines = [
        BEGIN_MARKER,
        f"Closes #{block.issue}",
        "",
        f"**Task** `{block.task_id}` · **Spec** `{block.spec}`",
        "",
        "### Acceptance criteria",
    ]
    if block.criteria:
        for ac_id, text in block.criteria:
            shown = f"**{ac_id}**: {text}" if text else f"**{ac_id}** (not found in the spec)"
            lines.append(f"- [ ] {shown}")
    else:
        lines.append("none")
    lines += ["", "### Done when"]
    lines += [f"- [ ] {item}" for item in block.done_when] or ["none"]
    lines += ["", "### Out-of-zone changes"]
    if block.out_of_zone:
        for item in block.out_of_zone:
            where = "unzoned" if item.unzoned else "zone " + ", ".join(item.zones)
            lines.append(f"- `{item.path}`: {where}")
    else:
        lines.append("none")
    lines += ["", "### Conflict resolutions"]
    if block.resolutions:
        for sync in block.resolutions:
            head = f"- Sync at {sync.at} onto `{sync.onto[:12]}`"
            if sync.force_resolve:
                head += " — **force-resolve**: out-of-zone or contract conflicts resolved here"
            lines.append(head)
            lines += [f"  - `{path}`: {_resolution(how)}" for path, how in sync.conflicts]
            if sync.pulls:
                links = ", ".join(
                    f"#{pr.number}" + (f" by `@{pr.author}`" if pr.author else "")
                    for pr in sync.pulls
                )
                lines.append(f"  - Upstream: {links}")
    else:
        lines.append("none")
    agent = block.agent
    lines += [
        "",
        "### Agent",
        f"- Agent `{agent.id}` ({agent.tool}), providers: "
        + (", ".join(agent.providers) or "none declared"),
    ]
    model = f"`{agent.model}`" if agent.model else "no model"
    via = (
        f" (its {agent.effective_tier} model)"
        if agent.effective_tier and agent.effective_tier != agent.tier
        else ""
    )
    effort = f"effort {agent.effort}" + (
        f" → `{agent.effort_value}`" if agent.effort_value else " (not mapped)"
    )
    lines.append(f"- Task tier {agent.tier} → {model}{via}, {effort}")
    if agent.tier_map:
        lines.append(
            "- Tier map: " + ", ".join(f"{tier} → `{m}`" for tier, m in agent.tier_map.items())
        )
    lines += [f"- Note: {note}" for note in agent.notes]
    lines.append(END_MARKER)
    return "\n".join(lines)


# --------------------------------------------------------------------------- body updates


def find_block(body: str) -> tuple[int, int] | None:
    """``(start, end)`` of the first complete Forge block in ``body`` (``end`` is just past
    the end marker), or ``None`` when there is no begin marker followed by an end marker."""
    start = body.find(BEGIN_MARKER)
    if start < 0:
        return None
    end = body.find(END_MARKER, start + len(BEGIN_MARKER))
    if end < 0:
        return None
    return start, end + len(END_MARKER)


def checked_items(text: str) -> set[str]:
    """The texts of the ticked checklist items (``- [x] text``) in ``text``."""
    found: set[str] = set()
    for line in text.splitlines():
        match = _CHECKBOX_RE.match(line)
        if match is not None and match.group("mark") in "xX":
            found.add(match.group("text"))
    return found


def keep_checked(block: str, checked: set[str]) -> str:
    """``block`` with every unticked item whose text is in ``checked`` ticked."""
    if not checked:
        return block
    out: list[str] = []
    for line in block.split("\n"):
        match = _CHECKBOX_RE.match(line)
        if match is not None and match.group("mark") == " " and match.group("text") in checked:
            line = f"{match.group('lead')}[x] {match.group('text')}"
        out.append(line)
    return "\n".join(out)


def initial_body(block: str, template: str | None) -> str:
    """The body of a new PR: the block, then the repository's PR template when there is one."""
    if template is None or not template.strip():
        return block + "\n"
    return block + "\n\n" + template


def update_body(old: str, block: str) -> str:
    """Replace the Forge block of ``old`` with ``block``; text outside the markers is kept
    byte for byte, and items ticked in the old block stay ticked when their text is unchanged.

    A body without a complete block (a PR opened by hand, or markers deleted) gets the block
    prepended, followed by a blank line and the old body unchanged.
    """
    span = find_block(old)
    if span is None:
        if not old:
            return block + "\n"
        return block + "\n\n" + old
    start, end = span
    new = keep_checked(block, checked_items(old[start:end]))
    return old[:start] + new + old[end:]


def read_template(root: Path) -> str | None:
    """The PR template under ``root`` (:data:`TEMPLATE_PATH`), or ``None`` when absent."""
    path = root / TEMPLATE_PATH
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return None
