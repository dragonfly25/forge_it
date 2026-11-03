"""`forge propose --title T --body-file F`: propose new work as a ``status:proposed`` issue
(spec §12 ``/dev``, ADR 0012).

The issue has the given title, the body followed by a footer under
``<!-- forge:proposal -->``, the ``status:proposed`` label only (never ``forge:task``: the lead
adds the metadata when planning) and the current task's milestone. Proposing the same title
again while the proposal is open returns it with ``created: false`` and posts nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forge.core.errors import ForgeError
from forge.core.github import GitHubClient, Issue
from forge.core.lease import load_task
from forge.core.output import Result
from forge.core.profile import AgentProfile
from forge.core.refresh import ensure_labels
from forge.core.tasks import Status

PROPOSAL_MARKER = "<!-- forge:proposal -->"
"""Marks the footer Forge appends to a proposal's body."""

PROPOSED_LABEL = Status.PROPOSED.label


@dataclass(frozen=True)
class CurrentTaskRef:
    """The task the proposer is working on: id, issue number and GitHub milestone."""

    task_id: str
    issue: int
    milestone: str | None


@dataclass
class ProposeContext:
    """Inputs of ``forge propose``.

    ``login`` is the proposer's login (``None``: ask GitHub). ``agent`` is ``None`` without a
    profile. ``current_issue`` is the issue number of the current task, if any.
    """

    github: GitHubClient
    login: str | None = None
    agent: AgentProfile | None = None
    current_issue: int | None = None


def normalize_title(title: str) -> str:
    """Key used to detect a duplicate proposal: trimmed and case-folded."""
    return title.strip().casefold()


def proposal_footer(login: str, agent: AgentProfile | None, task: CurrentTaskRef | None) -> str:
    """The footer appended to a proposal's body, marker line included."""
    line = f"Proposed by @{login}"
    if agent is not None:
        line += f" (agent {agent.id})"
    if task is not None:
        line += f" while working on {task.task_id} (#{task.issue})"
    return f"{PROPOSAL_MARKER}\n{line}"


def proposal_body(body: str, footer: str) -> str:
    """The issue body: the user's text, a blank line, then the footer."""
    text = body.rstrip()
    return (text + "\n\n" if text else "") + footer + "\n"


def find_open_proposal(github: GitHubClient, title: str) -> Issue | None:
    """The oldest open ``status:proposed`` issue with the same title (trimmed, any case)."""
    key = normalize_title(title)
    for issue in github.list_issues(state="open", labels=[PROPOSED_LABEL]):
        if normalize_title(issue.title) == key:
            return issue
    return None


def _current_task(ctx: ProposeContext, warnings: list[str]) -> CurrentTaskRef | None:
    if ctx.current_issue is None:
        return None
    try:
        record = load_task(ctx.github, ctx.current_issue)
    except ForgeError as exc:
        warnings.append(
            f"current task #{ctx.current_issue} not used in the proposal: {exc.message}"
        )
        return None
    return CurrentTaskRef(record.meta.id, record.issue.number, record.issue.milestone)


def _issue_data(issue: Issue) -> dict[str, Any]:
    return {
        "number": issue.number,
        "title": issue.title,
        "url": issue.url,
        "labels": list(issue.labels),
        "milestone": issue.milestone,
        "author": issue.author,
    }


def propose(ctx: ProposeContext, title: str, body: str) -> Result:
    """Create the proposal issue, or return the open one with the same title (ADR 0012).

    An empty title is an error (exit 1). Catalogue labels missing from the repository are
    created before the issue.
    """
    clean_title = title.strip()
    if not clean_title:
        raise ForgeError("the proposal title is empty", hint='pass --title "<short summary>"')
    existing = find_open_proposal(ctx.github, clean_title)
    if existing is not None:
        result = Result(data={"created": False, "issue": _issue_data(existing), "task": None})
        result.info(
            f"an open proposal with this title already exists (#{existing.number}); "
            "nothing was posted"
        )
        return result

    warnings: list[str] = []
    task = _current_task(ctx, warnings)
    login = ctx.login if ctx.login else ctx.github.current_user()
    footer = proposal_footer(login, ctx.agent, task)
    ensure_labels(ctx.github)
    issue = ctx.github.create_issue(
        clean_title,
        proposal_body(body, footer),
        labels=[PROPOSED_LABEL],
        milestone=None if task is None else task.milestone,
    )
    result = Result(
        data={
            "created": True,
            "issue": _issue_data(issue),
            "task": None
            if task is None
            else {"id": task.task_id, "issue": task.issue, "milestone": task.milestone},
        }
    )
    for text in warnings:
        result.warn(text)
    return result
