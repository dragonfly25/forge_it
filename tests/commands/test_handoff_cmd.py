"""End-to-end tests for `forge handoff` through the CLI (spec §8.3, ADR 0012).

A real bare remote and clones (``git_world``) and a shared ``FakeGitHub``. Alice claims with
`forge next` (which creates the task worktree), hands off from the worktree, then `forge
status` lists the task under handoffs and bob's `forge next` prefers it and prints the note.
The profile lives under a temporary ``XDG_CONFIG_HOME``; the clock is injected through
``forge.commands._claims``.
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
from forge.core.config import ForgeConfig
from forge.core.context import HANDOFF_MARKER
from forge.core.git import Git
from forge.core.github import GitHubClient, GitHubError
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

NOTE = """\
## Done
- the API answers

## Remaining
- pagination (see src/api/page.py)

## Decisions
- cursor-based pages

## Gotchas
- [bold] is literal text, not markup

## Next step
- write the pagination test

## How to verify
- uv run pytest -q
"""


def profile_yaml(login: str, worktrees: Path) -> str:
    return (
        f"github: {login}\n"
        "agents:\n"
        "  - id: claude\n"
        "    tool: claude-code\n"
        "    providers: [anthropic]\n"
        "    tiers: {strong: opus, standard: sonnet, light: haiku}\n"
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
        size=Size.S if task_id.startswith("M1") else Size.M,
        tier=Tier.STANDARD,
        type=TaskType.IMPL,
        done_when=("tests pass",),
    )
    return upsert_block(f"Implement {task_id}.\n", meta)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Env:
    git_world: GitWorld
    fake: FakeGitHub
    xdg: Path
    worktrees: Path
    clock: Clock
    tmp: Path
    clones: dict[str, Git] = field(default_factory=dict)

    def clone(self, user: str) -> Path:
        if user not in self.clones:
            self.clones[user] = self.git_world.clone(user)
        return self.clones[user].cwd

    def task(self, task_id: str, zones: tuple[str, ...] = ("api",)) -> int:
        issue = self.fake.create_issue(
            f"[{task_id}] task", task_body(task_id, zones), labels=["forge:task", "status:ready"]
        )
        return issue.number

    def worktree(self, n: int, user: str = "alice") -> Path:
        return self.worktrees / user / f"task-{n}"

    def note(self, text: str = NOTE, name: str = "note.md") -> Path:
        path = self.tmp / name
        path.write_text(text)
        return path

    def profile(self, login: str) -> None:
        path = self.xdg / "forge" / "profile.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(profile_yaml(login, self.worktrees / login))

    def invoke(self, repo: Path, *args: str, user: str = "alice") -> tuple[int, dict[str, Any]]:
        self.fake.fake_set_user(user)
        self.profile(user)
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

    def remote_tip(self, n: int) -> str | None:
        return self.git_world.remote_git.rev_parse(f"refs/heads/task/{n}")

    def claim(self, task_id: str = "M1-T01") -> int:
        n = self.task(task_id)
        code, out = self.invoke(self.clone("alice"), "next", "--task", task_id)
        assert code == 0, out
        return n


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
    monkeypatch.setattr(_context, "github_factory", factory)
    monkeypatch.setattr(_claims, "clock", clock)
    monkeypatch.setattr(_claims, "sleep", lambda _s: None)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    notes = tmp_path / "notes"
    notes.mkdir()
    return Env(git_world, fake, xdg, tmp_path / "worktrees", clock, notes)


def install_pre_push(git_dir_of: Path, script: str) -> None:
    git = Git(git_dir_of)
    hooks = Path(git.run("rev-parse", "--git-common-dir").stdout.strip())
    if not hooks.is_absolute():
        hooks = git_dir_of / hooks
    hook = hooks / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\n" + script + "\n")
    hook.chmod(0o755)


# --------------------------------------------------------------------------- handoff


def test_handoff_then_status_lists_it_and_next_prefers_it_with_the_note(env: Env) -> None:
    n = env.claim("M9-T01")  # a late milestone: only the handoff preference puts it first
    env.task("M1-T02", ("web",))
    worktree = env.worktree(n)
    (worktree / "src" / "api").mkdir(parents=True)
    (worktree / "src" / "api" / "page.py").write_text("draft\n")
    env.clock.now = T0 + timedelta(hours=2)

    code, out = env.invoke(worktree, "handoff", "--note-file", str(env.note()))

    assert code == 0, out
    data = out["data"]
    assert data["outcome"] == "handed_off" and data["status"] == "handoff"
    assert data["wip_commit"] is not None and data["commits"] == 1
    assert data["current_cleared"] is True and data["missing_headings"] == []
    assert data["handoff_commit"] == env.remote_tip(n)
    assert not (worktree / ".forge/local/current.json").exists()
    assert worktree.is_dir() and Git(worktree).current_branch() == f"task/{n}"
    issue = env.fake.get_issue(n)
    assert issue.assignees == ()
    assert "status:handoff" in issue.labels and "status:in-progress" not in issue.labels
    lease = find_lease(env.fake.list_comments(n))
    assert lease is not None and lease.data is not None
    assert lease.data.released_at == T0 + timedelta(hours=2)
    comment = next(c for c in env.fake.list_comments(n) if HANDOFF_MARKER in c.body)
    assert f"tip `{data['handoff_commit']}`" in comment.body

    # forge status: under handoffs, by alice, with the note
    code, report = env.invoke(env.clone("carol"), "status", user="carol")
    assert code == 0, report
    handoffs = report["data"]["team"]["handoffs"]
    assert [h["issue"] for h in handoffs] == [n]
    assert handoffs[0]["by"] == "alice" and handoffs[0]["note"]["author"] == "alice"

    # forge next (bob): the handoff task first, its note in the context
    code, taken = env.invoke(env.clone("bob"), "next", user="bob")
    assert code == 0, taken
    claimed = taken["data"]["claimed"]
    assert claimed["issue"] == n and claimed["resumed"] is True
    note = taken["data"]["context"]["handoff"]
    assert note["author"] == "alice" and "## Next step" in note["body"]
    bob_tree = Path(taken["data"]["workspace"]["path"])
    assert (bob_tree / "src" / "api" / "page.py").read_text() == "draft\n"


def test_next_human_output_prints_the_handoff_note(env: Env) -> None:
    n = env.claim()
    assert env.invoke(env.worktree(n), "handoff", "--note-file", str(env.note()))[0] == 0
    result = env.human(env.clone("bob"), "next", user="bob")
    assert result.exit_code == 0, result.output
    assert "## Latest handoff note (by alice" in result.stdout
    assert "write the pagination test" in result.stdout


def test_handoff_human_output(env: Env) -> None:
    n = env.claim()
    result = env.human(env.worktree(n), "handoff", "--note-file", str(env.note()))
    assert result.exit_code == 0, result.output
    assert f"Handed off  M1-T01 #{n} on task/{n}" in result.stdout
    assert "status: handoff; you are unassigned" in result.stdout
    assert "current.json cleared; the worktree is kept" in result.stdout


def test_missing_headings_are_a_warning(env: Env) -> None:
    n = env.claim()
    code, out = env.invoke(env.worktree(n), "handoff", "--note-file", str(env.note("## Done\nx\n")))
    assert code == 0
    assert out["data"]["missing_headings"] == [
        "Remaining",
        "Decisions",
        "Gotchas",
        "Next step",
        "How to verify",
    ]
    warnings = [m["text"] for m in out["messages"] if m["level"] == "warning"]
    assert any("lacks the §8.3 heading(s)" in w for w in warnings)


def test_rerun_after_an_interruption_completes(env: Env) -> None:
    n = env.claim()
    worktree = env.worktree(n)
    real_comment = env.fake.create_comment
    calls = {"n": 0}

    def flaky(number: int, body: str) -> Any:
        if HANDOFF_MARKER in body and calls["n"] == 0:
            calls["n"] += 1
            raise GitHubError("injected", status=502)
        return real_comment(number, body)

    env.fake.create_comment = flaky  # type: ignore[method-assign]
    code, _out = env.invoke(worktree, "handoff", "--note-file", str(env.note()))
    assert code == 1
    tip = env.remote_tip(n)
    assert (worktree / ".forge/local/current.json").exists()  # not complete: kept

    code, out = env.invoke(worktree, "handoff", "--note-file", str(env.note()))

    assert code == 0 and out["data"]["recovered"] is True
    assert env.remote_tip(n) == tip
    assert out["data"]["current_cleared"] is True
    assert sum(HANDOFF_MARKER in c.body for c in env.fake.list_comments(n)) == 1


# --------------------------------------------------------------------------- refusals


def test_empty_note_is_exit_1_and_changes_nothing(env: Env) -> None:
    n = env.claim()
    tip = env.remote_tip(n)
    code, out = env.invoke(env.worktree(n), "handoff", "--note-file", str(env.note("\n  \n")))
    assert code == 1
    assert "note is empty" in out["messages"][0]["text"]
    assert env.remote_tip(n) == tip
    assert (env.worktree(n) / ".forge/local/current.json").exists()


def test_missing_note_file_is_exit_1(env: Env) -> None:
    n = env.claim()
    code, out = env.invoke(env.worktree(n), "handoff", "--note-file", str(env.tmp / "nope.md"))
    assert code == 1 and "not found" in out["messages"][0]["text"]


def test_note_file_option_is_required(env: Env) -> None:
    n = env.claim()
    env.profile("alice")
    result = runner.invoke(app, ["--repo", str(env.worktree(n)), "handoff"])
    assert result.exit_code == 1


def test_no_current_task_is_exit_5(env: Env) -> None:
    code, out = env.invoke(env.clone("alice"), "handoff", "--note-file", str(env.note()))
    assert code == 5
    assert "no current task" in out["messages"][0]["text"]


def test_someone_elses_claim_is_exit_5(env: Env) -> None:
    n = env.task("M1-T01")
    assert env.invoke(env.clone("bob"), "next", user="bob")[0] == 0
    alice = env.clone("alice")
    Git(alice).fetch()
    Git(alice).run("checkout", "--quiet", "-b", f"task/{n}", f"origin/task/{n}")
    code, out = env.invoke(alice, "handoff", "--note-file", str(env.note()))
    assert code == 5 and "belongs to bob" in out["messages"][0]["text"]


def test_pre_push_hook_rejection_is_exit_2_with_the_hook_output(env: Env) -> None:
    n = env.claim()
    worktree = env.worktree(n)
    (worktree / "src" / "api").mkdir(parents=True)
    (worktree / "src" / "api" / "a.py").write_text("x\n")
    install_pre_push(worktree, "echo 'typecheck failed' >&2; exit 1")
    tip = env.remote_tip(n)

    code, out = env.invoke(worktree, "handoff", "--note-file", str(env.note()))

    assert code == 2
    assert out["data"]["outcome"] == "push_rejected"
    assert "typecheck failed" in out["data"]["output"]
    assert out["data"]["current_cleared"] is False
    assert any(m["level"] == "error" and "pre-push hook" in m["text"] for m in out["messages"])
    assert env.remote_tip(n) == tip
    assert (worktree / ".forge/local/current.json").exists()
    result = env.human(worktree, "handoff", "--note-file", str(env.note()))
    assert result.exit_code == 2
    assert "Hook output (tail):" in result.stdout and "typecheck failed" in result.stdout


def test_open_pull_request_is_exit_5_and_keeps_everything(env: Env) -> None:
    n = env.claim()
    worktree = env.worktree(n)
    tip = env.remote_tip(n)
    env.fake.fake_set_user("alice")
    pr = env.fake.create_pull("[M1-T01] task", "", f"task/{n}", "main")

    code, out = env.invoke(worktree, "handoff", "--note-file", str(env.note()))

    assert code == 5
    message = out["messages"][0]
    assert f"open pull request (#{pr.number})" in message["text"]
    assert message["hint"] == (
        "the task is in review: address review comments on the PR yourself, or close the PR, "
        "then hand off"
    )
    assert env.remote_tip(n) == tip
    assert (worktree / ".forge/local/current.json").exists()
    assert not any(HANDOFF_MARKER in c.body for c in env.fake.list_comments(n))


def test_rerun_with_unpublished_local_work_is_exit_2_and_keeps_current(env: Env) -> None:
    n = env.claim()
    worktree = env.worktree(n)
    real_comment = env.fake.create_comment
    calls = {"n": 0}

    def flaky(number: int, body: str) -> Any:
        if HANDOFF_MARKER in body and calls["n"] == 0:
            calls["n"] += 1
            raise GitHubError("injected", status=502)
        return real_comment(number, body)

    env.fake.create_comment = flaky  # type: ignore[method-assign]
    assert env.invoke(worktree, "handoff", "--note-file", str(env.note()))[0] == 1
    tip = env.remote_tip(n)
    (worktree / "src" / "api").mkdir(parents=True)
    (worktree / "src" / "api" / "late.py").write_text("late\n")

    code, out = env.invoke(worktree, "handoff", "--note-file", str(env.note()))

    assert code == 2
    data = out["data"]
    assert data["outcome"] == "unpublished_work" and data["recovered"] is True
    assert data["unpublished"] == ["uncommitted change src/api/late.py"]
    assert data["current_cleared"] is False
    assert (worktree / ".forge/local/current.json").exists()
    errors = [m for m in out["messages"] if m["level"] == "error"]
    assert len(errors) == 1 and "src/api/late.py" in errors[0]["text"]
    assert env.remote_tip(n) == tip
    assert sum(HANDOFF_MARKER in c.body for c in env.fake.list_comments(n)) == 1

    result = env.human(worktree, "handoff", "--note-file", str(env.note()))
    assert result.exit_code == 2
    assert "Handed off, but local work of this clone is not part of the handoff" in result.stdout
    assert "uncommitted change src/api/late.py" in result.stdout
    assert "current.json kept" in result.stdout
