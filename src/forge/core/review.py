"""`forge review <pr>`: the review context of a pull request, and review submission
(spec §11.2, ADR 0012).

:func:`review_context` gathers what a reviewing agent needs: the pull request, its linked task
with the spec's acceptance criteria, the files and the diff (computed with git from
``refs/pull/<n>/head``), a zone check, CI and the existing reviews. It is read-only on GitHub;
locally it only fetches into ``refs/forge/review/<n>``.

:func:`submit_review` wraps ``GitHubClient.submit_review`` and appends a traceability footer.

Both enforce ``models.min_tier_for.reviewer`` (spec §6.5.4) through :func:`check_reviewer_tier`
unless forced.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.core.config import ForgeConfig
from forge.core.context import extract_acceptance_criteria, metadata_dict
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.git import Git, GitError
from forge.core.github import GitHubClient, GitHubError, PullRequest, ReviewEvent
from forge.core.lease import check_identity, load_task
from forge.core.models import Resolution, can_serve, resolve
from forge.core.output import Result
from forge.core.presets import Preset
from forge.core.profile import AgentProfile
from forge.core.refresh import TaskRecord
from forge.core.tiers import Effort, Tier
from forge.core.workspace import task_branch_issue
from forge.core.zones import ZonesConfig

DIFF_CAP_BYTES = 300_000
"""Largest diff (in UTF-8 bytes) put in the review context; longer diffs are cut."""

REVIEW_REF_PREFIX = "refs/forge/review/"
"""Local namespace where ``refs/pull/<n>/head`` is fetched (``refs/forge/review/<n>``)."""

REVIEWER_ROLE = "reviewer"
"""The ``models.min_tier_for`` key (and role) checked by ``forge review``."""

REVIEWER_TIER = Tier.STRONG
REVIEWER_EFFORT = Effort.HIGH
"""Declared tier and effort of the reviewer (spec §12.1), used for the footer's model."""

SUBMIT_EVENTS: dict[str, ReviewEvent] = {
    "approve": "APPROVE",
    "request-changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}
"""``--submit`` values and the GitHub review event each one maps to."""

FOOTER_PREFIX = "Reviewed with Forge"

_CLOSES_RE = re.compile(r"\bcloses\s+#(\d+)\b", re.IGNORECASE)


# --------------------------------------------------------------------------- inputs


@dataclass
class ReviewContext:
    """Inputs of ``forge review``. ``agent`` is ``None`` when there is no profile."""

    config: ForgeConfig
    git: Git
    github: GitHubClient
    repo_root: Path
    zones: ZonesConfig | None
    agent: AgentProfile | None
    presets: Mapping[str, Preset] | None = None
    remote: str = "origin"


# --------------------------------------------------------------------------- tier check


@dataclass(frozen=True)
class TierCheck:
    """Outcome of the ``models.min_tier_for.reviewer`` check for one agent.

    ``required`` is ``None`` when the project sets no minimum. ``forced`` is True when the
    agent cannot serve ``required`` and ``--force`` let the review run anyway.
    """

    agent: AgentProfile
    required: Tier | None
    served: bool
    forced: bool
    resolution: Resolution

    def to_data(self) -> dict[str, Any]:
        """JSON view: agent, providers, required tier and the resolved reviewer model."""
        res = self.resolution
        return {
            "agent": self.agent.id,
            "tool": self.agent.tool,
            "providers": list(self.agent.providers),
            "required_tier": None if self.required is None else str(self.required),
            "served": self.served,
            "forced": self.forced,
            "tier": str(res.tier),
            "effective_tier": None if res.effective_tier is None else str(res.effective_tier),
            "model": res.model,
            "effort": str(res.effort),
            "effort_value": res.effort_value,
            "notes": [{"level": str(n.level), "text": n.text} for n in res.notes],
        }

    @property
    def warning(self) -> str | None:
        """The warning to show when the check was forced."""
        if not self.forced:
            return None
        return (
            f"agent {self.agent.id} cannot serve tier {self.required} required by "
            f"models.min_tier_for.{REVIEWER_ROLE}; continuing because of --force"
        )


def check_reviewer_tier(
    config: ForgeConfig,
    agent: AgentProfile,
    *,
    force: bool = False,
    presets: Mapping[str, Preset] | None = None,
) -> TierCheck:
    """Check that ``agent`` can serve ``models.min_tier_for.reviewer`` (spec §6.5.4).

    Raises :class:`PreconditionError` (exit 5) when it cannot, unless ``force``. The returned
    resolution (roles ``["reviewer"]``, the reviewer's declared strong/high, falling back to
    the agent's highest tier) gives the model named in the review footer.
    """
    required = config.models.min_tier_for.get(REVIEWER_ROLE)
    served = required is None or can_serve(agent, required)
    if not served and not force:
        raise PreconditionError(
            f"agent {agent.id} cannot serve tier {required}, required for reviews by "
            f"models.min_tier_for.{REVIEWER_ROLE}",
            hint=f"review with an agent that maps tier {required} or stronger (--agent <id>), "
            "or pass --force to review anyway",
        )
    resolution = resolve(
        agent,
        declared_tier=REVIEWER_TIER,
        declared_effort=REVIEWER_EFFORT,
        roles=[REVIEWER_ROLE],
        constraints=config.models,
        presets=presets,
        any_tier=True,
    )
    return TierCheck(
        agent=agent, required=required, served=served, forced=not served, resolution=resolution
    )


# --------------------------------------------------------------------------- linked task


def linked_task_number(pr: PullRequest) -> tuple[int, str] | None:
    """The issue a pull request works on and how it was found (ADR 0012).

    The issue ``n`` of a ``task/<n>`` head wins (``"branch"``); otherwise the first
    ``Closes #n`` of the body (``"closes"``). ``None`` when neither is present.
    """
    issue = task_branch_issue(pr.head)
    if issue is not None:
        return issue, "branch"
    match = _CLOSES_RE.search(pr.body or "")
    if match is not None:
        return int(match.group(1)), "closes"
    return None


def _spec_criteria(repo_root: Path, record: TaskRecord, warnings: list[str]) -> dict[str, str]:
    meta = record.meta
    if not meta.covers:
        return {}
    try:
        text = (repo_root / meta.spec).read_text(encoding="utf-8")
    except FileNotFoundError:
        warnings.append(f"spec {meta.spec} not found; acceptance criteria not shown")
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        warnings.append(f"spec {meta.spec} could not be read ({exc}); criteria not shown")
        return {}
    found = extract_acceptance_criteria(text, meta.covers)
    for ac in meta.covers:
        if ac not in found:
            warnings.append(f"{ac} is not defined in {meta.spec}")
    return found


def _load_linked(
    github: GitHubClient, pr: PullRequest, warnings: list[str]
) -> tuple[TaskRecord, str] | None:
    link = linked_task_number(pr)
    if link is None:
        warnings.append(
            f"pull request #{pr.number} is not linked to a task "
            "(no task/<n> head branch and no `Closes #n` in the body)"
        )
        return None
    number, how = link
    try:
        record = load_task(github, number)
    except ForgeError as exc:
        warnings.append(f"pull request #{pr.number}: linked issue #{number} skipped: {exc.message}")
        return None
    return record, how


# --------------------------------------------------------------------------- diff and zones


@dataclass(frozen=True)
class DiffInfo:
    """Files and diff of a pull request, computed with git."""

    ref: str
    base: str
    head: str
    files: tuple[str, ...]
    diff: str
    diff_bytes: int
    diff_truncated: bool


def cap_diff(diff: str, cap: int = DIFF_CAP_BYTES) -> tuple[str, int, bool]:
    """Cut ``diff`` to at most ``cap`` UTF-8 bytes, at a line end when possible.

    Returns ``(text, full_size_in_bytes, truncated)``.
    """
    raw = diff.encode("utf-8")
    if len(raw) <= cap:
        return diff, len(raw), False
    cut = raw[:cap]
    newline = cut.rfind(b"\n")
    if newline >= 0:
        cut = cut[: newline + 1]
    return cut.decode("utf-8", errors="ignore"), len(raw), True


def _fetch(git: Git, remote: str, refspecs: list[str]) -> str | None:
    """Fetch ``refspecs`` from ``remote``; the error output on failure, else None."""
    try:
        git.fetch(remote, prune=False, refspecs=refspecs)
    except GitError as exc:
        return exc.stderr.strip()
    return None


def pull_diff(
    git: Git,
    pr_number: int,
    base_branch: str,
    warnings: list[str],
    remote: str = "origin",
    *,
    fallback_branch: str | None = None,
) -> DiffInfo:
    """Fetch ``refs/pull/<n>/head`` into ``refs/forge/review/<n>`` and diff it with git.

    ``base_branch`` is the pull request's base (``pr.base``), so a PR stacked on another
    branch shows only its own changes. The range is ``merge-base(<remote>/<base>, ref)..ref``.
    When the base branch cannot be fetched (deleted after the PR was opened) the
    ``fallback_branch`` (the default branch) is used, with a warning. A failure to fetch the
    pull request raises :class:`ForgeError` (exit 1).

    The patch is read as bytes (:meth:`Git.diff_bytes`) and decoded as UTF-8; bytes that are
    not valid UTF-8 (e.g. a Latin-1 file) are replaced by U+FFFD, with a warning.
    ``diff_bytes`` is the size of git's patch; the cap applies to the text put in the context.
    """
    ref = f"{REVIEW_REF_PREFIX}{pr_number}"
    pull_spec = f"+refs/pull/{pr_number}/head:{ref}"

    def spec(branch: str) -> str:
        return f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"

    upstream = f"refs/remotes/{remote}/{base_branch}"
    error = _fetch(git, remote, [pull_spec, spec(base_branch)])
    if error is not None:
        # Tell a missing base apart from a missing pull request.
        if _fetch(git, remote, [pull_spec]) is not None:
            raise ForgeError(
                f"cannot fetch pull request #{pr_number} from {remote}: {error}",
                hint="check the pull request number and your access to the remote",
            )
        if fallback_branch is None or fallback_branch == base_branch:
            raise ForgeError(
                f"pull request #{pr_number}: cannot fetch its base branch {base_branch} from "
                f"{remote}: {error}",
                hint="check that the base branch exists on the remote",
            )
        fallback_error = _fetch(git, remote, [spec(fallback_branch)])
        if fallback_error is not None:
            raise ForgeError(
                f"pull request #{pr_number}: cannot fetch {base_branch} nor {fallback_branch} "
                f"from {remote}: {fallback_error}",
                hint="check your access to the remote",
            )
        warnings.append(
            f"pull request #{pr_number}: its base branch {base_branch} could not be fetched; "
            f"the diff is computed against {fallback_branch}"
        )
        upstream = f"refs/remotes/{remote}/{fallback_branch}"
    head = git.rev_parse(ref)
    if head is None:
        raise ForgeError(f"pull request #{pr_number}: {ref} was not created by the fetch")
    base = git.merge_base(upstream, ref)
    files = tuple(git.diff_names(base, ref))
    raw = git.diff_bytes(base, ref)
    try:
        diff_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        diff_text = raw.decode("utf-8", errors="replace")
        warnings.append(
            f"pull request #{pr_number}: the diff is not valid UTF-8 (e.g. a Latin-1 file); "
            "the invalid bytes are replaced by U+FFFD"
        )
    text, _, truncated = cap_diff(diff_text)
    if truncated:
        warnings.append(
            f"pull request #{pr_number}: diff truncated to {DIFF_CAP_BYTES} bytes "
            f"(full size {len(raw)}); run `git diff {base[:12]} {ref}` for the rest"
        )
    return DiffInfo(ref, base, head, files, text, len(raw), truncated)


def zone_check(
    files: tuple[str, ...],
    zones: ZonesConfig | None,
    task_zones: tuple[str, ...] | None,
    warnings: list[str],
) -> dict[str, Any] | None:
    """Out-of-zone and unzoned files against the task's zones.

    *Unzoned* files match no zone at all. *Out-of-zone* files match another zone but none of
    the task's (each listed with the zones it belongs to). Without a task, only unzoned files
    are reported (``out_of_zone`` is ``None``). Without a zones config the check is ``None``.
    """
    if zones is None:
        return None
    unzoned = zones.unzoned_paths(files)
    out_of_zone: list[dict[str, Any]] | None = None
    if task_zones is not None:
        try:
            outside = zones.files_outside(files, task_zones)
        except ForgeError as exc:
            warnings.append(f"zone check skipped: {exc.message}")
            return None
        out_of_zone = [
            {"path": path, "zones": zones.zones_for_path(path)}
            for path in outside
            if path not in unzoned
        ]
    return {
        "task_zones": None if task_zones is None else list(task_zones),
        "out_of_zone": out_of_zone,
        "unzoned": unzoned,
        "unzoned_policy": zones.unzoned,
    }


# --------------------------------------------------------------------------- context


def _pull_data(pr: PullRequest) -> dict[str, Any]:
    return {
        "number": pr.number,
        "title": pr.title,
        "url": pr.url,
        "author": pr.author,
        "head": pr.head,
        "head_sha": pr.head_sha,
        "base": pr.base,
        "state": pr.state,
        "draft": pr.draft,
        "merged": pr.merged,
        "requested_reviewers": list(pr.requested_reviewers),
        "body": pr.body,
    }


def _task_data(
    record: TaskRecord, how: str, repo_root: Path, warnings: list[str]
) -> dict[str, Any]:
    issue = record.issue
    return {
        "issue": issue.number,
        "id": record.meta.id,
        "title": issue.title,
        "url": issue.url,
        "state": issue.state,
        "milestone": issue.milestone,
        "linked_by": how,
        "metadata": metadata_dict(record.meta),
        "acceptance_criteria": _spec_criteria(repo_root, record, warnings),
    }


def review_context(ctx: ReviewContext, pr_number: int, *, force: bool = False) -> Result:
    """Build the review context of pull request ``pr_number`` (ADR 0012).

    Exit 5 when the active agent cannot serve ``models.min_tier_for.reviewer`` and not
    ``force``. Without an agent (no profile) the tier is not checked and a warning says so.
    """
    warnings: list[str] = []
    tier: TierCheck | None = None
    if ctx.agent is None:
        warnings.append("no Forge profile: the reviewer tier (models.min_tier_for) is not checked")
    else:
        tier = check_reviewer_tier(ctx.config, ctx.agent, force=force, presets=ctx.presets)
        if tier.warning is not None:
            warnings.append(tier.warning)

    pr = ctx.github.get_pull(pr_number)
    linked = _load_linked(ctx.github, pr, warnings)
    task = None if linked is None else _task_data(linked[0], linked[1], ctx.repo_root, warnings)
    diff = pull_diff(
        ctx.git,
        pr_number,
        pr.base,
        warnings,
        remote=ctx.remote,
        fallback_branch=ctx.config.project.default_branch,
    )
    task_zones = None if linked is None else linked[0].meta.zones
    if ctx.zones is None:
        warnings.append("zone check skipped: .forge/zones.yml not loaded")
    zones = zone_check(diff.files, ctx.zones, task_zones, warnings)

    ci: dict[str, Any] | None
    try:
        status = ctx.github.commit_ci(pr.head_sha)
    except GitHubError as exc:
        ci = None
        warnings.append(f"pull request #{pr.number}: CI state not available ({exc.message})")
    else:
        ci = {
            "sha": pr.head_sha,
            "state": status.state,
            "total": status.total,
            "failing": list(status.failing),
            "pending": list(status.pending),
        }
    reviews = [
        {"id": r.id, "author": r.author, "state": r.state, "body": r.body}
        for r in ctx.github.list_reviews(pr_number)
    ]
    if diff.head != pr.head_sha:
        warnings.append(
            f"pull request #{pr.number}: fetched head {diff.head[:12]} differs from GitHub's "
            f"head {pr.head_sha[:12]}"
        )

    result = Result(
        data={
            "pr": _pull_data(pr),
            "task": task,
            "range": {"ref": diff.ref, "base": diff.base, "head": diff.head},
            "files": list(diff.files),
            "diff": diff.diff,
            "diff_bytes": diff.diff_bytes,
            "diff_truncated": diff.diff_truncated,
            "zones": zones,
            "ci": ci,
            "reviews": reviews,
            "reviewer": None if tier is None else tier.to_data(),
            "warnings": list(warnings),
        }
    )
    for text in warnings:
        result.warn(text)
    return result


# --------------------------------------------------------------------------- submit


def review_footer(tier: TierCheck) -> str:
    """The footer appended to a submitted review (ADR 0012)."""
    agent = tier.agent
    providers = ", ".join(agent.providers) or "no providers"
    model = tier.resolution.model or "unknown"
    line = f"{FOOTER_PREFIX} — agent {agent.id} ({providers}), model {model}"
    if tier.forced:
        line += (
            f" — tier forced: models.min_tier_for.{REVIEWER_ROLE} requires {tier.required}, "
            "which this agent cannot serve"
        )
    return line


def submit_review(
    ctx: ReviewContext,
    pr_number: int,
    action: str,
    body: str,
    *,
    login: str,
    force: bool = False,
) -> Result:
    """Submit a review of ``pr_number`` (``action``: approve, request-changes or comment).

    ``login`` is the reviewer's GitHub login from the profile. It must match the login gh is
    authenticated as (:func:`~forge.core.lease.check_identity`, exit 5 otherwise, before
    anything is submitted); the own-PR guard then uses the authenticated login. Approving or
    requesting changes on one's own pull request exits 5; commenting is allowed. A submit
    needs an agent (a profile): without one it exits 5.
    """
    event = SUBMIT_EVENTS.get(action)
    if event is None:
        raise ForgeError(
            f"unknown --submit value {action!r}",
            hint="use one of: " + ", ".join(SUBMIT_EVENTS),
        )
    if ctx.agent is None:
        raise PreconditionError(
            "submitting a review needs a Forge profile (the footer names the agent)",
            hint="create ~/.config/forge/profile.yml (forge setup arrives later)",
        )
    tier = check_reviewer_tier(ctx.config, ctx.agent, force=force, presets=ctx.presets)
    reviewer = check_identity(ctx.github, login)
    pr = ctx.github.get_pull(pr_number)
    if event != "COMMENT" and pr.author.casefold() == reviewer.casefold():
        raise PreconditionError(
            f"pull request #{pr_number} is yours ({pr.author}): you cannot {action} it",
            hint="ask another team member to review it, or use --submit comment",
        )
    footer = review_footer(tier)
    full_body = (body.rstrip() + "\n\n" if body.strip() else "") + footer + "\n"
    try:
        review = ctx.github.submit_review(pr_number, event, full_body)
    except GitHubError as exc:
        raise ForgeError(
            f"pull request #{pr_number}: review not submitted: {exc.message}",
            hint=exc.hint,
            code=ExitCode.ERROR,
        ) from exc
    result = Result(
        data={
            "pr": {"number": pr.number, "url": pr.url, "author": pr.author},
            "reviewer_login": reviewer,
            "action": action,
            "event": event,
            "review": {"id": review.id, "state": review.state, "author": review.author},
            "footer": footer,
            "forced": tier.forced,
            "reviewer": tier.to_data(),
        }
    )
    if tier.warning is not None:
        result.warn(tier.warning)
    return result
