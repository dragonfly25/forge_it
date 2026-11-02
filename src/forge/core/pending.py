"""Pending-claim records: tell an interrupted claim from one still in flight (ADR 0009 amendment).

Login and agent id alone cannot tell a claim whose run crashed after the push from a claim
that another live run (same account, same agent) is still completing. Before pushing a
claim, ``forge next`` writes ``<git-common-dir>/forge/pending/task-<n>.json``::

    {"claim": "<sha>", "pid": 4242, "host": "laptop", "started_at": "2026-11-02T09:00:00Z"}

and removes it once the post-claim GitHub steps are done, or after yielding or losing the
race. An incomplete claim of mine is taken over automatically only when the record exists
on this host, names that claim commit and its pid is no longer running
(:func:`may_take_over`). The git common dir is shared by every worktree of one clone, so
runs started from a task worktree see the same records.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from forge.core.git import Git
from forge.core.lease import format_time, parse_time

_KEYS = ("claim", "pid", "host", "started_at")


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


def pending_dir(git: Git) -> Path:
    """``<git-common-dir>/forge/pending`` of the clone ``git`` runs in."""
    common = git.run("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(common) / "forge" / "pending"


def pending_path(git: Git, issue_number: int) -> Path:
    """Path of the record for ``task/<issue_number>``."""
    return pending_dir(git) / f"task-{issue_number}.json"


def write_pending(git: Git, record: PendingClaim) -> Path:
    """Write ``record`` atomically (temporary file + rename); return its path."""
    path = pending_path(git, record.issue)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
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


def remove_pending(git: Git, issue_number: int, claim: str) -> bool:
    """Remove the record of ``task/<issue_number>`` if it names ``claim``; True if removed.

    A record naming another claim belongs to another run and is left alone.
    """
    record = read_pending(git, issue_number)
    if record is None or record.claim != claim:
        return False
    try:
        pending_path(git, issue_number).unlink()
    except FileNotFoundError:
        return False
    return True


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
