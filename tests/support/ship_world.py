"""Scenario helpers for the ship tests (not a test module).

A :class:`ShipWorld` is a :class:`support.sync_world.SyncWorld` (bare remote, one clone per
user, shared ``FakeGitHub``) with a spec on ``main``, a team, and :class:`ShipContext`
factories. Users claim through the real ``forge next`` core and ship through the real sync.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forge.core.config import ForgeConfig, parse_config
from forge.core.github import PullRequest
from forge.core.ship import ShipContext, ShipOptions, ShipOutcome, ship
from forge.core.sync import CommandRun
from forge.core.team import Member, Team
from forge.core.validate import Fields
from forge.core.zones import ZonesConfig
from support.sync_world import DEFAULT_RESOLVERS, SYNC_ZONES, Recorder, SyncWorld

if TYPE_CHECKING:
    from conftest import GitWorld

SPEC_PATH = "specs/m1/x.md"
SPEC = """\
# Drafts

## Requirements
- AC-1: The API saves drafts
  and returns their id.
- AC-2: Not covered by the task.
"""

TEAM = Team(
    members=(
        Member("alice", "alice", frozenset({"dev"}), ("anthropic",)),
        Member("bob", "bob", frozenset({"dev"}), ("openai",)),
        Member("carol", "carol", frozenset({"lead"}), ("anthropic", "openai")),
        Member("dave", "dave", frozenset({"dev"})),
    )
)


def ship_config(
    *,
    checks: Mapping[str, str] | None = None,
    required_approvals: int = 1,
    cross_provider: bool = True,
    resolvers: Sequence[Mapping[str, str]] = DEFAULT_RESOLVERS,
) -> ForgeConfig:
    data: dict[str, Any] = {
        "forge_version": ">=0.1,<0.2",
        "project": {"name": "shop", "default_branch": "main"},
        "github": {"repo": "acme/shop"},
        "lease_hours": 24,
        "resolvers": [dict(r) for r in resolvers],
        "review": {"cross_provider": cross_provider, "required_approvals": required_approvals},
    }
    if checks is not None:
        data["checks"] = dict(checks)
    return parse_config(Fields(data, "config.yml"))


class CheckRecorder(Recorder):
    """Records check commands; a command listed in ``fail`` exits 1 without running."""

    def __init__(self, fail: Sequence[str] = ()) -> None:
        super().__init__()
        self.fail = set(fail)

    def __call__(self, command: str, cwd: Path) -> CommandRun:
        if command in self.fail:
            self.calls.append((command, cwd))
            return CommandRun(1, f"{command}: failed\n")
        return super().__call__(command, cwd)


class ShipWorld(SyncWorld):
    """A sync world with a spec on ``main`` and ship contexts."""

    def __init__(self, git_world: GitWorld) -> None:
        super().__init__(git_world)
        self.upstream({SPEC_PATH: SPEC}, message="docs: spec", link=False)

    def ship_ctx(
        self,
        user: str = "alice",
        *,
        config: ForgeConfig | None = None,
        team: Team | None = TEAM,
        zones: ZonesConfig = SYNC_ZONES,
        checks: CheckRecorder | None = None,
    ) -> ShipContext:
        extra: dict[str, Any] = {} if checks is None else {"run_check": checks}
        sync_ctx = self.ctx(user, config=config or ship_config(), team=team, **extra)
        return ShipContext(sync=replace(sync_ctx, zones=zones))

    def ship(self, user: str = "alice", *, draft: bool = False, **kwargs: Any) -> ShipOutcome:
        return ship(self.ship_ctx(user, **kwargs), ShipOptions(draft=draft))

    def pulls(self, issue: int) -> list[PullRequest]:
        return self.fake.list_pulls(state="all", head=f"task/{issue}")

    def pull(self, issue: int) -> PullRequest:
        pulls = self.pulls(issue)
        assert len(pulls) == 1, pulls
        return self.fake.get_pull(pulls[0].number)
