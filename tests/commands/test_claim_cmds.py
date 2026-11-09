"""End-to-end tests for the Phase 3 commands through the CLI.

`forge next`, `forge task show`, `forge heartbeat`, `forge release-claim` and `forge reclaim`
run against a real bare remote and clones (``git_world``) and a shared ``FakeGitHub``. The
profile lives under a temporary ``XDG_CONFIG_HOME``; the clock and sleep are injected through
``forge.commands._claims`` so nothing waits for real.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from forge.cli import app
from forge.commands import _claims, _context
from forge.core import report
from forge.core.config import ForgeConfig
from forge.core.git import Git
from forge.core.github import GitHubClient
from forge.core.github_fake import FakeGitHub
from forge.core.lease import find_lease
from forge.core.refresh import ensure_labels
from forge.core.tasks import Size, TaskMetadata, TaskType, upsert_block
from forge.core.tiers import Tier

if TYPE_CHECKING:
    from conftest import GitWorld

runner = CliRunner()
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
ENVELOPE_KEYS = {"ok", "code", "data", "messages"}

CONFIG = """\
forge_version: ">=0.1,<0.2"
project:
  name: shop
  default_branch: main
github:
  repo: acme/shop
lease_hours: 24
"""

ZONES_YML = """\
zones:
  api:
    paths: ["src/api/**"]
  web:
    paths: ["src/web/**"]
"""

SPEC = """\
# Shop

## Requirements
- AC-1: the API answers.
"""


CODEX_AGENT = (
    "  - id: codex\n"
    "    tool: codex\n"
    "    providers: [openai]\n"
    "    tiers: {strong: gpt-strong, standard: gpt-standard, light: gpt-light}\n"
)


def profile_yaml(login: str, worktrees: Path, extra_agents: str = "") -> str:
    return (
        f"github: {login}\n"
        "agents:\n"
        "  - id: claude\n"
        "    tool: claude-code\n"
        "    providers: [anthropic]\n"
        "    tiers: {strong: opus, standard: sonnet, light: haiku}\n"
        f"{extra_agents}"
        "default_agent: claude\n"
        f"worktrees_dir: {worktrees}\n"
    )


def task_body(task_id: str, zones: tuple[str, ...]) -> str:
    meta = TaskMetadata(
        id=task_id,
        spec="specs/m1/x.md",
        covers=("AC-1",),
        depends_on=(),
        zones=zones,
        size=Size.S,
        tier=Tier.STANDARD,
        type=TaskType.IMPL,
        done_when=("tests pass",),
    )
    return upsert_block(f"Implement {task_id}.\n", meta)


class Clock:
    """A settable clock."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Env:
    """A remote with a Forge project, one clone per user, a fake GitHub and a profile."""

    git_world: GitWorld
    fake: FakeGitHub
    xdg: Path
    worktrees: Path
    clock: Clock
    sleeps: list[float] = field(default_factory=list)
    clones: dict[str, Git] = field(default_factory=dict)
    extra_agents: str = ""
    extra_top: str = ""

    def clone(self, user: str) -> Path:
        if user not in self.clones:
            self.clones[user] = self.git_world.clone(user)
        return self.clones[user].cwd

    def task(self, task_id: str, zones: tuple[str, ...] = ("api",)) -> int:
        issue = self.fake.create_issue(
            f"[{task_id}] task",
            task_body(task_id, zones),
            labels=["forge:task", "status:ready"],
        )
        return issue.number

    def worktree(self, n: int, user: str = "alice") -> Path:
        return self.worktrees / user / f"task-{n}"

    def profile(self, login: str) -> Path:
        path = self.xdg / "forge" / "profile.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            profile_yaml(login, self.worktrees / login, self.extra_agents) + self.extra_top
        )
        return path

    def invoke(
        self, repo: Path, *args: str, user: str = "alice", profile: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        """Run ``forge --json --repo <repo> <args>`` as ``user``; return (exit code, envelope)."""
        self.fake.fake_set_user(user)
        self.profile(profile or user)
        result = runner.invoke(app, ["--json", "--repo", str(repo), *args])
        envelope = json.loads(result.stdout)
        assert set(envelope) == ENVELOPE_KEYS, envelope
        assert envelope["ok"] is (envelope["code"] == 0)
        assert isinstance(envelope["data"], dict)
        for message in envelope["messages"]:
            assert set(message) == {"level", "text", "hint"}
        assert result.exit_code == envelope["code"], result.output
        return result.exit_code, envelope

    def human(self, repo: Path, *args: str, user: str = "alice") -> Any:
        self.fake.fake_set_user(user)
        self.profile(user)
        return runner.invoke(app, ["--repo", str(repo), *args])

    def remote_refs(self) -> dict[str, str]:
        return {
            ref: sha
            for ref in self.git_world.remote_git.list_refs("refs/heads")
            if (sha := self.git_world.remote_git.rev_parse(ref)) is not None
        }


@pytest.fixture
def env(git_world: GitWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    lead = git_world.clone("lead")
    git_world.commit_file(lead, ".forge/config.yml", CONFIG, "chore: forge config")
    git_world.commit_file(lead, ".forge/zones.yml", ZONES_YML, "chore: zones")
    git_world.commit_file(lead, "specs/m1/x.md", SPEC, "docs: spec")
    git_world.push(lead, "main")

    fake = FakeGitHub(repo="acme/shop")
    ensure_labels(fake)

    def factory(config: ForgeConfig) -> GitHubClient:
        assert config.github.repo == "acme/shop"
        return fake

    clock = Clock()
    sleeps: list[float] = []
    monkeypatch.setattr(_context, "github_factory", factory)
    monkeypatch.setattr(_claims, "clock", clock)
    monkeypatch.setattr(_claims, "sleep", sleeps.append)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return Env(git_world, fake, xdg, tmp_path / "worktrees", clock, sleeps)


def lease_of(fake: FakeGitHub, number: int) -> Any:
    found = find_lease(fake.list_comments(number))
    assert found is not None and found.data is not None
    return found.data


# --------------------------------------------------------------------------- next


def test_next_claims_and_creates_worktree(env: Env) -> None:
    n = env.task("M1-T01")
    repo = env.clone("alice")

    code, out = env.invoke(repo, "next")

    assert code == 0
    data = out["data"]
    assert data["claimed"]["issue"] == n and data["claimed"]["id"] == "M1-T01"
    worktree = env.worktree(n)
    assert data["workspace"]["path"] == str(worktree)
    assert data["workspace"]["in_place"] is False
    assert Git(worktree).current_branch() == f"task/{n}"
    current = json.loads((worktree / ".forge/local/current.json").read_text())
    assert current["issue"] == n and current["task"] == "M1-T01"
    assert current["worktree"] == str(worktree)
    assert current["claim"] == data["claimed"]["claim"]
    assert data["current"]["path"] == str(worktree / ".forge/local/current.json")
    # .forge/local is ignored: the worktree stays clean
    assert not Git(worktree).is_dirty(include_untracked=True)
    # the main clone is untouched
    assert Git(repo).current_branch() == "main"
    # GitHub side
    issue = env.fake.get_issue(n)
    assert issue.assignees == ("alice",)
    assert "status:in-progress" in issue.labels
    lease = lease_of(env.fake, n)
    assert lease.owner == "alice" and lease.claim == data["claimed"]["claim"]
    # task context
    context = data["context"]
    assert context["task"] == "M1-T01"
    assert context["spec"]["acceptance_criteria"][0]["text"].endswith("the API answers.")
    assert context["zones"][0]["paths"] == ["src/api/**"]
    assert context["lease"]["state"] == "live" and context["lease"]["owner"] == "alice"
    assert env.remote_refs()[f"refs/heads/task/{n}"] == data["claimed"]["claim"]


def commit_subagent_definitions(env: Env, gitignore: bool = True) -> None:
    """Commit the template `.forge/agents` (and its `.gitignore`) to main."""
    from importlib import resources

    template = resources.files("forge") / "templates" / "project"
    lead = env.git_world.clone("lead-agents")
    for name in ("explorer", "reviewer", "test-writer"):
        text = (template / ".forge" / "agents" / f"{name}.md").read_text("utf-8")
        env.git_world.commit_file(lead, f".forge/agents/{name}.md", text, f"chore: {name}")
    if gitignore:
        text = (template / ".gitignore").read_text("utf-8")
        env.git_world.commit_file(lead, ".gitignore", text, "chore: gitignore")
    env.git_world.push(lead, "main")


def test_next_renders_subagents_into_the_task_worktree(env: Env) -> None:
    commit_subagent_definitions(env)
    env.extra_agents = CODEX_AGENT
    n = env.task("M1-T01")
    repo = env.clone("alice")

    code, out = env.invoke(repo, "next")

    assert code == 0, out
    worktree = env.worktree(n)
    reviewer = worktree / ".claude" / "agents" / "forge-reviewer.md"
    assert "model: opus" in reviewer.read_text()
    assert (worktree / ".codex" / "agents" / "forge-explorer.toml").is_file()
    assert not Git(worktree).is_dirty(include_untracked=True)
    assert not (repo / ".claude").exists()  # the main clone is not touched by `next`


def test_next_subagent_render_failure_is_a_warning(env: Env) -> None:
    commit_subagent_definitions(env, gitignore=False)
    n = env.task("M1-T01")
    repo = env.clone("alice")

    code, out = env.invoke(repo, "next")

    assert code == 0, out
    assert out["data"]["claimed"]["issue"] == n
    warnings = [m["text"] for m in out["messages"] if m["level"] == "warning"]
    assert any("machine-level adapters not rendered" in w for w in warnings)
    assert not (env.worktree(n) / ".claude").exists()
    assert Git(env.worktree(n)).current_branch() == f"task/{n}"


def test_next_without_subagent_definitions_renders_nothing(env: Env) -> None:
    n = env.task("M1-T01")
    code, out = env.invoke(env.clone("alice"), "next")
    assert code == 0
    assert not (env.worktree(n) / ".claude").exists()
    assert not any("machine-level" in m["text"] for m in out["messages"])


def test_next_no_worktree_checks_out_in_place(env: Env) -> None:
    n = env.task("M1-T01")
    repo = env.clone("alice")

    code, out = env.invoke(repo, "next", "--no-worktree")

    assert code == 0
    assert out["data"]["workspace"]["in_place"] is True
    assert out["data"]["workspace"]["path"] == str(repo)
    assert Git(repo).current_branch() == f"task/{n}"
    current = json.loads((repo / ".forge/local/current.json").read_text())
    assert current["worktree"] is None and current["issue"] == n
    assert not env.worktrees.exists()
    assert not Git(repo).is_dirty(include_untracked=True)


def test_next_dry_run_pushes_nothing(env: Env) -> None:
    first = env.task("M1-T01")
    second = env.task("M1-T02", ("web",))
    repo = env.clone("alice")
    before = env.remote_refs()

    code, out = env.invoke(repo, "next", "--dry-run")

    assert code == 0
    assert out["data"]["dry_run"] is True and out["data"]["claimed"] is None
    assert [c["issue"] for c in out["data"]["candidates"]] == [first, second]
    assert env.remote_refs() == before
    assert env.fake.get_issue(first).assignees == ()
    assert env.fake.list_comments(first) == []
    assert not env.worktrees.exists()

    human = env.human(repo, "next", "--dry-run")
    assert human.exit_code == 0
    assert "Candidates (2)" in human.stdout and "M1-T02" in human.stdout


def test_next_nothing_available_is_code_3(env: Env) -> None:
    env.task("M1-T01")
    repo = env.clone("alice")
    assert env.invoke(repo, "next")[0] == 0

    code, out = env.invoke(repo, "next")

    assert code == 3
    assert out["ok"] is False
    assert out["data"]["claimed"] is None
    assert out["data"]["excluded"] == {"mine": 1}
    assert out["data"]["reason"] == "nothing claimable"
    levels = {m["level"] for m in out["messages"]}
    assert "info" in levels  # "you already hold #1"


def test_next_local_step_failure_keeps_the_claim_in_data(env: Env) -> None:
    """The claim is held even when the worktree cannot be prepared: --json must say so."""
    n = env.task("M1-T01")
    repo = env.clone("alice")
    git = Git(repo)
    git.run("branch", f"task/{n}", "main")
    git.run("switch", "--quiet", f"task/{n}")
    (repo / "local.txt").write_text("local\n")
    git.add(["local.txt"])
    git.commit("chore: local work")  # task/<n> now diverges from the claimed tip
    git.run("switch", "--quiet", "main")

    code, out = env.invoke(repo, "next")

    assert code == 5
    assert out["ok"] is False
    claimed = out["data"]["claimed"]
    assert claimed is not None and claimed["issue"] == n
    assert env.remote_refs()[f"refs/heads/task/{n}"] == claimed["claim"]
    texts = [m["text"] for m in out["messages"]]
    assert any("has diverged" in t for t in texts)
    assert any(f"forge next --task {n}" in t for t in texts)
    assert lease_of(env.fake, n).claim == claimed["claim"]


def test_next_with_unknown_task_is_code_3(env: Env) -> None:
    env.task("M1-T01")
    code, out = env.invoke(env.clone("alice"), "next", "--task", "M1-T09")
    assert code == 3
    assert "no open Forge task matches" in out["data"]["reason"]


def test_next_without_profile_is_code_5(env: Env) -> None:
    env.task("M1-T01")
    repo = env.clone("alice")
    env.fake.fake_set_user("alice")
    # no profile written: invoke the CLI directly
    result = runner.invoke(app, ["--json", "--repo", str(repo), "next"])
    out = json.loads(result.stdout)
    assert result.exit_code == 5 and out["code"] == 5 and set(out) == ENVELOPE_KEYS
    assert "no Forge profile" in out["messages"][0]["text"]
    assert out["messages"][0]["hint"] == _claims.PROFILE_HINT


def test_next_with_invalid_profile_is_code_5(env: Env) -> None:
    env.task("M1-T01")
    repo = env.clone("alice")
    path = env.profile("alice")
    path.write_text("github: alice\nagents: []\ndefault_agent: x\n")
    env.fake.fake_set_user("alice")
    result = runner.invoke(app, ["--json", "--repo", str(repo), "next"])
    out = json.loads(result.stdout)
    assert result.exit_code == 5 and out["code"] == 5
    assert "agents" in out["messages"][0]["text"]


def test_next_unknown_agent_is_code_5(env: Env) -> None:
    env.task("M1-T01")
    env.fake.fake_set_user("alice")
    env.profile("alice")
    repo = env.clone("alice")
    result = runner.invoke(app, ["--json", "--agent", "nope", "--repo", str(repo), "next"])
    out = json.loads(result.stdout)
    assert result.exit_code == 5 and out["code"] == 5
    assert "unknown agent" in out["messages"][0]["text"]


def test_next_login_mismatch_is_code_5(env: Env) -> None:
    n = env.task("M1-T01")
    code, out = env.invoke(env.clone("alice"), "next", user="bob", profile="alice")
    assert code == 5
    assert "'bob'" in out["messages"][0]["text"] and "'alice'" in out["messages"][0]["text"]
    assert f"refs/heads/task/{n}" not in env.remote_refs()


def test_next_version_mismatch_is_code_5(env: Env) -> None:
    n = env.task("M1-T01")
    repo = env.clone("alice")
    (repo / ".forge/config.yml").write_text(CONFIG.replace(">=0.1,<0.2", ">=9"))
    code, out = env.invoke(repo, "next")
    assert code == 5
    assert "forge_version" in out["messages"][0]["text"]
    assert f"refs/heads/task/{n}" not in env.remote_refs()


def test_next_reports_profile_constraints_as_warnings(env: Env) -> None:
    env.task("M1-T01")
    repo = env.clone("alice")
    config = CONFIG + "models:\n  allowed_providers: [openai]\n"
    (repo / ".forge/config.yml").write_text(config)
    code, out = env.invoke(repo, "next", "--dry-run")
    assert code == 0
    warnings = [m for m in out["messages"] if m["level"] == "warning"]
    assert any("allowed_providers" in m["text"] for m in warnings)


def test_next_human_output_renders_the_context(env: Env) -> None:
    env.task("M1-T01")
    result = env.human(env.clone("alice"), "next")
    assert result.exit_code == 0, result.output
    assert "Claimed #1 M1-T01 on task/1" in result.stdout
    assert "## Acceptance criteria" in result.stdout
    assert "## Lease" in result.stdout


# --------------------------------------------------------------------------- task show


def test_task_show_by_ref_and_current(env: Env) -> None:
    n = env.task("M1-T01")
    env.task("M1-T02", ("web",))
    repo = env.clone("alice")
    assert env.invoke(repo, "next")[0] == 0
    worktree = env.worktree(n)
    refs = env.remote_refs()
    comments = len(env.fake.list_comments(n))
    labels = env.fake.get_issue(n).labels

    for ref in ("M1-T01", f"#{n}", str(n)):
        code, out = env.invoke(repo, "task", "show", ref)
        assert code == 0, out
        assert out["data"]["task"] == "M1-T01" and out["data"]["issue"]["number"] == n
        assert out["data"]["lease"]["state"] == "live"

    code, out = env.invoke(worktree, "task", "show")
    assert code == 0 and out["data"]["task"] == "M1-T01"

    code, out = env.invoke(repo, "task", "show", "M1-T02")
    assert code == 0 and out["data"]["lease"] is None

    # read-only
    assert env.remote_refs() == refs
    assert len(env.fake.list_comments(n)) == comments
    assert env.fake.get_issue(n).labels == labels

    human = env.human(worktree, "task", "show")
    assert human.exit_code == 0 and human.stdout.startswith(f"# #{n} [M1-T01] task")


def test_task_show_outside_a_task_is_code_5(env: Env) -> None:
    env.task("M1-T01")
    code, out = env.invoke(env.clone("alice"), "task", "show")
    assert code == 5
    assert out["messages"][0]["hint"] == _claims.NO_CURRENT_HINT


def test_task_show_unknown_task_is_code_1(env: Env) -> None:
    env.task("M1-T01")
    code, out = env.invoke(env.clone("alice"), "task", "show", "M9-T01")
    assert code == 1
    assert "no Forge task matches" in out["messages"][0]["text"]


# --------------------------------------------------------------------------- heartbeat


# --------------------------------------------------------------------------- architecture

PLAN_WITH_ARCH = """\
milestone: M1
tasks:
  - id: M1-T01
    title: task
    component: api-svc
    implements: [IF-schema]
    uses: [IF-legacy]
"""

ARCH_FILE = "docs/architecture/M1.md"
FILLED_ARCH = {
    "path": ARCH_FILE,
    "component": "api-svc",
    "implements": ["IF-schema"],
    "uses": ["IF-legacy"],
}


def commit_architecture(env: Env) -> None:
    """Push ``plans/M1.yml`` (with architecture fields) and ``docs/architecture/M1.md``."""
    lead = env.git_world.clone("lead-arch")
    env.git_world.commit_file(lead, "plans/M1.yml", PLAN_WITH_ARCH, "chore: plan")
    env.git_world.commit_file(lead, ARCH_FILE, "# M1 architecture\n", "docs: architecture")
    env.git_world.push(lead, "main")


def test_next_context_has_the_architecture(env: Env) -> None:
    env.task("M1-T01")
    repo = env.clone("alice")

    code, out = env.invoke(repo, "next")

    assert code == 0, out
    assert out["data"]["context"]["architecture"] == {
        "path": None,
        "component": None,
        "implements": [],
        "uses": [],
    }


def test_next_reads_the_architecture_from_the_fresh_worktree(env: Env) -> None:
    n = env.task("M1-T01")
    repo = env.clone("alice")  # cloned before the architecture lands: a stale checkout
    commit_architecture(env)

    code, out = env.invoke(repo, "next")

    assert code == 0, out
    assert not (repo / ARCH_FILE).exists()  # the main clone is still stale
    assert (env.worktree(n) / ARCH_FILE).is_file()
    assert out["data"]["context"]["architecture"] == FILLED_ARCH
    assert not any(m["level"] == "warning" for m in out["messages"])


def test_task_show_has_the_architecture_block(env: Env) -> None:
    n = env.task("M1-T01")
    commit_architecture(env)
    repo = env.clone("alice")
    assert env.invoke(repo, "next")[0] == 0
    worktree = env.worktree(n)

    code, out = env.invoke(worktree, "task", "show")
    assert code == 0, out
    assert out["data"]["architecture"] == FILLED_ARCH

    code, out = env.invoke(repo, "task", "show", "M1-T01")
    assert code == 0 and out["data"]["architecture"] == FILLED_ARCH

    human = env.human(worktree, "task", "show")
    assert human.exit_code == 0, human.output
    assert (
        "## Architecture\n"
        f"- path: {ARCH_FILE}\n"
        "- component: api-svc\n"
        "- implements: IF-schema\n"
        "- uses: IF-legacy\n"
    ) in human.stdout


def test_task_show_without_architecture_has_no_block(env: Env) -> None:
    env.task("M1-T01")
    repo = env.clone("alice")
    assert env.invoke(repo, "next")[0] == 0
    human = env.human(env.worktree(1), "task", "show")
    assert human.exit_code == 0 and "## Architecture" not in human.stdout


def test_heartbeat_updates_the_lease(env: Env) -> None:
    n = env.task("M1-T01")
    assert env.invoke(env.clone("alice"), "next")[0] == 0
    worktree = env.worktree(n)
    env.clock.now = T0 + timedelta(hours=5)

    code, out = env.invoke(worktree, "heartbeat")

    assert code == 0
    assert out["data"]["heartbeat_at"] == "2026-11-02T14:00:00Z"
    assert out["data"]["stale_at"] == "2026-11-03T14:00:00Z"
    assert lease_of(env.fake, n).heartbeat_at == T0 + timedelta(hours=5)

    human = env.human(worktree, "heartbeat")
    assert human.exit_code == 0 and "renewed at" in human.stdout


def test_heartbeat_on_my_stale_claim_is_code_5_with_a_reclaim_hint(env: Env) -> None:
    n = env.task("M1-T01")
    assert env.invoke(env.clone("alice"), "next")[0] == 0
    env.clock.now = T0 + timedelta(hours=30)
    code, out = env.invoke(env.worktree(n), "heartbeat")
    assert code == 5
    error = out["messages"][-1]
    assert error["level"] == "error" and "went stale" in error["text"]
    assert f"forge reclaim {n}" in error["hint"]
    assert lease_of(env.fake, n).heartbeat_at == T0  # untouched


def test_heartbeat_and_release_use_the_agent_recorded_in_current_json(env: Env) -> None:
    env.extra_agents = CODEX_AGENT
    n = env.task("M1-T01")
    code, out = env.invoke(env.clone("alice"), "--agent", "codex", "next")
    assert code == 0 and out["data"]["claimed"]["agent"] == "codex"
    worktree = env.worktree(n)
    env.clock.now = T0 + timedelta(hours=1)

    code, out = env.invoke(worktree, "heartbeat")  # no --agent: codex from current.json
    assert code == 0, out
    assert lease_of(env.fake, n).agent == "codex"
    # an explicit --agent still wins
    code, out = env.invoke(worktree, "--agent", "claude", "heartbeat")
    assert code == 5 and "belongs to alice with agent codex" in out["messages"][-1]["text"]

    code, out = env.invoke(worktree, "release-claim")
    assert code == 0, out
    assert lease_of(env.fake, n).released_at is not None


def test_heartbeat_outside_a_task_is_code_5(env: Env) -> None:
    env.task("M1-T01")
    code, _out = env.invoke(env.clone("alice"), "heartbeat")
    assert code == 5


def test_heartbeat_on_someone_elses_claim_is_code_5(env: Env) -> None:
    n = env.task("M1-T01")
    assert env.invoke(env.clone("alice"), "next")[0] == 0
    code, out = env.invoke(env.worktree(n), "heartbeat", user="bob")
    assert code == 5
    assert "belongs to alice" in out["messages"][0]["text"]


# --------------------------------------------------------------------------- release-claim


def test_release_claim_clears_current(env: Env) -> None:
    n = env.task("M1-T01")
    assert env.invoke(env.clone("alice"), "next")[0] == 0
    worktree = env.worktree(n)
    assert (worktree / ".forge/local/current.json").exists()

    code, out = env.invoke(worktree, "release-claim")

    assert code == 0
    assert out["data"]["current_cleared"] is True
    assert out["data"]["status"] == "ready"
    assert out["data"]["release_commit"] is not None
    assert not (worktree / ".forge/local/current.json").exists()
    issue = env.fake.get_issue(n)
    assert issue.assignees == ()
    assert "status:ready" in issue.labels
    assert lease_of(env.fake, n).released_at is not None


def test_release_claim_delete_branch(env: Env) -> None:
    n = env.task("M1-T01")
    assert env.invoke(env.clone("alice"), "next", "--no-worktree")[0] == 0
    code, out = env.invoke(env.clone("alice"), "release-claim", "--delete-branch")
    assert code == 0 and out["data"]["deleted"] is True
    assert f"refs/heads/task/{n}" not in env.remote_refs()


def test_release_claim_outside_a_task_is_code_5(env: Env) -> None:
    code, _out = env.invoke(env.clone("alice"), "release-claim")
    assert code == 5


# --------------------------------------------------------------------------- reclaim


def test_reclaim_fresh_is_3_stale_is_0(env: Env) -> None:
    n = env.task("M1-T01")
    assert env.invoke(env.clone("bob"), "next", user="bob")[0] == 0
    alice = env.clone("alice")

    code, out = env.invoke(alice, "reclaim", str(n))
    assert code == 3
    assert out["data"]["claimed"] is None
    assert "stale" in out["data"]["reason"]

    env.clock.now = T0 + timedelta(hours=25)
    code, out = env.invoke(alice, "reclaim", f"#{n}")

    assert code == 0
    claimed = out["data"]["claimed"]
    assert claimed["reclaimed"] is True and claimed["previous_claimant"] == "bob"
    worktree = Path(out["data"]["workspace"]["path"])
    assert Git(worktree).current_branch() == f"task/{n}"
    current = json.loads((worktree / ".forge/local/current.json").read_text())
    assert current["claim"] == claimed["claim"]
    assert env.fake.get_issue(n).assignees == ("alice",)
    assert out["data"]["context"]["lease"]["owner"] == "alice"
    assert env.sleeps == []


# --------------------------------------------------------------------------- delegation


def commit_all_subagents(env: Env, config: str | None = None) -> None:
    """Commit every template subagent (implementer included), the `.gitignore` and, when
    given, a replacement `.forge/config.yml` to main."""
    from importlib import resources

    template = resources.files("forge") / "templates" / "project"
    lead = env.git_world.clone("lead-delegation")
    for name in ("explorer", "implementer", "reviewer", "test-writer"):
        text = (template / ".forge" / "agents" / f"{name}.md").read_text("utf-8")
        env.git_world.commit_file(lead, f".forge/agents/{name}.md", text, f"chore: {name}")
    text = (template / ".gitignore").read_text("utf-8")
    env.git_world.commit_file(lead, ".gitignore", text, "chore: gitignore")
    if config is not None:
        env.git_world.commit_file(lead, ".forge/config.yml", config, "chore: config")
    env.git_world.push(lead, "main")


def big_task(env: Env, task_id: str = "M1-T01") -> int:
    body = task_body(task_id, ("api",)).replace("size: S", "size: M")
    issue = env.fake.create_issue(f"[{task_id}] task", body, labels=["forge:task", "status:ready"])
    return issue.number


def test_next_delegates_an_m_task(env: Env) -> None:
    commit_all_subagents(env)
    n = big_task(env)
    code, out = env.invoke(env.clone("alice"), "next")
    assert code == 0, out
    assert (env.worktree(n) / ".claude/agents/forge-implementer.md").is_file()
    assert out["data"]["context"]["delegation"] == {
        "mode": "auto",
        "requested": "auto",
        "source": "project",
        "delegate": True,
        "reason": "size M",
        "subagent": "forge-implementer",
        "model": "sonnet",
        "warnings": [],
        "notes": [],
    }


def test_next_delegation_override_off(env: Env) -> None:
    commit_all_subagents(env)
    big_task(env)
    code, out = env.invoke(env.clone("alice"), "next", "--delegation", "off")
    assert code == 0, out
    delegation = out["data"]["context"]["delegation"]
    assert (delegation["mode"], delegation["source"], delegation["delegate"]) == (
        "off",
        "invocation",
        False,
    )
    assert delegation["reason"] == "requested by --delegation"


def test_next_small_task_is_not_delegated_by_auto(env: Env) -> None:
    commit_all_subagents(env)
    env.task("M1-T01")
    code, out = env.invoke(env.clone("alice"), "next")
    assert code == 0
    delegation = out["data"]["context"]["delegation"]
    assert (delegation["mode"], delegation["delegate"]) == ("auto", False)
    assert delegation["reason"] == "1 zone, size S"


def test_next_human_output_has_the_delegation_block(env: Env) -> None:
    commit_all_subagents(env)
    big_task(env)
    result = env.human(env.clone("alice"), "next")
    assert result.exit_code == 0, result.output
    assert "## Delegation" in result.stdout
    assert "- decision: delegate to `forge-implementer` (`sonnet`): size M" in result.stdout


def test_task_show_reports_delegation_in_the_worktree(env: Env) -> None:
    commit_all_subagents(env)
    n = big_task(env)
    assert env.invoke(env.clone("alice"), "next")[0] == 0
    code, out = env.invoke(env.worktree(n), "task", "show")
    assert code == 0
    assert out["data"]["delegation"]["delegate"] is True
    code, out = env.invoke(env.worktree(n), "task", "show", "--delegation", "off")
    assert out["data"]["delegation"]["mode"] == "off"
    human = env.human(env.worktree(n), "task", "show", "--delegation", "on")
    assert "## Delegation" in human.stdout
    assert "- mode: on (requested on by --delegation)" in human.stdout


def test_task_show_in_main_clone_without_rendered_files_is_off_with_hint(env: Env) -> None:
    commit_all_subagents(env)
    big_task(env)
    code, out = env.invoke(env.clone("alice"), "task", "show", "M1-T01", "--delegation", "on")
    assert code == 0
    delegation = out["data"]["delegation"]
    assert (delegation["mode"], delegation["requested"]) == ("off", "on")
    assert delegation["reason"] == "forge-implementer not rendered"
    infos = [m["text"] for m in out["messages"] if m["level"] == "info"]
    assert any("forge adapters generate --local" in t for t in infos)


def test_task_show_without_profile_is_off(env: Env) -> None:
    big_task(env)
    repo = env.clone("alice")
    result = runner.invoke(app, ["--json", "--repo", str(repo), "task", "show", "M1-T01"])
    assert result.exit_code == 0, result.output
    delegation = json.loads(result.stdout)["data"]["delegation"]
    assert (delegation["mode"], delegation["reason"]) == ("off", "no profile")
    human = runner.invoke(app, ["--repo", str(repo), "task", "show", "M1-T01"])
    assert "## Delegation" in human.stdout
    assert "implement in the main session: no profile" in human.stdout


def test_task_show_profile_mode_and_policy_fallback(env: Env) -> None:
    config = CONFIG + "delegation:\n  default: auto\n  allowed: [off, auto]\n"
    commit_all_subagents(env, config)
    n = big_task(env)
    assert env.invoke(env.clone("alice"), "next")[0] == 0
    worktree = env.worktree(n)
    env.profile("alice")
    path = env.xdg / "forge" / "profile.yml"
    path.write_text(path.read_text() + "delegation: off\n")
    result = runner.invoke(app, ["--json", "--repo", str(worktree), "task", "show"])
    delegation = json.loads(result.stdout)["data"]["delegation"]
    assert (delegation["mode"], delegation["source"]) == ("off", "profile")
    # an override the project does not allow falls back to the project default, with a warning
    code, out = env.invoke(worktree, "task", "show", "--delegation", "on")
    assert code == 0
    delegation = out["data"]["delegation"]
    assert (delegation["mode"], delegation["requested"], delegation["delegate"]) == (
        "auto",
        "on",
        True,
    )
    warnings = [m["text"] for m in out["messages"] if m["level"] == "warning"]
    assert any("delegation.allowed" in w for w in warnings)


def test_task_show_rejects_an_unknown_delegation_mode(env: Env) -> None:
    env.task("M1-T01")
    result = env.human(env.clone("alice"), "task", "show", "M1-T01", "--delegation", "always")
    assert result.exit_code == 1
    assert "always" in result.output


# ------------------------------------------------------------ delegation: read-only profile


def _show(env: Env, repo: Path, *args: str) -> tuple[int, dict[str, Any]]:
    """``task show --json`` without rewriting the profile (it is set up by the test)."""
    env.fake.fake_set_user("alice")
    result = runner.invoke(app, ["--json", "--repo", str(repo), "task", "show", *args])
    return result.exit_code, json.loads(result.stdout)


@pytest.mark.parametrize("how", ["flag", "env"])
def test_task_show_unknown_agent_never_fails(
    env: Env, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    commit_all_subagents(env, OFF_CONFIG)  # delegation off: messages must match v0.1.0
    env.task("M1-T01")
    repo = env.clone("alice")
    env.profile("alice")
    _, baseline = _show(env, repo, "M1-T01")
    if how == "flag":
        env.fake.fake_set_user("alice")
        result = runner.invoke(
            app, ["--json", "--repo", str(repo), "--agent", "ghost", "task", "show", "M1-T01"]
        )
        code, out = result.exit_code, json.loads(result.stdout)
    else:
        monkeypatch.setenv("FORGE_AGENT", "ghost")
        code, out = _show(env, repo, "M1-T01")
    assert code == 0, out
    assert out["messages"] == baseline["messages"]
    assert any("'ghost' is not in the profile" in n for n in out["data"]["delegation"]["notes"])


def test_task_show_invalid_profile_is_one_note_inside_delegation(env: Env) -> None:
    commit_all_subagents(env, OFF_CONFIG)  # delegation off: messages must match v0.1.0
    env.task("M1-T01")
    repo = env.clone("alice")
    path = env.profile("alice")
    _, baseline = _show(env, repo, "M1-T01")
    path.write_text("github: [not, a, login]\n")
    code, out = _show(env, repo, "M1-T01")
    assert code == 0
    assert out["messages"] == baseline["messages"]
    notes = out["data"]["delegation"]["notes"]
    assert [n for n in notes if n.startswith("profile not used:")] == notes[-1:]
    assert len([n for n in notes if "profile" in n]) == 1


# ------------------------------------------------------------ delegation: recorded override


def _current_json(worktree: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((worktree / ".forge/local/current.json").read_text())
    return data


def test_next_records_the_override_and_task_show_follows_it(env: Env) -> None:
    commit_all_subagents(env)
    n = env.task("M1-T01")
    code, out = env.invoke(env.clone("alice"), "next", "--delegation", "on")
    assert code == 0, out
    worktree = env.worktree(n)
    assert _current_json(worktree)["delegation_override"] == "on"
    assert out["data"]["current"]["delegation_override"] == "on"
    code, out = env.invoke(worktree, "task", "show")
    delegation = out["data"]["delegation"]
    assert (delegation["mode"], delegation["source"], delegation["delegate"]) == (
        "on",
        "invocation",
        True,
    )


def test_task_show_override_on_the_current_task_is_recorded(env: Env) -> None:
    commit_all_subagents(env)
    n = env.task("M1-T01")
    other = env.task("M1-T02", ("web",))
    assert env.invoke(env.clone("alice"), "next", "--task", str(n))[0] == 0
    worktree = env.worktree(n)
    assert "delegation_override" not in _current_json(worktree)
    # another task: shown with the override, nothing recorded
    env.invoke(worktree, "task", "show", str(other), "--delegation", "off")
    assert "delegation_override" not in _current_json(worktree)
    # the current task, by default and by reference
    env.invoke(worktree, "task", "show", "--delegation", "off")
    assert _current_json(worktree)["delegation_override"] == "off"
    env.invoke(worktree, "task", "show", "M1-T01", "--delegation", "on")
    assert _current_json(worktree)["delegation_override"] == "on"
    # a later run without the flag follows the recorded override
    _, out = env.invoke(worktree, "task", "show")
    assert out["data"]["delegation"]["source"] == "invocation"


def test_resume_keeps_the_recorded_override(env: Env) -> None:
    n = env.task("M1-T01")
    repo = env.clone("alice")
    assert env.invoke(repo, "next", "--delegation", "off")[0] == 0
    code, out = env.invoke(repo, "next", "--task", str(n))
    assert code == 0, out
    assert _current_json(env.worktree(n))["delegation_override"] == "off"


def test_status_me_reports_the_recorded_override(env: Env) -> None:
    commit_all_subagents(env)
    n = big_task(env)
    assert env.invoke(env.clone("alice"), "next", "--delegation", "off")[0] == 0
    code, out = env.invoke(env.worktree(n), "status", "--me")
    assert code == 0, out
    delegation = out["data"]["me"]["current"]["delegation"]
    assert (delegation["mode"], delegation["source"], delegation["delegate"]) == (
        "off",
        "invocation",
        False,
    )


# ------------------------------------------------------- delegation: v0.1.0 equivalence


OFF_CONFIG = CONFIG + "delegation:\n  default: off\n  allowed: [off]\n"
V010_CONTEXT_KEYS = [
    "task",
    "metadata",
    "issue",
    "body",
    "spec",
    "zones",
    "dependencies",
    "handoff",
    "adrs",
    "lease",
    "warnings",
]
V010_CURRENT_KEYS = [
    "path",
    "task",
    "issue",
    "branch",
    "base",
    "claim",
    "agent",
    "claimed_at",
    "worktree",
]


def _without(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in keys}


def test_delegation_off_everywhere_equals_v010(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Done-when of ADR 0017 for `next`, `task show` and `status`.

    Approach: `off` in the project, the profile and `--delegation off`; each payload is
    compared with the same command run with the delegation code path neutralised (the hook
    patched to do nothing, as in v0.1.0), after removing only the new fields
    (`delegation`, and `delegation_override` in `current`); `messages` must be equal. For
    `next`, which cannot be re-run on the same claim, the claimed context is compared with the
    neutralised `task show` of the same task, the key sets are pinned to v0.1.0's, and the
    delegation step is checked to add no message.
    """
    commit_all_subagents(env, OFF_CONFIG)
    env.extra_top = "delegation: off\n"
    n = big_task(env)

    added: list[int] = []
    real_attach = _claims.attach_delegation

    def spy(*args: Any, **kwargs: Any) -> None:
        result = args[6]
        before = len(result.messages)
        real_attach(*args, **kwargs)
        added.append(len(result.messages) - before)

    with monkeypatch.context() as patch:
        patch.setattr(_claims, "attach_delegation", spy)
        code, nxt = env.invoke(env.clone("alice"), "next", "--delegation", "off")
    assert code == 0, nxt
    assert added == [0]
    data = nxt["data"]
    assert list(data["context"]) == [
        *V010_CONTEXT_KEYS[:10],
        "delegation",
        "architecture",
        "warnings",
    ]
    assert list(data["current"]) == [*V010_CURRENT_KEYS, "delegation_override"]
    assert data["context"]["delegation"]["mode"] == "off"
    assert all(not m["text"].lower().count("delegation") for m in nxt["messages"])

    worktree = env.worktree(n)
    real = env.invoke(worktree, "task", "show", "--delegation", "off")
    with monkeypatch.context() as patch:
        patch.setattr(_claims, "show_delegation", lambda *a, **k: None)
        neutral = env.invoke(worktree, "task", "show", "--delegation", "off")
    assert real[0] == neutral[0] == 0
    assert real[1]["messages"] == neutral[1]["messages"]
    # ADR 0019 adds `architecture`; it is stripped like `delegation` (ADR 0017).
    new_keys = ("delegation", "architecture")
    assert _without(real[1]["data"], *new_keys) == _without(neutral[1]["data"], *new_keys)
    assert list(_without(neutral[1]["data"], *new_keys)) == V010_CONTEXT_KEYS
    assert _without(data["context"], *new_keys) == _without(neutral[1]["data"], *new_keys)

    real_status = env.invoke(worktree, "status")
    with monkeypatch.context() as patch:
        patch.setattr(report, "delegation_for", lambda *a, **k: None)
        neutral_status = env.invoke(worktree, "status")
    assert real_status[0] == neutral_status[0]
    assert real_status[1]["messages"] == neutral_status[1]["messages"]
    real_me = real_status[1]["data"]["me"]["current"]
    neutral_me = neutral_status[1]["data"]["me"]["current"]
    assert real_me["delegation"]["mode"] == "off" and neutral_me["delegation"] is None
    real_data = real_status[1]["data"]
    real_data["me"]["current"] = _without(real_me, "delegation")
    neutral_data = neutral_status[1]["data"]
    neutral_data["me"]["current"] = _without(neutral_me, "delegation")
    assert real_data == neutral_data


# ------------------------------------------------------- delegation: saved agent, worktree


def test_status_me_uses_the_agent_saved_for_the_task(env: Env) -> None:
    commit_all_subagents(env)
    env.extra_agents = CODEX_AGENT
    n = big_task(env)
    env.fake.fake_set_user("alice")
    env.profile("alice")
    repo = env.clone("alice")
    result = runner.invoke(app, ["--json", "--repo", str(repo), "--agent", "codex", "next"])
    assert result.exit_code == 0, result.output
    assert _current_json(env.worktree(n))["agent"] == "codex"
    # status runs with the default agent (claude) but reports the claim's agent (codex)
    code, out = env.invoke(env.worktree(n), "status", "--me")
    assert code == 0, out
    assert out["data"]["me"]["agent"] == "claude"
    delegation = out["data"]["me"]["current"]["delegation"]
    assert (delegation["delegate"], delegation["model"]) == (True, "gpt-standard")
    # task show agrees
    _, shown = env.invoke(env.worktree(n), "task", "show")
    assert shown["data"]["delegation"]["model"] == "gpt-standard"


def test_task_show_from_main_clone_decides_in_the_task_worktree(env: Env) -> None:
    commit_all_subagents(env)
    n = big_task(env)
    repo = env.clone("alice")
    assert env.invoke(repo, "next")[0] == 0
    assert not (repo / ".claude/agents/forge-implementer.md").exists()
    code, out = env.invoke(repo, "task", "show", "M1-T01")
    assert code == 0
    delegation = out["data"]["delegation"]
    assert (delegation["delegate"], delegation["model"]) == (True, "sonnet")
    # an override given from the main clone is saved in the task's worktree
    env.invoke(repo, "task", "show", "M1-T01", "--delegation", "off")
    assert _current_json(env.worktree(n))["delegation_override"] == "off"
    assert not (repo / ".forge/local/current.json").exists()


def test_override_write_rereads_current_json(env: Env) -> None:
    n = env.task("M1-T01")
    assert env.invoke(env.clone("alice"), "next")[0] == 0
    worktree = env.worktree(n)
    location = _claims.find_current_task(Git(worktree))
    assert location is not None and location.current is not None
    # a concurrent update (e.g. a sync moving the claim) lands after our read
    newer = {**_current_json(worktree), "claim": "f" * 40}
    (worktree / ".forge/local/current.json").write_text(json.dumps(newer))
    assert _claims.remember_override(location, n, "on") == "on"
    saved = _current_json(worktree)
    assert saved["claim"] == "f" * 40
    assert saved["delegation_override"] == "on"
