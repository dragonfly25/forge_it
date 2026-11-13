"""Tests for forge.core.report: the data model behind ``forge status`` (ADR 0010).

Git runs against real temporary repositories (a bare remote plus one clone per simulated
user); GitHub is a ``FakeGitHub``. The claim plumbing (``World``: clones, event commits,
work commits) is reused from ``test_claim``. The seeded scenario is compared with the
committed snapshots in ``tests/snapshots/`` after volatile values (shas, temporary paths)
are replaced by placeholders; ``FORGE_UPDATE_SNAPSHOTS=1`` rewrites them.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from forge.core.claim import NextOptions, claim_next, read_task_state
from forge.core.config import ForgeConfig, parse_config
from forge.core.delegation import IMPLEMENTER_PATH, rendered_path
from forge.core.errors import PreconditionError
from forge.core.git import Git
from forge.core.github import CiStatus
from forge.core.github_fake import FakeGitHub
from forge.core.profile import AgentProfile
from forge.core.report import (
    QUEUE_SIZE,
    SECTIONS,
    StatusOptions,
    StatusReport,
    build_status,
)
from forge.core.tiers import Tier
from forge.core.validate import Fields
from forge.core.workspace import CurrentTask, write_current
from support.paths import replace_paths
from support.scenarios import (
    AGENT,
    T0,
    NoChecksGitHub,
    Seeder,
    Ticker,
    World,
    make_config,
    profile_for,
    seed_full,
)

if TYPE_CHECKING:
    from conftest import GitWorld

SNAPSHOTS = Path(__file__).resolve().parent.parent / "snapshots"
UPDATE = os.environ.get("FORGE_UPDATE_SNAPSHOTS") == "1"


def config_with(**extra: Any) -> ForgeConfig:
    data: dict[str, Any] = {
        "forge_version": ">=0.1,<0.2",
        "project": {"name": "shop", "default_branch": "main"},
        "github": {"repo": "acme/shop"},
        **extra,
    }
    return parse_config(Fields(data, "config.yml"))


@pytest.fixture
def seeder(git_world: GitWorld) -> Seeder:
    return Seeder(World(git_world, fake=FakeGitHub(clock=Ticker())))


# --------------------------------------------------------------------------- snapshots

_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")


def normalize(data: dict[str, Any], tmp: Path) -> str:
    """JSON text with shas → ``<sha:N>`` (by first appearance) and temp paths → ``<tmp>``."""
    text = json.dumps(replace_paths(data, [tmp], "<tmp>"), indent=2) + "\n"
    seen: dict[str, str] = {}

    def repl(match: re.Match[str]) -> str:
        return seen.setdefault(match.group(0), f"<sha:{len(seen) + 1}>")

    return _SHA_RE.sub(repl, text)


def check_snapshot(name: str, report: StatusReport, tmp: Path) -> bool:
    """Compare ``report`` with ``tests/snapshots/<name>``; return True when it was rewritten.

    ``FORGE_UPDATE_SNAPSHOTS=1`` rewrites the file instead of comparing, and is refused in CI
    (``CI`` set) so a run there can never accept a changed snapshot.
    """
    text = normalize(report.to_data(), tmp)
    path = SNAPSHOTS / name
    if UPDATE:
        if os.environ.get("CI"):
            pytest.fail(
                "FORGE_UPDATE_SNAPSHOTS=1 is not allowed in CI; update the snapshots locally, "
                "review the diff and commit them"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return True
    assert path.is_file(), f"missing snapshot {path}; run with FORGE_UPDATE_SNAPSHOTS=1"
    assert text == path.read_text(), (
        f"{name} differs; review and rerun with FORGE_UPDATE_SNAPSHOTS=1"
    )
    return False


def test_status_snapshots(seeder: Seeder, git_world: GitWorld) -> None:
    seed_full(seeder)
    ctx = seeder.ctx("alice")
    updated = [
        check_snapshot("status_full.json", build_status(ctx), git_world.base),
        check_snapshot(
            "status_me.json", build_status(ctx, StatusOptions(me_only=True)), git_world.base
        ),
        check_snapshot(
            "status_milestone_m2.json",
            build_status(ctx, StatusOptions(milestone="M2")),
            git_world.base,
        ),
    ]
    if any(updated):
        pytest.skip("snapshot updated; review the diff of tests/snapshots/")


def test_snapshot_update_is_refused_in_ci(
    seeder: Seeder, git_world: GitWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(globals(), "UPDATE", True)
    monkeypatch.setenv("CI", "true")
    seeder.task("M1-T01", ["api"])
    with pytest.raises(pytest.fail.Exception, match="not allowed in CI"):
        check_snapshot("never_written.json", build_status(seeder.ctx("alice")), git_world.base)
    assert not (SNAPSHOTS / "never_written.json").exists()


# --------------------------------------------------------------------------- full scenario


def ids(items: Sequence[dict[str, Any]]) -> list[str]:
    return [item["id"] for item in items]


def test_full_scenario_sections(seeder: Seeder) -> None:
    n = seed_full(seeder)
    alice = seeder.world.git("alice")
    alice.pushes.clear()
    data = build_status(seeder.ctx("alice")).to_data()
    assert data["sections"] == list(SECTIONS)
    assert data["generated_at"] == "2026-11-02T09:00:00Z"

    me = data["me"]
    assert (me["login"], me["login_source"], me["agent"]) == ("alice", "profile", "claude")
    cur = me["current"]
    assert (cur["issue"], cur["status"], cur["source"], cur["branch"]) == (
        n["mine"],
        "in-progress",
        "branch",
        f"task/{n['mine']}",
    )
    # exact last activity: the lease heartbeat, not the (older) claim commit
    assert cur["claim"]["last_activity"] == "2026-11-02T08:30:00Z"
    assert cur["claim"]["stale_at"] == "2026-11-03T08:30:00Z"
    assert cur["claim"]["stale"] is False
    assert cur["sync"] == {"ref": f"task/{n['mine']}", "behind": 3, "unpushed": 1, "pending": True}
    drift = cur["drift"]
    assert drift["total"] == 2
    assert [c["subject"] for c in drift["commits"]] == ["feat: v2 users", "feat: api routes"]
    assert drift["commits"][0]["zones"] == ["api"]
    assert drift["commits"][0]["files"] == ["src/api/v2/users.py"]
    assert cur["pull"] is None and cur["ci"] is None
    assert [o["issue"] for o in me["others"]] == [n["review"]]
    other = me["others"][0]
    assert other["pull"]["number"] == n["pr"]
    assert other["ci"]["state"] == "failure"
    assert other["ci"]["failing"] == ["lint", "test"]

    team = data["team"]
    assert ids(team["in_progress"]) == ["M1-T03", "M1-T04"]
    assert team["in_progress"][1]["claim"]["stale"] is True
    assert team["in_progress"][1]["claim"]["owner"] == "bob"
    assert ids(team["in_review"]) == ["M1-T06"]
    assert team["in_review"][0]["pull"]["author"] == "alice"
    handoff = team["handoffs"][0]
    assert (handoff["id"], handoff["by"], handoff["note"]["author"]) == ("M1-T07", "carol", "carol")

    queue = data["queue"]
    assert queue["ready"] == 5  # M1-T09..T11, M2-T01, M2-T02; M1-T08 has blocked:conflict
    assert ids(queue["next"]) == ["M1-T07", "M1-T09", "M1-T10"]

    blocked = data["blocked"]
    assert ids(blocked["by_dependencies"]) == ["M1-T05"]
    assert blocked["by_dependencies"][0]["waiting_on"] == [
        {"id": "M1-T04", "issue": n["stale"], "status": "in-progress"},
        {"id": "M2-T01", "issue": n["m2a"], "status": "ready"},
    ]
    assert ids(blocked["by_conflict"]) == ["M1-T08"]

    ms = data["milestone"]
    assert (ms["title"], ms["source"], ms["open"]) == ("M1", "current_task", True)
    assert (ms["done"], ms["total"]) == (1, 10)  # dropped and proposed tasks are not counted
    assert ids(ms["dropped"]) == ["M1-T02"]
    assert ms["proposed"] == ["M1-T12"]
    assert ms["statuses"] == {
        "ready": 4,
        "blocked": 1,
        "in-progress": 2,
        "handoff": 1,
        "in-review": 1,
        "done": 1,
    }
    waves = {w["wave"]: w for w in ms["waves"]}
    assert waves[1]["tasks"] == ["M1-T03", "M1-T05"]  # M1-T05 waits on M2-T01 (open)
    assert (waves[0]["done"], waves[0]["total"]) == (1, 8)
    assert all("M1-T12" not in w["tasks"] and "M1-T02" not in w["tasks"] for w in ms["waves"])
    acs = ms["acs"]
    assert acs["covered"] == ["AC-1"]
    assert acs["uncovered"] == ["AC-2", "AC-3", "AC-4", "AC-5", "AC-9"]
    assert acs["total"] == 6
    assert acs["missing_specs"] == ["specs/m1/missing.md"]

    health = data["health"]
    assert health["version"] == {"cli": "0.1.0", "required": "<0.2,>=0.1", "ok": True}
    assert health["profile"] is True
    assert health["presets"] == [
        {"agent": "claude", "tier": "standard", "current": "sonnet", "preset": "sonnet-5"}
    ]
    assert health["unknown_labels"] == [{"label": "status:wip", "issues": [n["ready1"]]}]
    assert [p["issue"] for p in health["without_metadata"]] == [n["nometa"]]
    assert health["without_metadata"][0]["error"] == "no Forge block"
    assert [(p["issue"], p["task"], p["forge_task"]) for p in health["proposals"]] == [
        (n["proposal"], "M1-T12", True)
    ]
    assert any("specs/m1/missing.md" in w for w in data["warnings"])
    # read-only: nothing pushed from my clone
    assert alice.pushes == []


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
    "reopen_",
    "merge_",
    "lock_",
    "upsert_",
)


class ReadOnlyGitHub:
    """Forwards reads to a FakeGitHub; records every call and fails on any write method."""

    def __init__(self, fake: FakeGitHub) -> None:
        self._fake = fake
        self.calls: list[str] = []
        self.writes: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._fake, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            if name.startswith(WRITE_PREFIXES) or name.startswith("fake_"):
                self.writes.append(name)
                pytest.fail(f"forge status called the GitHub write method {name}")
            return attr(*args, **kwargs)

        return call


def test_status_never_writes_to_github_or_pushes(seeder: Seeder, git_world: GitWorld) -> None:
    seed_full(seeder)
    alice = seeder.world.git("alice")
    alice.pushes.clear()
    remote = git_world.remote_git
    refs_before = remote.list_refs("refs/heads")
    github = ReadOnlyGitHub(seeder.fake)
    for opts in (StatusOptions(), StatusOptions(me_only=True), StatusOptions(milestone="M2")):
        build_status(seeder.ctx("alice", github=github), opts)
    assert github.writes == []
    assert {"list_issues", "list_pulls", "list_comments", "commit_ci"} <= set(github.calls)
    assert alice.pushes == []
    assert remote.list_refs("refs/heads") == refs_before


def test_queue_matches_forge_next_dry_run(seeder: Seeder) -> None:
    seed_full(seeder)
    queue = build_status(seeder.ctx("alice")).to_data()["queue"]
    assert queue["agent"] == AGENT.id
    picks = [(p["id"], p["issue"]) for p in queue["next"]]
    assert len(picks) == QUEUE_SIZE
    outcome = claim_next(seeder.world.ctx("alice"), NextOptions(dry_run=True))
    assert outcome.dry_run and outcome.claimed is None
    expected = [(c.view.record.meta.id, c.view.record.issue.number) for c in outcome.candidates]
    assert picks == expected[:QUEUE_SIZE]


def test_milestone_option_scopes_team_queue_blocked(seeder: Seeder) -> None:
    seed_full(seeder)
    data = build_status(seeder.ctx("alice"), StatusOptions(milestone="M2")).to_data()
    assert data["scope"] == {"me_only": False, "milestone": "M2"}
    assert data["team"] == {"in_progress": [], "in_review": [], "handoffs": []}
    assert data["blocked"] == {"by_dependencies": [], "by_conflict": []}
    assert data["queue"]["ready"] == 2
    assert ids(data["queue"]["next"]) == ["M2-T01", "M2-T02"]
    ms = data["milestone"]
    assert (ms["title"], ms["source"], ms["done"], ms["total"]) == ("M2", "option", 0, 2)
    # M2-T02 depends on M1-T01, closed in another milestone: satisfied, so wave 0
    assert [(w["wave"], w["tasks"]) for w in ms["waves"]] == [(0, ["M2-T01", "M2-T02"])]
    assert ms["acs"]["uncovered"] == ["AC-1", "AC-2"]
    # Me is never scoped
    assert data["me"]["current"]["id"] == "M1-T03"


def test_me_only_keeps_me_and_queue(seeder: Seeder) -> None:
    seed_full(seeder)
    data = build_status(seeder.ctx("alice"), StatusOptions(me_only=True)).to_data()
    assert data["sections"] == ["me", "queue"]
    for key in ("team", "blocked", "milestone", "health"):
        assert data[key] is None
    assert data["me"]["current"] is not None
    assert len(data["queue"]["next"]) == 3


# --------------------------------------------------------------------------- small worlds


def test_default_milestone_is_first_open_with_tasks(seeder: Seeder) -> None:
    seeder.milestone("M1")  # open, but no tasks
    seeder.task("M10-T01", ["db"])
    seeder.task("M2-T01", ["docs"])
    data = build_status(seeder.ctx("alice")).to_data()
    assert data["me"]["current"] is None
    assert (data["milestone"]["title"], data["milestone"]["source"]) == ("M2", "first_open")


def test_default_milestone_is_null_without_open_milestones(seeder: Seeder) -> None:
    number = seeder.task("M1-T01", ["db"])
    seeder.fake.close_milestone(1)
    data = build_status(seeder.ctx("alice")).to_data()
    assert data["sections"] == list(SECTIONS)
    assert data["milestone"] is None
    assert data["queue"]["ready"] == 0  # its milestone is closed: not active
    assert number == 1


def test_unknown_zone_is_a_warning_and_ignored_for_drift(seeder: Seeder) -> None:
    number = seeder.task("M1-T01", ["api", "ghost"])
    seeder.claim("alice", number, "M1-T01", T0 - timedelta(hours=1))
    seeder.main_commit("src/api/a.py", "a\n", T0 - timedelta(minutes=30), "feat: a")
    seeder.main_commit("ghost/x.txt", "x\n", T0 - timedelta(minutes=20), "feat: x")
    seeder.checkout_task("alice", number)
    report = build_status(seeder.ctx("alice"), StatusOptions(me_only=True))
    current = report.to_data()["me"]["current"]
    assert current["sync"]["behind"] == 2 and current["sync"]["unpushed"] == 0
    assert [c["subject"] for c in current["drift"]["commits"]] == ["feat: a"]
    assert any("'ghost'" in w and "ignored for drift" in w for w in report.warnings)


def test_queue_without_zones_picks_nothing(seeder: Seeder) -> None:
    seeder.task("M1-T01", ["api"])
    report = build_status(seeder.ctx("alice", zones=None), StatusOptions())
    queue = report.to_data()["queue"]
    assert queue["ready"] == 1 and queue["next"] == []
    assert any("zones.yml" in w and "queue" in w for w in report.warnings)


def test_drift_lists_at_most_ten_commits_with_the_total(seeder: Seeder) -> None:
    number = seeder.task("M1-T01", ["api"])
    seeder.claim("alice", number, "M1-T01", T0 - timedelta(hours=20))
    for i in range(12):
        when = T0 - timedelta(hours=12 - i)
        seeder.main_commit(f"src/api/f{i}.py", f"{i}\n", when, f"feat: {i}")
    seeder.checkout_task("alice", number)
    drift = build_status(seeder.ctx("alice")).to_data()["me"]["current"]["drift"]
    assert drift["total"] == 12
    assert [c["subject"] for c in drift["commits"]] == [f"feat: {i}" for i in range(11, 1, -1)]


def test_current_task_without_local_branch_uses_the_remote_branch(seeder: Seeder) -> None:
    number = seeder.task("M1-T01", ["api"])
    claim = seeder.claim("alice", number, "M1-T01", T0 - timedelta(hours=1))
    seeder.main_commit("src/api/a.py", "a\n", T0 - timedelta(minutes=30), "feat: a")
    git = seeder.world.git("alice")
    git.fetch()
    git.run("checkout", "--quiet", "--detach", "origin/main")
    write_current(
        git.cwd,
        CurrentTask("M1-T01", number, f"task/{number}", claim, claim, "claude", T0, None),
    )
    current = build_status(seeder.ctx("alice")).to_data()["me"]["current"]
    assert current["source"] == "current"
    assert current["sync"] == {
        "ref": f"origin/task/{number}",
        "behind": 1,
        "unpushed": 0,
        "pending": True,
    }
    assert current["drift"]["total"] == 1


def test_current_task_pull_request_ci(seeder: Seeder) -> None:
    number = seeder.task("M1-T01", ["api"])
    seeder.claim("alice", number, "M1-T01", T0 - timedelta(hours=2))
    tip = seeder.world.push_work("alice", number, "src/api/a.py", T0 - timedelta(hours=1))
    pr = seeder.as_user("alice").create_pull("M1-T01", "x", f"task/{number}", "main", draft=True)
    seeder.fake.fake_set_head_sha(pr.number, tip)
    seeder.fake.fake_set_ci(tip, CiStatus("pending", 2, (), ("build",)))
    seeder.checkout_task("alice", number)
    data = build_status(seeder.ctx("alice")).to_data()
    current = data["me"]["current"]
    assert current["status"] == "in-review"
    assert current["pull"]["draft"] is True
    assert current["ci"] == {
        "sha": tip,
        "state": "pending",
        "total": 2,
        "failing": [],
        "pending": ["build"],
    }
    assert data["me"]["others"] == []
    assert data["team"]["in_review"][0]["pull"]["number"] == pr.number


def test_unreadable_ci_is_a_warning_and_null(git_world: GitWorld) -> None:
    seeder = Seeder(World(git_world, fake=NoChecksGitHub(clock=Ticker())))
    number = seeder.task("M1-T01", ["api"])
    seeder.claim("alice", number, "M1-T01", T0 - timedelta(hours=1))
    pr = seeder.as_user("alice").create_pull("M1-T01", "x", f"task/{number}", "main")
    seeder.checkout_task("alice", number)
    report = build_status(seeder.ctx("alice"))
    current = report.to_data()["me"]["current"]
    assert current["pull"]["number"] == pr.number
    assert current["ci"] is None
    assert any(f"pull request #{pr.number}" in w and "HTTP 403" in w for w in report.warnings)


def test_no_profile_degrades_with_warnings(seeder: Seeder) -> None:
    seeder.task("M1-T01", ["api"])
    seeder.task("M1-T02", ["db"])
    report = build_status(seeder.ctx("dave", agent=None))
    data = report.to_data()
    assert (data["me"]["login"], data["me"]["login_source"], data["me"]["agent"]) == (
        "dave",
        "gh",
        None,
    )
    assert data["queue"] == {"ready": 2, "agent": None, "next": []}
    health = data["health"]
    assert (health["profile"], health["violations"], health["presets"]) == (False, [], [])
    assert any("queue cannot pick" in w and "forge setup" in w for w in report.warnings)
    assert any("profile constraints" in w for w in report.warnings)


def test_profile_violations_are_reported(seeder: Seeder) -> None:
    seeder.task("M1-T01", ["api"])
    config = config_with(
        models={"allowed_providers": ["openai"], "min_tier_for": {"plan": "strong"}}
    )
    light = AgentProfile(id="local", tool="ollama", providers=("local",), tiers={Tier.LIGHT: "x"})
    ctx = seeder.ctx("alice", profile=profile_for("alice", light), agent=light, config=config)
    health = build_status(ctx).to_data()["health"]
    messages = [v["message"] for v in health["violations"]]
    assert any("'local' is not in models.allowed_providers" in m for m in messages)
    assert any("cannot serve 'plan'" in m for m in messages)
    assert all(v["agent"] == "local" for v in health["violations"])


def test_version_mismatch_is_a_warning(seeder: Seeder) -> None:
    seeder.task("M1-T01", ["api"])
    report = build_status(seeder.ctx("alice", cli_version="0.3.0"))
    data = report.to_data()
    assert data["health"]["version"] == {"cli": "0.3.0", "required": "<0.2,>=0.1", "ok": False}
    assert any("does not satisfy forge_version" in w for w in report.warnings)
    # also with --me, where Health is omitted
    me_only = build_status(seeder.ctx("alice", cli_version="0.3.0"), StatusOptions(me_only=True))
    assert any("does not satisfy forge_version" in w for w in me_only.warnings)


def test_proposals_include_issues_without_forge_task(seeder: Seeder) -> None:
    seeder.milestone("M1")
    loose = seeder.as_user("bob").create_issue("Idea", "text", labels=["status:proposed"])
    data = build_status(seeder.ctx("alice")).to_data()
    assert data["health"]["proposals"] == [
        {
            "issue": loose.number,
            "title": "Idea",
            "author": "bob",
            "url": loose.url,
            "task": None,
            "forge_task": False,
        }
    ]


def test_invalid_block_and_unknown_labels(seeder: Seeder) -> None:
    seeder.milestone("M1")
    for name in ("tier:ultra", "Blocked:Later", "area:web"):
        seeder.fake.create_label(name, "cccccc")
    bad = seeder.as_user("lead").create_issue(
        "Bad",
        "<!-- forge:begin -->\n```yaml\nid: [\n```\n<!-- forge:end -->\n",
        labels=["forge:task", "tier:ultra", "Blocked:Later", "area:web"],
        milestone="M1",
    )
    closed = seeder.as_user("lead").create_issue("Old", "x", labels=["forge:task", "tier:ultra"])
    seeder.fake.update_issue(closed.number, state="closed")
    health = build_status(seeder.ctx("alice")).to_data()["health"]
    assert health["unknown_labels"] == [
        {"label": "Blocked:Later", "issues": [bad.number]},
        {"label": "tier:ultra", "issues": [bad.number]},
    ]
    assert [p["issue"] for p in health["without_metadata"]] == [bad.number]
    assert health["without_metadata"][0]["error"].startswith("invalid YAML")


def test_waves_with_a_cycle_warn_and_are_empty(seeder: Seeder) -> None:
    seeder.task("M1-T01", ["api"], deps=["M1-T02"])
    seeder.task("M1-T02", ["db"], deps=["M1-T01"])
    report = build_status(seeder.ctx("alice"))
    assert report.milestone is not None and report.milestone.waves == ()
    assert any("milestone M1" in w and "waves not shown" in w for w in report.warnings)


def test_shallow_clone_is_a_precondition_error(seeder: Seeder, git_world: GitWorld) -> None:
    seeder.main_commit("a.txt", "a\n", T0, "feat: a")
    path = git_world.base / "shallow"
    Git(git_world.base).run(
        "clone", "--quiet", "--depth", "1", f"file://{git_world.remote}", str(path)
    )
    ctx = seeder.ctx("alice")
    ctx.git = Git(path)
    with pytest.raises(PreconditionError, match="shallow"):
        build_status(ctx)


def test_read_task_state_reads_every_lease_when_exact(seeder: Seeder) -> None:
    number = seeder.task("M1-T01", ["api"])
    claim = seeder.claim("bob", number, "M1-T01", T0 - timedelta(hours=2))
    seeder.lease("bob", number, "M1-T01", claim, T0 - timedelta(minutes=5))
    git = seeder.world.git("alice")
    exact = read_task_state(git, seeder.fake, make_config(), now=lambda: T0, warnings=[])
    lazy = read_task_state(git, seeder.fake, make_config(), now=lambda: T0, warnings=[], exact=())
    assert exact.snapshot.views[number].activity == T0 - timedelta(minutes=5)
    assert lazy.snapshot.views[number].activity == T0 - timedelta(hours=2)  # tip date only
    assert exact.github.pulls == () and len(exact.github.issues) == 1


# --------------------------------------------------------------------------- delegation


def test_me_current_task_shows_the_effective_delegation(seeder: Seeder) -> None:
    seed_full(seeder)
    worktree = Path(build_status(seeder.ctx("alice")).to_data()["me"]["current"]["worktree"])
    template = worktree / IMPLEMENTER_PATH
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(
        "---\nname: implementer\ndescription: d\ntier: standard\neffort: medium\n"
        "tools: [read, edit, shell]\n---\nbody\n",
        encoding="utf-8",
    )
    rendered = rendered_path("claude-code")
    assert rendered is not None
    (worktree / rendered).parent.mkdir(parents=True, exist_ok=True)
    (worktree / rendered).write_text("x\n", encoding="utf-8")

    profile = replace(profile_for("alice"), delegation="on")
    data = build_status(seeder.ctx("alice", profile=profile)).to_data()
    delegation = data["me"]["current"]["delegation"]
    assert (delegation["mode"], delegation["source"], delegation["delegate"]) == (
        "on",
        "profile",
        True,
    )
    assert delegation["model"] == "sonnet"


def test_me_current_task_delegation_without_profile(seeder: Seeder) -> None:
    seed_full(seeder)
    data = build_status(seeder.ctx("alice", agent=None)).to_data()
    delegation = data["me"]["current"]["delegation"]
    assert (delegation["mode"], delegation["reason"]) == ("off", "no profile")
