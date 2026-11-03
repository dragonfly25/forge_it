"""Scenario helpers for the `forge plan apply` tests (not a test module).

``PlanWorld`` couples the bare-remote ``GitWorld`` (one clone for the lead who applies, one
for a developer who claims) with a ``FakeGitHub`` that starts empty: no labels, no
milestones. ``RecordingGitHub`` wraps a FakeGitHub to count writes, refuse them, or crash a
run in the middle of its writes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from forge.core.config import ForgeConfig
from forge.core.git import Git
from forge.core.github import GitHubError
from forge.core.github_fake import FakeGitHub
from forge.core.output import Result
from forge.core.plan import PlanFile, PlanTask, PlanValidation, Roadmap, RoadmapMilestone
from forge.core.plan_apply import plan_apply
from forge.core.tasks import (
    TRAILER_AGENT,
    TRAILER_CLAIM,
    TRAILER_HANDOFF,
    TRAILER_RELEASE,
    TRAILER_TASK,
    Size,
    TaskMetadata,
    TaskType,
)
from forge.core.tiers import Tier
from support.scenarios import make_config

if TYPE_CHECKING:
    from conftest import GitWorld

WRITE_PREFIXES = (
    "create_",
    "update_",
    "delete_",
    "add_",
    "remove_",
    "set_",
    "request_",
    "submit_",
    "close_",
)


class Crash(Exception):
    """Simulates the process dying in the middle of a run (not a GitHubError)."""


class RecordingGitHub:
    """Forwards to a FakeGitHub and records every call.

    - ``read_only``: any write fails the test;
    - ``crash_before`` / ``crash_after``: raise :class:`Crash` before (the write does not
      happen) or after (the write happened, its response was lost) the n-th write (1-based);
    - ``before`` / ``after``: hooks run once around the first call of a method;
    - ``fail``: method name -> number of calls that raise ``GitHubError`` (status 502);
    - ``on_issue``: issue number -> hook run once before the first ``get_issue`` of it.
    """

    def __init__(
        self,
        fake: FakeGitHub,
        *,
        read_only: bool = False,
        crash_before: int | None = None,
        crash_after: int | None = None,
    ) -> None:
        self._fake = fake
        self.read_only = read_only
        self.crash_before = crash_before
        self.crash_after = crash_after
        self.calls: list[str] = []
        self.writes: list[str] = []
        self.before: dict[str, Callable[[], None]] = {}
        self.after: dict[str, Callable[[], None]] = {}
        self.fail: dict[str, int] = {}
        self.on_issue: dict[int, Callable[[], None]] = {}

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._fake, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            if self.fail.get(name, 0) > 0:
                self.fail[name] -= 1
                raise GitHubError(f"injected failure in {name}", status=502)
            if name == "get_issue" and args and args[0] in self.on_issue:
                self.on_issue.pop(args[0])()
            is_write = name.startswith(WRITE_PREFIXES) or name.startswith("fake_")
            if is_write:
                if self.read_only:
                    pytest.fail(f"plan apply called the GitHub write method {name}")
                if self.crash_before is not None and len(self.writes) + 1 == self.crash_before:
                    raise Crash(f"crash before write {self.crash_before} ({name})")
                self.writes.append(name)
            hook = self.before.pop(name, None)
            if hook is not None:
                hook()
            result = attr(*args, **kwargs)
            hook = self.after.pop(name, None)
            if hook is not None:
                hook()
            if is_write and self.crash_after is not None and len(self.writes) == self.crash_after:
                raise Crash(f"crash after write {self.crash_after} ({name})")
            return result

        return call


@dataclass
class ApplyCtx:
    """What ``plan_apply`` needs (``RefreshContext``)."""

    config: ForgeConfig
    git: Git
    github: Any


def meta(
    task_id: str,
    *,
    zones: Sequence[str] = ("api",),
    deps: Sequence[str] = (),
    done_when: Sequence[str] = ("tests pass",),
    size: Size = Size.S,
    tier: Tier = Tier.STANDARD,
    type: TaskType = TaskType.IMPL,
    covers: Sequence[str] = ("AC-1",),
    spec: str = "specs/m1/core.md",
) -> TaskMetadata:
    return TaskMetadata(
        id=task_id,
        spec=spec,
        covers=tuple(covers),
        depends_on=tuple(deps),
        zones=tuple(zones),
        size=size,
        tier=tier,
        type=type,
        done_when=tuple(done_when),
    )


def task(
    task_id: str,
    title: str | None = None,
    description: str = "Do the work.\n",
    **kwargs: Any,
) -> PlanTask:
    return PlanTask(meta(task_id, **kwargs), title or f"Task {task_id}", description)


def plan_of(*tasks: PlanTask, milestone: str = "M1") -> PlanFile:
    return PlanFile(milestone, tuple(tasks), Path(f"plans/{milestone}.yml"))


def validation_of(plan: PlanFile, *, roadmap: Roadmap | None = None) -> PlanValidation:
    return PlanValidation(
        milestone=plan.milestone,
        plan=plan,
        errors=(),
        warnings=(),
        waves=(),
        coverage={},
        roadmap=roadmap,
        file=plan.path.as_posix(),
    )


def roadmap_of(milestone: str, title: str, goal: str | None = None) -> Roadmap:
    return Roadmap((RoadmapMilestone(milestone, title, goal),), Path("plans/roadmap.yml"))


@dataclass
class PlanWorld:
    """A bare remote with a lead clone (who applies) and a dev clone (who claims)."""

    git_world: GitWorld
    fake: FakeGitHub = field(default_factory=FakeGitHub)

    def __post_init__(self) -> None:
        self.lead = self.git_world.clone("lead")
        self.dev = self.git_world.clone("bob")
        self.config = make_config()

    def ctx(self, github: Any = None) -> ApplyCtx:
        return ApplyCtx(self.config, self.lead, github if github is not None else self.fake)

    def apply(
        self,
        plan: PlanFile,
        *,
        github: Any = None,
        dry_run: bool = False,
        roadmap: Roadmap | None = None,
    ) -> Result:
        return plan_apply(self.ctx(github), validation_of(plan, roadmap=roadmap), dry_run=dry_run)

    def push_event(self, issue: int, task_id: str, event: str = "claim", who: str = "bob") -> str:
        """Push an empty Forge event commit on ``task/<issue>`` from the dev clone."""
        trailer = {"claim": TRAILER_CLAIM, "handoff": TRAILER_HANDOFF, "release": TRAILER_RELEASE}
        trailers = {TRAILER_TASK: task_id, trailer[event]: who}
        if event == "claim":
            trailers[TRAILER_AGENT] = "claude"
        git = self.dev
        git.fetch()
        ref = f"refs/heads/task/{issue}"
        parent = self.git_world.remote_git.rev_parse(ref) or "origin/main"
        sha = git.commit_tree(git.tree_of(parent), [parent], f"forge: {event}", trailers)
        assert git.push(sha, ref).ok
        return sha

    def open_pr(self, issue: int) -> int:
        return self.fake.create_pull("work", "", f"task/{issue}", "main").number

    def snapshot(self) -> dict[str, Any]:
        """Issues and comments, normalised (no timestamps)."""
        issues = {
            i.number: (
                i.title,
                i.body,
                i.state,
                i.state_reason,
                tuple(sorted(i.labels)),
                i.milestone,
            )
            for i in self.fake.list_issues(state="all")
        }
        comments = {n: tuple(c.body for c in self.fake.list_comments(n)) for n in sorted(issues)}
        milestones = tuple((m.title, m.description) for m in self.fake.list_milestones())
        labels = tuple(lb.name for lb in self.fake.list_labels())
        return {
            "issues": issues,
            "comments": comments,
            "milestones": milestones,
            "labels": labels,
        }
