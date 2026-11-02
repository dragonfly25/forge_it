"""Tests for forge.core.pending: pending-claim records (ADR 0009 amendment)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.git import Git
from forge.core.pending import (
    MAX_RUN_AGE,
    UNKNOWN,
    PendingClaim,
    forge_dir,
    list_pending,
    lock_path,
    machine_id,
    machine_id_path,
    may_take_over,
    pending_path,
    pid_running,
    prune_orphans,
    read_pending,
    remove_pending,
    require_flock,
    run_alive,
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


def test_previous_tip_round_trip_and_older_records(git_world: GitWorld) -> None:
    """``previous`` is stored (a sha or null); a record without the key says UNKNOWN."""
    git = git_world.clone("alice")
    for previous in ("b" * 40, None):
        record = PendingClaim(7, SHA, 123, "laptop", T0, previous)
        write_pending(git, record)
        assert json.loads(pending_path(git, 7).read_text())["previous"] == previous
        assert read_pending(git, 7) == record
    legacy = {"claim": SHA, "pid": 123, "host": "laptop", "started_at": "2026-11-02T09:00:00Z"}
    pending_path(git, 7).write_text(json.dumps(legacy))
    older = read_pending(git, 7)
    assert older == RECORD and older.previous is UNKNOWN
    write_pending(git, RECORD)
    assert "previous" not in json.loads(pending_path(git, 7).read_text())


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
        '{"claim": "x", "pid": 1, "host": "h", "started_at": "2026-11-02T09:00:00Z", '
        '"previous": ""}',
        '{"claim": "x", "pid": 1, "host": "h", "started_at": "2026-11-02T09:00:00Z", '
        '"previous": 5}',
        '{"claim": "x", "pid": 1, "host": "h", "started_at": "2026-11-02T09:00:00Z", "other": 1}',
    ],
)
def test_unreadable_record_counts_as_absent(git_world: GitWorld, content: str) -> None:
    git = git_world.clone("alice")
    path = pending_path(git, 7)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert read_pending(git, 7) is None
    assert not may_take_over(
        read_pending(git, 7), SHA, host="laptop", running=lambda _: False, now=T0
    )


def test_may_take_over() -> None:
    dead = lambda _pid: False  # noqa: E731
    alive = lambda _pid: True  # noqa: E731
    assert may_take_over(RECORD, SHA, host="laptop", running=dead, now=T0)
    assert not may_take_over(RECORD, SHA, host="laptop", running=alive, now=T0)
    assert not may_take_over(RECORD, SHA, host="desktop", running=dead, now=T0)
    assert not may_take_over(RECORD, "b" * 40, host="laptop", running=dead, now=T0)
    assert not may_take_over(None, SHA, host="laptop", running=dead, now=T0)


def test_an_old_record_is_dead_whatever_its_pid() -> None:
    """The pid of a record older than MAX_RUN_AGE may have been reused: the run is gone."""
    alive = lambda _pid: True  # noqa: E731
    at_bound = T0 + MAX_RUN_AGE
    later = at_bound + timedelta(seconds=1)
    assert timedelta(minutes=15) == MAX_RUN_AGE
    assert run_alive(RECORD, host="laptop", running=alive, now=at_bound)
    assert not run_alive(RECORD, host="laptop", running=alive, now=later)
    # A record written by a clock that was ahead ages out the same way (ADR 0009).
    assert run_alive(RECORD, host="laptop", running=alive, now=T0 - timedelta(minutes=1))
    assert not run_alive(RECORD, host="laptop", running=alive, now=T0 - timedelta(hours=1))
    assert not run_alive(RECORD, host="desktop", running=alive, now=T0)
    assert not may_take_over(RECORD, SHA, host="laptop", running=alive, now=at_bound)
    assert may_take_over(RECORD, SHA, host="laptop", running=alive, now=later)
    assert not may_take_over(RECORD, SHA, host="desktop", running=alive, now=later)


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
        PendingClaim(6, SHA, live, here, T0 - MAX_RUN_AGE * 2),  # pid reused: pruned
    ]
    for record in records:
        write_pending(git, record)
    (pending_path(git, 9).parent / "task-9.json").write_text("garbage")
    claims = {1: SHA, 2: "b" * 40}
    with task_lock(git, 5) as held:
        assert held
        pruned = prune_orphans(git, claims, pid=0, host=here, running=lambda p: p == live, now=T0)
    assert pruned == [2, 4, 6]
    assert [r.issue for r in list_pending(git)] == [1, 3, 5]
    assert pending_path(git, 9).exists()  # unreadable: left alone


def test_task_lock_refuses_windows(git_world: GitWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """No O_EXCL fallback: on Windows claims stop with a clear precondition error."""
    git = git_world.clone("alice")
    require_flock("linux")
    require_flock("darwin")
    with pytest.raises(PreconditionError, match="claims need macOS, Linux or WSL") as info:
        require_flock("win32")
    assert info.value.code == ExitCode.PRECONDITION
    monkeypatch.setattr(sys, "platform", "win32")
    with (
        pytest.raises(PreconditionError, match="claims need macOS, Linux or WSL"),
        task_lock(git, 7),
    ):
        pass  # pragma: no cover - never entered
    assert not lock_path(git, 7).exists()


def test_machine_id_location_honours_xdg_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    assert machine_id_path({"XDG_STATE_HOME": str(state), "HOME": str(home)}) == (
        state / "forge" / "machine-id"
    )
    fallback = home / ".local" / "state" / "forge" / "machine-id"
    assert machine_id_path({"HOME": str(home)}) == fallback
    assert machine_id_path({"XDG_STATE_HOME": "", "HOME": str(home)}) == fallback
    assert machine_id_path({"XDG_STATE_HOME": "rel/state", "HOME": str(home)}) == fallback

    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    found = machine_id()
    assert (state / "forge" / "machine-id").read_text().strip() == found
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(home))
    assert machine_id() != found and fallback.read_text().strip() == machine_id()


def test_machine_id_is_kept_outside_the_clone(
    git_world: GitWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One clone, two state dirs (host and a container with its own home): two ids."""
    git = git_world.clone("alice")
    host_state, container_state = tmp_path / "host", tmp_path / "container"
    monkeypatch.setenv("XDG_STATE_HOME", str(host_state))
    on_host = machine_id()
    monkeypatch.setenv("XDG_STATE_HOME", str(container_state))
    in_container = machine_id()
    assert on_host != in_container
    assert not (forge_dir(git) / "machine-id").exists()
    assert not git.is_dirty(include_untracked=True)


def test_machine_id_is_created_once_and_shared(tmp_path: Path) -> None:
    path = machine_id_path({"XDG_STATE_HOME": str(tmp_path)})
    ids: list[str] = []
    threads = [threading.Thread(target=lambda: ids.append(machine_id(path))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(ids) == 8 and len(set(ids)) == 1 and len(ids[0]) == 32
    assert machine_id(path) == ids[0]
    assert sorted(p.name for p in path.parent.iterdir()) == ["machine-id"]  # no temp file left
    path.write_text("not-an-id\n")
    with pytest.raises(ForgeError, match="machine id"):
        machine_id(path)


def test_unwritable_state_dir_is_a_precondition_error(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o500)
    try:
        with pytest.raises(PreconditionError) as info:
            machine_id(state / "forge" / "machine-id")
    finally:
        state.chmod(0o700)
    assert info.value.code == ExitCode.PRECONDITION
    assert "XDG_STATE_HOME" in (info.value.hint or "")
