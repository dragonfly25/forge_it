"""Thin, typed wrapper around the ``git`` executable (ADR 0007).

Every git call in Forge goes through :class:`Git`. Commands run through ``subprocess`` with a
forced ``C`` locale so that output can be parsed on any machine, and with terminal prompts and
optional locks disabled so that nothing blocks or interferes with the user's own git usage.

A non-zero exit raises :class:`GitError`, except for operations whose failure is an expected
outcome: pushes that lose a compare-and-swap or fast-forward check (:class:`PushResult`),
rebases that stop on conflicts (:class:`RebaseResult`), ``rev_parse`` of a missing ref
(``None``) and ``is_ancestor`` answering no (``False``).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from forge.core.errors import ForgeError

GIT_ENV: Mapping[str, str] = {
    "LC_ALL": "C",
    "LANG": "C",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}
"""Environment variables merged over ``os.environ`` for every git call."""

_VERSION_RE = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")

_FIELD_SEP = "\x00"
_NON_INTERACTIVE: Mapping[str, str] = {"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"}
_RECORD_SEP = "\x1e"


class GitError(ForgeError):
    """A git command failed unexpectedly.

    ``git_args`` holds the arguments passed to ``git`` (without the executable),
    ``returncode`` its exit status and ``stderr`` its error output.
    """

    def __init__(self, git_args: Sequence[str], returncode: int, stderr: str):
        self.git_args = tuple(git_args)
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else "no error output"
        super().__init__(f"git {' '.join(self.git_args)} failed (exit {returncode}): {detail}")


_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class Lease:
    """Expected state of a remote ref for ``--force-with-lease``.

    ``expected=None`` means the ref must not exist yet; a sha means the ref must point at it.
    ``ref`` must be the full destination ref of the push it is used with (``refs/heads/...``);
    :meth:`Git.push` rejects a lease for any other ref.
    """

    ref: str
    expected: str | None

    def __post_init__(self) -> None:
        if self.expected is not None and not _FULL_SHA.fullmatch(self.expected):
            raise ValueError(f"Lease.expected must be a full hex object id, got {self.expected!r}")

    def option(self) -> str:
        """Return the ``--force-with-lease=...`` command-line option."""
        return f"--force-with-lease={self.ref}:{self.expected or ''}"


@dataclass(frozen=True)
class PushResult:
    """Outcome of a push.

    ``rejected`` is True when the push lost a compare-and-swap or fast-forward check: a local
    ``[rejected]`` (stale lease, non-fast-forward, fetch first) or a ``[remote rejected]``
    verified to be a lost race. ``reason`` is a short human-readable explanation (empty on a
    clean success).
    """

    ok: bool
    rejected: bool
    stderr: str
    reason: str = ""


@dataclass(frozen=True)
class RebaseResult:
    """Outcome of a rebase step. When ``ok`` is False the rebase is left in progress."""

    ok: bool
    conflicts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CommitInfo:
    """One commit as returned by :meth:`Git.commits`."""

    sha: str
    author: str
    author_email: str
    date: datetime  # committer date, timezone-aware
    subject: str
    trailers: dict[str, str]


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse ``git --version`` output such as ``git version 2.39.3 (Apple Git-146)``.

    Raises :class:`GitError` when the text is not recognised.
    """
    match = _VERSION_RE.search(text)
    if match is None:
        raise GitError(["--version"], 0, f"unrecognised git version string: {text!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def format_message(message: str, trailers: Mapping[str, str] | None = None) -> str:
    """Return a commit message with ``Key: value`` trailers after a blank line."""
    body = message.rstrip("\n")
    if trailers:
        body += "\n\n" + "\n".join(f"{key}: {value}" for key, value in trailers.items())
    return body + "\n"


def _parse_trailers(text: str) -> dict[str, str]:
    trailers: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            trailers[key.strip()] = value.strip()
    return trailers


def _split_z(output: str) -> list[str]:
    return [item for item in output.split("\x00") if item]


class Git:
    """Run git commands in one working directory (a clone, a worktree or a bare repository)."""

    def __init__(self, cwd: Path):
        self.cwd = Path(cwd)

    def __repr__(self) -> str:
        return f"Git({str(self.cwd)!r})"

    # ------------------------------------------------------------------ basics

    @staticmethod
    def env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Return the environment used for git calls: ``os.environ`` + :data:`GIT_ENV`."""
        env = dict(os.environ)
        env.update(GIT_ENV)
        if extra:
            env.update(extra)
        return env

    def run(
        self,
        *args: str,
        check: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``git <args>`` in ``cwd`` and return the completed process.

        With ``check=True`` a non-zero exit raises :class:`GitError`. ``env`` adds variables on
        top of the forced git environment.
        """
        proc = subprocess.run(
            ["git", *args],
            cwd=self.cwd,
            env=self.env(env),
            input=input,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise GitError(args, proc.returncode, proc.stderr)
        return proc

    def _out(self, *args: str, input: str | None = None) -> str:
        return self.run(*args, input=input).stdout.strip()

    @classmethod
    def version(cls) -> tuple[int, int, int]:
        """Return the installed git version as ``(major, minor, patch)``."""
        proc = subprocess.run(
            ["git", "--version"],
            env=cls.env(),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise GitError(["--version"], proc.returncode, proc.stderr)
        return parse_version(proc.stdout)

    def root(self) -> Path:
        """Return the top-level directory of the working tree."""
        return Path(self._out("rev-parse", "--show-toplevel"))

    def current_branch(self) -> str | None:
        """Return the checked-out branch name, or None when HEAD is detached."""
        proc = self.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if proc.returncode == 0:
            return proc.stdout.strip()
        if proc.returncode == 1:
            return None
        raise GitError(("symbolic-ref", "--quiet", "--short", "HEAD"), proc.returncode, proc.stderr)

    def is_dirty(self, include_untracked: bool = True) -> bool:
        """Return True when the working tree or index has changes."""
        untracked = "--untracked-files=normal" if include_untracked else "--untracked-files=no"
        return bool(self._out("status", "--porcelain", untracked))

    def config_get(self, key: str) -> str | None:
        """Return the value of a git config key, or None when it is not set."""
        args = ("config", "--get", key)
        proc = self.run(*args, check=False)
        if proc.returncode == 0:
            return proc.stdout.rstrip("\n")
        if proc.returncode == 1:
            return None
        raise GitError(args, proc.returncode, proc.stderr)

    def config_set(self, key: str, value: str) -> None:
        """Set a repository-local git config key."""
        self.run("config", "--local", key, value)

    # ------------------------------------------------------------------ refs

    def rev_parse(self, ref: str) -> str | None:
        """Return the commit sha ``ref`` points to, or None when it does not name a commit.

        Any other failure (for example ``cwd`` is not a git repository) raises
        :class:`GitError`.
        """
        args = ("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        proc = self.run(*args, check=False)
        if proc.returncode == 0:
            return proc.stdout.strip()
        if proc.returncode == 1:
            return None
        raise GitError(args, proc.returncode, proc.stderr)

    def is_ancestor(self, a: str, b: str) -> bool:
        """Return True when commit ``a`` is an ancestor of (or equal to) commit ``b``.

        Raises :class:`GitError` when either commit is unknown or git fails otherwise.
        """
        args = ("merge-base", "--is-ancestor", a, b)
        proc = self.run(*args, check=False)
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise GitError(args, proc.returncode, proc.stderr)

    def _has_object(self, sha: str) -> bool:
        """Return True when the object ``sha`` exists in the local object store."""
        return self.run("cat-file", "-e", sha, check=False).returncode == 0

    def ls_remote(self, remote: str = "origin", pattern: str | None = None) -> dict[str, str]:
        """Return ``{ref: sha}`` for refs on ``remote``, optionally filtered by ``pattern``."""
        args = ["ls-remote", remote]
        if pattern is not None:
            args.append(pattern)
        refs: dict[str, str] = {}
        for line in self._out(*args).splitlines():
            sha, _, ref = line.partition("\t")
            if ref:
                refs[ref] = sha
        return refs

    def remote_branch_sha(self, branch: str, remote: str = "origin") -> str | None:
        """Return the sha of ``branch`` on ``remote`` (queried live), or None if missing."""
        ref = f"refs/heads/{branch}"
        return self.ls_remote(remote, ref).get(ref)

    def update_ref(self, ref: str, sha: str) -> None:
        """Point ``ref`` (a full ref name) at ``sha``, creating it if needed."""
        self.run("update-ref", ref, sha)

    def delete_ref(self, ref: str) -> None:
        """Delete the local ref ``ref``."""
        self.run("update-ref", "-d", ref)

    def list_refs(self, prefix: str) -> dict[str, str]:
        """Return ``{ref: sha}`` for local refs under ``prefix`` (e.g. ``refs/forge/backup/``)."""
        out = self._out("for-each-ref", "--format=%(refname) %(objectname)", prefix)
        refs: dict[str, str] = {}
        for line in out.splitlines():
            ref, _, sha = line.partition(" ")
            refs[ref] = sha
        return refs

    def branch_exists(self, name: str) -> bool:
        """Return True when the local branch ``name`` exists."""
        proc = self.run("show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False)
        return proc.returncode == 0

    # ------------------------------------------------------------------ sync

    def fetch(self, remote: str = "origin", prune: bool = True) -> None:
        """Fetch from ``remote`` (pruning deleted branches by default)."""
        args = ["fetch", "--quiet"]
        if prune:
            args.append("--prune")
        args.append(remote)
        self.run(*args)

    # ------------------------------------------------------------------ history and diffs

    def merge_base(self, a: str, b: str) -> str:
        """Return the best common ancestor of ``a`` and ``b``."""
        return self._out("merge-base", a, b)

    def diff_names(self, a: str, b: str) -> list[str]:
        """Return paths that differ between the trees of ``a`` and ``b`` (two-dot diff)."""
        return _split_z(self.run("diff", "--name-only", "-z", a, b, "--").stdout)

    def changed_files_since(self, base: str, head: str = "HEAD") -> list[str]:
        """Return paths changed on ``head`` since its merge base with ``base`` (``base...head``)."""
        return _split_z(self.run("diff", "--name-only", "-z", f"{base}...{head}", "--").stdout)

    def commits(self, range_: str) -> list[CommitInfo]:
        """Return commits in ``range_`` (any ``git log`` revision range), newest first."""
        fmt = "%x00".join(["%H", "%an", "%ae", "%cI", "%s", "%(trailers:only,unfold)"])
        out = self.run("log", f"--format={fmt}%x1e", range_, "--").stdout
        result: list[CommitInfo] = []
        for record in out.split(_RECORD_SEP):
            record = record.lstrip("\n")
            if not record:
                continue
            sha, author, email, date, subject, trailers = record.split(_FIELD_SEP, 5)
            result.append(
                CommitInfo(
                    sha=sha,
                    author=author,
                    author_email=email,
                    date=datetime.fromisoformat(date),
                    subject=subject,
                    trailers=_parse_trailers(trailers),
                )
            )
        return result

    def commit_date(self, ref: str) -> datetime:
        """Return the committer date of ``ref`` as a timezone-aware datetime."""
        return datetime.fromisoformat(self._out("log", "-1", "--format=%cI", ref, "--"))

    # ------------------------------------------------------------------ commits

    def add(self, paths: Sequence[str] | None = None) -> None:
        """Stage ``paths``, or every change (``git add -A``) when ``paths`` is None."""
        if paths is None:
            self.run("add", "-A")
        else:
            self.run("add", "--", *paths)

    def commit(
        self,
        message: str,
        *,
        allow_empty: bool = False,
        trailers: Mapping[str, str] | None = None,
    ) -> str:
        """Commit the index with ``message`` and optional trailers; return the new sha."""
        args = ["commit", "--quiet", "-F", "-"]
        if allow_empty:
            args.append("--allow-empty")
        self.run(*args, input=format_message(message, trailers))
        return self._out("rev-parse", "HEAD")

    def tree_of(self, ref: str) -> str:
        """Return the tree sha of commit ``ref``."""
        return self._out("rev-parse", f"{ref}^{{tree}}")

    def commit_tree(
        self,
        tree: str,
        parents: Sequence[str],
        message: str,
        trailers: Mapping[str, str] | None = None,
    ) -> str:
        """Create a commit object without touching HEAD, the index or the working tree."""
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        args += ["-F", "-"]
        return self._out(*args, input=format_message(message, trailers))

    # ------------------------------------------------------------------ push

    def push(
        self,
        src: str,
        dst_ref: str,
        *,
        lease: Lease | None = None,
        remote: str = "origin",
    ) -> PushResult:
        """Push ``src`` (a sha or ref, empty to delete) to ``dst_ref`` on ``remote``.

        With a ``lease`` the push is a compare-and-swap (``--force-with-lease``); without one
        it is a plain push. ``lease.ref`` must equal ``dst_ref`` (``ValueError`` otherwise).

        Outcomes:

        - success: ``PushResult(ok=True, rejected=False)``;
        - local ``[rejected]`` (stale lease, non-fast-forward, fetch first, already exists):
          ``PushResult(ok=False, rejected=True)``;
        - ``[remote rejected]``: the server's wording does not distinguish a lost race from a
          permanent error, so the remote ref is queried. It is a lost race (``rejected=True``)
          only when the ref moved away from the expected value and is not the value we pushed.
          When it already holds the value we pushed, the push landed and ``ok=True``.
          Otherwise (hook, protected branch, ref name clash, ...) :class:`GitError` is raised.

        Any other failure raises :class:`GitError`.
        """
        if lease is not None and lease.ref != dst_ref:
            raise ValueError(f"lease ref {lease.ref!r} does not match push destination {dst_ref!r}")
        args = ["push", "--porcelain"]
        if lease is not None:
            args.append(lease.option())
        args += [remote, f"{src}:{dst_ref}"]
        proc = self.run(*args, check=False)
        output = proc.stderr + proc.stdout
        if proc.returncode == 0:
            return PushResult(ok=True, rejected=False, stderr=proc.stderr)
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or parts[0] != "!":
                continue
            summary = parts[2]
            if summary.startswith("[rejected]"):
                return PushResult(ok=False, rejected=True, stderr=output, reason=summary)
            if summary.startswith("[remote rejected]"):
                full_ref = parts[1].rpartition(":")[2] or dst_ref
                return self._verify_remote_rejection(
                    args, proc.returncode, output, summary, src, full_ref, lease, remote
                )
        raise GitError(args, proc.returncode, output)

    def _verify_remote_rejection(
        self,
        args: Sequence[str],
        returncode: int,
        output: str,
        summary: str,
        src: str,
        ref: str,
        lease: Lease | None,
        remote: str,
    ) -> PushResult:
        """Classify a ``[remote rejected]`` push by looking at the remote ref's current value."""
        try:
            target = self._resolve_object(src) if src else None
            current = self.ls_remote(remote, ref).get(ref)
        except GitError as exc:
            raise GitError(
                args, returncode, f"{output}\nverification of the rejection failed: {exc.stderr}"
            ) from exc
        shown = current or "absent"
        if current == target:
            return PushResult(
                ok=True,
                rejected=False,
                stderr=output,
                reason=f"{summary}; remote ref already at the pushed value ({shown})",
            )
        if lease is not None:
            lost = current != lease.expected
        else:
            # A plain push has no explicit expectation: it lost a race when the remote ref now
            # holds a commit the pushed one does not descend from (a retry would be rejected as
            # non-fast-forward). Deletions without a lease cannot lose a race.
            lost = (
                current is not None
                and target is not None
                and not (self._has_object(current) and self.is_ancestor(current, target))
            )
        if lost:
            return PushResult(
                ok=False,
                rejected=True,
                stderr=output,
                reason=f"{summary}; remote ref changed concurrently (now {shown})",
            )
        raise GitError(args, returncode, output)

    def _resolve_object(self, rev: str) -> str:
        """Return the object id ``rev`` names (a tag object stays a tag object)."""
        return self._out("rev-parse", "--verify", "--end-of-options", rev)

    def delete_remote_branch(
        self, branch: str, expected_sha: str, remote: str = "origin"
    ) -> PushResult:
        """Delete ``branch`` on ``remote`` only if it still points at ``expected_sha``.

        ``ok`` means the ref is absent afterwards (deleted by us or, concurrently, by someone
        else). On a re-run the ref may already be gone and the stale lease yields
        ``rejected``: callers must treat ``rejected`` with an absent remote ref as done.
        """
        ref = f"refs/heads/{branch}"
        return self.push("", ref, lease=Lease(ref, expected_sha), remote=remote)

    # ------------------------------------------------------------------ rebase

    def rebase(self, onto: str) -> RebaseResult:
        """Rebase the current branch onto ``onto``.

        On conflicts the rebase is left in progress and the conflicted paths are returned.
        """
        return self._rebase_step(("rebase", onto))

    def rebase_continue(self) -> RebaseResult:
        """Continue an in-progress rebase without opening an editor."""
        return self._rebase_step(("rebase", "--continue"))

    def _rebase_step(self, args: tuple[str, ...]) -> RebaseResult:
        proc = self.run(*args, check=False, env=_NON_INTERACTIVE)
        if proc.returncode == 0:
            return RebaseResult(ok=True)
        if self.rebase_in_progress():
            return RebaseResult(ok=False, conflicts=self.conflicted_files())
        raise GitError(args, proc.returncode, proc.stderr)

    def rebase_abort(self) -> None:
        """Abort the in-progress rebase and restore the original branch."""
        self.run("rebase", "--abort", env=_NON_INTERACTIVE)

    def rebase_in_progress(self) -> bool:
        """Return True when a rebase is in progress in this working tree."""
        for name in ("rebase-merge", "rebase-apply"):
            path = Path(self._out("rev-parse", "--git-path", name))
            if not path.is_absolute():
                path = self.cwd / path
            if path.exists():
                return True
        return False

    def conflicted_files(self) -> list[str]:
        """Return paths with unresolved merge conflicts."""
        return _split_z(self.run("diff", "--name-only", "-z", "--diff-filter=U").stdout)

    # ------------------------------------------------------------------ worktrees

    def worktree_add(self, path: Path, branch: str, *, start_point: str | None = None) -> None:
        """Add a worktree at ``path`` on ``branch``.

        With ``start_point`` the branch is created there; otherwise it must already exist
        (locally, or as a single remote-tracking branch git can guess).
        """
        if start_point is not None:
            self.run("worktree", "add", "--quiet", "-b", branch, str(path), start_point)
        else:
            self.run("worktree", "add", "--quiet", str(path), branch)

    def worktree_remove(self, path: Path, force: bool = False) -> None:
        """Remove the worktree at ``path`` (``force`` discards local changes)."""
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        self.run(*args)

    def worktree_list(self) -> list[Path]:
        """Return the paths of all worktrees, the main one first."""
        out = self._out("worktree", "list", "--porcelain")
        return [
            Path(line[len("worktree ") :])
            for line in out.splitlines()
            if line.startswith("worktree ")
        ]

    # ------------------------------------------------------------------ branches

    def checkout(self, branch: str, *, create_from: str | None = None) -> None:
        """Switch to ``branch``, creating it from ``create_from`` when given."""
        if create_from is not None:
            self.run("switch", "--quiet", "-c", branch, create_from)
        else:
            self.run("switch", "--quiet", branch)

    def reset_hard(self, ref: str) -> None:
        """Reset HEAD, index and working tree to ``ref``."""
        self.run("reset", "--quiet", "--hard", ref)
