"""Leases: the lease comment, last activity, staleness and heartbeat (spec §8.2, ADR 0009).

A task has a **live claim** when its branch exists, its last Forge event (ADR 0008) is a
claim and the claim is not stale. The **claim commit** carries that last ``Forge-Claim``
event. **Last activity** is the later of the branch tip's committer date and the lease
comment's ``heartbeat_at``, the latter only when the lease comment names the same claim
(:func:`lease_names_claim`). A claim is **stale** when its last activity is more than
``lease_hours`` old.

A lease comment **names** a claim when its ``claim`` sha is the claim commit's sha or, when
both carry one, its ``run`` equals the claim commit's ``Forge-Run`` trailer (ADR 0011): a
claim commit rewritten by ``forge sync``'s rebase keeps its run, so it stays complete and its
heartbeats keep counting before the lease comment is rewritten. Lease comments written before
ADR 0011 have no ``run`` and match by sha only.

The lease comment is one comment per issue::

    <!-- forge:lease -->
    ...human summary...
    <!-- forge:lease-data {"task": ..., "owner": ..., ...} -->

When several comments carry the marker the newest one is authoritative and is edited in
place; if the edit fails, a new comment is created and becomes the newest.

All timestamps are UTC ISO 8601 with a ``Z`` suffix. The clock is always injected.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from forge.core.config import ForgeConfig
from forge.core.errors import ForgeError, PreconditionError
from forge.core.git import CommitInfo, Git
from forge.core.github import Comment, GitHubClient, GitHubError
from forge.core.profile import AgentProfile
from forge.core.refresh import (
    TaskBranch,
    TaskRecord,
    check_full_history,
    fetch_task_refs,
    gather_task_branches,
)
from forge.core.tasks import (
    TRAILER_RUN,
    BranchEvent,
    TaskBodyError,
    TaskMetadata,
    last_event_from_trailers,
    parse_body,
)

LEASE_MARKER = "<!-- forge:lease -->"
"""First line of every lease comment."""

_DATA_PREFIX = "<!-- forge:lease-data "
_DATA_RE = re.compile(r"^<!-- forge:lease-data (\{.*\}) -->[ \t]*$", re.MULTILINE)
_DATA_KEYS = ("task", "owner", "agent", "claim", "claimed_at", "heartbeat_at", "released_at")
_RUN_KEY = "run"
"""Optional key (ADR 0011): the claim commit's ``Forge-Run`` trailer. Written only when set,
so lease comments of claims without a run stay readable by earlier Forge versions."""


# --------------------------------------------------------------------------- timestamps


def format_time(value: datetime) -> str:
    """Return ``value`` as UTC ISO 8601 with a ``Z`` suffix, to the second."""
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime:
    """Parse a UTC ISO 8601 timestamp (``Z`` or an explicit offset). Raises ``ValueError``."""
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError(f"timestamp without a timezone: {text!r}")
    return value.astimezone(UTC)


# --------------------------------------------------------------------------- lease comment


@dataclass(frozen=True)
class LeaseData:
    """The machine data of a lease comment (ADR 0009)."""

    task: str
    owner: str
    agent: str | None
    claim: str
    claimed_at: datetime
    heartbeat_at: datetime
    released_at: datetime | None = None
    run: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return the JSON object stored on the ``forge:lease-data`` line.

        ``run`` is present only when set (ADR 0011).
        """
        data: dict[str, Any] = {
            "task": self.task,
            "owner": self.owner,
            "agent": self.agent,
            "claim": self.claim,
            "claimed_at": format_time(self.claimed_at),
            "heartbeat_at": format_time(self.heartbeat_at),
            "released_at": None if self.released_at is None else format_time(self.released_at),
        }
        if self.run is not None:
            data[_RUN_KEY] = self.run
        return data


@dataclass(frozen=True)
class FoundLease:
    """The authoritative (newest) lease comment and its data (``None`` if unreadable)."""

    comment: Comment
    data: LeaseData | None


def render_lease(data: LeaseData, *, branch: str) -> str:
    """Render a lease comment body: marker, human summary, final machine-data line."""
    payload = json.dumps(data.to_json(), separators=(",", ":"))
    # ``>`` never occurs in logins, ids or shas; escaping it keeps ``-->`` out of the line.
    payload = payload.replace(">", "\\u003e")
    lines = [
        LEASE_MARKER,
        f"**Forge lease** for `{data.task}` on `{branch}`",
        "",
        f"- Owner: {data.owner}",
        f"- Agent: {data.agent or 'unknown'}",
        f"- Claimed: {format_time(data.claimed_at)} (claim commit `{data.claim[:12]}`)",
        f"- Last heartbeat: {format_time(data.heartbeat_at)}",
    ]
    if data.released_at is not None:
        lines.append(f"- Released: {format_time(data.released_at)}")
    lines += ["", f"{_DATA_PREFIX}{payload} -->"]
    return "\n".join(lines) + "\n"


def is_lease_comment(body: str) -> bool:
    """True when ``body`` starts with the lease marker."""
    return body.lstrip().startswith(LEASE_MARKER)


def parse_lease(body: str) -> LeaseData | None:
    """Return the data of a lease comment, or ``None`` when it is not one or is unreadable.

    The last ``forge:lease-data`` line counts. Missing keys, wrong types or bad timestamps
    make the data unreadable (``None``); the comment still counts as a lease comment. The
    ``run`` key is optional (ADR 0011); when present it must be a non-empty string.
    """
    if not is_lease_comment(body):
        return None
    matches = _DATA_RE.findall(body)
    if not matches:
        return None
    try:
        raw = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or set(raw) - {_RUN_KEY} != set(_DATA_KEYS):
        return None
    try:
        task, owner, claim = raw["task"], raw["owner"], raw["claim"]
        agent, released = raw["agent"], raw["released_at"]
        run = raw.get(_RUN_KEY)
        if not all(isinstance(v, str) and v for v in (task, owner, claim)):
            return None
        if _RUN_KEY in raw and not (isinstance(run, str) and run):
            return None
        if agent is not None and not isinstance(agent, str):
            return None
        if released is not None and not isinstance(released, str):
            return None
        return LeaseData(
            task=task,
            owner=owner,
            agent=agent,
            claim=claim,
            claimed_at=parse_time(str(raw["claimed_at"])),
            heartbeat_at=parse_time(str(raw["heartbeat_at"])),
            released_at=None if released is None else parse_time(released),
            run=run,
        )
    except (TypeError, ValueError):
        return None


def find_lease(comments: Sequence[Comment]) -> FoundLease | None:
    """Return the newest comment carrying the lease marker (comments come oldest first)."""
    for comment in reversed(comments):
        if is_lease_comment(comment.body):
            return FoundLease(comment, parse_lease(comment.body))
    return None


def upsert_lease(
    github: GitHubClient,
    issue_number: int,
    data: LeaseData,
    *,
    comments: Sequence[Comment] | None = None,
) -> Comment:
    """Write ``data`` into the issue's lease comment.

    The newest lease comment is edited in place. If there is none, or the edit fails with
    :class:`GitHubError` (e.g. a 403 on another user's comment), a new comment is created; it
    is then the newest and therefore authoritative. ``comments`` avoids a second listing when
    the caller already has them.
    """
    if comments is None:
        comments = github.list_comments(issue_number)
    body = render_lease(data, branch=TaskMetadata.branch_name(issue_number))
    found = find_lease(comments)
    if found is not None:
        if found.comment.body == body:
            return found.comment
        try:
            return github.update_comment(found.comment.id, body)
        except GitHubError:
            pass
    return github.create_comment(issue_number, body)


# --------------------------------------------------------------------------- activity


def lease_names_claim(lease: LeaseData | None, claim_sha: str, claim_run: str | None) -> bool:
    """True when ``lease`` names the claim ``claim_sha`` whose ``Forge-Run`` is ``claim_run``.

    ADR 0011 matching rule: ``lease.claim == claim_sha``, or both runs are set and equal (a
    claim commit rewritten by a rebase keeps its run). A lease without ``run`` (written
    before ADR 0011) matches by sha only.
    """
    if lease is None:
        return False
    if lease.claim == claim_sha:
        return True
    return lease.run is not None and claim_run is not None and lease.run == claim_run


def last_activity(
    tip_date: datetime,
    lease: LeaseData | None,
    claim_sha: str,
    claim_run: str | None = None,
) -> datetime:
    """Return the later of ``tip_date`` and the lease heartbeat for the same claim.

    A heartbeat left over from another claim (one the lease does not name, see
    :func:`lease_names_claim`) never counts.
    """
    if (
        lease is not None
        and lease_names_claim(lease, claim_sha, claim_run)
        and lease.heartbeat_at > tip_date
    ):
        return lease.heartbeat_at
    return tip_date


def stale_at(activity: datetime, lease_hours: int) -> datetime:
    """Return when a claim whose last activity is ``activity`` becomes stale."""
    return activity + timedelta(hours=lease_hours)


def is_stale(activity: datetime, lease_hours: int, now: datetime) -> bool:
    """True when ``activity`` is more than ``lease_hours`` before ``now``.

    A timestamp in the future (clock skew between machines) is simply not stale.
    """
    return now > stale_at(activity, lease_hours)


# --------------------------------------------------------------------------- claims on branches


@dataclass(frozen=True)
class ClaimInfo:
    """The claim carried by a task branch whose last Forge event is ``Forge-Claim``."""

    issue: int
    task_id: str
    tip: str
    tip_date: datetime
    sha: str
    date: datetime
    owner: str
    agent: str | None
    index: int  # position of the claim commit in the branch's first-parent commits
    run: str | None = None  # the claim commit's Forge-Run trailer (ADR 0011)

    @property
    def order_key(self) -> tuple[datetime, int]:
        """Order between two claims: claim commit committer date, then issue number."""
        return (self.date, self.issue)

    def is_mine(self, login: str, agent_id: str) -> bool:
        """True when the claim names ``login`` (case-insensitively) and ``agent_id``."""
        return self.owner.casefold() == login.casefold() and self.agent == agent_id

    def named_by(self, lease: LeaseData | None) -> bool:
        """True when ``lease`` names this claim (:func:`lease_names_claim`)."""
        return lease_names_claim(lease, self.sha, self.run)


def run_of(trailers: Mapping[str, str]) -> str | None:
    """The ``Forge-Run`` trailer among ``trailers`` (key case-insensitive), or ``None``."""
    wanted = TRAILER_RUN.lower()
    for key, value in trailers.items():
        if key.strip().lower() == wanted and value.strip():
            return value.strip()
    return None


def event_commit_index(commits: Sequence[CommitInfo], task_id: str) -> int | None:
    """Index of the newest commit carrying a Forge event for ``task_id`` (or ``None``)."""
    for index, commit in enumerate(commits):
        if last_event_from_trailers([commit.trailers], task_id=task_id) is not None:
            return index
    return None


def find_claim(issue_number: int, branch: TaskBranch, task_id: str) -> ClaimInfo | None:
    """Return the claim of ``branch`` when its last Forge event for ``task_id`` is a claim.

    ``branch.commits`` must be populated (:func:`gather_task_branches` does it).
    """
    index = event_commit_index(branch.commits, task_id)
    if index is None:
        return None
    commit = branch.commits[index]
    event = last_event_from_trailers([commit.trailers], task_id=task_id)
    if event is None or event.event is not BranchEvent.CLAIM:
        return None
    return ClaimInfo(
        issue=issue_number,
        task_id=task_id,
        tip=branch.tip,
        tip_date=branch.commits[0].date,
        sha=commit.sha,
        date=commit.date,
        owner=event.author,
        agent=event.agent,
        index=index,
        run=run_of(commit.trailers),
    )


# --------------------------------------------------------------------------- context and checks


class LeaseContext(Protocol):
    """What lease operations need; ``forge.core.claim.ClaimContext`` satisfies it."""

    @property
    def config(self) -> ForgeConfig:
        """The project configuration."""
        ...

    @property
    def git(self) -> Git:
        """Git access to the repository."""
        ...

    @property
    def github(self) -> GitHubClient:
        """GitHub access to ``config.github.repo``."""
        ...

    @property
    def agent(self) -> AgentProfile:
        """The active agent (``--agent`` or ``default_agent``)."""
        ...

    @property
    def profile_login(self) -> str:
        """The ``github`` field of the machine profile."""
        ...

    @property
    def remote(self) -> str:
        """The git remote holding the task branches (``origin``)."""
        ...

    def now(self) -> datetime:
        """Current time from the injected clock (timezone-aware)."""
        ...


def check_identity(github: GitHubClient, profile_login: str) -> str:
    """Return the authenticated GitHub login; exit 5 if it differs from the profile's."""
    login = github.current_user()
    if login.casefold() != profile_login.casefold():
        raise PreconditionError(
            f"the GitHub login {login!r} (gh) differs from the profile's github {profile_login!r}",
            hint=f"run `forge profile set github={login}` or `gh auth switch` to the "
            f"{profile_login!r} account",
        )
    return login


def load_task(github: GitHubClient, issue_number: int) -> TaskRecord:
    """Return the issue and its parsed metadata; :class:`ForgeError` if it is not a task."""
    issue = github.get_issue(issue_number)
    try:
        meta = parse_body(issue.body, issue_number=issue.number, milestone=issue.milestone)
    except TaskBodyError as exc:
        raise ForgeError(exc.message, hint=exc.hint) from exc
    if meta is None:
        raise ForgeError(
            f"issue #{issue_number} is not a Forge task (no Forge block)",
            hint="pass the issue number of a Forge task",
        )
    return TaskRecord(issue, meta)


def read_branch(ctx: LeaseContext, issue_number: int) -> TaskBranch | None:
    """Fetch the task branches and return ``task/<issue_number>`` (``None`` if absent)."""
    default = ctx.config.project.default_branch
    fetch_task_refs(ctx.git, default, ctx.remote)
    return gather_task_branches(ctx.git, default, ctx.remote).branches.get(issue_number)


# --------------------------------------------------------------------------- heartbeat


@dataclass(frozen=True)
class OwnClaim:
    """A claim of the current user on one task, with its lease comment.

    ``stale_since`` is when the claim went stale, or ``None`` while it is live.
    """

    record: TaskRecord
    claim: ClaimInfo
    lease: LeaseData | None
    comments: tuple[Comment, ...]
    login: str
    stale_since: datetime | None = None


def require_own_claim(ctx: LeaseContext, issue_number: int) -> OwnClaim:
    """Check that the last Forge event of ``task/<issue_number>`` is a claim of mine; return it.

    My own **stale** claim is returned too, with :attr:`OwnClaim.stale_since` set; the
    caller decides (``heartbeat`` refuses it, ADR 0009 implementation notes).

    Raises :class:`PreconditionError` (exit 5) when the login differs from the profile, the
    clone is shallow, or the branch's last event is not a claim made by my login and agent
    (someone else's claim is refused whether it is fresh or stale).
    """
    login = check_identity(ctx.github, ctx.profile_login)
    check_full_history(ctx.git, "claims")
    record = load_task(ctx.github, issue_number)
    branch = read_branch(ctx, issue_number)
    name = TaskMetadata.branch_name(issue_number)
    claim = None if branch is None else find_claim(issue_number, branch, record.meta.id)
    if claim is None:
        raise PreconditionError(
            f"#{issue_number} ({record.meta.id}) has no claim: {name} "
            + ("does not exist" if branch is None else "does not end with a claim"),
            hint=f"claim it first with `forge next --task {record.meta.id}`",
        )
    if not claim.is_mine(login, ctx.agent.id):
        raise PreconditionError(
            f"the claim on #{issue_number} ({record.meta.id}) belongs to {claim.owner} "
            f"with agent {claim.agent or 'unknown'}, not to {login} with agent {ctx.agent.id}",
            hint="use the agent that made the claim (--agent), or claim another task",
        )
    comments = tuple(ctx.github.list_comments(issue_number))
    found = find_lease(comments)
    lease = None if found is None else found.data
    activity = last_activity(claim.tip_date, lease, claim.sha, claim.run)
    stale_since = None
    if is_stale(activity, ctx.config.lease_hours, ctx.now()):
        stale_since = stale_at(activity, ctx.config.lease_hours)
    return OwnClaim(record, claim, lease, comments, login, stale_since)


@dataclass(frozen=True)
class HeartbeatOutcome:
    """Result of :func:`heartbeat`."""

    issue: int
    task_id: str
    claim: str
    heartbeat_at: datetime
    stale_at: datetime
    warnings: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` for ``forge heartbeat``."""
        return {
            "issue": self.issue,
            "id": self.task_id,
            "claim": self.claim,
            "heartbeat_at": format_time(self.heartbeat_at),
            "stale_at": format_time(self.stale_at),
            "warnings": list(self.warnings),
        }


def heartbeat(ctx: LeaseContext, issue_number: int) -> HeartbeatOutcome:
    """Refresh the lease of my live, complete claim on ``issue_number`` (sets ``heartbeat_at``).

    Raises :class:`PreconditionError` (exit 5), changing nothing, when (ADR 0009):

    - the branch's last event is not a claim of mine;
    - my claim is **stale**: while stale it held no zones, so it is renewed only through the
      CAS push and verification of ``forge reclaim <n>``;
    - the claim is **incomplete** (the lease comment does not name it, by sha or run): a
      heartbeat would mark it complete without verification; ``forge next --task <n>``
      completes it;
    - the remote branch moved since it was read (re-run to retry).
    """
    own = require_own_claim(ctx, issue_number)
    claim = own.claim
    lease = own.lease
    task_id = own.record.meta.id
    if own.stale_since is not None:
        raise PreconditionError(
            f"your claim on #{issue_number} ({task_id}) went stale at "
            f"{format_time(own.stale_since)}; a stale claim holds no zones, so a heartbeat "
            "cannot renew it",
            hint=f"renew it through a verified claim: `forge reclaim {issue_number}`",
        )
    if lease is None or not claim.named_by(lease):
        raise PreconditionError(
            f"your claim on #{issue_number} ({task_id}) is incomplete: the lease comment "
            f"does not name its claim commit {claim.sha[:12]}",
            hint=f"complete it with `forge next --task {issue_number}`",
        )
    name = TaskMetadata.branch_name(issue_number)
    current = ctx.git.remote_branch_sha(name, ctx.remote)
    if current != claim.tip:
        raise PreconditionError(
            f"{name} moved on {ctx.remote} while renewing the lease "
            f"({claim.tip[:12]} -> {current[:12] if current else 'deleted'}); nothing was "
            "changed",
            hint="re-run `forge heartbeat`",
        )
    now = ctx.now()
    data = LeaseData(
        task=task_id,
        owner=own.login,
        agent=ctx.agent.id,
        claim=claim.sha,
        claimed_at=lease.claimed_at,
        heartbeat_at=now,
        run=claim.run,
    )
    upsert_lease(ctx.github, issue_number, data, comments=own.comments)
    activity = last_activity(claim.tip_date, data, claim.sha, claim.run)
    return HeartbeatOutcome(
        issue=issue_number,
        task_id=task_id,
        claim=claim.sha,
        heartbeat_at=now,
        stale_at=stale_at(activity, ctx.config.lease_hours),
    )


# --------------------------------------------------------------------------- summary


def lease_summary(
    lease: LeaseData | None,
    claim: ClaimInfo | None,
    *,
    lease_hours: int,
    now: datetime,
) -> dict[str, Any] | None:
    """Summarize the lease of one task for the task context (``None``: never claimed).

    ``claim`` is the branch's claim when its last Forge event is a claim, else ``None``.
    ``state`` is ``live`` or ``stale`` for a claim, otherwise ``released`` (the lease comment
    says so) or ``ended`` (the branch moved on, e.g. a handoff, or is gone). The lease
    comment's timestamps count only when it names the branch's claim
    (:func:`lease_names_claim`).
    """
    if claim is None:
        if lease is None:
            return None
        return {
            "state": "released" if lease.released_at is not None else "ended",
            "owner": lease.owner,
            "agent": lease.agent,
            "claim": lease.claim,
            "claimed_at": format_time(lease.claimed_at),
            "heartbeat_at": format_time(lease.heartbeat_at),
            "released_at": None if lease.released_at is None else format_time(lease.released_at),
            "last_activity": None,
            "stale_at": None,
        }
    same = claim.named_by(lease)
    activity = last_activity(claim.tip_date, lease, claim.sha, claim.run)
    return {
        "state": "stale" if is_stale(activity, lease_hours, now) else "live",
        "owner": claim.owner,
        "agent": claim.agent,
        "claim": claim.sha,
        "claimed_at": format_time(lease.claimed_at if same and lease else claim.date),
        "heartbeat_at": format_time(lease.heartbeat_at) if same and lease else None,
        "released_at": None,
        "last_activity": format_time(activity),
        "stale_at": format_time(stale_at(activity, lease_hours)),
    }


def describe_lease(
    git: Git,
    config: ForgeConfig,
    issue_number: int,
    task_id: str,
    comments: Sequence[Comment],
    *,
    now: datetime,
    remote: str = "origin",
) -> dict[str, Any] | None:
    """Fetch the task branches and return :func:`lease_summary` for ``issue_number``.

    Read-only on the remote: it fetches, never pushes.
    """
    default = config.project.default_branch
    fetch_task_refs(git, default, remote)
    branch = gather_task_branches(git, default, remote).branches.get(issue_number)
    claim = None if branch is None else find_claim(issue_number, branch, task_id)
    found = find_lease(comments)
    return lease_summary(
        None if found is None else found.data, claim, lease_hours=config.lease_hours, now=now
    )
