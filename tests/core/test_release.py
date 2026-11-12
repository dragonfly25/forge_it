"""Tests for `forge release` (ADR 0015): real temporary repositories plus a FakeGitHub.

Step 1 opens the release pull request on ``release/vX.Y.Z``; the tests merge it on the bare
remote (merge commit or squash) and mark it merged in the fake; step 2 then tags, publishes
and closes. Re-runs, crashes between steps and two leads racing must all converge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.git import Git
from forge.core.github import GitHubError, Issue, PullRequest
from forge.core.output import Level, Result
from forge.core.plan import Roadmap, RoadmapMilestone
from forge.core.refresh import TaskRecord
from forge.core.release import (
    CHANGELOG_HEADER,
    NO_CHANGES_LINE,
    ChangelogEntry,
    check_version,
    collect_entries,
    extract_section,
    insert_section,
    is_forge_release_commit,
    is_lead,
    render_section,
    replace_section,
    section_date,
    section_sha256,
    strip_id_prefix,
    tag_message_milestone,
    version_hint,
    with_marker,
)
from forge.core.tasks import TaskType
from forge.core.team import Member, Team
from support.plan_world import Crash, RecordingGitHub, meta
from support.release_world import TODAY, ReleaseWorld

if TYPE_CHECKING:
    from conftest import GitWorld

TAG_REF = "refs/tags/v1.2.0"
BRANCH_REF = "refs/heads/release/v1.2.0"


@pytest.fixture
def world(git_world: GitWorld) -> ReleaseWorld:
    w = ReleaseWorld(git_world)
    w.task(1, "Login form", TaskType.IMPL)
    w.task(2, "Auth API contract", TaskType.CONTRACT)
    w.task(3, "Bump deps", TaskType.CHORE)
    return w


def texts(result: Result, level: Level | None = None) -> list[str]:
    return [m.text for m in result.messages if level is None or m.level == level]


def remote_refs(world: ReleaseWorld) -> dict[str, str]:
    return world.git_world.remote_git.list_refs("refs/")


# --------------------------------------------------------------------------- pure: section


def _entry(pr: int, type: TaskType, title: str = "t") -> ChangelogEntry:
    return ChangelogEntry(pr=pr, issue=pr - 1, task_id=f"M1-T{pr:02d}", type=type, title=title)


def test_render_section_groups_in_order_and_omits_empty_groups() -> None:
    entries = [
        _entry(9, TaskType.IMPL, "Later feature"),
        _entry(4, TaskType.CHORE, "Chore"),
        _entry(3, TaskType.IMPL, "Early feature"),
        _entry(7, TaskType.CONTRACT, "Contract"),
    ]
    assert render_section("1.2.0", "2026-11-03", entries) == (
        "## [1.2.0] - 2026-11-03\n"
        "\n"
        "### Contracts\n"
        "\n"
        "- Contract (#7)\n"
        "\n"
        "### Features\n"
        "\n"
        "- Early feature (#3)\n"
        "- Later feature (#9)\n"
        "\n"
        "### Chores\n"
        "\n"
        "- Chore (#4)\n"
    )


def test_render_section_without_entries() -> None:
    assert render_section("0.1.0", "d", []) == f"## [0.1.0] - d\n\n{NO_CHANGES_LINE}\n"


def test_strip_id_prefix() -> None:
    assert strip_id_prefix("[M1-T01] Login form", "M1-T01") == "Login form"
    assert strip_id_prefix("[M1-T02] Login", "M1-T01") == "[M1-T02] Login"
    assert strip_id_prefix("Plain title", "M1-T01") == "Plain title"
    assert strip_id_prefix("[M1-T01]", "M1-T01") == "[M1-T01]"


def _issue(number: int, title: str) -> Issue:
    when = datetime(2026, 1, 1, tzinfo=UTC)
    return Issue(number, title, "", "closed", "completed", (), (), "M1", "a", "", when, when, when)


def _pull(number: int, head: str, *, merged: bool = True, base: str = "main") -> PullRequest:
    return PullRequest(
        number, "pr title", "", head, "sha", base, "closed", merged, None, None, False, "a",
        (), (), (), "",
    )  # fmt: skip


def test_collect_entries_filters_and_orders_by_pr_number() -> None:
    records = [
        TaskRecord(_issue(1, "[M1-T01] One"), meta("M1-T01", type=TaskType.TEST)),
        TaskRecord(_issue(2, "[M1-T02] Two"), meta("M1-T02")),
    ]
    pulls = [
        _pull(12, "task/2"),
        _pull(10, "task/1"),
        _pull(11, "task/1", merged=False),  # closed without merging
        _pull(13, "task/1", base="develop"),  # merged elsewhere
        _pull(14, "task/99"),  # not a task of this milestone
        _pull(15, "feature/x"),  # not a task branch
        _pull(16, "task/01"),  # not a canonical task branch name
        _pull(17, "task/2"),  # a second merged PR of the same task
    ]
    entries = collect_entries(records, pulls, "main")
    assert [(e.pr, e.task_id, e.type, e.title) for e in entries] == [
        (10, "M1-T01", TaskType.TEST, "One"),
        (12, "M1-T02", TaskType.IMPL, "Two"),
        (17, "M1-T02", TaskType.IMPL, "Two"),
    ]


# --------------------------------------------------------------------------- pure: changelog

SECTION = "## [1.2.0] - 2026-11-03\n\n### Features\n\n- A (#1)\n"


def test_insert_creates_a_keep_a_changelog_file() -> None:
    text, kept = insert_section(None, SECTION)
    assert text == CHANGELOG_HEADER + "\n" + SECTION and not kept


def test_insert_below_an_empty_unreleased() -> None:
    old = "# Changelog\n\nIntro.\n\n## [Unreleased]\n\n\n## [1.1.0] - 2026-01-01\n\n- old\n"
    text, kept = insert_section(old, SECTION)
    assert not kept
    assert text == (
        "# Changelog\n\nIntro.\n\n## [Unreleased]\n\n"
        + SECTION
        + "\n## [1.1.0] - 2026-01-01\n\n- old\n"
    )


def test_insert_keeps_unreleased_content_in_place() -> None:
    old = "# Changelog\n\n## [Unreleased]\n\n- human note\n\n## [1.1.0]\n"
    text, kept = insert_section(old, SECTION)
    assert kept
    assert text == "# Changelog\n\n## [Unreleased]\n\n- human note\n\n" + SECTION + "\n## [1.1.0]\n"


def test_insert_without_unreleased_goes_below_title_and_intro() -> None:
    old = "# Changelog\n\nAll notable changes.\n\n## [1.1.0] - x\n\n- old\n"
    text, kept = insert_section(old, SECTION)
    assert not kept
    assert (
        text == "# Changelog\n\nAll notable changes.\n\n" + SECTION + "\n## [1.1.0] - x\n\n- old\n"
    )
    assert insert_section("# Changelog\n", SECTION)[0] == "# Changelog\n\n" + SECTION


def test_insert_without_title_goes_on_top() -> None:
    assert insert_section("## [1.1.0]\n", SECTION)[0] == SECTION + "\n## [1.1.0]\n"
    assert insert_section("", SECTION)[0] == SECTION


def test_extract_and_replace_section() -> None:
    text = "# C\n\n## [Unreleased]\n\n" + SECTION + "\n\n## [1.1.0]\n\n- old\n"
    assert extract_section(text, "1.2.0") == SECTION
    assert extract_section(text, "1.1.0") == "## [1.1.0]\n\n- old\n"
    assert extract_section(text, "9.0.0") is None
    assert section_date(SECTION) == "2026-11-03"
    assert section_date("## [1.1.0]\n") is None
    new = "## [1.2.0] - 2026-11-03\n\n### Tests\n\n- B (#2)\n"
    assert replace_section(text, "1.2.0", new) == (
        "# C\n\n## [Unreleased]\n\n" + new + "\n## [1.1.0]\n\n- old\n"
    )
    with pytest.raises(ValueError):
        replace_section(text, "9.0.0", new)


# --------------------------------------------------------------------------- pure: versions


@pytest.mark.parametrize("text", ["1.2.0", "0.1.0", "10.20.30"])
def test_valid_versions(text: str) -> None:
    assert check_version(text) == text


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("v1.2.0", "'v' prefix"),
        ("V1.2.0", "'v' prefix"),
        ("1.2", "exactly X.Y.Z"),
        ("1.2.0.1", "exactly X.Y.Z"),
        ("1.2.0rc1", "exactly X.Y.Z"),
        ("1.2.0.dev1", "exactly X.Y.Z"),
        ("1.2.0.post1", "exactly X.Y.Z"),
        ("1.2.0+local", "exactly X.Y.Z"),
        ("1!1.2.0", "exactly X.Y.Z"),
        ("01.2.0", "write it as 1.2.0"),
        ("banana", "not a version"),
        ("", "not a version"),
    ],
)
def test_invalid_versions(text: str, why: str) -> None:
    with pytest.raises(PreconditionError) as exc:
        check_version(text)
    assert why in exc.value.message
    assert exc.value.code == ExitCode.PRECONDITION


def test_version_hint() -> None:
    assert "0.1.0" in version_hint([])
    hint = version_hint(["v0.9.0", "v1.10.2", "v1.9.9", "v2.0.0rc1", "vX", "1.99.0", "light"])
    assert "latest tag is v1.10.2" in hint
    assert "--version 1.10.3 (next patch)" in hint
    assert "--version 1.11.0 (next minor)" in hint


def test_is_lead() -> None:
    roadmap = Roadmap((RoadmapMilestone("M1", "Editor", lead="Lead"),), Path("plans/roadmap.yml"))
    team = Team(
        (
            Member("boss", "Boss", frozenset({"lead"})),
            Member("dev", "Dev", frozenset({"dev"})),
        )
    )
    assert is_lead("lead", "M1", roadmap, None)
    assert not is_lead("lead", "M2", roadmap, None)
    assert is_lead("BOSS", "M1", None, team)
    assert not is_lead("dev", "M1", roadmap, team)
    assert not is_lead("anyone", "M1", None, None)


# --------------------------------------------------------------------------- checks


def test_open_tasks_exit_5_and_are_listed(world: ReleaseWorld) -> None:
    open_issue, _ = world.task(4, "Unfinished", closed=False, merged=False)
    guard = RecordingGitHub(world.fake, read_only=True)
    before = remote_refs(world)
    result = world.run(github=guard)
    assert result.code == ExitCode.PRECONDITION
    assert [i["number"] for i in result.data["open_tasks"]] == [open_issue]
    assert result.data["open_tasks"][0]["title"] == "[M1-T04] Unfinished"
    assert any(f"#{open_issue}" in t for t in texts(result, Level.ERROR))
    assert remote_refs(world) == before


def test_missing_version_exit_5_with_next_version_hint(world: ReleaseWorld) -> None:
    head = world.lead.rev_parse("HEAD") or ""
    world.lead.run("tag", "v1.1.4", head)
    world.lead.run("tag", "v1.0.9", head)
    world.lead.run("tag", "nightly", head)
    for name in ("v1.1.4", "v1.0.9", "nightly"):
        assert world.lead.push(f"refs/tags/{name}", f"refs/tags/{name}").ok
    world.fake.fake_set_user("mallory")  # a non-lead: the warning must survive the exit 5
    result = world.run(version=None)
    assert result.code == ExitCode.PRECONDITION
    errors = [m for m in result.messages if m.level == Level.ERROR]
    assert [m.text for m in errors] == ["--version is required"]
    hint = errors[0].hint or ""
    assert "1.1.5 (next patch)" in hint and "1.2.0 (next minor)" in hint
    assert result.data["latest_tag"] == "v1.1.4"
    assert any("mallory is not the lead" in t for t in texts(result, Level.WARNING))


def test_invalid_version_exit_5_before_any_git_or_github(world: ReleaseWorld) -> None:
    guard = RecordingGitHub(world.fake, read_only=True)
    with pytest.raises(PreconditionError):
        world.run(version="v1.2.0", github=guard)
    assert guard.calls == []


def test_unknown_milestone_exit_5(world: ReleaseWorld) -> None:
    with pytest.raises(PreconditionError) as exc:
        world.run(milestone="M9")
    assert "M9" in exc.value.message


def test_shallow_clone_exit_5(world: ReleaseWorld, tmp_path: Path) -> None:
    world.main_commit("x.txt", "x\n")
    shallow = tmp_path / "shallow"
    Git(tmp_path).run(
        "clone", "--quiet", "--depth", "1", f"file://{world.git_world.remote}", str(shallow)
    )
    with pytest.raises(PreconditionError) as exc:
        world.run(git=Git(shallow))
    assert "shallow" in exc.value.message


def test_non_lead_gets_a_warning_and_continues(world: ReleaseWorld) -> None:
    world.fake.fake_set_user("mallory")
    result = world.run()
    assert result.code == ExitCode.OK
    assert result.data["lead"] is False
    assert any("mallory is not the lead of M1" in t for t in texts(result, Level.WARNING))


def test_lead_from_team_gets_no_warning(world: ReleaseWorld) -> None:
    world.fake.fake_set_user("boss")
    team = Team((Member("boss", "Boss", frozenset({"lead"})),))
    result = world.run(roadmap=None, team=team)
    assert result.data["lead"] is True
    assert not texts(result, Level.WARNING)


# --------------------------------------------------------------------------- step 1


def test_step1_opens_the_release_pr_and_never_touches_main(world: ReleaseWorld) -> None:
    main_before = world.remote_sha("refs/heads/main")
    head_before = world.lead.rev_parse("HEAD")
    result = world.run()
    assert result.code == ExitCode.OK, result.messages
    data = result.data
    assert data["step"] == "pr_opened" and "would" not in data
    expected = (
        f"## [1.2.0] - {TODAY}\n\n"
        "### Contracts\n\n- Auth API contract (#4)\n\n"
        "### Features\n\n- Login form (#2)\n\n"
        "### Chores\n\n- Bump deps (#6)\n"
    )
    assert data["section"] == expected
    assert [e["pr"] for e in data["entries"]] == [2, 4, 6]

    # main is untouched; the release branch is main + one commit adding the changelog.
    assert world.remote_sha("refs/heads/main") == main_before
    tip = world.remote_sha(BRANCH_REF)
    assert tip == data["commit"]
    remote = world.git_world.remote_git
    assert remote.rev_parse(f"{tip}^1") == main_before
    assert remote.diff_names(main_before or "", tip or "") == ["CHANGELOG.md"]
    assert world.remote_file(BRANCH_REF) == CHANGELOG_HEADER + "\n" + expected
    assert world.remote_sha(TAG_REF) is None
    # The lead's clone is untouched: same HEAD, no CHANGELOG in the working tree.
    assert world.lead.rev_parse("HEAD") == head_before
    assert not (world.lead.cwd / "CHANGELOG.md").exists()

    pulls = world.fake.list_pulls(state="open", head="release/v1.2.0")
    assert len(pulls) == 1
    pr = pulls[0]
    assert (pr.title, pr.body, pr.base) == (
        "chore(release): v1.2.0",
        expected + "\n<!-- forge:release-notes milestone=M1 -->\n",
        "main",
    )
    commit = remote.commits(f"{tip}^!")[0]
    assert commit.trailers == {
        "Forge-Release-Notes": "v1.2.0",
        "Forge-Milestone": "M1",
        "Forge-Section-Sha256": section_sha256(expected),
    }
    assert is_forge_release_commit(remote, tip or "", "1.2.0")
    assert data["pull"] == {"number": pr.number, "url": pr.url, "created": True, "updated": False}
    assert world.fake.list_milestones()[0].state == "open"
    assert world.fake.get_release_by_tag("v1.2.0") is None


def test_step1_rerun_is_idempotent(world: ReleaseWorld) -> None:
    world.run()
    refs = remote_refs(world)
    guard = RecordingGitHub(world.fake, read_only=True)
    later = datetime(2026, 11, 5, tzinfo=UTC)
    result = world.run(github=guard, clock=lambda: later)
    assert result.code == ExitCode.OK
    assert result.data["commit"] is None
    assert result.data["pull"]["created"] is False and result.data["pull"]["updated"] is False
    assert remote_refs(world) == refs
    assert guard.writes == []
    # The section keeps the date of the first run.
    assert result.data["section"].startswith(f"## [1.2.0] - {TODAY}\n")


def test_step1_rerun_regenerates_a_changed_section_with_a_lease(world: ReleaseWorld) -> None:
    world.run()
    old_tip = world.remote_sha(BRANCH_REF)
    _, pr4 = world.task(4, "Coverage", TaskType.TEST)  # merged after the first run
    result = world.run(clock=lambda: datetime(2026, 11, 9, tzinfo=UTC))
    assert result.code == ExitCode.OK
    new_tip = world.remote_sha(BRANCH_REF)
    assert new_tip != old_tip
    # Rebuilt as one commit on main (force-with-lease on the old tip), never stacked.
    remote = world.git_world.remote_git
    assert remote.rev_parse(f"{new_tip}^1") == world.remote_sha("refs/heads/main")
    assert not remote.is_ancestor(old_tip or "", new_tip or "")
    assert len(remote.commits(f"refs/heads/main..{BRANCH_REF}")) == 1
    section = result.data["section"]
    assert section.startswith(f"## [1.2.0] - {TODAY}\n")  # date kept
    assert f"### Tests\n\n- Coverage (#{pr4})\n" in section
    changelog = world.remote_file(BRANCH_REF) or ""
    assert changelog.count("## [1.2.0]") == 1 and extract_section(changelog, "1.2.0") == section
    pr = world.fake.list_pulls(state="open", head="release/v1.2.0")[0]
    assert pr.body == with_marker(section, "M1") and result.data["pull"]["updated"] is True


def test_step1_preserves_existing_changelog_and_crlf(world: ReleaseWorld) -> None:
    world.main_commit(
        "CHANGELOG.md",
        "# Changelog\r\n\r\n## [Unreleased]\r\n\r\n## [1.1.0] - 2026-01-01\r\n\r\n- old\r\n",
        "docs: changelog",
    )
    result = world.run()
    assert result.code == ExitCode.OK
    raw = world.git_world.remote_git.read_file(BRANCH_REF, "CHANGELOG.md") or b""
    assert b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode().replace("\r\n", "\n")
    assert text.startswith("# Changelog\n\n## [Unreleased]\n\n## [1.2.0] - ")
    assert text.endswith("\n## [1.1.0] - 2026-01-01\n\n- old\n")


def test_step1_warns_when_unreleased_has_content(world: ReleaseWorld) -> None:
    world.main_commit("CHANGELOG.md", "# Changelog\n\n## [Unreleased]\n\n- by hand\n", "docs")
    result = world.run()
    assert any("[Unreleased]" in t for t in texts(result, Level.WARNING))
    text = world.remote_file(BRANCH_REF) or ""
    assert text.index("- by hand") < text.index("## [1.2.0]")


def test_step1_crash_after_push_then_rerun_opens_the_pr(world: ReleaseWorld) -> None:
    crashing = RecordingGitHub(world.fake, crash_before=1)  # dies before create_pull
    with pytest.raises(Crash):
        world.run(github=crashing)
    tip = world.remote_sha(BRANCH_REF)
    assert tip is not None and not world.fake.list_pulls(state="open")
    result = world.run()
    assert result.data["commit"] is None  # nothing pushed again
    assert world.remote_sha(BRANCH_REF) == tip
    assert result.data["pull"]["created"] is True
    assert len(world.fake.list_pulls(state="open", head="release/v1.2.0")) == 1


def test_step1_existing_tag_without_section_exit_5(world: ReleaseWorld) -> None:
    head = world.lead.rev_parse("HEAD") or ""
    world.lead.create_annotated_tag("v1.2.0", head, "stray")
    assert world.lead.push_tag("v1.2.0").ok
    with pytest.raises(PreconditionError) as exc:
        world.run()
    assert "tag v1.2.0 already exists" in exc.value.message
    assert world.remote_sha(BRANCH_REF) is None


def test_step1_pre_push_hook_rejection_exit_2(world: ReleaseWorld) -> None:
    hook = world.lead.cwd / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho no >&2\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(ForgeError) as exc:
        world.run()
    assert exc.value.code == ExitCode.AGENT_ACTION
    assert "pre-push hook" in exc.value.message
    assert world.remote_sha(BRANCH_REF) is None
    assert not world.fake.list_pulls(state="open")


def test_step1_two_leads_race_converge_on_one_branch_and_one_pr(world: ReleaseWorld) -> None:
    """Bob's run fetches, then Alice's whole run lands before Bob pushes: Bob's lease
    (branch absent) is rejected, he re-fetches, finds the same section and reuses her PR."""
    bob = world.clone("bob")
    alice_results: list[Result] = []

    def alice_runs_first(src: str, dst: str) -> None:
        bob.before_push = None
        alice_results.append(world.run(git=world.lead))

    bob.before_push = alice_runs_first
    result = world.run(git=bob)
    assert alice_results[0].code == ExitCode.OK and alice_results[0].data["pull"]["created"]
    assert result.code == ExitCode.OK
    assert result.data["commit"] is None  # Bob pushed nothing
    assert [p[2] for p in bob.pushes] == [False]  # one attempt, rejected (hooks run)
    assert any("changed on origin while pushing" in t for t in texts(result))
    assert world.remote_sha(BRANCH_REF) == alice_results[0].data["commit"]
    pulls = world.fake.list_pulls(state="open", head="release/v1.2.0")
    assert len(pulls) == 1 and result.data["pull"]["number"] == pulls[0].number


def test_step1_concurrent_pr_creation_reuses_the_winner(world: ReleaseWorld) -> None:
    """Both runs saw no PR; the other one creates it first, ours gets 422 and reuses it."""
    racing = RecordingGitHub(world.fake)

    def other_creates() -> None:
        world.fake.create_pull("chore(release): v1.2.0", "other body", "release/v1.2.0", "main")

    racing.before["create_pull"] = other_creates
    result = world.run(github=racing)
    assert result.code == ExitCode.OK
    pulls = world.fake.list_pulls(state="open", head="release/v1.2.0")
    assert len(pulls) == 1
    assert result.data["pull"]["number"] == pulls[0].number
    assert pulls[0].body == with_marker(result.data["section"], "M1")  # regenerated on reuse


def test_step1_dry_run_writes_nothing(world: ReleaseWorld) -> None:
    guard = RecordingGitHub(world.fake, read_only=True)
    refs = remote_refs(world)
    objects = world.lead.run("count-objects").stdout
    result = world.run(github=guard, dry_run=True)
    assert result.code == ExitCode.OK
    assert result.data["step"] == "dry_run" and result.data["would"] == "pr_opened"
    assert result.data["section"].startswith("## [1.2.0] - ")
    assert result.data["pull"] is None and result.data["commit"] is None
    assert remote_refs(world) == refs and guard.writes == []
    assert world.lead.run("count-objects").stdout == objects  # not even a dangling commit
    assert world.lead.tag_target("v1.2.0") is None


# --------------------------------------------------------------------------- step 2


@pytest.mark.parametrize("method", ["merge", "squash", "rebase"])
def test_step2_tags_the_commit_that_landed_the_section(world: ReleaseWorld, method: Any) -> None:
    step1 = world.run()
    merged = world.merge_release(method=method)
    world.main_commit("after.txt", "later work\n")  # main moved on after the merge
    result = world.run()
    assert result.code == ExitCode.OK, result.messages
    data = result.data
    assert data["step"] == "released"
    assert data["commit"] == merged != step1.data["commit"]
    remote = world.git_world.remote_git
    assert remote.rev_parse(TAG_REF) == merged
    assert remote.run("cat-file", "-t", "refs/tags/v1.2.0").stdout.strip() == "tag"  # annotated
    assert data["tag_pushed"] is True
    release = world.fake.get_release_by_tag("v1.2.0")
    assert release is not None
    assert release.name == "v1.2.0 — Editor"
    assert release.body == with_marker(step1.data["section"], "M1")
    assert step1.data["section"] == data["section"]
    assert data["target_source"] == f"pull request #{step1.data['pull']['number']}"
    message = remote.run("cat-file", "tag", "refs/tags/v1.2.0").stdout
    assert message.endswith("\n\nv1.2.0 (M1)\n")
    assert data["release"] == {"name": release.name, "url": release.url, "created": True}
    assert world.fake.list_milestones()[0].state == "closed"
    assert data["milestone_closed"] is True
    assert remote.rev_parse("refs/heads/main") != merged  # main itself was not moved by us


def test_step2_rerun_is_idempotent(world: ReleaseWorld) -> None:
    world.run()
    world.merge_release()
    world.run()
    refs = remote_refs(world)
    guard = RecordingGitHub(world.fake, read_only=True)
    result = world.run(github=guard)
    assert result.code == ExitCode.OK
    assert result.data["tag_pushed"] is False
    assert result.data["release"]["created"] is False
    assert guard.writes == [] and remote_refs(world) == refs


def test_step2_crash_after_tag_push_then_rerun_completes(world: ReleaseWorld) -> None:
    world.run()
    merged = world.merge_release()
    crashing = RecordingGitHub(world.fake, crash_before=1)  # dies before create_release
    with pytest.raises(Crash):
        world.run(github=crashing)
    assert world.remote_sha(TAG_REF) == merged
    assert world.fake.get_release_by_tag("v1.2.0") is None
    tag_object = world.git_world.remote_git.run("rev-parse", TAG_REF).stdout.strip()
    result = world.run()
    assert result.code == ExitCode.OK
    assert result.data["tag_pushed"] is False and result.data["release"]["created"] is True
    assert world.git_world.remote_git.run("rev-parse", TAG_REF).stdout.strip() == tag_object
    assert world.fake.list_milestones()[0].state == "closed"


def test_step2_crash_after_release_creation_then_rerun_completes(world: ReleaseWorld) -> None:
    world.run()
    world.merge_release()
    crashing = RecordingGitHub(world.fake, crash_after=1)  # release created, response lost
    with pytest.raises(Crash):
        world.run(github=crashing)
    assert world.fake.get_release_by_tag("v1.2.0") is not None
    assert world.fake.list_milestones()[0].state == "open"
    recorder = RecordingGitHub(world.fake)
    result = world.run(github=recorder)
    assert result.code == ExitCode.OK
    assert recorder.writes == ["close_milestone"]  # no second release
    assert world.fake.list_milestones()[0].state == "closed"


def test_step2_crash_after_local_tag_before_push(world: ReleaseWorld) -> None:
    """The local tag exists (same commit) but was never pushed: a re-run pushes it."""
    world.run()
    merged = world.merge_release()
    world.lead.fetch()
    world.lead.create_annotated_tag("v1.2.0", merged, "v1.2.0 (M1)")  # made before a crash
    result = world.run()
    assert result.code == ExitCode.OK and result.data["tag_pushed"] is True
    assert world.remote_sha(TAG_REF) == merged


def test_step2_remote_tag_on_another_commit_exit_5(world: ReleaseWorld) -> None:
    world.run()
    world.merge_release()
    other = world.git_world.clone("carol")
    first = other.rev_parse("HEAD~1") or ""
    other.create_annotated_tag("v1.2.0", first, "wrong")
    assert other.push_tag("v1.2.0").ok
    with pytest.raises(PreconditionError) as exc:
        world.run()
    assert "tag v1.2.0 exists on origin" in exc.value.message
    assert world.fake.get_release_by_tag("v1.2.0") is None
    assert world.fake.list_milestones()[0].state == "open"


def test_step2_local_tag_on_another_commit_exit_5(world: ReleaseWorld) -> None:
    world.run()
    world.merge_release()
    world.lead.create_annotated_tag("v1.2.0", world.git_world.initial_sha, "stale local tag")
    guard = RecordingGitHub(world.fake, read_only=True)
    with pytest.raises(PreconditionError) as exc:
        world.run(github=guard)
    assert "tag v1.2.0 exists locally" in exc.value.message
    assert exc.value.hint is not None and "git tag -d v1.2.0" in exc.value.hint
    assert world.remote_sha(TAG_REF) is None and guard.writes == []


def _push_tag_from(world: ReleaseWorld, user: str, commit: str) -> None:
    other = world.clone(user)
    other.fetch()
    other.create_annotated_tag("v1.2.0", commit, f"{user}'s tag")
    assert other.push_tag("v1.2.0").ok


def test_step2_tag_race_same_commit_converges(world: ReleaseWorld) -> None:
    """Another lead pushes the same tag (same commit) between our check and our push."""
    world.run()
    merged = world.merge_release()
    world.lead.before_push = lambda src, dst: _push_tag_from(world, "carol", merged)
    result = world.run()
    assert result.code == ExitCode.OK
    assert result.data["tag_pushed"] is False
    assert any("pushed concurrently" in t for t in texts(result))
    assert world.remote_sha(TAG_REF) == merged
    assert world.fake.get_release_by_tag("v1.2.0") is not None
    assert world.fake.list_milestones()[0].state == "closed"


def test_step2_tag_race_other_commit_exit_5(world: ReleaseWorld) -> None:
    """The racing tag points elsewhere: exactly one tag wins and we stop with exit 5."""
    world.run()
    merged = world.merge_release()
    initial = world.git_world.initial_sha
    world.lead.before_push = lambda src, dst: _push_tag_from(world, "carol", initial)
    with pytest.raises(PreconditionError) as exc:
        world.run()
    assert "not on" in exc.value.message
    assert world.remote_sha(TAG_REF) == initial != merged
    assert world.fake.get_release_by_tag("v1.2.0") is None


def test_step2_dry_run_writes_nothing(world: ReleaseWorld) -> None:
    world.run()
    merged = world.merge_release()
    refs = remote_refs(world)
    guard = RecordingGitHub(world.fake, read_only=True)
    result = world.run(github=guard, dry_run=True)
    assert result.code == ExitCode.OK
    assert result.data["step"] == "dry_run" and result.data["would"] == "released"
    assert result.data["commit"] == merged
    assert result.data["release"] is None and result.data["tag_pushed"] is False
    assert any("would tag" in t for t in texts(result))
    assert any("would close milestone M1" in t for t in texts(result))
    assert guard.writes == [] and remote_refs(world) == refs
    assert world.lead.tag_target("v1.2.0") is None


def test_step2_draft_release_blocks_with_a_hint(world: ReleaseWorld) -> None:
    world.run()
    world.merge_release()
    world.fake.create_release("v1.2.0", "draft", "")
    world.fake.fake_set_release_draft("v1.2.0", True)
    with pytest.raises(ForgeError) as exc:
        world.run()
    assert exc.value.hint is not None and "draft release" in exc.value.hint
    assert world.fake.list_milestones()[0].state == "open"
    world.fake.fake_set_release_draft("v1.2.0", False)  # the lead publishes it by hand
    result = world.run()
    assert result.code == ExitCode.OK and result.data["release"]["created"] is False
    assert world.fake.list_milestones()[0].state == "closed"


def test_step2_release_create_failure_then_rerun(world: ReleaseWorld) -> None:
    world.run()
    world.merge_release()
    failing = RecordingGitHub(world.fake)
    failing.fail["create_release"] = 1
    with pytest.raises(GitHubError):
        world.run(github=failing)
    result = world.run()
    assert result.code == ExitCode.OK and result.data["release"]["created"] is True


def test_step2_warns_about_a_still_open_release_pr(world: ReleaseWorld) -> None:
    world.run()
    world.main_commit("CHANGELOG.md", "# Changelog\n\n## [1.2.0] - x\n", "hand-written section")
    result = world.run()
    assert result.code == ExitCode.OK and result.data["step"] == "released"
    assert any("still open" in t for t in texts(result, Level.WARNING))


def test_released_milestone_already_closed_is_fine(world: ReleaseWorld) -> None:
    world.run()
    world.merge_release()
    world.fake.close_milestone(world.milestone.number)
    recorder = RecordingGitHub(world.fake)
    result = world.run(github=recorder)
    assert result.code == ExitCode.OK
    assert "close_milestone" not in recorder.writes and result.data["milestone_closed"] is True


# --------------------------------------------------------------------------- review fixes


def test_rebase_merge_of_a_two_commit_branch_tags_the_last_commit(world: ReleaseWorld) -> None:
    """A hand-edited branch has two commits; a rebase-merge puts both on main's first-parent
    chain. The merged PR's merge_commit_sha (the last one) is tagged, not the first commit
    that carried the heading (which lacks the hand-written line)."""
    world.run()
    world.hand_edit_release_branch()
    tip = world.merge_release(method="rebase")
    remote = world.git_world.remote_git
    first = remote.rev_parse(f"{tip}~1") or ""
    assert remote.first_parent_line_origin(tip, "CHANGELOG.md", "## [1.2.0]") == first
    result = world.run()
    assert result.code == ExitCode.OK
    assert result.data["commit"] == tip != first
    assert remote.rev_parse(TAG_REF) == tip
    assert "- By hand" in result.data["section"]


def test_manual_merge_falls_back_to_the_first_parent_walk(world: ReleaseWorld) -> None:
    world.run()
    squashed = world.merge_release(method="squash", mark_merged=False)
    pr = world.fake.list_pulls(state="open", head="release/v1.2.0")[0]
    world.fake.fake_close_pull(pr.number)  # closed by hand after merging on the command line
    world.main_commit("later.txt", "x\n")
    result = world.run()
    assert result.code == ExitCode.OK
    assert result.data["target_source"] == "first-parent history"
    assert world.remote_sha(TAG_REF) == squashed
    # The PR closed unmerged is not still open: no "still open" warning.
    assert not any("still open" in t for t in texts(result, Level.WARNING))


def test_merged_release_pr_commit_not_on_main_exit_5(world: ReleaseWorld) -> None:
    world.run()
    world.merge_release(mark_merged=False)
    pr = world.fake.list_pulls(state="open", head="release/v1.2.0")[0]
    world.fake.fake_merge_pull(pr.number)  # synthetic merge_commit_sha, not on main
    guard = RecordingGitHub(world.fake, read_only=True)
    with pytest.raises(PreconditionError) as exc:
        world.run(github=guard)
    assert f"#{pr.number} was merged as" in exc.value.message
    assert world.remote_sha(TAG_REF) is None and guard.writes == []


def test_version_already_released_for_another_milestone_exit_5(world: ReleaseWorld) -> None:
    world.run()
    world.merge_release()
    assert world.run().data["step"] == "released"
    world.fake.create_milestone("M2")
    world.task(1, "Second milestone work", milestone="M2")
    guard = RecordingGitHub(world.fake, read_only=True)
    refs = remote_refs(world)
    with pytest.raises(PreconditionError) as exc:
        world.run(milestone="M2", github=guard)
    assert "version 1.2.0 was already released for M1" in exc.value.message
    assert exc.value.hint is not None and "another --version" in exc.value.hint
    assert guard.writes == [] and remote_refs(world) == refs
    assert [m.state for m in world.fake.list_milestones()] == ["closed", "open"]


@pytest.mark.parametrize("evidence", ["tag", "release"])
def test_version_owned_by_another_milestone_without_merged_pr(
    world: ReleaseWorld, evidence: str
) -> None:
    """The section landed by hand (no merged release PR); the tag message or the release
    marker still names M1, so releasing M2 with the same version stops before any write."""
    world.main_commit("CHANGELOG.md", "# Changelog\n\n## [1.2.0] - x\n\n- M1 work\n", "docs")
    if evidence == "tag":
        head = world.remote_sha("refs/heads/main") or ""
        other = world.clone("carol")
        other.fetch()
        other.create_annotated_tag("v1.2.0", head, "v1.2.0 (M1)")
        assert other.push_tag("v1.2.0").ok
    else:
        world.fake.create_release("v1.2.0", "v1.2.0 — Editor", with_marker("notes", "M1"))
    world.fake.create_milestone("M2")
    world.task(1, "Other", milestone="M2")
    guard = RecordingGitHub(world.fake, read_only=True)
    with pytest.raises(PreconditionError) as exc:
        world.run(milestone="M2", github=guard)
    assert "already released for M1" in exc.value.message
    assert guard.writes == []


def test_version_being_released_for_another_milestone_exit_5(world: ReleaseWorld) -> None:
    world.run()  # M1's release PR is open on release/v1.2.0
    world.fake.create_milestone("M2")
    world.task(1, "Other", milestone="M2")
    tip = world.remote_sha(BRANCH_REF)
    guard = RecordingGitHub(world.fake, read_only=True)
    with pytest.raises(PreconditionError) as exc:
        world.run(milestone="M2", github=guard)
    assert "version 1.2.0 is being released for M1" in exc.value.message
    assert world.remote_sha(BRANCH_REF) == tip and guard.writes == []


def test_branch_trailer_names_the_owner_even_without_a_pr(world: ReleaseWorld) -> None:
    crashing = RecordingGitHub(world.fake, crash_before=1)  # branch pushed, PR never opened
    with pytest.raises(Crash):
        world.run(github=crashing)
    world.fake.create_milestone("M2")
    world.task(1, "Other", milestone="M2")
    with pytest.raises(PreconditionError) as exc:
        world.run(milestone="M2")
    assert "is being released for M1 (branch release/v1.2.0)" in exc.value.message


def test_hand_edited_branch_is_not_regenerated(world: ReleaseWorld) -> None:
    world.run()
    edited = world.hand_edit_release_branch()
    pr = world.fake.list_pulls(state="open", head="release/v1.2.0")[0]
    body = pr.body
    _, pr_new = world.task(4, "Late feature", TaskType.IMPL)  # merged after the edit
    recorder = RecordingGitHub(world.fake)
    result = world.run(github=recorder)
    assert result.code == ExitCode.OK
    assert world.remote_sha(BRANCH_REF) == edited  # kept
    assert result.data["hand_edited"] is True and result.data["commit"] is None
    assert [m["pr"] for m in result.data["missing"]] == [pr_new]
    warning = next(t for t in texts(result, Level.WARNING) if "changed by hand" in t)
    assert f"#{pr_new} (M1-T04)" in warning
    assert recorder.writes == []  # the PR body is not overwritten either
    assert world.fake.get_pull(pr.number).body == body


def test_hand_edited_branch_without_missing_prs_warns_only(world: ReleaseWorld) -> None:
    world.run()
    world.hand_edit_release_branch()
    result = world.run()
    assert result.data["missing"] == []
    assert any("changed by hand" in t for t in texts(result, Level.WARNING))


def test_version_not_newer_than_latest_tag_warns(world: ReleaseWorld) -> None:
    head = world.lead.rev_parse("HEAD") or ""
    world.lead.run("tag", "v2.0.0", head)
    assert world.lead.push("refs/tags/v2.0.0", "refs/tags/v2.0.0").ok
    result = world.run()
    assert result.code == ExitCode.OK
    assert any("not newer than the latest tag v2.0.0" in t for t in texts(result, Level.WARNING))
    newer = world.run(version="2.1.0")
    assert not any("not newer" in t for t in texts(newer, Level.WARNING))


def test_tag_message_milestone() -> None:
    assert tag_message_milestone("v1.2.0 (M1)\n") == "M1"
    assert tag_message_milestone("v1.2.0 (M1)\n-----BEGIN PGP SIGNATURE-----\n") == "M1"
    assert tag_message_milestone("Release 1.2.0") is None
    assert tag_message_milestone(None) is None


def test_regeneration_race_two_leads_converge(world: ReleaseWorld) -> None:
    """Both leads regenerate after a new merge; Alice's force-with-lease lands first, Bob's
    lease on the old tip is rejected, he re-fetches and finds the same section."""
    world.run()
    world.task(4, "Late", TaskType.TEST)
    bob = world.clone("bob")
    alice: list[Result] = []

    def alice_first(src: str, dst: str) -> None:
        bob.before_push = None
        alice.append(world.run(git=world.lead))

    bob.before_push = alice_first
    result = world.run(git=bob)
    assert alice[0].data["commit"] is not None
    assert result.code == ExitCode.OK and result.data["commit"] is None
    assert world.remote_sha(BRANCH_REF) == alice[0].data["commit"]
    remote = world.git_world.remote_git
    assert len(remote.commits(f"refs/heads/main..{BRANCH_REF}")) == 1
    assert len(world.fake.list_pulls(state="open", head="release/v1.2.0")) == 1


def test_hand_edit_racing_a_regeneration_is_never_overwritten(world: ReleaseWorld) -> None:
    """A human pushes to the release branch between our fetch and our push: the lease on the
    fetched tip rejects our push, and the retry sees a hand-edited branch and keeps it."""
    world.run()
    world.task(4, "Late", TaskType.TEST)
    edited: list[str] = []

    def human_pushes(src: str, dst: str) -> None:
        world.lead.before_push = None
        edited.append(world.hand_edit_release_branch())

    world.lead.before_push = human_pushes
    result = world.run()
    assert result.code == ExitCode.OK
    assert world.remote_sha(BRANCH_REF) == edited[0]
    assert result.data["hand_edited"] is True and result.data["commit"] is None
    assert any("changed on origin while pushing" in t for t in texts(result))


# --------------------------------------------------------------------------- second review


@pytest.mark.parametrize(
    ("section_line", "new_file"),
    [("- Amended by hand (#123)\n", None), (None, "extra.txt")],
    ids=["amended-section", "added-file"],
)
def test_amended_forge_commit_counts_as_a_hand_edit(
    world: ReleaseWorld, section_line: str | None, new_file: str | None
) -> None:
    world.run()
    amended = world.amend_release_branch(section_line=section_line, new_file=new_file)
    remote = world.git_world.remote_git
    assert remote.commits(f"{amended}^!")[0].trailers["Forge-Release-Notes"] == "v1.2.0"
    assert not is_forge_release_commit(remote, amended, "1.2.0")
    _, late = world.task(4, "Late", TaskType.TEST)  # would trigger a regeneration
    recorder = RecordingGitHub(world.fake)
    result = world.run(github=recorder)
    assert result.code == ExitCode.OK
    assert world.remote_sha(BRANCH_REF) == amended  # never force-pushed over
    assert result.data["hand_edited"] is True and result.data["commit"] is None
    assert [m["pr"] for m in result.data["missing"]] == [late]
    assert recorder.writes == []


def test_recovery_by_tagging_the_right_commit_by_hand(world: ReleaseWorld) -> None:
    """The merged PR's commit is not on main (exit 5); the user follows the hint and tags
    the right commit; the re-run accepts that tag, publishes and closes."""
    world.run()
    merged_tip = world.merge_release(mark_merged=False)
    pr = world.fake.list_pulls(state="open", head="release/v1.2.0")[0]
    world.fake.fake_merge_pull(pr.number)  # GitHub reports a commit that main does not have
    with pytest.raises(PreconditionError) as exc:
        world.run()
    hint = exc.value.hint or ""
    assert '`git tag -a v1.2.0 -m "v1.2.0 (M1)" <commit>`' in hint
    assert "`git push origin v1.2.0`" in hint and "&&" not in hint  # PowerShell 5.1 has no &&
    user = world.clone("carol")
    user.fetch()
    user.run("tag", "-a", "v1.2.0", "-m", "v1.2.0 (M1)", merged_tip)
    user.run("push", "--quiet", "origin", "v1.2.0")
    result = world.run()
    assert result.code == ExitCode.OK and result.data["step"] == "released"
    assert result.data["target_source"] == "tag v1.2.0"
    assert result.data["commit"] == merged_tip and result.data["tag_pushed"] is False
    assert world.fake.get_release_by_tag("v1.2.0") is not None
    assert world.fake.list_milestones()[0].state == "closed"


def test_hand_tag_for_another_milestone_is_not_accepted_as_recovery(world: ReleaseWorld) -> None:
    world.run()
    merged_tip = world.merge_release(mark_merged=False)
    pr = world.fake.list_pulls(state="open", head="release/v1.2.0")[0]
    world.fake.fake_merge_pull(pr.number)
    user = world.clone("carol")
    user.fetch()
    user.run("tag", "-a", "v1.2.0", "-m", "v1.2.0 (M7)", merged_tip)
    user.run("push", "--quiet", "origin", "v1.2.0")
    with pytest.raises(PreconditionError) as exc:
        world.run()
    assert "already released for M7" in exc.value.message


def test_release_pr_merged_after_the_fetch_goes_to_step_2(world: ReleaseWorld) -> None:
    world.run()
    racing = RecordingGitHub(world.fake)
    racing.before["list_pulls"] = lambda: world.merge_release() and None
    result = world.run(github=racing)
    assert result.code == ExitCode.OK and result.data["step"] == "released"
    assert world.remote_sha(TAG_REF) is not None
    assert len(world.fake.list_pulls(state="all", head="release/v1.2.0")) == 1  # no second PR


def test_merged_release_pr_without_section_on_main_exit_5(world: ReleaseWorld) -> None:
    world.run()
    pr = world.fake.list_pulls(state="open", head="release/v1.2.0")[0]
    world.fake.fake_merge_pull(pr.number)  # merged, then reverted: main never has the section
    guard = RecordingGitHub(world.fake, read_only=True)
    with pytest.raises(PreconditionError) as exc:
        world.run(github=guard)
    assert f"#{pr.number} for v1.2.0 is merged" in exc.value.message
    assert (
        guard.writes == [] and len(world.fake.list_pulls(state="all", head="release/v1.2.0")) == 1
    )


@pytest.mark.parametrize("milestone", ["M 1", "Milestone 1", "m1", "M1 ", ""])
def test_invalid_milestone_id_exit_5(world: ReleaseWorld, milestone: str) -> None:
    guard = RecordingGitHub(world.fake, read_only=True)
    with pytest.raises(PreconditionError) as exc:
        world.run(milestone=milestone, github=guard)
    assert "invalid milestone id" in exc.value.message
    assert guard.calls == []


def test_hand_edited_branch_of_another_milestone_exit_5(world: ReleaseWorld) -> None:
    """M1's Forge commit is under a human commit and M1's PR was closed: the branch's
    Forge-Milestone trailers (base..tip) still forbid M2 from reusing it."""
    world.run()
    pr = world.fake.list_pulls(state="open", head="release/v1.2.0")[0]
    world.fake.fake_close_pull(pr.number)
    edited = world.hand_edit_release_branch()
    world.fake.create_milestone("M2")
    world.task(1, "Other", milestone="M2")
    with pytest.raises(PreconditionError) as exc:
        world.run(milestone="M2")
    assert "is being released for M1 (branch release/v1.2.0)" in exc.value.message
    assert world.remote_sha(BRANCH_REF) == edited


@pytest.mark.parametrize(("kind", "why"), [("light", "lightweight"), ("M2", "names M2")])
def test_local_tag_on_the_right_commit_must_be_forge_annotated(
    world: ReleaseWorld, kind: str, why: str
) -> None:
    world.run()
    merged = world.merge_release()
    world.lead.fetch()
    if kind == "light":
        world.lead.run("tag", "v1.2.0", merged)
    else:
        world.lead.create_annotated_tag("v1.2.0", merged, "v1.2.0 (M2)")
    with pytest.raises(PreconditionError) as exc:
        world.run()
    assert why in exc.value.message
    assert exc.value.hint is not None and "git tag -d v1.2.0" in exc.value.hint
    assert world.remote_sha(TAG_REF) is None
