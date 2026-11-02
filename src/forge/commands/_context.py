"""Shared command plumbing: locate the repository, load the config, build git/GitHub access."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class AppContext:
    """Everything a repository-bound command needs."""

    root: Path
    config: ForgeConfig
    git: Git
    github: GitHubClient


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
    """Find the repository, load `.forge/config.yml` and build the git and GitHub clients."""
    root = find_repo_root(opts)
    config = load_config(root)
    return AppContext(root=root, config=config, git=Git(root), github=github_factory(config))
