"""`forge refresh`: recompute task status labels from git and GitHub (spec §7.2, ADR 0008).

The work is split in three steps so that the decision logic stays pure and testable:

1. gather: :func:`run_refresh` lists issues and open pull requests through
   :class:`GitHubClient` **first**, then fetches and reads the remote task branches through
   :class:`Git` (:func:`fetch_task_refs`, :func:`gather_task_branches`). A claim that lands
   between the two reads is seen on the branch, and the label diff is computed against the
   earlier issue snapshot, so a concurrent claim label is never removed;
2. plan: :func:`plan_refresh` resolves each branch's last Forge event for its task id and
   derives every task's status and label diff (pure);
3. apply: :func:`apply_refresh` creates missing catalogue labels and applies the diffs, issue
   by issue; a failed issue is reported and the others still proceed.

Running refresh twice in a row changes nothing the second time.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Any, Protocol

from forge.core.config import ForgeConfig
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.git import CommitInfo, Git, GitError
from forge.core.github import GitHubClient, GitHubError, Issue
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
    has_label,
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

    def to_data(self, *, applied: bool = True) -> dict[str, Any]:
        """ADR 0008 shape: ``{"issue", "id", "from", "to", "added", "removed", "applied"}``.

        ``applied`` is false when applying the change failed.
        """
        return {
            "issue": self.issue,
            "id": self.task_id,
            "from": None if self.from_status is None else self.from_status.value,
            "to": None if self.to_status is None else self.to_status.value,
            "added": list(self.added),
            "removed": list(self.removed),
            "applied": applied,
        }


@dataclass(frozen=True)
class RefreshPlan:
    """What a refresh would change, plus warnings and summary counts."""

    changes: tuple[RefreshChange, ...]
    warnings: tuple[str, ...]
    counts: Mapping[str, Any]

    def to_data(self, failed: Set[int] = frozenset()) -> dict[str, Any]:
        """ADR 0008 ``data``: ``{"changes": [...], "warnings": [...], "counts": {...}}``.

        ``failed`` holds the issue numbers whose changes could not be applied: they are
        marked ``"applied": false``, and ``counts`` reports ``changed`` (applied changes
        only) and ``failed`` separately.
        """
        counts = dict(self.counts)
        n_failed = sum(1 for c in self.changes if c.issue in failed)
        counts["changed"] = len(self.changes) - n_failed
        counts["failed"] = n_failed
        return {
            "changes": [c.to_data(applied=c.issue not in failed) for c in self.changes],
            "warnings": list(self.warnings),
            "counts": counts,
        }


# --------------------------------------------------------------------------- gather


@dataclass(frozen=True)
class TaskBranch:
    """A remote ``task/<n>`` branch: its tip and the trailers of its own commits.

    ``trailers`` holds one mapping per commit between the merge-base with the default branch
    and the tip, following first parents only, newest first. Which event counts depends on
    the task id, known only once the issue is parsed (see :meth:`state`). ``commits`` holds
    the same commits with their sha and committer date, when gathered from git (claim and
    lease logic needs them; the pure refresh plan does not).
    """

    tip: str
    trailers: tuple[Mapping[str, str], ...] = ()
    commits: tuple[CommitInfo, ...] = ()

    def state(self, task_id: str) -> BranchState:
        """Return the branch state, counting only events whose ``Forge-Task`` is ``task_id``."""
        return BranchState.from_event(
            self.tip, last_event_from_trailers(self.trailers, task_id=task_id)
        )


@dataclass(frozen=True)
class BranchScan:
    """Result of :func:`gather_task_branches`: branches by issue number, plus warnings."""

    branches: Mapping[int, TaskBranch]
    warnings: tuple[str, ...] = ()


def _config_hint(default_branch: str, remote: str) -> str:
    return (
        f"set project.default_branch in .forge/config.yml to the default branch that exists "
        f"on {remote} (currently {default_branch!r})"
    )


def check_full_history(git: Git, command: str = "refresh") -> None:
    """Raise :class:`PreconditionError` (exit 5) when ``git`` is a shallow clone.

    Merge-bases between task branches and the default branch need the full history.
    ``command`` names what needs it in the message (``refresh``, ``claims``...).
    """
    if git.is_shallow():
        raise PreconditionError(
            f"the repository is a shallow clone; {command} needs full history to find merge-bases",
            hint="run `git fetch --unshallow`; in GitHub Actions use actions/checkout with "
            "`fetch-depth: 0`",
        )


def fetch_task_refs(git: Git, default_branch: str, remote: str = "origin") -> None:
    """Fetch the default branch and every ``task/*`` branch from ``remote``, with prune.

    The refspecs are explicit, so single-branch clones (whose configured refspec covers one
    branch) still see every task branch. A failure raises :class:`PreconditionError` when the
    default branch is missing on the remote, otherwise :class:`ForgeError` with a hint.
    """
    refspecs = [
        f"+refs/heads/{default_branch}:refs/remotes/{remote}/{default_branch}",
        f"+refs/heads/task/*:refs/remotes/{remote}/task/*",
    ]
    try:
        git.fetch(remote, prune=True, refspecs=refspecs)
    except GitError as exc:
        head = f"refs/heads/{default_branch}"
        try:
            on_remote: bool | None = head in git.ls_remote(remote, head)
        except GitError:
            on_remote = None
        if on_remote is False:
            raise PreconditionError(
                f"branch {default_branch!r} does not exist on {remote}",
                hint=_config_hint(default_branch, remote),
            ) from exc
        detail = exc.stderr.strip().splitlines()[-1] if exc.stderr.strip() else str(exc)
        raise ForgeError(
            f"could not fetch from {remote}: {detail}",
            hint=f"check that the git remote {remote!r} exists and is reachable (network, "
            "credentials), then re-run forge refresh",
        ) from exc


def gather_task_branches(git: Git, default_branch: str, remote: str = "origin") -> BranchScan:
    """Read every remote ``task/<n>`` branch, keyed by issue number.

    Reads the remote-tracking refs ``refs/remotes/<remote>/task/<n>``; the caller must have
    fetched first (:func:`fetch_task_refs`) and checked :func:`check_full_history`. For each
    branch the commits between its merge-base with ``<remote>/<default_branch>`` and its tip
    are listed with ``--first-parent`` (ADR 0008). Branches whose name is not
    ``task/<positive int>`` are ignored. A branch without a merge-base becomes a warning and
    is treated as absent. A missing ``<remote>/<default_branch>`` raises
    :class:`PreconditionError` naming ``project.default_branch``.
    """
    prefix = f"refs/remotes/{remote}/task/"
    base = git.rev_parse(f"refs/remotes/{remote}/{default_branch}")
    if base is None:
        raise PreconditionError(
            f"{remote}/{default_branch} not found; task branches cannot be compared with it",
            hint=_config_hint(default_branch, remote),
        )
    branches: dict[int, TaskBranch] = {}
    warnings: list[str] = []
    for ref, tip in sorted(git.list_refs(prefix.rstrip("/")).items()):
        if not ref.startswith(prefix):
            continue
        name = ref[len(prefix) :]
        if _TASK_BRANCH_RE.fullmatch(name) is None:
            continue
        try:
            merge_base = git.merge_base(base, tip)
        except GitError:
            warnings.append(
                f"branch task/{name}: no common history with {remote}/{default_branch}; "
                "ignored (treated as absent)"
            )
            continue
        commits = git.commits(f"{merge_base}..{tip}", first_parent=True)
        branches[int(name)] = TaskBranch(tip, tuple(c.trailers for c in commits), tuple(commits))
    return BranchScan(branches, tuple(warnings))


# --------------------------------------------------------------------------- plan


def parse_records(issues: Sequence[Issue], warnings: list[str]) -> list[TaskRecord]:
    """Parse the Forge block of each issue (sorted by number) into :class:`TaskRecord`.

    Issues whose block is invalid or missing are skipped, with a warning appended to
    ``warnings``. A record closed as ``not_planned`` whose task id is also carried by another
    issue that is not closed as ``not_planned`` is a superseded duplicate (for example one
    `forge plan apply` closed): it is dropped silently, so it neither counts as a dropped task
    nor triggers duplicate-id warnings.
    """
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
    live = {r.meta.id for r in records if not _closed_not_planned(r.issue)}
    return [r for r in records if not (_closed_not_planned(r.issue) and r.meta.id in live)]


def _closed_not_planned(issue: Issue) -> bool:
    return issue.state == "closed" and issue.state_reason == "not_planned"


def _not_planned_warnings(rec: TaskRecord, not_planned: Set[str]) -> list[str]:
    """Warn an open, non-proposed task about dependencies closed as ``not_planned``."""
    if rec.issue.state == "closed" or has_label(rec.issue.labels, Status.PROPOSED.label):
        return []
    return [
        f"issue #{rec.issue.number} ({rec.meta.id}): dependency {dep} was closed as not "
        "planned; it still counts as closed (fix: drop it from depends_on, or reopen it if "
        "the work is still needed)"
        for dep in rec.meta.depends_on
        if dep in not_planned
    ]


@dataclass(frozen=True)
class DerivedTask:
    """A parsed task issue and its derived status (``None`` = no status label)."""

    record: TaskRecord
    status: Status | None


def derive_task_statuses(
    issues: Sequence[Issue],
    branches: Mapping[int, TaskBranch],
    open_pr_heads: Set[str],
    warnings: list[str],
) -> list[DerivedTask]:
    """Derive the status of every parseable task issue, in issue-number order (pure).

    This is the decision core of :func:`plan_refresh`, shared with ``forge plan apply``.
    Warnings (unparseable issues, duplicate ids, unknown or not-planned dependencies) are
    appended to ``warnings``. Dependencies are resolved by task id over all parsed issues;
    when two issues share an id, the lowest issue number wins.
    """
    records = parse_records(issues, warnings)

    owner: dict[str, int] = {}
    deps_closed: dict[str, bool] = {}
    not_planned: set[str] = set()
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
        if rec.issue.state == "closed" and rec.issue.state_reason == "not_planned":
            not_planned.add(rec.meta.id)

    derived_tasks: list[DerivedTask] = []
    for rec in records:
        number = rec.issue.number
        branch = branches.get(number)
        derived = derive_status(
            issue=rec.issue,
            meta=rec.meta,
            deps_closed=deps_closed,
            branch=BranchState.absent() if branch is None else branch.state(rec.meta.id),
            has_open_pr=TaskMetadata.branch_name(number) in open_pr_heads,
        )
        warnings.extend(derived.warnings)
        warnings.extend(_not_planned_warnings(rec, not_planned))
        derived_tasks.append(DerivedTask(rec, derived.status))
    return derived_tasks


def plan_refresh(
    issues: Sequence[Issue],
    branches: Mapping[int, TaskBranch],
    open_pr_heads: Set[str],
    *,
    warnings: Sequence[str] = (),
) -> RefreshPlan:
    """Compute the label changes that align every task issue with its derived status.

    Pure. ``issues`` are the ``forge:task`` issues, open and closed. ``branches`` are the
    remote task branches by issue number; a branch's last event only counts when its
    ``Forge-Task`` trailer equals the issue's task id. ``warnings`` (e.g. from gathering)
    are reported first. Issues with a missing or invalid block become warnings and are left
    alone. Dependencies are resolved by task id over all parsed issues; when two issues share
    an id, the lowest issue number wins and a warning is emitted. A dependency closed as
    ``not_planned`` still counts as closed (spec §7.2), with a warning on each open dependent.
    Only issues whose labels actually change appear in ``changes``.
    """
    warnings = list(warnings)
    derived_tasks = derive_task_statuses(issues, branches, open_pr_heads, warnings)

    changes: list[RefreshChange] = []
    statuses: Counter[str] = Counter()
    for task in derived_tasks:
        rec, status = task.record, task.status
        statuses["none" if status is None else status.value] += 1
        diff = desired_labels(rec.issue.labels, status, rec.meta)
        if diff.empty:
            continue
        changes.append(
            RefreshChange(
                issue=rec.issue.number,
                task_id=rec.meta.id,
                from_status=Status.from_labels(rec.issue.labels),
                to_status=status,
                added=tuple(sorted(diff.add)),
                removed=tuple(sorted(diff.remove)),
            )
        )

    counts: dict[str, Any] = {
        "issues": len(issues),
        "tasks": len(derived_tasks),
        "skipped": len(issues) - len(derived_tasks),
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


def apply_refresh(github: GitHubClient, plan: RefreshPlan) -> dict[int, str]:
    """Apply ``plan``: ensure the catalogue labels exist, then add and remove labels.

    Each issue is applied independently: a :class:`GitHubError` on one issue is recorded and
    the remaining issues still proceed. Returns a warning per failed issue, keyed by issue
    number (empty when all succeeded). A failure while creating catalogue labels propagates,
    before any issue changes. Re-running refresh converges, because the next plan starts
    from the labels actually present.
    """
    if not plan.changes:
        return {}
    ensure_labels(github)
    failures: dict[int, str] = {}
    for change in plan.changes:
        try:
            if change.added:
                github.add_labels(change.issue, list(change.added))
            for label in change.removed:
                github.remove_label(change.issue, label)
        except GitHubError as exc:
            failures[change.issue] = (
                f"issue #{change.issue} ({change.task_id}): label update failed: "
                f"{exc.message}; re-run forge refresh"
            )
    return failures


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
    """Read GitHub, then fetch and read git; plan and apply the label changes.

    Order matters for concurrent claims: issues and pull requests are listed **before** the
    fetch (see the module docstring). ``data`` is :meth:`RefreshPlan.to_data` with the failed
    issues marked; every warning, including per-issue apply failures, is
    repeated as a warning message. The exit code is 1 when any issue failed to update.
    """
    git = ctx.git
    default_branch = ctx.config.project.default_branch
    check_full_history(git)
    issues = ctx.github.list_issues(state="all", labels=[FORGE_TASK_LABEL])
    heads = {pr.head for pr in ctx.github.list_pulls(state="open")}
    fetch_task_refs(git, default_branch)
    scan = gather_task_branches(git, default_branch)
    plan = plan_refresh(issues, scan.branches, heads, warnings=scan.warnings)
    failures = apply_refresh(ctx.github, plan)

    data = plan.to_data(failed=failures.keys())
    data["warnings"].extend(failures.values())
    data["counts"]["warnings"] += len(failures)
    result = Result(data=data)
    for warning in data["warnings"]:
        result.warn(warning)
    n = data["counts"]["changed"]
    summary = f"refresh: {n} task{'s' if n != 1 else ''} updated"
    if failures:
        result.code = ExitCode.ERROR
        summary += f", {len(failures)} failed"
    result.info(summary)
    return result
