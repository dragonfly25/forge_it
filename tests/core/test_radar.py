"""Tests for `forge refresh --radar` (core/radar.py): real temp repos + FakeGitHub."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from forge.core.config import ForgeConfig
from forge.core.git import Git
from forge.core.github import GitHubClient
from forge.core.github_fake import FakeGitHub
from forge.core.radar import (
    RADAR_MARKER,
    RadarFinding,
    StaleLease,
    code_span,
    is_radar_comment,
    run_radar,
    run_refresh_radar,
)
from forge.core.report import DriftCommit
from forge.core.zones import ZonesConfig
from support.plan_world import RecordingGitHub
from support.scenarios import T0, ZONES, Clock, World, make_config, run

if TYPE_CHECKING:
    from conftest import GitWorld


@dataclass
class Ctx:
    config: ForgeConfig
    git: Git
    github: GitHubClient
    zones: ZonesConfig | None
    clock: Callable[[], datetime] = field(default_factory=Clock)


class Radar:
    """A World with a claimed in-progress task and helpers to move main and the branch."""

    def __init__(self, git_world: GitWorld) -> None:
        self.world = World(git_world)
        self.fake: FakeGitHub = self.world.fake
        self.ci = self.world.git("ci")  # the clone the radar runs in (like a CI checkout)

    def task(self, task_id: str, zones: tuple[str, ...] = ("api",)) -> int:
        return self.world.task(task_id, zones)

    def claim(self, issue: int, user: str = "bob") -> None:
        outcome = run(self.world.ctx(user, clock=Clock(T0)), task=str(issue))
        assert outcome.claimed is not None, outcome

    def upstream(self, path: str, message: str = "feat: upstream") -> str:
        git = self.world.git("lead")
        git.fetch()
        git.run("checkout", "--quiet", "-B", "main", "origin/main")
        target = git.cwd / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{message}\n")
        git.add([path])
        sha = git.commit(message)
        assert git.push("refs/heads/main", "refs/heads/main").ok
        return sha

    def sync_task(self, issue: int, user: str = "bob") -> None:
        """Merge origin/main into task/<issue> and push it (the task is synced)."""
        git = self.world.git(user)
        git.fetch()
        git.run("checkout", "--quiet", "-B", f"task/{issue}", f"origin/task/{issue}")
        git.run("merge", "--quiet", "--no-edit", "origin/main")
        assert git.push(f"refs/heads/task/{issue}", f"refs/heads/task/{issue}").ok

    def ctx(
        self, *, hours: float = 1, github: Any = None, zones: ZonesConfig | None = ZONES
    ) -> Ctx:
        return Ctx(
            config=make_config(),
            git=self.ci,
            github=github if github is not None else self.fake,
            zones=zones,
            clock=Clock(T0 + timedelta(hours=hours)),
        )

    def radar_comments(self, issue: int) -> list[str]:
        return [c.body for c in self.fake.list_comments(issue) if is_radar_comment(c.body)]


@pytest.fixture
def radar(git_world: GitWorld) -> Radar:
    return Radar(git_world)


def entry(data: dict[str, Any], issue: int) -> dict[str, Any]:
    found = [t for t in data["tasks"] if t["issue"] == issue]
    assert len(found) == 1, data
    return found[0]


# --------------------------------------------------------------------------- rendering


def test_code_span_neutralizes_backticks_and_mentions() -> None:
    assert code_span("bob") == "`bob`"
    assert code_span("a `b` c") == "``a `b` c``"
    assert code_span("`x`") == "`` `x` ``"
    assert code_span("line1\nline2") == "`line1 line2`"


def test_render_is_deterministic_and_never_mentions() -> None:
    commit = DriftCommit(
        sha="a" * 40,
        date=T0,
        author="Bob",
        subject="fix @alice's bug, see #12",
        zones=("api",),
        files=("src/api/x.py",),
    )
    lease = StaleLease("bob", "claude", T0, T0 + timedelta(hours=24))
    finding = RadarFinding(4, "M1-T04", "main", (commit,), lease)
    body = finding.body()
    assert body is not None and body == finding.body()
    assert body.startswith(RADAR_MARKER + "\n")
    assert "`fix @alice's bug, see #12`" in body
    assert "`bob`" in body and "forge reclaim 4" in body and "forge sync" in body
    assert RadarFinding(4, "M1-T04", "main").body() is None


# --------------------------------------------------------------------------- comments


def test_drift_comment_lists_commits_touching_zones(radar: Radar) -> None:
    issue = radar.task("M1-T01")
    radar.claim(issue)
    api = radar.upstream("src/api/a.py", "feat: api change")
    web = radar.upstream("src/web/a.js", "feat: web change")

    outcome = run_radar(radar.ctx())

    assert outcome.failures == []
    data = outcome.to_data()
    item = entry(data, issue)
    assert item["comment"] == "created" and item["in_progress"] is True
    assert [c["sha"] for c in item["drift"]["commits"]] == [api]
    assert item["lease"] is None
    [body] = radar.radar_comments(issue)
    assert api[:7] in body and web[:7] not in body
    assert "Sync recommended" in body and "forge sync" in body
    assert "Stale lease" not in body
    assert "@" not in body


def test_stale_lease_comment(radar: Radar) -> None:
    issue = radar.task("M1-T01")
    radar.claim(issue)

    outcome = run_radar(radar.ctx(hours=30))

    item = entry(outcome.to_data(), issue)
    assert item["comment"] == "created"
    assert item["drift"]["total"] == 0
    assert item["lease"]["owner"] == "bob"
    [body] = radar.radar_comments(issue)
    assert "Stale lease" in body and "`forge reclaim 1`" in body and "`bob`" in body
    assert "Sync recommended" not in body


def test_drift_and_stale_lease_in_one_comment(radar: Radar) -> None:
    issue = radar.task("M1-T01")
    radar.claim(issue)
    radar.upstream("src/api/a.py")

    run_radar(radar.ctx(hours=30))

    [body] = radar.radar_comments(issue)
    assert "Sync recommended" in body and "Stale lease" in body


def test_comment_updated_when_new_commits_land(radar: Radar) -> None:
    issue = radar.task("M1-T01")
    radar.claim(issue)
    first = radar.upstream("src/api/a.py", "feat: first")
    run_radar(radar.ctx())
    [before] = [c for c in radar.fake.list_comments(issue) if is_radar_comment(c.body)]

    second = radar.upstream("src/api/b.py", "feat: second")
    outcome = run_radar(radar.ctx())

    assert entry(outcome.to_data(), issue)["comment"] == "updated"
    [after] = [c for c in radar.fake.list_comments(issue) if is_radar_comment(c.body)]
    assert after.id == before.id
    assert first[:7] in after.body and second[:7] in after.body
    assert "2 commits" in after.body


def test_comment_deleted_when_task_synced_and_lease_fresh(radar: Radar) -> None:
    issue = radar.task("M1-T01")
    radar.claim(issue)
    radar.upstream("src/api/a.py")
    run_radar(radar.ctx())
    assert len(radar.radar_comments(issue)) == 1

    radar.sync_task(issue)
    outcome = run_radar(radar.ctx())

    item = entry(outcome.to_data(), issue)
    assert item["comment"] == "deleted" and item["in_progress"] is True
    assert radar.radar_comments(issue) == []
    # The lease comment is left alone.
    assert any("forge:lease" in c.body for c in radar.fake.list_comments(issue))


def test_second_run_makes_zero_writes(radar: Radar) -> None:
    issue = radar.task("M1-T01")
    radar.claim(issue)
    other = radar.task("M1-T02", ("web",))
    radar.claim(other, user="carol")
    radar.upstream("src/api/a.py")

    first = run_refresh_radar(radar.ctx(hours=30))
    assert first.ok, first.messages
    recorder = RecordingGitHub(radar.fake, read_only=True)
    second = run_refresh_radar(radar.ctx(hours=30, github=recorder))

    assert second.ok, second.messages
    assert recorder.writes == []
    counts = second.data["radar"]["counts"]
    assert counts["unchanged"] == 2
    assert counts["created"] == counts["updated"] == counts["deleted"] == 0


def test_not_in_progress_tasks_get_no_comment_and_stale_ones_are_deleted(radar: Radar) -> None:
    ready = radar.task("M1-T01")
    leftover = radar.task("M1-T02")
    radar.fake.create_comment(leftover, f"{RADAR_MARKER}\nold report\n")
    radar.upstream("src/api/a.py")

    outcome = run_radar(radar.ctx(hours=30))

    data = outcome.to_data()
    assert [t["issue"] for t in data["tasks"]] == [leftover]
    item = entry(data, leftover)
    assert item["comment"] == "deleted" and item["in_progress"] is False
    assert radar.radar_comments(ready) == []
    assert radar.radar_comments(leftover) == []
    assert data["counts"]["in_progress"] == 0


def test_duplicate_radar_comments_are_collapsed(radar: Radar) -> None:
    issue = radar.task("M1-T01")
    radar.claim(issue)
    radar.upstream("src/api/a.py")
    run_radar(radar.ctx())
    [body] = radar.radar_comments(issue)
    radar.fake.create_comment(issue, body)

    outcome = run_radar(radar.ctx())

    assert entry(outcome.to_data(), issue)["comment"] == "updated"
    assert radar.radar_comments(issue) == [body]


def test_without_zones_nothing_is_written(radar: Radar) -> None:
    issue = radar.task("M1-T01")
    radar.claim(issue)
    radar.upstream("src/api/a.py")
    recorder = RecordingGitHub(radar.fake, read_only=True)

    outcome = run_radar(radar.ctx(github=recorder, zones=None))

    assert outcome.entries == [] and recorder.writes == []
    assert any("radar skipped" in w for w in outcome.warnings)


def test_unknown_zone_is_a_warning(radar: Radar) -> None:
    issue = radar.task("M1-T01", ("api", "web"))
    radar.claim(issue)
    radar.upstream("src/api/a.py")
    radar.upstream("src/web/a.js")
    without_web = ZonesConfig(zones={k: v for k, v in ZONES.zones.items() if k != "web"})

    outcome = run_radar(radar.ctx(zones=without_web))

    assert any("'web'" in w and "ignored for drift" in w for w in outcome.warnings)
    assert entry(outcome.to_data(), issue)["drift"]["total"] == 1


def test_write_failure_is_reported_and_others_proceed(radar: Radar) -> None:
    first = radar.task("M1-T01")
    radar.claim(first)
    second = radar.task("M1-T02", ("web",))
    radar.claim(second, user="carol")
    recorder = RecordingGitHub(radar.fake)
    recorder.fail["create_comment"] = 1

    result = run_refresh_radar(radar.ctx(hours=30, github=recorder))

    assert int(result.code) == 1
    assert entry(result.data["radar"], first)["applied"] is False
    assert entry(result.data["radar"], second)["comment"] == "created"
    assert len(radar.radar_comments(second)) == 1
