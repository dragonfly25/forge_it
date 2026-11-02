"""Pending-claim records: tell an interrupted claim from one still in flight (ADR 0009 amendment).

Login and agent id alone cannot tell a claim whose run crashed after the push from a claim
that another live run (same account, same agent) is still completing. Before pushing a
claim, ``forge next`` writes ``<git-common-dir>/forge/pending/task-<n>.json``::

    {"claim": "<sha>", "pid": 4242, "host": "laptop", "started_at": "2026-11-02T09:00:00Z"}

and removes it once the post-claim GitHub steps are done, or after yielding or losing the
race. An incomplete claim of mine is taken over automatically only when the record exists
on this machine, names that claim commit and its pid is no longer running
(:func:`may_take_over`). The git common dir is shared by every worktree of one clone, so
runs started from a task worktree see the same records.

``host`` is not the network hostname, which can change with the network, but a per-clone
machine id (:func:`machine_id`, ``<git-common-dir>/forge/machine-id``).

Reading a record and replacing it is a read-modify-write: two runs of one clone could both
see the same dead record and both take the claim over. Every such update happens under an
exclusive per-task lock (:func:`task_lock`).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from forge.core.errors import ForgeError
from forge.core.git import Git
from forge.core.lease import format_time, parse_time

if sys.platform != "win32":
    import fcntl

_KEYS = ("claim", "pid", "host", "started_at")
_RECORD_RE = re.compile(r"task-([1-9][0-9]*)\.json")
_MACHINE_ID_RE = re.compile(r"[0-9a-f]{32}")
LOCK_TIMEOUT = 2.0
"""Seconds :func:`task_lock` waits for another run's lock (it is held for milliseconds)."""


@dataclass(frozen=True)
class PendingClaim:
    """A claim that a run has pushed (or is pushing) but not yet completed."""

    issue: int
    claim: str
    pid: int
    host: str
    started_at: datetime

    def to_json(self) -> dict[str, Any]:
        """The JSON object stored in the record file."""
        return {
            "claim": self.claim,
            "pid": self.pid,
            "host": self.host,
            "started_at": format_time(self.started_at),
        }


def forge_dir(git: Git) -> Path:
    """``<git-common-dir>/forge`` of the clone ``git`` runs in (shared by its worktrees)."""
    common = git.run("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(common) / "forge"


def pending_dir(git: Git) -> Path:
    """``<git-common-dir>/forge/pending`` of the clone ``git`` runs in."""
    return forge_dir(git) / "pending"


def pending_path(git: Git, issue_number: int) -> Path:
    """Path of the record for ``task/<issue_number>``."""
    return pending_dir(git) / f"task-{issue_number}.json"


def write_pending(git: Git, record: PendingClaim) -> Path:
    """Write ``record`` atomically (temporary file + rename); return its path.

    Callers that replace another run's record hold :func:`task_lock` around the read that
    justified it and this write.
    """
    path = pending_path(git, record.issue)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(record.to_json(), separators=(",", ":")) + "\n")
    os.replace(tmp, path)
    return path


def read_pending(git: Git, issue_number: int) -> PendingClaim | None:
    """Return the record for ``task/<issue_number>``; ``None`` when absent or unreadable.

    An unreadable record counts as absent, which is the safe side: the claim is then never
    taken over automatically.
    """
    path = pending_path(git, issue_number)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or set(raw) != set(_KEYS):
        return None
    claim, pid, host, started = raw["claim"], raw["pid"], raw["host"], raw["started_at"]
    if not (isinstance(claim, str) and claim and isinstance(host, str) and host):
        return None
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(started, str):
        return None
    try:
        started_at = parse_time(started)
    except ValueError:
        return None
    return PendingClaim(issue_number, claim, pid, host, started_at)


def remove_pending(
    git: Git, issue_number: int, claim: str, *, timeout: float = LOCK_TIMEOUT
) -> bool:
    """Remove the record of ``task/<issue_number>`` if it names ``claim``; True if removed.

    A record naming another claim belongs to another run and is left alone. The check and
    the removal happen under :func:`task_lock`; when the lock stays busy the record is left
    in place (a later ``forge next`` prunes it once it is an orphan).
    """
    with task_lock(git, issue_number, timeout=timeout) as held:
        record = read_pending(git, issue_number) if held else None
        if record is None or record.claim != claim:
            return False
        try:
            pending_path(git, issue_number).unlink()
        except FileNotFoundError:  # pragma: no cover - removals hold the lock
            return False
        return True


def list_pending(git: Git) -> list[PendingClaim]:
    """Every readable record of this clone, by issue number (unreadable ones are skipped)."""
    folder = pending_dir(git)
    if not folder.is_dir():
        return []
    numbers = sorted(
        int(match.group(1))
        for path in folder.iterdir()
        if (match := _RECORD_RE.fullmatch(path.name)) is not None
    )
    return [record for n in numbers if (record := read_pending(git, n)) is not None]


def prune_orphans(
    git: Git,
    claims: Mapping[int, str],
    *,
    pid: int,
    host: str,
    running: Callable[[int], bool],
) -> list[int]:
    """Remove records left behind by runs that are gone; return their issue numbers.

    ``claims`` maps an issue to the claim commit its remote branch ends with (its last Forge
    event). A record is an orphan when it names another sha (or the task has no claim), and
    its run is gone: its pid is not ``running``, or it was written by another ``host``. A
    record whose sha is still the remote claim is kept (it allows the takeover), and so is a
    live run's record. Each removal re-reads the record under :func:`task_lock`; a record
    whose lock is busy is left for a later run.
    """
    pruned: list[int] = []
    for record in list_pending(git):
        if claims.get(record.issue) == record.claim:
            continue
        if record.host == host and running(record.pid):
            continue
        with task_lock(git, record.issue, pid=pid, host=host, timeout=0) as held:
            if not held or read_pending(git, record.issue) != record:
                continue
            pending_path(git, record.issue).unlink(missing_ok=True)
            pruned.append(record.issue)
    return pruned


# --------------------------------------------------------------------------- per-task lock


def lock_path(git: Git, issue_number: int) -> Path:
    """Path of the lock that serialises updates of ``task/<issue_number>``'s record."""
    return pending_dir(git) / f"task-{issue_number}.lock"


@contextmanager
def task_lock(
    git: Git,
    issue_number: int,
    *,
    pid: int | None = None,
    host: str = "",
    timeout: float = LOCK_TIMEOUT,
) -> Iterator[bool]:
    """Hold the exclusive lock of ``task/<issue_number>``'s record; yield False if busy.

    The lock is ``<git-common-dir>/forge/pending/task-<n>.lock``, holding ``{pid, host}``
    (default: this process) for diagnostics, locked with ``flock`` and removed on exit. The
    kernel releases a ``flock`` when its process dies, so a lock file left by a crashed run
    is never "held": it is simply locked again, and no stale-lock breaking (which cannot be
    made atomic with ``O_EXCL`` files alone) is needed. Callers keep the critical section
    local (read and write records, no network), so ``timeout`` seconds is plenty; a lock
    still busy after it yields False and the caller treats the task as in flight.
    """
    path = lock_path(git, issue_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = _acquire(path, timeout)
    if fd is None:
        yield False
        return
    try:
        os.ftruncate(fd, 0)
        holder = {"pid": os.getpid() if pid is None else pid, "host": host}
        os.write(fd, json.dumps(holder).encode() + b"\n")
        yield True
    finally:
        # Unlink while still holding the lock: a waiter that opened this inode notices the
        # unlink (inode check in _acquire) and retries on a fresh file.
        path.unlink(missing_ok=True)
        os.close(fd)


def _acquire(path: Path, timeout: float) -> int | None:
    """Open and lock ``path``; the locked fd, or None after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        fd = _try_lock(path)
        if fd is not None:
            try:
                current = os.stat(path).st_ino
            except FileNotFoundError:
                current = None
            if os.fstat(fd).st_ino == current:
                return fd
            os.close(fd)  # locked a file its holder had just removed: retry on the new one
            continue
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def _try_lock(path: Path) -> int | None:
    if sys.platform == "win32":  # pragma: no cover - Forge v0 runs on macOS, Linux and WSL
        try:
            return os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return None
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


# --------------------------------------------------------------------------- machine identity


def machine_id(git: Git) -> str:
    """This clone's machine id: ``<git-common-dir>/forge/machine-id``, created once.

    A random uuid4 (hex) instead of the hostname, which can change with the network and
    would then disable the automatic takeover of this machine's records. The file is created
    atomically (written to a temporary file, then hard-linked into place, which fails if
    another run created it first), so concurrent first runs agree on one id.
    """
    path = forge_dir(git) / "machine-id"
    found = _read_machine_id(path)
    if found is not None:
        return found
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".machine-id.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(uuid.uuid4().hex + "\n")
    try:
        os.link(tmp, path)
    except FileExistsError:
        pass
    finally:
        tmp.unlink(missing_ok=True)
    found = _read_machine_id(path)
    if found is None:
        raise ForgeError(
            f"{path} does not hold a machine id (32 hex digits)",
            hint="delete the file; Forge creates a new one",
        )
    return found


def _read_machine_id(path: Path) -> str | None:
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        return None
    return text if _MACHINE_ID_RE.fullmatch(text) else None


def pid_running(pid: int) -> bool:
    """True when a process with ``pid`` exists on this machine.

    ``os.kill(pid, 0)``: ``ProcessLookupError`` means gone, ``PermissionError`` means it runs
    under another user. On Windows signal 0 would terminate the process, so the answer there
    is always True (never take over automatically; ``forge next --task`` still works).
    """
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Forge v0 runs on macOS, Linux and WSL
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def may_take_over(
    record: PendingClaim | None,
    claim_sha: str,
    *,
    host: str,
    running: Callable[[int], bool],
) -> bool:
    """True when an incomplete claim may be completed automatically (ADR 0009 amendment).

    The record must exist, come from this ``host``, name ``claim_sha``, and its pid must no
    longer be ``running``.
    """
    if record is None or record.host != host or record.claim != claim_sha:
        return False
    return not running(record.pid)
