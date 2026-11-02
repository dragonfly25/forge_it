"""Tests for forge.core.tasks (metadata block, labels, branch events, derived status)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from forge.core.errors import ForgeError
from forge.core.github import Issue
from forge.core.tasks import (
    BEGIN_MARKER,
    BLOCKED_CONFLICT,
    END_MARKER,
    LABELS,
    TRAILER_AGENT,
    TRAILER_CLAIM,
    TRAILER_HANDOFF,
    TRAILER_RELEASE,
    TRAILER_TASK,
    BranchEvent,
    BranchState,
    ForgeEvent,
    Size,
    Status,
    TaskBodyError,
    TaskMetadata,
    TaskType,
    derive_status,
    desired_labels,
    last_event_from_trailers,
    metadata_labels,
    parse_body,
    render_metadata,
    split_body,
    upsert_block,
)
from forge.core.tiers import Tier

SPEC_EXAMPLE = """Human-readable description.

<!-- forge:begin -->
```yaml
id: M1-T07
spec: specs/m1/page-builder.md
covers: [AC-3, AC-4]
depends_on: [M1-T01, M1-T02]
zones: [api/pages, db/pages]
size: M
tier: standard
type: impl
done_when:
  - integration tests for save/load pass
  - OpenAPI contract respected
```
<!-- forge:end -->
"""

META = TaskMetadata(
    id="M1-T07",
    spec="specs/m1/page-builder.md",
    covers=("AC-3", "AC-4"),
    depends_on=("M1-T01", "M1-T02"),
    zones=("api/pages", "db/pages"),
    size=Size.M,
    tier=Tier.STANDARD,
    type=TaskType.IMPL,
    done_when=("integration tests for save/load pass", "OpenAPI contract respected"),
)

VALID_YAML = """id: M1-T07
spec: specs/m1/x.md
covers: [AC-1]
depends_on: [M1-T01]
zones: [api]
size: S
tier: light
type: chore
done_when:
  - it works"""


def block(yaml_text: str) -> str:
    return f"{BEGIN_MARKER}\n```yaml\n{yaml_text}\n```\n{END_MARKER}\n"


def with_field(key: str, value: str) -> str:
    """VALID_YAML with ``key`` replaced (or added) by ``key: value``."""
    lines = [ln for ln in VALID_YAML.splitlines() if not ln.startswith(f"{key}:")]
    if key == "done_when":
        lines = [ln for ln in lines if not ln.startswith("  - ")]
    return block("\n".join([*lines, f"{key}: {value}"]))


def body_error(body: str, **kwargs: object) -> TaskBodyError:
    with pytest.raises(TaskBodyError) as info:
        parse_body(body, **kwargs)  # type: ignore[arg-type]
    return info.value


# -- parse: valid --------------------------------------------------------------------------


def test_parse_spec_example() -> None:
    assert parse_body(SPEC_EXAMPLE, issue_number=42, milestone="M1") == META


def test_parse_no_markers_is_none() -> None:
    assert parse_body("just a normal issue") is None
    assert parse_body("") is None


def test_metadata_properties() -> None:
    assert META.milestone == "M1"
    assert META.number == 7
    assert META.branch_name(42) == "task/42"
    assert not META.allow_contract_change


def test_parse_allow_contract_change_and_defaults() -> None:
    text = "\n".join(
        [
            "id: M2-T10",
            "spec: specs/m2/a.md",
            "size: S",
            "tier: strong",
            "type: contract",
            "done_when: [ok]",
            "allow_contract_change: true",
        ]
    )
    meta = parse_body(block(text))
    assert meta is not None
    assert meta.allow_contract_change
    assert meta.covers == meta.depends_on == meta.zones == ()
    assert meta.number == 10


def test_parse_crlf_body() -> None:
    meta = parse_body(SPEC_EXAMPLE.replace("\n", "\r\n"))
    assert meta == META


def test_parse_tolerates_marker_indentation() -> None:
    body = SPEC_EXAMPLE.replace(BEGIN_MARKER, "  " + BEGIN_MARKER)
    assert parse_body(body) == META


# -- parse: malformed ----------------------------------------------------------------------


def test_begin_without_end() -> None:
    err = body_error(f"x\n{BEGIN_MARKER}\n```yaml\nid: M1-T01\n```\n", issue_number=42)
    assert "without" in err.reason
    assert err.issue_number == 42
    assert err.message.startswith("issue #42: forge block: ")
    assert err.hint


def test_end_without_begin() -> None:
    assert "without" in body_error(f"x\n{END_MARKER}\n").reason


def test_end_before_begin() -> None:
    assert "before" in body_error(f"{END_MARKER}\n{BEGIN_MARKER}\n").reason


def test_two_blocks() -> None:
    err = body_error(block(VALID_YAML) + "\n" + block(VALID_YAML))
    assert "more than one" in err.reason


def test_no_fence_inside() -> None:
    err = body_error(f"{BEGIN_MARKER}\nid: M1-T01\n{END_MARKER}\n")
    assert "fenced block" in err.reason


def test_unclosed_fence() -> None:
    err = body_error(f"{BEGIN_MARKER}\n```yaml\nid: M1-T01\n{END_MARKER}\n")
    assert "not closed" in err.reason


def test_text_outside_fence_inside_markers() -> None:
    err = body_error(f"{BEGIN_MARKER}\nhello\n```yaml\n{VALID_YAML}\n```\n{END_MARKER}\n")
    assert "unexpected text" in err.reason


def test_invalid_yaml() -> None:
    err = body_error(block("id: [M1-T01\nsize: S"), issue_number=3)
    assert "invalid YAML" in err.reason
    assert err.message.startswith("issue #3: forge block: invalid YAML")


def test_yaml_not_a_mapping() -> None:
    assert "mapping" in body_error(block("- a\n- b")).reason


def test_error_is_forge_error() -> None:
    assert isinstance(body_error(block("- a")), ForgeError)


@pytest.mark.parametrize(
    ("body", "key", "fragment"),
    [
        (with_field("size", "L"), "size", "too large"),
        (with_field("size", "XL"), "size", "invalid value"),
        (with_field("id", "T07"), "id", "invalid task id"),
        (with_field("id", "M1-T7x"), "id", "invalid task id"),
        (with_field("depends_on", "[M1-T01, bad]"), "depends_on[1]", "invalid task id"),
        (with_field("depends_on", "[M1-T07]"), "depends_on[0]", "itself"),
        (with_field("depends_on", "[M1-T01, M1-T01]"), "depends_on[1]", "duplicate"),
        (with_field("done_when", "[]"), "done_when", "non-empty"),
        (with_field("done_when", '[""]'), "done_when[0]", "empty item"),
        (with_field("covers", "[AC-1, X-2]"), "covers[1]", "invalid acceptance"),
        (with_field("spec", "/abs/path.md"), "spec", "relative"),
        (with_field("spec", "../out.md"), "spec", "relative"),
        (with_field("tier", "huge"), "tier", "invalid value"),
        (with_field("type", "bug"), "type", "invalid value"),
        (with_field("allow_contract_change", "1"), "allow_contract_change", "boolean"),
        (with_field("owner", "bob"), "owner", "unknown key"),
        (with_field("zones", "api"), "zones", "list"),
    ],
)
def test_invalid_fields(body: str, key: str, fragment: str) -> None:
    err = body_error(body, issue_number=42)
    assert err.message.startswith(f"issue #42: forge block: {key}: "), err.message
    assert fragment in err.reason
    assert err.hint


def test_size_l_suggests_split() -> None:
    err = body_error(with_field("size", "L"))
    assert err.hint is not None and "split" in err.hint


def test_missing_required_field() -> None:
    text = "\n".join(ln for ln in VALID_YAML.splitlines() if not ln.startswith("tier:"))
    err = body_error(block(text))
    assert err.reason.startswith("tier: required key is missing")


def test_milestone_mismatch() -> None:
    err = body_error(block(VALID_YAML), issue_number=5, milestone="M2")
    assert err.message.startswith("issue #5: forge block: id: ")
    assert "M2" in err.reason
    assert parse_body(block(VALID_YAML), milestone="M1") is not None


# -- render / upsert -----------------------------------------------------------------------


def test_render_matches_spec_example() -> None:
    _, inner, _ = split_body(SPEC_EXAMPLE)
    assert inner == render_metadata(META)


def test_render_round_trip_with_quoting() -> None:
    meta = replace(
        META,
        spec="specs/m1/a b.md",
        zones=("api/pages", "x: y"),
        done_when=("a: b", "#hash", "  padded ", "yes", "[x]", "é"),
        allow_contract_change=True,
    )
    body = upsert_block("", meta)
    assert parse_body(body) == meta
    assert upsert_block(body, meta) == body


def test_upsert_existing_preserves_surrounding_text() -> None:
    before = "Title line\n\nSome *markdown*  \n"
    after = "\nTrailing notes\n  with spaces  "
    body = before + block(VALID_YAML).rstrip("\n") + after
    out = upsert_block(body, META)
    assert out.startswith(before)
    assert out.endswith(after)
    assert parse_body(out) == META
    b, _, a = split_body(out)
    assert (b, a) == (before, after)


def test_upsert_spec_example_is_identity() -> None:
    assert upsert_block(SPEC_EXAMPLE, META) == SPEC_EXAMPLE


def test_upsert_appends_after_blank_line() -> None:
    out = upsert_block("Description.\n", META)
    assert out.startswith("Description.\n\n" + BEGIN_MARKER + "\n")
    assert out.endswith(END_MARKER + "\n")
    assert parse_body(out) == META
    assert upsert_block(out, META) == out


def test_upsert_no_trailing_newline() -> None:
    out = upsert_block("Description.", META)
    assert out.startswith("Description.\n\n" + BEGIN_MARKER)
    assert parse_body(out) == META


def test_upsert_body_already_ending_with_blank_line() -> None:
    assert upsert_block("Description.\n\n", META).startswith("Description.\n\n" + BEGIN_MARKER)


def test_upsert_empty_body() -> None:
    out = upsert_block("", META)
    assert out == f"{BEGIN_MARKER}\n{render_metadata(META)}{END_MARKER}\n"


def test_upsert_crlf_body() -> None:
    before = "Line one\r\nLine two\r\n\r\n"
    after = "\r\nfooter\r\n"
    body = before + block(VALID_YAML).replace("\n", "\r\n").rstrip("\r\n") + after
    out = upsert_block(body, META)
    assert out.startswith(before)
    assert out.endswith(after)
    assert "\n" not in out.replace("\r\n", "")
    assert parse_body(out) == META


def test_upsert_crlf_append() -> None:
    out = upsert_block("Desc\r\n", META)
    assert out.startswith("Desc\r\n\r\n" + BEGIN_MARKER + "\r\n")
    assert "\n" not in out.replace("\r\n", "")
    assert parse_body(out) == META


def test_upsert_replaces_invalid_fields() -> None:
    body = "keep\n" + with_field("size", "L") + "tail\n"
    out = upsert_block(body, META)
    assert out.startswith("keep\n") and out.endswith("tail\n")
    assert parse_body(out) == META


def test_upsert_rejects_unbalanced_markers() -> None:
    with pytest.raises(TaskBodyError):
        upsert_block(f"x\n{BEGIN_MARKER}\n", META)


def test_split_body_without_block() -> None:
    assert split_body("hello") == ("hello", None, "")


# -- labels ---------------------------------------------------------------------------------


def test_status_labels() -> None:
    assert Status.IN_PROGRESS.label == "status:in-progress"
    assert Status.from_labels(["bug", "status:handoff"]) is Status.HANDOFF
    assert Status.from_labels(["bug", "status:nope"]) is None


def test_catalogue_covers_every_managed_label() -> None:
    names = {spec.name for spec in LABELS}
    assert {s.label for s in Status} <= names
    assert metadata_labels(META) <= names
    assert BLOCKED_CONFLICT in names
    assert len(names) == len(LABELS)
    assert all(len(spec.color) == 6 for spec in LABELS)


def test_metadata_labels() -> None:
    assert metadata_labels(META) == {"forge:task", "size:M", "tier:standard", "type:impl"}


def test_desired_labels_replaces_status_and_keeps_others() -> None:
    current = [
        "forge:task",
        "status:ready",
        "status:blocked",
        "size:M",
        "tier:standard",
        "type:impl",
        BLOCKED_CONFLICT,
        "bug",
    ]
    diff = desired_labels(current, Status.IN_PROGRESS, META)
    assert diff.add == {"status:in-progress"}
    assert diff.remove == {"status:ready", "status:blocked"}


def test_desired_labels_mirror_changes() -> None:
    current = ["status:ready", "size:S", "tier:light", "type:chore", "type:bug"]
    diff = desired_labels(current, Status.READY, META)
    assert diff.add == {"forge:task", "size:M", "tier:standard", "type:impl"}
    assert diff.remove == {"size:S", "tier:light", "type:chore"}  # type:bug is a user label


def test_desired_labels_none_status_removes_status() -> None:
    diff = desired_labels(["forge:task", "status:done", *metadata_labels(META)], None, META)
    assert diff.add == frozenset()
    assert diff.remove == {"status:done"}


def test_desired_labels_without_meta_touches_status_only() -> None:
    diff = desired_labels(["size:S", "status:ready"], Status.BLOCKED, None)
    assert diff.add == {"status:blocked"}
    assert diff.remove == {"status:ready"}


def test_desired_labels_idempotent() -> None:
    current = [Status.READY.label, *metadata_labels(META), BLOCKED_CONFLICT]
    assert desired_labels(current, Status.READY, META).empty


# -- branch events --------------------------------------------------------------------------


def test_last_event_newest_wins() -> None:
    commits = [
        {"Signed-off-by": "x"},
        {TRAILER_TASK: "M1-T07", TRAILER_HANDOFF: "bob"},
        {TRAILER_TASK: "M1-T07", TRAILER_CLAIM: "alice", TRAILER_AGENT: "claude"},
    ]
    assert last_event_from_trailers(commits) == ForgeEvent(
        BranchEvent.HANDOFF, "bob", None, "M1-T07"
    )


def test_last_event_claim_with_agent() -> None:
    commits = [{}, {TRAILER_TASK: "M1-T07", TRAILER_CLAIM: "alice", TRAILER_AGENT: "codex"}]
    event = last_event_from_trailers(commits)
    assert event == ForgeEvent(BranchEvent.CLAIM, "alice", "codex", "M1-T07")
    state = BranchState.from_event("abc", event)
    assert state == BranchState(True, "abc", BranchEvent.CLAIM, "alice", "codex")


def test_last_event_release_and_case_insensitive_keys() -> None:
    event = last_event_from_trailers([{"forge-release": "carol", "forge-task": "M1-T01"}])
    assert event is not None and event.event is BranchEvent.RELEASE


def test_last_event_none_and_malformed_skipped() -> None:
    assert last_event_from_trailers([]) is None
    assert last_event_from_trailers([{"Co-Authored-By": "x"}]) is None
    both = {TRAILER_CLAIM: "a", TRAILER_RELEASE: "a"}
    claim = {TRAILER_CLAIM: "b"}
    event = last_event_from_trailers([both, claim])
    assert event is not None and event.author == "b"


# -- derive_status ------------------------------------------------------------------------

NOW = datetime(2026, 11, 2, tzinfo=UTC)


def issue(
    *,
    state: str = "open",
    reason: str | None = None,
    labels: tuple[str, ...] = (),
) -> Issue:
    return Issue(
        number=42,
        title="[M1-T07] x",
        body="",
        state=state,  # type: ignore[arg-type]
        state_reason=reason,
        labels=labels,
        assignees=(),
        milestone="M1",
        author="lead",
        url="https://example.invalid/42",
        created_at=NOW,
        updated_at=NOW,
        closed_at=None,
    )


ALL_CLOSED = {"M1-T01": True, "M1-T02": True}
ONE_OPEN = {"M1-T01": True, "M1-T02": False}
NO_BRANCH = BranchState.absent()
CLAIMED = BranchState(True, "sha", BranchEvent.CLAIM, "alice", "claude")
HANDED_OFF = BranchState(True, "sha", BranchEvent.HANDOFF, "alice", None)
RELEASED = BranchState(True, "sha", BranchEvent.RELEASE, "alice", None)
BARE = BranchState(True, "sha")


@pytest.mark.parametrize(
    ("iss", "deps", "branch", "pr", "expected"),
    [
        # rule 1: closed beats everything
        (issue(state="closed", reason="completed"), ONE_OPEN, CLAIMED, True, Status.DONE),
        (issue(state="closed", reason=None), ALL_CLOSED, NO_BRANCH, False, Status.DONE),
        (issue(state="closed", reason="not_planned"), ALL_CLOSED, NO_BRANCH, False, None),
        # rule 2: proposed is sticky
        (issue(labels=("status:proposed",)), ALL_CLOSED, CLAIMED, True, Status.PROPOSED),
        # rule 3: open PR beats a claim and open deps
        (issue(labels=("status:in-progress",)), ONE_OPEN, CLAIMED, True, Status.IN_REVIEW),
        # rule 4: claim beats open deps
        (issue(), ONE_OPEN, CLAIMED, False, Status.IN_PROGRESS),
        # rule 5: handoff
        (issue(), ONE_OPEN, HANDED_OFF, False, Status.HANDOFF),
        # rule 6/7: released branch falls through to deps
        (issue(labels=("status:in-progress",)), ALL_CLOSED, RELEASED, False, Status.READY),
        (issue(), ONE_OPEN, RELEASED, False, Status.BLOCKED),
        (issue(), ONE_OPEN, NO_BRANCH, False, Status.BLOCKED),
        (issue(), ALL_CLOSED, NO_BRANCH, False, Status.READY),
        (issue(labels=(BLOCKED_CONFLICT,)), ALL_CLOSED, NO_BRANCH, False, Status.READY),
    ],
)
def test_derive_status_rules(
    iss: Issue, deps: dict[str, bool], branch: BranchState, pr: bool, expected: Status | None
) -> None:
    result = derive_status(issue=iss, meta=META, deps_closed=deps, branch=branch, has_open_pr=pr)
    assert result.status == expected
    assert result.warnings == ()


def test_derive_status_unknown_dependency_blocks_with_warning() -> None:
    result = derive_status(
        issue=issue(),
        meta=META,
        deps_closed={"M1-T01": True},
        branch=NO_BRANCH,
        has_open_pr=False,
    )
    assert result.status is Status.BLOCKED
    assert len(result.warnings) == 1
    assert "M1-T02" in result.warnings[0] and "unknown dependency" in result.warnings[0]


def test_derive_status_branch_without_event_is_not_a_claim() -> None:
    result = derive_status(
        issue=issue(), meta=META, deps_closed=ALL_CLOSED, branch=BARE, has_open_pr=False
    )
    assert result.status is Status.READY
    assert any("no Forge event" in w for w in result.warnings)


def test_derive_status_without_meta() -> None:
    result = derive_status(
        issue=issue(), meta=None, deps_closed={}, branch=NO_BRANCH, has_open_pr=False
    )
    assert result.status is Status.READY
