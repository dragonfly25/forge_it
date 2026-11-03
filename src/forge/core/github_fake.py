"""In-memory ``GitHubClient`` for tests (ADR 0007).

``FakeGitHub`` is deterministic: timestamps come from an injectable clock and ids increment.
It mirrors the GitHub rules Forge relies on:

- issue and pull request numbers share one counter;
- labels must exist before they are put on an issue. GitHub would silently auto-create a
  missing label; the fake raises ``GitHubError`` instead so that code which forgets to ensure
  its labels (``forge plan apply``) fails in tests rather than producing colourless labels;
- closing sets ``closed_at`` and ``state_reason`` (``completed`` by default), reopening clears
  ``closed_at`` and sets ``state_reason`` to ``reopened``;
- an author cannot approve or request changes on their own pull request;
- only one open pull request may exist for a given head/base pair;
- labels, assignees and comments work on pull request numbers too;
- merging refuses drafts and unmergeable pull requests (405), and closes the issues named by
  closing keywords only when the base is the repository's default branch;
- ``commit_pulls`` returns the pull requests linked to a commit. Links come from
  ``fake_link_commit`` and from ``fake_merge_pull``, which links the merged pull request to its
  ``head_sha``. The fake does not model the commits a real merge creates on the base branch
  (merge or squash commits), nor GitHub's rule that only the merged pull request is listed for
  a default-branch commit: tests link such commits explicitly with ``fake_link_commit``.

Ordering matches ``GitHubClient``: issues, pull requests, comments and reviews oldest first,
labels by name (case-insensitive), milestones by number. Methods prefixed ``fake_`` are test
helpers that drive state GitHub would change on its own (merges, pushes, the acting user).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from forge.core.github import (
    STATE_REASONS,
    UNSET,
    CiStatus,
    Comment,
    GitHubError,
    Issue,
    IssueState,
    Label,
    Milestone,
    PullRequest,
    Review,
    ReviewEvent,
    StateFilter,
    Unset,
)

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_NO_CI = CiStatus("none", 0, (), ())
_CLOSING_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE)
_REVIEW_STATES: dict[str, str] = {
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "COMMENT": "COMMENTED",
}


class _TickingClock:
    """Default clock: starts at 2026-01-01 UTC and advances one second per call."""

    def __init__(self) -> None:
        self._now = _EPOCH

    def __call__(self) -> datetime:
        current = self._now
        self._now += timedelta(seconds=1)
        return current


def _matches(state: StateFilter, actual: IssueState) -> bool:
    return state == "all" or state == actual


class FakeGitHub:
    """In-memory, deterministic implementation of ``GitHubClient``."""

    def __init__(
        self,
        *,
        repo: str = "acme/shop",
        user: str = "alice",
        clock: Callable[[], datetime] | None = None,
        default_branch: str = "main",
    ):
        self.repo = repo
        self.default_branch = default_branch
        self._user = user
        self._clock: Callable[[], datetime] = clock or _TickingClock()
        self._labels: dict[str, Label] = {}  # key: lower-cased name
        self._milestones: dict[int, Milestone] = {}
        self._issues: dict[int, Issue] = {}
        self._pulls: dict[int, PullRequest] = {}
        self._pull_files: dict[int, list[str]] = {}
        self._comments: dict[int, Comment] = {}
        self._reviews: dict[int, Review] = {}
        self._ci: dict[str, CiStatus] = {}  # key: commit sha
        self._commit_links: dict[str, list[int]] = {}  # key: commit sha, value: PR numbers
        self._next_number = 1  # shared by issues and pull requests
        self._next_milestone = 1
        self._next_comment_id = 1
        self._next_review_id = 1

    # ------------------------------------------------------------------ helpers

    def _url(self, kind: str, number: int) -> str:
        return f"https://github.com/{self.repo}/{kind}/{number}"

    def _take_number(self) -> int:
        number = self._next_number
        self._next_number += 1
        return number

    def _issue(self, number: int) -> Issue:
        if number in self._pulls:
            raise GitHubError(f"#{number} is a pull request, not an issue", status=404)
        try:
            return self._issues[number]
        except KeyError:
            raise GitHubError(f"issue #{number} not found", status=404) from None

    def _pull(self, number: int) -> PullRequest:
        try:
            return self._pulls[number]
        except KeyError:
            raise GitHubError(f"pull request #{number} not found", status=404) from None

    def _canonical_labels(self, labels: Sequence[str]) -> tuple[str, ...]:
        names: list[str] = []
        for name in labels:
            label = self._labels.get(name.lower())
            if label is None:
                raise GitHubError(
                    f"label {name!r} does not exist",
                    hint="create labels before using them (FakeGitHub does not auto-create)",
                    status=422,
                )
            if label.name not in names:
                names.append(label.name)
        return tuple(names)

    def _milestone_by_title(self, title: str) -> Milestone:
        for m in self._milestones.values():
            if m.title == title:
                return m
        raise GitHubError(f"milestone {title!r} does not exist", status=422)

    def _labels_of(self, number: int) -> tuple[str, ...]:
        if number in self._pulls:
            return self._pulls[number].labels
        return self._issue(number).labels

    def _store_labels(self, number: int, labels: tuple[str, ...]) -> None:
        if number in self._pulls:
            self._pulls[number] = replace(self._pulls[number], labels=labels)
        else:
            issue = self._issue(number)
            self._issues[number] = replace(issue, labels=labels, updated_at=self._clock())

    def _assignees_of(self, number: int) -> tuple[str, ...]:
        if number in self._pulls:
            return self._pulls[number].assignees
        return self._issue(number).assignees

    def _store_assignees(self, number: int, assignees: tuple[str, ...]) -> None:
        if number in self._pulls:
            self._pulls[number] = replace(self._pulls[number], assignees=assignees)
        else:
            issue = self._issue(number)
            self._issues[number] = replace(issue, assignees=assignees, updated_at=self._clock())

    def _require_target(self, number: int) -> None:
        if number not in self._issues and number not in self._pulls:
            raise GitHubError(f"issue #{number} not found", status=404)

    # ------------------------------------------------------------------ user

    def current_user(self) -> str:
        """Login of the acting user (see ``fake_set_user``)."""
        return self._user

    # ------------------------------------------------------------------ labels

    def list_labels(self) -> list[Label]:
        """All labels, by name (case-insensitive)."""
        return sorted(self._labels.values(), key=lambda lb: (lb.name.lower(), lb.name))

    def create_label(self, name: str, color: str, description: str = "") -> Label:
        """Create a label. Names are unique case-insensitively."""
        if name.lower() in self._labels:
            raise GitHubError(f"label {name!r} already exists", status=422)
        label = Label(name=name, color=color.lstrip("#"), description=description)
        self._labels[name.lower()] = label
        return label

    def update_label(
        self,
        name: str,
        *,
        new_name: str | None = None,
        color: str | None = None,
        description: str | None = None,
    ) -> Label:
        """Change a label; renaming also renames it on every issue."""
        old = self._labels.get(name.lower())
        if old is None:
            raise GitHubError(f"label {name!r} not found", status=404)
        renaming = new_name is not None and new_name.lower() != name.lower()
        if renaming and new_name is not None and new_name.lower() in self._labels:
            raise GitHubError(f"label {new_name!r} already exists", status=422)
        label = Label(
            name=new_name if new_name is not None else old.name,
            color=color.lstrip("#") if color is not None else old.color,
            description=description if description is not None else old.description,
        )
        del self._labels[name.lower()]
        self._labels[label.name.lower()] = label
        if label.name != old.name:
            for n, issue in self._issues.items():
                renamed = tuple(label.name if x == old.name else x for x in issue.labels)
                self._issues[n] = replace(issue, labels=renamed)
            for n, pr in self._pulls.items():
                renamed = tuple(label.name if x == old.name else x for x in pr.labels)
                self._pulls[n] = replace(pr, labels=renamed)
        return label

    # ------------------------------------------------------------------ milestones

    def list_milestones(self, state: StateFilter = "all") -> list[Milestone]:
        """Milestones in the given state, by number."""
        return [m for _, m in sorted(self._milestones.items()) if _matches(state, m.state)]

    def create_milestone(self, title: str, description: str = "") -> Milestone:
        """Create an open milestone. Titles are unique."""
        if any(m.title == title for m in self._milestones.values()):
            raise GitHubError(f"milestone {title!r} already exists", status=422)
        milestone = Milestone(
            number=self._next_milestone,
            title=title,
            state="open",
            description=description,
            due_on=None,
        )
        self._next_milestone += 1
        self._milestones[milestone.number] = milestone
        return milestone

    def close_milestone(self, number: int) -> Milestone:
        """Close a milestone."""
        return self.update_milestone(number, state="closed")

    def update_milestone(
        self,
        number: int,
        *,
        title: str | None = None,
        description: str | None = None,
        state: IssueState | None = None,
        due_on: datetime | None = None,
    ) -> Milestone:
        """Change a milestone; renaming keeps titles unique and renames it on issues."""
        old = self._milestones.get(number)
        if old is None:
            raise GitHubError(f"milestone {number} not found", status=404)
        if state is not None and state not in ("open", "closed"):
            raise GitHubError(f"invalid state {state!r}", status=422)
        if due_on is not None and due_on.tzinfo is None:
            raise GitHubError("due_on must be timezone-aware", status=422)
        taken = {m.title for m in self._milestones.values() if m.number != number}
        if title is not None and title in taken:
            raise GitHubError(f"milestone {title!r} already exists", status=422)
        milestone = replace(
            old,
            title=title if title is not None else old.title,
            description=description if description is not None else old.description,
            state=state if state is not None else old.state,
            due_on=due_on.astimezone(UTC) if due_on is not None else old.due_on,
        )
        self._milestones[number] = milestone
        if milestone.title != old.title:
            for n, issue in self._issues.items():
                if issue.milestone == old.title:
                    self._issues[n] = replace(issue, milestone=milestone.title)
        return milestone

    # ------------------------------------------------------------------ issues

    def list_issues(
        self,
        *,
        state: StateFilter = "open",
        labels: Sequence[str] = (),
        milestone: str | None = None,
    ) -> list[Issue]:
        """Issues (never pull requests) having all ``labels`` and the given milestone title."""
        wanted = {name.lower() for name in labels}
        result: list[Issue] = []
        for number in sorted(self._issues):
            issue = self._issues[number]
            if not _matches(state, issue.state):
                continue
            if not wanted <= {x.lower() for x in issue.labels}:
                continue
            if milestone is not None and issue.milestone != milestone:
                continue
            result.append(issue)
        return result

    def get_issue(self, number: int) -> Issue:
        """One issue. Raises ``GitHubError`` if missing or a pull request."""
        return self._issue(number)

    def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: Sequence[str] = (),
        milestone: str | None = None,
        assignees: Sequence[str] = (),
    ) -> Issue:
        """Create an issue authored by the current user."""
        canonical = self._canonical_labels(labels)
        if milestone is not None:
            self._milestone_by_title(milestone)
        now = self._clock()
        number = self._take_number()
        issue = Issue(
            number=number,
            title=title,
            body=body,
            state="open",
            state_reason=None,
            labels=canonical,
            assignees=tuple(dict.fromkeys(assignees)),
            milestone=milestone,
            author=self._user,
            url=self._url("issues", number),
            created_at=now,
            updated_at=now,
            closed_at=None,
        )
        self._issues[number] = issue
        return issue

    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: IssueState | None = None,
        state_reason: str | None = None,
        milestone: str | Unset | None = UNSET,
    ) -> Issue:
        """Update fields. ``milestone=None`` clears it; ``UNSET`` leaves it unchanged."""
        issue = self._issue(number)
        if state_reason is not None and state_reason not in STATE_REASONS:
            raise GitHubError(f"invalid state_reason {state_reason!r}", status=422)
        if state is not None and state not in ("open", "closed"):
            raise GitHubError(f"invalid state {state!r}", status=422)
        now = self._clock()
        changes: dict[str, object] = {"updated_at": now}
        if title is not None:
            changes["title"] = title
        if body is not None:
            changes["body"] = body
        if not isinstance(milestone, Unset):
            if milestone is not None:
                self._milestone_by_title(milestone)
            changes["milestone"] = milestone
        if state == "closed" and issue.state == "open":
            changes.update(state="closed", closed_at=now, state_reason=state_reason or "completed")
        elif state == "open" and issue.state == "closed":
            changes.update(state="open", closed_at=None, state_reason="reopened")
        elif state_reason is not None:
            changes["state_reason"] = state_reason
        updated = replace(issue, **changes)  # type: ignore[arg-type]
        self._issues[number] = updated
        return updated

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        """Add existing labels to an issue or pull request."""
        self._require_target(number)
        canonical = self._canonical_labels(labels)
        current = self._labels_of(number)
        self._store_labels(number, current + tuple(x for x in canonical if x not in current))

    def remove_label(self, number: int, label: str) -> None:
        """Remove a label; absent labels are ignored."""
        self._require_target(number)
        current = self._labels_of(number)
        kept = tuple(x for x in current if x.lower() != label.lower())
        if kept != current:
            self._store_labels(number, kept)

    def set_labels(self, number: int, labels: Sequence[str]) -> None:
        """Replace all labels."""
        self._require_target(number)
        self._store_labels(number, self._canonical_labels(labels))

    def add_assignees(self, number: int, logins: Sequence[str]) -> None:
        """Assign users to an issue or pull request (duplicates ignored)."""
        self._require_target(number)
        merged = tuple(dict.fromkeys((*self._assignees_of(number), *logins)))
        self._store_assignees(number, merged)

    def remove_assignees(self, number: int, logins: Sequence[str]) -> None:
        """Unassign users; logins not assigned are ignored."""
        self._require_target(number)
        gone = set(logins)
        self._store_assignees(number, tuple(x for x in self._assignees_of(number) if x not in gone))

    # ------------------------------------------------------------------ comments

    def list_comments(self, number: int) -> list[Comment]:
        """Comments on an issue or pull request, oldest first."""
        self._require_target(number)
        return [c for c in self._comments.values() if c.issue_number == number]

    def create_comment(self, number: int, body: str) -> Comment:
        """Post a comment as the current user."""
        self._require_target(number)
        now = self._clock()
        comment = Comment(
            id=self._next_comment_id,
            issue_number=number,
            body=body,
            author=self._user,
            created_at=now,
            updated_at=now,
        )
        self._next_comment_id += 1
        self._comments[comment.id] = comment
        return comment

    def update_comment(self, comment_id: int, body: str) -> Comment:
        """Edit a comment in place and bump ``updated_at``."""
        if comment_id not in self._comments:
            raise GitHubError(f"comment {comment_id} not found", status=404)
        comment = replace(self._comments[comment_id], body=body, updated_at=self._clock())
        self._comments[comment_id] = comment
        return comment

    def delete_comment(self, comment_id: int) -> None:
        """Delete a comment."""
        if self._comments.pop(comment_id, None) is None:
            raise GitHubError(f"comment {comment_id} not found", status=404)

    # ------------------------------------------------------------------ pull requests

    def list_pulls(
        self,
        *,
        state: StateFilter = "open",
        head: str | None = None,
        base: str | None = None,
    ) -> list[PullRequest]:
        """Pull requests filtered by state and head/base branch names.

        Like GitHub's list endpoint, ``mergeable`` is not computed here (always ``None``);
        use :meth:`get_pull` to read it.
        """
        return [
            replace(pr, mergeable=None)
            for _, pr in sorted(self._pulls.items())
            if _matches(state, pr.state)
            and (head is None or pr.head == head)
            and (base is None or pr.base == base)
        ]

    def get_pull(self, number: int) -> PullRequest:
        """One pull request."""
        return self._pull(number)

    def create_pull(
        self, title: str, body: str, head: str, base: str, *, draft: bool = False
    ) -> PullRequest:
        """Open a pull request authored by the current user."""
        if head == base:
            raise GitHubError("head and base must be different branches", status=422)
        if self.list_pulls(state="open", head=head, base=base):
            raise GitHubError(f"a pull request already exists for {head} → {base}", status=422)
        number = self._take_number()
        pr = PullRequest(
            number=number,
            title=title,
            body=body,
            head=head,
            head_sha=hashlib.sha1(
                f"{self.repo}#{number}:{head}".encode(), usedforsecurity=False
            ).hexdigest(),
            base=base,
            state="open",
            merged=False,
            merged_at=None,
            mergeable=True,
            draft=draft,
            author=self._user,
            labels=(),
            assignees=(),
            requested_reviewers=(),
            url=self._url("pull", number),
        )
        self._pulls[number] = pr
        return pr

    def update_pull(
        self, number: int, *, title: str | None = None, body: str | None = None
    ) -> PullRequest:
        """Update title or body."""
        pr = self._pull(number)
        pr = replace(
            pr,
            title=title if title is not None else pr.title,
            body=body if body is not None else pr.body,
        )
        self._pulls[number] = pr
        return pr

    def request_reviewers(self, number: int, logins: Sequence[str]) -> None:
        """Request reviews; the author cannot be a reviewer (as on GitHub)."""
        pr = self._pull(number)
        if pr.author in logins:
            raise GitHubError("review cannot be requested from the pull request author", status=422)
        merged = tuple(dict.fromkeys((*pr.requested_reviewers, *logins)))
        self._pulls[number] = replace(pr, requested_reviewers=merged)

    def submit_review(self, number: int, event: ReviewEvent, body: str) -> Review:
        """Submit a review as the current user; clears their pending review request."""
        pr = self._pull(number)
        if event not in _REVIEW_STATES:
            raise GitHubError(f"invalid review event {event!r}", status=422)
        if event != "COMMENT" and pr.author == self._user:
            raise GitHubError(
                "can not approve or request changes on your own pull request", status=422
            )
        review = Review(
            id=self._next_review_id,
            pr_number=number,
            author=self._user,
            state=_REVIEW_STATES[event],
            body=body,
        )
        self._next_review_id += 1
        self._reviews[review.id] = review
        pending = tuple(x for x in pr.requested_reviewers if x != self._user)
        self._pulls[number] = replace(pr, requested_reviewers=pending)
        return review

    def list_reviews(self, number: int) -> list[Review]:
        """Reviews of a pull request, oldest first."""
        self._pull(number)
        return [r for r in self._reviews.values() if r.pr_number == number]

    def pull_files(self, number: int) -> list[str]:
        """Paths set with ``fake_add_pull_files``."""
        self._pull(number)
        return list(self._pull_files.get(number, []))

    def commit_pulls(self, sha: str) -> list[PullRequest]:
        """Pull requests linked to ``sha`` (``fake_link_commit``, ``fake_merge_pull``).

        Returned in the order they were linked, each at most once, with their current state.
        An unknown commit has no pull requests.
        """
        return [self._pulls[n] for n in self._commit_links.get(sha, [])]

    # ------------------------------------------------------------------ CI

    def commit_ci(self, sha: str) -> CiStatus:
        """Combined CI state of a commit; ``none`` unless set with ``fake_set_ci``."""
        return self._ci.get(sha, _NO_CI)

    # ------------------------------------------------------------------ test helpers

    def fake_set_ci(self, sha: str, status: CiStatus) -> None:
        """Set the CI state GitHub reports for commit ``sha``."""
        self._ci[sha] = status

    def fake_set_user(self, login: str) -> None:
        """Change the acting user (affects authorship of new objects)."""
        self._user = login

    def fake_merge_pull(self, number: int) -> PullRequest:
        """Merge an open pull request, as GitHub's merge endpoint would.

        Drafts and unmergeable pull requests are refused (405). Issues named by closing
        keywords in the body are closed only when the base is ``default_branch``. The pull
        request is linked to its ``head_sha`` for ``commit_pulls``.
        """
        pr = self._pull(number)
        if pr.state != "open":
            raise GitHubError(f"pull request #{number} is not open", status=405)
        if pr.draft:
            raise GitHubError(f"pull request #{number} is a draft", status=405)
        if pr.mergeable is False:
            raise GitHubError(f"pull request #{number} is not mergeable", status=405)
        pr = replace(pr, state="closed", merged=True, merged_at=self._clock())
        self._pulls[number] = pr
        if pr.head_sha:
            self.fake_link_commit(pr.head_sha, number)
        if pr.base != self.default_branch:
            return pr
        for match in _CLOSING_RE.finditer(pr.body):
            target = int(match.group(1))
            issue = self._issues.get(target)
            if issue is not None and issue.state == "open":
                self.update_issue(target, state="closed", state_reason="completed")
        return pr

    def fake_set_head_sha(self, number: int, sha: str) -> PullRequest:
        """Simulate a push to the pull request's head branch."""
        pr = replace(self._pull(number), head_sha=sha)
        self._pulls[number] = pr
        return pr

    def fake_set_mergeable(self, number: int, mergeable: bool | None) -> PullRequest:
        """Set what GitHub reports as ``mergeable`` (``None``: not computed yet)."""
        pr = replace(self._pull(number), mergeable=mergeable)
        self._pulls[number] = pr
        return pr

    def fake_set_draft(self, number: int, draft: bool) -> PullRequest:
        """Mark a pull request as draft or ready for review."""
        pr = replace(self._pull(number), draft=draft)
        self._pulls[number] = pr
        return pr

    def fake_link_commit(self, sha: str, pr_number: int) -> None:
        """Associate commit ``sha`` with pull request ``pr_number`` for ``commit_pulls``.

        Linking the same pair twice is a no-op; an unknown pull request raises (404).
        """
        self._pull(pr_number)
        links = self._commit_links.setdefault(sha, [])
        if pr_number not in links:
            links.append(pr_number)

    def fake_add_pull_files(self, number: int, files: Sequence[str]) -> None:
        """Record paths changed by a pull request."""
        self._pull(number)
        self._pull_files.setdefault(number, []).extend(files)


if TYPE_CHECKING:
    from forge.core.github import GitHubClient

    _check: GitHubClient = FakeGitHub()
