"""Scenario helpers for the `forge review` and `forge propose` tests (ADR 0012).

``ReviewWorld`` wraps a :class:`conftest.GitWorld` (bare remote plus clones) and a
``FakeGitHub``. Pull requests are opened by pushing a branch from the author's clone, creating
the PR in the fake and then creating ``refs/pull/<n>/head`` on the bare remote, as GitHub does.

Imported as ``support.review_world`` (``pythonpath = ["tests"]``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from forge.core.config import ForgeConfig, parse_config
from forge.core.git import Git
from forge.core.github import PullRequest
from forge.core.github_fake import FakeGitHub
from forge.core.refresh import ensure_labels
from forge.core.tasks import Size, TaskMetadata, TaskType, upsert_block
from forge.core.tiers import Tier
from forge.core.validate import Fields
from forge.core.zones import Zone, ZonesConfig

if TYPE_CHECKING:
    from conftest import GitWorld

SPEC_PATH = "specs/m1/x.md"
SPEC_TEXT = """\
# Feature

## Requirements
- AC-1: Users can log in.
- AC-2: Users can log out.
  Sessions are cleared.
- AC-3: Not covered.
"""

ZONES = ZonesConfig(
    zones={
        "api": Zone("api", ("src/api/**",)),
        "web": Zone("web", ("src/web/**",)),
    }
)


def review_config(min_tier_for: Mapping[str, str] | None = None) -> ForgeConfig:
    """A project config with an optional ``models.min_tier_for``."""
    data: dict[str, Any] = {
        "forge_version": ">=0.2,<0.3",
        "project": {"name": "shop", "default_branch": "main"},
        "github": {"repo": "acme/shop"},
    }
    if min_tier_for is not None:
        data["models"] = {"min_tier_for": dict(min_tier_for)}
    return parse_config(Fields(data, "config.yml"))


class ReviewWorld:
    """Alice authors pull requests, Bob reviews them from his clone."""

    def __init__(self, git_world: GitWorld) -> None:
        self.gw = git_world
        self.fake = FakeGitHub(user="alice")
        ensure_labels(self.fake)
        self.alice = git_world.clone("alice")
        self.bob = git_world.clone("bob")
        (self.bob.cwd / "specs" / "m1").mkdir(parents=True)
        (self.bob.cwd / SPEC_PATH).write_text(SPEC_TEXT)

    def task(
        self,
        task_id: str = "M1-T01",
        zones: Sequence[str] = ("api",),
        covers: Sequence[str] = ("AC-1", "AC-2"),
        milestone: str | None = "M1",
    ) -> int:
        """Create a Forge task issue and return its number."""
        meta = TaskMetadata(
            id=task_id,
            spec=SPEC_PATH,
            covers=tuple(covers),
            depends_on=(),
            zones=tuple(zones),
            size=Size.S,
            tier=Tier.STANDARD,
            type=TaskType.IMPL,
            done_when=("tests pass",),
        )
        if milestone is not None and not any(
            m.title == milestone for m in self.fake.list_milestones()
        ):
            self.fake.create_milestone(milestone)
        issue = self.fake.create_issue(
            f"[{task_id}] task",
            upsert_block("Description.\n", meta),
            labels=["forge:task", "status:in-progress"],
            milestone=milestone,
        )
        return issue.number

    def open_pr(
        self,
        branch: str,
        files: Mapping[str, str],
        *,
        body: str = "",
        author: str = "alice",
        title: str = "feat: work",
    ) -> PullRequest:
        """Commit ``files`` on ``branch`` (from ``main``) in the author's clone, push, open a PR."""
        git = self.alice if author == "alice" else self.bob
        git.fetch()
        git.run("checkout", "--quiet", "-B", branch, "origin/main")
        for path, content in files.items():
            target = git.cwd / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git.add(list(files))
        sha = git.commit(f"{title}\n")
        self.gw.push(git, branch)
        git.run("checkout", "--quiet", "main")
        self.fake.fake_set_user(author)
        pr = self.fake.create_pull(title, body, branch, "main")
        self.fake.fake_set_user("bob")
        pr = self.fake.fake_set_head_sha(pr.number, sha)
        self.gw.remote_git.update_ref(f"refs/pull/{pr.number}/head", sha)
        return pr

    def main_commit(self, path: str, content: str) -> str:
        """Advance ``main`` on the remote with one commit from the seed clone."""
        seed = Git(self.gw.base / "seed")
        seed.run("pull", "--quiet", "--ff-only")
        sha = self.gw.commit_file(seed, path, content, f"chore: {path}")
        self.gw.push(seed, "main")
        return sha
