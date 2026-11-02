"""Tests for forge.core.context: AC extraction, ADR relevance, handoff notes, task context."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from forge.core.context import (
    HANDOFF_MARKER,
    build_context,
    defined_acceptance_criteria,
    extract_acceptance_criteria,
    find_relevant_adrs,
    latest_handoff,
    metadata_dict,
    render_context,
)
from forge.core.github import Comment, Issue
from forge.core.tasks import Size, TaskMetadata, TaskType, upsert_block
from forge.core.tiers import Tier
from forge.core.zones import ZonesConfig, parse_zones

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

SPEC = """\
---
milestone: M1
status: ready
AC-9: front matter is not a definition
---
# Page builder

This paragraph mentions AC-1 and AC-9 inline only.

## Requirements
- **AC-1** A user can drag a block from the palette onto the canvas.
- AC-2: Blocks can be reordered.
  Reordering is keyboard accessible.

  Undo restores the previous order.
- AC-10: Ten is not one.

```markdown
- AC-3: this is an example in a code fence, not a definition
```

### AC-3 Saving
Pages save on Ctrl+S.

- details stay with the heading section

### AC-4 Loading
Loading is fast.

## Constraints
1. `AC-5` Paragraph-ish numbered item.
AC-6: A plain paragraph definition
that wraps.

Next paragraph.
"""


def meta(**kw: object) -> TaskMetadata:
    base: dict[str, object] = {
        "id": "M1-T07",
        "spec": "specs/m1/page-builder.md",
        "covers": ("AC-1", "AC-3"),
        "depends_on": ("M1-T01", "M1-T02"),
        "zones": ("api/pages",),
        "size": Size.M,
        "tier": Tier.STANDARD,
        "type": TaskType.IMPL,
        "done_when": ("tests pass",),
    }
    base.update(kw)
    return TaskMetadata(**base)  # type: ignore[arg-type]


def issue(
    number: int, title: str, *, body: str = "", labels: tuple[str, ...] = (), state: str = "open"
) -> Issue:
    return Issue(
        number=number,
        title=title,
        body=body,
        state="closed" if state == "closed" else "open",
        state_reason=None,
        labels=labels,
        assignees=("alice",) if number == 42 else (),
        milestone="M1",
        author="lead",
        url=f"https://example.test/issues/{number}",
        created_at=NOW,
        updated_at=NOW,
        closed_at=None,
    )


def comment(cid: int, body: str, *, minutes: int = 0, author: str = "bob") -> Comment:
    at = NOW + timedelta(minutes=minutes)
    return Comment(cid, 42, body, author, at, at)


def zones() -> ZonesConfig:
    return parse_zones(
        {
            "zones": {
                "api/pages": {"paths": ["api/pages/**"]},
                "db": {"paths": ["db/**"], "contract": True},
            }
        },
        ".forge/zones.yml",
    )


# -- acceptance criteria ---------------------------------------------------------------------


def test_extract_list_items_with_continuation() -> None:
    found = extract_acceptance_criteria(SPEC, ["AC-1", "AC-2", "AC-10"])
    assert found["AC-1"] == "- **AC-1** A user can drag a block from the palette onto the canvas."
    assert found["AC-2"] == (
        "- AC-2: Blocks can be reordered.\n"
        "  Reordering is keyboard accessible.\n\n"
        "  Undo restores the previous order."
    )
    assert found["AC-10"] == "- AC-10: Ten is not one."


def test_extract_heading_section_skips_fenced_example() -> None:
    found = extract_acceptance_criteria(SPEC, ["AC-3", "AC-4"])
    assert found["AC-3"].startswith("### AC-3 Saving\nPages save on Ctrl+S.")
    assert "details stay with the heading section" in found["AC-3"]
    assert "AC-4" not in found["AC-3"]
    assert found["AC-4"] == "### AC-4 Loading\nLoading is fast."


def test_extract_numbered_item_and_paragraph() -> None:
    found = extract_acceptance_criteria(SPEC, ["AC-5", "AC-6"])
    assert found["AC-5"] == "1. `AC-5` Paragraph-ish numbered item."
    assert found["AC-6"] == "AC-6: A plain paragraph definition\nthat wraps."


def test_extract_missing_and_inline_mentions_and_order() -> None:
    assert extract_acceptance_criteria(SPEC, ["AC-9", "AC-99"]) == {}
    assert extract_acceptance_criteria("see AC-1 for details\n", ["AC-1"]) == {}
    assert list(extract_acceptance_criteria(SPEC, iter(["AC-4", "AC-1"]))) == ["AC-4", "AC-1"]


def test_defined_acceptance_criteria_lists_definitions_once_in_order() -> None:
    assert defined_acceptance_criteria(SPEC) == [
        "AC-1",
        "AC-2",
        "AC-10",
        "AC-3",
        "AC-4",
        "AC-5",
        "AC-6",
    ]
    assert defined_acceptance_criteria("- AC-2: a\n- AC-2: again\nsee AC-7 inline\n") == ["AC-2"]
    assert defined_acceptance_criteria("") == []


# -- handoff ---------------------------------------------------------------------------------


def test_latest_handoff_newest_wins() -> None:
    comments = [
        comment(1, f"{HANDOFF_MARKER}\nold note", minutes=1),
        comment(2, "plain comment", minutes=5),
        comment(3, f"{HANDOFF_MARKER}\n## Done\nnew note", minutes=3, author="carol"),
    ]
    note = latest_handoff(comments)
    assert note is not None
    assert note.comment_id == 3 and note.author == "carol"
    assert note.body == "## Done\nnew note"
    assert latest_handoff([comment(1, "nothing")]) is None


def test_latest_handoff_tie_breaks_on_id() -> None:
    note = latest_handoff([comment(5, f"{HANDOFF_MARKER} a"), comment(4, f"{HANDOFF_MARKER} b")])
    assert note is not None and note.comment_id == 5


# -- ADRs ------------------------------------------------------------------------------------


def write_adrs(root: Path) -> None:
    d = root / "docs/decisions"
    d.mkdir(parents=True)
    (d / "0000-template.md").write_text("# Template\nM1-T07 specs/m1/page-builder.md\n")
    (d / "0001-task.md").write_text("# 0001 — Task decision\nApplies to M1-T07.\n")
    (d / "0002-other.md").write_text("# 0002 — Other\nApplies to M1-T070 and api/pages-old.\n")
    (d / "0003-spec.md").write_text("intro\n# 0003 — Spec\nSee specs/m1/page-builder.md.\n")
    (d / "0004-zone.md").write_text("Zone `api/pages` is special.\n")


def test_find_relevant_adrs(tmp_path: Path) -> None:
    write_adrs(tmp_path)
    refs = find_relevant_adrs(
        tmp_path, task_id="M1-T07", spec_path="specs/m1/page-builder.md", zones=["api/pages"]
    )
    assert [(r.path, r.title, r.matched) for r in refs] == [
        ("docs/decisions/0001-task.md", "0001 — Task decision", ("M1-T07",)),
        ("docs/decisions/0003-spec.md", "0003 — Spec", ("specs/m1/page-builder.md",)),
        ("docs/decisions/0004-zone.md", "0004-zone", ("api/pages",)),
    ]


def test_find_relevant_adrs_without_directory(tmp_path: Path) -> None:
    assert find_relevant_adrs(tmp_path, task_id="M1-T07", spec_path=None, zones=[]) == []


# -- build_context ---------------------------------------------------------------------------


def setup_repo(root: Path) -> None:
    (root / "specs/m1").mkdir(parents=True)
    (root / "specs/m1/page-builder.md").write_text(SPEC)
    write_adrs(root)


def the_issue(m: TaskMetadata) -> Issue:
    body = upsert_block("Implement save/load.\n", m) + "\nTrailing notes.\n"
    return issue(42, "[M1-T07] Save/load", body=body, labels=("forge:task", "status:in-progress"))


def test_build_context_full(tmp_path: Path) -> None:
    setup_repo(tmp_path)
    m = meta(zones=("api/pages", "ghost"), depends_on=("M1-T01", "M1-T02", "M1-T99"))
    tasks = {
        "M1-T01": issue(10, "[M1-T01] Schema", labels=("status:done",), state="closed"),
        "M1-T02": issue(11, "[M1-T02] API", labels=()),
    }
    ctx = build_context(
        the_issue(m),
        m,
        comments=[comment(1, f"{HANDOFF_MARKER}\nkeep going")],
        tasks=tasks,
        repo_root=tmp_path,
        zones=zones(),
        lease={"owner": "alice", "agent": "claude-1"},
    )
    assert ctx.body == "Implement save/load.\n\nTrailing notes."
    assert set(ctx.acceptance_criteria) == {"AC-1", "AC-3"}
    assert [(z.name, z.paths, z.known) for z in ctx.zones] == [
        ("api/pages", ("api/pages/**",), True),
        ("ghost", (), False),
    ]
    assert [(d.id, d.issue, d.state, d.status) for d in ctx.dependencies] == [
        ("M1-T01", 10, "closed", "done"),
        ("M1-T02", 11, "open", "unknown"),
        ("M1-T99", None, None, "unknown"),
    ]
    assert ctx.handoff is not None and ctx.handoff.body == "keep going"
    assert [a.path for a in ctx.adrs] == [
        "docs/decisions/0001-task.md",
        "docs/decisions/0003-spec.md",
        "docs/decisions/0004-zone.md",
    ]
    assert any("ghost" in w for w in ctx.warnings)
    assert any("M1-T02" in w and "status label" in w for w in ctx.warnings)
    assert any("M1-T99" in w for w in ctx.warnings)

    data = ctx.to_data()
    json.dumps(data)  # JSON-able
    assert data["metadata"] == metadata_dict(m)
    assert data["metadata"]["size"] == "M" and data["metadata"]["covers"] == ["AC-1", "AC-3"]
    assert data["issue"]["number"] == 42 and data["issue"]["assignees"] == ["alice"]
    assert data["spec"]["acceptance_criteria"][0]["id"] == "AC-1"
    assert data["handoff"]["created_at"] == "2026-11-02T09:00:00Z"
    assert data["lease"] == {"owner": "alice", "agent": "claude-1"}


def test_build_context_missing_spec_and_ac(tmp_path: Path) -> None:
    m = meta(covers=("AC-1",), depends_on=())
    ctx = build_context(the_issue(m), m, comments=[], tasks={}, repo_root=tmp_path, zones=zones())
    assert ctx.acceptance_criteria == {}
    assert ctx.warnings == [
        "spec specs/m1/page-builder.md not found; acceptance criteria not shown"
    ]

    setup_repo(tmp_path)
    m2 = meta(covers=("AC-1", "AC-77"), depends_on=())
    ctx2 = build_context(
        the_issue(m2), m2, comments=[], tasks={}, repo_root=tmp_path, zones=zones()
    )
    assert list(ctx2.acceptance_criteria) == ["AC-1"]
    assert ctx2.warnings == ["AC-77 is not defined in specs/m1/page-builder.md"]
    assert ctx2.to_data()["spec"]["acceptance_criteria"][1] == {"id": "AC-77", "text": None}


def test_render_context(tmp_path: Path) -> None:
    setup_repo(tmp_path)
    m = meta(zones=("api/pages", "db"), depends_on=("M1-T01",), covers=("AC-1", "AC-77"))
    ctx = build_context(
        the_issue(m),
        m,
        comments=[comment(1, f"{HANDOFF_MARKER}\nnext: wire the route", author="carol")],
        tasks={"M1-T01": issue(10, "[M1-T01] Schema", labels=("status:done",), state="closed")},
        repo_root=tmp_path,
        zones=zones(),
        lease={"owner": "alice", "released_at": None},
    )
    text = render_context(ctx)
    assert text.startswith("# #42 [M1-T07] Save/load\n\nhttps://example.test/issues/42\n")
    assert "- covers: AC-1, AC-77" in text
    assert "## Description\nImplement save/load.\n\nTrailing notes." in text
    assert "## Acceptance criteria (specs/m1/page-builder.md)\n- **AC-1** A user can drag" in text
    assert "AC-77: (not found in the spec)" in text
    assert "- api/pages: api/pages/**\n- db [contract]: db/**" in text
    assert "- M1-T01 #10 [M1-T01] Schema (closed, done)" in text
    assert "## Lease\n- owner: alice\n" in text and "released_at" not in text
    assert "## Latest handoff note (by carol, 2026-11-02T09:00:00Z)\nnext: wire the route" in text
    assert "- docs/decisions/0001-task.md: 0001 — Task decision" in text
    assert "## Warnings\n- AC-77 is not defined" in text
    assert "forge:begin" not in text


def test_render_context_minimal(tmp_path: Path) -> None:
    m = meta(covers=(), depends_on=(), zones=())
    ctx = build_context(the_issue(m), m, comments=[], tasks={}, repo_root=tmp_path, zones=None)
    text = render_context(ctx)
    assert "(none covered)" in text and "## Lease" not in text and "## Warnings" not in text
    assert "## Latest handoff" not in text
    assert ctx.warnings == []


def test_closed_dependency_without_label_is_done_or_dropped(tmp_path: Path) -> None:
    setup_repo(tmp_path)
    m = meta(depends_on=("M1-T01", "M1-T02"))
    dropped = replace(issue(11, "[M1-T02] Old", state="closed"), state_reason="not_planned")
    tasks = {"M1-T01": issue(10, "[M1-T01] Schema", state="closed"), "M1-T02": dropped}
    ctx = build_context(
        the_issue(m), m, comments=[], tasks=tasks, repo_root=tmp_path, zones=zones()
    )
    assert [(d.id, d.status) for d in ctx.dependencies] == [
        ("M1-T01", "done"),
        ("M1-T02", "dropped"),
    ]
    assert not any("status label" in w for w in ctx.warnings)
