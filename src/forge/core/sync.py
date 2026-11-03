"""``forge sync``: rebase the current task onto the default branch (spec §10, ADR 0011).

Flow of :func:`sync`:

1. **preconditions** (nothing changes on failure, exit 5): no ``sync.json`` and no rebase in
   progress, a current task checked out on ``task/<n>``, the task's zones known to
   ``zones.yml``, my own live and complete claim (:func:`forge.core.lease.require_own_claim`
   fetches the default branch and the task branches), a full clone, and a local branch that
   contains ``origin/task/<n>`` (otherwise the forced push would drop remote commits);
2. **analysis** (read-only, :func:`classify`): upstream commits since the merge base, files
   they share with the task (its commits plus the uncommitted changes), the categories and
   the upstream context (commits → PRs → issues). Every GitHub read happens here, before
   anything changes, so a GitHub failure leaves the worktree untouched;
3. **preflight**: a dirty tree is committed as ``forge: wip`` (user hooks run; a rejection is
   exit 2 and the changes are unstaged again), a local backup ref
   ``refs/forge/backup/task-<n>/<UTC timestamp>`` is saved at HEAD and ``sync.json`` is
   written (state ``running``) right away, so an interruption at any later point is
   recoverable with ``--continue`` or ``--abort``; only the newest :data:`MAX_BACKUPS` backup
   refs of the task are kept;
4. a branch already up to date finishes here (no checks, no push);
5. **rebase** with rerere, ``--keep-empty`` and ``--committer-date-is-author-date``; each stop
   goes through the resolvers, then the escalation check (exit 4) or a stop for the agent
   (exit 2);
6. **verify** (``config.checks``) then **push** with ``--force-with-lease`` against the remote
   tip seen at preflight; then the lease is rewritten for the rebased claim commit,
   ``blocked:conflict`` is removed, the sync is logged, a summary is posted when conflicts
   were resolved, ``current.json`` follows the new claim sha, and ``sync.json`` is removed.

Every step after the push is idempotent (the log entry is keyed by the backup ref, the
summary comment by a key line, the lease comment is edited in place), so ``--continue``
after an interruption converges. Core never prints: callers render :class:`SyncOutcome`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from forge.core.config import ForgeConfig, Resolver
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.escalation import (
    SYNC_FILE,
    SYNC_LOG_FILE,
    ConflictEscalation,
    UpstreamContext,
    digest,
    escalate_conflict,
    gather_upstream_context,
    key_line,
    post_once,
    read_json_file,
)
from forge.core.git import CommitFiles, Git, GitAddFailed, GitError, Lease, PushHookRejected
from forge.core.github import GitHubClient
from forge.core.lease import (
    LeaseData,
    OwnClaim,
    event_commit_index,
    find_lease,
    format_time,
    is_stale,
    lease_names_claim,
    parse_time,
    require_own_claim,
    run_of,
    stale_at,
    upsert_lease,
)
from forge.core.output import Level, Message, Result
from forge.core.profile import AgentProfile
from forge.core.refresh import parse_records
from forge.core.tasks import (
    BLOCKED_CONFLICT,
    FORGE_TASK_LABEL,
    BranchEvent,
    TaskMetadata,
    TaskType,
    has_label,
    last_event_from_trailers,
)
from forge.core.team import Team
from forge.core.workspace import (
    LOCAL_DIR,
    ensure_local_dir,
    find_current_task,
    read_current,
    write_current,
)
from forge.core.zones import UNKNOWN_ZONE_FIX, ZonesConfig, glob_matches, load_zones

SYNC_MARKER = "<!-- forge:sync -->"
"""First line of the sync summary comment (posted only when conflicts were resolved)."""

BACKUP_PREFIX = "refs/forge/backup"
WIP_MESSAGE = "forge: wip"
OUTPUT_TAIL_LINES = 40
OUTPUT_TAIL_CHARS = 4000
STATE_VERSION = 1
MAX_BACKUPS = 10
"""Backup refs kept per task (the one an in-progress ``sync.json`` names is never pruned)."""
ABORTED_SUFFIX = "-aborted"
"""Suffix of the backup ref that ``--abort`` saves when HEAD moved since the sync started."""

_CONTINUE_HINT = (
    "resolve the files following the /sync rules, `git add` them, then run "
    "`forge sync --continue` (or `forge sync --abort`)"
)


# --------------------------------------------------------------------------- shell runner


@dataclass(frozen=True)
class CommandRun:
    """Exit code and combined output of a shell command."""

    returncode: int
    output: str


ShellRunner = Callable[[str, Path], CommandRun]
"""Runs a shell command in a directory: checks and ``regenerate`` resolver commands."""


def run_shell(command: str, cwd: Path) -> CommandRun:
    """Run ``command`` with the system shell in ``cwd``; stdout and stderr are combined."""
    proc = subprocess.run(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        check=False,
    )
    return CommandRun(proc.returncode, proc.stdout)


def tail(text: str) -> str:
    """The last :data:`OUTPUT_TAIL_LINES` lines of ``text``, at most :data:`OUTPUT_TAIL_CHARS`."""
    lines = text.rstrip("\n").splitlines()[-OUTPUT_TAIL_LINES:]
    return "\n".join(lines)[-OUTPUT_TAIL_CHARS:]


# --------------------------------------------------------------------------- context


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SyncContext:
    """Everything sync needs. Satisfies ``lease.LeaseContext``.

    ``git`` must run at the root of the task's worktree (or the main working tree when the
    task is checked out in place). ``zones`` is the validated ``.forge/zones.yml``
    (:func:`load_sync_zones` turns a problem into exit 5). ``team`` is optional (only
    ``forge escalate`` mentions leads). ``run_check`` runs ``config.checks`` and
    ``run_command`` the ``regenerate`` resolver commands; tests inject them.
    """

    config: ForgeConfig
    git: Git
    github: GitHubClient
    zones: ZonesConfig
    agent: AgentProfile
    profile_login: str
    team: Team | None = None
    clock: Callable[[], datetime] = _utc_now
    remote: str = "origin"
    run_check: ShellRunner = run_shell
    run_command: ShellRunner = run_shell

    def now(self) -> datetime:
        """Current time from :attr:`clock`, as an aware UTC datetime."""
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("the clock must return timezone-aware datetimes")
        return value.astimezone(UTC)


@dataclass(frozen=True)
class SyncOptions:
    """Options of a plain ``forge sync``."""

    force_resolve: bool = False


@dataclass
class AbortContext:
    """What ``forge sync --abort`` needs: git at the worktree root and a clock.

    ``--abort`` is a local restore. It needs no profile, no GitHub and no project files, so it
    works when ``.forge/config.yml`` or ``zones.yml`` hold conflict markers mid-rebase. The
    task is the one named by the worktree's ``sync.json``.
    """

    git: Git
    clock: Callable[[], datetime] = _utc_now

    def now(self) -> datetime:
        """Current time from :attr:`clock`, as an aware UTC datetime."""
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("the clock must return timezone-aware datetimes")
        return value.astimezone(UTC)


def load_sync_zones(root: Path) -> ZonesConfig:
    """Load ``.forge/zones.yml``; any problem is a :class:`PreconditionError` (exit 5)."""
    try:
        return load_zones(root)
    except ForgeError as exc:
        raise PreconditionError(exc.message, hint=exc.hint) from exc


# --------------------------------------------------------------------------- classification


class Category(StrEnum):
    """Conflict categories, in precedence order (ADR 0011, kickoff B6)."""

    OUT_OF_ZONE = "out_of_zone"
    SEMANTIC = "semantic"
    TEXTUAL = "textual"
    NONE = "none"


@dataclass(frozen=True)
class Classification:
    """What the analysis found. ``categories`` are all that apply, in precedence order."""

    primary: Category
    categories: tuple[Category, ...]
    shared: tuple[str, ...]  # I: upstream files ∩ task files
    out_of_zone: tuple[str, ...]  # files of I outside the task's zones
    semantic: Mapping[str, tuple[str, ...]]  # contract zone the task depends on -> upstream files
    upstream_files: int = 0
    task_files: int = 0

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "primary": self.primary.value,
            "categories": [c.value for c in self.categories],
            "shared": list(self.shared),
            "out_of_zone": list(self.out_of_zone),
            "semantic": {zone: list(files) for zone, files in self.semantic.items()},
            "upstream_files": self.upstream_files,
            "task_files": self.task_files,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> Classification:
        """Rebuild from :meth:`to_data` output."""
        return cls(
            primary=Category(data["primary"]),
            categories=tuple(Category(c) for c in data["categories"]),
            shared=tuple(data["shared"]),
            out_of_zone=tuple(data["out_of_zone"]),
            semantic={str(k): tuple(v) for k, v in dict(data["semantic"]).items()},
            upstream_files=int(data.get("upstream_files", 0)),
            task_files=int(data.get("task_files", 0)),
        )


def contract_dependencies(
    zones: ZonesConfig, task_zones: Iterable[str], dependency_zones: Iterable[str]
) -> list[str]:
    """The ``contract: true`` zones a task depends on: in its own zones or its deps' zones."""
    names = {*task_zones, *dependency_zones}
    return sorted(n for n in names if n in zones.zones and zones.zones[n].contract)


def classify(
    upstream_files: Iterable[str],
    task_files: Iterable[str],
    *,
    zones: ZonesConfig,
    task_zones: Sequence[str],
    dependency_zones: Iterable[str] = (),
) -> Classification:
    """Classify a sync (pure, ADR 0011).

    - ``textual``: the task and upstream changed a same file;
    - ``out_of_zone``: one of those files matches none of ``task_zones``;
    - ``semantic``: an upstream file lies in a contract zone the task depends on (its own
      zones, or ``dependency_zones``: the zones of the tasks in its ``depends_on``);
    - ``none``: none of the above.

    The primary category follows the precedence ``out_of_zone`` > ``semantic`` > ``textual``
    > ``none``. ``task_zones`` must all be known to ``zones``.
    """
    upstream = sorted(set(upstream_files))
    task = set(task_files)
    shared = tuple(sorted(set(upstream) & task))
    outside = tuple(zones.files_outside(shared, task_zones)) if shared else ()
    semantic: dict[str, tuple[str, ...]] = {}
    for name in contract_dependencies(zones, task_zones, dependency_zones):
        globs = zones.zones[name].paths
        hit = tuple(f for f in upstream if any(glob_matches(g, f) for g in globs))
        if hit:
            semantic[name] = hit
    found: list[Category] = []
    if outside:
        found.append(Category.OUT_OF_ZONE)
    if semantic:
        found.append(Category.SEMANTIC)
    if shared:
        found.append(Category.TEXTUAL)
    primary = found[0] if found else Category.NONE
    return Classification(
        primary=primary,
        categories=tuple(found) or (Category.NONE,),
        shared=shared,
        out_of_zone=outside,
        semantic=semantic,
        upstream_files=len(upstream),
        task_files=len(task),
    )


# --------------------------------------------------------------------------- state files


class SyncState(StrEnum):
    """``sync.json`` states. ADR 0011 names ``conflicts`` and ``checks_failed``; ``running``
    marks a sync interrupted before it stopped, ``push_rejected`` a pre-push hook refusal."""

    RUNNING = "running"
    CONFLICTS = "conflicts"
    CHECKS_FAILED = "checks_failed"
    PUSH_REJECTED = "push_rejected"


@dataclass
class ConflictEntry:
    """One conflicted path at one rebase stop and how it was resolved (``None``: not yet)."""

    path: str
    zones: tuple[str, ...]
    contract: bool
    outside: bool
    commit: str | None
    resolution: str | None = None
    detail: str | None = None

    def to_data(self) -> dict[str, Any]:
        """JSON form (``sync.json`` and ``sync-log.json``)."""
        return {
            "path": self.path,
            "zones": list(self.zones),
            "contract": self.contract,
            "outside": self.outside,
            "commit": self.commit,
            "resolution": self.resolution,
            "detail": self.detail,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ConflictEntry:
        """Rebuild from :meth:`to_data` output."""
        return cls(
            path=str(data["path"]),
            zones=tuple(data["zones"]),
            contract=bool(data["contract"]),
            outside=bool(data["outside"]),
            commit=data.get("commit"),
            resolution=data.get("resolution"),
            detail=data.get("detail"),
        )

    @property
    def why(self) -> str:
        """Why this path escalates: outside the zones and/or in a contract zone."""
        reasons = []
        if self.outside:
            reasons.append("outside the task's zones")
        if self.contract:
            reasons.append("contract zone " + ", ".join(self.zones))
        return "; ".join(reasons) or "in the task's zones"


@dataclass(frozen=True)
class CheckRun:
    """One ``config.checks`` entry and its result."""

    name: str
    command: str
    exit_code: int
    output: str

    @property
    def passed(self) -> bool:
        """True when the check exited 0."""
        return self.exit_code == 0

    def to_data(self) -> dict[str, Any]:
        """JSON form (``output`` is a tail)."""
        return {
            "name": self.name,
            "command": self.command,
            "exit_code": self.exit_code,
            "output": self.output,
        }


@dataclass
class SyncRecord:
    """The content of ``.forge/local/sync.json`` (ADR 0011)."""

    task: str
    issue: int
    branch: str
    login: str
    started_at: datetime
    backup_ref: str
    original_tip: str
    wip: bool
    remote_tip: str
    onto: str
    base: str
    claim: str
    claim_run: str | None
    force_resolve: bool
    classification: Classification
    upstream: UpstreamContext
    upstream_count: int
    state: SyncState = SyncState.RUNNING
    conflicts: list[ConflictEntry] = field(default_factory=list)
    checks: list[CheckRun] = field(default_factory=list)
    rebased: bool = False

    def to_data(self) -> dict[str, Any]:
        """JSON form written to ``sync.json``."""
        return {
            "version": STATE_VERSION,
            "task": self.task,
            "issue": self.issue,
            "branch": self.branch,
            "login": self.login,
            "started_at": format_time(self.started_at),
            "backup_ref": self.backup_ref,
            "original_tip": self.original_tip,
            "wip": self.wip,
            "remote_tip": self.remote_tip,
            "onto": self.onto,
            "base": self.base,
            "claim": self.claim,
            "claim_run": self.claim_run,
            "force_resolve": self.force_resolve,
            "classification": self.classification.to_data(),
            "upstream": self.upstream.to_data(),
            "upstream_count": self.upstream_count,
            "state": self.state.value,
            "conflicts": [c.to_data() for c in self.conflicts],
            "checks": [c.to_data() for c in self.checks],
            "rebased": self.rebased,
        }

    @classmethod
    def from_data(cls, data: Any, file: Path) -> SyncRecord:
        """Rebuild from ``sync.json``; anything malformed raises :class:`ForgeError`."""
        try:
            if not isinstance(data, Mapping) or data.get("version") != STATE_VERSION:
                raise ValueError("unsupported version")
            return cls(
                task=str(data["task"]),
                issue=int(data["issue"]),
                branch=str(data["branch"]),
                login=str(data["login"]),
                started_at=parse_time(str(data["started_at"])),
                backup_ref=str(data["backup_ref"]),
                original_tip=str(data["original_tip"]),
                wip=bool(data["wip"]),
                remote_tip=str(data["remote_tip"]),
                onto=str(data["onto"]),
                base=str(data["base"]),
                claim=str(data["claim"]),
                claim_run=data.get("claim_run"),
                force_resolve=bool(data["force_resolve"]),
                classification=Classification.from_data(data["classification"]),
                upstream=UpstreamContext.from_data(data["upstream"]),
                upstream_count=int(data["upstream_count"]),
                state=SyncState(data["state"]),
                conflicts=[ConflictEntry.from_data(c) for c in data["conflicts"]],
                checks=[
                    CheckRun(
                        str(c["name"]), str(c["command"]), int(c["exit_code"]), str(c["output"])
                    )
                    for c in data["checks"]
                ],
                rebased=bool(data.get("rebased", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ForgeError(
                f"{file}: unreadable sync state ({exc})",
                hint="restore the branch from the newest refs/forge/backup/ ref if needed "
                "(`git reset --hard <ref>`), then delete the file",
            ) from exc


def _write_json(path: Path, data: Any) -> None:
    ensure_local_dir(path.parent.parent.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    os.replace(tmp, path)


def sync_file(root: Path) -> Path:
    """``<root>/.forge/local/sync.json``."""
    return root / SYNC_FILE


def sync_log_file(root: Path) -> Path:
    """``<root>/.forge/local/sync-log.json``."""
    return root / SYNC_LOG_FILE


def read_sync(root: Path) -> SyncRecord | None:
    """The in-progress sync of the worktree at ``root`` (``None`` when there is none)."""
    path = sync_file(root)
    data = read_json_file(path)
    return None if data is None else SyncRecord.from_data(data, path)


def write_sync(root: Path, record: SyncRecord) -> None:
    """Atomically write ``sync.json``."""
    _write_json(sync_file(root), record.to_data())


def remove_sync(root: Path) -> None:
    """Remove ``sync.json`` (no error when absent)."""
    sync_file(root).unlink(missing_ok=True)


def read_sync_log(root: Path) -> list[dict[str, Any]]:
    """The entries of ``sync-log.json`` (oldest first; empty when missing)."""
    data = read_json_file(sync_log_file(root))
    if data is None:
        return []
    if not isinstance(data, list):
        raise ForgeError(
            f"{sync_log_file(root)}: expected a JSON list",
            hint="fix or delete the file (it is local Forge state)",
        )
    return [entry for entry in data if isinstance(entry, dict)]


def append_sync_log(root: Path, entry: Mapping[str, Any]) -> None:
    """Append ``entry``, replacing an earlier entry with the same ``id`` (idempotent)."""
    entries = [e for e in read_sync_log(root) if e.get("id") != entry["id"]]
    entries.append(dict(entry))
    _write_json(sync_log_file(root), entries)


# --------------------------------------------------------------------------- outcome


@dataclass
class SyncOutcome:
    """Result of :func:`sync`, :func:`sync_continue` and :func:`sync_abort`.

    ``outcome`` is one of ``pushed``, ``up_to_date``, ``conflicts``, ``checks_failed``,
    ``push_rejected``, ``lease_rejected``, ``wip_rejected``, ``escalated``, ``aborted``.
    ``saved_ref`` is set by ``--abort`` when it saved a HEAD that differed from the backup.
    """

    code: ExitCode
    outcome: str
    issue: int
    task_id: str
    branch: str
    head: str | None = None
    onto: str | None = None
    rebased: bool = False
    classification: Classification | None = None
    upstream: UpstreamContext | None = None
    conflicts: list[ConflictEntry] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    checks: list[CheckRun] = field(default_factory=list)
    escalation: ConflictEscalation | None = None
    lease: dict[str, Any] | None = None
    backup_ref: str | None = None
    saved_ref: str | None = None
    state_file: str | None = None
    output: str | None = None
    messages: list[Message] = field(default_factory=list)

    def info(self, text: str, hint: str | None = None) -> None:
        """Append an info message."""
        self.messages.append(Message(Level.INFO, text, hint))

    def warn(self, text: str, hint: str | None = None) -> None:
        """Append a warning."""
        self.messages.append(Message(Level.WARNING, text, hint))

    def error(self, text: str, hint: str | None = None) -> None:
        """Append an error message."""
        self.messages.append(Message(Level.ERROR, text, hint))

    @property
    def warnings(self) -> list[str]:
        """The texts of the warning messages."""
        return [m.text for m in self.messages if m.level is Level.WARNING]

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` for ``forge sync``."""
        return {
            "outcome": self.outcome,
            "issue": self.issue,
            "id": self.task_id,
            "branch": self.branch,
            "head": self.head,
            "onto": self.onto,
            "rebased": self.rebased,
            "classification": None
            if self.classification is None
            else self.classification.to_data(),
            "upstream": None if self.upstream is None else self.upstream.to_data(),
            "conflicts": [c.to_data() for c in self.conflicts],
            "unresolved": list(self.unresolved),
            "checks": [c.to_data() for c in self.checks],
            "escalation": None if self.escalation is None else self.escalation.to_data(),
            "lease": self.lease,
            "backup_ref": self.backup_ref,
            "saved_ref": self.saved_ref,
            "state_file": self.state_file,
            "output": self.output,
        }

    def to_result(self) -> Result:
        """The command result: code, data and messages."""
        return Result(code=self.code, data=self.to_data(), messages=list(self.messages))


def _outcome(record: SyncRecord, code: ExitCode, outcome: str, root: Path) -> SyncOutcome:
    return SyncOutcome(
        code=code,
        outcome=outcome,
        issue=record.issue,
        task_id=record.task,
        branch=record.branch,
        onto=record.onto,
        rebased=record.rebased,
        classification=record.classification,
        upstream=record.upstream,
        conflicts=list(record.conflicts),
        checks=list(record.checks),
        backup_ref=record.backup_ref,
        state_file=str(sync_file(root)) if sync_file(root).exists() else None,
    )


# --------------------------------------------------------------------------- claim checks


def _local_claim(git: Git, head: str, onto: str, task_id: str) -> tuple[str, str | None] | None:
    """The claim commit (sha, run) carried by ``head`` above its merge base with ``onto``."""
    base = git.merge_base(onto, head)
    commits = git.commits(f"{base}..{head}", first_parent=True)
    index = event_commit_index(commits, task_id)
    if index is None:
        return None
    event = last_event_from_trailers([commits[index].trailers], task_id=task_id)
    if event is None or event.event is not BranchEvent.CLAIM:
        return None
    return commits[index].sha, run_of(commits[index].trailers)


def _verify_claim(ctx: SyncContext, issue: int, record: SyncRecord | None = None) -> OwnClaim:
    """My own, complete and live claim on ``issue`` (exit 5 otherwise).

    During a sync the lease may still name the claim as it was before the rebase. With the
    ADR 0011 run rule it matches anyway; a lease without ``run`` (older claims) is accepted
    when it names the recorded pre-sync claim and the remote claim is the rebased copy
    carried by the local HEAD (a push that landed before an interruption).
    """
    own = require_own_claim(ctx, issue)
    claim, lease = own.claim, own.lease
    task_id = own.record.meta.id
    named = claim.named_by(lease)
    if not named and record is not None and lease is not None:
        head = ctx.git.rev_parse("HEAD")
        local = None if head is None else _local_claim(ctx.git, head, record.onto, task_id)
        named = (
            lease_names_claim(lease, record.claim, record.claim_run)
            and local is not None
            and local[0] == claim.sha
        )
    if lease is None or not named or lease.released_at is not None:
        raise PreconditionError(
            f"your claim on #{issue} ({task_id}) is incomplete: the lease comment does not "
            f"name its claim commit {claim.sha[:12]}",
            hint=f"complete it with `forge next --task {issue}`, then sync",
        )
    activity = max(claim.tip_date, lease.heartbeat_at)
    if is_stale(activity, ctx.config.lease_hours, ctx.now()):
        raise PreconditionError(
            f"your claim on #{issue} ({task_id}) went stale at "
            f"{format_time(stale_at(activity, ctx.config.lease_hours))}; a stale claim holds "
            "no zones, so it cannot be synced",
            hint=f"renew it through a verified claim: `forge next --task {issue}`",
        )
    return own


def _dependency_zones(ctx: SyncContext, meta: TaskMetadata, warnings: list[str]) -> list[str]:
    if not meta.depends_on:
        return []
    issues = ctx.github.list_issues(state="all", labels=[FORGE_TASK_LABEL])
    by_id: dict[str, TaskMetadata] = {}
    for rec in parse_records(issues, []):
        by_id.setdefault(rec.meta.id, rec.meta)
    zones: list[str] = []
    for dep in meta.depends_on:
        found = by_id.get(dep)
        if found is None:
            warnings.append(f"dependency {dep} not found; its zones are not considered")
            continue
        zones.extend(found.zones)
    return zones


# --------------------------------------------------------------------------- git helpers


def _backup_ref(git: Git, issue: int, now: datetime, suffix: str = "") -> str:
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    ref = f"{BACKUP_PREFIX}/task-{issue}/{stamp}{suffix}"
    n = 1
    while git.rev_parse(ref) is not None:
        n += 1
        ref = f"{BACKUP_PREFIX}/task-{issue}/{stamp}{suffix}-{n}"
    return ref


def _backup_order(ref: str) -> list[str | int]:
    """Sort key: the UTC timestamp in the name, then numeric suffixes compared as numbers."""
    name = ref.rsplit("/", 1)[-1]
    return [int(t) if i % 2 else t for i, t in enumerate(re.split(r"(\d+)", name))]


def _in_progress_backups(git: Git) -> set[str]:
    """Backup refs named by a ``sync.json`` in any worktree of this repository."""
    named: set[str] = set()
    try:
        worktrees = git.worktree_list()
    except GitError:
        worktrees = [git.root()]
    for worktree in worktrees:
        try:
            data = read_json_file(sync_file(worktree))
        except (ForgeError, OSError):
            continue
        if isinstance(data, Mapping) and isinstance(data.get("backup_ref"), str):
            named.add(data["backup_ref"])
    return named


_ABORTED_NAME = re.compile(re.escape(ABORTED_SUFFIX) + r"(?:-\d+)?$")


def is_aborted_ref(ref: str) -> bool:
    """True for a ref saved by ``--abort`` or an escalation (``<stamp>-aborted[-n]``)."""
    return _ABORTED_NAME.search(ref.rsplit("/", 1)[-1]) is not None


def prune_backups(git: Git, issue: int, *, keep: Iterable[str] = ()) -> list[str]:
    """Delete the oldest backup refs of task ``issue`` beyond the newest :data:`MAX_BACKUPS`.

    Refs in ``keep`` and those named by an in-progress ``sync.json`` (in any worktree) are
    never deleted; they still count towards the limit. ``-aborted`` refs are never deleted
    and do not count: they may be the only ref to work that a restore took off the branch.
    Returns the deleted refs.
    """
    refs = sorted(
        (r for r in git.list_refs(f"{BACKUP_PREFIX}/task-{issue}/") if not is_aborted_ref(r)),
        key=_backup_order,
    )
    protected = {*keep, *_in_progress_backups(git)}
    excess = len(refs) - MAX_BACKUPS
    deleted: list[str] = []
    for ref in refs:
        if excess <= 0:
            break
        if ref in protected:
            continue
        git.delete_ref(ref)
        deleted.append(ref)
        excess -= 1
    return deleted


def _head(git: Git) -> str:
    head = git.rev_parse("HEAD")
    if head is None:  # pragma: no cover - a task branch always has commits
        raise ForgeError("HEAD does not name a commit")
    return head


def _restore(git: Git, record: SyncRecord, now: datetime) -> str | None:
    """Abort a rebase in progress and restore the pre-sync tip and wip changes.

    ``git rebase --abort`` returns to the HEAD the *rebase* started from, which is not the
    backup when ``--continue`` restarted the rebase on top of new commits. Whenever HEAD then
    differs from the backup, it is saved first under ``<UTC timestamp>-aborted`` (never
    pruned). Returns that ref, or ``None`` when nothing had to be saved. Raises
    :class:`PreconditionError`, before any reset, when the backup ref is missing.
    """
    backup = git.rev_parse(record.backup_ref)
    if backup is None:
        raise PreconditionError(
            f"the backup ref {record.backup_ref} of this sync is missing; nothing was reset",
            hint="restore the branch by hand (`git reflog`), then delete .forge/local/sync.json",
        )
    if git.rebase_in_progress():
        git.rebase_abort()
    saved: str | None = None
    head = _head(git)
    if head != backup:
        saved = _backup_ref(git, record.issue, now, ABORTED_SUFFIX)
        git.update_ref(saved, head)
    git.reset(record.backup_ref, "hard")
    if record.wip:
        git.reset(record.original_tip, "mixed")
    return saved


@dataclass(frozen=True)
class TaskScope:
    """The task's zones and the contract zones it owns (ADR 0011).

    A task owns a contract zone listed in its zones when it is ``type: contract`` or sets
    ``allow_contract_change: true``; conflicts there do not escalate.
    """

    zones: tuple[str, ...]
    owned_contracts: frozenset[str]


def _entry(ctx: SyncContext, scope: TaskScope, path: str, commit: str | None) -> ConflictEntry:
    zones = tuple(ctx.zones.zones_for_path(path))
    contract = any(ctx.zones.zones[z].contract and z not in scope.owned_contracts for z in zones)
    outside = bool(ctx.zones.files_outside([path], scope.zones))
    return ConflictEntry(path, zones, contract, outside, commit)


def _resolver_for(resolvers: Sequence[Resolver], path: str) -> Resolver | None:
    for resolver in resolvers:
        if glob_matches(resolver.glob, path):
            return resolver
    return None


def _apply_resolver(ctx: SyncContext, root: Path, resolver: Resolver, path: str) -> str | None:
    """Resolve ``path`` mechanically; return ``None`` on success, else why it failed."""
    git = ctx.git
    if resolver.strategy == "regenerate":
        try:
            git.checkout_stage(path, "ours")  # during a rebase, "ours" is upstream
        except GitError as exc:
            return f"no upstream version to take: {exc.message}"
        assert resolver.command is not None
        run = ctx.run_command(resolver.command, root)
        if run.returncode != 0:
            return f"`{resolver.command}` exited {run.returncode}: {tail(run.output)}"
        git.add_paths([path])
        return None
    ours, theirs = git.stage_blob(2, path), git.stage_blob(3, path)
    if ours is None or theirs is None:
        return "one side deleted the file; union needs both versions"
    base = git.stage_blob(1, path) or b""
    merged = git.merge_file_union(ours, base, theirs)
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(merged)
    git.add_paths([path])
    return None


# --------------------------------------------------------------------------- the rebase loop


def _stop(
    ctx: SyncContext,
    root: Path,
    record: SyncRecord,
    task_zones: TaskScope,
    rerere: Sequence[str],
) -> SyncOutcome | None:
    """Handle one rebase stop. ``None`` means the rebase can continue.

    ``rerere`` are the paths of this stop that rerere resolved from a recorded resolution
    (:attr:`RebaseResult.rerere`). A path that a resolver handles gets its conflict back and
    goes through the resolver (a fresh ``regenerate`` beats a replayed one). The others are
    recorded as ``rerere`` and go through the escalation check like open conflicts: a
    resolution recorded during an earlier ``--force-resolve`` sync must not be replayed
    outside the zones without it.
    """
    git = ctx.git
    commit = git.rev_parse("REBASE_HEAD")
    unmerged = git.conflicted_files()
    replayed: list[ConflictEntry] = []
    mechanical: set[str] = set()
    for path in rerere:
        if path in unmerged:
            continue
        if _resolver_for(ctx.config.resolvers, path) is not None:
            mechanical.add(path)
            if git.restore_conflict(path):
                continue
        entry = _entry(ctx, task_zones, path, commit)
        entry.resolution = "rerere"
        record.conflicts.append(entry)
        replayed.append(entry)
    unmerged = git.conflicted_files()
    for path in unmerged:
        entry = _entry(ctx, task_zones, path, commit)
        resolver = _resolver_for(ctx.config.resolvers, path)
        if resolver is not None:
            failure = _apply_resolver(ctx, root, resolver, path)
            if failure is None:
                entry.resolution = resolver.strategy
            else:
                entry.detail = f"{resolver.strategy} failed: {failure}"
        record.conflicts.append(entry)
    remaining = git.conflicted_files()
    open_entries = [c for c in record.conflicts if c.resolution is None and c.path in remaining]
    # resolver paths are mechanical: they never escalate, even when rerere replayed them
    blocking_replays = [
        c for c in replayed if (c.outside or c.contract) and c.path not in mechanical
    ]
    blocking = [c for c in open_entries if c.outside or c.contract] + blocking_replays
    if blocking and not record.force_resolve:
        return _escalate(ctx, root, record, [*open_entries, *blocking_replays])
    if not remaining:
        return None
    record.state = SyncState.CONFLICTS
    write_sync(root, record)
    out = _outcome(record, ExitCode.AGENT_ACTION, "conflicts", root)
    out.unresolved = list(remaining)
    for entry in open_entries:
        if entry.detail:
            out.warn(f"{entry.path}: {entry.detail}")
    out.error(
        f"{len(remaining)} conflicted file(s) left for you: {', '.join(remaining)}",
        hint=_CONTINUE_HINT,
    )
    return out


def _drive(
    ctx: SyncContext, root: Path, record: SyncRecord, task_zones: TaskScope, *, start: bool
) -> SyncOutcome | None:
    """Run (``start``) or continue the rebase until it ends (``None``) or stops for good."""
    git = ctx.git
    record.rebased = True
    write_sync(root, record)
    if start:
        result = git.rebase(
            record.onto, rerere=True, keep_empty=True, committer_date_is_author_date=True
        )
    else:
        result = git.rebase_continue(rerere=True)
    last_stuck: str | None = None
    while not result.ok:
        outcome = _stop(ctx, root, record, task_zones, result.rerere)
        if outcome is not None:
            return outcome
        here = git.rev_parse("REBASE_HEAD") or ""
        if not result.conflicts and last_stuck == here:
            # git refused to continue with nothing unmerged at the same stop twice.
            record.state = SyncState.CONFLICTS
            write_sync(root, record)
            out = _outcome(record, ExitCode.AGENT_ACTION, "conflicts", root)
            out.error(
                f"git cannot continue the rebase at {here[:12]} although nothing is unmerged",
                hint="inspect `git status`, finish the step with git, then run "
                "`forge sync --continue` (or `forge sync --abort`)",
            )
            return out
        last_stuck = here if not result.conflicts else None
        write_sync(root, record)
        result = git.rebase_continue(rerere=True)
    write_sync(root, record)
    return None


def _escalate(
    ctx: SyncContext, root: Path, record: SyncRecord, open_entries: Sequence[ConflictEntry]
) -> SyncOutcome:
    """Exit 4: restore the pre-sync state, then label and comment (ADR 0011)."""
    saved = _restore(ctx.git, record, ctx.now())
    remove_sync(root)
    files = [(c.path, _why(c)) for c in open_entries]
    key = digest(str(record.issue), record.onto, record.original_tip)
    escalation = escalate_conflict(
        ctx.github,
        issue_number=record.issue,
        task_id=record.task,
        me=record.login,
        files=files,
        context=record.upstream,
        key=key,
        default_branch=ctx.config.project.default_branch,
        saved_ref=saved,
    )
    append_sync_log(root, _log_entry(record, ctx.now(), head=_head(ctx.git), outcome="escalated"))
    out = _outcome(record, ExitCode.ESCALATION, "escalated", root)
    out.head = _head(ctx.git)
    out.unresolved = [c.path for c in open_entries]
    out.escalation = escalation
    out.saved_ref = saved
    for warning in escalation.warnings:
        out.warn(warning)
    if saved is not None:
        out.warn(
            f"the branch was reset to its pre-sync state; the commits made since the sync "
            f"started are saved at {saved}",
            hint=f"bring them back with `git cherry-pick {record.backup_ref}..{saved}`",
        )
    out.error(
        "conflicts outside the task's zones or in a contract zone: "
        + ", ".join(f"{c.path} ({_why(c)})" for c in open_entries)
        + f"; the rebase was aborted and #{record.issue} is labelled {BLOCKED_CONFLICT}",
        hint="wait for the people mentioned on the issue, or resolve anyway with "
        "`forge sync --force-resolve` if you own the change",
    )
    return out


def _why(entry: ConflictEntry) -> str:
    if entry.resolution == "rerere":
        return f"{entry.why}; rerere replayed an earlier resolution"
    return entry.why


# --------------------------------------------------------------------------- verify and push


def _log_entry(
    record: SyncRecord,
    now: datetime,
    *,
    head: str,
    outcome: str,
    checks_passed: bool | None = None,
    checked_head: str | None = None,
) -> dict[str, Any]:
    return {
        "id": record.backup_ref,
        "at": format_time(now),
        "onto": record.onto,
        "head": head,
        "upstream": record.upstream_count,
        "classification": record.classification.to_data(),
        "conflicts": [c.to_data() for c in record.conflicts],
        "force_resolve": record.force_resolve,
        "checks_passed": checks_passed,
        "checked_head": checked_head,
        "outcome": outcome,
        "upstream_context": record.upstream.to_data(),
    }


def _run_checks(ctx: SyncContext, root: Path) -> list[CheckRun]:
    runs: list[CheckRun] = []
    for name, command in ctx.config.checks.items():
        result = ctx.run_check(command, root)
        runs.append(CheckRun(name, command, result.returncode, tail(result.output)))
    return runs


def _refresh_lease(ctx: SyncContext, record: SyncRecord, head: str) -> dict[str, Any]:
    """Rewrite the lease for the claim carried by ``head`` (keeps ``claimed_at``)."""
    local = _local_claim(ctx.git, head, record.onto, record.task)
    sha, run = local if local is not None else (record.claim, record.claim_run)
    comments = ctx.github.list_comments(record.issue)
    found = find_lease(comments)
    lease = None if found is None else found.data
    now = ctx.now()
    claimed_at = lease.claimed_at if lease is not None else now
    data = LeaseData(
        task=record.task,
        owner=record.login,
        agent=ctx.agent.id,
        claim=sha,
        claimed_at=claimed_at,
        heartbeat_at=now,
        run=run,
    )
    upsert_lease(ctx.github, record.issue, data, comments=comments)
    return {"claim": sha, "run": run, "heartbeat_at": format_time(now)}


def _finish(
    ctx: SyncContext,
    root: Path,
    record: SyncRecord,
    head: str,
    outcome: str,
    checks_passed: bool | None,
) -> SyncOutcome:
    """After the push (or when up to date): lease, label, log, summary, current.json."""
    out = _outcome(record, ExitCode.OK, outcome, root)
    out.head = head
    out.lease = _refresh_lease(ctx, record, head)
    if outcome == "pushed":
        issue = ctx.github.get_issue(record.issue)
        if has_label(issue.labels, BLOCKED_CONFLICT):
            ctx.github.remove_label(record.issue, BLOCKED_CONFLICT)
            out.info(f"removed {BLOCKED_CONFLICT} from #{record.issue}")
    append_sync_log(
        root,
        _log_entry(
            record,
            ctx.now(),
            head=head,
            outcome=outcome,
            checks_passed=checks_passed,
            checked_head=head if checks_passed else None,
        ),
    )
    resolved = [c for c in record.conflicts if c.resolution is not None]
    if outcome == "pushed" and resolved:
        key = digest(record.backup_ref, head)
        lines = [
            SYNC_MARKER,
            key_line(key),
            f"**Forge sync**: `{record.branch}` rebased onto "
            f"`{ctx.config.project.default_branch}` (`{record.onto[:12]}`, "
            f"{record.upstream_count} upstream commit(s)) and pushed (`{head[:12]}`). "
            f"Category: {record.classification.primary.value}.",
            "",
            "Conflicts resolved:",
            *(f"- `{c.path}`: {c.resolution}" for c in resolved),
        ]
        post_once(ctx.github, record.issue, key, "\n".join(lines) + "\n")
    current = read_current(root)
    claim = out.lease["claim"]
    if current is not None and current.issue == record.issue and current.claim != claim:
        write_current(root, replace(current, claim=claim))
    remove_sync(root)
    out.state_file = None
    if outcome == "pushed":
        out.info(f"{record.branch} synced onto {ctx.config.project.default_branch} and pushed")
    else:
        out.info(f"{record.branch} is up to date with {ctx.config.project.default_branch}")
    return out


def _verify_and_push(ctx: SyncContext, root: Path, record: SyncRecord) -> SyncOutcome:
    git = ctx.git
    head = _head(git)
    if not record.rebased and head == record.remote_tip:
        return _finish(ctx, root, record, head, "up_to_date", None)
    record.checks = _run_checks(ctx, root)
    failed = [c for c in record.checks if not c.passed]
    if failed:
        record.state = SyncState.CHECKS_FAILED
        write_sync(root, record)
        out = _outcome(record, ExitCode.AGENT_ACTION, "checks_failed", root)
        out.head = head
        out.error(
            "failing checks: " + ", ".join(f"{c.name} (exit {c.exit_code})" for c in failed),
            hint="fix your own code, commit, then run `forge sync --continue`; if the failing "
            "area belongs to someone else, run `forge escalate --reason ...`",
        )
        return out
    write_sync(root, record)
    ref = f"refs/heads/{record.branch}"
    try:
        result = git.push(head, ref, lease=Lease(ref, record.remote_tip), remote=ctx.remote)
    except PushHookRejected as exc:
        record.state = SyncState.PUSH_REJECTED
        write_sync(root, record)
        out = _outcome(record, ExitCode.AGENT_ACTION, "push_rejected", root)
        out.head = head
        out.output = tail(exc.stderr)
        out.error(
            "your pre-push hook rejected the push",
            hint="fix what the hook reports, commit, then run `forge sync --continue`",
        )
        return out
    if not result.ok:
        current = git.remote_branch_sha(record.branch, ctx.remote)
        if current != head:
            write_sync(root, record)
            out = _outcome(record, ExitCode.PRECONDITION, "lease_rejected", root)
            out.head = head
            out.error(
                f"{record.branch} moved on {ctx.remote} since the sync started "
                f"({record.remote_tip[:12]} -> {current[:12] if current else 'deleted'}); "
                "nothing was pushed",
                hint=f"run `forge sync --abort`, then check `forge task show {record.issue}` "
                "(the task may have been reclaimed)",
            )
            return out
    return _finish(ctx, root, record, head, "pushed", True)


# --------------------------------------------------------------------------- entry points


def _task_zones(ctx: SyncContext, meta: TaskMetadata) -> TaskScope:
    unknown = sorted(z for z in meta.zones if z not in ctx.zones.zones)
    if unknown:
        raise PreconditionError(
            f"task {meta.id} names zones that .forge/zones.yml does not define: "
            + ", ".join(unknown),
            hint=UNKNOWN_ZONE_FIX,
        )
    owns = meta.type is TaskType.CONTRACT or meta.allow_contract_change
    owned = frozenset(z for z in meta.zones if owns and ctx.zones.zones[z].contract)
    return TaskScope(tuple(meta.zones), owned)


def _require_record(root: Path, what: str) -> SyncRecord:
    record = read_sync(root)
    if record is None:
        raise PreconditionError(
            f"no sync in progress in {root}: nothing to {what}",
            hint="start one with `forge sync`",
        )
    return record


def sync(ctx: SyncContext, options: SyncOptions | None = None) -> SyncOutcome:
    """``forge sync``: rebase the current task onto ``origin/<default>`` and push (ADR 0011).

    Returns exit 0 (pushed or up to date), 2 (conflicts left, failing checks, a hook rejected
    the wip commit or the push), 4 (escalated) or 5 (push lease rejected). Preconditions
    raise :class:`PreconditionError` (exit 5) before anything changes.
    """
    opts = options or SyncOptions()
    git = ctx.git
    root = git.root()
    if sync_file(root).exists():
        raise PreconditionError(
            f"a sync is already in progress ({sync_file(root)})",
            hint="run `forge sync --continue` or `forge sync --abort`",
        )
    if git.rebase_in_progress():
        raise PreconditionError(
            "a rebase is in progress that Forge did not start",
            hint="finish it (`git rebase --continue`) or abort it (`git rebase --abort`), "
            "then run `forge sync`",
        )
    location = find_current_task(git)
    if location is None:
        raise PreconditionError(
            f"no current task in {root}",
            hint="run this inside the worktree of a claimed task (or on its task/<n> branch)",
        )
    if git.current_branch() != location.branch:
        raise PreconditionError(
            f"{location.branch} is not checked out in {root}",
            hint=f"`git switch {location.branch}`, then run `forge sync`",
        )
    issue = location.issue
    own = _verify_claim(ctx, issue)
    meta = own.record.meta
    task_zones = _task_zones(ctx, meta)
    remote_tip = own.claim.tip
    original_tip = _head(git)
    if not git.is_ancestor(remote_tip, original_tip):
        raise PreconditionError(
            f"the local {location.branch} does not contain {ctx.remote}/{location.branch} "
            f"({remote_tip[:12]}); a forced push would drop those commits",
            hint=f"bring them in first with `git rebase {ctx.remote}/{location.branch}` (or "
            f"`git pull --rebase`), then run `forge sync`; do not reset to "
            f"{ctx.remote}/{location.branch}, that would discard your local commits",
        )

    # ---- analysis: read-only (git and GitHub), before anything changes
    warnings: list[str] = []
    onto_ref = f"refs/remotes/{ctx.remote}/{ctx.config.project.default_branch}"
    onto = git.rev_parse(onto_ref)
    if onto is None:  # pragma: no cover - require_own_claim fetched it
        raise PreconditionError(f"{onto_ref} not found")
    base = git.merge_base(onto, original_tip)  # a wip commit on top does not change it
    upstream: list[CommitFiles] = git.commits_with_files(f"{base}..{onto}")
    task_commits = git.commits_with_files(f"{base}..{original_tip}")
    upstream_files = {f for c in upstream for f in c.files}
    # the files of the wip commit below are exactly the uncommitted changes
    task_files = {f for c in task_commits for f in c.files} | set(git.dirty_files())
    classification = classify(
        upstream_files,
        task_files,
        zones=ctx.zones,
        task_zones=task_zones.zones,
        dependency_zones=_dependency_zones(ctx, meta, warnings),
    )
    relevant = set(classification.shared)
    for files in classification.semantic.values():
        relevant.update(files)
    context = gather_upstream_context(ctx.github, upstream, relevant)
    warnings.extend(context.warnings)

    # ---- preflight: wip commit, backup ref, sync.json (local only, no GitHub call)
    wip = False
    if git.is_dirty(include_untracked=True):
        try:
            git.commit_all(WIP_MESSAGE)
        except GitAddFailed as exc:
            _unstage(git)
            raise ForgeError(
                f"`git add -A` failed while preparing the `{WIP_MESSAGE}` commit; nothing "
                "was changed: " + "; ".join(tail(exc.stderr).strip().splitlines()),
                hint="fix what git reports (e.g. an unreadable file), or commit your changes "
                "yourself, then run `forge sync`",
            ) from exc
        except GitError as exc:
            git.reset("HEAD", "mixed")
            out = SyncOutcome(
                ExitCode.AGENT_ACTION,
                "wip_rejected",
                issue,
                meta.id,
                location.branch,
                head=original_tip,
                output=tail(exc.stderr),
            )
            out.error(
                "a commit hook rejected the `forge: wip` commit; nothing else was done",
                hint="fix what the hook reports (or commit your changes yourself), then run "
                "`forge sync`",
            )
            return out
        wip = True
    head = _head(git)
    now = ctx.now()
    backup = _backup_ref(git, issue, now)
    git.update_ref(backup, head)
    claim = own.claim
    record = SyncRecord(
        task=meta.id,
        issue=issue,
        branch=location.branch,
        login=own.login,
        started_at=now,
        backup_ref=backup,
        original_tip=original_tip,
        wip=wip,
        remote_tip=remote_tip,
        onto=onto,
        base=base,
        claim=claim.sha,
        claim_run=claim.run,
        force_resolve=opts.force_resolve,
        classification=classification,
        upstream=context,
        upstream_count=len(upstream),
    )
    write_sync(root, record)
    prune_backups(git, issue, keep=[backup])

    behind = not git.is_ancestor(onto, head)
    if not behind and head == remote_tip:
        out = _finish(ctx, root, record, head, "up_to_date", None)
        _add_warnings(out, warnings, wip)
        return out
    stopped = _drive(ctx, root, record, task_zones, start=True) if behind else None
    out = stopped if stopped is not None else _verify_and_push(ctx, root, record)
    _add_warnings(out, warnings, wip)
    return out


def _unstage(git: Git) -> None:
    """Best effort: undo a partial ``git add -A`` (the index is reset to HEAD)."""
    with contextlib.suppress(GitError):
        git.reset("HEAD", "mixed")


def _add_warnings(out: SyncOutcome, warnings: Iterable[str], wip: bool) -> None:
    if wip:
        out.info(f"uncommitted changes were committed as `{WIP_MESSAGE}`")
    for text in warnings:
        out.warn(text)


def sync_continue(ctx: SyncContext) -> SyncOutcome:
    """``forge sync --continue``: resume a stopped sync (ADR 0011).

    Requires ``sync.json`` (exit 5 otherwise) and my live, complete claim. With a rebase in
    progress, unmerged paths left are exit 2; otherwise the rebase continues (resolvers and
    the escalation check apply to new stops, with the recorded ``force_resolve``). Without
    one, a dirty tree is exit 5 (commit the fixes first). Then the checks and the push run as
    in :func:`sync`.
    """
    git = ctx.git
    root = git.root()
    record = _require_record(root, "continue")
    own = _verify_claim(ctx, record.issue, record)
    task_zones = _task_zones(ctx, own.record.meta)
    if git.rebase_in_progress():
        unmerged = git.conflicted_files()
        if unmerged:
            out = _outcome(record, ExitCode.AGENT_ACTION, "conflicts", root)
            out.unresolved = list(unmerged)
            out.error(f"unmerged paths left: {', '.join(unmerged)}", hint=_CONTINUE_HINT)
            return out
        _mark_manual(record)
        stopped = _drive(ctx, root, record, task_zones, start=False)
        if stopped is not None:
            return stopped
        return _verify_and_push(ctx, root, record)
    if git.current_branch() != record.branch:
        raise PreconditionError(
            f"{record.branch} is not checked out in {root}",
            hint=f"`git switch {record.branch}`, then run `forge sync --continue`",
        )
    if git.is_dirty(include_untracked=True):
        raise PreconditionError(
            f"the working tree {root} has uncommitted changes",
            hint="commit your fixes, then run `forge sync --continue`",
        )
    head = _head(git)
    if not git.is_ancestor(record.onto, head):
        # the rebase never started, or was aborted outside Forge: start it again. Every
        # conflict of the earlier attempt is replayed (and recorded again) by the new rebase,
        # so the old entries are dropped: no path is reported twice.
        record.conflicts = []
        stopped = _drive(ctx, root, record, task_zones, start=True)
        if stopped is not None:
            return stopped
    else:
        _mark_manual(record)
    return _verify_and_push(ctx, root, record)


def _mark_manual(record: SyncRecord) -> None:
    for entry in record.conflicts:
        if entry.resolution is None:
            entry.resolution = "manual"
            entry.detail = None


def sync_abort(ctx: SyncContext | AbortContext) -> SyncOutcome:
    """``forge sync --abort``: restore the exact pre-sync state (ADR 0011).

    Only ``git`` and the clock of ``ctx`` are used (no GitHub, no project files). With a
    rebase in progress, the rebase is aborted; the uncommitted changes of the stop (conflict
    resolutions in progress) are lost and listed in a warning. Without one, a dirty tree is
    exit 5 (the reset would discard those changes). Either way a HEAD that differs from the
    backup (commits made after failing checks, or a rebase that ``--continue`` restarted on
    new commits) is saved first under ``refs/forge/backup/task-<n>/<UTC timestamp>-aborted``
    (:attr:`SyncOutcome.saved_ref`). Then HEAD is reset hard to the backup ref and, when a wip
    commit was made, mixed to the original tip (the wip changes come back uncommitted,
    untracked files untracked). ``sync.json`` is removed; the backup ref is kept. Exit 5
    without ``sync.json``. Safe to re-run after an interruption: when only ``sync.json`` is
    left (HEAD at the original tip after a wip restore), it is simply removed.
    """
    git = ctx.git
    root = git.root()
    record = _require_record(root, "abort")
    saved: str | None = None
    discarded: list[str] = []
    if git.rebase_in_progress():
        discarded = git.dirty_files()
        saved = _restore(git, record, ctx.now())
    else:
        if git.current_branch() != record.branch:
            raise PreconditionError(
                f"{record.branch} is not checked out in {root}; nothing was changed",
                hint=f"`git switch {record.branch}`, then run `forge sync --abort`",
            )
        if record.wip and _head(git) == record.original_tip:
            pass  # an earlier --abort stopped after its last reset: only sync.json is left
        elif git.is_dirty(include_untracked=True):
            raise PreconditionError(
                f"the working tree {root} has uncommitted changes that --abort would discard; "
                "nothing was changed",
                hint="commit or discard your changes first, then run `forge sync --abort`",
            )
        else:
            saved = _restore(git, record, ctx.now())
    remove_sync(root)
    prune_backups(git, record.issue, keep=[record.backup_ref])
    out = _outcome(record, ExitCode.OK, "aborted", root)
    out.head = _head(git)
    out.state_file = None
    out.saved_ref = saved
    out.info(
        f"{record.branch} restored to {record.original_tip[:12]}"
        + (" with your uncommitted changes" if record.wip else "")
        + f"; backup kept at {record.backup_ref}"
    )
    if saved is not None:
        out.info(
            f"the commits made during the sync are saved at {saved}",
            hint=f"bring them back with `git cherry-pick {record.backup_ref}..{saved}`",
        )
    if discarded:
        out.warn(
            "the rebase abort discarded the uncommitted changes of the stopped rebase "
            "(conflict resolutions in progress) in: " + ", ".join(discarded)
        )
    return out


__all__ = [
    "ABORTED_SUFFIX",
    "BACKUP_PREFIX",
    "LOCAL_DIR",
    "MAX_BACKUPS",
    "SYNC_MARKER",
    "AbortContext",
    "Category",
    "CheckRun",
    "Classification",
    "CommandRun",
    "ConflictEntry",
    "ShellRunner",
    "SyncContext",
    "SyncOptions",
    "SyncOutcome",
    "SyncRecord",
    "SyncState",
    "TaskScope",
    "classify",
    "contract_dependencies",
    "is_aborted_ref",
    "load_sync_zones",
    "prune_backups",
    "read_sync",
    "read_sync_log",
    "run_shell",
    "sync",
    "sync_abort",
    "sync_continue",
]
