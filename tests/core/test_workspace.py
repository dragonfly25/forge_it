"""Tests for forge.core.workspace: branch + worktree preparation and current.json."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forge.core.errors import ConfigError, ExitCode, PreconditionError
from forge.core.git import Git
from forge.core.workspace import (
    CURRENT_FILE,
    LOCAL_GITIGNORE,
    CurrentTask,
    clear_current,
    ensure_local_dir,
    find_current_task,
    format_timestamp,
    parse_timestamp,
    prepare_workspace,
    read_current,
    task_branch_issue,
    worktree_path,
    write_current,
)

if TYPE_CHECKING:
    from conftest import GitWorld

N = 7
BRANCH = f"task/{N}"


def claim(git: Git, parent: str = "origin/main") -> str:
    """Create an empty claim commit on ``parent`` and push it to ``task/<N>``; return its sha."""
    sha = git.commit_tree(git.tree_of(parent), [git.rev_parse(parent) or ""], "forge: claim")
    result = git.push(sha, f"refs/heads/{BRANCH}")
    assert result.ok, result.stderr
    return sha


# -- paths -----------------------------------------------------------------------------------


def test_worktree_path_default_and_custom(tmp_path: Path) -> None:
    repo = tmp_path / "shop"
    assert worktree_path(repo, 42) == tmp_path / "shop-worktrees" / "task-42"
    assert worktree_path(repo, 42, tmp_path / "wt") == tmp_path / "wt" / "task-42"


def test_task_branch_issue() -> None:
    assert task_branch_issue("task/12") == 12
    for name in (None, "task/012", "task/x", "main", "task/1/a"):
        assert task_branch_issue(name) is None


# -- prepare_workspace: worktree mode ----------------------------------------------------------


def test_new_branch_in_worktree(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    ws = prepare_workspace(alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip)
    assert ws.path == worktree_path(alice.cwd, N)
    assert ws.branch_created and ws.worktree_created and not ws.worktree_reused
    wt = Git(ws.path)
    assert wt.current_branch() == BRANCH
    assert wt.rev_parse("HEAD") == tip
    assert alice.config_get(f"branch.{BRANCH}.remote") == "origin"
    assert alice.config_get(f"branch.{BRANCH}.merge") == f"refs/heads/{BRANCH}"
    # .forge/local exists with its own .gitignore and git status stays clean
    assert (ws.path / ".forge/local/.gitignore").read_text() == LOCAL_GITIGNORE
    (ws.path / ".forge/local/scratch.txt").write_text("x")
    assert not wt.is_dirty(include_untracked=True)


def test_reuse_worktree_is_idempotent(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    prepare_workspace(alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip)
    again = prepare_workspace(alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip)
    assert again.worktree_reused and not again.worktree_created
    assert not again.branch_created and not again.branch_fast_forwarded


def test_reused_worktree_is_fast_forwarded_inside(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    old = claim(alice)
    ws = prepare_workspace(alice, repo_root=alice.cwd, issue_number=N, claim_tip=old)
    # a new claim lands on top (e.g. a resume)
    new = alice.commit_tree(alice.tree_of(old), [old], "forge: claim again")
    (ws.path / "notes.txt").write_text("untracked work survives\n")
    again = prepare_workspace(alice, repo_root=alice.cwd, issue_number=N, claim_tip=new)
    assert again.worktree_reused and again.branch_fast_forwarded
    assert Git(ws.path).rev_parse("HEAD") == new
    assert not Git(ws.path).is_dirty(include_untracked=False)
    assert (ws.path / "notes.txt").exists()


def test_existing_branch_not_checked_out_is_fast_forwarded(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    base = alice.rev_parse("origin/main")
    assert base is not None
    alice.run("branch", BRANCH, base)
    ws = prepare_workspace(alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip)
    assert ws.branch_fast_forwarded and not ws.branch_created
    assert alice.rev_parse(BRANCH) == tip


def test_diverged_branch_exits_5_and_is_untouched(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    alice.checkout(BRANCH, create_from="origin/main")
    local = git_world.commit_file(alice, "local.txt", "x\n", "feat: local only")
    alice.checkout("main")
    with pytest.raises(PreconditionError) as exc:
        prepare_workspace(alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip)
    assert exc.value.code == ExitCode.PRECONDITION
    assert "diverged" in exc.value.message and exc.value.hint
    assert alice.rev_parse(BRANCH) == local
    assert not worktree_path(alice.cwd, N).exists()


def test_occupied_path_exits_5(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    path = worktree_path(alice.cwd, N)
    path.mkdir(parents=True)
    (path / "junk.txt").write_text("x")
    with pytest.raises(PreconditionError, match="already exists"):
        prepare_workspace(alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip)
    assert not alice.branch_exists(BRANCH)


def test_empty_directory_at_path_is_used(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    path = worktree_path(alice.cwd, N)
    path.mkdir(parents=True)
    ws = prepare_workspace(alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip)
    assert ws.worktree_created and Git(path).current_branch() == BRANCH


def test_branch_checked_out_elsewhere_exits_5(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    alice.checkout(BRANCH, create_from=tip)
    with pytest.raises(PreconditionError, match="already checked out"):
        prepare_workspace(alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip)


def test_custom_worktrees_dir(git_world: GitWorld, tmp_path: Path) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    ws = prepare_workspace(
        alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip, worktrees_dir=tmp_path / "wt"
    )
    assert ws.path == tmp_path / "wt" / f"task-{N}"


def test_two_users_get_independent_workspaces(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    bob = git_world.clone("bob")
    tip = claim(alice)
    bob.fetch()
    a = prepare_workspace(alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip)
    b = prepare_workspace(bob, repo_root=bob.cwd, issue_number=N, claim_tip=tip)
    assert a.path != b.path
    assert Git(b.path).rev_parse("HEAD") == tip


# -- prepare_workspace: in place ---------------------------------------------------------------


def test_in_place_checkout(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    ws = prepare_workspace(
        alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip, use_worktree=False
    )
    assert ws.in_place and ws.path == alice.cwd and ws.branch_created
    assert alice.current_branch() == BRANCH
    assert alice.rev_parse("HEAD") == tip
    assert not alice.is_dirty(include_untracked=True)
    assert not worktree_path(alice.cwd, N).exists()


def test_in_place_dirty_tree_exits_5(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    (alice.cwd / "README.md").write_text("changed\n")
    with pytest.raises(PreconditionError, match="uncommitted") as exc:
        prepare_workspace(
            alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip, use_worktree=False
        )
    assert exc.value.code == ExitCode.PRECONDITION
    assert alice.current_branch() == "main"
    assert not alice.branch_exists(BRANCH)


def test_in_place_already_on_branch_and_dirty_needs_no_move(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    alice.checkout(BRANCH, create_from=tip)
    (alice.cwd / "wip.txt").write_text("x")
    ws = prepare_workspace(
        alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip, use_worktree=False
    )
    assert not ws.branch_created and not ws.branch_fast_forwarded


def test_in_place_fast_forward_on_checked_out_branch(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    old = claim(alice)
    alice.checkout(BRANCH, create_from=old)
    new = alice.commit_tree(alice.tree_of(old), [old], "forge: claim again")
    ws = prepare_workspace(
        alice, repo_root=alice.cwd, issue_number=N, claim_tip=new, use_worktree=False
    )
    assert ws.branch_fast_forwarded
    assert alice.rev_parse("HEAD") == new


# -- current.json ----------------------------------------------------------------------------


def current(worktree: Path | None = None) -> CurrentTask:
    return CurrentTask(
        task="M1-T07",
        issue=N,
        branch=BRANCH,
        base="a" * 40,
        claim="b" * 40,
        agent="claude-1",
        claimed_at=datetime(2026, 11, 2, 9, 30, tzinfo=UTC),
        worktree=worktree,
    )


def test_timestamps() -> None:
    cet = timezone(timedelta(hours=1))
    assert format_timestamp(datetime(2026, 1, 1, 10, 0, tzinfo=cet)) == "2026-01-01T09:00:00Z"
    assert parse_timestamp("2026-01-01T09:00:00Z") == datetime(2026, 1, 1, 9, tzinfo=UTC)
    with pytest.raises(ValueError):
        format_timestamp(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        parse_timestamp("2026-01-01T09:00:00+01:00")


def test_current_round_trip_and_clear(tmp_path: Path) -> None:
    assert read_current(tmp_path) is None
    cur = current(tmp_path / "wt")
    file = write_current(tmp_path, cur)
    assert file == tmp_path / CURRENT_FILE
    data = json.loads(file.read_text())
    assert data["claimed_at"] == "2026-11-02T09:30:00Z"
    assert list(data) == [
        "task",
        "issue",
        "branch",
        "base",
        "claim",
        "agent",
        "claimed_at",
        "worktree",
    ]
    assert read_current(tmp_path) == cur
    assert (tmp_path / ".forge/local/.gitignore").read_text() == "*\n"
    assert [p.name for p in file.parent.iterdir() if p.suffix == ".tmp"] == []
    assert clear_current(tmp_path) is True
    assert clear_current(tmp_path) is False
    assert read_current(tmp_path) is None


def test_current_null_worktree_round_trip(tmp_path: Path) -> None:
    write_current(tmp_path, current(None))
    assert read_current(tmp_path) == current(None)


@pytest.mark.parametrize(
    ("mutate", "key"),
    [
        (lambda d: d.pop("claim"), "claim"),
        (lambda d: d.update(extra=1), "extra"),
        (lambda d: d.update(task="T7"), "task"),
        (lambda d: d.update(issue="7"), "issue"),
        (lambda d: d.update(issue=True), "issue"),
        (lambda d: d.update(branch="task/8"), "branch"),
        (lambda d: d.update(base="abc"), "base"),
        (lambda d: d.update(claimed_at="yesterday"), "claimed_at"),
        (lambda d: d.update(worktree=3), "worktree"),
    ],
)
def test_current_invalid_field(tmp_path: Path, mutate: object, key: str) -> None:
    file = write_current(tmp_path, current(None))
    data = json.loads(file.read_text())
    mutate(data)  # type: ignore[operator]
    file.write_text(json.dumps(data))
    with pytest.raises(ConfigError) as exc:
        read_current(tmp_path)
    assert exc.value.key == key
    assert str(file) in str(exc.value) and "delete the file" in str(exc.value)


def test_current_not_json(tmp_path: Path) -> None:
    ensure_local_dir(tmp_path)
    (tmp_path / CURRENT_FILE).write_text("{not json")
    with pytest.raises(ConfigError, match="invalid JSON"):
        read_current(tmp_path)
    (tmp_path / CURRENT_FILE).write_text("[]")
    with pytest.raises(ConfigError, match="JSON object"):
        read_current(tmp_path)


# -- find_current_task -----------------------------------------------------------------------


def test_find_from_current_json_in_worktree(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    ws = prepare_workspace(alice, repo_root=alice.cwd, issue_number=N, claim_tip=tip)
    write_current(ws.path, current(ws.path))
    sub = ws.path / "src"
    sub.mkdir()
    loc = find_current_task(alice, sub)
    assert loc is not None and loc.source == "current"
    assert loc.task_id == "M1-T07" and loc.issue == N and loc.branch == BRANCH
    assert Path(loc.workspace).resolve() == ws.path.resolve()
    # the main checkout has no current task
    assert find_current_task(alice) is None


def test_find_from_branch_name(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    alice.checkout(BRANCH, create_from=tip)
    loc = find_current_task(alice)
    assert loc is not None and loc.source == "branch"
    assert loc.issue == N and loc.task_id is None and loc.current is None


def test_find_ignores_current_json_for_another_branch(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = claim(alice)
    write_current(alice.cwd, current(None))
    alice.checkout("task/9", create_from=tip)
    loc = find_current_task(alice)
    assert loc is not None and loc.source == "branch" and loc.issue == 9
    assert loc.notes and "ignored" in loc.notes[0]
    alice.checkout("main")
    assert find_current_task(alice) is None


def test_find_corrupt_current_raises(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    ensure_local_dir(alice.cwd)
    (alice.cwd / CURRENT_FILE).write_text("oops")
    with pytest.raises(ConfigError):
        find_current_task(alice)
