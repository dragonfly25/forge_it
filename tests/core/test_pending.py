"""Tests for forge.core.pending: pending-claim records (ADR 0009 amendment)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from forge.core.errors import ForgeError
from forge.core.git import Git
from forge.core.pending import (
    PendingClaim,
    list_pending,
    lock_path,
    machine_id,
    may_take_over,
    pending_path,
    pid_running,
    prune_orphans,
    read_pending,
    remove_pending,
    task_lock,
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


def test_task_lock_is_exclusive_and_removed_on_exit(git_world: GitWorld) -> None:
    git = git_world.clone("alice")
    path = lock_path(git, 7)
    with task_lock(git, 7, pid=11, host="m") as held:
        assert held
        assert json.loads(path.read_text()) == {"pid": 11, "host": "m"}
        with task_lock(git, 7, timeout=0) as again:
            assert not again  # a second holder in the same process is refused too
        with task_lock(git, 8, timeout=0) as other:
            assert other  # one lock per task
        assert path.exists()  # the refused attempt did not remove the holder's file
    assert not path.exists()
    with task_lock(git, 7, timeout=0) as held:
        assert held


def test_task_lock_left_by_a_dead_process_is_free(git_world: GitWorld) -> None:
    """A crashed holder leaves its file, but the kernel released its flock."""
    git = git_world.clone("alice")
    script = (
        "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
        "fcntl.flock(fd, fcntl.LOCK_EX); os.write(fd, b'{\"pid\": 1}'); os._exit(9)"
    )
    path = lock_path(git, 7)
    path.parent.mkdir(parents=True)
    subprocess.run([sys.executable, "-c", script, str(path)], check=False)
    assert path.exists()
    with task_lock(git, 7, timeout=0) as held:
        assert held


def test_task_lock_waiters_get_it_one_at_a_time(git_world: GitWorld) -> None:
    git = git_world.clone("alice")
    inside: list[int] = []
    overlaps: list[int] = []
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for _ in range(20):
                with task_lock(git, 7, pid=i, timeout=10) as held:
                    assert held
                    if inside:
                        overlaps.append(i)
                    inside.append(i)
                    inside.remove(i)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors and not overlaps


def test_remove_pending_leaves_a_locked_record_alone(git_world: GitWorld) -> None:
    git = git_world.clone("alice")
    write_pending(git, RECORD)
    with task_lock(git, 7) as held:
        assert held
        assert not remove_pending(git, 7, SHA, timeout=0)
    assert read_pending(git, 7) == RECORD


def test_prune_orphans(git_world: GitWorld) -> None:
    git = git_world.clone("alice")
    live, dead, here, there = 1, 2, "laptop", "desktop"
    records = [
        PendingClaim(1, SHA, dead, here, T0),  # still the remote claim: kept
        PendingClaim(2, SHA, dead, here, T0),  # dead, branch has another claim: pruned
        PendingClaim(3, SHA, live, here, T0),  # live run on this machine: kept
        PendingClaim(4, SHA, live, there, T0),  # another machine: pruned
        PendingClaim(5, SHA, dead, here, T0),  # dead but its lock is busy: kept
    ]
    for record in records:
        write_pending(git, record)
    (pending_path(git, 9).parent / "task-9.json").write_text("garbage")
    claims = {1: SHA, 2: "b" * 40}
    with task_lock(git, 5) as held:
        assert held
        pruned = prune_orphans(git, claims, pid=0, host=here, running=lambda p: p == live)
    assert pruned == [2, 4]
    assert [r.issue for r in list_pending(git)] == [1, 3, 5]
    assert pending_path(git, 9).exists()  # unreadable: left alone


def test_machine_id_is_created_once_and_shared(git_world: GitWorld) -> None:
    git = git_world.clone("alice")
    ids: list[str] = []
    threads = [threading.Thread(target=lambda: ids.append(machine_id(git))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(ids) == 8 and len(set(ids)) == 1 and len(ids[0]) == 32
    worktree = git.cwd.parent / "alice-wt"
    git.worktree_add(worktree, "side", start_point="origin/main")
    assert machine_id(Git(worktree)) == ids[0]
    assert machine_id(git_world.clone("bob")) != ids[0]  # per clone
    folder = pending_path(git, 1).parent.parent
    assert sorted(p.name for p in folder.iterdir()) == ["machine-id"]  # no temp file left
    (folder / "machine-id").write_text("not-an-id\n")
    with pytest.raises(ForgeError, match="machine id"):
        machine_id(git)
