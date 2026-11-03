"""Thin, typed wrapper around the ``git`` executable (ADR 0007).

Every git call in Forge goes through :class:`Git`. Commands run through ``subprocess`` with a
forced ``C`` locale so that output can be parsed on any machine, and with terminal prompts and
optional locks disabled so that nothing blocks or interferes with the user's own git usage.

A non-zero exit raises :class:`GitError`, except for operations whose failure is an expected
outcome: pushes that lose a compare-and-swap or fast-forward check (:class:`PushResult`),
rebases that stop on conflicts (:class:`RebaseResult`), ``rev_parse`` of a missing ref
(``None``) and ``is_ancestor`` answering no (``False``). A push refused by the user's
``pre-push`` hook raises :class:`PushHookRejected`, a :class:`GitError` subclass, so that callers
can tell it apart from a lost lease (:attr:`PushResult.rejected`).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

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
_RERERE: tuple[str, ...] = ("-c", "rerere.enabled=true", "-c", "rerere.autoupdate=true")
# rerere.c prints one of these (to stderr) for each path it resolved from a recorded
# resolution: "Staged" with rerere.autoupdate, "Resolved" without. The C locale is forced.
_RERERE_REUSED = re.compile(r"^(?:Staged|Resolved) '(.+)' using previous resolution\.$")

ResetMode = Literal["hard", "mixed", "soft"]
"""``git reset`` modes accepted by :meth:`Git.reset`."""


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


class PushHookRejected(GitError):
    """The user's ``pre-push`` hook refused a push; nothing was sent to the remote.

    ``stderr`` holds the hook's output (and git's). Raised only by :meth:`Git.push` without
    ``no_verify``. Git runs the hook even when a ref is about to be rejected for a stale lease,
    so a hook rejection takes precedence: the lease is not evaluated by the remote.
    """


class GitAddFailed(GitError):
    """``git add -A`` failed in :meth:`Git.commit_all` (e.g. an unreadable file); no commit
    was attempted, so no commit hook ran."""


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
    """Outcome of a rebase step. When ``ok`` is False the rebase is left in progress.

    ``conflicts`` are the paths still unmerged at the stop. ``rerere`` are the paths that
    conflicted at the stop but that rerere resolved with a recorded resolution (staged with
    ``rerere.autoupdate``): they are no longer unmerged, so they appear only here.
    """

    ok: bool
    conflicts: list[str] = field(default_factory=list)
    rerere: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CommitInfo:
    """One commit as returned by :meth:`Git.commits`."""

    sha: str
    author: str
    author_email: str
    date: datetime  # committer date, timezone-aware
    subject: str
    trailers: dict[str, str]


@dataclass(frozen=True)
class CommitFiles:
    """A commit with the paths it changed, as returned by :meth:`Git.commits_with_files`.

    ``files`` are repository-relative paths changed versus the first parent, in git's order.
    Renames are not detected: a renamed file appears as its old and its new path.
    """

    commit: CommitInfo
    files: tuple[str, ...]


@dataclass(frozen=True)
class RemoteTag:
    """A tag ref on a remote, as listed by ``git ls-remote``.

    ``object`` is what ``refs/tags/<name>`` holds: the tag object of an annotated tag, or the
    tagged object itself for a lightweight tag. ``target`` is the peeled object (the commit
    an annotated tag points to; equal to ``object`` for a lightweight tag).
    """

    name: str
    object: str
    target: str


_ERE_SPECIAL = re.compile(r"([.\[\]()*+?{}|^$\\])")


def _ere_literal(text: str) -> str:
    """Escape ``text`` for a POSIX extended regular expression (as used by ``git log -G``)."""
    return _ERE_SPECIAL.sub(r"\\\1", text)


def _has_line_starting_with(content: bytes, prefix: str) -> bool:
    """True when a line of ``content`` (LF or CRLF endings) starts with ``prefix``."""
    wanted = prefix.encode("utf-8")
    return any(line.rstrip(b"\r").startswith(wanted) for line in content.split(b"\n"))


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


def _has_ref_status(porcelain_stdout: str) -> bool:
    """Return True when ``git push --porcelain`` output reports the status of at least one ref."""
    for line in porcelain_stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and len(parts[0]) == 1:
            return True
    return False


def _split_z(output: str) -> list[str]:
    return [item for item in output.split("\x00") if item]


_COMMIT_FORMAT = "%x00".join(["%H", "%an", "%ae", "%cI", "%s", "%(trailers:only,unfold)"])
"""``git log`` format of one commit: the :class:`CommitInfo` fields separated by NUL."""

_COMMIT_FIELDS = 6


def _commit_from_fields(fields: Sequence[str]) -> CommitInfo:
    """Build a :class:`CommitInfo` from the fields printed by :data:`_COMMIT_FORMAT`."""
    sha, author, email, date, subject, trailers = fields
    return CommitInfo(
        sha=sha,
        author=author,
        author_email=email,
        date=datetime.fromisoformat(date),
        subject=subject,
        trailers=_parse_trailers(trailers),
    )


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

    def dirty_files(self) -> list[str]:
        """Paths a ``git add -A`` + commit would change: tracked changes versus HEAD (staged or
        not, deletions included, renames as both paths) and untracked, non-ignored files.

        Repository-relative, sorted, without duplicates.
        """
        tracked = _split_z(
            self.run(
                "-c",
                "diff.relative=false",
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                "HEAD",
                "--",
            ).stdout
        )
        untracked = _split_z(
            self.run("ls-files", "--others", "--exclude-standard", "--full-name", "-z", ":/").stdout
        )
        return sorted({*tracked, *untracked})

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

    def update_ref(self, ref: str, sha: str, *, old: str | None = None) -> bool:
        """Point ``ref`` (a full ref name) at ``sha``, creating it if needed.

        With ``old`` the update is a compare-and-swap (``git update-ref <ref> <sha> <old>``):
        when ``ref`` no longer points at ``old`` nothing changes and False is returned. Any
        other failure raises :class:`GitError`. Returns True when the ref was updated.
        """
        if old is None:
            self.run("update-ref", ref, sha)
            return True
        args = ("update-ref", ref, sha, old)
        proc = self.run(*args, check=False)
        if proc.returncode == 0:
            return True
        if self.run("check-ref-format", ref, check=False).returncode != 0:
            raise GitError(args, proc.returncode, proc.stderr)  # not a lost race
        if self.rev_parse(ref) != self._resolve_object(old):
            return False  # moved (or deleted) since ``old`` was read
        raise GitError(args, proc.returncode, proc.stderr)

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

    def fetch(
        self,
        remote: str = "origin",
        prune: bool = True,
        refspecs: Sequence[str] | None = None,
    ) -> None:
        """Fetch from ``remote`` (pruning deleted branches by default).

        Without ``refspecs`` git uses the remote's configured refspec, which on a
        ``--single-branch`` clone covers one branch only. With ``refspecs`` exactly those are
        fetched (for example ``+refs/heads/task/*:refs/remotes/origin/task/*``); ``prune``
        then only removes stale refs under their destinations.
        """
        args = ["fetch", "--quiet"]
        if prune:
            args.append("--prune")
        args.append(remote)
        if refspecs:
            args += ["--", *refspecs]
        self.run(*args)

    def is_shallow(self) -> bool:
        """Return True when the repository is a shallow clone (missing older history)."""
        return self._out("rev-parse", "--is-shallow-repository") == "true"

    # ------------------------------------------------------------------ history and diffs

    def merge_base(self, a: str, b: str) -> str:
        """Return the best common ancestor of ``a`` and ``b``."""
        return self._out("merge-base", a, b)

    def diff_names(self, a: str, b: str) -> list[str]:
        """Return paths that differ between the trees of ``a`` and ``b`` (two-dot diff)."""
        return _split_z(self.run("diff", "--name-only", "-z", a, b, "--").stdout)

    def diff_bytes(self, a: str, b: str) -> bytes:
        """Return the patch between the trees of ``a`` and ``b`` (two-dot diff) as raw bytes.

        The output is the file contents as stored, whatever their encoding (decode it with
        ``errors="replace"`` for display). User configuration cannot change its shape: no
        color, no textconv filters, no external diff driver, ``a/``/``b/`` prefixes (even with
        ``diff.noprefix`` or ``diff.mnemonicPrefix``) and paths relative to the repository
        root (even with ``diff.relative``).
        """
        return self._run_bytes(
            "-c",
            "diff.relative=false",
            "diff",
            "--no-color",
            "--no-textconv",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            a,
            b,
            "--",
        ).stdout

    def changed_files_since(self, base: str, head: str = "HEAD") -> list[str]:
        """Return paths changed on ``head`` since its merge base with ``base`` (``base...head``)."""
        return _split_z(self.run("diff", "--name-only", "-z", f"{base}...{head}", "--").stdout)

    def commits(self, range_: str, *, first_parent: bool = False) -> list[CommitInfo]:
        """Return commits in ``range_`` (any ``git log`` revision range), newest first.

        With ``first_parent`` only the first parent of each merge is followed
        (``--first-parent``), so commits brought in by merges are skipped.
        """
        args = ["log", f"--format={_COMMIT_FORMAT}%x1e"]
        if first_parent:
            args.append("--first-parent")
        out = self.run(*args, range_, "--").stdout
        result: list[CommitInfo] = []
        for record in out.split(_RECORD_SEP):
            record = record.lstrip("\n")
            if not record:
                continue
            result.append(_commit_from_fields(record.split(_FIELD_SEP, _COMMIT_FIELDS - 1)))
        return result

    def commits_with_files(self, range_: str) -> list[CommitFiles]:
        """Return non-merge commits in ``range_`` with the paths each one changed, newest first.

        Paths are repository-relative and compared with the commit's first parent (a root
        commit lists every file it adds). Merge commits are excluded (``--no-merges``); a
        commit that changes nothing (e.g. an empty claim commit) has ``files == ()``. Renames
        are not detected (``--no-renames``), so a rename lists both the old and the new path.
        Any path name is supported (spaces, newlines, non-ASCII): the output is NUL-delimited.
        """
        # With -z git prints, per commit, the formatted header and a NUL; then, only if the
        # commit changed files, "\n" followed by NUL-terminated paths. Nothing separates the
        # last path from the next header, so the format starts with an empty field: split on
        # NUL, every header is an empty token (never a valid path) plus _COMMIT_FIELDS tokens,
        # and the paths are the tokens up to the next empty one.
        out = self.run(
            "-c",
            "log.showRoot=true",
            "-c",
            "log.showSignature=false",
            "-c",
            "diff.relative=false",
            "log",
            "-z",
            "--no-merges",
            "--no-renames",
            "--name-only",
            f"--format=%x00{_COMMIT_FORMAT}",
            range_,
            "--",
        ).stdout
        tokens = out.split(_FIELD_SEP)
        result: list[CommitFiles] = []
        i = 0
        while i < len(tokens) - 1:
            header = tokens[i + 1 : i + 1 + _COMMIT_FIELDS]
            if (
                tokens[i] != ""
                or len(header) != _COMMIT_FIELDS
                or not _FULL_SHA.fullmatch(header[0])
            ):
                raise GitError(["log", range_], 0, f"unexpected git log output near {header!r}")
            i += 1 + _COMMIT_FIELDS
            files: list[str] = []
            while i < len(tokens) and tokens[i] != "":
                files.append(tokens[i])
                i += 1
            if files:
                if not files[0].startswith("\n"):
                    raise GitError(["log", range_], 0, f"unexpected git log output {files[0]!r}")
                files[0] = files[0][1:]
            result.append(CommitFiles(_commit_from_fields(header), tuple(files)))
        if i < len(tokens) and tokens[i] != "":
            raise GitError(["log", range_], 0, "unexpected trailing git log output")
        return result

    def commit_date(self, ref: str) -> datetime:
        """Return the committer date of ``ref`` as a timezone-aware datetime."""
        return datetime.fromisoformat(self._out("log", "-1", "--format=%cI", ref, "--"))

    def read_file(self, rev: str, path: str) -> bytes | None:
        """Return the content of ``path`` (repository-relative) in commit ``rev``.

        Returns None when the commit has no such file (or it is not a regular file blob).
        An unknown ``rev`` raises :class:`GitError`. The content is returned as stored, with
        no text conversion.
        """
        if self.rev_parse(rev) is None:
            raise GitError(("rev-parse", rev), 1, f"unknown revision {rev!r}")
        args = ("rev-parse", "--verify", "--quiet", f"{rev}:{path}")
        proc = self.run(*args, check=False)
        if proc.returncode == 1:
            return None
        if proc.returncode != 0:
            raise GitError(args, proc.returncode, proc.stderr)
        blob = proc.stdout.strip()
        if self._out("cat-file", "-t", blob) != "blob":
            return None
        return self._run_bytes("cat-file", "blob", blob).stdout

    def first_parent_line_origin(self, rev: str, path: str, prefix: str) -> str | None:
        """Return the commit since which ``path`` has a line starting with ``prefix`` on ``rev``.

        Only the first-parent chain of ``rev`` is considered, so on a default branch the
        answer is the commit that landed the line there: the merge commit of a merged pull
        request, the squash commit, or the rebased commit (the commit on the second-parent
        side of a merge is never returned). When the line was added, removed and added again,
        the latest addition counts: the result is the oldest commit ``c`` such that ``c`` and
        every first-parent descendant of it up to ``rev`` contain the line.

        Returns None when ``path`` at ``rev`` has no such line. Requires full history (callers
        check :meth:`is_shallow`).

        Candidates come from ``git log --first-parent --diff-merges=first-parent -G`` (commits
        whose diff against their first parent adds or removes a matching line; merges are
        diffed against their first parent too), newest first. The presence of the line can
        only change at a candidate, so the first candidate whose first parent lacks the line
        is the answer. Each check reads the file content, which keeps the search exact even
        for lines that were only edited (e.g. a heading's date changed).
        """
        content = self.read_file(rev, path)
        if content is None or not _has_line_starting_with(content, prefix):
            return None
        out = self._out(
            "-c",
            "log.showRoot=true",
            "-c",
            "diff.relative=false",
            "log",
            "--no-patch",
            "--first-parent",
            "--diff-merges=first-parent",
            "--no-textconv",
            "--no-ext-diff",
            f"-G^{_ere_literal(prefix)}",
            "--format=%H",
            rev,
            "--",
            path,
        )
        for sha in out.split():
            parent = self.rev_parse(f"{sha}^1")
            if parent is None:
                return sha  # root commit
            before = self.read_file(parent, path)
            if before is None or not _has_line_starting_with(before, prefix):
                return sha
        raise GitError(
            ("log", "--first-parent", rev, "--", path),
            0,
            f"cannot find the commit that introduced {prefix!r} in {path} on {rev}",
        )

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

    def commit_all(self, message: str) -> str:
        """Stage every change (``git add -A``) and commit it with ``message``; return the sha.

        The user's commit hooks (``pre-commit``, ``commit-msg``, ...) run. When git fails, for
        example because a hook rejects the commit or there is nothing to commit,
        :class:`GitError` is raised with git's and the hooks' output in ``stderr``. When the
        staging itself fails, :class:`GitAddFailed` is raised instead (nothing is committed).
        """
        add = self.run("add", "-A", check=False)
        if add.returncode != 0:
            raise GitAddFailed(("add", "-A"), add.returncode, add.stderr)
        args = ("commit", "--quiet", "-m", message)
        proc = self.run(*args, check=False)
        if proc.returncode != 0:
            raise GitError(args, proc.returncode, proc.stderr + proc.stdout)
        return self._out("rev-parse", "HEAD")

    def tree_of(self, ref: str) -> str:
        """Return the tree sha of commit ``ref``."""
        return self._out("rev-parse", f"{ref}^{{tree}}")

    def worktree_tree(self) -> str:
        """Return the tree sha that ``git add -A`` followed by a commit would record now.

        Tracked changes (staged or not, deletions included) and untracked, non-ignored files
        are included. The real index is never touched: a copy of it is used (keeping its stat
        cache), so this is safe to call at any time. Writes blobs to the object store.
        """
        index = Path(self._out("rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = self.cwd / index
        # A split index would make the scratch copy share (and write) shared index files.
        no_split = ("-c", "core.splitIndex=false")
        with tempfile.TemporaryDirectory(prefix="forge-index-") as tmp:
            scratch = Path(tmp) / "index"
            env = {"GIT_INDEX_FILE": str(scratch)}
            if index.exists():
                scratch.write_bytes(index.read_bytes())
            else:
                self.run(*no_split, "read-tree", "HEAD", env=env)
            args = (*no_split, "add", "-A", "--", ":/")
            add = self.run(*args, check=False, env=env)
            if add.returncode != 0:
                failed = GitAddFailed(args, add.returncode, add.stderr)
                lines = add.stderr.strip().splitlines()
                errors = [x for x in lines if x.startswith("error:")] or lines[-1:]
                failed.message = (
                    "cannot read the working tree to compare it (`git add -A` on a scratch "
                    "index failed; your index was not touched): "
                    + ("; ".join(errors) or "no error output")
                )
                failed.hint = (
                    "fix what git reports (e.g. make the file readable, or ignore it in "
                    ".gitignore), then run the command again"
                )
                raise failed
            return self.run(*no_split, "write-tree", env=env).stdout.strip()

    def index_matches(self, ref: str) -> bool:
        """Return True when the index (staged content) equals the tree of commit ``ref``.

        Read-only: ``git diff --cached --quiet`` never writes the index.
        """
        args = ("diff", "--cached", "--quiet", "--no-ext-diff", ref, "--")
        proc = self.run(*args, check=False)
        if proc.returncode in (0, 1):
            return proc.returncode == 0
        raise GitError(args, proc.returncode, proc.stderr)

    def commit_tree(
        self,
        tree: str,
        parents: Sequence[str],
        message: str,
        trailers: Mapping[str, str] | None = None,
        *,
        date: datetime | None = None,
    ) -> str:
        """Create a commit object without touching HEAD, the index or the working tree.

        ``date`` (timezone-aware) sets both the author and the committer date; by default git
        uses the current time. Forge passes its injectable clock here, so that claim order and
        staleness follow the same clock in production and in tests.
        """
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        args += ["-F", "-"]
        env: dict[str, str] | None = None
        if date is not None:
            if date.tzinfo is None:
                raise ValueError("commit_tree date must be timezone-aware")
            stamp = f"{int(date.timestamp())} +0000"
            env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        return self.run(*args, input=format_message(message, trailers), env=env).stdout.strip()

    def commit_file(
        self,
        parent: str,
        path: str,
        content: bytes,
        message: str,
        trailers: Mapping[str, str] | None = None,
        *,
        date: datetime | None = None,
    ) -> str:
        """Create a commit on top of ``parent`` whose tree is ``parent``'s with ``path`` set to
        ``content``; return its sha.

        Nothing is checked out: HEAD, the index, the working tree and every ref stay as they
        are (a scratch index is used), so the commit can be prepared from any state and then
        pushed by sha. ``path`` is repository-relative; an existing file keeps its mode, a new
        one is a regular file (``100644``). ``date`` is as in :meth:`commit_tree`.
        """
        mode = "100644"
        listing = self._out("ls-tree", "-z", parent, "--", path).rstrip("\x00")
        if listing:
            meta, _, name = listing.partition("\t")
            entry_mode, entry_type, _sha = meta.split(" ")
            if name != path or entry_type != "blob":
                raise GitError(("ls-tree", parent, path), 0, f"{path} is not a file in {parent}")
            mode = entry_mode
        blob = self._hash_blob(content)
        with tempfile.TemporaryDirectory(prefix="forge-index-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            no_split = ("-c", "core.splitIndex=false")
            self.run(*no_split, "read-tree", parent, env=env)
            self.run(
                *no_split,
                "update-index",
                "--add",
                "--cacheinfo",
                f"{mode},{blob},{path}",
                env=env,
            )
            tree = self.run(*no_split, "write-tree", env=env).stdout.strip()
        return self.commit_tree(tree, [parent], message, trailers, date=date)

    def _hash_blob(self, content: bytes) -> str:
        """Write ``content`` to the object store as a blob (no filters) and return its sha."""
        proc = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=self.cwd,
            env=self.env(),
            input=content,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise GitError(
                ("hash-object", "-w", "--stdin"),
                proc.returncode,
                proc.stderr.decode("utf-8", "replace"),
            )
        return proc.stdout.decode("ascii").strip()

    # ------------------------------------------------------------------ tags

    def create_annotated_tag(self, name: str, commit: str, message: str) -> str:
        """Create the local annotated tag ``name`` on ``commit``; return the tag object's sha.

        Refuses (``GitError``) when ``refs/tags/<name>`` already exists: it never moves a tag.
        The tagger is the repository's configured identity. The user's ``tag.gpgSign``
        setting applies.
        """
        self.run("tag", "--annotate", "--file=-", "--", name, commit, input=message + "\n")
        return self._resolve_object(f"refs/tags/{name}")

    def tag_target(self, name: str) -> str | None:
        """Return the commit the local tag ``name`` points to (peeled), or None when absent."""
        return self.rev_parse(f"refs/tags/{name}")

    def remote_tags(self, pattern: str = "*", remote: str = "origin") -> dict[str, RemoteTag]:
        """Return the tags on ``remote`` whose name matches ``pattern`` (a glob such as
        ``v*``), keyed by tag name, queried live with ``git ls-remote``."""
        refs = self.ls_remote(remote, f"refs/tags/{pattern}")
        tags: dict[str, RemoteTag] = {}
        for ref, sha in refs.items():
            if ref.endswith("^{}") or not ref.startswith("refs/tags/"):
                continue
            name = ref[len("refs/tags/") :]
            tags[name] = RemoteTag(name, sha, refs.get(f"{ref}^{{}}", sha))
        return tags

    def remote_tag(self, name: str, remote: str = "origin") -> RemoteTag | None:
        """Return the tag ``name`` on ``remote`` (queried live), or None when it is absent."""
        ref = f"refs/tags/{name}"
        refs = self._ls_remote_exact(remote, (ref, f"{ref}^{{}}"))
        sha = refs.get(ref)
        if sha is None:
            return None
        return RemoteTag(name, sha, refs.get(f"{ref}^{{}}", sha))

    def local_tag_message(self, name: str) -> str | None:
        """Return the message of the local annotated tag ``name``; None when it is absent or
        lightweight."""
        ref = f"refs/tags/{name}"
        proc = self.run("cat-file", "-t", ref, check=False)
        if proc.returncode != 0 or proc.stdout.strip() != "tag":
            return None
        return self.run("cat-file", "tag", ref).stdout.partition("\n\n")[2]

    def tag_message(self, tag: RemoteTag, remote: str = "origin") -> str | None:
        """Return the message of the annotated remote tag ``tag``, or None for a lightweight
        tag (one whose ref points straight at its target).

        The tag object is fetched by ref when it is not in the local object store; no local
        ref is written (``--no-tags --no-write-fetch-head``). A signed tag's message includes
        its signature block after the text.
        """
        if tag.object == tag.target:
            return None
        if not self._has_object(tag.object):
            self.run(
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                remote,
                f"refs/tags/{tag.name}",
            )
        if self._out("cat-file", "-t", tag.object) != "tag":
            return None
        raw = self.run("cat-file", "tag", tag.object).stdout
        return raw.partition("\n\n")[2]

    def _ls_remote_exact(self, remote: str, refs: Sequence[str]) -> dict[str, str]:
        """``ls-remote`` restricted to exactly ``refs`` (patterns match ref tails)."""
        found: dict[str, str] = {}
        for line in self._out("ls-remote", remote, *refs).splitlines():
            sha, _, ref = line.partition("\t")
            if ref in refs:
                found[ref] = sha
        return found

    def push_tag(self, name: str, remote: str = "origin") -> PushResult:
        """Push the local tag ``name`` to ``remote`` only if the remote does not have it yet.

        The push carries the lease ``refs/tags/<name>:`` (expected absent), so an existing
        remote tag is never overwritten: the result is then ``rejected`` (or ``ok`` when the
        remote already holds exactly this tag object, i.e. a re-run). The user's ``pre-push``
        hook is not run, because a tag carries no new code (ADR 0015).
        """
        ref = f"refs/tags/{name}"
        return self.push(ref, ref, lease=Lease(ref, None), remote=remote, no_verify=True)

    # ------------------------------------------------------------------ push

    def push(
        self,
        src: str,
        dst_ref: str,
        *,
        lease: Lease | None = None,
        remote: str = "origin",
        no_verify: bool = False,
    ) -> PushResult:
        """Push ``src`` (a sha or ref, empty to delete) to ``dst_ref`` on ``remote``.

        With a ``lease`` the push is a compare-and-swap (``--force-with-lease``); without one
        it is a plain push. ``lease.ref`` must equal ``dst_ref`` (``ValueError`` otherwise).
        ``no_verify`` skips the user's ``pre-push`` hook (``--no-verify``); Forge uses it for
        its own event pushes, which carry no user code (ADR 0009).

        Outcomes:

        - success: ``PushResult(ok=True, rejected=False)``;
        - local ``[rejected]`` (stale lease, non-fast-forward, fetch first, already exists):
          ``PushResult(ok=False, rejected=True)``;
        - ``[remote rejected]``: the server's wording does not distinguish a lost race from a
          permanent error, so the remote ref is queried. It is a lost race (``rejected=True``)
          only when the ref moved away from the expected value and is not the value we pushed.
          When it already holds the value we pushed, the push landed and ``ok=True``.
          Otherwise (hook, protected branch, ref name clash, ...) :class:`GitError` is raised.

        - the user's ``pre-push`` hook refused the push (without ``no_verify``): git reports
          no per-ref status and :class:`PushHookRejected` is raised. It takes precedence over
          a stale lease, because git runs the hook before the remote checks the lease.

        Any other failure raises :class:`GitError`.
        """
        if lease is not None and lease.ref != dst_ref:
            raise ValueError(f"lease ref {lease.ref!r} does not match push destination {dst_ref!r}")
        args = ["push", "--porcelain"]
        if no_verify:
            args.append("--no-verify")
        if lease is not None:
            args.append(lease.option())
        args += [remote, f"{src}:{dst_ref}"]
        proc = self.run(*args, check=False)
        output = proc.stderr + proc.stdout
        if proc.returncode == 0:
            return PushResult(ok=True, rejected=False, stderr=proc.stderr)
        # The pre-push hook runs before anything is sent; when it fails git prints no per-ref
        # status at all. An unknown source fails the same way, before the hook runs.
        if (
            not no_verify
            and not _has_ref_status(proc.stdout)
            and self._has_pre_push_hook()
            and (not src or self.rev_parse(src) is not None)
        ):
            raise PushHookRejected(args, proc.returncode, output)
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

    def _has_pre_push_hook(self) -> bool:
        """Return True when an executable ``pre-push`` hook exists (honours core.hooksPath)."""
        path = Path(self._out("rev-parse", "--git-path", "hooks/pre-push"))
        if not path.is_absolute():
            path = self.cwd / path
        return path.is_file() and os.access(path, os.X_OK)

    def _resolve_object(self, rev: str) -> str:
        """Return the object id ``rev`` names (a tag object stays a tag object)."""
        return self._out("rev-parse", "--verify", "--end-of-options", rev)

    def delete_remote_branch(
        self,
        branch: str,
        expected_sha: str,
        remote: str = "origin",
        *,
        no_verify: bool = False,
    ) -> PushResult:
        """Delete ``branch`` on ``remote`` only if it still points at ``expected_sha``.

        ``ok`` means the ref is absent afterwards (deleted by us or, concurrently, by someone
        else). On a re-run the ref may already be gone and the stale lease yields
        ``rejected``: callers must treat ``rejected`` with an absent remote ref as done.
        ``no_verify`` skips the user's ``pre-push`` hook, as in :meth:`push`.
        """
        ref = f"refs/heads/{branch}"
        return self.push(
            "", ref, lease=Lease(ref, expected_sha), remote=remote, no_verify=no_verify
        )

    # ------------------------------------------------------------------ rebase

    def rebase(
        self,
        onto: str,
        *,
        rerere: bool = False,
        keep_empty: bool = False,
        committer_date_is_author_date: bool = False,
    ) -> RebaseResult:
        """Rebase the current branch onto ``onto``.

        ``rerere`` enables recorded resolutions for this command only
        (``-c rerere.enabled=true -c rerere.autoupdate=true``); ``keep_empty`` keeps commits
        that start empty (``--keep-empty``); ``committer_date_is_author_date`` sets each
        rewritten commit's committer date to its author date. On conflicts the rebase is left
        in progress and the conflicted paths are returned. With ``rerere`` a stop may report
        no conflicts at all: rerere already resolved and staged them, and the caller only has
        to continue.
        """
        args: list[str] = [*_RERERE] if rerere else []
        args.append("rebase")
        if keep_empty:
            args.append("--keep-empty")
        if committer_date_is_author_date:
            args.append("--committer-date-is-author-date")
        args.append(onto)
        return self._rebase_step(tuple(args))

    def rebase_continue(self, *, rerere: bool = False) -> RebaseResult:
        """Continue an in-progress rebase without opening an editor.

        ``rerere`` passes the same per-command options as :meth:`rebase`, so the resolutions
        of this stop are recorded and later stops can reuse them. Options given to
        :meth:`rebase` (``--keep-empty``, dates) are kept by git in the rebase state.
        """
        args: list[str] = [*_RERERE] if rerere else []
        return self._rebase_step((*args, "rebase", "--continue"))

    def _rebase_step(self, args: tuple[str, ...]) -> RebaseResult:
        proc = self.run(*args, check=False, env=_NON_INTERACTIVE)
        if proc.returncode == 0:
            return RebaseResult(ok=True)
        if self.rebase_in_progress():
            unmerged = self.conflicted_files()
            return RebaseResult(
                ok=False,
                conflicts=unmerged,
                rerere=self._rerere_reused(proc.stderr + "\n" + proc.stdout, unmerged),
            )
        raise GitError(args, proc.returncode, proc.stderr)

    def _rerere_reused(self, output: str, unmerged: Sequence[str]) -> list[str]:
        """Paths of this stop that rerere resolved from a recorded resolution.

        A step runs until the next stop, and the sequencer stops at every commit that
        conflicted (even when rerere resolved everything), so the messages in ``output``
        belong to the current stop. Paths still unmerged (rerere's result left conflicts, or
        ``rerere.autoupdate`` was off) and paths unknown to the index are left out.
        """
        found: list[str] = []
        for line in output.splitlines():
            match = _RERERE_REUSED.match(line.strip("\r"))
            if match is None:
                continue
            path = match.group(1)
            if path in found or path in unmerged or not self._index_stages(path):
                continue
            found.append(path)
        return found

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

    def operation_in_progress(self) -> str | None:
        """Return the name of a merge, cherry-pick or revert in progress in this working tree
        (``"merge"``, ``"cherry-pick"`` or ``"revert"``), or None when there is none.

        Detected by ``MERGE_HEAD``, ``CHERRY_PICK_HEAD`` and ``REVERT_HEAD`` (resolved with
        ``rev-parse --git-path``, so a linked worktree checks its own state). Rebases are
        reported by :meth:`rebase_in_progress`.
        """
        for name, operation in (
            ("MERGE_HEAD", "merge"),
            ("CHERRY_PICK_HEAD", "cherry-pick"),
            ("REVERT_HEAD", "revert"),
        ):
            path = Path(self._out("rev-parse", "--git-path", name))
            if not path.is_absolute():
                path = self.cwd / path
            if path.exists():
                return operation
        return None

    def conflicted_files(self) -> list[str]:
        """Return paths with unresolved merge conflicts (unmerged index entries).

        Paths are relative to the repository root. This is also the list of unmerged files
        while a rebase is stopped.
        """
        return _split_z(self.run("diff", "--name-only", "-z", "--diff-filter=U").stdout)

    unmerged_files = conflicted_files

    # ------------------------------------------------------------------ conflict resolution

    def _run_bytes(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        """Run ``git <args>`` like :meth:`run` but with binary stdout (stderr is decoded)."""
        proc = subprocess.run(
            ["git", *args],
            cwd=self.cwd,
            env=self.env(),
            capture_output=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise GitError(args, proc.returncode, proc.stderr.decode("utf-8", "replace"))
        return proc

    def _index_stages(self, path: str) -> dict[int, str]:
        """Return ``{stage: blob sha}`` of the index entries for exactly ``path``."""
        out = self.run("--literal-pathspecs", "ls-files", "--stage", "-z", "--", path).stdout
        stages: dict[int, str] = {}
        for entry in _split_z(out):
            meta, _, name = entry.partition("\t")
            if name != path:
                continue
            _mode, sha, stage = meta.split(" ")
            stages[int(stage)] = sha
        return stages

    def stage_blob(self, stage: int, path: str) -> bytes | None:
        """Return the content of index stage ``stage`` (1 base, 2 ours, 3 theirs) of ``path``.

        ``path`` is relative to the repository root, as :meth:`conflicted_files` returns it.
        Returns None when that stage does not exist (e.g. no base for a file added on both
        sides, no ``theirs`` for a file deleted there). During a rebase stage 2 is the branch
        being rebased onto (upstream) and stage 3 the commit being replayed. The content is
        returned as stored, without text conversion.
        """
        if stage not in (1, 2, 3):
            raise ValueError(f"stage must be 1, 2 or 3, got {stage}")
        sha = self._index_stages(path).get(stage)
        if sha is None:
            return None
        return self._run_bytes("cat-file", "blob", sha).stdout

    def checkout_stage(self, path: str, stage: Literal["ours", "theirs"]) -> None:
        """Write the ``ours`` or ``theirs`` side of conflicted ``path`` to the working tree.

        Runs ``git checkout --ours|--theirs -- path`` (the path is taken literally). The path
        stays unmerged in the index until it is staged with :meth:`add_paths`. During a rebase
        ``ours`` is the upstream side.
        """
        if stage not in ("ours", "theirs"):
            raise ValueError(f"stage must be 'ours' or 'theirs', got {stage!r}")
        self.run("--literal-pathspecs", "checkout", f"--{stage}", "--", path)

    def restore_conflict(self, path: str) -> bool:
        """Re-create the conflict of a resolved ``path`` (``git checkout -m``).

        Uses the index's resolve-undo record, so it works for a path resolved during the
        current merge or rebase stop (by ``git add`` or by rerere). The path is unmerged again,
        with its stages and conflict markers. Returns False (nothing changed) when git has no
        record to re-create the conflict from.
        """
        proc = self.run("--literal-pathspecs", "checkout", "-m", "--", path, check=False)
        return proc.returncode == 0 and path in self.conflicted_files()

    def merge_file_union(self, ours: bytes, base: bytes, theirs: bytes) -> bytes:
        """Return the union merge of three file versions (``git merge-file --union -p``).

        Conflicting hunks keep the lines of both sides (ours first) without markers. The
        inputs are written to a temporary directory that is removed afterwards.
        """
        with tempfile.TemporaryDirectory(prefix="forge-merge-") as tmp:
            files = []
            for name, content in (("ours", ours), ("base", base), ("theirs", theirs)):
                target = Path(tmp) / name
                target.write_bytes(content)
                files.append(str(target))
            proc = self._run_bytes("merge-file", "--union", "-p", *files, check=False)
        # merge-file exits with the number of conflicts (0 with --union), negative on error.
        if proc.returncode != 0:
            raise GitError(
                ("merge-file", "--union", "-p"),
                proc.returncode,
                proc.stderr.decode("utf-8", "replace"),
            )
        return proc.stdout

    def add_paths(self, paths: Sequence[str]) -> None:
        """Stage exactly ``paths`` (taken literally), including deletions (``git add -A``).

        Staging a conflicted path marks it resolved. An empty sequence does nothing.
        """
        if paths:
            self.run("--literal-pathspecs", "add", "-A", "--", *paths)

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

    def reset(self, ref: str, mode: ResetMode) -> None:
        """Reset HEAD to ``ref``: ``hard`` (index and tree too), ``mixed`` (index) or ``soft``."""
        if mode not in ("hard", "mixed", "soft"):
            raise ValueError(f"reset mode must be hard, mixed or soft, got {mode!r}")
        self.run("reset", "--quiet", f"--{mode}", ref, "--")

    def reset_hard(self, ref: str) -> None:
        """Reset HEAD, index and working tree to ``ref``."""
        self.reset(ref, "hard")
