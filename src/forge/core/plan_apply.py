"""`forge plan apply <milestone>`: create and update a milestone's task issues (spec §9.5,
ADR 0013).

The work is split like ``forge refresh`` so that every decision is pure:

1. gather (:func:`gather_apply_state`): list every ``forge:task`` issue (open and closed),
   the open pull requests, the labels and the milestones through :class:`GitHubClient`
   **first**, then fetch and read the remote task branches through :class:`Git`. A claim that
   lands between the two reads is seen on the branch, so the task counts as started;
2. plan (:func:`compute_apply_plan`): diff the validated plan against that snapshot and list
   every write: catalogue labels, the milestone, issue creations, title/body/milestone
   updates, label changes (status recomputed with the refresh logic over the snapshot as it
   will be after the apply), comments and closures. The comments already on an issue are an
   input, so a warning is never posted twice; :attr:`ApplyPlan.comment_issues` names the
   issues whose comments the plan needs;
3. execute (:func:`execute_apply_plan`): perform the writes in a fixed order, issue by issue.
   After creating issues it lists them again and closes the duplicates that a concurrent
   apply may have created (the lowest issue number wins).

Applying the same plan twice writes nothing the second time, and an interrupted run followed
by a re-run converges to the state of an uninterrupted run: every write is computed as a diff,
comments are deduplicated by marker, and a closure is always preceded by its comment.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from functools import partial
from typing import Any, Literal

from forge.core.errors import ExitCode, ForgeError
from forge.core.git import Git, GitError
from forge.core.github import (
    Comment,
    GitHubClient,
    GitHubError,
    Issue,
    Label,
    Milestone,
)
from forge.core.output import Result
from forge.core.plan import PlanFile, PlanTask, PlanValidation, RoadmapMilestone, plan_path
from forge.core.refresh import (
    RefreshContext,
    TaskBranch,
    TaskRecord,
    check_full_history,
    derive_task_statuses,
    fetch_task_refs,
    gather_task_branches,
    parse_records,
)
from forge.core.tasks import (
    FORGE_TASK_LABEL,
    LABELS,
    BranchEvent,
    Status,
    TaskBodyError,
    TaskMetadata,
    description_text_problem,
    desired_labels,
    has_label,
    metadata_labels,
    parse_body,
    upsert_block,
    upsert_description,
)

PLAN_WARNING_MARKER = "<!-- forge:plan-warning -->"
"""First line of a warning comment about plan changes that were not applied (ADR 0013)."""

PLAN_REMOVED_MARKER = "<!-- forge:plan-removed -->"
"""First line of the comment posted before closing a task removed from the plan."""

PLAN_DUPLICATE_MARKER = "<!-- forge:plan-duplicate -->"
"""First line of the comment posted before closing a duplicate task issue."""

STRUCTURAL_FIELDS: tuple[str, ...] = (
    "spec",
    "covers",
    "depends_on",
    "zones",
    "size",
    "tier",
    "type",
    "allow_contract_change",
)
"""Metadata fields that are not applied to a started task (only ``done_when`` is)."""

STARTED_STATUSES: frozenset[Status] = frozenset(
    {Status.IN_PROGRESS, Status.HANDOFF, Status.IN_REVIEW}
)

_PLACEHOLDER_BASE = 1_000_000_000
"""Issue numbers given to not-yet-created issues while simulating the post-apply state."""

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def warning_key_line(key: str) -> str:
    """The hidden line that carries a warning comment's key."""
    return f"<!-- forge:plan-warning-key {key} -->"


# --------------------------------------------------------------------------- state


@dataclass(frozen=True)
class ApplyState:
    """What :func:`compute_apply_plan` reads: a snapshot of GitHub and of the task branches.

    ``issues`` are the ``forge:task`` issues in every state; ``branches`` the remote task
    branches by issue number; ``open_pr_heads`` the head branch names of open pull requests.
    ``comments`` holds the comments of the issues listed in
    :attr:`ApplyPlan.comment_issues` (an issue missing from it counts as having none).
    ``warnings`` come from gathering; ``proposals`` holds the issues that plan tasks adopt.
    """

    issues: tuple[Issue, ...]
    branches: Mapping[int, TaskBranch]
    open_pr_heads: frozenset[str]
    labels: tuple[Label, ...]
    milestones: tuple[Milestone, ...]
    comments: Mapping[int, tuple[Comment, ...]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    proposals: Mapping[int, Issue | None] = field(default_factory=dict)
    """Issues named by plan tasks' ``proposal`` field (``None``: not found)."""


# --------------------------------------------------------------------------- actions


@dataclass(frozen=True)
class MilestoneAction:
    """Create the GitHub milestone, or update its description."""

    action: Literal["create", "update"]
    title: str
    description: str
    number: int | None = None

    def to_data(self) -> dict[str, Any]:
        """JSON view."""
        return {"action": self.action, "title": self.title, "description": self.description}


@dataclass(frozen=True)
class CreateIssue:
    """A task issue to create."""

    task_id: str
    title: str
    body: str
    labels: tuple[str, ...]
    milestone: str
    status: Status | None

    def to_data(self) -> dict[str, Any]:
        """JSON view (the body is left out)."""
        return {
            "id": self.task_id,
            "title": self.title,
            "labels": list(self.labels),
            "milestone": self.milestone,
            "status": None if self.status is None else self.status.value,
        }


@dataclass(frozen=True)
class UpdateIssue:
    """Title, body and milestone changes of one issue; ``None`` leaves a field unchanged.

    ``fields`` names what changes: ``title``, ``description``, ``metadata`` (not started),
    ``done_when`` (started), ``block_format`` (the block is rewritten with equal values) and
    ``milestone``. ``adopt`` marks the adoption of a ``forge propose`` issue. ``task`` is the
    plan task, used to rebuild the update from the issue as it is right before the write.
    """

    issue: int
    task_id: str
    fields: tuple[str, ...]
    title: str | None = None
    body: str | None = None
    milestone: str | None = None
    started: Status | None = None
    adopt: bool = False
    task: PlanTask | None = field(default=None, compare=False, repr=False)

    def to_data(self) -> dict[str, Any]:
        """JSON view (the body is left out)."""
        return {
            "issue": self.issue,
            "id": self.task_id,
            "fields": list(self.fields),
            "title": self.title,
            "milestone": self.milestone,
            "started": None if self.started is None else self.started.value,
            "adopt": self.adopt,
        }


@dataclass(frozen=True)
class LabelChange:
    """Labels to add to and remove from one issue."""

    issue: int
    task_id: str
    from_status: Status | None
    to_status: Status | None
    add: tuple[str, ...]
    remove: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON view, in the shape of a ``forge refresh`` change."""
        return {
            "issue": self.issue,
            "id": self.task_id,
            "from": None if self.from_status is None else self.from_status.value,
            "to": None if self.to_status is None else self.to_status.value,
            "added": list(self.add),
            "removed": list(self.remove),
        }


CommentKind = Literal["plan_warning", "removed", "duplicate"]


@dataclass(frozen=True)
class PostComment:
    """A comment to post on one issue, unless a comment containing ``needle`` exists."""

    issue: int
    task_id: str
    kind: CommentKind
    body: str
    needle: str

    def to_data(self) -> dict[str, Any]:
        """JSON view, body included."""
        return {"issue": self.issue, "id": self.task_id, "kind": self.kind, "body": self.body}


@dataclass(frozen=True)
class CloseIssue:
    """An issue to close as ``not_planned``."""

    issue: int
    task_id: str
    reason: Literal["removed", "duplicate"]
    duplicate_of: int | None = None

    def to_data(self) -> dict[str, Any]:
        """JSON view."""
        return {
            "issue": self.issue,
            "id": self.task_id,
            "reason": self.reason,
            "duplicate_of": self.duplicate_of,
        }


TaskAction = Literal["create", "adopt", "update", "unchanged", "closed", "skipped"]


@dataclass(frozen=True)
class TaskOutcome:
    """What the apply does for one plan task."""

    task_id: str
    issue: int | None
    action: TaskAction
    started: Status | None = None
    fields: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    reason: str | None = None

    def to_data(self) -> dict[str, Any]:
        """JSON view."""
        return {
            "id": self.task_id,
            "issue": self.issue,
            "action": self.action,
            "started": None if self.started is None else self.started.value,
            "fields": list(self.fields),
            "pending": list(self.pending),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExtraOutcome:
    """An issue outside the plan's upserts: a removed task or a duplicate.

    ``action`` is ``close`` (closed as not planned), ``keep`` (started: left open with a
    warning) or ``skip`` (``status:proposed``, left alone).
    """

    task_id: str
    issue: int
    action: Literal["close", "keep", "skip"]
    started: Status | None = None
    duplicate_of: int | None = None

    def to_data(self) -> dict[str, Any]:
        """JSON view."""
        return {
            "id": self.task_id,
            "issue": self.issue,
            "action": self.action,
            "started": None if self.started is None else self.started.value,
            "duplicate_of": self.duplicate_of,
        }


@dataclass(frozen=True)
class ApplyPlan:
    """Every write the apply would make, plus outcomes and warnings."""

    milestone: str
    file: str
    labels: tuple[Label, ...]
    milestone_action: MilestoneAction | None
    creates: tuple[CreateIssue, ...]
    updates: tuple[UpdateIssue, ...]
    label_changes: tuple[LabelChange, ...]
    comments: tuple[PostComment, ...]
    closes: tuple[CloseIssue, ...]
    tasks: tuple[TaskOutcome, ...]
    removed: tuple[ExtraOutcome, ...]
    duplicates: tuple[ExtraOutcome, ...]
    warnings: tuple[str, ...]
    comment_issues: frozenset[int]

    @property
    def write_count(self) -> int:
        """Number of GitHub write calls the plan makes (before duplicate cleanup)."""
        label_calls = sum((1 if c.add else 0) + len(c.remove) for c in self.label_changes)
        return (
            len(self.labels)
            + (0 if self.milestone_action is None else 1)
            + len(self.creates)
            + len(self.updates)
            + label_calls
            + len(self.comments)
            + len(self.closes)
        )

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` of ``forge plan apply`` (see the command)."""
        actions = Counter(t.action for t in self.tasks)
        return {
            "milestone": self.milestone,
            "file": self.file,
            "labels_created": [label.name for label in self.labels],
            "milestone_action": None
            if self.milestone_action is None
            else self.milestone_action.to_data(),
            "tasks": [t.to_data() for t in self.tasks],
            "removed": [r.to_data() for r in self.removed],
            "duplicates": [d.to_data() for d in self.duplicates],
            "creates": [c.to_data() for c in self.creates],
            "updates": [u.to_data() for u in self.updates],
            "label_changes": [c.to_data() for c in self.label_changes],
            "comments": [c.to_data() for c in self.comments],
            "closes": [c.to_data() for c in self.closes],
            "warnings": list(self.warnings),
            "counts": {
                "tasks": len(self.tasks),
                "create": actions.get("create", 0),
                "update": actions.get("update", 0),
                "unchanged": actions.get("unchanged", 0),
                "closed": actions.get("closed", 0),
                "skipped": actions.get("skipped", 0),
                "removed": len(self.removed),
                "duplicates": len(self.duplicates),
                "label_changes": len(self.label_changes),
                "comments": len(self.comments),
                "closes": len(self.closes),
                "writes": self.write_count,
                "warnings": len(self.warnings),
            },
        }


# --------------------------------------------------------------------------- helpers


def issue_title(task: PlanTask) -> str:
    """The issue title of a plan task: ``[<id>] <title>``."""
    return f"[{task.meta.id}] {task.title.strip()}"


def milestone_description(entry: RoadmapMilestone | None) -> str | None:
    """The GitHub milestone description from the roadmap entry (``None``: no roadmap entry)."""
    if entry is None:
        return None
    goal = (entry.goal or "").strip()
    return f"{entry.title.strip()}: {goal}" if goal else entry.title.strip()


def render_task_body(task: PlanTask) -> str:
    """The body of a new task issue: the description region, a blank line, the metadata block."""
    return upsert_block(upsert_description("", task.description), task.meta)


def started_status(
    number: int,
    task_id: str,
    branches: Mapping[int, TaskBranch],
    open_pr_heads: Set[str],
) -> Status | None:
    """``in-review``, ``in-progress`` or ``handoff`` when the task is started, else ``None``.

    Started means an open pull request from ``task/<n>``, or a last Forge event on the
    branch (for this task id) that is a claim or a handoff (ADR 0013). A stale claim still
    counts.
    """
    if TaskMetadata.branch_name(number) in open_pr_heads:
        return Status.IN_REVIEW
    branch = branches.get(number)
    if branch is None:
        return None
    event = branch.state(task_id).last_event
    if event is BranchEvent.CLAIM:
        return Status.IN_PROGRESS
    if event is BranchEvent.HANDOFF:
        return Status.HANDOFF
    return None


def _event_author(number: int, task_id: str, branches: Mapping[int, TaskBranch]) -> str | None:
    branch = branches.get(number)
    return None if branch is None else branch.state(task_id).event_author


def _fmt(value: Any) -> str:
    if isinstance(value, tuple):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _key(payload: Mapping[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _mentions(issue: Issue, task_id: str, branches: Mapping[int, TaskBranch]) -> str:
    """``@a @b`` for the assignees, else the author of the branch's last Forge event."""
    logins = list(issue.assignees)
    if not logins:
        author = _event_author(issue.number, task_id, branches)
        if author:
            logins = [author]
    return " ".join(f"@{login}" for login in logins)


def _has_comment(comments: Sequence[Comment], needle: str) -> bool:
    return any(needle in c.body for c in comments)


def pending_changes(current: TaskMetadata, target: TaskMetadata) -> list[tuple[str, str, str]]:
    """``(field, current, planned)`` for each structural field that differs (formatted)."""
    return [
        (name, _fmt(getattr(current, name)), _fmt(getattr(target, name)))
        for name in STRUCTURAL_FIELDS
        if getattr(current, name) != getattr(target, name)
    ]


def changes_warning(
    issue: Issue,
    task_id: str,
    milestone: str,
    file: str,
    started: Status,
    changes: Sequence[tuple[str, str, str]],
    branches: Mapping[int, TaskBranch],
) -> tuple[str, str]:
    """``(key, body)`` of the warning about plan changes not applied to a started task."""
    key = _key({"task": task_id, "kind": "changes", "changes": [list(c) for c in changes]})
    who = _mentions(issue, task_id, branches)
    lines = [
        PLAN_WARNING_MARKER,
        f"{who + ': ' if who else ''}`forge plan apply {milestone}` did not apply these "
        f"changes from `{file}` to {task_id}, because the task is started ({started.value}):",
        "",
        *(f"- `{name}`: {old} → {new}" for name, old, new in changes),
        "",
        "Talk with the lead: either adapt the work and the issue's Forge block, or revert the "
        "plan change.",
        warning_key_line(key),
    ]
    return key, "\n".join(lines) + "\n"


def removed_warning(
    issue: Issue,
    task_id: str,
    milestone: str,
    file: str,
    started: Status,
    branches: Mapping[int, TaskBranch],
) -> tuple[str, str]:
    """``(key, body)`` of the warning about a started task that left the plan."""
    key = _key({"task": task_id, "kind": "removed", "file": file})
    who = _mentions(issue, task_id, branches)
    lines = [
        PLAN_WARNING_MARKER,
        f"{who + ': ' if who else ''}{task_id} is no longer in `{file}`, but the task is "
        f"started ({started.value}), so `forge plan apply {milestone}` left it open.",
        "",
        "Talk with the lead: finish it, or release the claim and close the issue as not planned.",
        warning_key_line(key),
    ]
    return key, "\n".join(lines) + "\n"


def removed_comment(task_id: str, milestone: str, file: str) -> str:
    """Comment posted before closing a not-started task that left the plan."""
    return (
        f"{PLAN_REMOVED_MARKER}\n{task_id} is no longer in `{file}`; closed as not planned by "
        f"`forge plan apply {milestone}`.\n"
    )


def duplicate_comment(task_id: str, winner: int) -> str:
    """Comment posted before closing a duplicate task issue."""
    return (
        f"{PLAN_DUPLICATE_MARKER}\nDuplicate of #{winner}: issue #{winner} already carries "
        f"{task_id} (lowest issue number wins). Closed as not planned by `forge plan apply`.\n"
    )


def _placeholder_issue(number: int, create: CreateIssue) -> Issue:
    return Issue(
        number=number,
        title=create.title,
        body=create.body,
        state="open",
        state_reason=None,
        labels=create.labels,
        assignees=(),
        milestone=create.milestone,
        author="",
        url="",
        created_at=_EPOCH,
        updated_at=_EPOCH,
        closed_at=None,
    )


# --------------------------------------------------------------------------- duplicates


@dataclass(frozen=True)
class _DuplicateActions:
    outcomes: tuple[ExtraOutcome, ...]
    label_changes: tuple[LabelChange, ...]
    comments: tuple[PostComment, ...]
    closes: tuple[CloseIssue, ...]
    warnings: tuple[str, ...]
    comment_issues: frozenset[int]


def _by_id(records: Sequence[TaskRecord]) -> dict[str, list[TaskRecord]]:
    grouped: dict[str, list[TaskRecord]] = {}
    for rec in sorted(records, key=lambda r: r.issue.number):
        grouped.setdefault(rec.meta.id, []).append(rec)
    return grouped


def plan_duplicates(
    records: Sequence[TaskRecord],
    task_ids: Set[str],
    branches: Mapping[int, TaskBranch],
    open_pr_heads: Set[str],
    comments: Mapping[int, Sequence[Comment]],
) -> _DuplicateActions:
    """Close the open duplicates of each id in ``task_ids`` (pure).

    The winner of an id is its lowest-numbered issue, in any state (ADR 0013). Every other
    open issue carrying the id is closed as ``not_planned`` after a "duplicate of #n"
    comment, unless it is started: then it is kept, with a warning.
    """
    outcomes: list[ExtraOutcome] = []
    label_changes: list[LabelChange] = []
    posts: list[PostComment] = []
    closes: list[CloseIssue] = []
    warnings: list[str] = []
    lookups: set[int] = set()
    for task_id, recs in sorted(_by_id(records).items()):
        if task_id not in task_ids or len(recs) < 2:
            continue
        winner = recs[0].issue.number
        for rec in recs[1:]:
            issue = rec.issue
            if issue.state == "closed":
                continue
            started = started_status(issue.number, task_id, branches, open_pr_heads)
            if started is not None:
                outcomes.append(ExtraOutcome(task_id, issue.number, "keep", started, winner))
                warnings.append(
                    f"issue #{issue.number}: duplicate of #{winner} for {task_id}, but it is "
                    f"started ({started.value}); left open (fix: finish the work on one issue "
                    "and close the other)"
                )
                continue
            outcomes.append(ExtraOutcome(task_id, issue.number, "close", None, winner))
            diff = desired_labels(issue.labels, None, rec.meta)
            if not diff.empty:
                label_changes.append(
                    LabelChange(
                        issue.number,
                        task_id,
                        Status.from_labels(issue.labels),
                        None,
                        tuple(sorted(diff.add)),
                        tuple(sorted(diff.remove)),
                    )
                )
            lookups.add(issue.number)
            if not _has_comment(comments.get(issue.number, ()), PLAN_DUPLICATE_MARKER):
                posts.append(
                    PostComment(
                        issue.number,
                        task_id,
                        "duplicate",
                        duplicate_comment(task_id, winner),
                        PLAN_DUPLICATE_MARKER,
                    )
                )
            closes.append(CloseIssue(issue.number, task_id, "duplicate", winner))
    return _DuplicateActions(
        tuple(outcomes),
        tuple(label_changes),
        tuple(posts),
        tuple(closes),
        tuple(warnings),
        frozenset(lookups),
    )


# --------------------------------------------------------------------------- plan


def _plan_error(file: str, task_id: str, problem: str) -> ForgeError:
    return ForgeError(
        f"{file}: task {task_id}: description: {problem}",
        hint="remove Forge marker lines (<!-- forge:... -->) from the description",
    )


def compute_apply_plan(
    plan: PlanFile,
    state: ApplyState,
    *,
    milestone_description: str | None = None,
) -> ApplyPlan:
    """Diff ``plan`` against ``state`` and list every write (pure; ADR 0013 steps 3-7).

    ``milestone_description`` is the roadmap's description for the milestone (``None``
    without a roadmap entry: an existing milestone's description is then left alone).
    Raises :class:`ForgeError` when a plan description holds a Forge marker line.
    """
    milestone = plan.milestone
    file = plan_path(milestone).as_posix()  # relative: comments and keys are portable
    warnings: list[str] = list(state.warnings)
    for task in plan.tasks:
        problem = description_text_problem(task.description)
        if problem is not None:
            raise _plan_error(file, task.meta.id, problem)

    # 3. labels
    have = {label.name.casefold() for label in state.labels}
    labels = tuple(spec for spec in LABELS if spec.name.casefold() not in have)

    # 4. milestone
    milestone_action: MilestoneAction | None = None
    existing_ms = next((m for m in state.milestones if m.title == milestone), None)
    if existing_ms is None:
        milestone_action = MilestoneAction("create", milestone, milestone_description or "")
    else:
        if milestone_description is not None and existing_ms.description != milestone_description:
            milestone_action = MilestoneAction(
                "update", milestone, milestone_description, existing_ms.number
            )
        if existing_ms.state == "closed":
            warnings.append(
                f"milestone {milestone} is closed on GitHub; its issues are still updated"
            )

    ignored: list[str] = []
    records = parse_records(state.issues, ignored)
    parsed_numbers = {rec.issue.number for rec in records}
    unparsed = [i for i in state.issues if i.number not in parsed_numbers]
    by_id = _by_id(records)
    plan_ids = {t.meta.id for t in plan.tasks}

    def started(rec: TaskRecord) -> Status | None:
        return started_status(rec.issue.number, rec.meta.id, state.branches, state.open_pr_heads)

    outcomes: list[TaskOutcome] = []
    pending_creates: list[tuple[PlanTask, str]] = []
    updates: list[UpdateIssue] = []
    posts: list[PostComment] = []
    closes: list[CloseIssue] = []
    lookups: set[int] = set()
    simulated: dict[int, Issue] = {i.number: i for i in state.issues}
    adopted_issues: dict[int, Issue] = {}

    # 5. upsert
    for task in plan.tasks:
        task_id = task.meta.id
        recs = by_id.get(task_id, [])
        if task.proposal is not None:
            adopted = _check_proposal(task, recs, state, file)
            if adopted is not None:
                proposal, proposal_meta = adopted
                outcome, update, _ = _upsert(
                    task, proposal, proposal_meta, None, milestone, file, {}
                )
                outcomes.append(replace(outcome, action="adopt"))
                adopted_issues[proposal.number] = proposal
                if update is not None:  # None: only the labels are left to change
                    update = replace(update, adopt=True)
                    updates.append(update)
                    proposal = replace(
                        proposal,
                        title=update.title or proposal.title,
                        body=update.body or proposal.body,
                        milestone=update.milestone or proposal.milestone,
                    )
                simulated[proposal.number] = replace(
                    proposal,
                    labels=(
                        *(lb for lb in proposal.labels if not _is_proposed(lb)),
                        FORGE_TASK_LABEL,
                    ),
                )
                continue
        if not recs:
            blocker = next((i for i in unparsed if i.title.startswith(f"[{task_id}]")), None)
            if blocker is not None:
                outcomes.append(
                    TaskOutcome(task_id, blocker.number, "skipped", reason="invalid Forge block")
                )
                warnings.append(
                    f"issue #{blocker.number} looks like {task_id} but its Forge block cannot "
                    f"be read; {task_id} was not created (fix: repair the block, then re-run "
                    "forge plan apply)"
                )
                continue
            pending_creates.append((task, render_task_body(task)))
            continue
        winner = recs[0]
        issue = winner.issue
        if issue.state == "closed":
            outcomes.append(TaskOutcome(task_id, issue.number, "closed"))
            if issue.state_reason == "not_planned":
                warnings.append(
                    f"issue #{issue.number} ({task_id}) is closed as not planned but the task is "
                    f"in {file}; it is not updated (fix: reopen #{issue.number}, then re-run "
                    "forge plan apply)"
                )
            continue
        status = started(winner)
        try:
            outcome, update, post = _upsert(
                task, issue, winner.meta, status, milestone, file, state.comments, state.branches
            )
        except TaskBodyError as exc:
            hint = f" (fix: {exc.hint})" if exc.hint else ""
            warnings.append(f"{exc.message}{hint}; issue not updated")
            outcomes.append(
                TaskOutcome(task_id, issue.number, "skipped", status, reason=exc.reason)
            )
            continue
        outcomes.append(outcome)
        if post is not None:
            lookups.add(issue.number)
            posts.append(post)
        if update is not None:
            updates.append(update)
            simulated[issue.number] = replace(
                issue,
                title=update.title if update.title is not None else issue.title,
                body=update.body if update.body is not None else issue.body,
                milestone=update.milestone if update.milestone is not None else issue.milestone,
            )
        # The lead's plan accepts a proposed task (ADR 0008: only plan apply clears it).
        current = simulated[issue.number]
        simulated[issue.number] = replace(
            current, labels=tuple(lb for lb in current.labels if not _is_proposed(lb))
        )

    # duplicates of plan ids
    dups = plan_duplicates(records, plan_ids, state.branches, state.open_pr_heads, state.comments)
    warnings.extend(dups.warnings)
    posts.extend(dups.comments)
    closes.extend(dups.closes)
    lookups |= dups.comment_issues
    closing = {c.issue for c in dups.closes}

    # 6. removed tasks
    removed: list[ExtraOutcome] = []
    for rec in records:
        issue = rec.issue
        if issue.state == "closed" or rec.meta.milestone != milestone or rec.meta.id in plan_ids:
            continue
        if has_label(issue.labels, Status.PROPOSED.label):
            removed.append(ExtraOutcome(rec.meta.id, issue.number, "skip"))
            warnings.append(
                f"issue #{issue.number} ({rec.meta.id}) is not in {file} but is "
                f"{Status.PROPOSED.label}; left alone (fix: add it to the plan, or close it)"
            )
            continue
        status = started(rec)
        lookups.add(issue.number)
        comments = state.comments.get(issue.number, ())
        if status is not None:
            removed.append(ExtraOutcome(rec.meta.id, issue.number, "keep", status))
            key, body = removed_warning(issue, rec.meta.id, milestone, file, status, state.branches)
            if not _has_comment(comments, warning_key_line(key)):
                posts.append(
                    PostComment(
                        issue.number, rec.meta.id, "plan_warning", body, warning_key_line(key)
                    )
                )
            continue
        removed.append(ExtraOutcome(rec.meta.id, issue.number, "close"))
        if not _has_comment(comments, PLAN_REMOVED_MARKER):
            posts.append(
                PostComment(
                    issue.number,
                    rec.meta.id,
                    "removed",
                    removed_comment(rec.meta.id, milestone, file),
                    PLAN_REMOVED_MARKER,
                )
            )
        closes.append(CloseIssue(issue.number, rec.meta.id, "removed"))
        closing.add(issue.number)

    for number in closing:
        simulated[number] = replace(simulated[number], state="closed", state_reason="not_planned")

    # 7. statuses, computed with the refresh logic over the post-apply snapshot. Closed
    # duplicates that lost to a lower number never affect it (the lowest number wins).
    losers = {
        rec.issue.number
        for recs in by_id.values()
        for rec in recs[1:]
        if simulated[rec.issue.number].state == "closed"
    }
    creates_draft = [
        CreateIssue(
            task.meta.id,
            issue_title(task),
            body,
            tuple(sorted(metadata_labels(task.meta))),
            milestone,
            None,
        )
        for task, body in pending_creates
    ]
    sim_issues = [i for n, i in sorted(simulated.items()) if n not in losers]
    placeholders: dict[int, CreateIssue] = {}
    for index, create in enumerate(creates_draft):
        number = _PLACEHOLDER_BASE + index
        placeholders[number] = create
        sim_issues.append(_placeholder_issue(number, create))
    derive_warnings: list[str] = []
    derived = derive_task_statuses(sim_issues, state.branches, state.open_pr_heads, derive_warnings)
    warnings.extend(derive_warnings)

    creates: list[CreateIssue] = []
    label_changes: list[LabelChange] = list(dups.label_changes)
    original = {i.number: i for i in state.issues} | adopted_issues
    dup_closes = {c.issue for c in dups.closes}
    for d in derived:
        number = d.record.issue.number
        if number in placeholders:
            draft = placeholders[number]
            labels_ = set(draft.labels) | ({d.status.label} if d.status is not None else set())
            creates.append(replace(draft, labels=tuple(sorted(labels_)), status=d.status))
            continue
        before = original[number]
        if before.state == "closed" or number in dup_closes:
            continue  # closed issues are never modified; duplicates are relabelled above
        diff = desired_labels(before.labels, d.status, d.record.meta)
        if diff.empty:
            continue
        label_changes.append(
            LabelChange(
                number,
                d.record.meta.id,
                Status.from_labels(before.labels),
                d.status,
                tuple(sorted(diff.add)),
                tuple(sorted(diff.remove)),
            )
        )

    create_outcomes = {c.task_id: TaskOutcome(c.task_id, None, "create") for c in creates}
    task_order = {t.meta.id: i for i, t in enumerate(plan.tasks)}
    all_outcomes = sorted(
        [*outcomes, *create_outcomes.values()], key=lambda o: task_order[o.task_id]
    )
    label_changes.sort(key=lambda c: c.issue)
    return ApplyPlan(
        milestone=milestone,
        file=file,
        labels=labels,
        milestone_action=milestone_action,
        creates=tuple(creates),
        updates=tuple(updates),
        label_changes=tuple(label_changes),
        comments=tuple(posts),
        closes=tuple(closes),
        tasks=tuple(all_outcomes),
        removed=tuple(removed),
        duplicates=dups.outcomes,
        warnings=tuple(dict.fromkeys(warnings)),
        comment_issues=frozenset(lookups),
    )


def _is_proposed(label: str) -> bool:
    return label.casefold() == Status.PROPOSED.label.casefold()


def _upsert(
    task: PlanTask,
    issue: Issue,
    current: TaskMetadata | None,
    started: Status | None,
    milestone: str,
    file: str,
    comments: Mapping[int, Sequence[Comment]],
    branches: Mapping[int, TaskBranch] | None = None,
) -> tuple[TaskOutcome, UpdateIssue | None, PostComment | None]:
    """Diff one open issue against its plan task (ADR 0013 step 5).

    ``current`` is the issue's metadata (``None`` when adopting a proposal without a block).
    A started task only gets its title, description region and ``done_when``; its other
    pending changes become a warning comment unless ``comments`` already hold it.
    """
    task_id = task.meta.id
    fields: list[str] = []
    title = issue_title(task)
    if issue.title != title:
        fields.append("title")
    body = upsert_description(issue.body, task.description, issue_number=issue.number)
    if body != issue.body:
        fields.append("description")
    pending: list[tuple[str, str, str]] = []
    if started is None or current is None:
        started = None
        new_meta = task.meta
        if current != new_meta:
            fields.append("metadata")
    else:
        new_meta = replace(current, done_when=task.meta.done_when)
        if current.done_when != new_meta.done_when:
            fields.append("done_when")
        pending = pending_changes(current, task.meta)
    with_block = upsert_block(body, new_meta, issue_number=issue.number)
    if with_block != body and current == new_meta:
        fields.append("block_format")
    body = with_block
    new_milestone: str | None = None
    if started is None and issue.milestone != milestone:
        new_milestone = milestone
        fields.append("milestone")

    post: PostComment | None = None
    if started is not None and pending:
        key, text = changes_warning(
            issue, task_id, milestone, file, started, pending, branches or {}
        )
        if not _has_comment(comments.get(issue.number, ()), warning_key_line(key)):
            post = PostComment(issue.number, task_id, "plan_warning", text, warning_key_line(key))

    update: UpdateIssue | None = None
    if fields or current is None:
        update = UpdateIssue(
            issue.number,
            task_id,
            tuple(fields),
            title=title if "title" in fields else None,
            body=body if body != issue.body else None,
            milestone=new_milestone,
            started=started,
            task=task,
        )
    outcome = TaskOutcome(
        task_id,
        issue.number,
        "update" if fields else "unchanged",
        started,
        tuple(fields),
        tuple(name for name, _, _ in pending),
    )
    return outcome, update, post


def _proposal_error(task: PlanTask, number: int, problem: str, fix: str) -> ForgeError:
    return ForgeError(f"task {task.meta.id}: proposal #{number} {problem}", hint=fix)


def _check_proposal(
    task: PlanTask,
    recs: Sequence[TaskRecord],
    state: ApplyState,
    file: str,
) -> tuple[Issue, TaskMetadata | None] | None:
    """Check the issue a task adopts (``proposal``); return ``(issue, metadata)`` to adopt.

    ``None`` means the normal match applies: the task already has its issue, which is the
    proposal (adopted earlier). Raises :class:`ForgeError` (exit 1, before any write) when
    the proposal cannot be adopted.
    """
    number = task.proposal
    assert number is not None
    drop = f"remove `proposal: {number}` from {task.meta.id} in {file}"
    if recs:
        winner = recs[0].issue.number
        if winner == number:
            return None
        raise _proposal_error(
            task,
            number,
            f"cannot be adopted: {task.meta.id} already has issue #{winner}",
            f"{drop}, or close #{winner} as not planned first",
        )
    issue = state.proposals.get(number)
    if issue is None:
        raise _proposal_error(task, number, "does not exist (or is a pull request)", drop)
    if issue.state == "closed":
        raise _proposal_error(task, number, "is closed", f"reopen #{number}, or {drop}")
    try:
        current = parse_body(issue.body, issue_number=number)
    except TaskBodyError as exc:
        raise _proposal_error(
            task, number, f"has an invalid Forge block: {exc.reason}", exc.hint or drop
        ) from exc
    if current is not None and current.id != task.meta.id:
        raise _proposal_error(
            task, number, f"already carries task {current.id}", f"{drop}, or fix the id"
        )
    if current is None and not has_label(issue.labels, Status.PROPOSED.label):
        raise _proposal_error(
            task,
            number,
            f"is not a proposal (it has no {Status.PROPOSED.label} label)",
            f"{drop}, or label #{number} {Status.PROPOSED.label}",
        )
    return issue, current


# --------------------------------------------------------------------------- gather


def gather_apply_state(ctx: RefreshContext, plan: PlanFile | None = None) -> ApplyState:
    """Read GitHub, then fetch and read the task branches (see the module docstring).

    With ``plan``, the issues its tasks adopt (``proposal``) are read too. Raises
    :class:`PreconditionError` (exit 5) on a shallow clone or a missing default branch,
    :class:`ForgeError` (exit 1) on git or GitHub failures. Comments are not read here; see
    :func:`plan_with_comments`.
    """
    git: Git = ctx.git
    default_branch = ctx.config.project.default_branch
    check_full_history(git, "plan apply")
    github = ctx.github
    issues = tuple(github.list_issues(state="all", labels=[FORGE_TASK_LABEL]))
    heads = frozenset(pr.head for pr in github.list_pulls(state="open"))
    labels = tuple(github.list_labels())
    milestones = tuple(github.list_milestones(state="all"))
    proposals = _read_proposals(github, plan, issues)
    fetch_task_refs(git, default_branch)
    scan = gather_task_branches(git, default_branch)
    return ApplyState(
        issues=issues,
        branches=scan.branches,
        open_pr_heads=heads,
        labels=labels,
        milestones=milestones,
        warnings=scan.warnings,
        proposals=proposals,
    )


def _read_proposals(
    github: GitHubClient, plan: PlanFile | None, issues: Sequence[Issue]
) -> dict[int, Issue | None]:
    """The issues named by ``proposal`` fields; ``None`` for one that does not exist."""
    known = {i.number: i for i in issues}
    found: dict[int, Issue | None] = {}
    for task in plan.tasks if plan is not None else ():
        number = task.proposal
        if number is None or number in found:
            continue
        if number in known:
            found[number] = known[number]
            continue
        try:
            found[number] = github.get_issue(number)
        except GitHubError as exc:
            if exc.status != 404 and not _is_pull(github, number):
                raise
            found[number] = None
    return found


def _is_pull(github: GitHubClient, number: int) -> bool:
    """True when ``number`` is a pull request (``get_issue`` refuses those)."""
    try:
        github.get_pull(number)
    except GitHubError:
        return False
    return True


def plan_with_comments(
    github: GitHubClient,
    plan: PlanFile,
    state: ApplyState,
    *,
    milestone_description: str | None = None,
) -> tuple[ApplyPlan, ApplyState]:
    """Compute the plan, reading the comments of the issues it needs them for (read-only)."""
    result = compute_apply_plan(plan, state, milestone_description=milestone_description)
    missing = sorted(result.comment_issues - state.comments.keys())
    if not missing:
        return result, state
    comments = dict(state.comments)
    for number in missing:
        comments[number] = tuple(github.list_comments(number))
    state = replace(state, comments=comments)
    result = compute_apply_plan(plan, state, milestone_description=milestone_description)
    return result, state


# --------------------------------------------------------------------------- execute


@dataclass
class ExecutionReport:
    """What :func:`execute_apply_plan` did: created issue numbers, failures, cleanup."""

    created: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    writes: int = 0
    duplicates: list[ExtraOutcome] = field(default_factory=list)
    dedupe_warnings: list[str] = field(default_factory=list)
    downgraded: list[int] = field(default_factory=list)
    """Issues claimed during the run: only their non-structural fields were updated."""
    kept: list[int] = field(default_factory=list)
    """Issues that were to be closed but were started during the run: left open."""
    closed_duplicates: dict[int, int] = field(default_factory=dict)
    """Duplicates this run closed: loser -> winner."""

    def issue_of(self, task_id: str) -> int | None:
        """The issue a task created in this run ended up with (the winner if it lost)."""
        number = self.created.get(task_id)
        return None if number is None else self.closed_duplicates.get(number, number)

    notes: list[str] = field(default_factory=list)

    def to_data(self) -> dict[str, Any]:
        """JSON view."""
        return {
            "created": dict(self.created),
            "failures": list(self.failures),
            "writes": self.writes,
            "duplicates_closed_after_create": [d.to_data() for d in self.duplicates],
            "downgraded": list(self.downgraded),
            "kept": list(self.kept),
            "closed_duplicates": {str(k): v for k, v in self.closed_duplicates.items()},
        }


def _ensure_label(github: GitHubClient, label: Label) -> None:
    try:
        github.create_label(label.name, label.color, label.description)
    except GitHubError:
        # A concurrent run may have created it: only fail if it is still missing.
        if not any(lb.name.casefold() == label.name.casefold() for lb in github.list_labels()):
            raise


def _ensure_milestone(github: GitHubClient, action: MilestoneAction) -> None:
    if action.action == "update":
        assert action.number is not None
        github.update_milestone(action.number, description=action.description)
        return
    try:
        github.create_milestone(action.title, action.description)
    except GitHubError:
        if not any(m.title == action.title for m in github.list_milestones(state="all")):
            raise


def execute_apply_plan(
    ctx: RefreshContext,
    plan: ApplyPlan,
    state: ApplyState,
) -> ExecutionReport:
    """Perform the writes of ``plan`` (ADR 0013), then clean up concurrent duplicates.

    Order: labels, milestone (a failure there propagates: nothing else is written), issue
    creations, title/body/milestone updates, label changes, comments, closures. Each issue is
    applied independently: a :class:`GitHubError` on one is recorded in
    :attr:`ExecutionReport.failures` and the others proceed. An issue is never closed when
    its comment failed, so a re-run posts it first. When issues were created, the issues are
    listed again and every open duplicate of a plan id is closed (lowest number wins).

    Each update is rebuilt from the issue as it is right before the write, so human edits
    made during the run are kept, and a structural update re-checks ``task/<n>`` on the
    remote first: a task claimed (or handed off) since the snapshot gets only the started
    changes plus the warning comment (:func:`live_started`).
    """
    github = ctx.github
    report = ExecutionReport()

    def attempt(what: str, fn: Callable[[], object]) -> bool:
        try:
            fn()
        except GitHubError as exc:
            report.failures.append(f"{what}: {exc.message}; re-run forge plan apply")
            return False
        report.writes += 1
        return True

    for label in plan.labels:
        _ensure_label(github, label)
        report.writes += 1
    if plan.milestone_action is not None:
        _ensure_milestone(github, plan.milestone_action)
        report.writes += 1

    for create in plan.creates:

        def do_create(c: CreateIssue = create) -> None:
            issue = github.create_issue(c.title, c.body, labels=c.labels, milestone=c.milestone)
            report.created[c.task_id] = issue.number

        attempt(f"{create.task_id}: creating the issue failed", do_create)

    label_changes: dict[int, LabelChange | None] = {c.issue: c for c in plan.label_changes}
    posts: list[PostComment] = []
    for update in plan.updates:
        outcome = _execute_update(ctx, plan, state, update, report, attempt)
        if outcome.skip_labels:
            label_changes[update.issue] = None
        elif outcome.labels is not None:
            label_changes[update.issue] = outcome.labels or None
        if outcome.post is not None:
            posts.append(outcome.post)

    closing = {c.issue for c in plan.closes}
    rebuilt = {u.issue for u in plan.updates}  # their warning was rebuilt from the fresh issue
    planned_posts = [
        c
        for c in plan.comments
        if c.issue not in closing and not (c.kind == "plan_warning" and c.issue in rebuilt)
    ]
    _apply_labels(
        github,
        [c for n, c in label_changes.items() if c is not None and n not in closing],
        attempt,
    )
    _post_comments(github, [*posts, *planned_posts], attempt)
    _execute_closures(
        ctx,
        plan,
        state,
        plan.closes,
        {n: c for n, c in label_changes.items() if c is not None and n in closing},
        {c.issue: c for c in plan.comments if c.issue in closing},
        report,
        attempt,
    )

    if report.created:
        _dedupe_after_create(ctx, plan, state, report, attempt)
    return report


@dataclass(frozen=True)
class _UpdateOutcome:
    """How an update ended. ``labels``: ``None`` keeps the planned label change, ``False``
    drops it, a :class:`LabelChange` replaces it."""

    skip_labels: bool = False
    labels: LabelChange | Literal[False] | None = None
    post: PostComment | None = None


def live_branch(
    git: Git,
    default_branch: str,
    number: int,
    snapshot: TaskBranch | None,
    remote: str = "origin",
) -> TaskBranch | None:
    """Re-read ``task/<number>`` on the remote right before a structural write.

    Returns ``snapshot`` when the remote branch is still at its tip, ``None`` when the branch
    is gone (or has no common history with the default branch, as in refresh), else the
    branch fetched again. A tip that cannot be compared yields a branch whose last event is
    an (unknown) claim, so callers stay conservative. Raises :class:`GitError` when the
    remote cannot be read.
    """
    branch = TaskMetadata.branch_name(number)
    sha = git.remote_branch_sha(branch, remote)
    if sha is None:
        return None
    if snapshot is not None and sha == snapshot.tip:
        return snapshot
    git.fetch(
        remote, prune=False, refspecs=[f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"]
    )
    tip = git.rev_parse(f"refs/remotes/{remote}/{branch}")
    base = git.rev_parse(f"refs/remotes/{remote}/{default_branch}")
    if tip is None or base is None:
        return _UnreadableBranch(sha)  # cannot tell: callers treat it as started
    try:
        merge_base = git.merge_base(base, tip)
    except GitError:
        return None
    commits = git.commits(f"{merge_base}..{tip}", first_parent=True)
    return TaskBranch(tip, tuple(c.trailers for c in commits))


def live_started(
    git: Git,
    default_branch: str,
    number: int,
    task_id: str,
    snapshot: TaskBranch | None,
    remote: str = "origin",
) -> Status | None:
    """Whether ``task/<number>`` is started now (claim or handoff), read live on the remote.

    See :func:`live_branch`; the last Forge event for ``task_id`` decides, as in
    :func:`started_status`. Raises :class:`GitError` when the remote cannot be read.
    """
    branch = live_branch(git, default_branch, number, snapshot, remote)
    if branch is None:
        return None
    return _branch_started(number, task_id, branch)


@dataclass(frozen=True)
class _UnreadableBranch(TaskBranch):
    """A remote task branch whose events could not be read: counts as started."""


def _branch_started(number: int, task_id: str, branch: TaskBranch) -> Status | None:
    if isinstance(branch, _UnreadableBranch):
        return Status.IN_PROGRESS
    return started_status(number, task_id, {number: branch}, set())


def _execute_update(
    ctx: RefreshContext,
    plan: ApplyPlan,
    state: ApplyState,
    planned: UpdateIssue,
    report: ExecutionReport,
    attempt: Attempt,
) -> _UpdateOutcome:
    """Rebuild ``planned`` from the fresh issue (and the live branch), then write it."""
    github = ctx.github
    what = f"issue #{planned.issue} ({planned.task_id})"
    task = planned.task
    assert task is not None
    try:
        fresh = github.get_issue(planned.issue)
    except GitHubError as exc:
        report.failures.append(f"{what}: reading the issue failed: {exc.message}; re-run")
        return _UpdateOutcome(skip_labels=True)
    if fresh.state == "closed":
        report.notes.append(f"{what}: closed during the run; not modified")
        return _UpdateOutcome(skip_labels=True)
    changed = f"{what}: the issue's Forge block changed during the run; re-run forge plan apply"
    try:
        current = parse_body(
            fresh.body,
            issue_number=fresh.number,
            milestone=None if planned.adopt else fresh.milestone,
        )
    except TaskBodyError:
        report.failures.append(changed)
        return _UpdateOutcome(skip_labels=True)
    if (current is None and not planned.adopt) or (
        current is not None and current.id != planned.task_id
    ):
        report.failures.append(changed)
        return _UpdateOutcome(skip_labels=True)

    if planned.adopt and current is None and not has_label(fresh.labels, Status.PROPOSED.label):
        report.notes.append(
            f"{what}: no longer labelled {Status.PROPOSED.label}; not adopted (fix: label it "
            "again, or remove `proposal` from the task)"
        )
        return _UpdateOutcome(skip_labels=True)

    started = planned.started
    try:
        _, update, post = _upsert(
            task, fresh, current, started, plan.milestone, plan.file, {}, state.branches
        )
        structural = update is not None and bool({"metadata", "milestone"} & set(update.fields))
        if started is None and current is not None and structural:
            started = live_started(
                ctx.git,
                ctx.config.project.default_branch,
                fresh.number,
                planned.task_id,
                state.branches.get(fresh.number),
            )
            if started is not None:
                _, update, post = _upsert(
                    task, fresh, current, started, plan.milestone, plan.file, {}, state.branches
                )
    except TaskBodyError:
        report.failures.append(changed)
        return _UpdateOutcome(skip_labels=True)
    except GitError as exc:
        report.failures.append(f"{what}: could not re-check its task branch: {exc.message}")
        return _UpdateOutcome(skip_labels=True)
    if update is not None and (update.title or update.body or update.milestone):
        written = attempt(f"{what}: update failed", partial(_write_update, github, update))
        if not written and planned.adopt:
            # Never label a proposal forge:task before it carries its block: the next run
            # would no longer recognise it as a proposal.
            return _UpdateOutcome(skip_labels=True)
    if started is None or planned.started is not None:
        return _UpdateOutcome(post=post)
    # Claimed during the run: the planned labels (status, mirrors) no longer hold.
    assert current is not None
    report.downgraded.append(fresh.number)
    kept = replace(current, done_when=task.meta.done_when)
    diff = desired_labels(fresh.labels, started, kept)
    labels: LabelChange | Literal[False] = (
        False
        if diff.empty
        else LabelChange(
            fresh.number,
            planned.task_id,
            Status.from_labels(fresh.labels),
            started,
            tuple(sorted(diff.add)),
            tuple(sorted(diff.remove)),
        )
    )
    return _UpdateOutcome(labels=labels, post=post)


def _write_update(github: GitHubClient, update: UpdateIssue) -> None:
    if update.milestone is not None:
        github.update_issue(
            update.issue, title=update.title, body=update.body, milestone=update.milestone
        )
    else:
        github.update_issue(update.issue, title=update.title, body=update.body)


Attempt = Callable[[str, Callable[[], object]], bool]


def _apply_labels(github: GitHubClient, changes: Sequence[LabelChange], attempt: Attempt) -> None:
    for change in changes:
        if change.add:
            attempt(
                f"issue #{change.issue} ({change.task_id}): adding labels failed",
                partial(github.add_labels, change.issue, list(change.add)),
            )
        for label in change.remove:
            attempt(
                f"issue #{change.issue} ({change.task_id}): removing {label} failed",
                partial(github.remove_label, change.issue, label),
            )


def _post_comments(
    github: GitHubClient, posts: Sequence[PostComment], attempt: Attempt
) -> set[int]:
    """Post each comment unless it is already there; return the issues whose post failed.

    The comments are read again right before posting, which narrows (but cannot close) the
    window in which two concurrent applies post the same comment.
    """
    failed: set[int] = set()
    for post in posts:
        what = f"issue #{post.issue} ({post.task_id}): posting the {post.kind} comment failed"
        try:
            if _has_comment(github.list_comments(post.issue), post.needle):
                continue
        except GitHubError as exc:
            attempt(what, partial(_raise, exc))
            failed.add(post.issue)
            continue
        if not attempt(what, partial(github.create_comment, post.issue, post.body)):
            failed.add(post.issue)
    return failed


def _raise(exc: GitHubError) -> None:
    raise exc


def _execute_closures(
    ctx: RefreshContext,
    plan: ApplyPlan,
    state: ApplyState,
    closes: Sequence[CloseIssue],
    labels: Mapping[int, LabelChange],
    posts: Mapping[int, PostComment],
    report: ExecutionReport,
    attempt: Attempt,
) -> None:
    """Close each issue as not planned, after its label change and comment (ADR 0013).

    Right before, the issue is read again (skipped when already closed or when its block
    changed) and its task branch is re-checked on the remote: a task claimed or handed off
    during the run is not closed. A removed task then gets the "left open" warning comment
    instead; a duplicate is left open with a warning. An issue whose comment failed is not
    closed, so a re-run posts the comment first.
    """
    github = ctx.github
    for close in closes:
        number = close.issue
        what = f"issue #{number} ({close.task_id})"
        try:
            fresh = github.get_issue(number)
        except GitHubError as exc:
            report.failures.append(f"{what}: reading the issue failed: {exc.message}; re-run")
            continue
        if fresh.state == "closed":
            report.notes.append(f"{what}: closed during the run; not modified")
            continue
        try:
            current = parse_body(fresh.body, issue_number=number, milestone=fresh.milestone)
        except TaskBodyError:
            current = None
        if current is None or current.id != close.task_id:
            report.failures.append(
                f"{what}: the issue's Forge block changed during the run; not closed (re-run "
                "forge plan apply)"
            )
            continue
        if close.reason == "duplicate" and not _winner_holds(github, close, report):
            continue
        name = TaskMetadata.branch_name(number)
        default_branch = ctx.config.project.default_branch
        try:
            seen = ctx.git.remote_branch_sha(name)
            branch = live_branch(ctx.git, default_branch, number, state.branches.get(number))
        except GitError as exc:
            report.failures.append(f"{what}: could not re-check its task branch: {exc.message}")
            continue
        started = None if branch is None else _branch_started(number, close.task_id, branch)
        if started is not None:
            _keep(github, plan, close, fresh, started, branch, report, attempt)
            continue
        change = labels.get(number)
        if change is not None:
            _apply_labels(github, [change], attempt)
        post = posts.get(number)
        if post is not None and _post_comments(github, [post], attempt):
            continue
        # The labels and the comment took a few calls: a claim may have landed meanwhile.
        try:
            moved = ctx.git.remote_branch_sha(name) != seen
            if moved:
                branch = live_branch(ctx.git, default_branch, number, None)
        except GitError as exc:
            report.failures.append(f"{what}: could not re-check its task branch: {exc.message}")
            continue
        if moved:
            started = None if branch is None else _branch_started(number, close.task_id, branch)
            if started is not None:
                _keep(github, plan, close, fresh, started, branch, report, attempt)
            else:
                report.notes.append(
                    f"{what}: its task branch moved during the run; not closed (re-run forge "
                    "plan apply)"
                )
            continue
        closed = attempt(
            f"{what}: closing failed",
            partial(github.update_issue, number, state="closed", state_reason="not_planned"),
        )
        if closed and close.duplicate_of is not None:
            report.closed_duplicates[number] = close.duplicate_of


def _winner_holds(github: GitHubClient, close: CloseIssue, report: ExecutionReport) -> bool:
    """True when the duplicate's winner still carries the same task id (read right now)."""
    assert close.duplicate_of is not None
    what = f"issue #{close.issue} ({close.task_id})"
    try:
        winner = github.get_issue(close.duplicate_of)
        meta = parse_body(winner.body, issue_number=winner.number, milestone=winner.milestone)
    except GitHubError as exc:
        report.failures.append(f"{what}: reading #{close.duplicate_of} failed: {exc.message}")
        return False
    except TaskBodyError:
        meta = None
    if meta is None or meta.id != close.task_id:
        report.notes.append(
            f"{what}: #{close.duplicate_of} no longer carries {close.task_id}; the duplicate "
            "was not closed (re-run forge plan apply)"
        )
        return False
    return True


def _keep(
    github: GitHubClient,
    plan: ApplyPlan,
    close: CloseIssue,
    fresh: Issue,
    started: Status,
    branch: TaskBranch | None,
    report: ExecutionReport,
    attempt: Attempt,
) -> None:
    """Leave an issue open that was started during the run, with the warning it needs."""
    number = close.issue
    report.kept.append(number)
    if close.reason == "removed":
        branches = {} if branch is None else {number: branch}
        key, body = removed_warning(
            fresh, close.task_id, plan.milestone, plan.file, started, branches
        )
        warning = PostComment(number, close.task_id, "plan_warning", body, warning_key_line(key))
        _post_comments(github, [warning], attempt)
        return
    report.notes.append(
        f"issue #{number} ({close.task_id}): started during the run ({started.value}); left "
        f"open although it duplicates #{close.duplicate_of} (fix: finish the work on one issue "
        "and close the other)"
    )


def _dedupe_after_create(
    ctx: RefreshContext,
    plan: ApplyPlan,
    state: ApplyState,
    report: ExecutionReport,
    attempt: Attempt,
) -> None:
    """List the issues again and close the duplicates a concurrent apply created (ADR 0013)."""
    github = ctx.github
    try:
        issues = github.list_issues(state="all", labels=[FORGE_TASK_LABEL])
    except GitHubError as exc:
        report.failures.append(
            f"listing issues after creating them failed: {exc.message}; re-run forge plan "
            "apply to close any duplicate"
        )
        return
    ignored: list[str] = []
    records = parse_records(issues, ignored)
    ids = set(report.created)
    candidates = plan_duplicates(records, ids, state.branches, state.open_pr_heads, {})
    comments: dict[int, tuple[Comment, ...]] = {}
    for number in sorted(candidates.comment_issues):
        try:
            comments[number] = tuple(github.list_comments(number))
        except GitHubError as exc:
            report.failures.append(
                f"issue #{number}: reading comments failed: {exc.message}; re-run forge plan apply"
            )
            return
    dups = plan_duplicates(records, ids, state.branches, state.open_pr_heads, comments)
    report.duplicates.extend(dups.outcomes)
    report.dedupe_warnings.extend(dups.warnings)
    _execute_closures(
        ctx,
        plan,
        state,
        dups.closes,
        {c.issue: c for c in dups.label_changes},
        {c.issue: c for c in dups.comments},
        report,
        attempt,
    )


# --------------------------------------------------------------------------- run


def plan_apply(
    ctx: RefreshContext,
    validation: PlanValidation,
    *,
    dry_run: bool = False,
) -> Result:
    """Validate, gather, compute and (unless ``dry_run``) execute the apply (ADR 0013).

    Validation errors give exit 1 and nothing is read from or written to GitHub. Validation
    warnings are repeated as warning messages. A GitHub failure on an issue gives exit 1
    after the other issues were applied. ``data`` is :meth:`ApplyPlan.to_data` plus
    ``dry_run``, ``applied`` and, after execution, ``execution``.
    """
    result = Result()
    for issue in validation.warnings:
        result.warn(f"plan: {issue.message}", issue.hint)
    if not validation.ok or validation.plan is None:
        result.code = ExitCode.ERROR
        result.data = {
            "milestone": validation.milestone,
            "dry_run": dry_run,
            "applied": False,
            "validation": validation.to_data(),
        }
        for issue in validation.errors:
            where = ", ".join(x for x in (issue.file, issue.task, issue.field) if x)
            result.error(f"{where}: {issue.message}" if where else issue.message, issue.hint)
        result.info("plan apply: validation failed; nothing was written")
        return result

    entry = None if validation.roadmap is None else validation.roadmap.find(validation.milestone)
    description = milestone_description(entry)
    state = gather_apply_state(ctx, validation.plan)
    apply_plan, state = plan_with_comments(
        ctx.github, validation.plan, state, milestone_description=description
    )
    data = apply_plan.to_data()
    data["dry_run"] = dry_run
    data["applied"] = False
    result.data = data
    for warning in apply_plan.warnings:
        result.warn(warning)

    n = apply_plan.write_count
    if dry_run:
        result.info(
            f"plan apply {apply_plan.milestone} (dry run): {n} GitHub write"
            f"{'s' if n != 1 else ''} planned; nothing was written"
        )
        return result
    if n == 0:
        data["applied"] = True
        data["execution"] = ExecutionReport().to_data()
        result.info(f"plan apply {apply_plan.milestone}: already up to date")
        return result

    report = execute_apply_plan(ctx, apply_plan, state)
    data["applied"] = True
    data["execution"] = report.to_data()
    for task in data["tasks"]:
        if task["action"] == "create":
            task["issue"] = report.issue_of(task["id"])
            task["created_issue"] = report.created.get(task["id"])
    for warning in [*report.dedupe_warnings, *report.notes]:
        result.warn(warning)
    for number in report.downgraded:
        result.warn(
            f"issue #{number} was claimed during the run: only its title, description and "
            "done_when were updated, and a warning comment lists the other changes"
        )
    for failure in report.failures:
        result.warn(failure)
    summary = (
        f"plan apply {apply_plan.milestone}: {report.writes} GitHub write"
        f"{'s' if report.writes != 1 else ''}"
    )
    if report.failures:
        result.code = ExitCode.ERROR
        summary += f", {len(report.failures)} failed"
    result.info(summary)
    return result
