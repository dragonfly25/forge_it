"""Conflict escalation: the ``blocked:conflict`` label and escalation comments (ADR 0011).

Shared by the exit-4 path of ``forge sync`` (:func:`escalate_conflict`) and by
``forge escalate --reason R`` (:func:`escalate`). Both:

- add ``blocked:conflict`` to the task issue (creating the catalogue label if it is missing);
- post a comment starting with :data:`ESCALATION_MARKER`.

Every comment also carries a hidden key line (``<!-- forge:escalation-key K -->``). A comment
whose key is already on the issue is not posted again, so an escalation interrupted by a
GitHub failure converges when it is re-run instead of spamming the issues.

This module also holds what sync and escalate share about the local state files
(``<worktree>/.forge/local/sync.json`` and ``sync-log.json``) and the upstream context:
upstream commits mapped to their pull requests and the issues those pull requests close.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.core.errors import ForgeError, PreconditionError
from forge.core.git import CommitFiles, Git
from forge.core.github import Comment, GitHubClient, GitHubError, PullRequest
from forge.core.lease import load_task
from forge.core.tasks import BLOCKED_CONFLICT, LABELS, TaskMetadata, has_label
from forge.core.team import Team
from forge.core.workspace import LOCAL_DIR, find_current_task

ESCALATION_MARKER = "<!-- forge:escalation -->"
"""First line of every escalation comment (ADR 0011)."""

SYNC_FILE = LOCAL_DIR / "sync.json"
"""Path, relative to a worktree root, of the in-progress sync state."""

SYNC_LOG_FILE = LOCAL_DIR / "sync-log.json"
"""Path, relative to a worktree root, of the log of completed or escalated syncs."""

MAX_UPSTREAM_COMMITS = 20
"""At most this many relevant upstream commits are mapped to pull requests and issues."""

_KEY_PREFIX = "<!-- forge:escalation-key "
_CLOSING_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#([1-9][0-9]*)\b", re.IGNORECASE
)
_TASK_HEAD_RE = re.compile(r"^task/([1-9][0-9]*)$")


# --------------------------------------------------------------------------- helpers


def key_line(key: str) -> str:
    """The hidden line that identifies one escalation (or sync summary) comment."""
    return f"{_KEY_PREFIX}{key} -->"


def digest(*parts: str) -> str:
    """A short, stable key built from ``parts``."""
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def mention_list(logins: Iterable[str], *, exclude: Iterable[str] = ()) -> list[str]:
    """De-duplicate ``logins`` case-insensitively, in order, without ``exclude``."""
    skip = {login.casefold() for login in exclude}
    seen: set[str] = set()
    result: list[str] = []
    for login in logins:
        key = login.casefold()
        if not login or key in skip or key in seen:
            continue
        seen.add(key)
        result.append(login)
    return result


def closing_issues(text: str) -> list[int]:
    """Issue numbers named by closing keywords (``close[sd]``, ``fix(e[sd])``, ``resolve[sd]``)."""
    found: list[int] = []
    for match in _CLOSING_RE.finditer(text or ""):
        number = int(match.group(1))
        if number not in found:
            found.append(number)
    return found


def pull_issues(pr: PullRequest) -> list[int]:
    """Issues a pull request relates to: its closing keywords, then a ``task/<n>`` head."""
    found = closing_issues(pr.body)
    head = _TASK_HEAD_RE.match(pr.head)
    if head is not None and int(head.group(1)) not in found:
        found.append(int(head.group(1)))
    return found


def ensure_blocked(github: GitHubClient, issue_number: int, labels: Sequence[str]) -> bool:
    """Add ``blocked:conflict`` to the issue unless ``labels`` has it; return True if added.

    The catalogue label is created first when the repository lacks it.
    """
    if has_label(labels, BLOCKED_CONFLICT):
        return False
    if not any(label.name.casefold() == BLOCKED_CONFLICT for label in github.list_labels()):
        spec = next(s for s in LABELS if s.name == BLOCKED_CONFLICT)
        github.create_label(spec.name, spec.color, spec.description)
    github.add_labels(issue_number, [BLOCKED_CONFLICT])
    return True


def post_once(
    github: GitHubClient,
    issue_number: int,
    key: str,
    body: str,
    *,
    newest_only: bool = False,
) -> tuple[Comment | None, bool]:
    """Post ``body`` (which must contain :func:`key_line` of ``key``) unless already posted.

    Returns ``(comment, created)``. With ``newest_only`` only the newest comment carrying
    the same marker family (the first line of ``body``) counts as already posted; otherwise
    any comment carrying the key does.
    """
    line = key_line(key)
    if line not in body:
        raise ValueError("the comment body must carry its key line")
    comments = github.list_comments(issue_number)
    if newest_only:
        marker = body.splitlines()[0]
        family = [c for c in comments if c.body.startswith(marker)]
        candidates = family[-1:]
    else:
        candidates = comments
    for comment in candidates:
        if line in comment.body:
            return comment, False
    return github.create_comment(issue_number, body), True


def read_json_file(path: Path) -> Any:
    """Read a JSON state file; ``None`` when missing. Corrupt files raise :class:`ForgeError`."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ForgeError(
            f"{path}: invalid JSON ({exc.msg} at line {exc.lineno})",
            hint="fix or delete the file (it is local Forge state), then re-run the command",
        ) from None


# --------------------------------------------------------------------------- upstream context


@dataclass(frozen=True)
class UpstreamPull:
    """A pull request that brought upstream changes, with the issues it relates to."""

    number: int
    title: str
    author: str
    url: str
    head: str
    issues: tuple[int, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form stored in ``sync.json``."""
        return {
            "number": self.number,
            "title": self.title,
            "author": self.author,
            "url": self.url,
            "head": self.head,
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class UpstreamCommit:
    """A relevant upstream commit: it touches a shared file or a contract zone."""

    sha: str
    subject: str
    author: str
    files: tuple[str, ...]
    pulls: tuple[int, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON form stored in ``sync.json``."""
        return {
            "sha": self.sha,
            "subject": self.subject,
            "author": self.author,
            "files": list(self.files),
            "pulls": list(self.pulls),
        }


@dataclass(frozen=True)
class UpstreamContext:
    """Relevant upstream commits → pull requests → issues (ADR 0011)."""

    commits: tuple[UpstreamCommit, ...] = ()
    pulls: tuple[UpstreamPull, ...] = ()
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def issues(self) -> list[int]:
        """Every linked issue, in order of first appearance."""
        found: list[int] = []
        for pr in self.pulls:
            for number in pr.issues:
                if number not in found:
                    found.append(number)
        return found

    @property
    def authors(self) -> list[str]:
        """The authors of the upstream pull requests, de-duplicated."""
        return mention_list(pr.author for pr in self.pulls)

    def to_data(self) -> dict[str, Any]:
        """JSON form stored in ``sync.json`` and returned by ``forge sync``."""
        return {
            "commits": [c.to_data() for c in self.commits],
            "pulls": [p.to_data() for p in self.pulls],
            "issues": self.issues,
        }

    @classmethod
    def from_data(cls, data: Any) -> UpstreamContext:
        """Rebuild from :meth:`to_data` output; anything malformed is dropped."""
        if not isinstance(data, Mapping):
            return cls()
        commits: list[UpstreamCommit] = []
        for item in data.get("commits") or []:
            try:
                commits.append(
                    UpstreamCommit(
                        str(item["sha"]),
                        str(item["subject"]),
                        str(item["author"]),
                        tuple(str(f) for f in item["files"]),
                        tuple(int(n) for n in item["pulls"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        pulls: list[UpstreamPull] = []
        for item in data.get("pulls") or []:
            try:
                pulls.append(
                    UpstreamPull(
                        int(item["number"]),
                        str(item["title"]),
                        str(item["author"]),
                        str(item["url"]),
                        str(item["head"]),
                        tuple(int(n) for n in item["issues"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return cls(tuple(commits), tuple(pulls))


def gather_upstream_context(
    github: GitHubClient,
    upstream: Sequence[CommitFiles],
    relevant: Iterable[str],
    *,
    limit: int = MAX_UPSTREAM_COMMITS,
) -> UpstreamContext:
    """Map up to ``limit`` upstream commits touching ``relevant`` paths to PRs and issues.

    ``upstream`` comes newest first (:meth:`Git.commits_with_files`). Each commit is looked up
    with :meth:`GitHubClient.commit_pulls`; a failed lookup becomes a warning.
    """
    wanted = set(relevant)
    commits: list[UpstreamCommit] = []
    pulls: dict[int, UpstreamPull] = {}
    warnings: list[str] = []
    for item in upstream:
        files = tuple(f for f in item.files if f in wanted)
        if not files:
            continue
        if len(commits) >= limit:
            break
        sha = item.commit.sha
        try:
            found = github.commit_pulls(sha)
        except GitHubError as exc:
            warnings.append(f"could not list the pull requests of {sha[:12]}: {exc.message}")
            found = []
        for pr in found:
            if pr.number not in pulls:
                pulls[pr.number] = UpstreamPull(
                    pr.number, pr.title, pr.author, pr.url, pr.head, tuple(pull_issues(pr))
                )
        commits.append(
            UpstreamCommit(
                sha, item.commit.subject, item.commit.author, files, tuple(p.number for p in found)
            )
        )
    return UpstreamContext(tuple(commits), tuple(pulls.values()), tuple(warnings))


def _describe_context(context: UpstreamContext) -> list[str]:
    by_number = {p.number: p for p in context.pulls}
    lines: list[str] = []
    for commit in context.commits:
        refs = []
        for number in commit.pulls:
            pr = by_number.get(number)
            if pr is None:
                continue
            issues = ", ".join(f"#{n}" for n in pr.issues)
            refs.append(
                f"PR #{pr.number} by {pr.author}" + (f" (issues {issues})" if issues else "")
            )
        where = "; ".join(refs) if refs else "no pull request found"
        lines.append(f"- `{commit.sha[:12]}` {commit.subject} ({where})")
    return lines or ["- (no upstream pull request found)"]


# --------------------------------------------------------------------------- sync exit 4


@dataclass(frozen=True)
class ConflictEscalation:
    """What the exit-4 path of ``forge sync`` wrote on GitHub."""

    issue: int
    label_added: bool
    comment_id: int | None
    comment_created: bool
    upstream_comments: Mapping[int, bool]  # upstream issue -> comment created now
    mentioned: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    upstream_skipped: tuple[int, ...] = ()  # upstream issues of mine: no self-mention

    def to_data(self) -> dict[str, Any]:
        """JSON form returned by ``forge sync``."""
        return {
            "issue": self.issue,
            "label": BLOCKED_CONFLICT,
            "label_added": self.label_added,
            "comment": self.comment_id,
            "comment_created": self.comment_created,
            "upstream_issues": {str(k): v for k, v in self.upstream_comments.items()},
            "upstream_skipped": list(self.upstream_skipped),
            "mentioned": list(self.mentioned),
        }


def escalate_conflict(
    github: GitHubClient,
    *,
    issue_number: int,
    task_id: str,
    me: str,
    files: Sequence[tuple[str, str]],
    context: UpstreamContext,
    key: str,
    default_branch: str,
    saved_ref: str | None = None,
) -> ConflictEscalation:
    """Label the task issue, comment on it and on each linked upstream issue (ADR 0011).

    ``files`` holds ``(path, why)`` for each conflicted path (why: outside the zones, contract).
    ``saved_ref`` is the local ref where the restore saved commits made after the sync
    started (the branch was then reset to its pre-sync state, not left unchanged).
    The task comment mentions the upstream PR authors (never ``me``); each upstream issue's
    comment mentions ``me``, except on the issues that are mine (I authored a linked upstream
    PR or I am an assignee of the issue): those are skipped, since the comment would only
    mention me to myself. Comments already posted for ``key`` are not posted again.
    """
    warnings: list[str] = []
    issue = github.get_issue(issue_number)
    added = ensure_blocked(github, issue_number, issue.labels)
    mentioned = mention_list(context.authors, exclude=[me])
    branch = TaskMetadata.branch_name(issue_number)
    if saved_ref is None:
        restored = "the branch is unchanged"
    else:
        restored = (
            "the branch was reset to its state before the sync; local commits made since then "
            f"are saved on {me}'s machine at `{saved_ref}`"
        )
    lines = [
        ESCALATION_MARKER,
        key_line(key),
        f"**Forge escalation**: syncing `{task_id}` (`{branch}`, {me}) onto `{default_branch}` "
        f"stopped on conflicts that the task must not resolve alone. The rebase was aborted and "
        f"{restored}; the issue is labelled `{BLOCKED_CONFLICT}`.",
        "",
        "Conflicted files:",
        *(f"- `{path}` ({why})" for path, why in files),
        "",
        "Upstream changes:",
        *_describe_context(context),
    ]
    if mentioned:
        lines += [
            "",
            " ".join(f"@{login}" for login in mentioned) + ": please coordinate with "
            f"{me} on these files.",
        ]
    comment, created = post_once(github, issue_number, key, "\n".join(lines) + "\n")
    upstream: dict[int, bool] = {}
    skipped: list[int] = []
    folded = me.casefold()
    for number in context.issues:
        if number == issue_number:
            continue
        if any(number in pr.issues and pr.author.casefold() == folded for pr in context.pulls):
            skipped.append(number)
            continue
        try:
            assignees = github.get_issue(number).assignees
        except GitHubError as exc:
            warnings.append(f"could not read upstream issue #{number}: {exc.message}")
            continue
        if any(login.casefold() == folded for login in assignees):
            skipped.append(number)
            continue
        body = "\n".join(
            [
                ESCALATION_MARKER,
                key_line(f"{key}-{issue_number}"),
                f"**Forge escalation**: @{me}'s task `{task_id}` (#{issue_number}) conflicts with "
                f"upstream changes linked to this issue on: "
                + ", ".join(f"`{path}`" for path, _ in files)
                + f". See #{issue_number}.",
            ]
        )
        try:
            _, made = post_once(github, number, f"{key}-{issue_number}", body + "\n")
        except GitHubError as exc:
            warnings.append(f"could not comment on upstream issue #{number}: {exc.message}")
            continue
        upstream[number] = made
    return ConflictEscalation(
        issue=issue_number,
        label_added=added,
        comment_id=None if comment is None else comment.id,
        comment_created=created,
        upstream_comments=upstream,
        mentioned=tuple(mentioned),
        warnings=tuple(warnings),
        upstream_skipped=tuple(skipped),
    )


# --------------------------------------------------------------------------- forge escalate


@dataclass
class EscalateContext:
    """What ``forge escalate`` needs: ``git`` runs at the task's worktree root."""

    git: Git
    github: GitHubClient
    team: Team | None = None


@dataclass(frozen=True)
class EscalateOutcome:
    """Result of :func:`escalate` (exit 0)."""

    issue: int
    task_id: str
    reason: str
    label_added: bool
    comment_id: int | None
    comment_created: bool
    mentioned: tuple[str, ...]
    source: str | None  # where the upstream authors came from: "sync", "sync-log" or None
    notes: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` for ``forge escalate``."""
        return {
            "issue": self.issue,
            "id": self.task_id,
            "reason": self.reason,
            "label": BLOCKED_CONFLICT,
            "label_added": self.label_added,
            "comment": self.comment_id,
            "comment_created": self.comment_created,
            "mentioned": list(self.mentioned),
            "upstream_source": self.source,
        }


def locate_task(git: Git) -> tuple[Path, int, tuple[str, ...]]:
    """Return ``(worktree root, issue, notes)`` of the current task.

    ``current.json`` or a checked-out ``task/<n>`` branch (:func:`find_current_task`); during
    a sync stopped mid-rebase (detached HEAD, no ``current.json``) ``sync.json`` names it.
    Raises :class:`PreconditionError` (exit 5) when no task is found.
    """
    root = git.root()
    location = find_current_task(git)
    if location is not None:
        return location.workspace, location.issue, location.notes
    state = read_json_file(root / SYNC_FILE)
    if isinstance(state, Mapping) and isinstance(state.get("issue"), int):
        return root, int(state["issue"]), ()
    raise PreconditionError(
        f"no current task in {root}",
        hint="run this inside the worktree of a claimed task (or on its task/<n> branch)",
    )


def recorded_upstream(root: Path) -> tuple[UpstreamContext, str | None]:
    """The upstream context of the current ``sync.json``, else of the last sync-log entry."""
    state = read_json_file(root / SYNC_FILE)
    if isinstance(state, Mapping) and "upstream" in state:
        return UpstreamContext.from_data(state["upstream"]), "sync"
    log = read_json_file(root / SYNC_LOG_FILE)
    if isinstance(log, list) and log and isinstance(log[-1], Mapping):
        return UpstreamContext.from_data(log[-1].get("upstream_context")), "sync-log"
    return UpstreamContext(), None


def escalate(ctx: EscalateContext, reason: str) -> EscalateOutcome:
    """``forge escalate --reason R`` on the current task (ADR 0011). Exit 0 when done.

    Adds ``blocked:conflict``, comments with the reason and :data:`ESCALATION_MARKER`, and
    mentions the upstream PR authors (from ``sync.json``, else the last sync-log entry) plus
    the ``lead`` members of ``.forge/team.yml``, never the current user. It needs a current
    task but not a live claim. Re-running with the same reason right after does not post a
    second comment. Raises :class:`PreconditionError` (exit 5) without a task or a reason.
    """
    text = reason.strip()
    if not text:
        raise PreconditionError(
            "an escalation needs a reason", hint='pass --reason "what is blocked and why"'
        )
    root, issue_number, notes = locate_task(ctx.git)
    record = load_task(ctx.github, issue_number)
    me = ctx.github.current_user()
    context, source = recorded_upstream(root)
    leads = [] if ctx.team is None else [m.github for m in ctx.team.leads()]
    mentioned = mention_list([*context.authors, *leads], exclude=[me])
    added = ensure_blocked(ctx.github, issue_number, record.issue.labels)
    key = digest(str(issue_number), text, *mentioned)
    lines = [
        ESCALATION_MARKER,
        key_line(key),
        f"**Forge escalation** on `{record.meta.id}` by {me}:",
        "",
        *(f"> {line}" for line in text.splitlines()),
    ]
    if context.pulls:
        lines += ["", "Upstream changes:", *_describe_context(context)]
    if mentioned:
        lines += ["", " ".join(f"@{login}" for login in mentioned) + ": please take a look."]
    comment, created = post_once(
        ctx.github, issue_number, key, "\n".join(lines) + "\n", newest_only=True
    )
    return EscalateOutcome(
        issue=issue_number,
        task_id=record.meta.id,
        reason=text,
        label_added=added,
        comment_id=None if comment is None else comment.id,
        comment_created=created,
        mentioned=tuple(mentioned),
        source=source,
        notes=notes,
    )
