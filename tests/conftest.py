"""Shared pytest fixtures for the Forge test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.core.git import Git


@pytest.fixture(autouse=True)
def plain_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make CLI output plain and identical locally and in CI.

    Rich (used by Typer for help) forces colours when it detects GitHub Actions
    or ``FORCE_COLOR``. The ANSI codes it adds break substring assertions.
    """
    for var in ("GITHUB_ACTIONS", "FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "120")


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Isolate every test from the user's git configuration.

    Global config points to an empty file, system config is ignored and ``HOME`` is a fresh
    temporary directory, so signing, hooks, aliases or ``init.defaultBranch`` on the developer's
    machine cannot change test outcomes.
    """
    home = tmp_path_factory.mktemp("home")
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in (
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_DATE",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


class GitWorld:
    """A bare "remote" repository plus one clone per simulated user.

    Layout under ``base`` (a fresh temporary directory)::

        remote.git/   bare repository, default branch ``main``, seeded with README.md
        <user>/       one clone per call to :meth:`clone`

    Typical use in tests (two users racing)::

        alice = git_world.clone("alice")
        bob = git_world.clone("bob")
        git_world.commit_file(alice, "a.txt", "hi\\n", "feat: a")
        git_world.push(alice, "main")
        bob.fetch()

    Each clone has ``user.name`` = ``<user>`` and ``user.email`` = ``<user>@example.test``.
    ``remote_git`` is a :class:`Git` on the bare repository, handy to inspect server-side refs.
    """

    def __init__(self, base: Path):
        self.base = base
        self.remote = base / "remote.git"
        Git(base).run("init", "--quiet", "--bare", "--initial-branch=main", str(self.remote))
        self.remote_git = Git(self.remote)
        seed = self.clone("seed")
        self.initial_sha = self.commit_file(seed, "README.md", "# test repo\n", "chore: init")
        self.push(seed, "main")

    def clone(self, user: str) -> Git:
        """Clone the remote into ``base/<user>`` and configure that user's identity."""
        path = self.base / user
        Git(self.base).run("clone", "--quiet", str(self.remote), str(path))
        git = Git(path)
        git.config_set("user.name", user)
        git.config_set("user.email", f"{user}@example.test")
        git.config_set("init.defaultBranch", "main")
        return git

    def commit_file(self, git: Git, path: str, content: str, message: str) -> str:
        """Write ``content`` to ``path`` in ``git``'s working tree, commit it and return the sha."""
        target = git.cwd / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git.add([path])
        return git.commit(message)

    def push(self, git: Git, branch: str) -> None:
        """Push ``branch`` of ``git`` to ``origin`` (plain push; raises on failure)."""
        result = git.push(f"refs/heads/{branch}", f"refs/heads/{branch}")
        if not result.ok:
            raise AssertionError(f"push of {branch} rejected: {result.stderr}")


@pytest.fixture
def git_world(tmp_path_factory: pytest.TempPathFactory) -> GitWorld:
    """Return a :class:`GitWorld`: a bare remote seeded on ``main`` plus a clone factory."""
    return GitWorld(tmp_path_factory.mktemp("world"))
