"""GitHub access: data model, the ``GitHubClient`` protocol and the ``gh`` CLI implementation.

Every GitHub call Forge makes goes through :class:`GitHubClient` (ADR 0007). Production code
uses :class:`GhCliClient`, which shells out to ``gh api`` (REST). Tests use
:class:`forge.core.github_fake.FakeGitHub`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from urllib.parse import quote, urlencode

from forge.core.errors import ForgeError, PreconditionError

IssueState = Literal["open", "closed"]
StateFilter = Literal["open", "closed", "all"]
ReviewEvent = Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]

STATE_REASONS: tuple[str, ...] = ("completed", "not_planned", "reopened")
"""Values GitHub accepts for an issue's ``state_reason``."""


class Unset(Enum):
    """Sentinel type for "argument not given" where ``None`` is a meaningful value."""

    UNSET = "UNSET"


UNSET = Unset.UNSET
"""Pass-through default for ``update_issue(milestone=...)``: leave the milestone unchanged."""


# --------------------------------------------------------------------------- data model


@dataclass(frozen=True)
class Label:
    """A repository label. ``color`` is six hex digits without ``#``."""

    name: str
    color: str
    description: str


@dataclass(frozen=True)
class Milestone:
    """A repository milestone."""

    number: int
    title: str
    state: IssueState
    description: str


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
    """A pull request. ``head`` and ``base`` are branch names."""

    number: int
    title: str
    body: str
    head: str
    base: str
    state: IssueState
    merged: bool
    draft: bool
    author: str
    requested_reviewers: tuple[str, ...]
    url: str


@dataclass(frozen=True)
class Review:
    """A pull request review. ``state`` is GitHub's: APPROVED, CHANGES_REQUESTED, COMMENTED…"""

    id: int
    pr_number: int
    author: str
    state: str
    body: str


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
    """Everything Forge needs from GitHub. Issue lists never include pull requests."""

    def current_user(self) -> str:
        """Login of the authenticated user."""
        ...

    # labels
    def list_labels(self) -> list[Label]:
        """All repository labels."""
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
        """Milestones in the given state."""
        ...

    def create_milestone(self, title: str, description: str = "") -> Milestone:
        """Create an open milestone."""
        ...

    def close_milestone(self, number: int) -> Milestone:
        """Close a milestone."""
        ...

    # issues
    def list_issues(
        self,
        *,
        state: StateFilter = "open",
        labels: Sequence[str] = (),
        milestone: str | None = None,
    ) -> list[Issue]:
        """Issues having all ``labels`` and, if given, the milestone with that title."""
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
        """Assign users."""
        ...

    def remove_assignees(self, number: int, logins: Sequence[str]) -> None:
        """Unassign users."""
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

    # pull requests
    def list_pulls(
        self,
        *,
        state: StateFilter = "open",
        head: str | None = None,
        base: str | None = None,
    ) -> list[PullRequest]:
        """Pull requests, optionally filtered by head and base branch names."""
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
    )


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
    return PullRequest(
        number=int(data["number"]),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        head=str((data.get("head") or {}).get("ref", "")),
        base=str((data.get("base") or {}).get("ref", "")),
        state="closed" if data.get("state") == "closed" else "open",
        merged=merged,
        draft=bool(data.get("draft")),
        author=_login(data.get("user")),
        requested_reviewers=tuple(_login(u) for u in data.get("requested_reviewers") or ()),
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


class GhCliClient:
    """``GitHubClient`` backed by ``gh api`` (REST).

    ``repo`` is ``owner/name``. ``runner`` receives the full argv (starting with ``gh``) and
    the stdin text; it defaults to ``subprocess.run``. Lists use ``--paginate --slurp`` and
    are flattened; request bodies are JSON sent with ``--input -``.
    """

    def __init__(self, repo: str, runner: Runner | None = None):
        if repo.count("/") != 1 or not all(repo.split("/")):
            raise GitHubError(
                f"invalid repository {repo!r}", hint="use the form owner/name, e.g. acme/shop"
            )
        self.repo = repo
        self.owner = repo.split("/")[0]
        self._runner: Runner = runner or _subprocess_runner

    # ------------------------------------------------------------------ plumbing

    def _path(self, suffix: str, query: dict[str, str] | None = None) -> str:
        path = f"repos/{self.repo}/{suffix}"
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

    # ------------------------------------------------------------------ labels

    def list_labels(self) -> list[Label]:
        """All repository labels."""
        rows = self._call("GET", self._path("labels", {"per_page": "100"}), paginate=True)
        return [_label_from_json(r) for r in rows or []]

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
        """Milestones in the given state."""
        path = self._path("milestones", {"state": state, "per_page": "100"})
        return [_milestone_from_json(r) for r in self._call("GET", path, paginate=True) or []]

    def create_milestone(self, title: str, description: str = "") -> Milestone:
        """Create an open milestone."""
        body = {"title": title, "description": description}
        return _milestone_from_json(self._call("POST", self._path("milestones"), body))

    def close_milestone(self, number: int) -> Milestone:
        """Close a milestone."""
        path = self._path(f"milestones/{number}")
        return _milestone_from_json(self._call("PATCH", path, {"state": "closed"}))

    # ------------------------------------------------------------------ issues

    def list_issues(
        self,
        *,
        state: StateFilter = "open",
        labels: Sequence[str] = (),
        milestone: str | None = None,
    ) -> list[Issue]:
        """Issues having all ``labels`` and, if given, the milestone with that title."""
        query = {"state": state, "per_page": "100"}
        if labels:
            query["labels"] = ",".join(labels)
        if milestone is not None:
            number = self._milestone_number(milestone)
            if number is None:
                return []
            query["milestone"] = str(number)
        rows = self._call("GET", self._path("issues", query), paginate=True) or []
        return [_issue_from_json(r) for r in rows if "pull_request" not in r]

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
        """Assign users."""
        if logins:
            path = self._path(f"issues/{number}/assignees")
            self._call("POST", path, {"assignees": list(logins)})

    def remove_assignees(self, number: int, logins: Sequence[str]) -> None:
        """Unassign users."""
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

    # ------------------------------------------------------------------ pull requests

    def list_pulls(
        self,
        *,
        state: StateFilter = "open",
        head: str | None = None,
        base: str | None = None,
    ) -> list[PullRequest]:
        """Pull requests, optionally filtered by head and base branch names."""
        query = {"state": state, "per_page": "100"}
        if head is not None:
            query["head"] = f"{self.owner}:{head}"
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


if TYPE_CHECKING:
    _check_gh: GitHubClient = GhCliClient("owner/repo")
