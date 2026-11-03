"""Scenario helpers for the `forge release` tests (not a test module).

``ReleaseWorld`` couples the bare-remote ``GitWorld`` with a ``FakeGitHub`` holding a
milestone ``M1`` and its task issues. The lead runs ``forge release`` from the ``lead`` clone
(a ``HookedGit``, so tests can interleave other users' pushes); a ``maintainer`` clone plays
GitHub's merge button for the release pull request (merge commit or squash).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from forge.core.config import ForgeConfig
from forge.core.git import Git, Lease
from forge.core.github_fake import FakeGitHub
from forge.core.output import Result
from forge.core.plan import Roadmap, RoadmapMilestone
from forge.core.release import ReleaseOptions, run_release
from forge.core.tasks import FORGE_TASK_LABEL, TaskType, upsert_block
from forge.core.team import Team
from support.plan_world import meta
from support.scenarios import HookedGit, make_config

if TYPE_CHECKING:
    from conftest import GitWorld

NOW = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
TODAY = "2026-11-03"
ROADMAP = Roadmap(
    (RoadmapMilestone("M1", "Editor", "save a page", lead="lead"),),
    Path("plans/roadmap.yml"),
)


@dataclass
class Ctx:
    """What ``run_release`` needs (``ReleaseContext``)."""

    config: ForgeConfig
    git: Git
    github: Any


@dataclass
class ReleaseWorld:
    """A bare remote with a lead clone (who releases), plus a FakeGitHub with milestone M1."""

    git_world: GitWorld
    fake: FakeGitHub = field(default_factory=lambda: FakeGitHub(user="lead"))
    clock: Callable[[], datetime] = lambda: NOW

    def __post_init__(self) -> None:
        self.lead = self.clone("lead")
        self.config = make_config()
        self.fake.create_label(FORGE_TASK_LABEL, "ededed")
        self.milestone = self.fake.create_milestone("M1")

    def clone(self, user: str) -> HookedGit:
        self.git_world.clone(user)
        return HookedGit(self.git_world.base / user)

    def ctx(self, *, git: Git | None = None, github: Any = None) -> Ctx:
        return Ctx(self.config, git or self.lead, github if github is not None else self.fake)

    def run(
        self,
        version: str | None = "1.2.0",
        *,
        git: Git | None = None,
        github: Any = None,
        dry_run: bool = False,
        roadmap: Roadmap | None = ROADMAP,
        team: Team | None = None,
        milestone: str = "M1",
        clock: Callable[[], datetime] | None = None,
    ) -> Result:
        options = ReleaseOptions(
            milestone=milestone,
            version=version,
            dry_run=dry_run,
            roadmap=roadmap,
            team=team,
            clock=clock or self.clock,
        )
        return run_release(self.ctx(git=git, github=github), options)

    # ------------------------------------------------------------------ GitHub seeding

    def task(
        self,
        n: int,
        title: str,
        type: TaskType = TaskType.IMPL,
        *,
        closed: bool = True,
        merged: bool = True,
        milestone: str = "M1",
    ) -> tuple[int, int | None]:
        """Create task ``<milestone>-T<n>`` (issue) and, if ``merged``, its merged PR."""
        task_id = f"{milestone}-T{n:02d}"
        body = upsert_block("Do it.\n", meta(task_id, type=type))
        if milestone not in {m.title for m in self.fake.list_milestones()}:
            self.fake.create_milestone(milestone)
        issue = self.fake.create_issue(
            f"[{task_id}] {title}", body, labels=[FORGE_TASK_LABEL], milestone=milestone
        )
        pr_number: int | None = None
        if merged:
            pr_number = self.merged_pr(issue.number, f"[{task_id}] {title}")
        if closed:
            self.fake.update_issue(issue.number, state="closed")
        return issue.number, pr_number

    def merged_pr(self, issue: int, title: str = "work", base: str = "main") -> int:
        pr = self.fake.create_pull(title, f"Closes #{issue}", f"task/{issue}", base)
        self.fake.fake_merge_pull(pr.number)
        return pr.number

    # ------------------------------------------------------------------ git helpers

    def remote_sha(self, ref: str) -> str | None:
        return self.git_world.remote_git.rev_parse(ref)

    def remote_file(self, rev: str, path: str = "CHANGELOG.md") -> str | None:
        content = self.git_world.remote_git.read_file(rev, path)
        return None if content is None else content.decode()

    def main_commit(self, path: str, content: str, message: str = "feat: work") -> str:
        """Land a commit on the remote ``main`` (as a developer's merged work would)."""
        dev = self.git_world.base / "dev"
        git = Git(dev) if dev.exists() else self.git_world.clone("dev")
        git.fetch()
        git.reset_hard("origin/main")
        sha = self.git_world.commit_file(git, path, content, message)
        self.git_world.push(git, "main")
        return sha

    def maintainer(self) -> Git:
        path = self.git_world.base / "maintainer"
        git = Git(path) if path.exists() else self.git_world.clone("maintainer")
        git.fetch()
        git.reset_hard("origin/main")
        return git

    def merge_release(
        self,
        version: str = "1.2.0",
        method: Literal["merge", "squash", "rebase"] = "merge",
        *,
        mark_merged: bool = True,
    ) -> str:
        """Land ``release/v<version>`` on the remote ``main`` like GitHub's merge button.

        ``merge``: a merge commit; ``squash``: one squashed commit; ``rebase``: every branch
        commit replayed on ``main`` (main's first-parent chain gets them all). With
        ``mark_merged`` the open release PR is marked merged with ``merge_commit_sha`` set to
        the commit main was updated to (as GitHub reports); otherwise it is left open, as for
        a merge done by hand. Returns the new ``main`` tip.
        """
        git = self.maintainer()
        branch = f"origin/release/v{version}"
        if method == "merge":
            git.run("merge", "--no-ff", "--quiet", "-m", f"Merge release v{version}", branch)
        elif method == "squash":
            git.run("merge", "--squash", "--quiet", branch)
            git.commit(f"chore(release): v{version} (#99)")
        else:
            git.run("cherry-pick", "--allow-empty", f"origin/main..{branch}")
        self.git_world.push(git, "main")
        tip = git.rev_parse("HEAD") or ""
        if mark_merged:
            for pr in self.fake.list_pulls(state="open", head=f"release/v{version}"):
                self.fake.fake_merge_pull(pr.number, merge_commit_sha=tip)
        return tip

    def hand_edit_release_branch(self, version: str = "1.2.0", extra: str = "- By hand\n") -> str:
        """A human pushes a commit on top of ``release/v<version>`` appending to its section."""
        git = self.maintainer()
        git.run("switch", "--quiet", "-C", "hand", f"origin/release/v{version}")
        path = git.cwd / "CHANGELOG.md"
        path.write_text(path.read_text() + extra)
        git.add(["CHANGELOG.md"])
        sha = git.commit("docs: tweak release notes")
        assert git.push(sha, f"refs/heads/release/v{version}").ok
        git.run("switch", "--quiet", "main")
        return sha

    def amend_release_branch(
        self,
        version: str = "1.2.0",
        *,
        section_line: str | None = None,
        new_file: str | None = None,
    ) -> str:
        """A human amends Forge's release commit (``git commit --amend --no-edit`` keeps its
        trailers) and force-pushes it: appending ``section_line`` to the changelog and/or
        adding ``new_file``."""
        git = self.maintainer()
        git.run("switch", "--quiet", "-C", "hand", f"origin/release/v{version}")
        if section_line is not None:
            path = git.cwd / "CHANGELOG.md"
            path.write_text(path.read_text() + section_line)
            git.add(["CHANGELOG.md"])
        if new_file is not None:
            (git.cwd / new_file).write_text("extra\n")
            git.add([new_file])
        git.run("commit", "--quiet", "--amend", "--no-edit")
        sha = git.rev_parse("HEAD") or ""
        ref = f"refs/heads/release/v{version}"
        old = self.git_world.remote_git.rev_parse(ref)
        assert git.push(sha, ref, lease=Lease(ref, old)).ok
        git.run("switch", "--quiet", "main")
        return sha
