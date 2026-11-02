"""Tests for forge.core.lease: lease comment format, activity, staleness and heartbeat."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from forge.core.errors import PreconditionError
from forge.core.github import Comment, GitHubError
from forge.core.github_fake import FakeGitHub
from forge.core.lease import (
    LEASE_MARKER,
    LeaseData,
    find_lease,
    format_time,
    is_lease_comment,
    is_stale,
    last_activity,
    parse_lease,
    parse_time,
    render_lease,
    stale_at,
    upsert_lease,
)
from forge.core.lease import heartbeat as run_heartbeat
from support.scenarios import Clock, World, claim_of, run

if TYPE_CHECKING:
    from conftest import GitWorld

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SHA = "a" * 40
OTHER = "b" * 40

DATA = LeaseData(
    task="M1-T07",
    owner="alice",
    agent="claude-pro-1",
    claim=SHA,
    claimed_at=T0,
    heartbeat_at=T0 + timedelta(hours=2),
)


def comment(cid: int, body: str) -> Comment:
    return Comment(cid, 7, body, "alice", T0, T0)


# --------------------------------------------------------------------------- format


def test_time_format_round_trip() -> None:
    assert format_time(T0) == "2026-11-02T09:00:00Z"
    plus2 = datetime(2026, 11, 2, 11, 0, 0, 123, tzinfo=timezone(timedelta(hours=2)))
    assert format_time(plus2) == "2026-11-02T09:00:00Z"
    assert parse_time("2026-11-02T09:00:00Z") == T0
    assert parse_time("2026-11-02T11:00:00+02:00") == T0
    with pytest.raises(ValueError):
        format_time(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        parse_time("2026-11-02T09:00:00")


def test_render_parse_round_trip() -> None:
    body = render_lease(DATA, branch="task/7")
    lines = body.rstrip("\n").splitlines()
    assert lines[0] == LEASE_MARKER
    assert lines[-1].startswith("<!-- forge:lease-data {") and lines[-1].endswith("} -->")
    assert "alice" in body and "claude-pro-1" in body and "task/7" in body
    payload = json.loads(lines[-1][len("<!-- forge:lease-data ") : -len(" -->")])
    assert payload == {
        "task": "M1-T07",
        "owner": "alice",
        "agent": "claude-pro-1",
        "claim": SHA,
        "claimed_at": "2026-11-02T09:00:00Z",
        "heartbeat_at": "2026-11-02T11:00:00Z",
        "released_at": None,
    }
    assert parse_lease(body) == DATA
    released = replace(DATA, released_at=T0 + timedelta(hours=3), agent=None)
    rendered = render_lease(released, branch="task/7")
    assert "Released: 2026-11-02T12:00:00Z" in rendered
    assert parse_lease(rendered) == released
    assert render_lease(released, branch="task/7") == rendered  # deterministic


@pytest.mark.parametrize(
    "body",
    [
        "just a comment",
        "<!-- forge:lease -->\nno data line\n",
        "<!-- forge:lease -->\n<!-- forge:lease-data {not json} -->\n",
        '<!-- forge:lease -->\n<!-- forge:lease-data {"task": "M1-T01"} -->\n',
        "<!-- forge:lease -->\n<!-- forge:lease-data "
        + json.dumps(
            {
                **json.loads(render_lease(DATA, branch="b").splitlines()[-1][22:-4]),
                "claimed_at": "yesterday",
            }
        )
        + " -->\n",
        "<!-- forge:lease -->\n<!-- forge:lease-data "
        + json.dumps(
            {**json.loads(render_lease(DATA, branch="b").splitlines()[-1][22:-4]), "owner": 3}
        )
        + " -->\n",
    ],
)
def test_parse_rejects_bad_data(body: str) -> None:
    assert parse_lease(body) is None


def test_marker_detection() -> None:
    assert is_lease_comment("  \n" + LEASE_MARKER + "\nx")
    assert not is_lease_comment("<!-- forge:lease-data {} -->")
    assert not is_lease_comment("quoting <!-- forge:lease --> later")


def test_newest_marked_comment_wins() -> None:
    old = render_lease(DATA, branch="task/7")
    new = render_lease(replace(DATA, owner="bob", claim=OTHER), branch="task/7")
    comments = [comment(1, old), comment(2, new), comment(3, "thanks!")]
    found = find_lease(comments)
    assert found is not None and found.comment.id == 2
    assert found.data is not None and found.data.owner == "bob"
    assert find_lease([comment(1, "hi")]) is None
    broken = find_lease([comment(1, old), comment(2, LEASE_MARKER + "\ngarbage")])
    assert broken is not None and broken.comment.id == 2 and broken.data is None


# --------------------------------------------------------------------------- upsert


def _issue(fake: FakeGitHub) -> int:
    return fake.create_issue("[M1-T07] x", "body").number


def test_upsert_creates_then_edits_in_place() -> None:
    fake = FakeGitHub()
    n = _issue(fake)
    fake.create_comment(n, "unrelated")
    first = upsert_lease(fake, n, DATA)
    assert len(fake.list_comments(n)) == 2
    second = upsert_lease(fake, n, replace(DATA, heartbeat_at=T0 + timedelta(hours=5)))
    assert second.id == first.id
    assert len(fake.list_comments(n)) == 2
    found = find_lease(fake.list_comments(n))
    assert found is not None and found.data is not None
    assert found.data.heartbeat_at == T0 + timedelta(hours=5)
    # same content: no write at all
    third = upsert_lease(fake, n, replace(DATA, heartbeat_at=T0 + timedelta(hours=5)))
    assert third.updated_at == second.updated_at


class ForbiddenEdits(FakeGitHub):
    def update_comment(self, comment_id: int, body: str) -> Comment:
        raise GitHubError("Resource not accessible", status=403)


def test_upsert_falls_back_to_a_new_comment() -> None:
    fake = ForbiddenEdits()
    n = _issue(fake)
    fake.create_comment(n, render_lease(DATA, branch=f"task/{n}"))  # someone else's
    mine = replace(DATA, owner="bob", claim=OTHER)
    created = upsert_lease(fake, n, mine)
    comments = fake.list_comments(n)
    assert len(comments) == 2 and comments[-1].id == created.id
    found = find_lease(comments)
    assert found is not None and found.data == mine  # the new one is the newest


# --------------------------------------------------------------------------- activity


def test_last_activity_counts_heartbeat_only_for_same_claim() -> None:
    tip = T0 + timedelta(hours=1)
    assert last_activity(tip, DATA, SHA) == T0 + timedelta(hours=2)
    assert last_activity(tip, DATA, OTHER) == tip  # left over from an earlier claim
    assert last_activity(tip, None, SHA) == tip
    later_tip = T0 + timedelta(hours=9)
    assert last_activity(later_tip, DATA, SHA) == later_tip


def test_staleness() -> None:
    assert stale_at(T0, 24) == T0 + timedelta(hours=24)
    assert not is_stale(T0, 24, T0 + timedelta(hours=24))
    assert is_stale(T0, 24, T0 + timedelta(hours=24, seconds=1))
    # clock skew: activity in the future is never stale
    assert not is_stale(T0 + timedelta(hours=3), 1, T0)


# --------------------------------------------------------------------------- heartbeat


def test_heartbeat_on_git_world(git_world: GitWorld) -> None:
    world = World(git_world)
    n = world.task("M1-T01", ["api"])
    assert run(world.ctx("alice")).claimed is not None

    beat = run_heartbeat(world.ctx("alice", clock=Clock(T0 + timedelta(hours=5))), n)
    assert beat.heartbeat_at == T0 + timedelta(hours=5)
    assert beat.stale_at == T0 + timedelta(hours=29)
    assert beat.to_data()["stale_at"] == "2026-11-03T14:00:00Z"
    found = find_lease(world.fake.list_comments(n))
    assert found is not None and found.data is not None
    assert found.data.heartbeat_at == T0 + timedelta(hours=5)
    assert found.data.claimed_at == T0  # kept
    assert sum(is_lease_comment(c.body) for c in world.fake.list_comments(n)) == 1

    with pytest.raises(PreconditionError, match="belongs to alice"):
        run_heartbeat(world.ctx("bob"), n)
    with pytest.raises(PreconditionError, match="login"):
        run_heartbeat(world.ctx("alice", profile_login="bob"), n)
    # my own stale claim is refused (ADR 0009, revised): it is renewed only by `forge reclaim`
    late = T0 + timedelta(hours=30)
    with pytest.raises(PreconditionError, match="went stale at 2026-11-03T14:00:00Z") as info:
        run_heartbeat(world.ctx("alice", clock=Clock(late)), n)
    assert info.value.hint is not None and f"forge reclaim {n}" in info.value.hint
    after = find_lease(world.fake.list_comments(n))
    assert after is not None and after.data is not None
    assert after.data.heartbeat_at == T0 + timedelta(hours=5)  # untouched
    assert beat.warnings == ()
    other = world.task("M1-T02", ["web"])
    with pytest.raises(PreconditionError, match="does not exist"):
        run_heartbeat(world.ctx("alice"), other)


def test_heartbeat_refuses_someone_elses_claim_fresh_or_stale(git_world: GitWorld) -> None:
    world = World(git_world)
    n = world.task("M1-T01", ["api"])
    assert run(world.ctx("alice")).claimed is not None
    with pytest.raises(PreconditionError, match="belongs to alice"):
        run_heartbeat(world.ctx("bob"), n)
    with pytest.raises(PreconditionError, match="belongs to alice"):
        run_heartbeat(world.ctx("bob", clock=Clock(T0 + timedelta(hours=30))), n)
    before = find_lease(world.fake.list_comments(n))
    assert before is not None and before.data is not None
    assert before.data.heartbeat_at == T0  # untouched


def test_heartbeat_after_reclaim_is_refused_to_the_previous_owner(git_world: GitWorld) -> None:
    world = World(git_world)
    n = world.task("M1-T01", ["api"])
    assert run(world.ctx("alice")).claimed is not None
    late = Clock(T0 + timedelta(hours=30))
    taken = run(world.ctx("bob", clock=late), task=str(n), reclaim=True)
    assert taken.claimed is not None and taken.claimed.reclaimed
    with pytest.raises(PreconditionError, match="belongs to bob"):
        run_heartbeat(world.ctx("alice", clock=late), n)


def test_lease_summary_states(git_world: GitWorld) -> None:
    from forge.core.lease import describe_lease, lease_summary

    assert lease_summary(None, None, lease_hours=24, now=T0) is None
    ended = lease_summary(DATA, None, lease_hours=24, now=T0)
    assert ended is not None and ended["state"] == "ended" and ended["stale_at"] is None
    released = lease_summary(replace(DATA, released_at=T0), None, lease_hours=24, now=T0)
    assert released is not None and released["state"] == "released"

    world = World(git_world)
    n = world.task("M1-T01", ["api"])
    assert run(world.ctx("alice")).claimed is not None
    git = world.git("alice")
    comments = world.fake.list_comments(n)
    live = describe_lease(git, world.ctx("alice").config, n, "M1-T01", comments, now=T0)
    assert live is not None
    assert live["state"] == "live" and live["owner"] == "alice"
    assert live["heartbeat_at"] == "2026-11-02T09:00:00Z"
    assert live["stale_at"] == "2026-11-03T09:00:00Z"
    later = T0 + timedelta(hours=25)
    stale = describe_lease(git, world.ctx("alice").config, n, "M1-T01", comments, now=later)
    assert stale is not None and stale["state"] == "stale"


def test_heartbeat_refuses_an_incomplete_claim(git_world: GitWorld) -> None:
    """The lease comment does not name the claim: a heartbeat must not complete it."""
    world = World(git_world)
    n = world.task("M1-T01", ["api"])
    world.push_event("alice", n, claim_of("M1-T01", "alice"), T0)
    with pytest.raises(PreconditionError, match="incomplete") as info:
        run_heartbeat(world.ctx("alice"), n)
    assert info.value.hint is not None and f"forge next --task {n}" in info.value.hint
    assert find_lease(world.fake.list_comments(n)) is None

    # a lease left over from an earlier claim does not count either
    first = run(world.ctx("bob", clock=Clock(T0 + timedelta(hours=30))), task=str(n), reclaim=True)
    assert first.claimed is not None
    world.push_event("alice", n, claim_of("M1-T01", "alice"), T0 + timedelta(hours=31))
    with pytest.raises(PreconditionError, match="incomplete"):
        run_heartbeat(world.ctx("alice", clock=Clock(T0 + timedelta(hours=32))), n)


def test_heartbeat_refuses_when_the_branch_moves_meanwhile(git_world: GitWorld) -> None:
    world = World(git_world)
    n = world.task("M1-T01", ["api"])
    assert run(world.ctx("alice")).claimed is not None
    git = world.git("alice")

    def push_on_top() -> None:
        git.after_fetch = None
        world.push_work("bob", n, "src/api/x.py", T0 + timedelta(minutes=5))

    git.after_fetch = push_on_top
    with pytest.raises(PreconditionError, match="moved") as info:
        run_heartbeat(world.ctx("alice", clock=Clock(T0 + timedelta(hours=1))), n)
    assert info.value.hint == "re-run `forge heartbeat`"
    found = find_lease(world.fake.list_comments(n))
    assert found is not None and found.data is not None and found.data.heartbeat_at == T0
    # re-running succeeds once the read is current
    beat = run_heartbeat(world.ctx("alice", clock=Clock(T0 + timedelta(hours=1))), n)
    assert beat.heartbeat_at == T0 + timedelta(hours=1)
