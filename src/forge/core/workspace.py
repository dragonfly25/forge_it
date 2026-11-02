"""Local workspace for a claimed task: branch, worktree and ``.forge/local/`` state (ADR 0009).

After a claim is pushed and verified, :func:`prepare_workspace` makes the claimed tip available
locally:

- the local branch ``task/<n>`` is created at the claimed tip, or fast-forwarded to it. A local
  branch that is not an ancestor of the tip is left untouched and the call stops with exit 5;
- in worktree mode the branch is checked out in ``<worktrees_dir>/task-<n>`` (an existing
  worktree on that branch is reused); in place, the main working tree switches to the branch,
  which requires a clean tree;
- ``.forge/local/`` is created with its own ``.gitignore`` (``*``), so it never shows up in
  ``git status``, even before ``forge init`` adds the rule.

:class:`CurrentTask` is the content of ``.forge/local/current.json``. :func:`find_current_task`
locates the task the user is working on, from that file or from the branch name.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from forge.core.errors import ConfigError, ForgeError, PreconditionError
from forge.core.git import Git
from forge.core.tasks import TASK_ID_RE, TaskMetadata

LOCAL_DIR = Path(".forge/local")
"""Directory, relative to a workspace root, that holds local task state."""

CURRENT_FILE = LOCAL_DIR / "current.json"
"""Path, relative to a workspace root, of the current-task file."""

LOCAL_GITIGNORE = "*\n"
"""Content of ``.forge/local/.gitignore``: ignore everything, the file itself included."""

_TASK_BRANCH_RE = re.compile(r"^task/([1-9][0-9]*)$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_CURRENT_KEYS = ("task", "issue", "branch", "base", "claim", "agent", "claimed_at", "worktree")


# -- paths -----------------------------------------------------------------------------------


def main_worktree_root(git: Git, fallback: Path) -> Path:
    """Return the root of the main working tree, even when ``git`` runs inside a linked worktree.

    Task worktrees are siblings of the main clone, so a command run from inside a task
    worktree must not nest the next worktree under it. Bare repositories (no main working
    tree) return ``fallback``.
    """
    common = git.run("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    common_dir = Path(common)
    if common_dir.name == ".git":
        return common_dir.parent
    return fallback


def worktree_path(repo_root: Path, issue_number: int, worktrees_dir: Path | None = None) -> Path:
    """Return the worktree path for ``issue_number`` (spec §5.3, ADR 0009).

    ``<worktrees_dir>/task-<n>``, where ``worktrees_dir`` defaults to
    ``<parent-of-repo>/<repo-name>-worktrees``. A ``~`` in ``worktrees_dir`` is expanded.
    """
    base = (
        Path(worktrees_dir).expanduser()
        if worktrees_dir is not None
        else repo_root.parent / f"{repo_root.name}-worktrees"
    )
    return base / f"task-{issue_number}"


def ensure_local_dir(workspace: Path) -> Path:
    """Create ``<workspace>/.forge/local/`` with a ``.gitignore`` containing ``*``; return it."""
    local = workspace / LOCAL_DIR
    local.mkdir(parents=True, exist_ok=True)
    gitignore = local / ".gitignore"
    if not gitignore.exists() or gitignore.read_text() != LOCAL_GITIGNORE:
        gitignore.write_text(LOCAL_GITIGNORE)
    return local


def _same_path(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


# -- git helpers (local to this module; Git has no wrapper for them) ---------------------------


def _worktree_branches(git: Git) -> list[tuple[Path, str | None]]:
    """Return ``(path, branch)`` for every registered worktree (branch None when detached)."""
    out = git.run("worktree", "list", "--porcelain").stdout
    entries: list[tuple[Path, str | None]] = []
    path: Path | None = None
    branch: str | None = None
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
            branch = None
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/") :]
        elif not line and path is not None:
            entries.append((path, branch))
            path = None
    return entries


def _registered_missing(git: Git, path: Path) -> bool:
    """True when a worktree is registered at ``path`` but its directory is gone.

    Such a stale registration makes ``git worktree add`` refuse the path (and may still
    claim the branch). Only this exact path is considered; other stale entries stay.
    """
    return any(
        _same_path(entry, path) and not entry.exists() for entry, _ in _worktree_branches(git)
    )


def _checked_out_at(git: Git, branch: str) -> Path | None:
    """Return the worktree where ``branch`` is checked out, or None."""
    for path, name in _worktree_branches(git):
        if name == branch:
            return path
    return None


def _set_upstream(git: Git, branch: str, remote: str) -> bool:
    """Point ``branch`` at ``<remote>/<branch>`` as its upstream; return False if no such remote.

    The config keys are written directly, so this works before the remote-tracking ref exists
    (a claim push by sha does not always create it).
    """
    if git.config_get(f"remote.{remote}.url") is None:
        return False
    git.config_set(f"branch.{branch}.remote", remote)
    git.config_set(f"branch.{branch}.merge", f"refs/heads/{branch}")
    return True


def _fast_forward_in(worktree: Path, tip: str, branch: str) -> None:
    """Fast-forward the branch checked out in ``worktree`` to ``tip`` with ``merge --ff-only``."""
    proc = Git(worktree).run("merge", "--ff-only", "--quiet", tip, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "merge failed"
        raise PreconditionError(
            f"cannot fast-forward {branch} in {worktree} to the claimed tip {tip[:12]}: {detail}",
            hint=f"commit or stash the local changes in {worktree}, then run "
            f"`git merge --ff-only {tip}` there (or re-run the command)",
        )


# -- workspace -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Workspace:
    """Where the claimed task is checked out, and what :func:`prepare_workspace` did.

    ``path`` is the worktree (or the main working tree when ``in_place``). The flags record
    whether the local branch was created or fast-forwarded and whether the worktree was
    created or reused.
    """

    path: Path
    branch: str
    in_place: bool
    branch_created: bool = False
    branch_fast_forwarded: bool = False
    worktree_created: bool = False
    worktree_reused: bool = False

    def to_data(self) -> dict[str, Any]:
        """Return a JSON-able dict."""
        return {
            "path": str(self.path),
            "branch": self.branch,
            "in_place": self.in_place,
            "branch_created": self.branch_created,
            "branch_fast_forwarded": self.branch_fast_forwarded,
            "worktree_created": self.worktree_created,
            "worktree_reused": self.worktree_reused,
        }


def _occupied(path: Path) -> bool:
    """True when ``path`` exists and is not an empty directory."""
    if not path.exists() and not path.is_symlink():
        return False
    return not (path.is_dir() and not any(path.iterdir()))


def prepare_workspace(
    git: Git,
    *,
    repo_root: Path,
    issue_number: int,
    claim_tip: str,
    remote: str = "origin",
    use_worktree: bool = True,
    worktrees_dir: Path | None = None,
) -> Workspace:
    """Check out the claimed tip of ``task/<issue_number>`` locally (ADR 0009, post-claim step 5).

    ``git`` runs in the main repository; ``claim_tip`` must be a commit present locally (the
    claim commit just pushed). Every precondition is checked before anything changes, so a
    :class:`PreconditionError` (exit 5) leaves the repository as it was:

    - an existing local ``task/<n>`` that is not an ancestor of ``claim_tip`` (diverged);
    - worktree mode: the target path exists and is not a worktree on ``task/<n>``, or the
      branch is checked out in another worktree;
    - in place: the working tree has changes (tracked or untracked) and HEAD must move, or the
      branch is checked out in another worktree.

    A branch checked out in a worktree is fast-forwarded there with ``merge --ff-only``, never
    with ``update-ref``, so its working tree stays consistent.
    """
    branch = TaskMetadata.branch_name(issue_number)
    tip = git.rev_parse(claim_tip)
    if tip is None:
        raise ForgeError(
            f"the claimed tip {claim_tip} of {branch} is not available locally",
            hint=f"run `git fetch {remote}` and re-run the command",
        )

    # ---- inspect the local branch
    local_sha = git.rev_parse(f"refs/heads/{branch}")
    needs_ff = False
    if local_sha is not None and local_sha != tip:
        if not git.is_ancestor(local_sha, tip):
            raise PreconditionError(
                f"local branch {branch} ({local_sha[:12]}) has diverged from the claimed tip "
                f"{tip[:12]}; it was left untouched",
                hint=f"rename or delete the local branch (e.g. `git branch -m {branch} "
                f"{branch}-old`), then re-run `forge next --task #{issue_number}`; the claim "
                "is still held",
            )
        needs_ff = True
    checked_out = _checked_out_at(git, branch) if local_sha is not None else None

    # ---- check the target before changing anything
    stale_registration = False
    if use_worktree:
        path = worktree_path(main_worktree_root(git, repo_root), issue_number, worktrees_dir)
        stale_registration = _registered_missing(git, path)
        if stale_registration and checked_out is not None and _same_path(checked_out, path):
            checked_out = None  # registered there, but the directory was deleted
        reuse = checked_out is not None and _same_path(checked_out, path)
        if checked_out is not None and not reuse:
            raise PreconditionError(
                f"{branch} is already checked out at {checked_out}, not at {path}",
                hint=f"switch {checked_out} to another branch (or move that worktree with "
                f"`git worktree move {checked_out} {path}`), then re-run the command",
            )
        if not reuse and _occupied(path):
            raise PreconditionError(
                f"the worktree path {path} already exists and is not a worktree on {branch}",
                hint="remove or rename it (or set worktrees_dir in your profile), then re-run "
                "the command",
            )
    else:
        path = repo_root
        current = Git(repo_root).current_branch()
        if checked_out is not None and not _same_path(checked_out, repo_root):
            raise PreconditionError(
                f"{branch} is checked out in the worktree {checked_out}; it cannot also be "
                f"checked out in {repo_root}",
                hint=f"work in {checked_out}, or remove that worktree "
                f"(`git worktree remove {checked_out}`)",
            )
        reuse = current == branch
        moves_head = not reuse or needs_ff
        if moves_head and Git(repo_root).is_dirty(include_untracked=True):
            raise PreconditionError(
                f"the working tree {repo_root} has uncommitted or untracked changes; "
                f"cannot check out {branch} in place",
                hint="commit or stash your changes, or use a worktree "
                "(drop --no-worktree / set worktrees.enabled: true)",
            )

    # ---- create or fast-forward the branch
    branch_created = False
    if local_sha is None:
        git.run("branch", "--no-track", branch, tip)
        branch_created = True
    elif needs_ff:
        if checked_out is not None:
            _fast_forward_in(checked_out, tip, branch)
        else:
            git.update_ref(f"refs/heads/{branch}", tip)
    _set_upstream(git, branch, remote)

    # ---- check it out
    worktree_created = False
    if use_worktree:
        if not reuse:
            path.parent.mkdir(parents=True, exist_ok=True)
            if stale_registration:
                git.worktree_remove(path)  # drops only this path's registration
            git.worktree_add(path, branch)
            worktree_created = True
    elif not reuse:
        Git(repo_root).checkout(branch)

    ensure_local_dir(path)
    return Workspace(
        path=path,
        branch=branch,
        in_place=not use_worktree,
        branch_created=branch_created,
        branch_fast_forwarded=needs_ff,
        worktree_created=worktree_created,
        worktree_reused=use_worktree and reuse,
    )


# -- current.json ----------------------------------------------------------------------------


def format_timestamp(value: datetime) -> str:
    """Format an aware datetime as UTC ISO 8601 with a ``Z`` suffix (seconds precision)."""
    if value.tzinfo is None:
        raise ValueError(f"naive datetime {value.isoformat()} cannot be stored")
    return value.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def parse_timestamp(text: str) -> datetime:
    """Parse a UTC ISO 8601 timestamp with a ``Z`` suffix into an aware datetime."""
    if not text.endswith("Z"):
        raise ValueError(f"{text!r} is not a UTC timestamp ending in Z")
    value = datetime.fromisoformat(text[:-1] + "+00:00")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class CurrentTask:
    """Content of ``.forge/local/current.json`` (ADR 0009).

    ``base`` is the base sha (``origin/<default>`` at claim time, or the merge-base on a
    resume), ``claim`` the claim commit sha, ``worktree`` the worktree path (None when the
    task is checked out in the main working tree).
    """

    task: str
    issue: int
    branch: str
    base: str
    claim: str
    agent: str
    claimed_at: datetime
    worktree: Path | None

    def to_data(self) -> dict[str, Any]:
        """Return the JSON-able form written to ``current.json``."""
        return {
            "task": self.task,
            "issue": self.issue,
            "branch": self.branch,
            "base": self.base,
            "claim": self.claim,
            "agent": self.agent,
            "claimed_at": format_timestamp(self.claimed_at),
            "worktree": None if self.worktree is None else str(self.worktree),
        }


def write_current(workspace: Path, current: CurrentTask) -> Path:
    """Atomically write ``current`` to ``<workspace>/.forge/local/current.json``; return the path.

    The data goes to a temporary file in the same directory first and is then renamed over the
    target, so a reader never sees a half-written file.
    """
    local = ensure_local_dir(workspace)
    target = local / CURRENT_FILE.name
    tmp = local / f".{CURRENT_FILE.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(current.to_data(), indent=2, sort_keys=False) + "\n")
    os.replace(tmp, target)
    return target


def _corrupt(file: Path, key: str | None, problem: str) -> ConfigError:
    return ConfigError(
        file,
        key,
        problem,
        "delete the file and re-run `forge next --task <issue>` (it rewrites it; the claim on "
        "GitHub is unaffected)",
    )


def _parse_current(data: Any, file: Path) -> CurrentTask:
    if not isinstance(data, dict):
        raise _corrupt(file, None, "expected a JSON object")
    missing = [k for k in _CURRENT_KEYS if k not in data]
    if missing:
        raise _corrupt(file, missing[0], "missing key")
    unknown = sorted(set(data) - set(_CURRENT_KEYS))
    if unknown:
        raise _corrupt(file, unknown[0], "unknown key")

    def text(key: str) -> str:
        value = data[key]
        if not isinstance(value, str) or not value:
            raise _corrupt(file, key, "expected a non-empty string")
        return value

    task = text("task")
    if not TASK_ID_RE.match(task):
        raise _corrupt(file, "task", f"{task!r} is not a task id like M1-T07")
    issue = data["issue"]
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise _corrupt(file, "issue", "expected a positive integer")
    branch = text("branch")
    if branch != TaskMetadata.branch_name(issue):
        raise _corrupt(file, "branch", f"expected {TaskMetadata.branch_name(issue)!r}")
    shas = {}
    for key in ("base", "claim"):
        sha = text(key)
        if not _SHA_RE.match(sha):
            raise _corrupt(file, key, f"{sha!r} is not a full commit sha")
        shas[key] = sha
    try:
        claimed_at = parse_timestamp(text("claimed_at"))
    except ValueError as exc:
        raise _corrupt(file, "claimed_at", str(exc)) from None
    worktree = data["worktree"]
    if worktree is not None and (not isinstance(worktree, str) or not worktree):
        raise _corrupt(file, "worktree", "expected a path or null")
    return CurrentTask(
        task=task,
        issue=issue,
        branch=branch,
        base=shas["base"],
        claim=shas["claim"],
        agent=text("agent"),
        claimed_at=claimed_at,
        worktree=None if worktree is None else Path(worktree),
    )


def read_current(workspace: Path) -> CurrentTask | None:
    """Read ``<workspace>/.forge/local/current.json``; None when the file does not exist.

    An unreadable or invalid file raises :class:`ConfigError` naming the file, the key and the
    fix.
    """
    file = workspace / CURRENT_FILE
    try:
        raw = file.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _corrupt(file, None, f"invalid JSON ({exc.msg} at line {exc.lineno})") from None
    return _parse_current(data, file)


def clear_current(workspace: Path) -> bool:
    """Remove ``<workspace>/.forge/local/current.json``; return True if a file was removed."""
    file = workspace / CURRENT_FILE
    try:
        file.unlink()
    except FileNotFoundError:
        return False
    return True


# -- finding the current task ----------------------------------------------------------------


def task_branch_issue(branch: str | None) -> int | None:
    """Return ``n`` for a branch named exactly ``task/<n>``, else None."""
    if branch is None:
        return None
    match = _TASK_BRANCH_RE.match(branch)
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class TaskLocation:
    """The task the user is working on, and how it was found.

    ``source`` is ``"current"`` when it comes from ``current.json`` (then ``current`` is set and
    the task id is known) or ``"branch"`` when it was inferred from a ``task/<n>`` branch name
    (``current`` is None; the caller resolves the task id through GitHub). ``notes`` explains
    why ``current.json`` was ignored, if it was.
    """

    source: Literal["current", "branch"]
    issue: int
    branch: str
    workspace: Path
    current: CurrentTask | None = None
    notes: tuple[str, ...] = ()

    @property
    def task_id(self) -> str | None:
        """The task id when known (from ``current.json``), else None."""
        return None if self.current is None else self.current.task


def find_current_task(git: Git, cwd: Path | None = None) -> TaskLocation | None:
    """Locate the current task from ``cwd`` (default ``git.cwd``).

    ``<worktree root>/.forge/local/current.json`` wins when its branch is the one checked out
    (or HEAD is detached). Otherwise the task is inferred from a checked-out branch named
    ``task/<n>``. Returns None when neither applies. A corrupt ``current.json`` raises
    :class:`ConfigError`.
    """
    here = Git(cwd) if cwd is not None else git
    root = here.root()
    branch = here.current_branch()
    notes: list[str] = []
    current = read_current(root)
    if current is not None:
        if branch is None or branch == current.branch:
            return TaskLocation(
                source="current",
                issue=current.issue,
                branch=current.branch,
                workspace=root,
                current=current,
            )
        notes.append(
            f"{root / CURRENT_FILE} names {current.branch} but {branch} is checked out; ignored it"
        )
    issue = task_branch_issue(branch)
    if issue is None or branch is None:
        return None
    return TaskLocation(
        source="branch", issue=issue, branch=branch, workspace=root, notes=tuple(notes)
    )
