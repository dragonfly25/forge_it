"""Tests for forge.core.pending: pending-claim records (ADR 0009 amendment)."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from forge.core.git import Git
from forge.core.pending import (
    PendingClaim,
    may_take_over,
    pending_path,
    pid_running,
    read_pending,
    remove_pending,
    write_pending,
)

if TYPE_CHECKING:
    from conftest import GitWorld

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SHA = "a" * 40
RECORD = PendingClaim(7, SHA, 123, "laptop", T0)


def test_round_trip_under_the_git_common_dir(git_world: GitWorld) -> None:
    git = git_world.clone("alice")
    path = write_pending(git, RECORD)
    assert path == pending_path(git, 7)
    assert path.resolve() == (git.cwd / ".git/forge/pending/task-7.json").resolve()
    assert read_pending(git, 7) == RECORD
    assert read_pending(git, 8) is None
    assert not git.is_dirty(include_untracked=True)
    assert [p.name for p in path.parent.iterdir()] == ["task-7.json"]  # no temp file left


def test_worktrees_share_the_records(git_world: GitWorld) -> None:
    git = git_world.clone("alice")
    worktree = git.cwd.parent / "alice-wt"
    git.worktree_add(worktree, "side", start_point="origin/main")
    write_pending(Git(worktree), RECORD)
    assert read_pending(git, 7) == RECORD


def test_remove_only_the_record_of_the_same_claim(git_world: GitWorld) -> None:
    git = git_world.clone("alice")
    write_pending(git, RECORD)
    assert not remove_pending(git, 7, "b" * 40)
    assert read_pending(git, 7) == RECORD
    assert remove_pending(git, 7, SHA)
    assert read_pending(git, 7) is None
    assert not remove_pending(git, 7, SHA)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"claim": "x", "pid": 1, "host": "h"}',
        '{"claim": "x", "pid": true, "host": "h", "started_at": "2026-11-02T09:00:00Z"}',
        '{"claim": "", "pid": 1, "host": "h", "started_at": "2026-11-02T09:00:00Z"}',
        '{"claim": "x", "pid": 1, "host": "h", "started_at": "yesterday"}',
    ],
)
def test_unreadable_record_counts_as_absent(git_world: GitWorld, content: str) -> None:
    git = git_world.clone("alice")
    path = pending_path(git, 7)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert read_pending(git, 7) is None
    assert not may_take_over(read_pending(git, 7), SHA, host="laptop", running=lambda _: False)


def test_may_take_over() -> None:
    dead = lambda _pid: False  # noqa: E731
    alive = lambda _pid: True  # noqa: E731
    assert may_take_over(RECORD, SHA, host="laptop", running=dead)
    assert not may_take_over(RECORD, SHA, host="laptop", running=alive)
    assert not may_take_over(RECORD, SHA, host="desktop", running=dead)
    assert not may_take_over(RECORD, "b" * 40, host="laptop", running=dead)
    assert not may_take_over(None, SHA, host="laptop", running=dead)


def test_pid_running(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pid_running(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not pid_running(child.pid)
    assert not pid_running(0) and not pid_running(-1)

    def denied(pid: int, sig: int) -> None:
        raise PermissionError(pid)

    monkeypatch.setattr(os, "kill", denied)
    assert pid_running(12345)  # another user's process is still running
