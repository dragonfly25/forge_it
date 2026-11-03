"""`forge release <milestone> [--version X.Y.Z] [--dry-run]` (spec §11.4, ADR 0015).

Releasing takes two runs of the same command around a normal pull request review. Forge
never pushes to the default branch:

1. **Release PR** (the default branch has no ``## [X.Y.Z]`` heading in ``CHANGELOG.md``):
   build the changelog section from the merged ``task/<n>`` pull requests of the milestone,
   commit it on ``release/vX.Y.Z`` (made from ``origin/<default>``, without touching the
   working tree), push that branch with an explicit lease and open or update the pull request
   ``chore(release): vX.Y.Z``. ``data.step`` is ``pr_opened``.
2. **Release** (the heading is on the default branch, i.e. the release PR was merged): create
   the annotated tag ``vX.Y.Z`` on the merged release PR's ``merge_commit_sha`` (the merge,
   squash or rebased commit GitHub put on the default branch), push it with a lease that
   expects the tag to be absent, publish the GitHub release and close the milestone.
   ``data.step`` is ``released``. Only when no merged release PR exists (the section was
   merged by hand) is the tag put on the first-parent commit of ``origin/<default>`` where
   the heading appeared.

Every step is a compare-and-swap or a read-then-write that a re-run detects as done:

- the release branch is always one Forge commit (trailers ``Forge-Release-Notes: vX.Y.Z``,
  ``Forge-Milestone`` and ``Forge-Section-Sha256``) on ``origin/<default>``. The tip counts as
  Forge's only when its trailers, its section hash and its content (exactly Forge's insertion
  into its parent's ``CHANGELOG.md``, nothing else changed) all match, so an amended commit
  counts as a hand edit. A regenerated section rebuilds that commit
  and pushes it with ``--force-with-lease=refs/heads/release/vX.Y.Z:<fetched tip or absent>``.
  A lost race is retried after a new fetch, an unchanged section pushes nothing, and a branch
  edited by hand is never rewritten (a warning lists the task PRs missing from it);
- the version's owner is recorded in the tag message (``vX.Y.Z (M1)``) and in a hidden marker
  closing the PR body and the release notes; a version owned by another milestone exits 5
  before any write;
- step 2 tags the merged release PR's ``merge_commit_sha``, verified to be on
  ``origin/<default>`` with the heading. An existing remote annotated tag ``vX.Y.Z (M1)`` on
  such a commit is accepted first, which is also the documented recovery when that check
  fails; without a merged release PR the first-parent walk is the fallback;
- an existing open release PR is reused (a concurrent creation, refused by GitHub with 422, is
  resolved by listing again);
- a tag already on the remote is accepted only when it points at the expected commit (exit 5
  otherwise); a tag push that loses a race is re-checked the same way;
- an existing GitHub release is left as it is, and a closed milestone is not closed again.

``--dry-run`` performs the same reads (including the fetch of remote-tracking refs) and
writes nothing: no commit, tag, push or GitHub write.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from packaging.version import InvalidVersion, Version

from forge.core.config import ForgeConfig
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.git import Git, GitError, Lease, PushHookRejected
from forge.core.github import GitHubClient, GitHubError, Issue, Milestone, PullRequest
from forge.core.output import Result
from forge.core.plan import Roadmap
from forge.core.refresh import TaskRecord, check_full_history, parse_records
from forge.core.spec import MILESTONE_RE
from forge.core.tasks import FORGE_TASK_LABEL, TaskType
from forge.core.team import Team

CHANGELOG_PATH = "CHANGELOG.md"
"""The changelog file, at the repository root (Keep a Changelog)."""

CHANGELOG_HEADER = (
    "# Changelog\n"
    "\n"
    "All notable changes to this project will be documented in this file.\n"
    "\n"
    "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),\n"
    "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).\n"
    "\n"
    "## [Unreleased]\n"
)
"""Header of a ``CHANGELOG.md`` created by ``forge release``."""

GROUPS: tuple[tuple[TaskType, str], ...] = (
    (TaskType.CONTRACT, "Contracts"),
    (TaskType.IMPL, "Features"),
    (TaskType.TEST, "Tests"),
    (TaskType.CHORE, "Chores"),
)
"""Changelog groups, in order: task type and ``###`` heading."""

NO_CHANGES_LINE = "- No task pull requests were merged for this milestone."
"""The only line of a section without entries."""

PUSH_ATTEMPTS = 3
"""Release branch pushes tried before giving up when the branch keeps changing remotely."""

Step = Literal["pr_opened", "released"]

_TASK_HEAD_RE = re.compile(r"task/([1-9][0-9]*)")
_UNRELEASED_RE = re.compile(r"^## \[unreleased\]", re.IGNORECASE)
_SECTION_DATE_RE = re.compile(r"^## \[[^\]]+\] - (\S+)")
_REMOTE = "origin"


# --------------------------------------------------------------------------- versions


def check_version(text: str) -> str:
    """Return ``text`` when it is a valid release version ``X.Y.Z``; raise otherwise.

    Valid means: a PEP 440 version written in canonical form with exactly three release
    numbers, without a ``v`` prefix, epoch, pre-, post- or dev-release, or local part.
    Raises :class:`PreconditionError` (exit 5) naming the fix.
    """
    fix = "pass --version X.Y.Z, e.g. --version 1.2.0 (no 'v' prefix, no pre-release suffix)"
    if text[:1] in ("v", "V"):
        raise PreconditionError(f"invalid --version {text!r}: drop the 'v' prefix", hint=fix)
    try:
        parsed = Version(text)
    except InvalidVersion:
        raise PreconditionError(f"invalid --version {text!r}: not a version", hint=fix) from None
    if (
        parsed.is_prerelease
        or parsed.is_postrelease
        or parsed.is_devrelease
        or parsed.local is not None
        or parsed.epoch != 0
        or len(parsed.release) != 3
    ):
        raise PreconditionError(
            f"invalid --version {text!r}: a release version is exactly X.Y.Z", hint=fix
        )
    if str(parsed) != text:
        raise PreconditionError(
            f"invalid --version {text!r}: write it as {parsed}", hint=f"pass --version {parsed}"
        )
    return text


def _release_version(tag: str) -> Version | None:
    if not tag.startswith("v"):
        return None
    try:
        return Version(check_version(tag[1:]))
    except PreconditionError:
        return None


def latest_version(tags: Iterable[str]) -> Version | None:
    """The highest ``vX.Y.Z`` release among tag names (others are ignored)."""
    versions = [v for v in (_release_version(t) for t in tags) if v is not None]
    return max(versions, default=None)


def version_hint(tags: Iterable[str]) -> str:
    """Hint for a missing ``--version``: the next patch and minor after the latest tag."""
    latest = latest_version(tags)
    if latest is None:
        return "no vX.Y.Z tag yet: pass --version 0.1.0 (or the version you want to release)"
    major, minor, patch = latest.release
    return (
        f"latest tag is v{latest}: pass --version {major}.{minor}.{patch + 1} (next patch) "
        f"or --version {major}.{minor + 1}.0 (next minor)"
    )


def tag_name(version: str) -> str:
    """The git tag of a release version: ``v1.2.0``."""
    return f"v{version}"


def release_branch(version: str) -> str:
    """The release branch of a version: ``release/v1.2.0``."""
    return f"release/{tag_name(version)}"


def release_title(version: str) -> str:
    """Title of the release pull request (and of its commit)."""
    return f"chore(release): {tag_name(version)}"


def heading_prefix(version: str) -> str:
    """Start of the changelog heading of ``version``: ``## [1.2.0]``."""
    return f"## [{version}]"


# --------------------------------------------------------------------------- entries


@dataclass(frozen=True)
class ChangelogEntry:
    """One changelog line: a merged pull request of a milestone task."""

    pr: int
    issue: int
    task_id: str
    type: TaskType
    title: str

    def line(self) -> str:
        """The Markdown list item: ``- <title> (#<pr>)``."""
        return f"- {self.title} (#{self.pr})"

    def to_data(self) -> dict[str, Any]:
        """JSON view."""
        return {
            "pr": self.pr,
            "issue": self.issue,
            "task": self.task_id,
            "type": self.type.value,
            "title": self.title,
        }


def strip_id_prefix(title: str, task_id: str) -> str:
    """Remove the ``[<task id>] `` prefix that ``forge plan apply`` puts on task titles."""
    prefix = f"[{task_id}]"
    if title.startswith(prefix):
        return title[len(prefix) :].strip() or title
    return title


def collect_entries(
    records: Sequence[TaskRecord], pulls: Sequence[PullRequest], default_branch: str
) -> list[ChangelogEntry]:
    """Entries for the merged pull requests into ``default_branch`` whose head is
    ``task/<n>`` for a task issue ``n`` in ``records``, ordered by pull request number.

    Every merged pull request of a task counts (a task merged in two PRs has two lines).
    """
    by_issue = {rec.issue.number: rec for rec in records}
    entries: list[ChangelogEntry] = []
    for pr in sorted(pulls, key=lambda p: p.number):
        if not pr.merged or pr.base != default_branch:
            continue
        match = _TASK_HEAD_RE.fullmatch(pr.head)
        if match is None:
            continue
        rec = by_issue.get(int(match.group(1)))
        if rec is None:
            continue
        entries.append(
            ChangelogEntry(
                pr=pr.number,
                issue=rec.issue.number,
                task_id=rec.meta.id,
                type=rec.meta.type,
                title=strip_id_prefix(rec.issue.title, rec.meta.id),
            )
        )
    return entries


def render_section(version: str, day: str, entries: Sequence[ChangelogEntry]) -> str:
    """The changelog section ``## [X.Y.Z] - <day>`` with its ``###`` groups.

    Groups follow :data:`GROUPS` and empty ones are omitted; lines are ordered by pull request
    number. A section without entries holds :data:`NO_CHANGES_LINE`.
    """
    lines = [f"{heading_prefix(version)} - {day}", ""]
    for kind, heading in GROUPS:
        group = sorted((e for e in entries if e.type == kind), key=lambda e: e.pr)
        if group:
            lines += [f"### {heading}", "", *(e.line() for e in group), ""]
    if not entries:
        lines += [NO_CHANGES_LINE, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


# --------------------------------------------------------------------------- changelog text


def _is_h2(line: str) -> bool:
    return line.startswith("## ") or line.rstrip() == "##"


def _lines(text: str) -> list[str]:
    body = text[:-1] if text.endswith("\n") else text
    return body.split("\n") if body else []


def _join(lines: Sequence[str]) -> str:
    return "\n".join(lines) + "\n" if lines else ""


def _trim_blank_tail(lines: Sequence[str]) -> list[str]:
    out = list(lines)
    while out and not out[-1].strip():
        out.pop()
    return out


def _next_h2(lines: Sequence[str], start: int) -> int:
    return next((j for j in range(start, len(lines)) if _is_h2(lines[j])), len(lines))


def find_section(text: str, version: str) -> tuple[int, int] | None:
    """Line span ``[start, end)`` of the ``## [version]`` section in ``text`` (LF endings).

    The section runs up to the next ``##`` heading or the end of the file.
    """
    lines = _lines(text)
    prefix = heading_prefix(version)
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            return i, _next_h2(lines, i + 1)
    return None


def extract_section(text: str, version: str) -> str | None:
    """The ``## [version]`` section of ``text`` (trailing blank lines dropped), or None."""
    span = find_section(text, version)
    if span is None:
        return None
    return _join(_trim_blank_tail(_lines(text)[span[0] : span[1]]))


def section_date(section: str) -> str | None:
    """The date of a section heading ``## [X.Y.Z] - <date>``, or None."""
    match = _SECTION_DATE_RE.match(section)
    return match.group(1) if match else None


def insert_section(text: str | None, section: str) -> tuple[str, bool]:
    """Insert ``section`` into the changelog ``text`` (LF endings); return ``(text, kept)``.

    - no file (``None``): a Keep a Changelog header with an empty ``## [Unreleased]``, then the
      section;
    - an ``## [Unreleased]`` section: the new section goes right below it. When Unreleased is
      empty the section follows its heading directly; when it has content, that content is
      left in place (it is never moved or rewritten) and the section goes after it, and
      ``kept`` is True so the caller can warn;
    - no Unreleased section: below the ``# `` title and its introduction, before the first
      ``##`` heading; with no title, at the top.
    """
    new = _lines(section.rstrip("\n") + "\n")
    if text is None:
        return CHANGELOG_HEADER + "\n" + _join(new), False
    lines = _lines(text)
    unreleased = next((i for i, x in enumerate(lines) if _UNRELEASED_RE.match(x)), None)
    kept = False
    if unreleased is not None:
        end = _next_h2(lines, unreleased + 1)
        if all(not x.strip() for x in lines[unreleased + 1 : end]):
            head = lines[: unreleased + 1]
        else:
            head = _trim_blank_tail(lines[:end])
            kept = True
    else:
        title = next((i for i, x in enumerate(lines) if x.startswith("# ")), None)
        if title is None:
            end = 0
            head = []
        else:
            end = _next_h2(lines, title + 1)
            head = _trim_blank_tail(lines[:end])
    tail = lines[end:]
    out = [*head, ""] if head else []
    out += new
    if tail:
        out += ["", *tail]
    return _join(out), kept


def replace_section(text: str, version: str, section: str) -> str:
    """Replace the ``## [version]`` section of ``text`` (LF endings) with ``section``."""
    span = find_section(text, version)
    if span is None:
        raise ValueError(f"no section for {version} in the changelog")
    lines = _lines(text)
    out = [*lines[: span[0]], *_lines(section.rstrip("\n") + "\n")]
    if span[1] < len(lines):
        out += ["", *lines[span[1] :]]
    return _join(out)


def decode_changelog(content: bytes, where: str) -> tuple[str, str]:
    """Decode ``CHANGELOG.md`` bytes; return ``(text with LF endings, newline style)``."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ForgeError(
            f"{CHANGELOG_PATH} on {where} is not valid UTF-8",
            hint=f"re-encode {CHANGELOG_PATH} as UTF-8 in a pull request, then re-run",
        ) from None
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), newline


def encode_changelog(text: str, newline: str) -> bytes:
    """Encode changelog text, restoring the file's newline style."""
    return (text.replace("\n", newline) if newline != "\n" else text).encode("utf-8")


# --------------------------------------------------------------------------- context


class ReleaseContext(Protocol):
    """What :func:`run_release` needs; ``forge.commands._context.AppContext`` satisfies it."""

    @property
    def config(self) -> ForgeConfig:
        """The project configuration."""
        ...

    @property
    def git(self) -> Git:
        """Git access to the repository."""
        ...

    @property
    def github(self) -> GitHubClient:
        """GitHub access to ``config.github.repo``."""
        ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ReleaseOptions:
    """Inputs of one ``forge release`` run.

    ``roadmap`` and ``team`` serve the soft lead check and the release title; either may be
    absent. ``clock`` gives the section date (UTC) and the release commit's date.
    """

    milestone: str
    version: str | None
    dry_run: bool = False
    roadmap: Roadmap | None = None
    team: Team | None = None
    clock: Callable[[], datetime] = field(default=_utc_now)


# --------------------------------------------------------------------------- checks


def is_lead(login: str, milestone: str, roadmap: Roadmap | None, team: Team | None) -> bool:
    """True when ``login`` is the roadmap lead of ``milestone`` or a team member with role
    ``lead`` (logins compared case-insensitively)."""
    who = login.lower()
    entry = roadmap.find(milestone) if roadmap is not None else None
    if entry is not None and entry.lead is not None and entry.lead.lower() == who:
        return True
    if team is not None:
        return any(m.github.lower() == who and "lead" in m.roles for m in team.members)
    return False


def _find_milestone(github: GitHubClient, title: str) -> Milestone:
    for milestone in github.list_milestones("all"):
        if milestone.title == title:
            return milestone
    raise PreconditionError(
        f"milestone {title!r} does not exist on GitHub",
        hint=f"check the milestone id; `forge plan apply {title}` creates it",
    )


def _issue_data(issue: Issue) -> dict[str, Any]:
    return {"number": issue.number, "title": issue.title, "url": issue.url}


def _fetch(git: Git, default_branch: str) -> None:
    refspecs = [
        f"+refs/heads/{default_branch}:refs/remotes/{_REMOTE}/{default_branch}",
        f"+refs/heads/release/*:refs/remotes/{_REMOTE}/release/*",
    ]
    try:
        git.fetch(_REMOTE, prune=True, refspecs=refspecs)
    except GitError as exc:
        detail = exc.stderr.strip().splitlines()[-1] if exc.stderr.strip() else str(exc)
        raise ForgeError(
            f"could not fetch from {_REMOTE}: {detail}",
            hint=f"check that the remote {_REMOTE!r} is reachable and that "
            f"project.default_branch ({default_branch!r}) exists there, then re-run",
        ) from exc


def _remote_head(git: Git, branch: str) -> str | None:
    return git.rev_parse(f"refs/remotes/{_REMOTE}/{branch}")


def _changelog_at(git: Git, rev: str, where: str) -> tuple[str | None, str]:
    content = git.read_file(rev, CHANGELOG_PATH)
    if content is None:
        return None, "\n"
    return decode_changelog(content, where)


# --------------------------------------------------------------------------- provenance

TRAILER_RELEASE_NOTES = "Forge-Release-Notes"
"""Trailer of Forge's release-notes commit on ``release/vX.Y.Z`` (value: the tag). Distinct
from ``Forge-Release``, the release-claim event trailer."""

TRAILER_MILESTONE = "Forge-Milestone"
"""Trailer naming the milestone a release-notes commit belongs to."""

TRAILER_SECTION_SHA256 = "Forge-Section-Sha256"
"""Trailer holding :func:`section_sha256` of the section the release-notes commit wrote."""


def section_sha256(section: str) -> str:
    """SHA-256 (hex) of a rendered changelog section, as recorded in the release commit."""
    return hashlib.sha256(section.encode("utf-8")).hexdigest()


_MARKER_RE = re.compile(r"<!-- forge:release-notes milestone=(\S+) -->")
_TAG_MESSAGE_RE = re.compile(r"v\S+ \((\S+)\)")


def release_marker(milestone: str) -> str:
    """Hidden line closing the release PR body and the GitHub release notes; it records the
    milestone the version belongs to."""
    return f"<!-- forge:release-notes milestone={milestone} -->"


def marker_milestone(text: str) -> str | None:
    """The milestone named by a :func:`release_marker` in ``text``, or None."""
    match = _MARKER_RE.search(text)
    return match.group(1) if match else None


def with_marker(section: str, milestone: str) -> str:
    """``section`` followed by the release marker (PR body and release notes)."""
    return section.rstrip("\n") + "\n\n" + release_marker(milestone) + "\n"


def tag_message(tag: str, milestone: str) -> str:
    """Message of Forge's annotated release tag: ``vX.Y.Z (M1)``."""
    return f"{tag} ({milestone})"


def tag_message_milestone(message: str | None) -> str | None:
    """The milestone named by a Forge tag message (first line ``vX.Y.Z (M1)``), or None."""
    if not message:
        return None
    match = _TAG_MESSAGE_RE.fullmatch(message.split("\n", 1)[0].strip())
    return match.group(1) if match else None


def _check_owner(
    owner: str | None, milestone: str, version: str, source: str, *, done: bool
) -> None:
    """Exit 5 when ``owner`` (from ``source``) is another milestone than ``milestone``."""
    if owner is None or owner == milestone:
        return
    verb = "was already released" if done else "is being released"
    raise PreconditionError(
        f"version {version} {verb} for {owner} ({source}), not for {milestone}",
        hint=f"pick another --version for {milestone} (see `git ls-remote --tags origin`)",
    )


# --------------------------------------------------------------------------- run


def run_release(ctx: ReleaseContext, options: ReleaseOptions) -> Result:
    """Run one ``forge release`` step (see the module docstring) and return its result.

    Exit codes: 0 (``data.step`` is ``pr_opened``, ``released`` or ``dry_run``, and a dry run
    names the step it would take in ``data.would``); 5 for a missing or invalid version, open
    tasks (listed in ``data.open_tasks``), a shallow clone, an unknown milestone, a tag that
    exists on another commit, or a version that belongs to another milestone; 2 when the
    user's ``pre-push`` hook refuses the release branch; 1 for other git or GitHub failures.
    """
    if MILESTONE_RE.fullmatch(options.milestone) is None:
        raise PreconditionError(
            f"invalid milestone id {options.milestone!r}",
            hint="pass the milestone id as in plans/roadmap.yml, e.g. M1",
        )
    version = check_version(options.version) if options.version is not None else None
    git, github = ctx.git, ctx.github
    default_branch = ctx.config.project.default_branch
    milestone_id = options.milestone
    result = Result(data={"milestone": milestone_id, "dry_run": options.dry_run})

    check_full_history(git, "release")
    _fetch(git, default_branch)

    login = github.current_user()
    lead = is_lead(login, milestone_id, options.roadmap, options.team)
    result.data.update(login=login, lead=lead)
    if not lead:
        result.warn(
            f"{login} is not the lead of {milestone_id} (plans/roadmap.yml `lead`) nor a "
            "team member with role lead; releasing anyway"
        )

    tags = git.remote_tags("v*", _REMOTE)
    if version is None:
        latest = latest_version(tags)
        result.code = ExitCode.PRECONDITION
        result.data["latest_tag"] = None if latest is None else f"v{latest}"
        result.error("--version is required", hint=version_hint(tags))
        return result
    tag = tag_name(version)
    result.data.update(version=version, tag=tag)
    latest_other = latest_version(t for t in tags if t != tag)
    if latest_other is not None and Version(version) <= latest_other:
        result.warn(
            f"version {version} is not newer than the latest tag v{latest_other}",
            hint="check --version; a backport release is fine, a typo is not",
        )

    milestone = _find_milestone(github, milestone_id)
    open_tasks = github.list_issues(state="open", labels=[FORGE_TASK_LABEL], milestone=milestone_id)
    if open_tasks:
        result.code = ExitCode.PRECONDITION
        result.data["open_tasks"] = [_issue_data(i) for i in open_tasks]
        listing = ", ".join(f"#{i.number}" for i in open_tasks)
        result.error(
            f"{len(open_tasks)} open task(s) in milestone {milestone_id}: {listing}",
            hint="finish and close them, or move them to another milestone, then re-run",
        )
        return result

    base = _remote_head(git, default_branch)
    if base is None:
        raise PreconditionError(
            f"{_REMOTE}/{default_branch} not found",
            hint="set project.default_branch in .forge/config.yml to the remote's default branch",
        )
    released = _has_heading(git, base, version)
    if not released:
        base, released = _merged_meanwhile(ctx, milestone_id, version, base)
    if released:
        _release(ctx, options, milestone, version, base, result)
    else:
        _open_release_pr(ctx, options, milestone_id, version, base, result)
    return result


def _merged_meanwhile(
    ctx: ReleaseContext, milestone_id: str, version: str, base: str
) -> tuple[str, bool]:
    """Handle a release PR merged after our fetch: re-fetch and report whether the default
    branch now has the heading (step 2 then runs). A merged release PR whose section is still
    not on the default branch (reverted?) exits 5 rather than opening a second PR."""
    git, github = ctx.git, ctx.github
    default_branch = ctx.config.project.default_branch
    branch = release_branch(version)
    merged = [
        p for p in github.list_pulls(state="closed", head=branch, base=default_branch) if p.merged
    ]
    if not merged:
        return base, False
    pr = max(merged, key=lambda p: p.number)
    _check_owner(
        marker_milestone(pr.body), milestone_id, version, f"pull request #{pr.number}", done=True
    )
    _fetch(git, default_branch)
    base = _remote_head(git, default_branch) or base
    if _has_heading(git, base, version):
        return base, True
    raise PreconditionError(
        f"release pull request #{pr.number} for {tag_name(version)} is merged, but "
        f"{_REMOTE}/{default_branch} has no `{heading_prefix(version)}` section in "
        f"{CHANGELOG_PATH} (was it reverted?)",
        hint="release another --version, or restore the section on the default branch "
        "through a pull request, then re-run forge release",
    )


# --------------------------------------------------------------------------- step 1


def _entries_for(ctx: ReleaseContext, milestone_id: str, result: Result) -> list[ChangelogEntry]:
    github = ctx.github
    warnings: list[str] = []
    issues = github.list_issues(state="all", labels=[FORGE_TASK_LABEL], milestone=milestone_id)
    records = parse_records(issues, warnings)
    pulls = github.list_pulls(state="closed", base=ctx.config.project.default_branch)
    entries = collect_entries(records, pulls, ctx.config.project.default_branch)
    for text in warnings:
        result.warn(text)
    if not entries:
        result.warn(f"no merged task pull requests found for {milestone_id}")
    result.data["entries"] = [e.to_data() for e in entries]
    return entries


@dataclass(frozen=True)
class _Tip:
    """The release branch tip, whether it is Forge's own release-notes commit, and the
    milestones named by ``Forge-Milestone`` trailers on the branch (``base..tip``)."""

    sha: str
    forge: bool
    owners: tuple[str, ...]


def is_forge_release_commit(git: Git, sha: str, version: str) -> bool:
    """True when commit ``sha`` is exactly a release-notes commit Forge would make.

    All of these must hold: the ``Forge-Release-Notes`` trailer names the tag; the commit has
    a single parent and changes only ``CHANGELOG.md``; the ``Forge-Section-Sha256`` trailer is
    the hash of the section in the commit; and the file is exactly that section inserted
    into the parent's ``CHANGELOG.md`` (same newline style). An amended or hand-edited commit
    keeps the trailers but fails one of the content checks.
    """
    commits = git.commits(f"{sha}^!")
    if len(commits) != 1:
        return False
    trailers = commits[0].trailers
    if trailers.get(TRAILER_RELEASE_NOTES) != tag_name(version):
        return False
    parent = git.rev_parse(f"{sha}^1")
    if parent is None or git.rev_parse(f"{sha}^2") is not None:
        return False
    if git.diff_names(parent, sha) != [CHANGELOG_PATH]:
        return False
    raw = git.read_file(sha, CHANGELOG_PATH)
    if raw is None:
        return False
    text, _ = decode_changelog(raw, sha[:12])
    section = extract_section(text, version)
    if section is None or trailers.get(TRAILER_SECTION_SHA256) != section_sha256(section):
        return False
    before, newline = _changelog_at(git, parent, parent[:12])
    expected, _ = insert_section(before, section)
    return encode_changelog(expected, newline) == raw


def _read_tip(git: Git, branch: str, version: str, base: str) -> _Tip | None:
    sha = _remote_head(git, branch)
    if sha is None:
        return None
    owners = tuple(
        dict.fromkeys(
            c.trailers[TRAILER_MILESTONE]
            for c in git.commits(f"{base}..{sha}")
            if TRAILER_MILESTONE in c.trailers
        )
    )
    return _Tip(sha, is_forge_release_commit(git, sha, version), owners)


def _open_release_pr(
    ctx: ReleaseContext,
    options: ReleaseOptions,
    milestone_id: str,
    version: str,
    base: str,
    result: Result,
) -> None:
    """Step 1: make ``release/vX.Y.Z`` one Forge commit on ``origin/<default>`` carrying the
    section, and open or update the release PR.

    The branch is always a single release-notes commit: a regenerated section is committed on
    the current ``origin/<default>`` again and pushed with a lease on the branch's current tip
    (never stacked), so whatever merge method GitHub uses, the commit that lands the heading
    on the default branch carries the whole section. A branch whose tip is not Forge's commit
    was edited by hand and is never rewritten.
    """
    git, github = ctx.git, ctx.github
    default_branch = ctx.config.project.default_branch
    tag = tag_name(version)
    branch = release_branch(version)
    ref = f"refs/heads/{branch}"
    result.data.update(branch=branch, step="dry_run" if options.dry_run else "pr_opened")
    if options.dry_run:
        result.data["would"] = "pr_opened"

    remote = git.remote_tag(tag, _REMOTE)
    if remote is not None:
        owner = tag_message_milestone(git.tag_message(remote, _REMOTE))
        _check_owner(owner, milestone_id, version, f"tag {tag}", done=True)
        raise PreconditionError(
            f"tag {tag} already exists on {_REMOTE} (commit {remote.target[:12]}) but "
            f"{_REMOTE}/{default_branch} has no `{heading_prefix(version)}` section in "
            f"{CHANGELOG_PATH}",
            hint="pick another --version, or delete the stray tag if it was pushed by mistake",
        )
    for pr in github.list_pulls(state="open", head=branch, base=default_branch):
        owner = marker_milestone(pr.body)
        _check_owner(owner, milestone_id, version, f"pull request #{pr.number}", done=False)

    now = options.clock()
    today = now.astimezone(UTC).date().isoformat()
    pushed: str | None = None
    kept_unreleased = False
    hand_edited = False
    entries: list[ChangelogEntry] | None = None
    section = ""
    for attempt in range(PUSH_ATTEMPTS):
        if attempt:
            _fetch(git, default_branch)
            base = _remote_head(git, default_branch) or base
        tip = _read_tip(git, branch, version, base)
        for owner in tip.owners if tip is not None else ():
            _check_owner(owner, milestone_id, version, f"branch {branch}", done=False)
        if entries is None:
            entries = _entries_for(ctx, milestone_id, result)
        existing: str | None = None
        if tip is not None:
            current, _ = _changelog_at(git, tip.sha, f"{_REMOTE}/{branch}")
            existing = extract_section(current, version) if current is not None else None
        if tip is not None and not tip.forge:
            hand_edited = True
            section = existing or render_section(version, today, entries)
            _warn_hand_edited(branch, existing, entries, result)
            break
        day = (section_date(existing) if existing else None) or today
        section = render_section(version, day, entries)
        if tip is not None and existing == section:
            result.info(f"branch {branch} already carries the {tag} section")
            break
        if options.dry_run:
            verb = "update" if tip is not None else "create"
            result.info(f"would {verb} branch {branch} with the {tag} section")
            break
        base_text, newline = _changelog_at(git, base, f"{_REMOTE}/{default_branch}")
        updated, kept_unreleased = insert_section(base_text, section)
        commit = git.commit_file(
            base,
            CHANGELOG_PATH,
            encode_changelog(updated, newline),
            release_title(version),
            {
                TRAILER_RELEASE_NOTES: tag,
                TRAILER_MILESTONE: milestone_id,
                TRAILER_SECTION_SHA256: section_sha256(section),
            },
            date=now,
        )
        expected = tip.sha if tip is not None else None
        try:
            outcome = git.push(commit, ref, lease=Lease(ref, expected), remote=_REMOTE)
        except PushHookRejected as exc:
            raise ForgeError(
                f"your pre-push hook refused the push of {branch}",
                hint="fix what the hook reports, then re-run forge release",
                code=ExitCode.AGENT_ACTION,
            ) from exc
        if outcome.ok:
            pushed = commit
            result.info(f"pushed {branch} ({commit[:12]})")
            break
        result.info(f"{branch} changed on {_REMOTE} while pushing; retrying")
    else:
        raise ForgeError(
            f"{branch} kept changing on {_REMOTE}; gave up after {PUSH_ATTEMPTS} attempts",
            hint="wait for the other release run to finish, then re-run forge release",
        )
    if kept_unreleased:
        result.warn(
            f"the [Unreleased] section of {CHANGELOG_PATH} has content; it was left in place "
            f"above the {tag} section",
            hint=f"move those notes into the {tag} section in the release pull request if "
            "they belong to this release",
        )
    result.data.update(section=section, commit=pushed, hand_edited=hand_edited)
    body = with_marker(section, milestone_id)
    result.data["pull"] = _ensure_pull(
        github, branch, default_branch, version, body, options, update=not hand_edited
    )
    if not options.dry_run:
        pull = result.data["pull"]
        result.info(
            f"merge pull request #{pull['number']}, then run `forge release {milestone_id} "
            f"--version {version}` again to tag and publish {tag}"
        )


def _warn_hand_edited(
    branch: str, existing: str | None, entries: Sequence[ChangelogEntry], result: Result
) -> None:
    missing = [e for e in entries if f"(#{e.pr})" not in (existing or "")]
    text = (
        f"{branch} was changed by hand (its tip is not Forge's unmodified release-notes "
        "commit); the section was not regenerated"
    )
    if missing:
        listing = ", ".join(f"#{e.pr} ({e.task_id})" for e in missing)
        result.warn(
            f"{text}; merged task pull requests missing from it: {listing}",
            hint=f"add them to {CHANGELOG_PATH} in the release pull request",
        )
    else:
        result.warn(text)
    result.data["missing"] = [e.to_data() for e in missing]


def _ensure_pull(
    github: GitHubClient,
    branch: str,
    default_branch: str,
    version: str,
    body: str,
    options: ReleaseOptions,
    *,
    update: bool = True,
) -> dict[str, Any] | None:
    title = release_title(version)
    existing = github.list_pulls(state="open", head=branch, base=default_branch)
    if options.dry_run:
        if not existing:
            return None
        pr = existing[0]
        return {"number": pr.number, "url": pr.url, "created": False, "updated": False}
    if not existing:
        try:
            pr = github.create_pull(title, body, branch, default_branch)
            return {"number": pr.number, "url": pr.url, "created": True, "updated": False}
        except GitHubError as exc:
            if exc.status != 422:
                raise
            existing = github.list_pulls(state="open", head=branch, base=default_branch)
            if not existing:
                raise
    pr = existing[0]
    changed = update and (pr.title != title or pr.body != body)
    if changed:
        pr = github.update_pull(
            pr.number,
            title=title if pr.title != title else None,
            body=body if pr.body != body else None,
        )
    return {"number": pr.number, "url": pr.url, "created": False, "updated": changed}


# --------------------------------------------------------------------------- step 2


def _has_heading(git: Git, sha: str, version: str) -> bool:
    text, _ = _changelog_at(git, sha, sha[:12])
    return text is not None and find_section(text, version) is not None


def _tag_target(
    ctx: ReleaseContext, milestone_id: str, version: str, base: str, result: Result
) -> str:
    """The commit to tag: the merged release PR's ``merge_commit_sha`` (merge, squash or
    rebased commit), verified to be on ``origin/<default>`` with the heading; without a
    merged release PR (merged by hand), the first-parent commit where the heading appeared.
    """
    git, github = ctx.git, ctx.github
    default_branch = ctx.config.project.default_branch
    branch = release_branch(version)
    tag = tag_name(version)

    def on_default(sha: str | None) -> bool:
        return (
            sha is not None
            and git.rev_parse(sha) == sha
            and git.is_ancestor(sha, base)
            and _has_heading(git, sha, version)
        )

    remote = git.remote_tag(tag, _REMOTE)
    if remote is not None:
        owner = tag_message_milestone(git.tag_message(remote, _REMOTE))
        _check_owner(owner, milestone_id, version, f"tag {tag}", done=True)
        if owner == milestone_id and on_default(remote.target):
            result.data["target_source"] = f"tag {tag}"
            return remote.target
    merged = [
        p for p in github.list_pulls(state="closed", head=branch, base=default_branch) if p.merged
    ]
    if merged:
        pr = github.get_pull(max(merged, key=lambda p: p.number).number)
        owner = marker_milestone(pr.body)
        _check_owner(owner, milestone_id, version, f"pull request #{pr.number}", done=True)
        sha = pr.merge_commit_sha
        if sha is None or not on_default(sha):
            raise PreconditionError(
                f"release pull request #{pr.number} was merged as {sha or 'unknown'}, but that "
                f"commit is not on {_REMOTE}/{default_branch} with the "
                f"`{heading_prefix(version)}` section",
                hint=f"find the {default_branch} commit that carries the section, tag it with "
                f'`git tag -a {tag} -m "{tag_message(tag, milestone_id)}" <commit> && '
                f"git push origin {tag}`, then re-run forge release",
            )
        result.data["target_source"] = f"pull request #{pr.number}"
        return sha
    target = git.first_parent_line_origin(base, CHANGELOG_PATH, heading_prefix(version))
    if target is None:  # pragma: no cover - the caller saw the heading at ``base``
        raise ForgeError(
            f"cannot find the {tag_name(version)} section on {_REMOTE}/{default_branch}"
        )
    result.info(
        f"no merged release pull request for {branch}: tagging {target[:12]}, the first "
        f"{default_branch} commit (first parent) with the section"
    )
    result.data["target_source"] = "first-parent history"
    return target


def _release(
    ctx: ReleaseContext,
    options: ReleaseOptions,
    milestone: Milestone,
    version: str,
    base: str,
    result: Result,
) -> None:
    git, github = ctx.git, ctx.github
    default_branch = ctx.config.project.default_branch
    tag = tag_name(version)
    milestone_id = milestone.title
    result.data.update(step="dry_run" if options.dry_run else "released")
    if options.dry_run:
        result.data["would"] = "released"

    target = _tag_target(ctx, milestone_id, version, base, result)
    text, _ = _changelog_at(git, target, target[:12])
    section = extract_section(text or "", version) or ""
    result.data.update(commit=target, section=section)

    # Read-only provenance checks first: nothing is written for a version owned elsewhere.
    remote = git.remote_tag(tag, _REMOTE)
    if remote is not None:
        owner = tag_message_milestone(git.tag_message(remote, _REMOTE))
        _check_owner(owner, milestone_id, version, f"tag {tag}", done=True)
    release = github.get_release_by_tag(tag)
    if release is not None:
        owner = marker_milestone(release.body)
        _check_owner(owner, milestone_id, version, f"GitHub release {tag}", done=True)
        if owner is None:
            result.warn(
                f"the GitHub release {tag} was not created by forge release; left unchanged"
            )

    open_prs = github.list_pulls(state="open", head=release_branch(version), base=default_branch)
    for pr in open_prs:
        result.warn(
            f"release pull request #{pr.number} is still open although {default_branch} "
            f"already has the {tag} section",
            hint=f"close #{pr.number}",
        )

    tag_pushed = _ensure_tag(git, tag, target, milestone_id, options.dry_run, result)
    result.data["tag_pushed"] = tag_pushed

    name = f"{tag} — {_milestone_title(options, milestone)}"
    created = False
    if release is None and not options.dry_run:
        try:
            release = github.create_release(tag, name, with_marker(section, milestone_id))
            created = True
        except GitHubError as exc:
            if exc.status != 422:
                raise
            release = github.get_release_by_tag(tag)
            if release is None:
                raise ForgeError(
                    f"GitHub refused to create the release for {tag}: {exc.message}",
                    hint=f"a draft release for {tag} may exist: publish or delete it on GitHub, "
                    "then re-run forge release",
                ) from exc
            _check_owner(
                marker_milestone(release.body),
                milestone_id,
                version,
                f"GitHub release {tag}",
                done=True,
            )
    if options.dry_run and release is None:
        result.info(f"would publish the GitHub release {name!r}")
    result.data["release"] = (
        None if release is None else {"name": release.name, "url": release.url, "created": created}
    )
    if created:
        result.info(f"published the GitHub release {name!r}")

    closed = False
    if milestone.state == "open":
        if options.dry_run:
            result.info(f"would close milestone {milestone.title}")
        else:
            github.close_milestone(milestone.number)
            closed = True
            result.info(f"closed milestone {milestone.title}")
    result.data["milestone_closed"] = closed or milestone.state == "closed"


def _milestone_title(options: ReleaseOptions, milestone: Milestone) -> str:
    entry = options.roadmap.find(milestone.title) if options.roadmap is not None else None
    return entry.title if entry is not None else milestone.title


def _ensure_tag(
    git: Git, tag: str, target: str, milestone: str, dry_run: bool, result: Result
) -> bool:
    """Make ``tag`` exist on the remote at ``target``; return True when this run pushed it."""
    remote = git.remote_tag(tag, _REMOTE)
    if remote is not None:
        _require_same_commit(tag, remote.target, target, "on " + _REMOTE)
        result.info(f"tag {tag} already on {_REMOTE} at {target[:12]}")
        return False
    local = git.tag_target(tag)
    if local is not None:
        _require_same_commit(tag, local, target, "locally")
        owner = tag_message_milestone(git.local_tag_message(tag))
        if owner != milestone:
            what = "is lightweight or not Forge's" if owner is None else f"names {owner}"
            raise PreconditionError(
                f"the local tag {tag} {what}; Forge pushes only an annotated tag "
                f"`{tag_message(tag, milestone)}`",
                hint=f"delete the local tag with `git tag -d {tag}`, then re-run forge release",
            )
    if dry_run:
        result.info(f"would tag {target[:12]} as {tag} and push it")
        return False
    if local is None:
        git.create_annotated_tag(tag, target, tag_message(tag, milestone))
    outcome = git.push_tag(tag, _REMOTE)
    if outcome.ok:
        result.info(f"pushed tag {tag} ({target[:12]})")
        return True
    remote = git.remote_tag(tag, _REMOTE)
    if remote is None:
        raise ForgeError(
            f"the push of tag {tag} was rejected: {outcome.reason}",
            hint="re-run forge release",
        )
    owner = tag_message_milestone(git.tag_message(remote, _REMOTE))
    _check_owner(owner, milestone, tag[1:], f"tag {tag}", done=True)
    _require_same_commit(tag, remote.target, target, "on " + _REMOTE)
    result.info(f"tag {tag} was pushed concurrently at {target[:12]}")
    return False


def _require_same_commit(tag: str, actual: str, expected: str, where: str) -> None:
    if actual != expected:
        raise PreconditionError(
            f"tag {tag} exists {where} on commit {actual[:12]}, not on {expected[:12]} where "
            f"the {tag} changelog section landed",
            hint=f"delete the wrong tag (`git tag -d {tag}` locally, or on the remote if it "
            "was pushed by mistake), or release another --version",
        )
