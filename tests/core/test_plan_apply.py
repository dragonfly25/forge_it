"""Tests for `forge plan apply` (core/plan_apply.py, ADR 0013).

Git runs against a real bare remote with a lead clone (who applies) and a dev clone (who
claims). GitHub is a FakeGitHub, wrapped by ``RecordingGitHub`` to count writes, refuse
them or crash a run part-way.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from forge.core import plan_apply as plan_apply_mod
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.git import Git
from forge.core.github import GitHubError
from forge.core.github_fake import FakeGitHub
from forge.core.plan import PlanIssue, PlanValidation
from forge.core.plan_apply import (
    PLAN_DUPLICATE_MARKER,
    PLAN_REMOVED_MARKER,
    PLAN_WARNING_MARKER,
    ApplyState,
    compute_apply_plan,
    execute_apply_plan,
    gather_apply_state,
    issue_title,
    plan_apply,
    plan_with_comments,
    render_task_body,
)
from forge.core.propose import ProposeContext, propose
from forge.core.tasks import (
    DESCRIPTION_BEGIN,
    DESCRIPTION_END,
    LABELS,
    Size,
    TaskBodyError,
    TaskMetadata,
    parse_body,
    split_body,
    split_description,
    upsert_block,
    upsert_description,
)
from support.plan_world import (
    ApplyCtx,
    Crash,
    PlanWorld,
    RecordingGitHub,
    meta,
    plan_of,
    roadmap_of,
    task,
    validation_of,
)

if TYPE_CHECKING:
    from conftest import GitWorld


@pytest.fixture
def world(git_world: GitWorld) -> PlanWorld:
    return PlanWorld(git_world)


def by_title_id(fake: FakeGitHub) -> dict[str, list[Any]]:
    """Issues grouped by the task id of their title, all states."""
    out: dict[str, list[Any]] = {}
    for issue in fake.list_issues(state="all"):
        out.setdefault(issue.title.split("]")[0].lstrip("["), []).append(issue)
    return out


def statuses(fake: FakeGitHub) -> dict[int, list[str]]:
    return {
        i.number: sorted(lb for lb in i.labels if lb.startswith("status:"))
        for i in fake.list_issues(state="all")
    }


def seed(
    fake: FakeGitHub,
    m: TaskMetadata,
    *,
    body: str | None = None,
    title: str | None = None,
    labels: tuple[str, ...] = ("forge:task", "status:ready"),
    milestone: str | None = "M1",
    closed: str | None = None,
    assignees: tuple[str, ...] = (),
) -> int:
    """Create a task issue directly on the fake (labels and milestone are created as needed)."""
    have = {lb.name.lower() for lb in fake.list_labels()}
    for name in labels:
        if name.lower() not in have:
            fake.create_label(name, "ededed")
    if milestone is not None and not any(x.title == milestone for x in fake.list_milestones()):
        fake.create_milestone(milestone)
    issue = fake.create_issue(
        title or f"[{m.id}] old title",
        body if body is not None else upsert_block("Old description.\n", m),
        labels=list(labels),
        milestone=milestone,
        assignees=list(assignees),
    )
    if closed is not None:
        fake.update_issue(issue.number, state="closed", state_reason=closed)
    return issue.number


# --------------------------------------------------------------------------- body helpers


def test_upsert_description_inserts_at_top_and_preserves_the_rest() -> None:
    body = "Human notes.\n\n" + upsert_block("", meta("M1-T01"))
    out = upsert_description(body, "New text.\n")
    assert out == f"{DESCRIPTION_BEGIN}\nNew text.\n{DESCRIPTION_END}\n\n" + body
    # Replacing touches only the region; re-applying is a no-op.
    again = upsert_description(out, "Other.\n\n")
    assert again == f"{DESCRIPTION_BEGIN}\nOther.\n{DESCRIPTION_END}\n\n" + body
    assert upsert_description(again, "Other.") == again
    assert split_description(again)[1] == "Other.\n"


def test_upsert_description_follows_crlf_and_closes_open_fences() -> None:
    body = "a\r\nb\r\n"
    out = upsert_description(body, "x\n```py\ncode\n")
    assert (
        out
        == f"{DESCRIPTION_BEGIN}\r\nx\r\n```py\r\ncode\r\n```\r\n{DESCRIPTION_END}\r\n\r\n" + body
    )
    full = upsert_block(upsert_description("", "```\nunclosed\n"), meta("M1-T01"))
    assert parse_body(full) == meta("M1-T01")


def test_description_markers_inside_fences_are_ignored_and_unbalanced_rejected() -> None:
    quoted = f"```\n{DESCRIPTION_BEGIN}\n```\n"
    assert split_description(quoted) == (quoted, None, "")
    with pytest.raises(TaskBodyError):
        split_description(f"{DESCRIPTION_BEGIN}\nno end\n")
    with pytest.raises(ValueError, match="forge:begin"):
        upsert_description("", "<!-- forge:begin -->\n")


# --------------------------------------------------------------------------- first apply


def test_first_apply_creates_labels_milestone_and_issues(world: PlanWorld) -> None:
    plan = plan_of(
        task("M1-T01", "Schema", "Write the schema.\n"),
        task("M1-T02", "Endpoint", deps=["M1-T01"], size=Size.M),
    )
    result = world.apply(plan, roadmap=roadmap_of("M1", "Editor base", "save a page"))
    assert result.code == ExitCode.OK, result.messages
    fake = world.fake
    assert {lb.name for lb in fake.list_labels()} == {spec.name for spec in LABELS}
    [ms] = fake.list_milestones()
    assert (ms.title, ms.description) == ("M1", "Editor base: save a page")
    t1, t2 = fake.list_issues(state="all")
    assert t1.title == "[M1-T01] Schema"
    assert t1.body.startswith(f"{DESCRIPTION_BEGIN}\nWrite the schema.\n{DESCRIPTION_END}\n\n")
    assert parse_body(t1.body, milestone="M1") == plan.tasks[0].meta
    assert t1.milestone == t2.milestone == "M1"
    assert sorted(t1.labels) == sorted(
        ["forge:task", "size:S", "tier:standard", "type:impl", "status:ready"]
    )
    assert "status:blocked" in t2.labels and "size:M" in t2.labels
    data = result.data
    assert data["applied"] is True
    assert [t["action"] for t in data["tasks"]] == ["create", "create"]
    assert [t["issue"] for t in data["tasks"]] == [t1.number, t2.number]
    assert data["execution"]["created"] == {"M1-T01": t1.number, "M1-T02": t2.number}


def test_existing_milestone_description_updated_only_with_a_roadmap(world: PlanWorld) -> None:
    world.fake.create_milestone("M1", "hand written")
    plan = plan_of(task("M1-T01"))
    world.apply(plan)
    assert world.fake.list_milestones()[0].description == "hand written"
    world.apply(plan, roadmap=roadmap_of("M1", "Editor base"))
    assert world.fake.list_milestones()[0].description == "Editor base"


# --------------------------------------------------------------------------- rich scenario

PLAN_V1 = plan_of(
    task("M1-T01", "First", "New description.\n"),
    task("M1-T02", "Second", zones=["api", "db"], done_when=["tests pass", "docs"]),
    task("M1-T05", "Done already"),
    task("M1-T06", "Hand made", "Planned text.\n"),
    task("M1-T07", "New one", deps=["M1-T01"]),
    task("M1-T08", "New two", deps=["M1-T05"]),
)


def seed_rich(fake: FakeGitHub) -> dict[str, int]:
    """Issues covering every apply rule (see the test names); returns numbers by name."""
    n: dict[str, int] = {}
    n["t1"] = seed(fake, meta("M1-T01"), milestone=None)
    n["t2"] = seed(
        fake, meta("M1-T02"), labels=("forge:task", "status:in-progress"), assignees=("bob",)
    )
    n["t3"] = seed(fake, meta("M1-T03"))
    n["t4"] = seed(fake, meta("M1-T04"), assignees=("carol",))
    n["t5"] = seed(fake, meta("M1-T05"), closed="completed")
    n["t6"] = seed(fake, meta("M1-T06"), body="Human notes.\n\n" + upsert_block("", meta("M1-T06")))
    n["t6dup"] = seed(fake, meta("M1-T06"))
    return n


@pytest.fixture
def rich(world: PlanWorld) -> tuple[PlanWorld, dict[str, int]]:
    n = seed_rich(world.fake)
    world.push_event(n["t2"], "M1-T02", "claim")
    world.push_event(n["t4"], "M1-T04", "claim", who="carol")
    return world, n


def test_rich_apply_follows_every_rule(rich: tuple[PlanWorld, dict[str, int]]) -> None:
    world, n = rich
    fake = world.fake
    before = {i.number: i for i in fake.list_issues(state="all")}
    result = world.apply(PLAN_V1)
    assert result.code == ExitCode.OK, result.messages
    after = {i.number: i for i in fake.list_issues(state="all")}

    # not started: full update (title, description region, block, milestone)
    t1 = after[n["t1"]]
    assert t1.title == "[M1-T01] First" and t1.milestone == "M1"
    assert split_description(t1.body)[1] == "New description.\n"

    # started: title, description and done_when only; zones not applied; one warning
    t2 = after[n["t2"]]
    assert t2.title == "[M1-T02] Second"
    m2 = parse_body(t2.body)
    assert m2 is not None and m2.done_when == ("tests pass", "docs") and m2.zones == ("api",)
    [warning] = fake.list_comments(n["t2"])
    assert warning.body.startswith(PLAN_WARNING_MARKER) and "@bob" in warning.body
    assert "`zones`: [api] → [api, db]" in warning.body
    assert "done_when" not in warning.body

    # removed, not started: comment then closed as not planned, status label gone
    t3 = after[n["t3"]]
    assert (t3.state, t3.state_reason) == ("closed", "not_planned")
    assert not [lb for lb in t3.labels if lb.startswith("status:")]
    [removed] = fake.list_comments(n["t3"])
    assert removed.body.startswith(PLAN_REMOVED_MARKER) and "plans/M1.yml" in removed.body

    # removed, started: warning comment, left open
    t4 = after[n["t4"]]
    assert t4.state == "open"
    [kept] = fake.list_comments(n["t4"])
    assert kept.body.startswith(PLAN_WARNING_MARKER) and "@carol" in kept.body

    # closed: untouched, even its stale status label
    assert after[n["t5"]] == before[n["t5"]]
    assert fake.list_comments(n["t5"]) == []

    # hand-made issue: region inserted at the top, human text kept
    t6 = after[n["t6"]]
    assert t6.body.startswith(f"{DESCRIPTION_BEGIN}\nPlanned text.\n{DESCRIPTION_END}\n\n")
    assert "Human notes.\n\n<!-- forge:begin -->" in t6.body

    # duplicate of T06: closed as not planned with "duplicate of"
    dup = after[n["t6dup"]]
    assert (dup.state, dup.state_reason) == ("closed", "not_planned")
    [comment] = fake.list_comments(n["t6dup"])
    assert comment.body.startswith(PLAN_DUPLICATE_MARKER)
    assert f"Duplicate of #{n['t6']}" in comment.body

    # created issues and statuses (T07 waits on open T01; T08's dep T05 is closed)
    s = statuses(fake)
    created = result.data["execution"]["created"]
    assert s[created["M1-T07"]] == ["status:blocked"]
    assert s[created["M1-T08"]] == ["status:ready"]
    assert s[n["t2"]] == ["status:in-progress"] and s[n["t4"]] == ["status:in-progress"]
    assert s[n["t1"]] == ["status:ready"] and s[n["t6"]] == ["status:ready"]


def test_second_apply_writes_nothing(rich: tuple[PlanWorld, dict[str, int]]) -> None:
    world, _ = rich
    assert world.apply(PLAN_V1).code == ExitCode.OK
    guard = RecordingGitHub(world.fake, read_only=True)
    result = world.apply(PLAN_V1, github=guard)
    assert result.code == ExitCode.OK
    assert guard.writes == []
    assert result.data["counts"]["writes"] == 0
    assert {t["action"] for t in result.data["tasks"]} == {"unchanged", "closed"}
    assert "list_issues" in guard.calls


def test_dry_run_writes_nothing_and_reports_the_plan(
    rich: tuple[PlanWorld, dict[str, int]],
) -> None:
    world, n = rich
    guard = RecordingGitHub(world.fake, read_only=True)
    refs = world.git_world.remote_git.list_refs("refs/heads")
    result = world.apply(PLAN_V1, github=guard, dry_run=True)
    assert result.code == ExitCode.OK
    assert guard.writes == []
    assert world.git_world.remote_git.list_refs("refs/heads") == refs
    data = result.data
    assert data["dry_run"] is True and data["applied"] is False
    actions = {t["id"]: t["action"] for t in data["tasks"]}
    assert actions == {
        "M1-T01": "update",
        "M1-T02": "update",
        "M1-T05": "closed",
        "M1-T06": "update",
        "M1-T07": "create",
        "M1-T08": "create",
    }
    assert {r["id"]: r["action"] for r in data["removed"]} == {"M1-T03": "close", "M1-T04": "keep"}
    assert [d["issue"] for d in data["duplicates"]] == [n["t6dup"]]
    assert data["counts"]["writes"] > 0
    assert {c["status"] for c in data["creates"]} == {"blocked", "ready"}
    t2 = next(t for t in data["tasks"] if t["id"] == "M1-T02")
    assert t2["started"] == "in-progress" and t2["pending"] == ["zones"]
    assert t2["fields"] == ["title", "description", "done_when"]
    # A dry run after a real apply also reports nothing to do.
    world.apply(PLAN_V1)
    assert world.apply(PLAN_V1, dry_run=True).data["counts"]["writes"] == 0


@pytest.mark.parametrize("mode", ["before", "after"])
def test_interrupted_apply_then_rerun_converges(
    world: PlanWorld, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash the run before/after each write in turn; a re-run reaches the reference state."""
    n = seed_rich(world.fake)
    world.push_event(n["t2"], "M1-T02", "claim")
    world.push_event(n["t4"], "M1-T04", "claim", who="carol")

    def fresh() -> FakeGitHub:
        fake = FakeGitHub()
        assert seed_rich(fake) == n
        return fake

    reference = fresh()
    recorder = RecordingGitHub(reference)
    assert world.apply(PLAN_V1, github=recorder).code == ExitCode.OK
    total = len(recorder.writes)
    world.fake = reference
    expected = world.snapshot()
    assert total > 10
    # Git never changes below (apply does not push): skip the network round trips.
    monkeypatch.setattr(plan_apply_mod, "fetch_task_refs", lambda *a, **k: None)

    for k in range(1, total + 1):
        fake = fresh()
        kwargs = {"crash_before": k} if mode == "before" else {"crash_after": k}
        with pytest.raises(Crash):
            world.apply(PLAN_V1, github=RecordingGitHub(fake, **kwargs))
        assert world.apply(PLAN_V1, github=fake).code == ExitCode.OK
        world.fake = fake
        assert world.snapshot() == expected, f"diverged after a crash {mode} write {k}"
        guard = RecordingGitHub(fake, read_only=True)
        assert world.apply(PLAN_V1, github=guard).code == ExitCode.OK


# --------------------------------------------------------------------------- started tasks


@pytest.mark.parametrize("started", ["claim", "handoff", "pr"])
def test_started_task_edits_follow_9_5(world: PlanWorld, started: str) -> None:
    fake = world.fake
    old = meta("M1-T01", zones=["api"], done_when=["old"])
    number = seed(fake, old, assignees=("bob",), body="Intro.\n\n" + upsert_block("", old))
    expected = {"claim": "in-progress", "handoff": "handoff", "pr": "in-review"}[started]
    if started == "pr":
        world.push_event(number, "M1-T01", "claim")
        world.open_pr(number)
    else:
        world.push_event(number, "M1-T01", started)
    plan = plan_of(
        task("M1-T01", "Renamed", "Fresh text.\n", zones=["db"], deps=["M1-T09"], done_when=["new"])
    )
    world.apply(plan)
    issue = fake.get_issue(number)
    assert issue.title == "[M1-T01] Renamed"
    assert split_description(issue.body)[1] == "Fresh text.\n"
    applied = parse_body(issue.body)
    assert applied == replace(old, done_when=("new",))
    [comment] = fake.list_comments(number)
    assert "`zones`: [api] → [db]" in comment.body
    assert "`depends_on`: [] → [M1-T09]" in comment.body
    assert f"started ({expected})" in comment.body and "@bob" in comment.body
    assert statuses(fake)[number] == [f"status:{expected}"]

    # Re-running posts no second comment and writes nothing.
    guard = RecordingGitHub(fake, read_only=True)
    world.apply(plan, github=guard)
    assert guard.writes == []
    # A different pending change is a new warning.
    world.apply(
        plan_of(task("M1-T01", "Renamed", "Fresh text.\n", zones=["web"], done_when=["new"]))
    )
    assert len(fake.list_comments(number)) == 2


def test_warning_mentions_branch_author_without_assignees(world: PlanWorld) -> None:
    number = seed(world.fake, meta("M1-T01"))
    world.push_event(number, "M1-T01", "handoff", who="dana")
    world.apply(plan_of(task("M1-T01", zones=["db"])))
    [comment] = world.fake.list_comments(number)
    assert "@dana" in comment.body


def test_released_task_is_not_started(world: PlanWorld) -> None:
    number = seed(world.fake, meta("M1-T01"))
    world.push_event(number, "M1-T01", "claim")
    world.push_event(number, "M1-T01", "release")
    world.apply(plan_of(task("M1-T01", zones=["db"])))
    assert parse_body(world.fake.get_issue(number).body) == meta("M1-T01", zones=["db"])
    assert world.fake.list_comments(number) == []


def test_not_started_task_gets_every_change(world: PlanWorld) -> None:
    fake = world.fake
    number = seed(fake, meta("M1-T01"), labels=("forge:task", "status:ready", "size:S"))
    new = task("M1-T01", "X", zones=["db"], size=Size.M, deps=["M1-T02"])
    world.apply(plan_of(new, task("M1-T02")))
    issue = fake.get_issue(number)
    assert parse_body(issue.body) == new.meta
    assert "size:M" in issue.labels and "size:S" not in issue.labels
    assert "status:blocked" in issue.labels and "status:ready" not in issue.labels
    assert fake.list_comments(number) == []


def test_claim_landing_between_listing_and_fetch_counts_as_started(world: PlanWorld) -> None:
    """Issues are listed before the fetch, so a claim landing in between is seen."""
    number = seed(world.fake, meta("M1-T01"))
    guard = RecordingGitHub(world.fake)
    guard.after["list_milestones"] = lambda: world.push_event(number, "M1-T01", "claim")
    world.apply(plan_of(task("M1-T01", zones=["db"])), github=guard)
    assert parse_body(world.fake.get_issue(number).body) == meta("M1-T01")
    assert len(world.fake.list_comments(number)) == 1


def test_stale_remote_tracking_refs_are_pruned(world: PlanWorld) -> None:
    number = seed(world.fake, meta("M1-T01"))
    world.push_event(number, "M1-T01", "claim")
    world.lead.fetch()
    assert world.lead.rev_parse(f"refs/remotes/origin/task/{number}") is not None
    assert world.dev.push("", f"refs/heads/task/{number}").ok  # branch deleted on the remote
    world.apply(plan_of(task("M1-T01", zones=["db"])))
    assert parse_body(world.fake.get_issue(number).body) == meta("M1-T01", zones=["db"])


# --------------------------------------------------------------------------- removed / closed


def test_removed_tasks_rerun_is_quiet(world: PlanWorld) -> None:
    fake = world.fake
    gone = seed(fake, meta("M1-T03"))
    kept = seed(fake, meta("M1-T04"))
    other = seed(fake, meta("M2-T01"), milestone="M2")
    world.push_event(kept, "M1-T04", "claim")
    world.apply(plan_of(task("M1-T01")))
    assert fake.get_issue(gone).state_reason == "not_planned"
    assert fake.get_issue(kept).state == "open" and len(fake.list_comments(kept)) == 1
    assert fake.get_issue(other).state == "open"  # other milestones are never "removed"
    guard = RecordingGitHub(fake, read_only=True)
    world.apply(plan_of(task("M1-T01")), github=guard)
    assert guard.writes == []


def test_removed_proposed_issue_is_left_alone(world: PlanWorld) -> None:
    number = seed(world.fake, meta("M1-T03"), labels=("forge:task", "status:proposed"))
    result = world.apply(plan_of(task("M1-T01")))
    assert world.fake.get_issue(number).state == "open"
    assert any("status:proposed" in m.text for m in result.messages)


def test_plan_accepts_a_proposed_task(world: PlanWorld) -> None:
    number = seed(world.fake, meta("M1-T01"), labels=("forge:task", "status:proposed"))
    world.apply(plan_of(task("M1-T01")))
    assert statuses(world.fake)[number] == ["status:ready"]


def test_closed_issue_never_modified(world: PlanWorld) -> None:
    fake = world.fake
    number = seed(fake, meta("M1-T01"), closed="completed", labels=("forge:task", "status:ready"))
    before = fake.get_issue(number)
    guard = RecordingGitHub(fake)
    world.apply(plan_of(task("M1-T01", "Renamed", zones=["db"])), github=guard)
    assert fake.get_issue(number) == before
    assert fake.list_comments(number) == []


def test_statuses_recomputed_blocked_to_ready(world: PlanWorld) -> None:
    fake = world.fake
    dep = seed(fake, meta("M1-T01"), closed="completed", labels=("forge:task", "status:done"))
    waiting = seed(fake, meta("M1-T02", deps=["M1-T01"]), labels=("forge:task", "status:blocked"))
    m2 = seed(fake, meta("M2-T01", deps=["M1-T02"]), milestone="M2")
    plan = plan_of(task("M1-T01"), task("M1-T02", deps=["M1-T01"]), task("M1-T03", deps=["M1-T02"]))
    world.apply(plan)
    s = statuses(fake)
    assert s[waiting] == ["status:ready"]
    assert s[dep] == ["status:done"]
    assert s[m2] == ["status:blocked"]  # other milestones are relabelled too
    created = by_title_id(fake)["M1-T03"][0]
    assert s[created.number] == ["status:blocked"]


def test_human_text_outside_regions_is_preserved_byte_for_byte(world: PlanWorld) -> None:
    m = meta("M1-T01")
    body = upsert_description("", "old\n") + "\r\nHuman *intro*  \n\n" + upsert_block("", m)
    body += "\n\n> footer with `<!-- forge:begin -->` quoted\n```\n<!-- forge:end -->\n```\n"
    number = seed(world.fake, m, body=body)
    world.apply(plan_of(task("M1-T01", "T", "new text\n", zones=["db"], done_when=["x"])))
    new = world.fake.get_issue(number).body

    def human(text: str) -> tuple[str, str, str]:
        before, _, after = split_description(text)
        head, _, tail = split_body(after)
        return before, head, tail

    assert human(new) == human(body)
    assert split_description(new)[1] == "new text\r\n"  # the body uses CRLF somewhere
    assert parse_body(new) == meta("M1-T01", zones=["db"], done_when=["x"])


def test_issue_with_unreadable_block_blocks_creation(world: PlanWorld) -> None:
    fake = world.fake
    fake.create_label("forge:task", "ededed")
    fake.create_issue("[M1-T01] broken", "<!-- forge:begin -->\nnope\n", labels=["forge:task"])
    result = world.apply(plan_of(task("M1-T01")))
    assert len(fake.list_issues(state="all")) == 1
    assert result.data["tasks"][0]["action"] == "skipped"
    assert any("cannot be read" in m.text for m in result.messages)


def test_description_with_forge_marker_is_refused_before_writing(world: PlanWorld) -> None:
    guard = RecordingGitHub(world.fake, read_only=True)
    with pytest.raises(ForgeError, match=r"plans/M1\.yml: task M1-T01: description"):
        world.apply(plan_of(task("M1-T01", description="<!-- forge:end -->\n")), github=guard)


def test_validation_errors_exit_1_and_touch_nothing(world: PlanWorld) -> None:
    guard = RecordingGitHub(world.fake)
    validation = PlanValidation(
        milestone="M1",
        plan=plan_of(task("M1-T01")),
        errors=(PlanIssue("error", "size_too_large", "too big", "plans/M1.yml", "M1-T01", "size"),),
        warnings=(),
        waves=(),
        coverage={},
        roadmap=None,
    )
    result = plan_apply(world.ctx(guard), validation)
    assert result.code == ExitCode.ERROR
    assert guard.calls == []
    assert result.data["applied"] is False and result.data["validation"]["ok"] is False
    assert any("plans/M1.yml, M1-T01, size: too big" in m.text for m in result.messages)


def test_shallow_clone_is_a_precondition(world: PlanWorld) -> None:
    path = world.git_world.base / "shallow"
    Git(world.git_world.base).run(
        "clone", "--quiet", "--depth", "1", f"file://{world.git_world.remote}", str(path)
    )
    ctx = ApplyCtx(world.config, Git(path), RecordingGitHub(world.fake, read_only=True))
    with pytest.raises(PreconditionError):
        plan_apply(ctx, validation_of(plan_of(task("M1-T01"))))


# --------------------------------------------------------------------------- failures


def test_github_failure_on_one_issue_others_proceed(world: PlanWorld) -> None:
    fake = world.fake
    a = seed(fake, meta("M1-T01"))
    b = seed(fake, meta("M1-T02"))

    class Flaky(RecordingGitHub):
        def __getattr__(self, name: str) -> Any:
            inner = super().__getattr__(name)
            if name != "update_issue":
                return inner

            def call(number: int, **kwargs: Any) -> Any:
                if number == a:
                    raise GitHubError("boom", status=502)
                return inner(number, **kwargs)

            return call

    plan = plan_of(task("M1-T01", "A"), task("M1-T02", "B"))
    result = world.apply(plan, github=Flaky(fake))
    assert result.code == ExitCode.ERROR
    assert any("#1 (M1-T01): update failed: boom" in m.text for m in result.messages)
    assert fake.get_issue(b).title == "[M1-T02] B"
    assert world.apply(plan).code == ExitCode.OK
    assert fake.get_issue(a).title == "[M1-T01] A"


def test_failed_comment_keeps_the_issue_open(world: PlanWorld) -> None:
    fake = world.fake
    number = seed(fake, meta("M1-T03"))

    class NoComments(RecordingGitHub):
        def __getattr__(self, name: str) -> Any:
            if name == "create_comment":

                def fail(*args: Any, **kwargs: Any) -> Any:
                    raise GitHubError("forbidden", status=403)

                return fail
            return super().__getattr__(name)

    result = world.apply(plan_of(task("M1-T01")), github=NoComments(fake))
    assert result.code == ExitCode.ERROR
    assert fake.get_issue(number).state == "open"
    world.apply(plan_of(task("M1-T01")))
    assert fake.get_issue(number).state == "closed"
    assert len(fake.list_comments(number)) == 1


# --------------------------------------------------------------------------- races


def _prepare(world: PlanWorld, github: Any, plan: Any) -> tuple[Any, ApplyState]:
    state = gather_apply_state(world.ctx(github), plan)
    return plan_with_comments(github, plan, state)


RACE_PLAN = plan_of(task("M1-T01"), task("M1-T02", deps=["M1-T01"]))


def _assert_one_open_per_id(fake: FakeGitHub) -> dict[str, int]:
    winners: dict[str, int] = {}
    for task_id, issues in by_title_id(fake).items():
        open_ = [i for i in issues if i.state == "open"]
        assert len(open_) == 1, f"{task_id}: {[i.number for i in open_]}"
        assert open_[0].number == min(i.number for i in issues)
        winners[task_id] = open_[0].number
        for loser in issues:
            if loser.state == "closed":
                assert loser.state_reason == "not_planned"
                assert not [lb for lb in loser.labels if lb.startswith("status:")]
                [comment] = fake.list_comments(loser.number)
                assert f"Duplicate of #{open_[0].number}" in comment.body
    return winners


def test_race_second_listing_before_first_create_converges(world: PlanWorld) -> None:
    gh_a, gh_b = RecordingGitHub(world.fake), RecordingGitHub(world.fake)
    plan_b, state_b = _prepare(world, gh_b, RACE_PLAN)  # B lists first...
    assert world.apply(RACE_PLAN, github=gh_a).code == ExitCode.OK  # ...A runs to the end
    report = execute_apply_plan(
        world.ctx(gh_b), plan_b, state_b
    )  # ...then B creates from its snapshot
    assert report.failures == []
    assert {d.action for d in report.duplicates} == {"close"}
    winners = _assert_one_open_per_id(world.fake)
    assert winners == {"M1-T01": 1, "M1-T02": 2}
    guard = RecordingGitHub(world.fake, read_only=True)
    assert world.apply(RACE_PLAN, github=guard).code == ExitCode.OK


def test_race_interleaved_creates_converge(world: PlanWorld) -> None:
    """A creates T01; B then runs to the end (creates both, sees A's T01); A creates T02
    and its re-listing sees B's lower T02."""
    gh_a, gh_b = RecordingGitHub(world.fake), RecordingGitHub(world.fake)
    plan_a, state_a = _prepare(world, gh_a, RACE_PLAN)
    plan_b, state_b = _prepare(world, gh_b, RACE_PLAN)
    reports: list[Any] = []
    gh_a.after["create_issue"] = lambda: reports.append(
        execute_apply_plan(world.ctx(gh_b), plan_b, state_b)
    )
    report_a = execute_apply_plan(world.ctx(gh_a), plan_a, state_a)
    [report_b] = reports
    assert report_a.failures == [] and report_b.failures == []
    winners = _assert_one_open_per_id(world.fake)
    # A created T01 first (#1); B created T01 (#2) and T02 (#3); A created T02 (#4).
    assert winners == {"M1-T01": 1, "M1-T02": 3}
    assert [d.issue for d in report_b.duplicates] == [2]
    assert [d.issue for d in report_a.duplicates] == [4]
    guard = RecordingGitHub(world.fake, read_only=True)
    assert world.apply(RACE_PLAN, github=guard).code == ExitCode.OK


def test_race_crash_before_relisting_is_cleaned_by_the_next_run(world: PlanWorld) -> None:
    gh_b = RecordingGitHub(world.fake)
    plan_b, state_b = _prepare(world, gh_b, RACE_PLAN)
    world.apply(RACE_PLAN)
    gh_b.before["list_issues"] = lambda: (_ for _ in ()).throw(Crash("died"))
    with pytest.raises(Crash):
        execute_apply_plan(world.ctx(gh_b), plan_b, state_b)
    assert len(world.fake.list_issues(state="open")) == 4
    world.apply(RACE_PLAN)
    _assert_one_open_per_id(world.fake)


LAG = 50
"""List calls a new issue stays out of the fake's issue index: longer than any run here."""


def test_back_to_back_applies_under_list_lag_create_no_duplicate(world: PlanWorld) -> None:
    """Dogfood regression: the second run's listing must see the issues the first created."""
    world.fake.fake_set_list_lag(LAG)
    first = world.apply(RACE_PLAN)
    assert first.code == ExitCode.OK
    assert first.data["counts"]["create"] == 2
    second = world.apply(RACE_PLAN)
    assert second.code == ExitCode.OK
    assert second.data["counts"]["create"] == 0
    assert second.data["counts"]["duplicates"] == 0
    world.fake.fake_set_list_lag(0)
    assert _assert_one_open_per_id(world.fake) == {"M1-T01": 1, "M1-T02": 2}
    assert len(world.fake.list_issues(state="all")) == 2
    guard = RecordingGitHub(world.fake, read_only=True)
    assert world.apply(RACE_PLAN, github=guard).code == ExitCode.OK


def test_back_to_back_applies_with_skewed_list_lag_create_no_duplicate(
    world: PlanWorld,
) -> None:
    """Review HIGH: the newest-issues view already shows the first run's issues while the
    filtered list still lags; the second run must not mistake them for seen-and-filtered."""
    world.fake.fake_set_list_lag(LAG, recent_calls=0)
    assert world.apply(RACE_PLAN).data["counts"]["create"] == 2
    second = world.apply(RACE_PLAN)
    assert second.code == ExitCode.OK
    assert second.data["counts"]["create"] == 0
    world.fake.fake_set_list_lag(0)
    assert _assert_one_open_per_id(world.fake) == {"M1-T01": 1, "M1-T02": 2}
    assert len(world.fake.list_issues(state="all")) == 2


def test_unguarded_list_lag_reproduces_the_dogfood_duplicates(world: PlanWorld) -> None:
    """Without the read-your-writes guard the lag duplicates issues (the bug seen on GitHub);
    once the index catches up, the next run's duplicate cleanup converges."""
    world.fake.fake_set_list_lag(LAG, guarded=False)
    world.apply(RACE_PLAN)
    world.apply(RACE_PLAN)
    world.fake.fake_set_list_lag(0)
    assert len(world.fake.list_issues(state="open")) == 4
    world.apply(RACE_PLAN)
    assert _assert_one_open_per_id(world.fake) == {"M1-T01": 1, "M1-T02": 2}


def test_race_interleaved_creates_under_list_lag_converge(world: PlanWorld) -> None:
    """Two leads race while neither run's creations are indexed yet: as without lag, A
    creates T01, B runs to the end, A creates T02; both re-listings see every issue."""
    world.fake.fake_set_list_lag(LAG)
    gh_a, gh_b = RecordingGitHub(world.fake), RecordingGitHub(world.fake)
    plan_a, state_a = _prepare(world, gh_a, RACE_PLAN)
    plan_b, state_b = _prepare(world, gh_b, RACE_PLAN)
    reports: list[Any] = []
    gh_a.after["create_issue"] = lambda: reports.append(
        execute_apply_plan(world.ctx(gh_b), plan_b, state_b)
    )
    report_a = execute_apply_plan(world.ctx(gh_a), plan_a, state_a)
    [report_b] = reports
    assert report_a.failures == [] and report_b.failures == []
    assert [d.issue for d in report_b.duplicates] == [2]
    assert [d.issue for d in report_a.duplicates] == [4]
    world.fake.fake_set_list_lag(0)
    assert _assert_one_open_per_id(world.fake) == {"M1-T01": 1, "M1-T02": 3}
    guard = RecordingGitHub(world.fake, read_only=True)
    assert world.apply(RACE_PLAN, github=guard).code == ExitCode.OK


def test_started_duplicate_is_kept_with_a_warning(world: PlanWorld) -> None:
    fake = world.fake
    first = seed(fake, meta("M1-T01"))
    second = seed(fake, meta("M1-T01"))
    world.push_event(second, "M1-T01", "claim")
    result = world.apply(plan_of(task("M1-T01")))
    assert fake.get_issue(second).state == "open"
    assert fake.get_issue(first).state == "open"
    assert any(f"#{second}: duplicate of #{first}" in m.text for m in result.messages)


def test_concurrent_label_and_milestone_creation_is_tolerated(world: PlanWorld) -> None:
    gh_a, gh_b = RecordingGitHub(world.fake), RecordingGitHub(world.fake)
    plan_b, state_b = _prepare(world, gh_b, RACE_PLAN)
    world.apply(RACE_PLAN, github=gh_a)
    report = execute_apply_plan(world.ctx(gh_b), plan_b, state_b)
    assert report.failures == []
    assert len(world.fake.list_milestones()) == 1


def test_compute_is_pure_and_needs_comments_of_listed_issues(world: PlanWorld) -> None:
    fake = world.fake
    number = seed(fake, meta("M1-T03"))
    state = gather_apply_state(world.ctx())
    plan = compute_apply_plan(plan_of(task("M1-T01")), state)
    assert plan.comment_issues == {number}
    fake.create_comment(number, PLAN_REMOVED_MARKER + "\nalready said\n")
    with_comments = replace(state, comments={number: tuple(fake.list_comments(number))})
    again = compute_apply_plan(plan_of(task("M1-T01")), with_comments)
    assert [c.issue for c in plan.comments] == [number]
    assert again.comments == () and [c.issue for c in again.closes] == [number]


def test_plan_path_in_comments_is_relative(world: PlanWorld) -> None:
    number = seed(world.fake, meta("M1-T03"))
    plan = replace(plan_of(task("M1-T01")), path=Path("/abs/repo/plans/M1.yml"))
    world.apply(plan)
    [comment] = world.fake.list_comments(number)
    assert "`plans/M1.yml`" in comment.body and "/abs/" not in comment.body


# --------------------------------------------------------------------------- review fixes


def test_claim_and_human_edit_right_before_the_update(world: PlanWorld) -> None:
    """HIGH: a claim landing after the fetch downgrades the update to the started path, and
    the body is rebuilt from the issue as it is right before the write."""
    fake = world.fake
    old = meta("M1-T01")
    labels = ("forge:task", "status:ready", "size:S")
    number = seed(fake, old, body="Intro.\n\n" + upsert_block("", old), labels=labels)
    guard = RecordingGitHub(fake)

    def race() -> None:
        world.push_event(number, "M1-T01", "claim")
        body = fake.get_issue(number).body
        fake.update_issue(number, body=body.replace("Intro.", "Intro edited by a human."))

    guard.before["get_issue"] = race
    plan = plan_of(
        task("M1-T01", "Renamed", "Fresh.\n", zones=["db"], size=Size.M, done_when=["new"])
    )
    result = world.apply(plan, github=guard)
    assert result.code == ExitCode.OK, result.messages
    issue = fake.get_issue(number)
    assert issue.title == "[M1-T01] Renamed"
    assert parse_body(issue.body) == replace(old, done_when=("new",))
    assert "Intro edited by a human.\n" in issue.body
    assert split_description(issue.body)[1] == "Fresh.\n"
    [comment] = fake.list_comments(number)
    assert "`zones`: [api] → [db]" in comment.body and "`size`: S → M" in comment.body
    assert "status:in-progress" in issue.labels
    assert "status:ready" not in issue.labels and "size:M" not in issue.labels
    assert result.data["execution"]["downgraded"] == [number]
    assert any("claimed during the run" in m.text for m in result.messages)
    quiet = RecordingGitHub(fake, read_only=True)
    assert world.apply(plan, github=quiet).code == ExitCode.OK
    assert quiet.writes == []


def test_branch_moved_without_a_claim_still_gets_the_full_update(world: PlanWorld) -> None:
    fake = world.fake
    number = seed(fake, meta("M1-T01"))
    world.push_event(number, "M1-T01", "claim")
    world.push_event(number, "M1-T01", "release")
    guard = RecordingGitHub(fake)
    guard.before["get_issue"] = lambda: world.push_event(number, "M1-T01", "release")
    world.apply(plan_of(task("M1-T01", zones=["db"])), github=guard)
    assert parse_body(fake.get_issue(number).body) == meta("M1-T01", zones=["db"])
    assert fake.list_comments(number) == []


def test_human_edit_during_run_is_kept_on_a_full_update(world: PlanWorld) -> None:
    fake = world.fake
    number = seed(fake, meta("M1-T01"))
    guard = RecordingGitHub(fake)
    guard.before["get_issue"] = lambda: fake.update_issue(
        number, body="Late note.\n\n" + fake.get_issue(number).body
    )
    world.apply(plan_of(task("M1-T01", "New", "D.\n", zones=["db"])), github=guard)
    body = fake.get_issue(number).body
    assert "Late note.\n\nOld description.\n" in body
    assert parse_body(body) == meta("M1-T01", zones=["db"])


def test_issue_closed_during_the_run_is_not_modified(world: PlanWorld) -> None:
    fake = world.fake
    number = seed(fake, meta("M1-T01"))
    guard = RecordingGitHub(fake)
    guard.before["get_issue"] = lambda: fake.update_issue(number, state="closed")
    before = None

    def snapshot_after_close() -> None:
        nonlocal before
        before = fake.get_issue(number)

    guard.after["get_issue"] = snapshot_after_close
    result = world.apply(plan_of(task("M1-T01", "New", zones=["db"])), github=guard)
    assert fake.get_issue(number) == before
    assert any("closed during the run" in m.text for m in result.messages)


def test_closed_not_planned_task_in_the_plan_warns(world: PlanWorld) -> None:
    number = seed(world.fake, meta("M1-T01"), closed="not_planned")
    result = world.apply(plan_of(task("M1-T01")))
    assert result.data["tasks"][0]["action"] == "closed"
    assert any(f"reopen #{number}, then re-run forge plan apply" in m.text for m in result.messages)
    completed = seed(world.fake, meta("M1-T02"), closed="completed")
    result = world.apply(plan_of(task("M1-T01"), task("M1-T02")))
    assert not any(f"reopen #{completed}" in m.text for m in result.messages)


HAND_BLOCK = """\
<!-- forge:begin -->
```yaml
id: M1-T01
done_when: [tests pass]
spec: specs/m1/core.md
covers: [AC-1]
depends_on: []
zones: [api]
size: S
tier: standard
type: impl
```
<!-- forge:end -->
"""


@pytest.mark.parametrize("claimed", [True, False])
def test_format_only_block_rewrite_is_not_a_metadata_change(
    world: PlanWorld, claimed: bool
) -> None:
    number = seed(world.fake, meta("M1-T01"), body="Notes.\n\n" + HAND_BLOCK)
    if claimed:
        world.push_event(number, "M1-T01", "claim")
    result = world.apply(plan_of(task("M1-T01", "Old title")), dry_run=True)
    [outcome] = result.data["tasks"]
    assert "done_when" not in outcome["fields"] and "metadata" not in outcome["fields"]
    assert "block_format" in outcome["fields"]
    changed = world.apply(plan_of(task("M1-T01", done_when=["other"])), dry_run=True)
    fields = changed.data["tasks"][0]["fields"]
    assert ("done_when" if claimed else "metadata") in fields and "block_format" not in fields


# --------------------------------------------------------------------------- proposals


def propose_issue(fake: FakeGitHub, title: str = "Cache pages") -> int:
    """A real `forge propose` issue (status:proposed, proposal footer, no forge:task)."""
    result = propose(ProposeContext(github=fake, login="bob"), title, "We should cache.\n")
    number: int = result.data["issue"]["number"]
    return number


def adopting(number: int) -> Any:
    return plan_of(
        replace(task("M1-T04", "Page cache", "Cache rendered pages.\n"), proposal=number)
    )


def test_plan_adopts_a_forge_propose_issue(world: PlanWorld) -> None:
    fake = world.fake
    number = propose_issue(fake)
    proposal_body = fake.get_issue(number).body
    plan = adopting(number)
    result = world.apply(plan)
    assert result.code == ExitCode.OK, result.messages
    assert [i.number for i in fake.list_issues(state="all")] == [number]
    issue = fake.get_issue(number)
    assert issue.title == "[M1-T04] Page cache" and issue.milestone == "M1"
    assert sorted(issue.labels) == sorted(
        ["forge:task", "size:S", "tier:standard", "type:impl", "status:ready"]
    )
    assert parse_body(issue.body, milestone="M1") == plan.tasks[0].meta
    before, region, after = split_description(issue.body)
    head, _, tail = split_body(after)
    assert (before, region) == ("", "Cache rendered pages.\n")
    assert head == "\n\n" + proposal_body + "\n" and tail == "\n"  # proposal kept byte for byte
    assert result.data["tasks"][0]["action"] == "adopt"
    quiet = RecordingGitHub(fake, read_only=True)
    again = world.apply(plan, github=quiet)
    assert quiet.writes == [] and again.data["tasks"][0]["action"] == "unchanged"


@pytest.mark.parametrize("mode", ["before", "after", "github_error"])
def test_interrupted_adoption_converges(
    world: PlanWorld, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fresh() -> tuple[FakeGitHub, int]:
        fake = FakeGitHub()
        return fake, propose_issue(fake)

    reference, number = fresh()
    recorder = RecordingGitHub(reference)
    assert world.apply(adopting(number), github=recorder).code == ExitCode.OK
    world.fake = reference
    expected = world.snapshot()
    monkeypatch.setattr(plan_apply_mod, "fetch_task_refs", lambda *a, **k: None)
    if mode == "github_error":
        # The adoption write fails: the labels must not be touched either, or the issue
        # would stop being a proposal before it carries its block.
        fake, number = fresh()
        flaky = RecordingGitHub(fake)
        flaky.fail["update_issue"] = 1
        result = world.apply(adopting(number), github=flaky)
        assert result.code == ExitCode.ERROR
        issue = fake.get_issue(number)
        assert issue.labels == ("status:proposed",) and "forge:begin" not in issue.body
        assert world.apply(adopting(number), github=fake).code == ExitCode.OK
        world.fake = fake
        assert world.snapshot() == expected
        return
    for k in range(1, len(recorder.writes) + 1):
        fake, number = fresh()
        kwargs = {"crash_before": k} if mode == "before" else {"crash_after": k}
        with pytest.raises(Crash):
            world.apply(adopting(number), github=RecordingGitHub(fake, **kwargs))
        assert world.apply(adopting(number), github=fake).code == ExitCode.OK
        world.fake = fake
        assert world.snapshot() == expected, f"diverged after a crash {mode} write {k}"


@pytest.mark.parametrize("case", ["missing", "closed", "not_proposal", "taken", "other_task"])
def test_proposal_that_cannot_be_adopted_fails_before_any_write(
    world: PlanWorld, case: str
) -> None:
    fake = world.fake
    number = propose_issue(fake)
    expected = {
        "missing": "does not exist",
        "closed": "is closed",
        "not_proposal": "is not a proposal",
        "taken": "already has issue",
        "other_task": "already carries task M1-T09",
    }[case]
    if case == "missing":
        number = 99
    elif case == "closed":
        fake.update_issue(number, state="closed", state_reason="not_planned")
    elif case == "not_proposal":
        number = fake.create_issue("plain", "text").number
    elif case == "taken":
        seed(fake, meta("M1-T04"))
    else:
        fake.update_issue(number, body=upsert_block("x\n", meta("M1-T09")))
    guard = RecordingGitHub(fake, read_only=True)
    with pytest.raises(ForgeError, match=expected) as info:
        world.apply(adopting(number), github=guard)
    assert info.value.code == ExitCode.ERROR
    assert f"task M1-T04: proposal #{number}" in info.value.message
    assert info.value.hint and "proposal" in info.value.hint


# --------------------------------------------------------------------------- second review


@pytest.mark.parametrize("reason", ["removed", "duplicate"])
def test_task_claimed_just_before_its_closure_is_left_open(world: PlanWorld, reason: str) -> None:
    fake = world.fake
    keep = seed(fake, meta("M1-T01"))
    full = ("forge:task", "status:ready", "size:S", "tier:standard", "type:impl")
    target = seed(fake, meta("M1-T03" if reason == "removed" else "M1-T01"), labels=full)
    guard = RecordingGitHub(fake)

    def claim() -> None:
        task_id = "M1-T03" if reason == "removed" else "M1-T01"
        world.push_event(target, task_id, "claim")
        fake.remove_label(target, "status:ready")  # what the claimant's post-claim steps do
        fake.add_labels(target, ["status:in-progress"])

    guard.on_issue[target] = claim
    plan = plan_of(task("M1-T01", "old title", "Old description.\n"))
    result = world.apply(plan, github=guard)
    assert result.code == ExitCode.OK, result.messages
    issue = fake.get_issue(target)
    assert issue.state == "open" and "status:in-progress" in issue.labels
    assert result.data["execution"]["kept"] == [target]
    comments = [c.body for c in fake.list_comments(target)]
    if reason == "removed":
        [body] = comments
        assert body.startswith(PLAN_WARNING_MARKER) and "@bob" in body and "left it open" in body
    else:
        assert comments == []
        assert any(f"duplicates #{keep}" in m.text for m in result.messages)
    if reason == "removed":
        quiet = RecordingGitHub(fake, read_only=True)
        assert world.apply(plan, github=quiet).code == ExitCode.OK


@pytest.mark.parametrize("claimed", [True, False])
def test_block_edited_mid_run_triggers_the_branch_recheck(world: PlanWorld, claimed: bool) -> None:
    """The planned update only renames; a mid-run block edit makes the rebuilt update
    structural, so the task branch is re-checked before writing."""
    fake = world.fake
    number = seed(fake, meta("M1-T01"))
    guard = RecordingGitHub(fake)

    def edit() -> None:
        body = fake.get_issue(number).body
        fake.update_issue(number, body=body.replace("zones: [api]", "zones: [db]"))
        if claimed:
            world.push_event(number, "M1-T01", "claim")

    guard.on_issue[number] = edit
    result = world.apply(plan_of(task("M1-T01", "Renamed", "Old description.\n")), github=guard)
    issue = fake.get_issue(number)
    assert issue.title == "[M1-T01] Renamed"
    applied = parse_body(issue.body)
    assert applied is not None
    if claimed:
        assert applied.zones == ("db",)  # the human edit stays: the task is started
        [comment] = fake.list_comments(number)
        assert "`zones`: [db] → [api]" in comment.body
        assert result.data["execution"]["downgraded"] == [number]
    else:
        assert applied.zones == ("api",)  # not started: the plan wins
        assert fake.list_comments(number) == []


def test_proposal_rejected_mid_run_is_not_adopted(world: PlanWorld) -> None:
    fake = world.fake
    number = propose_issue(fake)
    before = fake.get_issue(number)
    guard = RecordingGitHub(fake)
    reject = lambda: fake.remove_label(number, "status:proposed")  # noqa: E731
    # The first read is the gathering; reject right before the execution's re-read.
    guard.on_issue[number] = lambda: guard.on_issue.__setitem__(number, reject)
    result = world.apply(adopting(number), github=guard)
    issue = fake.get_issue(number)
    assert (issue.title, issue.body, issue.labels) == (before.title, before.body, ())
    assert any("no longer labelled status:proposed" in m.text for m in result.messages)


def test_dry_run_comment_bodies_match_the_posted_ones(world: PlanWorld) -> None:
    number = seed(world.fake, meta("M1-T01"))
    world.push_event(number, "M1-T01", "handoff", who="dana")
    plan = plan_of(task("M1-T01", zones=["db"]))
    planned = world.apply(plan, dry_run=True).data["comments"]
    world.apply(plan)
    [posted] = world.fake.list_comments(number)
    assert [c["body"] for c in planned] == [posted.body]
    assert "@dana" in posted.body


@pytest.mark.parametrize("client", ["fake", "gh_cli_style"])
def test_proposal_naming_a_pull_request(world: PlanWorld, client: str) -> None:
    fake = world.fake
    pr = fake.create_pull("work", "", "feature", "main").number
    github: Any = RecordingGitHub(fake, read_only=True)
    if client == "gh_cli_style":

        class GhStyle(RecordingGitHub):
            def get_issue(self, number: int) -> Any:
                raise GitHubError(f"#{number} is a pull request, not an issue")  # no status

        github = GhStyle(fake, read_only=True)
    with pytest.raises(ForgeError, match=r"does not exist \(or is a pull request\)"):
        world.apply(adopting(pr), github=github)


def test_proposal_read_failure_is_not_mistaken_for_a_missing_issue(world: PlanWorld) -> None:
    number = propose_issue(world.fake)
    guard = RecordingGitHub(world.fake, read_only=True)
    guard.fail["get_issue"] = 1
    with pytest.raises(GitHubError, match="injected failure"):
        world.apply(adopting(number), github=guard)


# --------------------------------------------------------------------------- third review


@pytest.mark.parametrize("event", ["claim", "release"])
def test_branch_moving_while_the_closure_comments_blocks_the_close(
    world: PlanWorld, event: str
) -> None:
    fake = world.fake
    full = ("forge:task", "status:ready", "size:S", "tier:standard", "type:impl")
    number = seed(fake, meta("M1-T03"), labels=full)
    guard = RecordingGitHub(fake)
    guard.before["create_comment"] = lambda: world.push_event(number, "M1-T03", event)
    result = world.apply(plan_of(task("M1-T01")), github=guard)
    assert result.code == ExitCode.OK, result.messages
    assert fake.get_issue(number).state == "open"
    bodies = [c.body for c in fake.list_comments(number)]
    if event == "claim":
        assert result.data["execution"]["kept"] == [number]
        assert len(bodies) == 2 and bodies[0].startswith(PLAN_REMOVED_MARKER)
        assert bodies[1].startswith(PLAN_WARNING_MARKER) and "left it open" in bodies[1]
    else:
        assert result.data["execution"]["kept"] == []
        assert any("moved during the run; not closed" in m.text for m in result.messages)
        world.apply(plan_of(task("M1-T01")))  # released: the next run closes it
        assert fake.get_issue(number).state_reason == "not_planned"


@pytest.mark.parametrize("edit", ["resolves", "other_change"])
def test_started_task_block_edited_mid_run_posts_only_the_rebuilt_warning(
    world: PlanWorld, edit: str
) -> None:
    fake = world.fake
    number = seed(fake, meta("M1-T01"), assignees=("bob",))
    world.push_event(number, "M1-T01", "claim")
    plan = plan_of(task("M1-T01", "Renamed", "Old description.\n", zones=["db"]))
    planned = world.apply(plan, dry_run=True).data["comments"]
    assert len(planned) == 1 and "`zones`: [api] → [db]" in planned[0]["body"]
    guard = RecordingGitHub(fake)

    def change() -> None:
        new = meta("M1-T01", zones=["db"], size=Size.S if edit == "resolves" else Size.M)
        fake.update_issue(number, body=upsert_block(fake.get_issue(number).body, new))

    guard.on_issue[number] = change
    world.apply(plan, github=guard)
    bodies = [c.body for c in fake.list_comments(number)]
    if edit == "resolves":
        assert bodies == []
    else:
        [body] = bodies
        assert "`size`: M → S" in body and "zones" not in body


def test_duplicate_not_closed_when_the_winner_lost_the_id(world: PlanWorld) -> None:
    fake = world.fake
    planned = task("M1-T01")
    full = ("forge:task", "status:ready", "size:S", "tier:standard", "type:impl")
    winner = seed(
        fake, planned.meta, title=issue_title(planned), labels=full, body=render_task_body(planned)
    )
    loser = seed(fake, planned.meta, labels=full)
    guard = RecordingGitHub(fake)
    guard.on_issue[winner] = lambda: fake.update_issue(
        winner, body=upsert_block(fake.get_issue(winner).body, meta("M1-T09"))
    )
    result = world.apply(plan_of(planned), github=guard)
    assert fake.get_issue(loser).state == "open"
    assert any(f"#{winner} no longer carries M1-T01" in m.text for m in result.messages)


def test_created_issue_that_lost_the_race_reports_the_winner(world: PlanWorld) -> None:
    gh_a, gh_b = RecordingGitHub(world.fake), RecordingGitHub(world.fake)
    gh_b.before["create_issue"] = lambda: world.apply(RACE_PLAN, github=gh_a)
    result = world.apply(RACE_PLAN, github=gh_b)  # B listed before A created anything
    tasks = {t["id"]: (t["issue"], t["created_issue"]) for t in result.data["tasks"]}
    assert tasks == {"M1-T01": (1, 3), "M1-T02": (2, 4)}
    assert result.data["execution"]["closed_duplicates"] == {"3": 1, "4": 2}
    _assert_one_open_per_id(world.fake)
