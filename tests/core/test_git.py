"""Tests for forge.core.git against real temporary repositories."""

from __future__ import annotations

import stat
import subprocess
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forge.core.errors import ForgeError
from forge.core.git import (
    CommitFiles,
    Git,
    GitAddFailed,
    GitError,
    Lease,
    PushHookRejected,
    parse_version,
)

if TYPE_CHECKING:
    from conftest import GitWorld


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("git version 2.39.3 (Apple Git-146)\n", (2, 39, 3)),
        ("git version 2.43.0\n", (2, 43, 0)),
        ("git version 2.42.0.windows.2\n", (2, 42, 0)),
        ("git version 2.40\n", (2, 40, 0)),
    ],
)
def test_parse_version(text: str, expected: tuple[int, int, int]) -> None:
    assert parse_version(text) == expected


def test_parse_version_rejects_garbage() -> None:
    with pytest.raises(GitError):
        parse_version("hello")


def test_installed_version() -> None:
    assert Git.version() >= (2, 0, 0)


def test_basics(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    assert alice.root().resolve() == alice.cwd.resolve()
    assert alice.current_branch() == "main"
    assert alice.config_get("user.email") == "alice@example.test"
    assert alice.config_get("forge.missing") is None
    alice.config_set("rerere.enabled", "true")
    assert alice.config_get("rerere.enabled") == "true"
    assert alice.branch_exists("main")
    assert not alice.branch_exists("task/1")

    alice.run("checkout", "--quiet", "--detach")
    assert alice.current_branch() is None


def test_rev_parse(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    assert alice.rev_parse("HEAD") == git_world.initial_sha
    assert alice.rev_parse("origin/main") == git_world.initial_sha
    assert alice.rev_parse("refs/heads/nope") is None
    assert alice.rev_parse("0" * 40) is None
    assert alice.rev_parse("HEAD^{tree}") is None  # exists, but is not a commit


def test_rev_parse_outside_a_repository_raises(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError) as excinfo:
        Git(plain).rev_parse("HEAD")
    assert excinfo.value.returncode == 128
    assert "not a git repository" in excinfo.value.stderr


def test_is_ancestor(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    child = git_world.commit_file(alice, "a.txt", "a\n", "feat: a")
    base = git_world.initial_sha
    assert alice.is_ancestor(base, child)
    assert alice.is_ancestor(child, child)
    assert not alice.is_ancestor(child, base)
    alice.checkout("side", create_from=base)
    side = git_world.commit_file(alice, "b.txt", "b\n", "feat: b")
    assert not alice.is_ancestor(side, child)
    assert not alice.is_ancestor(child, side)
    with pytest.raises(GitError):
        alice.is_ancestor("1" * 40, child)
    with pytest.raises(GitError):
        alice.is_ancestor("refs/heads/nope", child)


def test_is_dirty(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    assert not alice.is_dirty()
    (alice.cwd / "new.txt").write_text("x\n")
    assert alice.is_dirty()
    assert not alice.is_dirty(include_untracked=False)
    (alice.cwd / "README.md").write_text("changed\n")
    assert alice.is_dirty(include_untracked=False)


def test_commit_with_trailers_and_commits(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    sha = alice.commit(
        "forge: claim M1-T07",
        allow_empty=True,
        trailers={"Forge-Claim": "alice", "Forge-Agent": "claude-1", "Forge-Task": "M1-T07"},
    )
    commits = alice.commits("origin/main..HEAD")
    assert len(commits) == 1
    info = commits[0]
    assert info.sha == sha
    assert info.author == "alice"
    assert info.author_email == "alice@example.test"
    assert info.subject == "forge: claim M1-T07"
    assert info.trailers == {
        "Forge-Claim": "alice",
        "Forge-Agent": "claude-1",
        "Forge-Task": "M1-T07",
    }
    assert info.date.tzinfo is not None
    assert alice.commit_date(sha) == info.date
    assert isinstance(alice.commit_date("HEAD"), datetime)
    # The seed commit has no trailers.
    assert alice.commits("HEAD~1")[0].trailers == {}


def test_commit_without_changes_fails(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    with pytest.raises(GitError):
        alice.commit("nothing")


def test_commit_tree_does_not_move_head(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    head = alice.rev_parse("HEAD")
    assert head is not None
    sha = alice.commit_tree(
        alice.tree_of(head), [head], "forge: claim M1-T01", trailers={"Forge-Task": "M1-T01"}
    )
    assert alice.rev_parse("HEAD") == head
    assert not alice.is_dirty()
    info = alice.commits(sha)[0]
    assert info.trailers == {"Forge-Task": "M1-T01"}
    assert alice.run("rev-parse", f"{sha}^").stdout.strip() == head
    assert alice.tree_of(sha) == alice.tree_of(head)


def _claim_commit(git: Git, user: str) -> str:
    base = git.rev_parse("origin/main")
    assert base is not None
    return git.commit_tree(git.tree_of(base), [base], "forge: claim", {"Forge-Claim": user})


def test_push_new_ref_lease_exactly_one_wins(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    bob = git_world.clone("bob")
    ref = "refs/heads/task/1"
    a_sha = _claim_commit(alice, "alice")
    b_sha = _claim_commit(bob, "bob")

    first = alice.push(a_sha, ref, lease=Lease(ref, None))
    assert first.ok and not first.rejected
    second = bob.push(b_sha, ref, lease=Lease(ref, None))
    assert not second.ok and second.rejected
    assert bob.remote_branch_sha("task/1") == a_sha


def test_push_new_ref_concurrent_race(git_world: GitWorld) -> None:
    users = [git_world.clone(name) for name in ("alice", "bob", "carol")]
    ref = "refs/heads/task/7"
    shas = [_claim_commit(git, git.cwd.name) for git in users]
    barrier = threading.Barrier(len(users))
    results: dict[int, bool] = {}

    def attempt(index: int) -> None:
        barrier.wait()
        results[index] = users[index].push(shas[index], ref, lease=Lease(ref, None)).ok

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(len(users))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [i for i, ok in results.items() if ok]
    assert len(results) == len(users)
    assert len(winners) == 1
    assert git_world.remote_git.rev_parse(ref) == shas[winners[0]]


def test_push_with_expected_sha(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    bob = git_world.clone("bob")
    ref = "refs/heads/task/2"
    first = _claim_commit(alice, "alice")
    assert alice.push(first, ref, lease=Lease(ref, None)).ok

    # Bob resumes from the current tip: matching expected sha wins.
    bob.fetch()
    resumed = bob.commit_tree(bob.tree_of(first), [first], "forge: claim", {"Forge-Claim": "bob"})
    assert bob.push(resumed, ref, lease=Lease(ref, first)).ok

    # Alice still believes the tip is `first`: her lease is stale.
    again = alice.commit_tree(alice.tree_of(first), [first], "forge: claim")
    result = alice.push(again, ref, lease=Lease(ref, first))
    assert result.rejected and not result.ok
    assert git_world.remote_git.rev_parse(ref) == resumed


def test_plain_push_non_fast_forward_is_rejected(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    bob = git_world.clone("bob")
    git_world.commit_file(alice, "a.txt", "a\n", "feat: a")
    git_world.push(alice, "main")
    git_world.commit_file(bob, "b.txt", "b\n", "feat: b")
    result = bob.push("HEAD", "refs/heads/main")
    assert result.rejected and not result.ok


def test_delete_remote_branch_with_lease(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    ref = "refs/heads/task/3"
    sha = _claim_commit(alice, "alice")
    assert alice.push(sha, ref, lease=Lease(ref, None)).ok

    stale = alice.delete_remote_branch("task/3", git_world.initial_sha)
    assert stale.rejected
    assert alice.remote_branch_sha("task/3") == sha

    assert alice.delete_remote_branch("task/3", sha).ok
    assert alice.remote_branch_sha("task/3") is None


def test_lease_ref_must_match_destination(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    sha = _claim_commit(alice, "alice")
    with pytest.raises(ValueError, match="does not match"):
        alice.push(sha, "refs/heads/task/1", lease=Lease("refs/heads/task/2", None))
    assert git_world.remote_git.rev_parse("refs/heads/task/1") is None
    assert git_world.remote_git.rev_parse("refs/heads/task/2") is None


# ---------------------------------------------------------------- server-side outcomes
#
# A ``pre-receive`` hook on the bare remote runs after the client-side lease check and before
# the server locks the ref, so it can deterministically reproduce "another push won in
# between" (it moves the ref itself) as well as permanent server-side rejections.


def _install_pre_receive(git_world: GitWorld, body: str) -> None:
    hook = git_world.remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _hook_moves_ref(git_world: GitWorld, ref: str, new_value: str | None) -> None:
    """Install a hook that sets ``ref`` to ``new_value`` (None deletes it) while a push runs."""
    command = f"git update-ref {ref} {new_value}" if new_value else f"git update-ref -d {ref}"
    _install_pre_receive(
        git_world,
        # Ref updates are forbidden inside the quarantine environment of a pre-receive hook.
        "unset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES\n"
        "while read old new ref; do\n"
        f'  if [ "$ref" = "{ref}" ]; then {command} || exit 3; fi\n'
        "done\n"
        "exit 0\n",
    )


def _remote_commit(git_world: GitWorld, user: str, message: str) -> str:
    """Create a commit that exists only on the remote (under ``refs/heads/<user>-scratch``)."""
    other = git_world.clone(user)
    sha = git_world.commit_file(other, f"{user}.txt", f"{user}\n", message)
    assert other.push(sha, f"refs/heads/{user}-scratch").ok
    return sha


def test_server_side_create_race_is_rejected(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    ref = "refs/heads/task/42"
    winner = _remote_commit(git_world, "bob", "forge: claim")
    _hook_moves_ref(git_world, ref, winner)

    result = alice.push(_claim_commit(alice, "alice"), ref, lease=Lease(ref, None))
    assert not result.ok and result.rejected
    assert "[remote rejected]" in result.reason
    assert winner in result.reason
    assert git_world.remote_git.rev_parse(ref) == winner


def test_server_side_update_race_is_rejected(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    ref = "refs/heads/task/43"
    first = _claim_commit(alice, "alice")
    assert alice.push(first, ref, lease=Lease(ref, None)).ok
    winner = _remote_commit(git_world, "bob", "forge: claim")
    _hook_moves_ref(git_world, ref, winner)

    resumed = alice.commit_tree(alice.tree_of(first), [first], "forge: claim")
    result = alice.push(resumed, ref, lease=Lease(ref, first))
    assert not result.ok and result.rejected
    assert "[remote rejected]" in result.reason
    assert git_world.remote_git.rev_parse(ref) == winner


def test_server_side_race_on_plain_push_is_rejected(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    winner = _remote_commit(git_world, "bob", "feat: bob")
    _hook_moves_ref(git_world, "refs/heads/main", winner)
    git_world.commit_file(alice, "a.txt", "a\n", "feat: a")

    result = alice.push("HEAD", "refs/heads/main")
    assert not result.ok and result.rejected
    assert "[remote rejected]" in result.reason
    assert git_world.remote_git.rev_parse("refs/heads/main") == winner


@pytest.mark.parametrize("use_lease", [True, False])
def test_directory_file_ref_clash_raises(git_world: GitWorld, use_lease: bool) -> None:
    alice = git_world.clone("alice")
    # A stray branch named `task` makes `refs/heads/task/42` impossible to create.
    git_world.remote_git.update_ref("refs/heads/task", git_world.initial_sha)
    ref = "refs/heads/task/42"
    lease = Lease(ref, None) if use_lease else None

    with pytest.raises(GitError) as excinfo:
        alice.push(_claim_commit(alice, "alice"), ref, lease=lease)
    assert "[remote rejected]" in excinfo.value.stderr
    assert git_world.remote_git.rev_parse(ref) is None
    assert git_world.remote_git.rev_parse("refs/heads/task") == git_world.initial_sha


def test_policy_hook_rejection_raises(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    _install_pre_receive(git_world, "echo 'policy: task branches are frozen' >&2\nexit 1\n")
    ref = "refs/heads/task/44"

    with pytest.raises(GitError) as excinfo:
        alice.push(_claim_commit(alice, "alice"), ref, lease=Lease(ref, None))
    assert "policy: task branches are frozen" in excinfo.value.stderr
    assert "pre-receive hook declined" in excinfo.value.stderr
    assert git_world.remote_git.rev_parse(ref) is None


def test_policy_hook_rejection_of_update_raises(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    ref = "refs/heads/task/45"
    first = _claim_commit(alice, "alice")
    assert alice.push(first, ref, lease=Lease(ref, None)).ok
    _install_pre_receive(git_world, "echo 'policy: no' >&2\nexit 1\n")

    resumed = alice.commit_tree(alice.tree_of(first), [first], "forge: claim")
    with pytest.raises(GitError):
        alice.push(resumed, ref, lease=Lease(ref, first))
    with pytest.raises(GitError):
        alice.delete_remote_branch("task/45", first)
    assert git_world.remote_git.rev_parse(ref) == first


def test_delete_with_lease_server_side_race_is_rejected(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    ref = "refs/heads/task/46"
    mine = _claim_commit(alice, "alice")
    assert alice.push(mine, ref, lease=Lease(ref, None)).ok
    winner = _remote_commit(git_world, "bob", "forge: claim")
    _hook_moves_ref(git_world, ref, winner)

    result = alice.delete_remote_branch("task/46", mine)
    assert not result.ok and result.rejected
    assert "[remote rejected]" in result.reason
    assert git_world.remote_git.rev_parse(ref) == winner


def test_delete_with_lease_already_gone_is_ok(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    ref = "refs/heads/task/47"
    mine = _claim_commit(alice, "alice")
    assert alice.push(mine, ref, lease=Lease(ref, None)).ok
    _hook_moves_ref(git_world, ref, None)

    # The server reports an error, but the ref is in the state the delete wanted.
    result = alice.delete_remote_branch("task/47", mine)
    assert result.ok and not result.rejected
    assert "[remote rejected]" in result.reason
    assert git_world.remote_git.rev_parse(ref) is None
    # A re-run leaves the ref absent; the stale lease is reported as a local rejection.
    _install_pre_receive(git_world, "exit 0\n")
    rerun = alice.delete_remote_branch("task/47", mine)
    assert rerun.rejected and rerun.reason.startswith("[rejected]")
    assert git_world.remote_git.rev_parse(ref) is None


class _ScriptedPush(Git):
    """A :class:`Git` whose ``push`` subprocess returns a scripted porcelain rejection."""

    def __init__(self, cwd: Path, summary: str):
        super().__init__(cwd)
        self.summary = summary

    def run(
        self,
        *args: str,
        check: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[:1] != ("push",):
            return super().run(*args, check=check, input=input, env=env)
        refspec = args[-1]
        stdout = f"To origin\n!\t{refspec}\t{self.summary}\nDone\n"
        return subprocess.CompletedProcess(["git", *args], 1, stdout, "error: failed to push\n")


def test_unknown_remote_wording_is_classified_by_remote_state(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    ref = "refs/heads/task/48"
    mine = _claim_commit(alice, "alice")
    scripted = _ScriptedPush(alice.cwd, "[remote rejected] (incorrect old value provided)")

    # Ref unchanged on the remote: not a race, so the unknown wording raises.
    with pytest.raises(GitError):
        scripted.push(mine, ref, lease=Lease(ref, None))

    # Somebody else created the ref: the same wording is a lost race.
    winner = _remote_commit(git_world, "bob", "forge: claim")
    git_world.remote_git.update_ref(ref, winner)
    result = scripted.push(mine, ref, lease=Lease(ref, None))
    assert result.rejected and not result.ok

    # The ref already holds the value we pushed: the push landed.
    git_world.remote_git.update_ref(ref, winner)
    landed = scripted.push(winner, ref, lease=Lease(ref, None))
    assert landed.ok and not landed.rejected


def test_push_unknown_remote_raises(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    with pytest.raises(GitError) as excinfo:
        alice.push("HEAD", "refs/heads/x", remote="nowhere")
    assert excinfo.value.returncode != 0
    assert excinfo.value.stderr


def test_fetch_and_ls_remote(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    bob = git_world.clone("bob")
    for n in (4, 5):
        ref = f"refs/heads/task/{n}"
        assert alice.push(_claim_commit(alice, "alice"), ref, lease=Lease(ref, None)).ok

    refs = bob.ls_remote("origin", "refs/heads/task/*")
    assert set(refs) == {"refs/heads/task/4", "refs/heads/task/5"}
    assert "refs/heads/main" in bob.ls_remote()

    assert bob.rev_parse("origin/task/4") is None
    bob.fetch()
    assert bob.rev_parse("origin/task/4") == refs["refs/heads/task/4"]

    assert alice.delete_remote_branch("task/4", refs["refs/heads/task/4"]).ok
    bob.fetch()  # prune removes the deleted remote-tracking branch
    assert bob.rev_parse("origin/task/4") is None


def test_diff_names_and_changed_files_since(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    bob = git_world.clone("bob")
    bob.checkout("task/1", create_from="origin/main")
    git_world.commit_file(bob, "src/b.py", "b\n", "feat: b")

    git_world.commit_file(alice, "docs/a.md", "a\n", "docs: a")
    git_world.push(alice, "main")
    bob.fetch()

    base = bob.merge_base("origin/main", "HEAD")
    assert base == git_world.initial_sha
    assert sorted(bob.diff_names("origin/main", "HEAD")) == ["docs/a.md", "src/b.py"]
    assert bob.changed_files_since("origin/main", "HEAD") == ["src/b.py"]
    assert bob.changed_files_since("HEAD", "origin/main") == ["docs/a.md"]


def test_rebase_clean(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    bob = git_world.clone("bob")
    git_world.commit_file(bob, "b.txt", "b\n", "feat: b")
    git_world.commit_file(alice, "a.txt", "a\n", "feat: a")
    git_world.push(alice, "main")
    bob.fetch()

    result = bob.rebase("origin/main")
    assert result.ok and result.conflicts == []
    assert not bob.rebase_in_progress()
    assert bob.merge_base("HEAD", "origin/main") == bob.rev_parse("origin/main")


def _conflicting_setup(git_world: GitWorld) -> tuple[Git, str]:
    alice = git_world.clone("alice")
    bob = git_world.clone("bob")
    git_world.commit_file(alice, "README.md", "alice\n", "feat: alice")
    git_world.push(alice, "main")
    before = git_world.commit_file(bob, "README.md", "bob\n", "feat: bob")
    bob.fetch()
    return bob, before


def test_rebase_conflict_then_continue(git_world: GitWorld) -> None:
    bob, _ = _conflicting_setup(git_world)
    result = bob.rebase("origin/main")
    assert not result.ok
    assert result.conflicts == ["README.md"]
    assert bob.rebase_in_progress()
    assert bob.conflicted_files() == ["README.md"]

    # Not resolved yet: continue stops again.
    assert not bob.rebase_continue().ok

    (bob.cwd / "README.md").write_text("alice\nbob\n")
    bob.add(["README.md"])
    assert bob.rebase_continue().ok
    assert not bob.rebase_in_progress()
    assert bob.current_branch() == "main"
    assert bob.commits("origin/main..HEAD")[0].subject == "feat: bob"


def test_rebase_conflict_then_abort(git_world: GitWorld) -> None:
    bob, before = _conflicting_setup(git_world)
    assert not bob.rebase("origin/main").ok
    bob.rebase_abort()
    assert not bob.rebase_in_progress()
    assert bob.rev_parse("HEAD") == before
    assert bob.current_branch() == "main"


def test_rebase_unknown_upstream_raises(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    with pytest.raises(GitError):
        alice.rebase("origin/nope")


def test_worktrees(git_world: GitWorld, tmp_path: Path) -> None:
    alice = git_world.clone("alice")
    wt = tmp_path / "wts" / "task-1"
    alice.worktree_add(wt, "task/1", start_point="origin/main")
    paths = [p.resolve() for p in alice.worktree_list()]
    assert paths == [alice.cwd.resolve(), wt.resolve()]

    wt_git = Git(wt)
    assert wt_git.current_branch() == "task/1"
    (wt / "dirty.txt").write_text("x\n")
    with pytest.raises(GitError):
        alice.worktree_remove(wt)
    alice.worktree_remove(wt, force=True)
    assert [p.resolve() for p in alice.worktree_list()] == [alice.cwd.resolve()]

    # Existing branch, no start point.
    wt2 = tmp_path / "wts" / "task-1b"
    alice.worktree_add(wt2, "task/1")
    assert Git(wt2).current_branch() == "task/1"


def test_checkout_and_reset_hard(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    alice.checkout("task/9", create_from="origin/main")
    assert alice.current_branch() == "task/9"
    git_world.commit_file(alice, "x.txt", "x\n", "feat: x")
    (alice.cwd / "README.md").write_text("dirty\n")
    alice.reset_hard(git_world.initial_sha)
    assert alice.rev_parse("HEAD") == git_world.initial_sha
    assert not alice.is_dirty()
    alice.checkout("main")
    assert alice.current_branch() == "main"


def test_backup_refs(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    sha = git_world.commit_file(alice, "x.txt", "x\n", "feat: x")
    alice.update_ref("refs/forge/backup/task-1/20261102T100000Z", git_world.initial_sha)
    alice.update_ref("refs/forge/backup/task-1/20261102T110000Z", sha)
    alice.update_ref("refs/forge/backup/task-2/20261102T110000Z", sha)

    refs = alice.list_refs("refs/forge/backup/task-1/")
    assert refs == {
        "refs/forge/backup/task-1/20261102T100000Z": git_world.initial_sha,
        "refs/forge/backup/task-1/20261102T110000Z": sha,
    }
    alice.delete_ref("refs/forge/backup/task-1/20261102T100000Z")
    assert list(alice.list_refs("refs/forge/backup/task-1/")) == [
        "refs/forge/backup/task-1/20261102T110000Z"
    ]
    assert alice.list_refs("refs/forge/none/") == {}


def test_git_error_carries_details(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(GitError) as excinfo:
        Git(not_a_repo).run("status")
    err = excinfo.value
    assert isinstance(err, ForgeError)
    assert err.git_args == ("status",)
    assert err.returncode != 0
    assert "not a git repository" in err.stderr
    assert "git status failed" in str(err)

    proc = Git(not_a_repo).run("status", check=False)
    assert proc.returncode != 0


def test_output_is_english_under_italian_locale(
    monkeypatch: pytest.MonkeyPatch, git_world: GitWorld, tmp_path: Path
) -> None:
    monkeypatch.setenv("LANG", "it_IT.UTF-8")
    monkeypatch.setenv("LC_ALL", "it_IT.UTF-8")
    monkeypatch.setenv("LC_MESSAGES", "it_IT.UTF-8")
    assert Git.env()["LC_ALL"] == "C"
    assert Git.env()["LANG"] == "C"
    assert Git.env()["GIT_TERMINAL_PROMPT"] == "0"

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError) as excinfo:
        Git(plain).run("status")
    assert "not a git repository" in excinfo.value.stderr

    bob, _ = _conflicting_setup(git_world)
    result = bob.rebase("origin/main")
    assert result.conflicts == ["README.md"]
    bob.rebase_abort()


@pytest.mark.parametrize("expected", ["abc123", "HEAD", "refs/heads/main", "A" * 40])
def test_lease_requires_full_sha(expected: str) -> None:
    with pytest.raises(ValueError):
        Lease("refs/heads/task/1", expected)


# --------------------------------------------------------------------------- refresh support


def _commit_at(git: Git, parents: list[str], message: str, when: str, **trailers: str) -> str:
    """Create a commit (via commit-tree) with a fixed author and committer date."""
    tree = git.tree_of(parents[0])
    args = ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    body = message + (
        "\n\n" + "\n".join(f"{k}: {v}" for k, v in trailers.items()) if trailers else ""
    )
    dates = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    return git.run(*args, input=body + "\n", env=dates).stdout.strip()


def test_commits_first_parent(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    base = git_world.initial_sha
    main_side = _commit_at(alice, [base], "feat: first", "2026-01-01T00:00:00Z")
    side = _commit_at(alice, [base], "feat: side", "2026-01-02T00:00:00Z", **{"Forge-X": "1"})
    merge = _commit_at(alice, [main_side, side], "merge side", "2026-01-03T00:00:00Z")

    every = [c.sha for c in alice.commits(f"{base}..{merge}")]
    assert every == [merge, side, main_side]
    first = alice.commits(f"{base}..{merge}", first_parent=True)
    assert [c.sha for c in first] == [merge, main_side]


def test_is_shallow(git_world: GitWorld) -> None:
    seed = Git(git_world.base / "seed")
    git_world.commit_file(seed, "b.txt", "b\n", "feat: b")
    git_world.push(seed, "main")
    full = git_world.clone("alice")
    assert full.is_shallow() is False

    shallow_path = git_world.base / "shallow"
    url = git_world.remote.as_uri()
    Git(git_world.base).run("clone", "--quiet", "--depth", "1", url, str(shallow_path))
    assert Git(shallow_path).is_shallow() is True


def test_fetch_explicit_refspecs_on_single_branch_clone(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    ref = "refs/heads/task/3"
    sha = _claim_commit(alice, "alice")
    assert alice.push(sha, ref, lease=Lease(ref, None)).ok
    assert alice.push(sha, "refs/heads/feature/x").ok

    path = git_world.base / "single"
    Git(git_world.base).run(
        "clone", "--quiet", "--single-branch", "--branch", "main", str(git_world.remote), str(path)
    )
    single = Git(path)
    single.fetch()  # the configured refspec only covers main
    assert single.rev_parse("origin/task/3") is None

    single.fetch(refspecs=["+refs/heads/task/*:refs/remotes/origin/task/*"])
    assert single.rev_parse("origin/task/3") == sha
    assert single.rev_parse("origin/feature/x") is None

    assert alice.delete_remote_branch("task/3", sha).ok
    single.fetch(refspecs=["+refs/heads/task/*:refs/remotes/origin/task/*"])  # prunes
    assert single.rev_parse("origin/task/3") is None
    assert single.rev_parse("origin/main") == git_world.initial_sha


# ---------------------------------------------------------------- hooks and dates (Phase 3)


def _install_pre_push(git: Git, body: str) -> None:
    hook = git.cwd / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_push_no_verify_skips_the_pre_push_hook(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    _install_pre_push(alice, "echo 'hook says no' >&2\nexit 1\n")
    ref = "refs/heads/task/60"
    sha = _claim_commit(alice, "alice")

    with pytest.raises(GitError) as excinfo:
        alice.push(sha, ref, lease=Lease(ref, None))
    assert "hook says no" in excinfo.value.stderr
    assert git_world.remote_git.rev_parse(ref) is None

    assert alice.push(sha, ref, lease=Lease(ref, None), no_verify=True).ok
    assert git_world.remote_git.rev_parse(ref) == sha

    with pytest.raises(GitError):
        alice.delete_remote_branch("task/60", sha)
    assert alice.delete_remote_branch("task/60", sha, no_verify=True).ok
    assert git_world.remote_git.rev_parse(ref) is None


def test_commit_tree_with_date(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    base = git_world.initial_sha
    when = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    sha = alice.commit_tree(alice.tree_of(base), [base], "forge: claim", date=when)
    assert alice.commit_date(sha) == when
    assert alice.commits(sha)[0].date == when
    with pytest.raises(ValueError, match="timezone-aware"):
        alice.commit_tree(alice.tree_of(base), [base], "x", date=datetime(2026, 1, 1))


# --------------------------------------------------------------------------- commits_with_files


def test_commits_with_files(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    first = git_world.commit_file(alice, "src/a.py", "a\n", "feat: a")
    claim = alice.commit("forge: claim T1", allow_empty=True, trailers={"Forge-Claim": "alice"})
    (alice.cwd / "src" / "b.py").write_text("b\n")
    (alice.cwd / "docs").mkdir()
    (alice.cwd / "docs" / "x.md").write_text("x\n")
    alice.add()
    second = alice.commit("feat: b and docs")

    result = alice.commits_with_files("origin/main..HEAD")
    assert [c.commit.sha for c in result] == [second, claim, first]
    assert all(isinstance(c, CommitFiles) for c in result)
    assert sorted(result[0].files) == ["docs/x.md", "src/b.py"]
    assert result[1].files == ()
    assert result[1].commit.trailers == {"Forge-Claim": "alice"}
    assert result[1].commit.subject == "forge: claim T1"
    assert result[2].files == ("src/a.py",)
    # Same CommitInfo as Git.commits.
    assert [c.commit for c in result] == alice.commits("origin/main..HEAD")


def test_commits_with_files_empty_range_and_root(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    assert alice.commits_with_files("origin/main..HEAD") == []
    root = alice.commits_with_files("HEAD")
    assert [(c.commit.sha, c.files) for c in root] == [(git_world.initial_sha, ("README.md",))]


def test_commits_with_files_empty_commits_only(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    one = alice.commit("forge: claim A", allow_empty=True)
    two = alice.commit("forge: claim B", allow_empty=True, trailers={"Forge-Task": "B"})
    result = alice.commits_with_files("origin/main..HEAD")
    assert [(c.commit.sha, c.files) for c in result] == [(two, ()), (one, ())]


def test_commits_with_files_excludes_merges(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    base = git_world.initial_sha
    left = git_world.commit_file(alice, "left.txt", "l\n", "feat: left")
    alice.checkout("side", create_from=base)
    right = git_world.commit_file(alice, "right.txt", "r\n", "feat: right")
    alice.checkout("main")
    alice.run("merge", "--no-ff", "--no-edit", "side")
    merge = alice.rev_parse("HEAD")
    assert merge not in (left, right)

    result = alice.commits_with_files(f"{base}..HEAD")
    assert {c.commit.sha for c in result} == {left, right}
    by_sha = {c.commit.sha: c.files for c in result}
    assert by_sha == {left: ("left.txt",), right: ("right.txt",)}


def test_commits_with_files_odd_paths(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    names = ["with space.txt", "new\nline.txt", "caffè/naïve ✓.md", "tab\there", '"quoted"']
    for name in names:
        target = alice.cwd / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    alice.add()
    odd = alice.commit("feat: odd names")
    empty = alice.commit("forge: claim", allow_empty=True)
    (alice.cwd / "\nleading-newline").write_text("y\n")
    alice.add()
    lead = alice.commit("feat: leading newline")

    result = alice.commits_with_files("origin/main..HEAD")
    assert [c.commit.sha for c in result] == [lead, empty, odd]
    assert result[0].files == ("\nleading-newline",)
    assert result[1].files == ()
    assert sorted(result[2].files) == sorted(names)


def test_commits_with_files_rename_lists_both_paths(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    alice.config_set("diff.renames", "true")
    alice.run("mv", "README.md", "README.txt")
    renamed = alice.commit("chore: rename")
    result = alice.commits_with_files("origin/main..HEAD")
    assert result[0].commit.sha == renamed
    assert sorted(result[0].files) == ["README.md", "README.txt"]


# --------------------------------------------------------------- sync primitives (Phase 5)


def test_rebase_keeps_empty_claim_commit_with_trailers_and_date(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    bob = git_world.clone("bob")
    when = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    base = git_world.initial_sha
    claim = alice.commit_tree(
        alice.tree_of(base),
        [base],
        "forge: claim T1",
        {"Forge-Claim": "alice", "Forge-Run": "run-1"},
        date=when,
    )
    alice.reset(claim, "hard")
    work = git_world.commit_file(alice, "src/a.py", "a\n", "feat: a")
    git_world.commit_file(bob, "docs/b.md", "b\n", "docs: b")
    git_world.push(bob, "main")
    alice.fetch()

    result = alice.rebase(
        "origin/main", rerere=True, keep_empty=True, committer_date_is_author_date=True
    )
    assert result.ok and result.conflicts == []
    rebased = alice.commits("origin/main..HEAD")
    assert [c.subject for c in rebased] == ["feat: a", "forge: claim T1"]
    new_claim = rebased[1]
    assert new_claim.sha != claim
    assert new_claim.trailers == {"Forge-Claim": "alice", "Forge-Run": "run-1"}
    assert new_claim.date == when
    assert alice.commit_date(new_claim.sha) == when
    assert alice.commits_with_files(new_claim.sha + "^!")[0].files == ()
    assert rebased[0].sha != work
    assert alice.rev_parse(new_claim.sha + "^") == alice.rev_parse("origin/main")


def test_rebase_with_rerere_replays_a_recorded_resolution(git_world: GitWorld) -> None:
    bob, before = _conflicting_setup(git_world)
    first = bob.rebase("origin/main", rerere=True)
    assert not first.ok and first.conflicts == ["README.md"] and first.rerere == []
    (bob.cwd / "README.md").write_text("alice\nbob\n")
    bob.add_paths(["README.md"])
    assert bob.rebase_continue(rerere=True).ok
    assert bob.config_get("rerere.enabled") is None  # per command only

    bob.reset(before, "hard")
    second = bob.rebase("origin/main", rerere=True)
    # rerere resolved and staged the conflict: the rebase stops with nothing unmerged.
    assert not second.ok and second.conflicts == [] and second.rerere == ["README.md"]
    assert bob.unmerged_files() == []
    assert (bob.cwd / "README.md").read_text() == "alice\nbob\n"
    assert bob.rebase_continue(rerere=True).ok
    assert not bob.rebase_in_progress()


def test_rebase_reports_rerere_paths_only_for_resolved_conflicts(git_world: GitWorld) -> None:
    """Two conflicted paths (one with a space and a quote), one resolution recorded: the stop
    reports the replayed path in ``rerere`` and the other one in ``conflicts``. (The two
    conflicts differ in content: rerere keys resolutions on the conflict text.)"""
    odd = "docs/it's a file.txt"
    alice = git_world.clone("alice")
    bob = git_world.clone("bob")
    git_world.commit_file(alice, odd, "base\n", "chore: base")
    git_world.commit_file(alice, "b.txt", "base\n", "chore: base b")
    git_world.push(alice, "main")
    bob.fetch()
    bob.reset("origin/main", "hard")
    git_world.commit_file(bob, odd, "bob\n", "feat: bob odd")
    before = git_world.commit_file(bob, "b.txt", "bob b\n", "feat: bob b")
    git_world.commit_file(alice, odd, "alice\n", "feat: alice odd")
    git_world.commit_file(alice, "b.txt", "alice b\n", "feat: alice b")
    git_world.push(alice, "main")
    bob.fetch()

    first = bob.rebase("origin/main", rerere=True)
    assert not first.ok and first.conflicts == [odd] and first.rerere == []
    (bob.cwd / odd).write_text("merged\n")
    bob.add_paths([odd])
    second = bob.rebase_continue(rerere=True)
    assert not second.ok and second.conflicts == ["b.txt"] and second.rerere == []
    bob.rebase_abort()  # the resolution of the odd path stays recorded
    assert bob.rev_parse("HEAD") == before

    third = bob.rebase("origin/main", rerere=True)
    assert not third.ok and third.conflicts == [] and third.rerere == [odd]
    assert (bob.cwd / odd).read_text() == "merged\n"
    fourth = bob.rebase_continue(rerere=True)
    assert not fourth.ok and fourth.conflicts == ["b.txt"] and fourth.rerere == []
    bob.rebase_abort()


def test_restore_conflict_after_rerere_and_after_add(git_world: GitWorld) -> None:
    bob, before = _conflicting_setup(git_world)
    assert not bob.rebase("origin/main", rerere=True).ok
    (bob.cwd / "README.md").write_text("alice\nbob\n")
    bob.add_paths(["README.md"])
    assert bob.restore_conflict("README.md")  # resolved by git add: back to unmerged
    assert bob.conflicted_files() == ["README.md"]
    assert "<<<<<<<" in (bob.cwd / "README.md").read_text()
    (bob.cwd / "README.md").write_text("alice\nbob\n")
    bob.add_paths(["README.md"])
    assert bob.rebase_continue(rerere=True).ok
    bob.reset(before, "hard")
    replay = bob.rebase("origin/main", rerere=True)
    assert replay.rerere == ["README.md"] and bob.conflicted_files() == []
    assert bob.restore_conflict("README.md")  # resolved by rerere: back to unmerged
    assert bob.conflicted_files() == ["README.md"]
    assert bob.stage_blob(2, "README.md") == b"alice\n"
    assert not bob.restore_conflict("missing.txt")
    bob.rebase_abort()


def test_commit_all_reports_a_failing_add_separately(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    tip = alice.rev_parse("HEAD")
    (alice.cwd / "README.md").write_text("changed\n")
    secret = alice.cwd / "unreadable.txt"
    secret.write_text("x\n")
    secret.chmod(0)
    try:
        with pytest.raises(GitAddFailed) as info:
            alice.commit_all("forge: wip")
        assert info.value.git_args == ("add", "-A") and "unreadable.txt" in info.value.stderr
        assert alice.rev_parse("HEAD") == tip
    finally:
        secret.chmod(0o644)
    _install_pre_commit = alice.cwd / ".git" / "hooks" / "pre-commit"
    _install_pre_commit.write_text("#!/bin/sh\nexit 1\n")
    _install_pre_commit.chmod(0o755)
    with pytest.raises(GitError) as hook:
        alice.commit_all("forge: wip")
    assert not isinstance(hook.value, GitAddFailed)


def test_rerere_messages_are_filtered(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    output = "\n".join(
        [
            "Recorded resolution for 'README.md'.",
            "Staged 'README.md' using previous resolution.",
            "Staged 'README.md' using previous resolution.",
            "Resolved 'still/unmerged.txt' using previous resolution.",
            "Staged 'not/in/index.txt' using previous resolution.",
            "Rebasing (1/2)\rStaged 'README.md' using previous resolution.",
        ]
    )
    assert alice._rerere_reused(output, ["still/unmerged.txt"]) == ["README.md"]


def test_dirty_files(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    git_world.commit_file(alice, "gone.txt", "x\n", "chore: gone")
    git_world.commit_file(alice, "old name.txt", "x\n", "chore: old")
    assert alice.dirty_files() == []
    (alice.cwd / ".gitignore").write_text("*.log\n")
    (alice.cwd / "debug.log").write_text("ignored\n")
    (alice.cwd / "README.md").write_text("changed\n")
    (alice.cwd / "gone.txt").unlink()
    alice.run("mv", "old name.txt", "new name.txt")
    (alice.cwd / "sub").mkdir()
    (alice.cwd / "sub/new.txt").write_text("new\n")
    (alice.cwd / "staged.txt").write_text("staged\n")
    alice.add(["staged.txt"])
    expected = [
        ".gitignore",
        "README.md",
        "gone.txt",
        "new name.txt",
        "old name.txt",
        "staged.txt",
        "sub/new.txt",
    ]
    assert alice.dirty_files() == expected
    assert Git(alice.cwd / "sub").dirty_files() == expected  # repository-relative anywhere
    alice.commit_all("chore: all")
    assert sorted(alice.commits_with_files("HEAD^!")[0].files) == expected
    assert alice.dirty_files() == []


def test_unmerged_files_is_conflicted_files() -> None:
    assert Git.unmerged_files is Git.conflicted_files


def test_stage_blob_and_checkout_stage(git_world: GitWorld) -> None:
    bob, _ = _conflicting_setup(git_world)
    assert not bob.rebase("origin/main").ok
    assert bob.stage_blob(1, "README.md") == b"# test repo\n"
    assert bob.stage_blob(2, "README.md") == b"alice\n"  # upstream during a rebase
    assert bob.stage_blob(3, "README.md") == b"bob\n"
    with pytest.raises(ValueError):
        bob.stage_blob(0, "README.md")

    bob.checkout_stage("README.md", "theirs")
    assert (bob.cwd / "README.md").read_text() == "bob\n"
    assert bob.conflicted_files() == ["README.md"]
    bob.checkout_stage("README.md", "ours")
    assert (bob.cwd / "README.md").read_text() == "alice\n"
    bob.add_paths(["README.md"])
    assert bob.conflicted_files() == []
    assert bob.stage_blob(2, "README.md") is None  # resolved: only stage 0 remains
    assert bob.rebase_continue().ok


def test_stage_blob_odd_path_without_base(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    bob = git_world.clone("bob")
    name = "caffè dir/naïve [x] *.txt"
    git_world.commit_file(alice, name, "alice\n", "feat: alice")
    git_world.push(alice, "main")
    git_world.commit_file(bob, name, "bob\n", "feat: bob")
    (bob.cwd / "caffè dir" / "naïve [y] z.txt").write_text("other\n")
    bob.add_paths(["caffè dir/naïve [y] z.txt"])
    bob.commit("feat: other")
    bob.fetch()

    result = bob.rebase("origin/main")
    assert not result.ok and result.conflicts == [name]
    assert bob.stage_blob(1, name) is None  # added on both sides: no base
    assert bob.stage_blob(2, name) == b"alice\n"
    assert bob.stage_blob(3, name) == b"bob\n"
    assert bob.stage_blob(2, "caffè dir/missing.txt") is None
    bob.checkout_stage(name, "theirs")
    bob.add_paths([name])
    assert bob.conflicted_files() == []


def test_merge_file_union(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    merged = alice.merge_file_union(b"a\nX\nc\n", b"a\nb\nc\n", b"a\nY\nc\n")
    assert merged == b"a\nX\nY\nc\n"
    added = alice.merge_file_union("- ours ✓\n".encode(), b"", b"- theirs\n")
    assert added == "- ours ✓\n- theirs\n".encode()
    assert alice.merge_file_union(b"same\n", b"old\n", b"same\n") == b"same\n"


def test_add_paths_stages_deletions_and_literal_names(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    git_world.commit_file(alice, "a b.txt", "x\n", "feat: a b")
    git_world.commit_file(alice, "*.txt", "star\n", "feat: star")
    (alice.cwd / "a b.txt").unlink()
    (alice.cwd / "*.txt").write_text("changed\n")
    (alice.cwd / "keep.txt").write_text("untouched\n")
    alice.add_paths(["a b.txt"])
    staged = _split(alice.run("diff", "--cached", "--name-status", "-z").stdout)
    assert staged == ["D", "a b.txt"]
    alice.add_paths(["*.txt"])  # literal: does not stage keep.txt
    staged = _split(alice.run("diff", "--cached", "--name-only", "-z").stdout)
    assert sorted(staged) == ["*.txt", "a b.txt"]
    alice.add_paths([])


def _split(out: str) -> list[str]:
    return [item for item in out.split("\x00") if item]


def _install_hook(git: Git, name: str, body: str) -> None:
    hook = git.cwd / ".git" / "hooks" / name
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_commit_all_stages_everything(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    git_world.commit_file(alice, "gone.txt", "x\n", "feat: gone")
    (alice.cwd / "gone.txt").unlink()
    (alice.cwd / "README.md").write_text("changed\n")
    (alice.cwd / "new file ✓.txt").write_text("new\n")
    sha = alice.commit_all("forge: wip")
    assert sha == alice.rev_parse("HEAD")
    assert not alice.is_dirty()
    assert alice.commits("HEAD^!")[0].subject == "forge: wip"
    assert sorted(alice.diff_names("HEAD^", "HEAD")) == ["README.md", "gone.txt", "new file ✓.txt"]
    with pytest.raises(GitError):
        alice.commit_all("forge: wip")  # nothing to commit


def test_commit_all_hook_rejection_raises_with_output(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    before = alice.rev_parse("HEAD")
    _install_hook(alice, "pre-commit", "echo 'lint failed on x.py'\nexit 1\n")
    (alice.cwd / "x.py").write_text("bad\n")
    with pytest.raises(GitError) as excinfo:
        alice.commit_all("forge: wip")
    assert "lint failed on x.py" in excinfo.value.stderr
    assert alice.rev_parse("HEAD") == before

    _install_hook(alice, "pre-commit", "exit 0\n")
    _install_hook(alice, "commit-msg", 'echo seen >> "$(git rev-parse --git-dir)/seen"\n')
    alice.commit_all("forge: wip")
    assert (alice.cwd / ".git" / "seen").read_text() == "seen\n"


def test_reset_modes(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    base = git_world.initial_sha
    git_world.commit_file(alice, "a.txt", "a\n", "feat: a")
    tip = alice.rev_parse("HEAD")
    assert tip is not None

    alice.reset(base, "soft")
    assert alice.rev_parse("HEAD") == base
    assert _split(alice.run("diff", "--cached", "--name-only", "-z").stdout) == ["a.txt"]

    alice.reset(tip, "hard")
    alice.reset(base, "mixed")
    assert alice.rev_parse("HEAD") == base
    assert _split(alice.run("diff", "--cached", "--name-only", "-z").stdout) == []
    assert (alice.cwd / "a.txt").read_text() == "a\n"  # untracked again

    alice.reset(tip, "hard")
    alice.reset(base, "hard")
    assert not (alice.cwd / "a.txt").exists()
    with pytest.raises(ValueError):
        alice.reset(base, "keep")  # type: ignore[arg-type]


def test_push_pre_push_hook_rejection_is_distinct_from_lease(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    ref = "refs/heads/task/7"
    first = _claim_commit(alice, "alice")
    assert alice.push(first, ref, lease=Lease(ref, None)).ok
    second = alice.commit_tree(alice.tree_of(first), [first], "feat: more")

    # Stale lease, no hook: a rejected result, not an exception.
    stale = Lease(ref, git_world.initial_sha)
    result = alice.push(second, ref, lease=stale)
    assert not result.ok and result.rejected

    # Hook rejects: a distinct exception, nothing pushed.
    _install_hook(alice, "pre-push", "echo 'tests failed' >&2\nexit 1\n")
    with pytest.raises(PushHookRejected) as excinfo:
        alice.push(second, ref, lease=Lease(ref, first))
    assert isinstance(excinfo.value, GitError)
    assert "tests failed" in excinfo.value.stderr
    assert git_world.remote_git.rev_parse(ref) == first

    # Hook rejects and the lease is stale: git runs the hook first, so the hook wins.
    with pytest.raises(PushHookRejected):
        alice.push(second, ref, lease=stale)

    # Hook passes, stale lease: rejected result; good lease: pushed.
    _install_hook(alice, "pre-push", "exit 0\n")
    result = alice.push(second, ref, lease=stale)
    assert not result.ok and result.rejected
    assert alice.push(second, ref, lease=Lease(ref, first)).ok
    assert git_world.remote_git.rev_parse(ref) == second


def test_push_hook_rejection_honours_core_hooks_path(git_world: GitWorld, tmp_path: Path) -> None:
    alice = git_world.clone("alice")
    hooks = tmp_path / "my hooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text("#!/bin/sh\necho 'custom hook' >&2\nexit 1\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    alice.config_set("core.hooksPath", str(hooks))
    ref = "refs/heads/task/8"
    sha = _claim_commit(alice, "alice")
    with pytest.raises(PushHookRejected) as excinfo:
        alice.push(sha, ref, lease=Lease(ref, None))
    assert "custom hook" in excinfo.value.stderr


def test_push_unknown_source_with_hook_is_not_a_hook_rejection(git_world: GitWorld) -> None:
    alice = git_world.clone("alice")
    _install_hook(alice, "pre-push", "exit 1\n")
    with pytest.raises(GitError) as excinfo:
        alice.push("refs/heads/nope", "refs/heads/task/9")
    assert not isinstance(excinfo.value, PushHookRejected)
