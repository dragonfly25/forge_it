"""Unit tests for forge.core.reviewers (ADR 0012 reviewer selection). Pure, no I/O."""

from __future__ import annotations

from forge.core.reviewers import (
    RANK_FULL_OVERLAP,
    RANK_NO_OVERLAP,
    RANK_NO_PROVIDERS,
    RANK_PARTIAL_OVERLAP,
    provider_rank,
    select_reviewers,
)
from forge.core.team import Member


def member(login: str, *providers: str) -> Member:
    return Member(github=login, name=login, roles=frozenset({"dev"}), providers=providers)


TEAM = (
    member("alice", "anthropic"),  # me
    member("bob", "openai"),  # no overlap
    member("carol", "anthropic", "openai"),  # partial
    member("dave"),  # none declared
    member("erin", "anthropic"),  # full overlap
    member("frank", "google"),  # no overlap
)


def pick(issue: int = 0, required: int = 1, **kwargs: object) -> list[str]:
    options: dict = {"me": "alice", "my_providers": ("anthropic",), **kwargs}
    return select_reviewers(TEAM, issue=issue, required=required, **options).logins


def test_provider_rank_each_case() -> None:
    assert provider_rank(["anthropic"], ["openai"]) == RANK_NO_OVERLAP
    assert provider_rank(["anthropic"], ["anthropic", "openai"]) == RANK_PARTIAL_OVERLAP
    assert provider_rank(["anthropic"], []) == RANK_NO_PROVIDERS
    assert provider_rank(["anthropic", "local"], ["anthropic"]) == RANK_FULL_OVERLAP
    assert provider_rank(["Anthropic"], ["ANTHROPIC"]) == RANK_FULL_OVERLAP


def test_ranks_in_order_no_overlap_partial_none_full() -> None:
    selection = select_reviewers(TEAM, me="alice", my_providers=("anthropic",), issue=0, required=5)
    assert [(c.login, c.rank) for c in selection.selected] == [
        ("bob", 1),
        ("frank", 1),
        ("carol", 2),
        ("dave", 3),
        ("erin", 4),
    ]
    assert selection.selected[0].reason == "no provider overlap with the author's agent"
    assert selection.warnings == ()


def test_each_rank_wins_when_better_ranks_are_taken() -> None:
    assert pick() == ["bob"]
    assert pick(requested=["dave"], reviewed=["x"], required=3) == ["bob"]
    taken = ["bob", "frank"]
    assert pick(required=3, requested=taken) == ["carol"]
    assert pick(required=4, requested=[*taken, "carol"]) == ["dave"]
    assert pick(required=5, requested=[*taken, "carol", "dave"]) == ["erin"]


def test_rotation_by_issue_number_is_deterministic() -> None:
    # Rank 1 holds bob (index 1) and frank (index 5); rotation by issue % 6 picks the first.
    assert pick(issue=0) == ["bob"]
    assert pick(issue=2) == ["frank"]  # rotated: carol, dave, erin, frank, alice, bob
    assert pick(issue=6) == ["bob"]
    assert pick(issue=2) == pick(issue=2)
    assert pick(issue=8) == pick(issue=2)


def test_excludes_me_case_insensitively_and_already_requested_or_reviewed() -> None:
    selection = select_reviewers(
        TEAM,
        me="ALICE",
        my_providers=("anthropic",),
        issue=0,
        required=3,
        requested=["Bob"],
        reviewed=["frank", "alice"],
    )
    assert selection.existing == ("Bob", "frank")  # me excluded
    assert selection.needed == 1
    assert selection.logins == ["carol"]
    assert "alice" not in pick(required=6)


def test_tops_up_to_required_approvals_only() -> None:
    assert pick(required=2) == ["bob", "frank"]
    assert pick(required=2, requested=["dave"]) == ["bob"]
    assert pick(required=2, requested=["dave"], reviewed=["zed"]) == []
    selection = select_reviewers(TEAM, me="alice", my_providers=("anthropic",), issue=0, required=0)
    assert selection.needed == 0 and selection.selected == () and selection.warnings == ()


def test_cross_provider_false_ranks_everyone_equally() -> None:
    selection = select_reviewers(
        TEAM, me="alice", my_providers=("anthropic",), issue=0, required=5, cross_provider=False
    )
    assert selection.logins == ["bob", "carol", "dave", "erin", "frank"]  # team order
    assert {c.rank for c in selection.selected} == {1}
    assert "cross_provider is false" in selection.selected[0].reason
    rotated = select_reviewers(
        TEAM, me="alice", my_providers=("anthropic",), issue=4, required=1, cross_provider=False
    )
    assert rotated.logins == ["erin"]


def test_no_candidates_is_a_warning() -> None:
    selection = select_reviewers(
        (member("alice", "anthropic"),),
        me="alice",
        my_providers=("anthropic",),
        issue=3,
        required=1,
    )
    assert selection.selected == ()
    assert selection.needed == 1
    assert len(selection.warnings) == 1 and "no reviewer candidates" in selection.warnings[0]
    empty = select_reviewers((), me="alice", my_providers=(), issue=1, required=1)
    assert empty.selected == () and empty.warnings


def test_shortfall_is_a_warning() -> None:
    selection = select_reviewers(
        (member("alice"), member("bob")), me="alice", my_providers=(), issue=0, required=2
    )
    assert selection.logins == ["bob"]
    assert "only 1 reviewer candidate" in selection.warnings[0]


def test_to_data_traces_ranks() -> None:
    selection = select_reviewers(
        TEAM, me="alice", my_providers=("anthropic",), issue=0, required=1, requested=["zed"]
    )
    assert selection.to_data() == {"required": 1, "existing": ["zed"], "needed": 0, "selected": []}
    data = select_reviewers(
        TEAM, me="alice", my_providers=("anthropic",), issue=0, required=1
    ).to_data()
    assert data["selected"] == [
        {"login": "bob", "rank": 1, "reason": "no provider overlap with the author's agent"}
    ]
