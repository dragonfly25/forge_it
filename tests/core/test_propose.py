"""Tests for forge.core.propose (ADR 0012)."""

from __future__ import annotations

import pytest

from forge.core.errors import ExitCode, ForgeError
from forge.core.github_fake import FakeGitHub
from forge.core.propose import (
    PROPOSAL_MARKER,
    ProposeContext,
    proposal_footer,
    propose,
)
from forge.core.refresh import ensure_labels
from forge.core.tasks import Size, TaskMetadata, TaskType, upsert_block
from forge.core.tiers import Tier
from support.scenarios import AGENT


def make_task(fake: FakeGitHub, task_id: str = "M1-T03", milestone: str = "M1") -> int:
    ensure_labels(fake)
    fake.create_milestone(milestone)
    meta = TaskMetadata(
        id=task_id,
        spec="specs/m1/x.md",
        covers=(),
        depends_on=(),
        zones=("api",),
        size=Size.S,
        tier=Tier.STANDARD,
        type=TaskType.IMPL,
        done_when=("tests pass",),
    )
    issue = fake.create_issue(
        "[M1-T03] task",
        upsert_block("Body.\n", meta),
        labels=["forge:task", "status:in-progress"],
        milestone=milestone,
    )
    return issue.number


def test_issue_shape_with_current_task_and_profile() -> None:
    fake = FakeGitHub(user="alice")
    n = make_task(fake)
    ctx = ProposeContext(github=fake, login="alice", agent=AGENT, current_issue=n)
    result = propose(ctx, "  Add rate limiting  ", "We need it.\n\n")
    assert result.code == ExitCode.OK
    assert result.data["created"] is True
    issue = fake.get_issue(result.data["issue"]["number"])
    assert issue.title == "Add rate limiting"
    assert issue.labels == ("status:proposed",)
    assert issue.milestone == "M1"
    assert issue.body == (
        f"We need it.\n\n{PROPOSAL_MARKER}\n"
        f"Proposed by @alice (agent claude) while working on M1-T03 (#{n})\n"
    )
    assert result.data["task"] == {"id": "M1-T03", "issue": n, "milestone": "M1"}


def test_footer_without_task_or_profile_uses_gh_user() -> None:
    fake = FakeGitHub(user="dave")
    result = propose(ProposeContext(github=fake), "Idea", "Text")
    issue = fake.get_issue(result.data["issue"]["number"])
    assert issue.body == f"Text\n\n{PROPOSAL_MARKER}\nProposed by @dave\n"
    assert issue.milestone is None
    assert result.data["task"] is None


def test_footer_variants() -> None:
    assert proposal_footer("a", None, None) == f"{PROPOSAL_MARKER}\nProposed by @a"
    assert proposal_footer("a", AGENT, None).endswith("Proposed by @a (agent claude)")


def test_current_issue_not_a_task_warns() -> None:
    fake = FakeGitHub(user="alice")
    ensure_labels(fake)
    plain = fake.create_issue("plain", "no block")
    result = propose(
        ProposeContext(github=fake, login="alice", current_issue=plain.number), "Idea", "x"
    )
    assert result.data["created"] is True and result.data["task"] is None
    assert any("not used in the proposal" in m.text for m in result.messages)


def test_idempotent_on_same_open_title() -> None:
    fake = FakeGitHub(user="alice")
    first = propose(ProposeContext(github=fake, login="alice"), "Add Cache", "one")
    count = len(fake.list_issues(state="all"))
    again = propose(ProposeContext(github=fake, login="alice"), "  add cache ", "two")
    assert again.code == ExitCode.OK
    assert again.data["created"] is False
    assert again.data["issue"]["number"] == first.data["issue"]["number"]
    assert len(fake.list_issues(state="all")) == count
    assert fake.get_issue(first.data["issue"]["number"]).body.startswith("one")


def test_idempotent_under_list_lag() -> None:
    """Dogfood regression: GitHub's issue list lags a few seconds behind a creation."""
    fake = FakeGitHub(user="alice")
    fake.fake_set_list_lag(50)
    first = propose(ProposeContext(github=fake, login="alice"), "Add Cache", "one")
    again = propose(ProposeContext(github=fake, login="alice"), "add cache", "two")
    assert again.data["created"] is False
    assert again.data["issue"]["number"] == first.data["issue"]["number"]
    fake.fake_set_list_lag(0)
    assert [i.title for i in fake.list_issues(state="all")] == ["Add Cache"]


def test_unguarded_list_lag_would_duplicate_the_proposal() -> None:
    """The hazard the guard closes: a raw lagging list misses the first proposal."""
    fake = FakeGitHub(user="alice")
    fake.fake_set_list_lag(50, guarded=False)
    propose(ProposeContext(github=fake, login="alice"), "Add Cache", "one")
    again = propose(ProposeContext(github=fake, login="alice"), "Add Cache", "two")
    assert again.data["created"] is True


def test_closed_proposal_does_not_block_a_new_one() -> None:
    fake = FakeGitHub(user="alice")
    first = propose(ProposeContext(github=fake, login="alice"), "Idea", "one")
    fake.update_issue(first.data["issue"]["number"], state="closed")
    again = propose(ProposeContext(github=fake, login="alice"), "Idea", "two")
    assert again.data["created"] is True
    assert again.data["issue"]["number"] != first.data["issue"]["number"]


def test_empty_title_is_error() -> None:
    with pytest.raises(ForgeError) as info:
        propose(ProposeContext(github=FakeGitHub()), "   ", "x")
    assert info.value.code == ExitCode.ERROR
