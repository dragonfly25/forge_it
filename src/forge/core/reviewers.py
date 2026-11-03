"""Reviewer selection for ``forge ship`` (spec §11.1, ADR 0012). Pure and deterministic.

:func:`select_reviewers` ranks the members of ``.forge/team.yml`` by how much their declared
providers overlap with the author's active agent (when ``review.cross_provider`` is true),
breaks ties with the team order rotated by the issue number, and tops the pull request up to
``review.required_approvals``. It never removes a reviewer.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from forge.core.team import Member

RANK_NO_OVERLAP = 1
RANK_PARTIAL_OVERLAP = 2
RANK_NO_PROVIDERS = 3
RANK_FULL_OVERLAP = 4

_REASONS = {
    RANK_NO_OVERLAP: "no provider overlap with the author's agent",
    RANK_PARTIAL_OVERLAP: "partial provider overlap with the author's agent",
    RANK_NO_PROVIDERS: "no providers declared in team.yml",
    RANK_FULL_OVERLAP: "providers all used by the author's agent",
}
_EQUAL_REASON = "any member (review.cross_provider is false)"


@dataclass(frozen=True)
class ReviewerChoice:
    """One selected reviewer, its rank (1 is preferred) and why it got that rank."""

    login: str
    rank: int
    reason: str

    def to_data(self) -> dict[str, Any]:
        """JSON form (``forge ship`` output)."""
        return {"login": self.login, "rank": self.rank, "reason": self.reason}


@dataclass(frozen=True)
class ReviewerSelection:
    """Result of :func:`select_reviewers`.

    ``existing`` are the logins already requested or already reviewing (me excluded);
    ``needed`` is how many more reviewers ``required`` asks for; ``selected`` the reviewers to
    request now (at most ``needed``).
    """

    required: int
    existing: tuple[str, ...]
    needed: int
    selected: tuple[ReviewerChoice, ...]
    warnings: tuple[str, ...] = ()

    @property
    def logins(self) -> list[str]:
        """The logins of :attr:`selected`, in order."""
        return [choice.login for choice in self.selected]

    def to_data(self) -> dict[str, Any]:
        """JSON form (``forge ship`` output)."""
        return {
            "required": self.required,
            "existing": list(self.existing),
            "needed": self.needed,
            "selected": [choice.to_data() for choice in self.selected],
        }


def provider_rank(mine: Iterable[str], theirs: Iterable[str]) -> int:
    """Rank of a member with providers ``theirs`` for an author whose agent uses ``mine``.

    1: no overlap; 2: partial overlap; 3: no providers declared; 4: full overlap (their
    providers are a subset of mine). Providers compare case-insensitively.
    """
    own = {p.casefold() for p in mine}
    other = {p.casefold() for p in theirs}
    if not other:
        return RANK_NO_PROVIDERS
    if not other & own:
        return RANK_NO_OVERLAP
    if other <= own:
        return RANK_FULL_OVERLAP
    return RANK_PARTIAL_OVERLAP


def _rotated(members: Sequence[Member], issue: int) -> list[Member]:
    if not members:
        return []
    offset = issue % len(members)
    return [*members[offset:], *members[:offset]]


def select_reviewers(
    members: Sequence[Member],
    *,
    me: str,
    my_providers: Sequence[str],
    issue: int,
    required: int,
    requested: Iterable[str] = (),
    reviewed: Iterable[str] = (),
    cross_provider: bool = True,
) -> ReviewerSelection:
    """Choose the reviewers to request on the pull request of ``issue`` (ADR 0012).

    - Candidates: ``members`` minus me (case-insensitive), minus anyone already requested or
      who already reviewed.
    - Ranking with ``cross_provider``: :func:`provider_rank`; without it all ranks are equal.
    - Ties: team order rotated left by ``issue % len(members)``, so requests spread across the
      team and the result is deterministic for a given issue.
    - Count: ``required`` minus the logins already requested or reviewing (me excluded), never
      below zero. A shortfall of candidates is a warning, never an error.
    """
    me_key = me.casefold()
    existing = tuple(
        dict.fromkeys(
            login
            for login in (*requested, *reviewed)
            if login.casefold() != me_key and login.strip()
        )
    )
    taken = {login.casefold() for login in existing} | {me_key}
    needed = max(0, required - len({login.casefold() for login in existing}))
    candidates: list[ReviewerChoice] = []
    for member in _rotated(members, issue):
        if member.github.casefold() in taken:
            continue
        taken.add(member.github.casefold())  # duplicate logins in team.yml count once
        if cross_provider:
            rank = provider_rank(my_providers, member.providers)
            reason = _REASONS[rank]
        else:
            rank, reason = RANK_NO_OVERLAP, _EQUAL_REASON
        candidates.append(ReviewerChoice(member.github, rank, reason))
    candidates.sort(key=lambda c: c.rank)  # stable: rotated team order within a rank
    selected = tuple(candidates[:needed])
    warnings: list[str] = []
    if needed and not candidates:
        warnings.append(
            f"no reviewer candidates in .forge/team.yml for #{issue}: "
            f"{needed} more review(s) needed (review.required_approvals = {required}); "
            "no review was requested"
        )
    elif len(selected) < needed:
        warnings.append(
            f"only {len(selected)} reviewer candidate(s) in .forge/team.yml for #{issue}; "
            f"review.required_approvals asks for {needed} more"
        )
    return ReviewerSelection(
        required=required,
        existing=existing,
        needed=needed,
        selected=selected,
        warnings=tuple(warnings),
    )
