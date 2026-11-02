"""Shared command plumbing: locate the repository, load the config, build git/GitHub access.

The GitHub client is built lazily, on the first access to :attr:`AppContext.github`, so
git-only commands work even when ``github.repo`` is wrong or ``gh`` is unavailable.

It is built by the module attribute :data:`github_factory`. Tests replace it with
``monkeypatch.setattr(_context, "github_factory", ...)``; for that to take effect, code must
look the factory up through this module at call time (as :attr:`AppContext.github` does) and
never bind it with ``from forge.commands._context import github_factory``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from forge.commands._output import GlobalOptions
from forge.core.config import ForgeConfig, load_config
from forge.core.errors import PreconditionError
from forge.core.git import Git, GitError
from forge.core.github import GhCliClient, GitHubClient


def _default_github(config: ForgeConfig) -> GitHubClient:
    return GhCliClient(config.github.repo)


github_factory: Callable[[ForgeConfig], GitHubClient] = _default_github
"""Builds the GitHub client for a config. Tests replace it with ``monkeypatch.setattr``."""


@dataclass
class AppContext:
    """Everything a repository-bound command needs.

    ``github`` is created on first access through the module's current
    :data:`github_factory` and then cached for the lifetime of the context.
    """

    root: Path
    config: ForgeConfig
    git: Git

    @cached_property
    def github(self) -> GitHubClient:
        """GitHub access to ``config.github.repo``, built on first use."""
        # A global lookup at call time, so a monkeypatched factory is honoured.
        return github_factory(self.config)


def find_repo_root(opts: GlobalOptions) -> Path:
    """Return the top-level directory of the repository named by ``--repo`` (or the cwd).

    Raises :class:`PreconditionError` when the path is not inside a git working tree.
    """
    start = (opts.repo if opts.repo is not None else Path.cwd()).expanduser()
    hint = "run forge inside a clone of your project, or pass --repo <path>"
    if not start.is_dir():
        raise PreconditionError(f"{start} is not a directory", hint=hint)
    try:
        return Git(start).root()
    except GitError as exc:
        raise PreconditionError(f"{start} is not inside a git repository", hint=hint) from exc


def load_context(opts: GlobalOptions) -> AppContext:
    """Find the repository, load `.forge/config.yml` and build git access (GitHub is lazy)."""
    root = find_repo_root(opts)
    config = load_config(root)
    return AppContext(root=root, config=config, git=Git(root))
