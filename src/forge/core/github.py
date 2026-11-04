"""GitHub access: data model, the ``GitHubClient`` protocol and the ``gh`` CLI implementation.

Every GitHub call Forge makes goes through :class:`GitHubClient` (ADR 0007). Production code
uses :class:`GhCliClient`, which shells out to ``gh api`` (REST). Tests use
:class:`forge.core.github_fake.FakeGitHub`.

List ordering is part of the contract: issues, pull requests, comments and reviews come in
creation order (oldest first), labels by name (case-insensitive) and milestones by number.

Read-your-writes. GitHub serves ``GET /repos/{r}/issues`` from an index that lags behind
writes, while ``GET /repos/{r}/issues/{n}`` is consistent at once. Measured on github.com
(2026-11-03): a new issue shows up in the ascending list ~2 s after its creation and in the
``direction=desc`` list after ~2-5 s; a label added to an issue changes label-filtered lists
after ~7 s. Pull request lists (``GET /pulls``, with or without ``head``) and comment lists
(``GET /issues/{n}/comments``) showed no lag. ``list_issues`` therefore closes the creation
gap itself (:func:`recover_unlisted`); a label or state change made in the last seconds may
still be missing from a filtered list (only creations are guarded).
"""

from __future__ import annotations

import email.utils
import json
import re
import subprocess
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, runtime_checkable
from urllib.parse import quote, urlencode

from forge.core.errors import ForgeError, PreconditionError

IssueState = Literal["open", "closed"]
StateFilter = Literal["open", "closed", "all"]
ReviewEvent = Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]
CiState = Literal["success", "failure", "pending", "none"]

STATE_REASONS: tuple[str, ...] = ("completed", "not_planned", "reopened")
"""Values GitHub accepts for an issue's ``state_reason``."""

MIN_GH_VERSION: tuple[int, int, int] = (2, 48, 0)
"""Oldest ``gh`` release with ``gh api --paginate --slurp``, which :class:`GhCliClient` uses."""

_GH_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_gh_version(text: str) -> tuple[int, int, int]:
    """Parse the output of ``gh --version`` (e.g. ``gh version 2.92.0 (2026-04-28)``).

    Raises ``ValueError`` if no ``X.Y.Z`` version is found.
    """
    match = _GH_VERSION_RE.search(text)
    if match is None:
        raise ValueError(f"cannot find a gh version in {text.strip()[:80]!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


class Unset(Enum):
    """Sentinel type for "argument not given" where ``None`` is a meaningful value."""

    UNSET = "UNSET"


UNSET = Unset.UNSET
"""Pass-through default for ``update_issue(milestone=...)``: leave the milestone unchanged."""


# --------------------------------------------------------------------------- read-your-writes

LIST_PROBE_CAP = 50
"""Most ``GET /issues/{n}`` probes above the newest known number in one ``list_issues``."""

RECENT_WINDOW = 10
"""How many of the newest issue and pull request rows ``list_issues`` reads (one request) to
find the newest number, the recent issues its filtered list missed, and the gaps."""

GAP_SPAN = 2 * RECENT_WINDOW
"""Gaps are only looked for among the ``GAP_SPAN`` numbers just below the newest one."""

_T = TypeVar("_T")


class Probe(Enum):
    """What reading one issue number found, when it is not an item to add."""

    MISSING = "missing"
    """The number was never allocated (HTTP 404)."""

    SKIP = "skip"
    """The number exists but is not wanted (a pull request, filtered out, deleted, moved)."""


def recover_unlisted(
    listed: Iterable[int],
    recent: Mapping[int, _T | Probe],
    fetch: Callable[[int], _T | Probe],
    *,
    window: int = RECENT_WINDOW,
    cap: int = LIST_PROBE_CAP,
    gap_span: int = GAP_SPAN,
) -> list[_T]:
    """Items a lagging issue list may have missed: the read-your-writes guard of ``list_issues``.

    ``listed`` holds the numbers the filtered list returned. ``recent`` maps the newest
    numbers of the unfiltered issue index (issues and pull requests, at most ``window`` of
    them; fewer means the index holds nothing older) to the row when it passes the same
    filters, else ``Probe.SKIP``. The two lists are separate requests and may lag
    differently, so a recent match missing from ``listed`` is added without a call.

    Issue and pull request numbers are allocated in sequence, so any other missed creation
    is above the newest known number or a gap below it. ``fetch(n)`` reads number ``n``
    from a consistent endpoint (the item to add, ``Probe.SKIP`` or ``Probe.MISSING``):

    1. above: ``newest + 1``, ``newest + 2``… until ``Probe.MISSING``, at most ``cap`` reads;
    2. gaps: numbers below the newest that neither list returned, only among the
       ``gap_span`` numbers just below it (and not below the oldest recent number when the
       recent window is full); a ``Probe.MISSING`` there is skipped.

    Cost in reads: normally 1 (the 404 above the newest number) plus one per gap, i.e. per
    deleted issue or GitHub Discussion (they share the number sequence) among the
    ``gap_span`` numbers below the newest. Worst case ``cap + gap_span`` (70 by default),
    reached only after ``cap`` creations within the few seconds of index lag; beyond that
    the result may be incomplete. A Discussion created in the lag window just above the
    newest issue stops step 1 early (accepted: rare, and ``forge plan apply`` re-checks
    duplicates on every run).
    """
    listed_set = set(listed)
    found: list[_T] = [
        item
        for number, item in sorted(recent.items())
        if number not in listed_set and not isinstance(item, Probe)
    ]
    known = listed_set | recent.keys()
    newest = max(known, default=0)
    number = newest + 1
    for _ in range(cap):
        item = fetch(number)
        if item is Probe.MISSING:
            break
        if not isinstance(item, Probe):
            found.append(item)
        number += 1
    low = min(recent) if len(recent) >= window else 1
    for number in range(max(low, newest - gap_span), newest):
        if number in known:
            continue
        item = fetch(number)
        if not isinstance(item, Probe):
            found.append(item)
    return found


# --------------------------------------------------------------------------- data model


@dataclass(frozen=True)
class Label:
    """A repository label. ``color`` is six hex digits without ``#``."""

    name: str
    color: str
    description: str


@dataclass(frozen=True)
class Milestone:
    """A repository milestone. ``due_on`` is timezone-aware when set."""

    number: int
    title: str
    state: IssueState
    description: str
    due_on: datetime | None


@dataclass(frozen=True)
class Issue:
    """An issue (never a pull request). ``milestone`` is the milestone title."""

    number: int
    title: str
    body: str
    state: IssueState
    state_reason: str | None
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    milestone: str | None
    author: str
    url: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True)
class Comment:
    """A comment on an issue or pull request."""

    id: int
    issue_number: int
    body: str
    author: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PullRequest:
    """A pull request. ``head`` and ``base`` are branch names; ``head_sha`` is the head commit.

    ``mergeable`` is ``None`` when GitHub has not computed it yet; list endpoints never include
    it, so read it with ``get_pull``.

    ``merge_commit_sha`` is GitHub's field of the same name. Once the pull request is merged it
    is the commit the base branch got: the merge commit, the squashed commit, or (rebase) the
    commit the base branch was updated to. Before the merge it is a test merge commit, so only
    use it when ``merged``. Read it with ``get_pull``; list items may omit it (``None``).
    """

    number: int
    title: str
    body: str
    head: str
    head_sha: str
    base: str
    state: IssueState
    merged: bool
    merged_at: datetime | None
    mergeable: bool | None
    draft: bool
    author: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    requested_reviewers: tuple[str, ...]
    url: str
    merge_commit_sha: str | None = None


@dataclass(frozen=True)
class Review:
    """A pull request review. ``state`` is GitHub's: APPROVED, CHANGES_REQUESTED, COMMENTED…"""

    id: int
    pr_number: int
    author: str
    state: str
    body: str


@dataclass(frozen=True)
class Release:
    """A GitHub release. ``tag`` is the tag name; ``target`` is GitHub's ``target_commitish``
    (a branch name or a sha; GitHub ignores it when the tag already exists)."""

    id: int
    tag: str
    name: str
    body: str
    draft: bool
    prerelease: bool
    target: str
    url: str


@dataclass(frozen=True)
class CiStatus:
    """Combined CI state of a commit: its check runs plus its commit statuses.

    ``state`` is ``failure`` if anything failed, else ``pending`` if anything is still running,
    else ``none`` when there is nothing at all (``total == 0``), else ``success``. ``total``
    counts the check runs (latest per app, check suite and name) and statuses (latest per
    context) considered; ``failing`` and ``pending`` hold their names (check run name or
    status context), sorted and without duplicates.
    """

    state: CiState
    total: int
    failing: tuple[str, ...]
    pending: tuple[str, ...]


CI_FAILING_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)
"""Conclusions of a completed check run that make the commit's CI fail. Every other
conclusion (``success``, ``neutral``, ``skipped``, ``stale``…) counts as passing."""

CI_FAILING_STATUSES: frozenset[str] = frozenset({"failure", "error"})
"""Commit status states that make the commit's CI fail."""


def combine_ci(
    check_runs: Sequence[tuple[str, str, str | None]],
    statuses: Sequence[tuple[str, str]],
) -> CiStatus:
    """Combine check runs ``(name, status, conclusion)`` and statuses ``(context, state)``.

    Inputs must already be reduced to the latest check run per app, check suite and name, and
    the latest status per context; two runs may share a name. See :class:`CiStatus` for the rules.
    """
    failing: set[str] = set()
    pending: set[str] = set()
    for name, status, conclusion in check_runs:
        if status != "completed":
            pending.add(name)
        elif conclusion in CI_FAILING_CONCLUSIONS:
            failing.add(name)
    for context, status_state in statuses:
        if status_state in CI_FAILING_STATUSES:
            failing.add(context)
        elif status_state == "pending":
            pending.add(context)
    total = len(check_runs) + len(statuses)
    state: CiState
    if failing:
        state = "failure"
    elif pending:
        state = "pending"
    elif total == 0:
        state = "none"
    else:
        state = "success"
    return CiStatus(state, total, tuple(sorted(failing)), tuple(sorted(pending)))


class GitHubError(ForgeError):
    """A GitHub request failed. ``status`` is the HTTP status when known."""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        status: int | None = None,
        stderr: str = "",
    ):
        super().__init__(message, hint=hint)
        self.status = status
        self.stderr = stderr


# --------------------------------------------------------------------------- protocol


@runtime_checkable
class GitHubClient(Protocol):
    """Everything Forge needs from GitHub. Issue lists never include pull requests.

    Ordering: issues, pull requests, comments and reviews are listed by creation, oldest
    first; labels by name (case-insensitive); milestones by number.
    """

    def current_user(self) -> str:
        """Login of the authenticated user."""
        ...

    def server_time(self) -> datetime | None:
        """GitHub's current time (the ``Date`` header of a response), ``None`` if unavailable."""
        ...

    # labels
    def list_labels(self) -> list[Label]:
        """All repository labels, by name (case-insensitive)."""
        ...

    def create_label(self, name: str, color: str, description: str = "") -> Label:
        """Create a label."""
        ...

    def update_label(
        self,
        name: str,
        *,
        new_name: str | None = None,
        color: str | None = None,
        description: str | None = None,
    ) -> Label:
        """Change a label's name, color or description."""
        ...

    # milestones
    def list_milestones(self, state: StateFilter = "all") -> list[Milestone]:
        """Milestones in the given state, by number."""
        ...

    def create_milestone(self, title: str, description: str = "") -> Milestone:
        """Create an open milestone."""
        ...

    def close_milestone(self, number: int) -> Milestone:
        """Close a milestone."""
        ...

    def update_milestone(
        self,
        number: int,
        *,
        title: str | None = None,
        description: str | None = None,
        state: IssueState | None = None,
        due_on: datetime | None = None,
    ) -> Milestone:
        """Change a milestone; ``None`` leaves a field unchanged. ``due_on`` must be aware."""
        ...

    # issues
    def list_issues(
        self,
        *,
        state: StateFilter = "open",
        labels: Sequence[str] = (),
        milestone: str | None = None,
    ) -> list[Issue]:
        """Issues having all ``labels`` and, if given, the milestone with that title.

        Oldest first. Read-your-writes for creations: an issue created before the call (by
        anyone) is included even while GitHub's list index lags (see the module docstring).
        """
        ...

    def get_issue(self, number: int) -> Issue:
        """One issue. Raises ``GitHubError`` if it does not exist or is a pull request."""
        ...

    def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: Sequence[str] = (),
        milestone: str | None = None,
        assignees: Sequence[str] = (),
    ) -> Issue:
        """Create an issue. ``milestone`` is a title."""
        ...

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
        ...

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        """Add labels to an issue or pull request."""
        ...

    def remove_label(self, number: int, label: str) -> None:
        """Remove a label. A label the issue does not have is not an error."""
        ...

    def set_labels(self, number: int, labels: Sequence[str]) -> None:
        """Replace all labels of an issue or pull request."""
        ...

    def add_assignees(self, number: int, logins: Sequence[str]) -> None:
        """Assign users to an issue or pull request."""
        ...

    def remove_assignees(self, number: int, logins: Sequence[str]) -> None:
        """Unassign users from an issue or pull request."""
        ...

    # comments
    def list_comments(self, number: int) -> list[Comment]:
        """Comments of an issue or pull request, oldest first."""
        ...

    def create_comment(self, number: int, body: str) -> Comment:
        """Post a comment."""
        ...

    def update_comment(self, comment_id: int, body: str) -> Comment:
        """Edit a comment in place."""
        ...

    def delete_comment(self, comment_id: int) -> None:
        """Delete a comment."""
        ...

    # pull requests
    def list_pulls(
        self,
        *,
        state: StateFilter = "open",
        head: str | None = None,
        base: str | None = None,
    ) -> list[PullRequest]:
        """Pull requests, optionally filtered by head and base branch names. Oldest first."""
        ...

    def get_pull(self, number: int) -> PullRequest:
        """One pull request."""
        ...

    def create_pull(
        self, title: str, body: str, head: str, base: str, *, draft: bool = False
    ) -> PullRequest:
        """Open a pull request from branch ``head`` into ``base``."""
        ...

    def update_pull(
        self, number: int, *, title: str | None = None, body: str | None = None
    ) -> PullRequest:
        """Update a pull request's title or body."""
        ...

    def request_reviewers(self, number: int, logins: Sequence[str]) -> None:
        """Request reviews from users."""
        ...

    def submit_review(self, number: int, event: ReviewEvent, body: str) -> Review:
        """Submit a review."""
        ...

    def list_reviews(self, number: int) -> list[Review]:
        """Reviews of a pull request, oldest first."""
        ...

    def pull_files(self, number: int) -> list[str]:
        """Paths changed by a pull request."""
        ...

    def commit_pulls(self, sha: str) -> list[PullRequest]:
        """Pull requests associated with commit ``sha``.

        For a commit on the default branch, GitHub returns the merged pull request that
        introduced it; otherwise open and merged pull requests containing it. Order as
        returned by GitHub. List endpoints never include ``mergeable``.
        """
        ...

    # CI
    def commit_ci(self, sha: str) -> CiStatus:
        """Combined CI state of a commit (check runs and commit statuses)."""
        ...

    # releases
    def get_release_by_tag(self, tag: str) -> Release | None:
        """The published release for tag ``tag``, or ``None`` when there is none.

        Draft releases are not returned (GitHub's get-by-tag endpoint only serves published
        releases).
        """
        ...

    def create_release(
        self, tag: str, name: str, body: str, *, target: str | None = None
    ) -> Release:
        """Publish a release (not a draft, not a prerelease) for tag ``tag``.

        When the tag does not exist yet GitHub creates it from ``target`` (default branch by
        default); Forge always pushes the tag first, so ``target`` is then ignored. A tag that
        already has a release fails with ``GitHubError`` (HTTP 422).
        """
        ...


# --------------------------------------------------------------------------- gh CLI client

Runner = Callable[[list[str], str | None], tuple[int, str, str]]
"""Runs a full argv (``["gh", "api", ...]``) with optional stdin; returns (code, out, err)."""

_AUTH_PATTERNS: tuple[str, ...] = ("gh auth login", "authentication", "http 401")
_STATUS_RE = re.compile(r"HTTP (\d{3})")


def _subprocess_runner(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
    """Default runner: ``subprocess.run`` with captured text output."""
    proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _req_time(value: Any) -> datetime:
    parsed = _parse_time(value)
    if parsed is None:
        raise GitHubError(f"GitHub returned an object without a timestamp: {value!r}")
    return parsed


def _login(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("login", ""))
    return ""


def _issue_from_json(data: dict[str, Any]) -> Issue:
    milestone = data.get("milestone")
    return Issue(
        number=int(data["number"]),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        state="closed" if data.get("state") == "closed" else "open",
        state_reason=data.get("state_reason"),
        labels=tuple(str(lb["name"]) for lb in data.get("labels") or ()),
        assignees=tuple(_login(u) for u in data.get("assignees") or ()),
        milestone=str(milestone["title"]) if isinstance(milestone, dict) else None,
        author=_login(data.get("user")),
        url=str(data.get("html_url") or ""),
        created_at=_req_time(data.get("created_at")),
        updated_at=_req_time(data.get("updated_at")),
        closed_at=_parse_time(data.get("closed_at")),
    )


def _label_from_json(data: dict[str, Any]) -> Label:
    return Label(
        name=str(data["name"]),
        color=str(data.get("color") or ""),
        description=str(data.get("description") or ""),
    )


def _milestone_from_json(data: dict[str, Any]) -> Milestone:
    return Milestone(
        number=int(data["number"]),
        title=str(data["title"]),
        state="closed" if data.get("state") == "closed" else "open",
        description=str(data.get("description") or ""),
        due_on=_parse_time(data.get("due_on")),
    )


def _format_time(value: datetime) -> str:
    """Format an aware datetime as GitHub's ``YYYY-MM-DDTHH:MM:SSZ``."""
    if value.tzinfo is None:
        raise GitHubError(
            f"naive datetime {value.isoformat()} sent to GitHub",
            hint="pass a timezone-aware datetime (e.g. datetime(..., tzinfo=UTC))",
        )
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _comment_from_json(data: dict[str, Any]) -> Comment:
    issue_url = str(data.get("issue_url") or "")
    tail = issue_url.rstrip("/").rsplit("/", 1)[-1]
    return Comment(
        id=int(data["id"]),
        issue_number=int(tail) if tail.isdigit() else 0,
        body=str(data.get("body") or ""),
        author=_login(data.get("user")),
        created_at=_req_time(data.get("created_at")),
        updated_at=_req_time(data.get("updated_at")),
    )


def _pull_from_json(data: dict[str, Any]) -> PullRequest:
    merged = bool(data.get("merged")) or data.get("merged_at") is not None
    head = data.get("head") or {}
    mergeable = data.get("mergeable")
    return PullRequest(
        number=int(data["number"]),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        head=str(head.get("ref", "")),
        head_sha=str(head.get("sha") or ""),
        base=str((data.get("base") or {}).get("ref", "")),
        state="closed" if data.get("state") == "closed" else "open",
        merged=merged,
        merged_at=_parse_time(data.get("merged_at")),
        mergeable=bool(mergeable) if mergeable is not None else None,
        draft=bool(data.get("draft")),
        author=_login(data.get("user")),
        labels=tuple(str(lb["name"]) for lb in data.get("labels") or ()),
        assignees=tuple(_login(u) for u in data.get("assignees") or ()),
        requested_reviewers=tuple(_login(u) for u in data.get("requested_reviewers") or ()),
        url=str(data.get("html_url") or ""),
        merge_commit_sha=str(data["merge_commit_sha"]) if data.get("merge_commit_sha") else None,
    )


def _latest_by_key(
    rows: Sequence[dict[str, Any]], key: Callable[[dict[str, Any]], Hashable]
) -> list[dict[str, Any]]:
    """Keep, for each value of ``key(row)``, the row with the highest ``id`` (newest)."""
    latest: dict[Hashable, dict[str, Any]] = {}
    for row in rows:
        group = key(row)
        current = latest.get(group)
        if current is None or int(row.get("id") or 0) > int(current.get("id") or 0):
            latest[group] = row
    return list(latest.values())


def _nested_id(row: dict[str, Any], field: str) -> str | None:
    """``row[field]["id"]`` as text, or ``None`` when the field or its id is missing."""
    value = row.get(field)
    if isinstance(value, dict) and value.get("id") is not None:
        return str(value["id"])
    return None


def _check_run_key(row: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """Identity of a check run across re-runs: ``(app id, check suite id, name)``.

    Runs with the same name from different apps or suites are distinct checks (one may fail
    while the other passes); a missing app or suite collapses to ``None``.
    """
    return (_nested_id(row, "app"), _nested_id(row, "check_suite"), str(row.get("name") or ""))


def _status_key(row: dict[str, Any]) -> str:
    """Identity of a commit status: its ``context``."""
    return str(row.get("context") or "")


def _page_items(pages: Sequence[Any], field: str) -> list[dict[str, Any]]:
    """Collect ``page[field]`` from paginated object responses (e.g. ``check_runs``)."""
    items: list[dict[str, Any]] = []
    for page in pages:
        if isinstance(page, dict):
            items.extend(r for r in page.get(field) or () if isinstance(r, dict))
    return items


def _release_from_json(data: dict[str, Any]) -> Release:
    return Release(
        id=int(data["id"]),
        tag=str(data.get("tag_name") or ""),
        name=str(data.get("name") or ""),
        body=str(data.get("body") or ""),
        draft=bool(data.get("draft")),
        prerelease=bool(data.get("prerelease")),
        target=str(data.get("target_commitish") or ""),
        url=str(data.get("html_url") or ""),
    )


def _review_from_json(data: dict[str, Any], pr_number: int) -> Review:
    return Review(
        id=int(data["id"]),
        pr_number=pr_number,
        author=_login(data.get("user")),
        state=str(data.get("state") or ""),
        body=str(data.get("body") or ""),
    )


def parse_date_header(text: str) -> datetime | None:
    """Return the ``Date`` HTTP header in ``gh api -i`` output, or ``None`` if absent."""
    for line in text.splitlines():
        if not line.strip():
            break  # end of the headers
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "date":
            try:
                parsed = email.utils.parsedate_to_datetime(value.strip())
            except (TypeError, ValueError):
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


class GhCliClient:
    """``GitHubClient`` backed by ``gh api`` (REST).

    ``repo`` is ``owner/name``; it may be ``None`` for calls that need no repository
    (:meth:`current_user`, :meth:`server_time`), and the repository methods then raise
    :class:`GitHubError`. ``runner`` receives the full argv (starting with ``gh``) and
    the stdin text; it defaults to ``subprocess.run``. Lists use ``--paginate --slurp`` and
    are flattened; request bodies are JSON sent with ``--input -``. Requires gh
    >= ``MIN_GH_VERSION``.
    """

    def __init__(self, repo: str | None = None, runner: Runner | None = None):
        if repo is not None and (repo.count("/") != 1 or not all(repo.split("/"))):
            raise GitHubError(
                f"invalid repository {repo!r}", hint="use the form owner/name, e.g. acme/shop"
            )
        self.repo: str | None = repo
        self.owner: str | None = repo.split("/")[0] if repo is not None else None
        self._runner: Runner = runner or _subprocess_runner

    # ------------------------------------------------------------------ plumbing

    def _require_repo(self) -> str:
        if self.repo is None:
            raise GitHubError(
                "this GitHub call needs a repository, but the client has none",
                hint="set `github.repo` in .forge/config.yml",
            )
        return self.repo

    def _path(self, suffix: str, query: dict[str, str] | None = None) -> str:
        path = f"repos/{self._require_repo()}/{suffix}"
        if query:
            path += "?" + urlencode(query, safe=":,/")
        return path

    def _call(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, paginate: bool = False
    ) -> Any:
        argv = ["gh", "api"]
        if method != "GET":
            argv += ["-X", method]
        if paginate:
            argv += ["--paginate", "--slurp"]
        argv.append(path)
        stdin: str | None = None
        if body is not None:
            argv += ["--input", "-"]
            stdin = json.dumps(body)
        try:
            code, out, err = self._runner(argv, stdin)
        except FileNotFoundError as exc:
            raise PreconditionError(
                "the GitHub CLI (gh) is not installed or not on PATH",
                hint="install it from https://cli.github.com and run `gh auth login`",
            ) from exc
        if code != 0:
            raise self._error(method, path, err)
        if not out.strip():
            return None
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"gh api {method} {path}: invalid JSON in response") from exc
        if paginate:
            return self._flatten(data)
        return data

    @staticmethod
    def _flatten(pages: Any) -> list[Any]:
        items: list[Any] = []
        for page in pages if isinstance(pages, list) else [pages]:
            if isinstance(page, list):
                items.extend(page)
            else:
                items.append(page)
        return items

    @staticmethod
    def _error(method: str, path: str, stderr: str) -> ForgeError:
        text = stderr.strip()
        lowered = text.lower()
        if any(p in lowered for p in _AUTH_PATTERNS):
            return PreconditionError(
                f"GitHub CLI is not authenticated: {text}", hint="gh auth login"
            )
        match = _STATUS_RE.search(text)
        status = int(match.group(1)) if match else None
        return GitHubError(f"gh api {method} {path} failed: {text}", status=status, stderr=text)

    def _milestone_number(self, title: str) -> int | None:
        for m in self.list_milestones("all"):
            if m.title == title:
                return m.number
        return None

    def _require_milestone(self, title: str) -> int:
        number = self._milestone_number(title)
        if number is None:
            raise GitHubError(
                f"milestone {title!r} does not exist in {self.repo}",
                hint="create it first (forge plan apply does this)",
            )
        return number

    # ------------------------------------------------------------------ user

    def current_user(self) -> str:
        """Login of the authenticated user."""
        data = self._call("GET", "user")
        return str(data["login"])

    def server_time(self) -> datetime | None:
        """GitHub's clock: the ``Date`` header of ``gh api -i /meta``.

        ``None`` when ``gh`` is missing, the call fails or the header cannot be read. Never
        raises for those cases, because callers only use it for a best-effort skew check.
        """
        try:
            code, out, _ = self._runner(["gh", "api", "-i", "/meta"], None)
        except OSError:
            return None
        return parse_date_header(out) if code == 0 else None

    # ------------------------------------------------------------------ labels

    def list_labels(self) -> list[Label]:
        """All repository labels, by name (case-insensitive)."""
        rows = self._call("GET", self._path("labels", {"per_page": "100"}), paginate=True)
        labels = [_label_from_json(r) for r in rows or []]
        return sorted(labels, key=lambda lb: (lb.name.lower(), lb.name))

    def create_label(self, name: str, color: str, description: str = "") -> Label:
        """Create a label."""
        body = {"name": name, "color": color.lstrip("#"), "description": description}
        return _label_from_json(self._call("POST", self._path("labels"), body))

    def update_label(
        self,
        name: str,
        *,
        new_name: str | None = None,
        color: str | None = None,
        description: str | None = None,
    ) -> Label:
        """Change a label's name, color or description."""
        body: dict[str, Any] = {}
        if new_name is not None:
            body["new_name"] = new_name
        if color is not None:
            body["color"] = color.lstrip("#")
        if description is not None:
            body["description"] = description
        path = self._path(f"labels/{quote(name, safe='')}")
        return _label_from_json(self._call("PATCH", path, body))

    # ------------------------------------------------------------------ milestones

    def list_milestones(self, state: StateFilter = "all") -> list[Milestone]:
        """Milestones in the given state, by number."""
        path = self._path("milestones", {"state": state, "per_page": "100"})
        rows = self._call("GET", path, paginate=True) or []
        return sorted((_milestone_from_json(r) for r in rows), key=lambda m: m.number)

    def create_milestone(self, title: str, description: str = "") -> Milestone:
        """Create an open milestone."""
        body = {"title": title, "description": description}
        return _milestone_from_json(self._call("POST", self._path("milestones"), body))

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
        """Change a milestone; ``None`` leaves a field unchanged. ``due_on`` must be aware."""
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if state is not None:
            payload["state"] = state
        if due_on is not None:
            payload["due_on"] = _format_time(due_on)
        path = self._path(f"milestones/{number}")
        return _milestone_from_json(self._call("PATCH", path, payload))

    # ------------------------------------------------------------------ issues

    def list_issues(
        self,
        *,
        state: StateFilter = "open",
        labels: Sequence[str] = (),
        milestone: str | None = None,
    ) -> list[Issue]:
        """Issues having all ``labels`` and, if given, the milestone with that title.

        Oldest first. The paginated list lags behind writes, so a second request reads the
        ``RECENT_WINDOW`` newest rows (``issues?state=all&sort=created&direction=desc``) and
        :func:`recover_unlisted` adds what the list missed: recent rows that pass the
        filters, then ``GET issues/{n}`` reads above the newest number (until a 404) and of
        the gaps just below it. Cost: normally two extra calls (that list and one 404), at
        most ``2 + LIST_PROBE_CAP + GAP_SPAN`` (72); see :func:`recover_unlisted`.
        """
        query = {"state": state, "sort": "created", "direction": "asc", "per_page": "100"}
        milestone_number: int | None = None
        if labels:
            query["labels"] = ",".join(labels)
        if milestone is not None:
            milestone_number = self._milestone_number(milestone)
            if milestone_number is None:
                # No guard needed: the milestone list was not observed to lag (2026-11-03),
                # and an issue cannot be in a milestone that does not exist.
                return []
            query["milestone"] = str(milestone_number)
        rows: list[dict[str, Any]] = (
            self._call("GET", self._path("issues", query), paginate=True) or []
        )
        recent_query = {
            "state": "all",
            "sort": "created",
            "direction": "desc",
            "per_page": str(RECENT_WINDOW),
        }
        recent_rows = self._call("GET", self._path("issues", recent_query)) or []
        wanted = {name.lower() for name in labels}

        def keep(data: Any, number: int) -> dict[str, Any] | Probe:
            return self._issue_row_if_wanted(data, number, state, wanted, milestone_number)

        def fetch(number: int) -> dict[str, Any] | Probe:
            try:
                data = self._call("GET", self._path(f"issues/{number}"))
            except GitHubError as exc:
                if exc.status == 404:
                    return Probe.MISSING
                if exc.status == 410:  # deleted
                    return Probe.SKIP
                raise
            return keep(data, number)

        recent = {int(r["number"]): keep(r, int(r["number"])) for r in recent_rows}
        extra = recover_unlisted([int(r["number"]) for r in rows], recent, fetch)
        if extra:
            rows = sorted([*rows, *extra], key=lambda r: (str(r["created_at"]), int(r["number"])))
        return [_issue_from_json(r) for r in rows if "pull_request" not in r]

    def _issue_row_if_wanted(
        self, data: Any, number: int, state: StateFilter, labels: set[str], milestone: int | None
    ) -> dict[str, Any] | Probe:
        """``data`` (issue ``number``'s JSON) if it is an issue of this repository passing the
        ``list_issues`` filters (labels compared case-insensitively), else ``Probe.SKIP``."""
        if not isinstance(data, dict) or "pull_request" in data:
            return Probe.SKIP
        repo_url = str(data.get("repository_url") or "")
        moved = repo_url and not repo_url.lower().endswith(f"/repos/{self.repo}".lower())
        if moved or int(data.get("number") or 0) != number:  # transferred (redirected)
            return Probe.SKIP
        if state != "all" and data.get("state") != state:
            return Probe.SKIP
        have = {str(lb.get("name", "")).lower() for lb in data.get("labels") or ()}
        if not labels <= have:
            return Probe.SKIP
        if milestone is not None:
            current = data.get("milestone")
            if not isinstance(current, dict) or current.get("number") != milestone:
                return Probe.SKIP
        return data

    def get_issue(self, number: int) -> Issue:
        """One issue. Raises ``GitHubError`` if it does not exist or is a pull request."""
        data = self._call("GET", self._path(f"issues/{number}"))
        if "pull_request" in data:
            raise GitHubError(f"#{number} is a pull request, not an issue")
        return _issue_from_json(data)

    def create_issue(
        self,
        title: str,
        body: str,
        *,
        labels: Sequence[str] = (),
        milestone: str | None = None,
        assignees: Sequence[str] = (),
    ) -> Issue:
        """Create an issue. ``milestone`` is a title."""
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        if assignees:
            payload["assignees"] = list(assignees)
        if milestone is not None:
            payload["milestone"] = self._require_milestone(milestone)
        return _issue_from_json(self._call("POST", self._path("issues"), payload))

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
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        if state_reason is not None:
            payload["state_reason"] = state_reason
        if milestone is None:
            payload["milestone"] = None
        elif not isinstance(milestone, Unset):
            payload["milestone"] = self._require_milestone(milestone)
        return _issue_from_json(self._call("PATCH", self._path(f"issues/{number}"), payload))

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        """Add labels to an issue or pull request."""
        if labels:
            self._call("POST", self._path(f"issues/{number}/labels"), {"labels": list(labels)})

    def remove_label(self, number: int, label: str) -> None:
        """Remove a label. A label the issue does not have is not an error."""
        path = self._path(f"issues/{number}/labels/{quote(label, safe='')}")
        try:
            self._call("DELETE", path)
        except GitHubError as exc:
            if exc.status != 404:
                raise

    def set_labels(self, number: int, labels: Sequence[str]) -> None:
        """Replace all labels of an issue or pull request."""
        self._call("PUT", self._path(f"issues/{number}/labels"), {"labels": list(labels)})

    def add_assignees(self, number: int, logins: Sequence[str]) -> None:
        """Assign users to an issue or pull request."""
        if logins:
            path = self._path(f"issues/{number}/assignees")
            self._call("POST", path, {"assignees": list(logins)})

    def remove_assignees(self, number: int, logins: Sequence[str]) -> None:
        """Unassign users from an issue or pull request."""
        if logins:
            path = self._path(f"issues/{number}/assignees")
            self._call("DELETE", path, {"assignees": list(logins)})

    # ------------------------------------------------------------------ comments

    def list_comments(self, number: int) -> list[Comment]:
        """Comments of an issue or pull request, oldest first."""
        path = self._path(f"issues/{number}/comments", {"per_page": "100"})
        return [_comment_from_json(r) for r in self._call("GET", path, paginate=True) or []]

    def create_comment(self, number: int, body: str) -> Comment:
        """Post a comment."""
        path = self._path(f"issues/{number}/comments")
        return _comment_from_json(self._call("POST", path, {"body": body}))

    def update_comment(self, comment_id: int, body: str) -> Comment:
        """Edit a comment in place."""
        path = self._path(f"issues/comments/{comment_id}")
        return _comment_from_json(self._call("PATCH", path, {"body": body}))

    def delete_comment(self, comment_id: int) -> None:
        """Delete a comment."""
        self._call("DELETE", self._path(f"issues/comments/{comment_id}"))

    # ------------------------------------------------------------------ pull requests

    def list_pulls(
        self,
        *,
        state: StateFilter = "open",
        head: str | None = None,
        base: str | None = None,
    ) -> list[PullRequest]:
        """Pull requests, optionally filtered by head and base branch names. Oldest first."""
        owner = self._require_repo().split("/")[0]
        query = {"state": state, "sort": "created", "direction": "asc", "per_page": "100"}
        if head is not None:
            query["head"] = f"{owner}:{head}"
        if base is not None:
            query["base"] = base
        rows = self._call("GET", self._path("pulls", query), paginate=True) or []
        return [_pull_from_json(r) for r in rows]

    def get_pull(self, number: int) -> PullRequest:
        """One pull request."""
        return _pull_from_json(self._call("GET", self._path(f"pulls/{number}")))

    def create_pull(
        self, title: str, body: str, head: str, base: str, *, draft: bool = False
    ) -> PullRequest:
        """Open a pull request from branch ``head`` into ``base``."""
        payload = {"title": title, "body": body, "head": head, "base": base, "draft": draft}
        return _pull_from_json(self._call("POST", self._path("pulls"), payload))

    def update_pull(
        self, number: int, *, title: str | None = None, body: str | None = None
    ) -> PullRequest:
        """Update a pull request's title or body."""
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        return _pull_from_json(self._call("PATCH", self._path(f"pulls/{number}"), payload))

    def request_reviewers(self, number: int, logins: Sequence[str]) -> None:
        """Request reviews from users."""
        if logins:
            path = self._path(f"pulls/{number}/requested_reviewers")
            self._call("POST", path, {"reviewers": list(logins)})

    def submit_review(self, number: int, event: ReviewEvent, body: str) -> Review:
        """Submit a review."""
        path = self._path(f"pulls/{number}/reviews")
        return _review_from_json(self._call("POST", path, {"event": event, "body": body}), number)

    def list_reviews(self, number: int) -> list[Review]:
        """Reviews of a pull request, oldest first."""
        path = self._path(f"pulls/{number}/reviews", {"per_page": "100"})
        return [_review_from_json(r, number) for r in self._call("GET", path, paginate=True) or []]

    def pull_files(self, number: int) -> list[str]:
        """Paths changed by a pull request."""
        path = self._path(f"pulls/{number}/files", {"per_page": "100"})
        return [str(r["filename"]) for r in self._call("GET", path, paginate=True) or []]

    def commit_pulls(self, sha: str) -> list[PullRequest]:
        """Pull requests associated with commit ``sha``.

        ``GET /repos/{owner}/{repo}/commits/{commit_sha}/pulls``, paginated. For a commit on
        the default branch GitHub returns the merged pull request that introduced it;
        otherwise open and merged pull requests containing it. The items are "pull request
        simple" objects: ``merged`` comes from ``merged_at`` and ``mergeable`` is ``None``.
        """
        path = self._path(f"commits/{quote(sha, safe='')}/pulls", {"per_page": "100"})
        return [_pull_from_json(r) for r in self._call("GET", path, paginate=True) or []]

    # ------------------------------------------------------------------ CI

    def commit_ci(self, sha: str) -> CiStatus:
        """Combined CI state of a commit (check runs and commit statuses).

        Check runs come from ``commits/{sha}/check-runs`` (``filter=latest``, then reduced to
        the newest run per app, check suite and name); statuses from the combined
        ``commits/{sha}/status`` endpoint, whose ``statuses`` list holds the latest status per
        context. Its top-level ``state`` is ignored because GitHub reports ``pending`` when
        there are no statuses.
        """
        ref = quote(sha, safe="")
        runs_path = self._path(f"commits/{ref}/check-runs", {"filter": "latest", "per_page": "100"})
        runs = _latest_by_key(
            _page_items(self._call("GET", runs_path, paginate=True) or [], "check_runs"),
            _check_run_key,
        )
        status_path = self._path(f"commits/{ref}/status", {"per_page": "100"})
        statuses = _latest_by_key(
            _page_items(self._call("GET", status_path, paginate=True) or [], "statuses"),
            _status_key,
        )
        return combine_ci(
            [
                (str(r.get("name") or ""), str(r.get("status") or ""), r.get("conclusion"))
                for r in runs
            ],
            [(str(s.get("context") or ""), str(s.get("state") or "")) for s in statuses],
        )

    # ------------------------------------------------------------------ releases

    def get_release_by_tag(self, tag: str) -> Release | None:
        """``GET /repos/{owner}/{repo}/releases/tags/{tag}``; a 404 means no published release."""
        try:
            data = self._call("GET", self._path(f"releases/tags/{quote(tag, safe='')}"))
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return _release_from_json(data)

    def create_release(
        self, tag: str, name: str, body: str, *, target: str | None = None
    ) -> Release:
        """``POST /repos/{owner}/{repo}/releases`` (published, not a prerelease,
        ``make_latest: legacy``).

        ``target`` becomes ``target_commitish``; GitHub ignores it when the tag exists.
        """
        payload: dict[str, Any] = {
            "tag_name": tag,
            "name": name,
            "body": body,
            "draft": False,
            "prerelease": False,
            # "legacy": GitHub picks the latest release by creation date and semantic version,
            # so publishing a backport (e.g. 1.1.5 after 1.2.0) does not make it "latest".
            "make_latest": "legacy",
        }
        if target is not None:
            payload["target_commitish"] = target
        return _release_from_json(self._call("POST", self._path("releases"), payload))


if TYPE_CHECKING:
    _check_gh: GitHubClient = GhCliClient("owner/repo")
