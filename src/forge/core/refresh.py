"""`forge refresh`: recompute task status labels from git and GitHub (spec §7.2, ADR 0008).

The work is split in three steps so that the decision logic stays pure and testable:

1. gather: :func:`gather_branch_states` reads the remote task branches through :class:`Git`;
   :func:`run_refresh` lists issues and open pull requests through :class:`GitHubClient`;
2. plan: :func:`plan_refresh` derives every task's status and the label diff (pure);
3. apply: :func:`apply_refresh` creates missing catalogue labels and applies the diffs.

Running refresh twice in a row changes nothing the second time.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Any, Protocol

from forge.core.config import ForgeConfig
from forge.core.git import Git
from forge.core.github import GitHubClient, Issue
from forge.core.output import Result
from forge.core.tasks import (
    FORGE_TASK_LABEL,
    LABELS,
    BranchState,
    Status,
    TaskBodyError,
    TaskMetadata,
    derive_status,
    desired_labels,
    last_event_from_trailers,
    parse_body,
)

_TASK_BRANCH_RE = re.compile(r"[1-9][0-9]*")


@dataclass(frozen=True)
class TaskRecord:
    """An issue together with its parsed metadata block."""

    issue: Issue
    meta: TaskMetadata


@dataclass(frozen=True)
class RefreshChange:
    """A label change on one task issue. ``None`` statuses mean "no status label"."""

    issue: int
    task_id: str
    from_status: Status | None
    to_status: Status | None
    added: tuple[str, ...]
    removed: tuple[str, ...]

    def to_data(self) -> dict[str, Any]:
        """ADR 0008 shape: ``{"issue", "id", "from", "to", "added", "removed"}``."""
        return {
            "issue": self.issue,
            "id": self.task_id,
            "from": None if self.from_status is None else self.from_status.value,
            "to": None if self.to_status is None else self.to_status.value,
            "added": list(self.added),
            "removed": list(self.removed),
        }


@dataclass(frozen=True)
class RefreshPlan:
    """What a refresh would change, plus warnings and summary counts."""

    changes: tuple[RefreshChange, ...]
    warnings: tuple[str, ...]
    counts: Mapping[str, Any]

    def to_data(self) -> dict[str, Any]:
        """ADR 0008 ``data``: ``{"changes": [...], "warnings": [...], "counts": {...}}``."""
        return {
            "changes": [c.to_data() for c in self.changes],
            "warnings": list(self.warnings),
            "counts": dict(self.counts),
        }


# --------------------------------------------------------------------------- gather


def gather_branch_states(
    git: Git, default_branch: str, remote: str = "origin"
) -> dict[int, BranchState]:
    """Return the state of every remote ``task/<n>`` branch, keyed by issue number.

    Reads the remote-tracking refs ``refs/remotes/<remote>/task/<n>``; the caller must have
    fetched first. The last Forge event of each branch is looked up among the commits between
    its merge-base with ``<remote>/<default_branch>`` and its tip (ADR 0008). Branches whose
    name is not ``task/<positive int>`` are ignored.
    """
    prefix = f"refs/remotes/{remote}/task/"
    base_ref = f"refs/remotes/{remote}/{default_branch}"
    base = git.rev_parse(base_ref)
    states: dict[int, BranchState] = {}
    for ref, tip in sorted(git.list_refs(prefix.rstrip("/")).items()):
        if not ref.startswith(prefix):
            continue
        name = ref[len(prefix) :]
        if _TASK_BRANCH_RE.fullmatch(name) is None:
            continue
        range_ = tip if base is None else f"{git.merge_base(base, tip)}..{tip}"
        commits = git.commits(range_)
        event = last_event_from_trailers([c.trailers for c in commits])
        states[int(name)] = BranchState.from_event(tip, event)
    return states


# --------------------------------------------------------------------------- plan


def _parse_records(issues: Sequence[Issue], warnings: list[str]) -> list[TaskRecord]:
    records: list[TaskRecord] = []
    for issue in sorted(issues, key=lambda i: i.number):
        try:
            meta = parse_body(issue.body, issue_number=issue.number, milestone=issue.milestone)
        except TaskBodyError as exc:
            hint = f" (fix: {exc.hint})" if exc.hint else ""
            warnings.append(f"{exc.message}{hint}; issue skipped")
            continue
        if meta is None:
            warnings.append(
                f"issue #{issue.number}: labelled {FORGE_TASK_LABEL} but has no Forge block "
                "(fix: add the block or remove the label); issue skipped"
            )
            continue
        records.append(TaskRecord(issue, meta))
    return records


def plan_refresh(
    issues: Sequence[Issue],
    branch_states: Mapping[int, BranchState],
    open_pr_heads: Set[str],
) -> RefreshPlan:
    """Compute the label changes that align every task issue with its derived status.

    Pure. ``issues`` are the ``forge:task`` issues, open and closed. Issues with a missing
    or invalid block become warnings and are left alone. Dependencies are resolved by task
    id over all parsed issues; when two issues share an id, the lowest issue number wins and
    a warning is emitted. Only issues whose labels actually change appear in ``changes``.
    """
    warnings: list[str] = []
    records = _parse_records(issues, warnings)

    owner: dict[str, int] = {}
    deps_closed: dict[str, bool] = {}
    for rec in records:
        first = owner.get(rec.meta.id)
        if first is not None:
            warnings.append(
                f"issue #{rec.issue.number}: duplicate task id {rec.meta.id} (also on issue "
                f"#{first}); dependencies on {rec.meta.id} use issue #{first}"
            )
            continue
        owner[rec.meta.id] = rec.issue.number
        deps_closed[rec.meta.id] = rec.issue.state == "closed"

    changes: list[RefreshChange] = []
    statuses: Counter[str] = Counter()
    for rec in records:
        number = rec.issue.number
        derived = derive_status(
            issue=rec.issue,
            meta=rec.meta,
            deps_closed=deps_closed,
            branch=branch_states.get(number, BranchState.absent()),
            has_open_pr=TaskMetadata.branch_name(number) in open_pr_heads,
        )
        warnings.extend(derived.warnings)
        statuses["none" if derived.status is None else derived.status.value] += 1
        diff = desired_labels(rec.issue.labels, derived.status, rec.meta)
        if diff.empty:
            continue
        changes.append(
            RefreshChange(
                issue=number,
                task_id=rec.meta.id,
                from_status=Status.from_labels(rec.issue.labels),
                to_status=derived.status,
                added=tuple(sorted(diff.add)),
                removed=tuple(sorted(diff.remove)),
            )
        )

    counts: dict[str, Any] = {
        "issues": len(issues),
        "tasks": len(records),
        "skipped": len(issues) - len(records),
        "changed": len(changes),
        "warnings": len(warnings),
        "statuses": dict(sorted(statuses.items())),
    }
    return RefreshPlan(tuple(changes), tuple(warnings), counts)


# --------------------------------------------------------------------------- apply


def ensure_labels(github: GitHubClient) -> list[str]:
    """Create every catalogue label (``tasks.LABELS``) missing from the repository.

    Existing labels, including user labels, are never changed or deleted. Names are compared
    case-insensitively, like GitHub does. Returns the names of the labels created.
    """
    existing = {label.name.lower() for label in github.list_labels()}
    created: list[str] = []
    for spec in LABELS:
        if spec.name.lower() not in existing:
            github.create_label(spec.name, spec.color, spec.description)
            created.append(spec.name)
    return created


def apply_refresh(github: GitHubClient, plan: RefreshPlan) -> None:
    """Apply ``plan``: ensure the catalogue labels exist, then add and remove labels."""
    if not plan.changes:
        return
    ensure_labels(github)
    for change in plan.changes:
        if change.added:
            github.add_labels(change.issue, list(change.added))
        for label in change.removed:
            github.remove_label(change.issue, label)


# --------------------------------------------------------------------------- run


class RefreshContext(Protocol):
    """What :func:`run_refresh` needs; ``forge.commands._context.AppContext`` satisfies it."""

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


def run_refresh(ctx: RefreshContext) -> Result:
    """Fetch, gather branch and GitHub state, plan and apply the label changes.

    ``data`` is :meth:`RefreshPlan.to_data`; every warning is repeated as a warning message.
    """
    ctx.git.fetch()
    states = gather_branch_states(ctx.git, ctx.config.project.default_branch)
    issues = ctx.github.list_issues(state="all", labels=[FORGE_TASK_LABEL])
    heads = {pr.head for pr in ctx.github.list_pulls(state="open")}
    plan = plan_refresh(issues, states, heads)
    apply_refresh(ctx.github, plan)
    result = Result(data=plan.to_data())
    for warning in plan.warnings:
        result.warn(warning)
    n = len(plan.changes)
    result.info(f"refresh: {n} task{'s' if n != 1 else ''} updated")
    return result
