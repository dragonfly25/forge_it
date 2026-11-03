"""``forge handoff``: publish the current task's work and hand it over (spec §8.3, ADR 0012).

Flow of :func:`handoff`:

1. **preconditions** (nothing changes on failure): a non-empty note whose comment fits in a
   GitHub comment, the gh login equal to the profile's, a full clone, and the branch's last
   Forge event is **my claim** (login and agent), live or stale. The claim must be complete
   (the lease comment names it, ADR 0011 matching rule). The task has **no open pull
   request**: a handed-off task in review would never be resumed by ``forge next``. For the
   publishing step, no sync, rebase, merge, cherry-pick or revert is in progress, ``task/<n>``
   is checked out and contains ``origin/task/<n>``;
2. **branch**: a dirty tree is committed as ``forge: wip`` (commit hooks run; a rejection is
   exit 2 and the changes are unstaged again). An empty event commit (``commit-tree``, the
   tree of HEAD) with the trailers ``Forge-Handoff``, ``Forge-Agent``, ``Forge-Task`` and
   ``Forge-Run`` goes on top;
3. **push**: one push of the event commit (and so of every local commit below it) with
   ``--force-with-lease=refs/heads/task/<n>:<fetched tip>``. The user's ``pre-push`` hook
   runs: the push carries user code. A hook rejection is exit 2 and a lease rejection exit 5;
   these and any other push failure (re-raised) undo the wip commit, so the tree is as it
   was. After the push the local branch moves to the event commit (same tree, so the working
   tree stays clean) with a compare-and-swap on the HEAD that was pushed: a commit made here
   while the pre-push hook ran is kept and reported as unpublished (exit 2);
4. **GitHub**: the note under ``<!-- forge:handoff -->`` with a header (author, agent, time,
   branch, event commit sha), unassign me, set the derived status (``handoff``) with the
   label-diff helper, then set ``released_at`` in the lease comment, **last**, as the
   completion marker. Labels and lease are written only while ``task/<n>`` still ends with my
   handoff: once someone resumed the task they belong to the new claim.

**Re-run after an interruption**: when the branch's last event is already my handoff
(login and agent) and the claim below it is the one the lease comment names, the git steps
are skipped and the GitHub steps are completed. Every one of them is idempotent: the note is
keyed by the event commit sha, unassigning and label diffs converge, and ``released_at`` is
kept once set. So a re-run never pushes a second event commit and never posts the note twice.
A re-run makes the same in-progress refusals as the first run (sync, rebase, merge,
cherry-pick, revert: exit 5). When the clone still holds local commits or changes that the
pushed handoff does not contain (the losing clone of a race, or work done after an interrupted
run), or ``task/<n>`` is not checked out so its working tree cannot be compared, the re-run
completes the GitHub steps but returns exit 2 (``unpublished_work``) listing them, so
``current.json`` is kept and nothing is silently left behind.

Core never prints: the caller renders :class:`HandoffOutcome` and clears ``current.json``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from forge.core.claim import ClaimContext
from forge.core.context import HANDOFF_MARKER
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.escalation import SYNC_FILE
from forge.core.git import CommitInfo, Git, GitAddFailed, GitError, Lease, PushHookRejected
from forge.core.github import Comment, GitHubClient
from forge.core.lease import (
    LeaseData,
    check_identity,
    event_commit_index,
    find_claim,
    find_lease,
    format_time,
    is_stale,
    last_activity,
    lease_names_claim,
    load_task,
    read_branch,
    run_of,
    stale_at,
    upsert_lease,
)
from forge.core.pending import remove_pending, require_flock
from forge.core.refresh import TaskBranch, TaskRecord, check_full_history, ensure_labels
from forge.core.tasks import (
    TRAILER_AGENT,
    TRAILER_HANDOFF,
    TRAILER_RUN,
    TRAILER_TASK,
    BranchEvent,
    BranchState,
    Status,
    TaskMetadata,
    derive_status,
    desired_labels,
    last_event_from_trailers,
)

WIP_MESSAGE = "forge: wip"
"""Subject of the commit that saves a dirty tree (the same as ``forge sync``)."""

REQUIRED_HEADINGS: tuple[str, ...] = (
    "Done",
    "Remaining",
    "Decisions",
    "Gotchas",
    "Next step",
    "How to verify",
)
"""The §8.3 sections a handoff note should have; a missing one is a warning (ADR 0012)."""

_HEADING_ALIASES: dict[str, tuple[str, ...]] = {
    "how to verify": ("how to verify", "how to run/verify", "how to run / verify"),
}

MAX_COMMENT_CHARS = 65536
"""GitHub's limit on the body of an issue comment."""

MAX_OUTPUT_CHARS = 4000
"""Tail of a hook's output kept in the outcome."""

_ATX_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t#]*$")
_BOLD_RE = re.compile(r"^[ \t]*(?:\*\*|__)(.+?)(?:\*\*|__)[ \t]*:?[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


# --------------------------------------------------------------------------- the note


def _normalize(heading: str) -> str:
    text = heading.strip().strip("*_").strip().rstrip(":").strip().casefold()
    return re.sub(r"\s+", " ", text)


def note_headings(note: str) -> list[str]:
    """The headings of ``note``, normalized: ATX headings and lines that are only bold text.

    Lines inside fenced code blocks are ignored.
    """
    found: list[str] = []
    fence: str | None = None
    for line in note.splitlines():
        opening = _FENCE_RE.match(line)
        if fence is not None:
            closing = opening is not None and opening.group(1)[0] == fence[0]
            if closing and opening is not None and len(opening.group(1)) >= len(fence):
                fence = None
            continue
        if opening is not None:
            fence = opening.group(1)
            continue
        match = _ATX_RE.match(line) or _BOLD_RE.match(line)
        if match is not None:
            found.append(_normalize(match.group(1)))
    return found


def missing_headings(note: str) -> list[str]:
    """The :data:`REQUIRED_HEADINGS` that ``note`` lacks (compared case-insensitively).

    A heading counts when it equals the required name or starts with it followed by a space
    or a colon (``## Decisions taken`` counts for *Decisions*).
    """
    headings = note_headings(note)
    missing: list[str] = []
    for required in REQUIRED_HEADINGS:
        names = _HEADING_ALIASES.get(required.casefold(), (required.casefold(),))
        if not any(h == n or h.startswith(n + " ") for h in headings for n in names):
            missing.append(required)
    return missing


def render_handoff_comment(
    note: str, *, login: str, agent: str, at: datetime, branch: str, tip: str
) -> str:
    """The handoff comment: the marker, a header, then the note as the agent wrote it.

    ``tip`` is the handoff event commit (the branch tip right after the push); its full sha
    also keys the comment, so a re-run never posts it twice.
    """
    return (
        f"{HANDOFF_MARKER}\n"
        f"**Forge handoff** by {login} (agent {agent}) at {format_time(at)}\n"
        f"Branch `{branch}`, tip `{tip}`\n\n"
        f"{note.strip()}\n"
    )


def _comment_for(comments: Sequence[Comment], tip: str) -> Comment | None:
    """The handoff comment already posted for event commit ``tip``, if any."""
    key = f"tip `{tip}`"
    for comment in comments:
        if HANDOFF_MARKER in comment.body and key in comment.body:
            return comment
    return None


def check_note(note: str) -> str:
    """Return ``note`` stripped; :class:`ForgeError` (exit 1) when it is empty."""
    text = note.strip()
    if not text:
        raise ForgeError(
            "the handoff note is empty",
            hint="write the note (Done, Remaining, Decisions, Gotchas, Next step, How to "
            "verify) to a file and pass it with --note-file",
        )
    return text


# --------------------------------------------------------------------------- outcome


@dataclass(frozen=True)
class HandoffOutcome:
    """Result of :func:`handoff`.

    ``outcome`` is ``handed_off`` (exit 0), ``wip_rejected`` or ``push_rejected`` (a commit
    or pre-push hook refused, exit 2), ``unpublished_work`` (exit 2: the handoff is complete,
    but local commits, changes or an unchecked working tree listed in ``unpublished`` are not
    part of it; e.g. a re-run in another clone, or a commit made while the push ran) or
    ``lease_rejected`` (``task/<n>`` moved on the remote, exit 5). ``commits`` counts the
    commits the push published besides the event commit (the wip commit included).
    ``recovered`` means a previous run had already pushed the handoff and this run completed
    the GitHub steps.
    """

    code: ExitCode
    outcome: str
    issue: int
    task_id: str
    branch: str
    claim: str | None
    handoff_commit: str | None = None
    wip_commit: str | None = None
    commits: int = 0
    status: Status | None = None
    comment_id: int | None = None
    comment_created: bool = False
    recovered: bool = False
    missing_headings: tuple[str, ...] = ()
    unpublished: tuple[str, ...] = ()
    output: str | None = None
    error: str | None = None
    hint: str | None = None
    warnings: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` for ``forge handoff``."""
        return {
            "outcome": self.outcome,
            "issue": self.issue,
            "id": self.task_id,
            "branch": self.branch,
            "claim": self.claim,
            "handoff_commit": self.handoff_commit,
            "wip_commit": self.wip_commit,
            "commits": self.commits,
            "status": None if self.status is None else self.status.value,
            "comment": self.comment_id,
            "comment_created": self.comment_created,
            "recovered": self.recovered,
            "missing_headings": list(self.missing_headings),
            "unpublished": list(self.unpublished),
            "output": self.output,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- helpers


def _tail(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT_CHARS else text[-MAX_OUTPUT_CHARS:]


def _dedupe(items: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _head(git: Git) -> str:
    sha = git.rev_parse("HEAD")
    if sha is None:
        raise PreconditionError("HEAD does not name a commit", hint="check out task/<n>")
    return sha


def _apply_labels(
    github: GitHubClient, issue_number: int, add: Sequence[str], remove: Sequence[str]
) -> None:
    if add:
        ensure_labels(github)
        github.add_labels(issue_number, sorted(add))
    for name in sorted(remove):
        github.remove_label(issue_number, name)


def _undo_wip(git: Git, wip: str | None, original: str) -> None:
    """Undo the wip commit (its changes come back uncommitted) when HEAD is still on it."""
    if wip is not None and git.rev_parse("HEAD") == wip:
        git.reset(original, "mixed")


@dataclass(frozen=True)
class _Handed:
    """A handoff already on the branch: the event commit and the claim it ended."""

    commit: CommitInfo
    claim_sha: str
    claim_run: str | None


def _previous_handoff(
    login: str, agent: str, task_id: str, branch: TaskBranch | None, lease: LeaseData | None
) -> _Handed | None:
    """My handoff, when it is the branch's last event and ends the claim the lease names."""
    if branch is None or lease is None:
        return None
    index = event_commit_index(branch.commits, task_id)
    if index is None:
        return None
    commit = branch.commits[index]
    event = last_event_from_trailers([commit.trailers], task_id=task_id)
    if (
        event is None
        or event.event is not BranchEvent.HANDOFF
        or event.author.casefold() != login.casefold()
        or event.agent != agent
    ):
        return None
    older = branch.commits[index + 1 :]
    below = event_commit_index(older, task_id)
    if below is None:
        return None
    claim_commit = older[below]
    claim = last_event_from_trailers([claim_commit.trailers], task_id=task_id)
    if (
        claim is None
        or claim.event is not BranchEvent.CLAIM
        or claim.author.casefold() != login.casefold()
        or claim.agent != agent
    ):
        return None
    run = run_of(claim_commit.trailers)
    if not lease_names_claim(lease, claim_commit.sha, run):
        return None
    return _Handed(commit, claim_commit.sha, run)


def _not_mine(
    record: TaskRecord, branch: TaskBranch | None, login: str, agent: str
) -> PreconditionError:
    number = record.issue.number
    task_id = record.meta.id
    name = TaskMetadata.branch_name(number)
    if branch is None:
        why = f"{name} does not exist"
    else:
        event = last_event_from_trailers(branch.trailers, task_id=task_id)
        if event is None:
            why = f"{name} has no Forge event"
        elif event.event is BranchEvent.CLAIM:
            why = (
                f"the claim belongs to {event.author} with agent {event.agent or 'unknown'}, "
                f"not to {login} with agent {agent}"
            )
        else:
            who = event.author + ("" if event.agent is None else f" (agent {event.agent})")
            why = f"{name} ends with a {event.event.value} by {who}"
    return PreconditionError(
        f"you hold no claim on #{number} ({task_id}) to hand off: {why}",
        hint="hand off only a task you claimed with this agent (see forge status --me)",
    )


# --------------------------------------------------------------------------- git steps


def _refuse_in_progress(git: Git) -> None:
    """Exit 5 when a ``forge sync``, rebase, merge, cherry-pick or revert is in progress in
    this working tree: the wip commit would conclude it, and a re-run could not tell which
    local work belongs to the handoff."""
    root = git.root()
    if (root / SYNC_FILE).exists():
        raise PreconditionError(
            f"a sync is in progress ({root / SYNC_FILE})",
            hint="finish it with `forge sync --continue` or undo it with `forge sync --abort`, "
            "then run `forge handoff`",
        )
    if git.rebase_in_progress():
        raise PreconditionError(
            "a rebase is in progress",
            hint="finish it (`git rebase --continue`) or abort it (`git rebase --abort`), then "
            "run `forge handoff`",
        )
    operation = git.operation_in_progress()
    if operation is not None:
        # The `forge: wip` commit would otherwise conclude it (e.g. record a merge commit).
        raise PreconditionError(
            f"a {operation} is in progress",
            hint=f"finish it (`git {operation} --continue`) or abort it (`git {operation} "
            "--abort`), then run `forge handoff`",
        )


@dataclass(frozen=True)
class _Published:
    handoff: str
    wip: str | None
    commits: int
    unpublished: tuple[str, ...] = ()


def _publish(
    ctx: ClaimContext,
    record: TaskRecord,
    login: str,
    remote_tip: str,
    now: datetime,
    warnings: list[str],
) -> _Published | HandoffOutcome:
    """Wip commit, event commit and the one push. Returns an outcome on a rejection."""
    git = ctx.git
    number = record.issue.number
    task_id = record.meta.id
    name = TaskMetadata.branch_name(number)
    ref = f"refs/heads/{name}"
    root = git.root()
    _refuse_in_progress(git)
    if git.current_branch() != name:
        raise PreconditionError(
            f"{name} is not checked out in {root}",
            hint=f"run `forge handoff` in the task's worktree, or `git switch {name}` first",
        )
    original = _head(git)
    if not git.is_ancestor(remote_tip, original):
        raise PreconditionError(
            f"the local {name} does not contain {ctx.remote}/{name} ({remote_tip[:12]}); the "
            "handoff push would drop those commits",
            hint=f"bring them in first with `git rebase {ctx.remote}/{name}` (or `git pull "
            f"--rebase`), then run `forge handoff`; do not reset to {ctx.remote}/{name}, that "
            "would discard your local commits",
        )

    def rejected(
        code: ExitCode, outcome: str, error: str, hint: str, **extra: Any
    ) -> HandoffOutcome:
        return HandoffOutcome(
            code=code,
            outcome=outcome,
            issue=number,
            task_id=task_id,
            branch=name,
            claim=None,
            error=error,
            hint=hint,
            **extra,
        )

    wip: str | None = None
    if git.is_dirty(include_untracked=True):
        try:
            wip = git.commit_all(WIP_MESSAGE)
        except GitAddFailed as exc:
            git.reset("HEAD", "mixed")
            raise ForgeError(
                f"`git add -A` failed while preparing the `{WIP_MESSAGE}` commit; nothing was "
                "changed: " + "; ".join(_tail(exc.stderr).strip().splitlines()),
                hint="fix what git reports (e.g. an unreadable file), or commit your changes "
                "yourself, then run `forge handoff`",
            ) from exc
        except GitError as exc:
            git.reset("HEAD", "mixed")
            return rejected(
                ExitCode.AGENT_ACTION,
                "wip_rejected",
                f"a commit hook rejected the `{WIP_MESSAGE}` commit; nothing was changed",
                "fix what the hook reports (or commit your changes yourself), then run "
                "`forge handoff` again",
                output=_tail(exc.stderr),
            )
    try:
        head = _head(git)
        commits = len(git.commits(f"{remote_tip}..{head}"))
        event = git.commit_tree(
            git.tree_of(head),
            [head],
            f"forge: handoff {task_id}",
            {
                TRAILER_HANDOFF: login,
                TRAILER_AGENT: ctx.agent.id,
                TRAILER_TASK: task_id,
                TRAILER_RUN: ctx.run_id(),
            },
            date=now,
        )
        result = git.push(event, ref, lease=Lease(ref, remote_tip), remote=ctx.remote)
    except PushHookRejected as exc:
        _undo_wip(git, wip, original)
        return rejected(
            ExitCode.AGENT_ACTION,
            "push_rejected",
            "your pre-push hook rejected the handoff push; nothing was pushed"
            + (" and the wip commit was undone" if wip else ""),
            "fix what the hook reports, then run `forge handoff` again",
            output=_tail(exc.stderr),
        )
    except BaseException:
        # Any other failure (network, remote error, interruption): give the changes back
        # uncommitted. Should the push have landed anyway, the re-run finds the handoff on
        # the branch, sees that the working tree holds exactly its content and aligns the
        # local branch (see _align_local).
        _undo_wip(git, wip, original)
        raise
    if not result.ok:
        _undo_wip(git, wip, original)  # first: the query below may fail
        try:
            current = git.remote_branch_sha(name, ctx.remote)
            moved_to = current[:12] if current else "deleted"
        except GitError:
            moved_to = "unknown"
        return rejected(
            ExitCode.PRECONDITION,
            "lease_rejected",
            f"{name} moved on {ctx.remote} since it was fetched ({remote_tip[:12]} -> "
            f"{moved_to}); nothing was pushed" + (" and the wip commit was undone" if wip else ""),
            f"check `forge task show {number}` (the task may have been reclaimed)",
        )
    # The event commit has HEAD's tree: moving the branch keeps the index and tree clean.
    # Compare-and-swap: a commit made here while the pre-push hook ran must not be dropped.
    if git.update_ref(ref, event, old=head):
        return _Published(event, wip, commits)
    moved = git.rev_parse(ref)
    warnings.append(
        f"the local {name} moved while the handoff was pushed ({head[:12]} -> "
        f"{moved[:12] if moved else 'deleted'}); it was left as it is"
    )
    return _Published(event, wip, commits, tuple(_align_local(git, name, event, warnings)))


UNCHECKED_PREFIX = "unchecked working tree"
"""Start of the ``unpublished`` entry for a workspace where ``task/<n>`` is not checked out."""


def _align_local(git: Git, name: str, tip: str, warnings: list[str]) -> list[str]:
    """Bring the local branch to the pushed handoff ``tip`` when nothing would be lost;
    return the local work that is **not** part of the handoff (empty when none).

    Unpublished work is: commits of the local ``task/<n>`` that ``tip`` does not contain; a
    working tree whose content is neither HEAD's nor the tip's; and, when ``task/<n>`` is not
    checked out in this workspace (a detached HEAD left by a stopped rebase, another branch),
    an *unchecked working tree* entry, since that work cannot be compared. A working tree
    holding exactly the tip's content (a wip commit undone after a push that landed, a clone
    that made the same changes) is published: the branch and the index are moved to the tip
    (a mixed reset, the files are left alone), unless the index holds staged content that is
    neither HEAD's nor the tip's, which is then left as it is with a warning.
    """
    ref = f"refs/heads/{name}"
    local = git.rev_parse(ref)
    unpublished: list[str] = []
    if local is not None and not git.is_ancestor(local, tip):
        for commit in git.commits(f"{tip}..{local}"):
            unpublished.append(f"commit {commit.sha[:12]} {commit.subject}")
    current = git.current_branch()
    if current != name or local is None:
        if current is None:
            head = git.rev_parse("HEAD")
            where = f"HEAD is detached at {head[:12] if head else 'nothing'}"
        else:
            where = f"{current} is checked out"
        unpublished.append(
            f"{UNCHECKED_PREFIX}: {name} is not checked out in {git.root()} ({where}); its "
            "work was not compared with the handoff"
        )
        return unpublished
    working = git.worktree_tree()
    tip_tree = git.tree_of(tip)
    if working != tip_tree and working != git.tree_of(local):
        files = git.dirty_files() or ["(the working tree differs from HEAD)"]
        unpublished.extend(f"uncommitted change {path}" for path in files)
    if unpublished:
        return unpublished
    if local == tip:
        return []
    if working == tip_tree:
        if git.index_matches(local) or git.index_matches(tip):
            # Mixed reset: moves the branch and the index, never touches the files.
            git.reset(tip, "mixed")
        else:
            warnings.append(
                f"the local {name} is behind the handoff tip {tip[:12]}; left as it is "
                "because the index holds staged content that is neither HEAD's nor the tip's"
            )
        return []
    warnings.append(
        f"the local {name} is behind the handoff tip {tip[:12]}; left as it is because the "
        "working tree differs from it (nothing local is lost)"
    )
    return []


# --------------------------------------------------------------------------- GitHub steps


@dataclass(frozen=True)
class _Completed:
    status: Status | None
    comment_id: int
    comment_created: bool


def _still_handed_off(ctx: ClaimContext, name: str, tip: str) -> bool:
    return ctx.git.remote_branch_sha(name, ctx.remote) == tip


def _complete(
    ctx: ClaimContext,
    record: TaskRecord,
    login: str,
    note: str,
    handoff_sha: str,
    claim_sha: str,
    claim_run: str | None,
    now: datetime,
    warnings: list[str],
) -> _Completed:
    """Comment, unassign, labels, then ``released_at`` in the lease comment (last)."""
    github = ctx.github
    number = record.issue.number
    name = TaskMetadata.branch_name(number)
    comments = github.list_comments(number)
    existing = _comment_for(comments, handoff_sha)
    if existing is None:
        body = render_handoff_comment(
            note, login=login, agent=ctx.agent.id, at=now, branch=name, tip=handoff_sha
        )
        posted = github.create_comment(number, body)
        comment_id, created = posted.id, True
    else:
        comment_id, created = existing.id, False
    github.remove_assignees(number, [login])

    moved = (
        f"{name} moved on {ctx.remote} after the handoff (someone resumed the task); its labels "
        "and lease comment now belong to the new claim and were left alone"
    )
    if not _still_handed_off(ctx, name, handoff_sha):
        warnings.append(moved)
        return _Completed(None, comment_id, created)
    issue = github.get_issue(number)
    derived = derive_status(
        issue=issue,
        meta=record.meta,
        deps_closed={},  # unused: the handoff event decides before dependencies (rule 5)
        branch=BranchState(True, handoff_sha, BranchEvent.HANDOFF, login, ctx.agent.id),
        has_open_pr=bool(github.list_pulls(state="open", head=name)),
    )
    warnings.extend(derived.warnings)
    diff = desired_labels(issue.labels, derived.status, record.meta)
    _apply_labels(github, number, sorted(diff.add), sorted(diff.remove))

    # The lease comment is the completion marker: written last, and only for my claim.
    if not _still_handed_off(ctx, name, handoff_sha):
        warnings.append(moved)
        return _Completed(derived.status, comment_id, created)
    fresh = github.list_comments(number)
    found = find_lease(fresh)
    lease = None if found is None else found.data
    if lease is None or not lease_names_claim(lease, claim_sha, claim_run):
        warnings.append(
            f"the lease comment of #{number} no longer names your claim {claim_sha[:12]}; it "
            "was left alone"
        )
        return _Completed(derived.status, comment_id, created)
    released = LeaseData(
        task=lease.task,
        owner=lease.owner,
        agent=lease.agent,
        claim=lease.claim,
        claimed_at=lease.claimed_at,
        heartbeat_at=lease.heartbeat_at,
        released_at=lease.released_at or now,
        run=lease.run,
    )
    upsert_lease(github, number, released, comments=fresh)
    return _Completed(derived.status, comment_id, created)


# --------------------------------------------------------------------------- entry point


def handoff(ctx: ClaimContext, issue_number: int, note: str) -> HandoffOutcome:
    """Hand off my claim on ``issue_number`` with ``note`` (``forge handoff``, ADR 0012).

    ``ctx.git`` must run in the task's worktree (or the main working tree when the task is
    checked out in place). Returns exit 0 (handed off, or an interrupted handoff completed),
    2 (a commit or pre-push hook refused, or a completed handoff left local work unpublished)
    or 5 (the push lease was rejected). Raises :class:`PreconditionError` (exit 5) before
    anything changes when: the login differs from the profile, the clone is shallow, the task
    has an open pull request, the branch's last event is not my claim (nor my own interrupted
    handoff), the claim is incomplete, a sync, rebase, merge, cherry-pick or revert is in
    progress, ``task/<n>`` is not checked out, or it does not contain ``origin/task/<n>``. An
    empty or oversized note is :class:`ForgeError` (exit 1). Any other failure of the push
    step undoes the wip commit and is re-raised.
    """
    text = check_note(note)
    require_flock()
    login = check_identity(ctx.github, ctx.profile_login)
    check_full_history(ctx.git, "handoff")
    github = ctx.github
    agent = ctx.agent.id
    record = load_task(github, issue_number)
    task_id = record.meta.id
    name = TaskMetadata.branch_name(issue_number)
    now = ctx.now()
    probe = render_handoff_comment(
        text, login=login, agent=agent, at=now, branch=name, tip="0" * 64
    )
    if len(probe) > MAX_COMMENT_CHARS:
        raise ForgeError(
            f"the handoff note is too long for a GitHub comment ({len(probe)} characters with "
            f"the header, the limit is {MAX_COMMENT_CHARS}); nothing was changed",
            hint="shorten the note; link longer material (an ADR, a doc in the branch) instead",
        )
    pulls = github.list_pulls(state="open", head=name)
    if pulls:
        numbers = ", ".join(f"#{pr.number}" for pr in pulls)
        raise PreconditionError(
            f"#{issue_number} ({task_id}) has an open pull request ({numbers}): handing it off "
            "now would leave it in review, which `forge next` never resumes; nothing was "
            "changed",
            hint="the task is in review: address review comments on the PR yourself, or close "
            "the PR, then hand off",
        )
    branch = read_branch(ctx, issue_number)
    comments = github.list_comments(issue_number)
    found = find_lease(comments)
    lease = None if found is None else found.data
    claim = None if branch is None else find_claim(issue_number, branch, task_id)
    warnings: list[str] = []
    missing = missing_headings(text)
    if missing:
        warnings.append("the handoff note lacks the §8.3 heading(s): " + ", ".join(missing))

    recovered = False
    unpublished: list[str] = []
    published: _Published | None = None
    if claim is not None and claim.is_mine(login, agent):
        if lease is None or not claim.named_by(lease) or lease.released_at is not None:
            raise PreconditionError(
                f"your claim on #{issue_number} ({task_id}) is incomplete: the lease comment "
                f"does not name its claim commit {claim.sha[:12]}",
                hint=f"complete it with `forge next --task {issue_number}`, then hand off",
            )
        activity = last_activity(claim.tip_date, lease, claim.sha, claim.run)
        if is_stale(activity, ctx.config.lease_hours, now):
            # Nobody reclaimed it (their claim would be the last event): hand it off anyway.
            warnings.append(
                f"your claim on #{issue_number} ({task_id}) had gone stale at "
                f"{format_time(stale_at(activity, ctx.config.lease_hours))}; handed off anyway"
            )
        result = _publish(ctx, record, login, claim.tip, now, warnings)
        if isinstance(result, HandoffOutcome):
            return replace(
                result,
                claim=claim.sha,
                missing_headings=tuple(missing),
                warnings=_dedupe(warnings),
            )
        published = result
        unpublished = list(result.unpublished)
        handoff_sha, claim_sha, claim_run = result.handoff, claim.sha, claim.run
    else:
        previous = _previous_handoff(login, agent, task_id, branch, lease)
        if previous is None:
            raise _not_mine(record, branch, login, agent)
        _refuse_in_progress(ctx.git)
        recovered = True
        handoff_sha = previous.commit.sha
        claim_sha, claim_run = previous.claim_sha, previous.claim_run
        warnings.append(f"#{issue_number}: completing an interrupted handoff")
        assert branch is not None
        unpublished = _align_local(ctx.git, name, branch.tip, warnings)

    done = _complete(ctx, record, login, text, handoff_sha, claim_sha, claim_run, now, warnings)
    # a leftover of an incomplete claim (normally removed when the claim completed)
    remove_pending(ctx.git, issue_number, claim_sha, timeout=ctx.lock_timeout)
    error = hint = None
    if unpublished:
        error = (
            f"{name} was handed off at {handoff_sha[:12]}, but this clone has local work that "
            "is not part of the handoff: " + "; ".join(unpublished)
        )
        hint = (
            f"to hand it off too, resume the task (`forge next --task {issue_number}`), then "
            "run `forge handoff` again; otherwise move it to another branch or stash it "
            "(`git stash -u`). The note, labels and lease of the handoff are complete"
        )
        if any(item.startswith(UNCHECKED_PREFIX) for item in unpublished):
            hint = (
                f"finish or set aside the work in this workspace, `git switch {name}`, then "
                f"run `forge handoff` again to compare it with the handoff; {hint}"
            )
    return HandoffOutcome(
        code=ExitCode.AGENT_ACTION if unpublished else ExitCode.OK,
        outcome="unpublished_work" if unpublished else "handed_off",
        issue=issue_number,
        task_id=task_id,
        branch=name,
        claim=claim_sha,
        handoff_commit=handoff_sha,
        wip_commit=None if published is None else published.wip,
        commits=0 if published is None else published.commits,
        status=done.status,
        comment_id=done.comment_id,
        comment_created=done.comment_created,
        recovered=recovered,
        missing_headings=tuple(missing),
        unpublished=tuple(unpublished),
        error=error,
        hint=hint,
        warnings=_dedupe(warnings),
    )


__all__ = [
    "MAX_COMMENT_CHARS",
    "REQUIRED_HEADINGS",
    "WIP_MESSAGE",
    "HandoffOutcome",
    "check_note",
    "handoff",
    "missing_headings",
    "note_headings",
    "render_handoff_comment",
]
