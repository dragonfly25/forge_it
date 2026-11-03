"""Tests for forge.core.review (spec §11.2, ADR 0012) on real temporary repositories.

Alice opens pull requests from her clone; Bob builds the review context from his. The bare
remote gets ``refs/pull/<n>/head`` as GitHub would create it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.github import CiStatus, GitHubError, PullRequest
from forge.core.profile import AgentProfile
from forge.core.review import (
    DIFF_CAP_BYTES,
    REVIEW_REF_PREFIX,
    ReviewContext,
    cap_diff,
    check_reviewer_tier,
    linked_task_number,
    review_context,
    review_footer,
    submit_review,
)
from forge.core.tiers import Tier
from support.review_world import ZONES, ReviewWorld, review_config
from support.scenarios import AGENT

if TYPE_CHECKING:
    from conftest import GitWorld

STANDARD_ONLY = AgentProfile(
    id="mid",
    tool="codex",
    providers=("openai", "local"),
    tiers={Tier.STANDARD: "gpt-x", Tier.LIGHT: "gpt-mini"},
)


@pytest.fixture
def world(git_world: GitWorld) -> ReviewWorld:
    return ReviewWorld(git_world)


def make_ctx(
    world: ReviewWorld,
    *,
    agent: AgentProfile | None = AGENT,
    min_tier_for: dict[str, str] | None = None,
    github: Any = None,
    zones: Any = ZONES,
) -> ReviewContext:
    world.fake.fake_set_user("bob")
    return ReviewContext(
        config=review_config(min_tier_for),
        git=world.bob,
        github=github if github is not None else world.fake,
        repo_root=world.bob.cwd,
        zones=zones,
        agent=agent,
    )


def warnings_of(result: Any) -> list[str]:
    return [m.text for m in result.messages if m.level == "warning"]


# --------------------------------------------------------------------------- linking


def test_linked_task_by_head_branch(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"}, body="Closes #999")
    assert linked_task_number(pr) == (n, "branch")


def test_linked_task_by_closes(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr("feature/x", {"src/api/a.py": "a\n"}, body=f"Intro.\n\ncloses #{n} and #7")
    assert linked_task_number(pr) == (n, "closes")


# --------------------------------------------------------------------------- context


def test_context_fields(world: ReviewWorld) -> None:
    n = world.task()
    world.main_commit("docs/upstream.md", "upstream\n")
    pr = world.open_pr(
        f"task/{n}",
        {"src/api/a.py": "print('a')\n", "src/web/b.js": "b\n", "tools/x.sh": "x\n"},
    )
    world.fake.fake_set_ci(pr.head_sha, CiStatus("failure", 2, ("lint",), ()))
    world.fake.fake_set_user("carol")
    world.fake.submit_review(pr.number, "COMMENT", "looks fine so far")

    result = review_context(make_ctx(world), pr.number)
    assert result.code == ExitCode.OK
    data = result.data
    assert data["pr"]["number"] == pr.number and data["pr"]["author"] == "alice"
    task = data["task"]
    assert task["issue"] == n and task["id"] == "M1-T01" and task["linked_by"] == "branch"
    assert task["metadata"]["zones"] == ["api"]
    assert task["acceptance_criteria"]["AC-1"] == "- AC-1: Users can log in."
    assert "Sessions are cleared." in task["acceptance_criteria"]["AC-2"]
    # files and diff come from git: the upstream docs commit is not part of the PR
    assert data["files"] == ["src/api/a.py", "src/web/b.js", "tools/x.sh"]
    assert "+print('a')" in data["diff"] and "upstream" not in data["diff"]
    assert data["diff_truncated"] is False and data["diff_bytes"] == len(data["diff"].encode())
    assert data["range"]["ref"] == f"{REVIEW_REF_PREFIX}{pr.number}"
    assert data["range"]["head"] == pr.head_sha
    assert world.bob.rev_parse(f"{REVIEW_REF_PREFIX}{pr.number}") == pr.head_sha
    # zones
    assert data["zones"]["task_zones"] == ["api"]
    assert data["zones"]["out_of_zone"] == [{"path": "src/web/b.js", "zones": ["web"]}]
    assert data["zones"]["unzoned"] == ["tools/x.sh"]
    # CI and reviews
    assert data["ci"]["state"] == "failure" and data["ci"]["failing"] == ["lint"]
    assert data["reviews"] == [
        {"id": 1, "author": "carol", "state": "COMMENTED", "body": "looks fine so far"}
    ]
    assert data["reviewer"]["agent"] == "claude" and data["reviewer"]["model"] == "opus"
    assert data["reviewer"]["required_tier"] is None
    assert warnings_of(result) == []


def test_context_via_closes_and_missing_ac(world: ReviewWorld) -> None:
    n = world.task(covers=("AC-1", "AC-9"))
    pr = world.open_pr("feature/x", {"src/api/a.py": "a\n"}, body=f"Closes #{n}")
    result = review_context(make_ctx(world), pr.number)
    assert result.data["task"]["linked_by"] == "closes"
    assert list(result.data["task"]["acceptance_criteria"]) == ["AC-1"]
    assert any("AC-9 is not defined" in w for w in warnings_of(result))


def test_unlinked_pr_has_null_task_and_warning(world: ReviewWorld) -> None:
    pr = world.open_pr("feature/y", {"src/web/b.js": "b\n", "other.txt": "o\n"})
    result = review_context(make_ctx(world), pr.number)
    assert result.code == ExitCode.OK
    assert result.data["task"] is None
    assert any("not linked to a task" in w for w in warnings_of(result))
    # without a task only unzoned files are reported
    assert result.data["zones"]["out_of_zone"] is None
    assert result.data["zones"]["unzoned"] == ["other.txt"]


def test_closes_non_task_issue_is_unlinked(world: ReviewWorld) -> None:
    issue = world.fake.create_issue("plain bug", "no block")
    pr = world.open_pr("feature/z", {"a.txt": "a\n"}, body=f"Fixes stuff. Closes #{issue.number}")
    result = review_context(make_ctx(world), pr.number)
    assert result.data["task"] is None
    assert any("not a Forge task" in w for w in warnings_of(result))


def test_diff_is_capped(world: ReviewWorld) -> None:
    n = world.task()
    big = "".join(f"line {i:07d} " + "x" * 80 + "\n" for i in range(4000))
    pr = world.open_pr(f"task/{n}", {"src/api/big.txt": big})
    result = review_context(make_ctx(world), pr.number)
    data = result.data
    assert data["diff_truncated"] is True
    assert data["diff_bytes"] > DIFF_CAP_BYTES
    assert len(data["diff"].encode()) <= DIFF_CAP_BYTES
    assert data["diff"].endswith("\n")
    assert any("diff truncated" in w for w in warnings_of(result))


def test_cap_diff_respects_utf8_and_lines() -> None:
    text = "é" * 10 + "\n" + "é" * 10
    cut, size, truncated = cap_diff(text, cap=25)
    assert truncated and size == len(text.encode())
    assert cut == "é" * 10 + "\n"
    assert cap_diff("short", cap=25) == ("short", 5, False)


def test_ci_degrades_to_null_with_warning(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    fake = world.fake

    class Proxy:
        """The shared fake, except that its token cannot read check runs (ADR 0010)."""

        def commit_ci(self, sha: str) -> CiStatus:
            raise GitHubError("HTTP 403: Resource not accessible", status=403)

        def __getattr__(self, name: str) -> Any:
            return getattr(fake, name)

    result = review_context(make_ctx(world, github=Proxy()), pr.number)
    assert result.code == ExitCode.OK
    assert result.data["ci"] is None
    assert any("CI state not available" in w for w in warnings_of(result))


def test_no_zones_config_skips_zone_check(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    result = review_context(make_ctx(world, zones=None), pr.number)
    assert result.data["zones"] is None
    assert any("zone check skipped" in w for w in warnings_of(result))


def test_unknown_pr_ref_fails(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    world.gw.remote_git.delete_ref(f"refs/pull/{pr.number}/head")
    with pytest.raises(ForgeError) as info:
        review_context(make_ctx(world), pr.number)
    assert info.value.code == ExitCode.ERROR


# --------------------------------------------------------------------------- tier


def test_min_tier_refuses_context(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    ctx = make_ctx(world, agent=STANDARD_ONLY, min_tier_for={"reviewer": "strong"})
    with pytest.raises(PreconditionError) as info:
        review_context(ctx, pr.number)
    assert info.value.code == ExitCode.PRECONDITION
    assert "min_tier_for.reviewer" in info.value.message


def test_min_tier_force_overrides_with_warning(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    ctx = make_ctx(world, agent=STANDARD_ONLY, min_tier_for={"reviewer": "strong"})
    result = review_context(ctx, pr.number, force=True)
    assert result.code == ExitCode.OK
    assert result.data["reviewer"]["forced"] is True
    assert result.data["reviewer"]["model"] == "gpt-x"  # highest tier the agent maps
    assert any("--force" in w for w in warnings_of(result))


def test_min_tier_served_is_not_forced(world: ReviewWorld) -> None:
    check = check_reviewer_tier(review_config({"reviewer": "standard"}), STANDARD_ONLY)
    assert check.served and not check.forced and check.required == Tier.STANDARD
    assert check.warning is None


def test_no_profile_context_warns(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    ctx = make_ctx(world, agent=None, min_tier_for={"reviewer": "strong"})
    result = review_context(ctx, pr.number)
    assert result.code == ExitCode.OK
    assert result.data["reviewer"] is None
    assert any("no Forge profile" in w for w in warnings_of(result))


# --------------------------------------------------------------------------- submit


@pytest.mark.parametrize(
    ("action", "event", "state"),
    [
        ("approve", "APPROVE", "APPROVED"),
        ("request-changes", "REQUEST_CHANGES", "CHANGES_REQUESTED"),
        ("comment", "COMMENT", "COMMENTED"),
    ],
)
def test_submit_maps_events_and_appends_footer(
    world: ReviewWorld, action: str, event: str, state: str
) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    result = submit_review(make_ctx(world), pr.number, action, "Looks good.\n", login="bob")
    assert result.code == ExitCode.OK
    assert result.data["event"] == event
    review = world.fake.list_reviews(pr.number)[-1]
    assert review.state == state and review.author == "bob"
    assert review.body.startswith("Looks good.\n\n")
    assert review.body.rstrip().endswith(
        "Reviewed with Forge — agent claude (anthropic), model opus"
    )
    assert result.data["forced"] is False


@pytest.mark.parametrize("action", ["approve", "request-changes"])
def test_approve_or_request_changes_own_pr_exits_5(world: ReviewWorld, action: str) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"}, author="bob")
    with pytest.raises(PreconditionError):
        submit_review(make_ctx(world), pr.number, action, "x", login="BOB")
    assert world.fake.list_reviews(pr.number) == []


def test_comment_on_own_pr_is_allowed(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"}, author="bob")
    result = submit_review(make_ctx(world), pr.number, "comment", "note", login="bob")
    assert result.code == ExitCode.OK
    assert world.fake.list_reviews(pr.number)[0].state == "COMMENTED"


def test_submit_refused_by_min_tier_then_forced(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    ctx = make_ctx(world, agent=STANDARD_ONLY, min_tier_for={"reviewer": "strong"})
    with pytest.raises(PreconditionError):
        submit_review(ctx, pr.number, "approve", "ok", login="bob")
    assert world.fake.list_reviews(pr.number) == []
    result = submit_review(ctx, pr.number, "approve", "ok", login="bob", force=True)
    assert result.data["forced"] is True
    body = world.fake.list_reviews(pr.number)[0].body
    assert "agent mid (openai, local), model gpt-x" in body
    assert "tier forced" in body and "strong" in body
    assert any("--force" in w for w in warnings_of(result))


def test_submit_without_profile_exits_5(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    with pytest.raises(PreconditionError):
        submit_review(make_ctx(world, agent=None), pr.number, "comment", "x", login="bob")


def test_submit_unknown_action_is_error(world: ReviewWorld) -> None:
    with pytest.raises(ForgeError) as info:
        submit_review(make_ctx(world), 1, "merge", "x", login="bob")
    assert info.value.code == ExitCode.ERROR


def test_footer_forced_text() -> None:
    check = check_reviewer_tier(review_config({"reviewer": "strong"}), STANDARD_ONLY, force=True)
    footer = review_footer(check)
    assert footer.startswith("Reviewed with Forge — agent mid (openai, local), model gpt-x")
    assert "tier forced" in footer


# --------------------------------------------------------------------------- review fixes


def open_raw_pr(
    world: ReviewWorld,
    branch: str,
    files: dict[str, bytes],
    *,
    start: str = "origin/main",
    base: str = "main",
    title: str = "feat: work",
) -> PullRequest:
    """Like ``ReviewWorld.open_pr`` with raw file bytes, from ``start`` into ``base``."""
    git = world.alice
    git.fetch()
    git.run("checkout", "--quiet", "-B", branch, start)
    for path, content in files.items():
        target = git.cwd / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    git.add(list(files))
    sha = git.commit(f"{title}\n")
    world.gw.push(git, branch)
    git.run("checkout", "--quiet", "main")
    world.fake.fake_set_user("alice")
    pr = world.fake.create_pull(title, "", branch, base)
    world.fake.fake_set_user("bob")
    pr = world.fake.fake_set_head_sha(pr.number, sha)
    world.gw.remote_git.update_ref(f"refs/pull/{pr.number}/head", sha)
    return pr


def test_latin1_diff_is_decoded_with_replacement_and_real_size(world: ReviewWorld) -> None:
    n = world.task()
    latin1 = "café crème\n".encode("latin-1")  # b"caf\xe9 cr\xe8me\n": not UTF-8
    pr = open_raw_pr(world, f"task/{n}", {"src/api/menu.txt": latin1, "src/api/a.py": b"a\n"})

    result = review_context(make_ctx(world), pr.number)

    assert result.code == ExitCode.OK
    data = result.data
    assert data["files"] == ["src/api/a.py", "src/api/menu.txt"]
    diff = data["diff"]
    assert isinstance(diff, str)
    assert "+caf� cr�me" in diff and "+a" in diff
    raw = world.bob.diff_bytes(data["range"]["base"], data["range"]["ref"])
    assert b"caf\xe9 cr\xe8me" in raw
    assert data["diff_bytes"] == len(raw)  # git's real size, not the decoded text's
    assert len(diff.encode()) > len(raw)  # each replaced byte became 3 bytes of U+FFFD
    assert data["diff_truncated"] is False
    assert any("not valid UTF-8" in w and "U+FFFD" in w for w in warnings_of(result))
    assert data["zones"]["out_of_zone"] == [] and data["zones"]["unzoned"] == []


def test_user_diff_config_does_not_change_the_context_diff(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "print('a')\n"})
    for key, value in (("color.ui", "always"), ("diff.noprefix", "true")):
        world.bob.config_set(key, value)
    (world.bob.cwd / ".git" / "info" / "attributes").write_text("*.py diff=upper\n")
    world.bob.config_set("diff.upper.textconv", "tr a-z A-Z <")

    diff = review_context(make_ctx(world), pr.number).data["diff"]

    assert "diff --git a/src/api/a.py b/src/api/a.py\n" in diff
    assert "+print('a')\n" in diff and "\x1b[" not in diff


def test_stacked_pr_is_diffed_against_its_own_base(world: ReviewWorld) -> None:
    n = world.task()
    lower = world.open_pr("feature/lower", {"src/web/lower.js": "lower\n"})
    world.main_commit("docs/upstream.md", "upstream\n")
    top = open_raw_pr(
        world,
        f"task/{n}",
        {"src/api/top.py": b"top\n"},
        start="origin/feature/lower",
        base="feature/lower",
    )
    assert top.base == "feature/lower" and lower.base == "main"

    result = review_context(make_ctx(world), top.number)

    data = result.data
    # only the PR's own change: not the lower PR's file, not main's later commit
    assert data["files"] == ["src/api/top.py"]
    assert "+top" in data["diff"] and "lower" not in data["diff"]
    assert "upstream" not in data["diff"]
    lower_tip = world.bob.rev_parse("refs/remotes/origin/feature/lower")
    assert data["range"]["base"] == lower_tip
    # the zone check uses the same file list: the lower PR's web file is not out of zone here
    assert data["zones"]["out_of_zone"] == [] and data["zones"]["unzoned"] == []
    assert warnings_of(result) == []


def test_deleted_base_branch_falls_back_to_the_default_branch(world: ReviewWorld) -> None:
    n = world.task()
    world.open_pr("feature/lower", {"src/web/lower.js": "lower\n"})
    top = open_raw_pr(
        world,
        f"task/{n}",
        {"src/api/top.py": b"top\n"},
        start="origin/feature/lower",
        base="feature/lower",
    )
    world.gw.remote_git.delete_ref("refs/heads/feature/lower")

    result = review_context(make_ctx(world), top.number)

    assert result.code == ExitCode.OK
    assert result.data["files"] == ["src/api/top.py", "src/web/lower.js"]
    assert any(
        "base branch feature/lower could not be fetched" in w and "against main" in w
        for w in warnings_of(result)
    )


def test_submit_with_a_different_gh_login_exits_5_before_submitting(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"})
    ctx = make_ctx(world)
    world.fake.fake_set_user("alice")  # gh is authenticated as the PR's author
    with pytest.raises(PreconditionError) as info:
        submit_review(ctx, pr.number, "approve", "ok", login="bob")
    assert info.value.code == ExitCode.PRECONDITION
    assert "'alice'" in info.value.message and "'bob'" in info.value.message
    assert world.fake.list_reviews(pr.number) == []
    # commenting is refused too: the footer would not match the real reviewer
    with pytest.raises(PreconditionError):
        submit_review(ctx, pr.number, "comment", "ok", login="bob")
    assert world.fake.list_reviews(pr.number) == []


def test_own_pr_guard_uses_the_authenticated_login(world: ReviewWorld) -> None:
    n = world.task()
    pr = world.open_pr(f"task/{n}", {"src/api/a.py": "a\n"}, author="bob")
    ctx = make_ctx(world)  # gh authenticated as bob
    with pytest.raises(PreconditionError, match="is yours"):
        submit_review(ctx, pr.number, "approve", "ok", login="Bob")
    result = submit_review(ctx, pr.number, "comment", "ok", login="Bob")
    assert result.data["reviewer_login"] == "bob"
