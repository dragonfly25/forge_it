"""Tests for forge.core.spec: frontmatter, acceptance criteria and open questions (ADR 0013)."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.core.errors import ConfigError
from forge.core.spec import load_spec, open_questions, parse_spec, split_frontmatter

READY = """\
---
milestone: M1
status: ready
owner: alice
---
# Editor
## Requirements
- **AC-1** drag a block.
- **AC-2** save a page.
## Open questions
- Which storage? RESOLVED: postgres.
"""


def test_parse_ready_spec() -> None:
    spec = parse_spec(READY, "specs/m1/editor.md")
    assert spec.milestone == "M1"
    assert spec.status == "ready"
    assert spec.owner == "alice"
    assert spec.acceptance_criteria == ("AC-1", "AC-2")
    assert len(spec.open_questions) == 1
    assert spec.unresolved == ()
    assert spec.is_ready
    assert spec.unknown_keys == ()


def test_owner_optional_and_unknown_keys_kept() -> None:
    text = "---\nmilestone: M2\nstatus: draft\nreviewers: [bob]\n---\n# X\n"
    spec = parse_spec(text, "s.md")
    assert spec.owner is None
    assert spec.unknown_keys == ("reviewers",)
    assert not spec.is_ready


@pytest.mark.parametrize(
    ("text", "key", "problem", "fix"),
    [
        ("# no frontmatter\n", None, "missing YAML frontmatter", "frontmatter block"),
        ("---\nmilestone: M1\n# never closed\n", None, "not closed", "`---`"),
        ("---\nmilestone: [M1\n---\n", None, "invalid YAML", "YAML syntax"),
        ("---\n- a\n- b\n---\n", None, "must be a mapping", "key: value"),
        ("---\nstatus: ready\n---\n", "milestone", "missing", "milestone:"),
        ("---\nmilestone: one\nstatus: ready\n---\n", "milestone", "invalid milestone", "M<n>"),
        ("---\nmilestone: M1\n---\n", "status", "missing", "status:"),
        ("---\nmilestone: M1\nstatus:\n---\n", "status", "empty", "status:"),
        ("---\nmilestone: M1\nstatus: done\n---\n", "status", "invalid value", "status:"),
        ("---\nmilestone: M1\nstatus: ready\nowner: 3\n---\n", "owner", "expected a string", ""),
    ],
)
def test_frontmatter_errors_name_file_key_and_fix(
    text: str, key: str | None, problem: str, fix: str
) -> None:
    with pytest.raises(ConfigError) as info:
        parse_spec(text, "specs/m1/x.md")
    exc = info.value
    assert exc.file == "specs/m1/x.md"
    assert exc.key == key
    assert problem in exc.problem
    assert exc.hint is not None and fix in exc.hint


def test_empty_frontmatter_reports_missing_milestone() -> None:
    with pytest.raises(ConfigError) as info:
        parse_spec("---\n---\n", "a.md")
    assert info.value.key == "milestone"


def test_split_frontmatter_returns_body() -> None:
    data, body = split_frontmatter("---\nmilestone: M1\n---\nhello\n", "f")
    assert data == {"milestone": "M1"}
    assert body == "hello\n"


def test_load_spec_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_spec(tmp_path, "specs/m1/none.md")
    assert info.value.file == "specs/m1/none.md"
    assert info.value.problem == "spec file not found"


def test_load_spec_reads_file(tmp_path: Path) -> None:
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "a.md").write_text(READY)
    assert load_spec(tmp_path, "specs/a.md").path == "specs/a.md"


def test_acs_in_frontmatter_and_code_are_ignored() -> None:
    text = "---\nmilestone: M1\nstatus: ready\n---\n```\nAC-9 in code\n```\n- AC-3 real\n"
    assert parse_spec(text, "s.md").acceptance_criteria == ("AC-3",)


# --------------------------------------------------------------------------- open questions


def _texts(body: str) -> list[tuple[str, bool]]:
    return [(q.text, q.resolved) for q in open_questions(body)]


def test_no_section_means_no_questions() -> None:
    assert open_questions("# Spec\n## Goal\n- a\n") == []


def test_empty_section() -> None:
    assert open_questions("## Open questions\n\n## Next\n- x\n") == []


def test_resolved_needs_upper_case_word() -> None:
    body = (
        "## Open questions\n"
        "- A? RESOLVED\n"
        "- B? resolved\n"
        "- C? UNRESOLVED\n"
        "- D? (RESOLVED: yes)\n"
        "1. E? RESOLVED.\n"
    )
    assert [r for _, r in _texts(body)] == [True, False, False, True, True]


def test_template_placeholder_counts_as_unresolved() -> None:
    body = (
        "## Open questions\n- (must be empty, or every item marked RESOLVED, for status: ready)\n"
    )
    # The placeholder itself mentions RESOLVED, yet ADR 0013 says it counts as unresolved.
    assert [r for _, r in _texts(body)] == [False]
    assert [r for _, r in _texts("## Open questions\n- (RESOLVED)\n- (a) RESOLVED\n")] == [
        False,
        True,
    ]


def test_section_ends_at_same_or_higher_heading() -> None:
    body = "## Open questions\n- A?\n## Constraints\n- not a question\n# Top\n- nor this\n"
    assert _texts(body) == [("- A?", False)]


def test_nested_headings_stay_inside_section() -> None:
    body = (
        "## Open questions\n"
        "### Storage\n"
        "- Which DB? RESOLVED: postgres\n"
        "### Auth\n"
        "- Which provider?\n"
        "#### Detail\n"
        "- Token lifetime?\n"
        "## Next\n"
        "- outside\n"
    )
    assert _texts(body) == [
        ("- Which DB? RESOLVED: postgres", True),
        ("- Which provider?", False),
        ("- Token lifetime?", False),
    ]


def test_sub_items_and_continuations_belong_to_their_item() -> None:
    body = "## Open questions\n- Which DB?\n  - RESOLVED: postgres\n- Cache?\n  still discussing\n"
    assert _texts(body) == [
        ("- Which DB? - RESOLVED: postgres", True),
        ("- Cache? still discussing", False),
    ]


def test_heading_level_and_case_insensitive_title() -> None:
    body = "### OPEN QUESTIONS\n- A?\n"
    assert len(open_questions(body)) == 1


def test_fenced_code_in_section_is_ignored() -> None:
    body = "## Open questions\n```\n- not an item\n```\n- real? RESOLVED\n"
    assert _texts(body) == [("- real? RESOLVED", True)]


def test_ready_spec_with_unresolved_question_is_not_ready() -> None:
    text = READY + "- Another?\n"
    spec = parse_spec(text, "s.md")
    assert [q.text for q in spec.unresolved] == ["- Another?"]
    assert not spec.is_ready


def test_duplicate_definitions_are_counted_per_spec() -> None:
    text = (
        "---\nmilestone: M1\nstatus: ready\n---\n"
        "- **AC-1** one\n- AC-2 two\n## AC-1 again\nSee AC-2, mentioned, not defined\n"
        "```\n- AC-2 in code\n```\n- [ ] AC-3 three\n- AC-3: and again\n"
    )
    spec = parse_spec(text, "s.md")
    assert spec.acceptance_criteria == ("AC-1", "AC-2", "AC-3")
    assert spec.duplicate_acceptance_criteria == ("AC-1", "AC-3")
