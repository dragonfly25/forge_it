"""``forge ship [--draft]``: sync, check, open or update the task's pull request (spec §11.1,
ADR 0012).

Flow of :func:`ship` (each step stops the run on failure, nothing later is touched):

1. **sync** (:func:`forge.core.sync.sync`): it requires the current task and my own live claim
   (exit 5 otherwise). Its exit codes 2, 4 and 5 are returned as they are; ``pushed`` and
   ``up_to_date`` continue;
2. **checks** (kickoff B7): skipped when the newest ``sync-log.json`` entry of the task has
   ``checks_passed: true`` for the current HEAD; otherwise ``config.checks`` run, and a failure
   is exit 2 with the same check report as sync;
3. **zones**: the files changed in ``merge-base(HEAD, origin/<default>)..HEAD``. Files outside
   the task's zones are listed in the PR (with their zone, or ``unzoned``) and warned about;
   with ``unzoned: error`` an unzoned file is exit 2 before the PR is touched;
4. **pull request** from ``task/<n>`` into the default branch: created with the Forge block
   followed by the repository's PR template (``--draft`` applies here only), or updated by
   rewriting only the Forge block (:mod:`forge.core.pr_body`); the title is the issue title;
5. **issue**: ``status:in-review`` through the label diff, then the lease heartbeat;
6. **reviewers** (:func:`forge.core.reviewers.select_reviewers`): topped up to
   ``review.required_approvals``, never removed.

Every step is idempotent, so re-running ship converges on one PR, one block and no duplicate
review requests. Core never prints: callers render :class:`ShipOutcome`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from forge.core.config import DelegationMode
from forge.core.context import extract_acceptance_criteria
from forge.core.delegation import delegation_for
from forge.core.errors import ExitCode, ForgeError
from forge.core.github import GitHubClient, GitHubError, PullRequest
from forge.core.lease import format_time, heartbeat, load_task
from forge.core.models import model_map, resolve
from forge.core.output import Level, Message, Result
from forge.core.pr_body import (
    AgentInfo,
    OutOfZoneFile,
    PrBlock,
    ac_text,
    initial_body,
    read_template,
    render_block,
    resolutions_from_log,
    update_body,
)
from forge.core.presets import Preset
from forge.core.profile import Profile
from forge.core.refresh import ensure_labels
from forge.core.reviewers import ReviewerSelection, select_reviewers
from forge.core.sync import (
    BACKUP_PREFIX,
    CheckRun,
    SyncContext,
    SyncOutcome,
    read_sync_log,
    run_checks,
    sync,
)
from forge.core.tasks import Status, TaskMetadata, desired_labels
from forge.core.workspace import find_current_task
from forge.core.zones import ZonesConfig


@dataclass
class ShipContext:
    """Everything ship needs: the sync context (config, git, GitHub, zones, agent, team,
    clock, check runner) plus the shipped presets used to resolve the effort value.

    ``profile`` is the machine profile, used for the delegation line of the PR body
    (ADR 0017); without it the decision is ``off`` (no profile).
    """

    sync: SyncContext
    presets: Mapping[str, Preset] | None = None
    profile: Profile | None = None


@dataclass(frozen=True)
class ShipOptions:
    """Options of ``forge ship``."""

    draft: bool = False


@dataclass
class ShipOutcome:
    """Result of :func:`ship`.

    ``outcome`` is ``created`` or ``updated`` (exit 0), ``sync_stopped`` (the sync's own code),
    ``checks_failed`` or ``unzoned`` (exit 2).
    """

    code: ExitCode
    outcome: str
    issue: int
    task_id: str
    branch: str
    head: str | None = None
    sync: SyncOutcome | None = None
    checks_skipped: bool = False
    checks: list[CheckRun] = field(default_factory=list)
    out_of_zone: list[OutOfZoneFile] = field(default_factory=list)
    pr: PullRequest | None = None
    body_changed: bool = False
    labels_added: list[str] = field(default_factory=list)
    labels_removed: list[str] = field(default_factory=list)
    heartbeat_at: str | None = None
    reviewers: ReviewerSelection | None = None
    requested: list[str] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)

    def info(self, text: str, hint: str | None = None) -> None:
        """Append an info message."""
        self.messages.append(Message(Level.INFO, text, hint))

    def warn(self, text: str, hint: str | None = None) -> None:
        """Append a warning."""
        self.messages.append(Message(Level.WARNING, text, hint))

    def error(self, text: str, hint: str | None = None) -> None:
        """Append an error message."""
        self.messages.append(Message(Level.ERROR, text, hint))

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` for ``forge ship``."""
        pr = self.pr
        return {
            "outcome": self.outcome,
            "issue": self.issue,
            "id": self.task_id,
            "branch": self.branch,
            "head": self.head,
            "sync": None if self.sync is None else self.sync.to_data(),
            "checks": {
                "skipped": self.checks_skipped,
                "runs": [c.to_data() for c in self.checks],
            },
            "out_of_zone": [f.to_data() for f in self.out_of_zone],
            "pr": None
            if pr is None
            else {
                "number": pr.number,
                "url": pr.url,
                "title": pr.title,
                "draft": pr.draft,
                "created": self.outcome == "created",
                "body_changed": self.body_changed,
            },
            "labels": {"added": self.labels_added, "removed": self.labels_removed},
            "heartbeat_at": self.heartbeat_at,
            "reviewers": None
            if self.reviewers is None
            else {**self.reviewers.to_data(), "requested": list(self.requested)},
        }

    def to_result(self) -> Result:
        """The command result: code, data and messages."""
        return Result(code=self.code, data=self.to_data(), messages=list(self.messages))


# --------------------------------------------------------------------------- steps


def _checks_cover(entries: list[dict[str, Any]], issue: int, head: str) -> bool:
    """True when the newest sync-log entry of ``task/<issue>`` passed the checks on ``head``."""
    prefix = f"{BACKUP_PREFIX}/task-{issue}/"
    mine = [e for e in entries if str(e.get("id", "")).startswith(prefix)]
    if not mine:
        return False
    newest = mine[-1]
    return newest.get("checks_passed") is True and newest.get("checked_head") == head


def changed_files(ctx: SyncContext, head: str) -> list[str]:
    """Files changed in ``merge-base(head, origin/<default>)..head``."""
    onto = f"refs/remotes/{ctx.remote}/{ctx.config.project.default_branch}"
    base = ctx.git.merge_base(head, onto)
    return sorted(ctx.git.diff_names(base, head))


def classify_files(
    files: list[str], zones: ZonesConfig, task_zones: tuple[str, ...]
) -> list[OutOfZoneFile]:
    """The files outside ``task_zones``, each with the zones it belongs to (none: unzoned)."""
    own = set(task_zones)
    found: list[OutOfZoneFile] = []
    for path in files:
        matched = zones.zones_for_path(path)
        if own.intersection(matched):
            continue
        found.append(OutOfZoneFile(path, tuple(matched)))
    return found


def _criteria(
    ctx: SyncContext, meta: TaskMetadata, out: ShipOutcome
) -> tuple[tuple[str, str | None], ...]:
    if not meta.covers:
        return ()
    spec = ctx.git.root() / meta.spec
    try:
        text = spec.read_text(encoding="utf-8")
    except FileNotFoundError:
        out.warn(f"spec {meta.spec} not found; acceptance criteria are listed without text")
        return tuple((ac, None) for ac in meta.covers)
    except (OSError, UnicodeDecodeError) as exc:
        out.warn(f"spec {meta.spec} could not be read ({exc}); criteria listed without text")
        return tuple((ac, None) for ac in meta.covers)
    found = extract_acceptance_criteria(text, meta.covers)
    for ac in meta.covers:
        if ac not in found:
            out.warn(f"{ac} is not defined in {meta.spec}")
    return tuple((ac, ac_text(ac, found[ac]) if ac in found else None) for ac in meta.covers)


def agent_info(ctx: ShipContext, meta: TaskMetadata) -> AgentInfo:
    """The author's agent, the model resolved for the task's tier (``tier: task``) and the
    delegation decision for the task (ADR 0017, decided in the current worktree)."""
    agent = ctx.sync.agent
    resolution = resolve(
        agent,
        declared_tier="task",
        task_tier=meta.tier,
        constraints=ctx.sync.config.models,
        presets=ctx.presets,
    )
    effective = resolution.effective_tier
    return AgentInfo(
        id=agent.id,
        tool=agent.tool,
        providers=tuple(agent.providers),
        tier=str(resolution.tier),
        effective_tier=None if effective is None else str(effective),
        model=resolution.model,
        effort=str(resolution.effort),
        effort_value=resolution.effort_value,
        tier_map=model_map(agent),
        notes=tuple(note.text for note in resolution.notes),
        delegation=delegation_for(
            meta,
            config=ctx.sync.config,
            profile=ctx.profile,
            agent=agent if ctx.profile is not None else None,
            worktree=ctx.sync.git.root(),
            override=_recorded_override(ctx, meta),
            presets=ctx.presets,
        ),
    )


def _recorded_override(ctx: ShipContext, meta: TaskMetadata) -> DelegationMode | None:
    """The ``--delegation`` override recorded in ``current.json`` for the task (ADR 0017)."""
    try:
        location = find_current_task(ctx.sync.git)
    except ForgeError:
        return None
    current = None if location is None else location.current
    if current is None or current.task != meta.id:
        return None
    return current.delegation_override


def _upsert_pull(
    github: GitHubClient,
    *,
    branch: str,
    base: str,
    title: str,
    block: str,
    template: str | None,
    draft: bool,
    out: ShipOutcome,
) -> PullRequest:
    existing = github.list_pulls(state="open", head=branch, base=base)
    if not existing:
        try:
            pr = github.create_pull(title, initial_body(block, template), branch, base, draft=draft)
        except GitHubError as exc:
            # GitHub refuses (422) a second pull request for the same head and base: another
            # ship opened it concurrently. Re-read it and update it instead.
            if exc.status != 422:
                raise
            existing = github.list_pulls(state="open", head=branch, base=base)
            if not existing:
                raise
            out.warn(
                f"pull request #{existing[0].number} for {branch} was opened concurrently; "
                "updating it"
            )
        else:
            out.outcome = "created"
            out.body_changed = True
            out.info(f"opened pull request #{pr.number}" + (" as a draft" if draft else ""))
            return pr
    pr = existing[0]
    out.outcome = "updated"
    if draft and not pr.draft:
        out.warn(
            f"--draft ignored: pull request #{pr.number} already exists (ship never "
            "changes its draft state)"
        )
    body = update_body(pr.body, block)
    if body != pr.body or title != pr.title:
        pr = github.update_pull(
            pr.number,
            title=title if title != pr.title else None,
            body=body if body != pr.body else None,
        )
        out.body_changed = body != existing[0].body
        out.info(f"updated pull request #{pr.number}")
    else:
        out.info(f"pull request #{pr.number} is already up to date")
    return pr


def _set_in_review(github: GitHubClient, issue: int, out: ShipOutcome) -> None:
    current = github.get_issue(issue)
    diff = desired_labels(current.labels, Status.IN_REVIEW, None)
    if diff.add:
        ensure_labels(github)
        github.add_labels(issue, sorted(diff.add))
    for name in sorted(diff.remove):
        github.remove_label(issue, name)
    out.labels_added = sorted(diff.add)
    out.labels_removed = sorted(diff.remove)


def _request_reviewers(ctx: SyncContext, pr: PullRequest, issue: int, out: ShipOutcome) -> None:
    github = ctx.github
    fresh = github.get_pull(pr.number)
    reviewed = [r.author for r in github.list_reviews(pr.number)]
    members = ctx.team.members if ctx.team is not None else ()
    selection = select_reviewers(
        members,
        me=ctx.profile_login,
        my_providers=ctx.agent.providers,
        issue=issue,
        required=ctx.config.review.required_approvals,
        requested=fresh.requested_reviewers,
        reviewed=reviewed,
        cross_provider=ctx.config.review.cross_provider,
    )
    out.reviewers = selection
    if ctx.team is None and selection.needed:
        out.warn("no usable .forge/team.yml: no review was requested")
    elif ctx.team is not None:
        for text in selection.warnings:
            out.warn(text)
    if selection.selected:
        try:
            github.request_reviewers(pr.number, selection.logins)
        except GitHubError as exc:
            # The pull request is done; a refused request (typically 422: a login that is not
            # a collaborator) must not fail the ship.
            out.warn(
                f"pull request #{pr.number}: reviews could not be requested from "
                + ", ".join(selection.logins)
                + f" ({exc.message})",
                hint="check they have access to the repository, or update .forge/team.yml; "
                "then re-run `forge ship` or request the reviews on GitHub",
            )
            return
        out.requested = selection.logins
        out.info("requested reviews from " + ", ".join(f"@{x}" for x in selection.logins))


# --------------------------------------------------------------------------- entry point


def ship(ctx: ShipContext, options: ShipOptions | None = None) -> ShipOutcome:
    """``forge ship``: sync, checks, zones, PR, ``status:in-review``, reviewers (ADR 0012).

    Returns exit 0 (PR created or updated), 2 (checks failed or unzoned files with
    ``unzoned: error``) or the sync's own code when it did not push (2, 4 or 5). Preconditions
    raise :class:`~forge.core.errors.PreconditionError` (exit 5) from the sync.
    """
    opts = options or ShipOptions()
    sctx = ctx.sync
    git = sctx.git
    root = git.root()

    # 1. sync
    synced = sync(sctx)
    out = ShipOutcome(
        code=ExitCode.OK,
        outcome="sync_stopped",
        issue=synced.issue,
        task_id=synced.task_id,
        branch=synced.branch,
        head=synced.head,
        sync=synced,
    )
    out.messages.extend(synced.messages)
    if synced.code is not ExitCode.OK:
        out.code = synced.code
        out.error(f"ship stopped: the sync ended with {synced.outcome}")
        return out
    head = git.rev_parse("HEAD")
    assert head is not None
    out.head = head
    issue = synced.issue

    # 2. checks (B7)
    if _checks_cover(read_sync_log(root), issue, head):
        out.checks_skipped = True
        out.info("checks skipped: the sync already passed them on this HEAD")
    else:
        out.checks = run_checks(sctx, root)
        failed = [c for c in out.checks if not c.passed]
        if failed:
            out.code = ExitCode.AGENT_ACTION
            out.outcome = "checks_failed"
            out.error(
                "failing checks: " + ", ".join(f"{c.name} (exit {c.exit_code})" for c in failed),
                hint="fix your code, commit, then run `forge ship` again",
            )
            return out

    # 3. zones
    record = load_task(sctx.github, issue)
    meta = record.meta
    out.out_of_zone = classify_files(changed_files(sctx, head), sctx.zones, meta.zones)
    unzoned = [f.path for f in out.out_of_zone if f.unzoned]
    outside = [f for f in out.out_of_zone if not f.unzoned]
    if unzoned and sctx.zones.unzoned == "error":
        out.code = ExitCode.AGENT_ACTION
        out.outcome = "unzoned"
        out.error(
            "files that match no zone (zones.yml has `unzoned: error`): " + ", ".join(unzoned),
            hint="move the changes into the task's zones, or ask the lead to add a zone to "
            ".forge/zones.yml; the pull request was not touched",
        )
        return out
    if outside:
        out.warn(
            "out-of-zone changes (listed in the PR): "
            + ", ".join(f"{f.path} ({', '.join(f.zones)})" for f in outside)
        )
    if unzoned:
        out.warn("unzoned changes (listed in the PR): " + ", ".join(unzoned))

    # 4. pull request
    block = render_block(
        PrBlock(
            issue=issue,
            task_id=meta.id,
            spec=meta.spec,
            criteria=_criteria(sctx, meta, out),
            done_when=meta.done_when,
            out_of_zone=tuple(out.out_of_zone),
            resolutions=tuple(resolutions_from_log(read_sync_log(root), issue)),
            agent=agent_info(ctx, meta),
        )
    )
    pr = _upsert_pull(
        sctx.github,
        branch=TaskMetadata.branch_name(issue),
        base=sctx.config.project.default_branch,
        title=record.issue.title,
        block=block,
        template=read_template(root),
        draft=opts.draft,
        out=out,
    )
    out.pr = pr

    # 5. issue label and heartbeat
    _set_in_review(sctx.github, issue, out)
    try:
        beat = heartbeat(sctx, issue)
    except ForgeError as exc:
        verb = "opened" if out.outcome == "created" else "updated"
        fix = f"{exc.hint.rstrip('.')}; then " if exc.hint else ""
        raise ForgeError(
            f"pull request #{pr.number} was {verb} ({pr.url}), but the lease heartbeat "
            f"failed: {exc.message}",
            hint=f"{fix}re-run `forge ship`: the pull request exists and is kept, and the "
            "re-run completes the remaining steps (heartbeat, reviewers)",
            code=exc.code,
        ) from exc
    out.heartbeat_at = format_time(beat.heartbeat_at)

    # 6. reviewers
    _request_reviewers(sctx, pr, issue, out)
    return out
