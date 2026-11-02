"""Tests for forge.core.git against real temporary repositories."""

from __future__ import annotations

import stat
import subprocess
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from forge.core.errors import ForgeError
from forge.core.git import Git, GitError, Lease, parse_version

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
