"""Scenario tests for forge.core.ship (ADR 0012) on real temporary repositories.

A bare remote plus one clone per simulated user and a shared FakeGitHub
(``support.ship_world``). Ship runs the real sync; checks are recorded shell commands.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from forge.core import ship as ship_module
from forge.core.config import DelegationConfig
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.git import Git
from forge.core.github import GitHubError
from forge.core.lease import format_time
from forge.core.pr_body import BEGIN_MARKER, find_block
from forge.core.profile import Profile
from forge.core.reviewers import select_reviewers
from forge.core.ship import ShipContext, ShipOutcome, ship
from forge.core.sync import SyncOptions, read_sync_log, sync, sync_continue
from forge.core.tasks import Status
from forge.core.workspace import read_current, write_current
from forge.core.zones import ZonesConfig
from support.scenarios import T0, UserGitHub
from support.ship_world import TEAM, CheckRecorder, ShipWorld, ship_config
from support.sync_world import SYNC_ZONES

if TYPE_CHECKING:
    from conftest import GitWorld

IN_REVIEW = Status.IN_REVIEW.label


@pytest.fixture
def sw(git_world: GitWorld) -> ShipWorld:
    return ShipWorld(git_world)


def head(git: Git) -> str:
    sha = git.rev_parse("HEAD")
    assert sha is not None
    return sha


def claimed(sw: ShipWorld, task_id: str = "M1-T01", zones: tuple[str, ...] = ("api",)) -> int:
    issue = sw.task(task_id, zones)
    sw.claim("alice", issue)
    return issue


def block_of(body: str) -> str:
    span = find_block(body)
    assert span is not None, body
    return body[span[0] : span[1]]


# --------------------------------------------------------------------------- end to end


def test_ship_opens_pr_with_block_label_heartbeat_and_reviewer(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    git = sw.world.git("alice")

    out = sw.ship()

    assert out.code == ExitCode.OK and out.outcome == "created", out.messages
    assert sw.remote_tip(issue) == head(git) == out.head
    pr = sw.pull(issue)
    assert pr.title == "[M1-T01] task" and pr.base == "main" and not pr.draft
    block = block_of(pr.body)
    assert block.startswith(BEGIN_MARKER + "\nCloses #" + str(issue))
    assert "**Task** `M1-T01` · **Spec** `specs/m1/x.md`" in block
    assert "- [ ] **AC-1**: The API saves drafts and returns their id." in block
    assert "AC-2" not in block
    assert "- [ ] tests pass" in block
    assert "### Out-of-zone changes\nnone" in block
    assert "### Conflict resolutions\nnone" in block
    assert "- Agent `claude` (claude-code), providers: anthropic" in block
    assert "- Task tier standard → `sonnet`" in block
    assert "- Tier map: strong → `opus`, standard → `sonnet`, light → `haiku`" in block
    assert pr.body == block + "\n"  # no template in this repository
    labels = sw.labels(issue)
    assert IN_REVIEW in labels and Status.IN_PROGRESS.label not in labels
    assert out.labels_added == [IN_REVIEW] and out.labels_removed == [Status.IN_PROGRESS.label]
    lease = sw.lease(issue).data
    assert lease is not None and out.heartbeat_at == format_time(lease.heartbeat_at)
    assert lease.heartbeat_at == T0 + timedelta(hours=1)
    expected = select_reviewers(
        TEAM.members, me="alice", my_providers=("anthropic",), issue=issue, required=1
    ).logins
    assert expected == ["bob"]  # the only member with no provider overlap
    assert list(pr.requested_reviewers) == expected == out.requested
    data = out.to_data()
    assert data["pr"]["created"] is True and data["pr"]["number"] == pr.number
    assert data["reviewers"]["selected"][0] == {
        "login": "bob",
        "rank": 1,
        "reason": "no provider overlap with the author's agent",
    }
    assert data["sync"]["outcome"] == "pushed"


def test_pull_opened_concurrently_is_updated_not_duplicated(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    real = sw.fake.create_pull

    def racing_create(title: str, body: str, head: str, base: str, *, draft: bool = False):  # type: ignore[no-untyped-def]
        real(title, "opened by another ship", head, base, draft=draft)
        raise GitHubError("HTTP 422: A pull request already exists", status=422)

    sw.fake.create_pull = racing_create  # type: ignore[method-assign]
    out = sw.ship()

    assert out.code == ExitCode.OK and out.outcome == "updated"
    assert len(sw.pulls(issue)) == 1
    assert "<!-- forge:pr-begin -->" in sw.pull(issue).body
    assert any("opened concurrently" in m.text for m in out.messages)


def test_rerun_is_idempotent(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    first = sw.ship()
    pr = sw.pull(issue)
    requests: list[list[str]] = []
    gh = sw.github("alice")
    real = sw.fake.request_reviewers

    def spy(number: int, logins: list[str]) -> None:
        requests.append(list(logins))
        real(number, logins)

    sw.fake.request_reviewers = spy  # type: ignore[method-assign]
    second = sw.ship()

    assert first.outcome == "created" and second.outcome == "updated"
    assert second.code == ExitCode.OK and not second.body_changed
    assert len(sw.pulls(issue)) == 1
    again = sw.pull(issue)
    assert again.body == pr.body
    assert again.requested_reviewers == pr.requested_reviewers == ("bob",)
    assert requests == [] and second.requested == []
    assert second.reviewers is not None and second.reviewers.existing == ("bob",)
    assert gh.get_issue(issue).labels.count(IN_REVIEW) == 1
    assert second.labels_added == [] and second.labels_removed == []


def test_update_keeps_human_text_ticks_and_template_is_appended_on_creation_only(
    sw: ShipWorld,
) -> None:
    template = "## Checklist\n- [ ] I tested it\n"
    sw.upstream({".github/pull_request_template.md": template}, link=False, message="chore: tpl")
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    assert sw.ship().outcome == "created"
    pr = sw.pull(issue)
    assert pr.body.endswith("\n\n" + template)
    assert pr.body.count("## Checklist") == 1

    # a reviewer ticks an item, a human edits text outside the markers
    ticked = pr.body.replace("- [ ] tests pass", "- [x] tests pass").replace(
        "- [ ] I tested it", "- [x] I tested it"
    )
    human = "Human intro  \r\n\n" + ticked + "\nHuman footer\t\n"
    sw.fake.update_pull(pr.number, body=human)
    sw.commit("alice", "src/web/w.js", "w\n")  # an out-of-zone change: the block changes

    out = sw.ship()

    assert out.outcome == "updated" and out.body_changed
    body = sw.pull(issue).body
    span = find_block(body)
    assert span is not None
    old_span = find_block(human)
    assert old_span is not None
    assert body[: span[0]] == human[: old_span[0]]  # byte for byte
    assert body[span[1] :] == human[old_span[1] :]
    block = body[span[0] : span[1]]
    assert "- [x] tests pass" in block
    assert "- `src/web/w.js`: zone web" in block
    assert body.count("## Checklist") == 1 and "- [x] I tested it" in body


def test_draft_applies_on_creation_only(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    assert sw.ship(draft=True).outcome == "created"
    pr = sw.pull(issue)
    assert pr.draft

    sw.fake.fake_set_draft(pr.number, False)
    out = sw.ship(draft=True)

    assert out.outcome == "updated"
    assert not sw.pull(issue).draft
    assert any("--draft ignored" in m.text for m in out.messages)
    sw.fake.fake_set_draft(pr.number, True)
    assert sw.ship().outcome == "updated" and sw.pull(issue).draft  # never undrafted


# --------------------------------------------------------------------------- checks (B7)


def test_checks_skipped_when_sync_passed_them_on_head(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    sw.upstream({"src/web/up.js": "up\n"})
    checks = CheckRecorder()

    out = sw.ship(config=ship_config(checks={"lint": "true"}), checks=checks)

    assert out.code == ExitCode.OK
    assert out.sync is not None and out.sync.rebased and out.sync.outcome == "pushed"
    assert out.checks_skipped and out.checks == []
    assert [c for c, _ in checks.calls] == ["true"]  # once, by the sync
    entry = read_sync_log(sw.world.git("alice").cwd)[-1]
    assert entry["checks_passed"] is True and entry["checked_head"] == out.head
    assert sw.remote_tip(issue) == out.head


def test_checks_run_when_sync_was_up_to_date(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    sw.push_task("alice", issue)
    checks = CheckRecorder()

    out = sw.ship(config=ship_config(checks={"lint": "true", "test": "true"}), checks=checks)

    assert out.code == ExitCode.OK
    assert out.sync is not None and out.sync.outcome == "up_to_date"
    assert not out.checks_skipped
    assert [c.name for c in out.checks] == ["lint", "test"]
    assert len(checks.calls) == 2


def test_failing_checks_exit_2_before_the_pr(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    sw.push_task("alice", issue)
    checks = CheckRecorder(fail=["make test"])

    out = sw.ship(config=ship_config(checks={"lint": "true", "test": "make test"}), checks=checks)

    assert out.code == ExitCode.AGENT_ACTION and out.outcome == "checks_failed"
    assert [(c.name, c.exit_code) for c in out.checks] == [("lint", 0), ("test", 1)]
    assert out.to_data()["checks"]["runs"][1]["output"] == "make test: failed"
    assert sw.pulls(issue) == []
    assert IN_REVIEW not in sw.labels(issue)


# --------------------------------------------------------------------------- zones


def test_out_of_zone_and_unzoned_files_are_listed_and_warned(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    sw.commit("alice", "src/web/w.js", "w\n")
    sw.commit("alice", "README.md", "readme\n")

    out = sw.ship()

    assert out.code == ExitCode.OK
    assert [(f.path, f.zones) for f in out.out_of_zone] == [
        ("README.md", ()),
        ("src/web/w.js", ("web",)),
    ]
    warnings = [m.text for m in out.messages if m.level == "warning"]
    assert any("out-of-zone" in w and "src/web/w.js (web)" in w for w in warnings)
    assert any("unzoned" in w and "README.md" in w for w in warnings)
    block = block_of(sw.pull(issue).body)
    assert "- `README.md`: unzoned\n- `src/web/w.js`: zone web" in block


def test_unzoned_error_exits_2_before_touching_the_pr(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    sw.commit("alice", "README.md", "readme\n")
    zones = ZonesConfig(zones=SYNC_ZONES.zones, unzoned="error")

    out = sw.ship(zones=zones)

    assert out.code == ExitCode.AGENT_ACTION and out.outcome == "unzoned"
    assert any("README.md" in m.text and m.level == "error" for m in out.messages)
    assert sw.pulls(issue) == []
    assert IN_REVIEW not in sw.labels(issue)

    # with only zoned files the same configuration ships
    git = sw.world.git("alice")
    git.run("rm", "--quiet", "README.md")
    git.commit("chore: drop readme change")
    git.run("checkout", "--quiet", "origin/main", "--", "README.md")
    git.commit("chore: restore readme")
    assert sw.ship(zones=zones).outcome == "created"


# --------------------------------------------------------------------------- sync results


def test_sync_conflicts_exit_2_then_resolutions_are_listed(sw: ShipWorld) -> None:
    issue = claimed(sw)
    git = sw.world.git("alice")
    sw.commit("alice", "src/api/a.py", "task\n")
    sw.commit("alice", "CHANGELOG.md", "- task\n")
    sw.upstream({"src/api/a.py": "upstream\n", "CHANGELOG.md": "- upstream\n"}, author="bob")

    stopped = sw.ship()

    assert stopped.code == ExitCode.AGENT_ACTION and stopped.outcome == "sync_stopped"
    assert stopped.sync is not None and stopped.sync.outcome == "conflicts"
    assert sw.pulls(issue) == []
    assert IN_REVIEW not in sw.labels(issue)

    (git.cwd / "src/api/a.py").write_text("both\n")
    git.add(["src/api/a.py"])
    assert sync_continue(sw.ctx()).outcome == "pushed"
    out = sw.ship()

    assert out.code == ExitCode.OK and out.outcome == "created"
    block = block_of(sw.pull(issue).body)
    section = block.split("### Conflict resolutions\n", 1)[1].split("\n###", 1)[0]
    assert "  - `CHANGELOG.md`: resolver `union`" in section
    assert "  - `src/api/a.py`: manual" in section
    assert "by `@bob`" in section
    assert "by @bob" not in section
    assert "force-resolve" not in section


def test_sync_escalation_exit_4_then_force_resolve_is_flagged(sw: ShipWorld) -> None:
    other = sw.task("M1-T09", ["web"])
    sw.upstream({"src/web/app.js": "base\n"}, link=False, message="chore: base")
    issue = claimed(sw)
    git = sw.world.git("alice")
    sw.commit("alice", "src/api/a.py", "a\n")
    sw.commit("alice", "src/web/app.js", "task\n")
    sw.upstream({"src/web/app.js": "upstream\n"}, closes=other, author="bob")

    escalated = sw.ship()

    assert escalated.code == ExitCode.ESCALATION and escalated.outcome == "sync_stopped"
    assert sw.pulls(issue) == []

    assert sync(sw.ctx(), SyncOptions(force_resolve=True)).code == ExitCode.AGENT_ACTION
    (git.cwd / "src/web/app.js").write_text("merged\n")
    git.add(["src/web/app.js"])
    assert sync_continue(sw.ctx()).outcome == "pushed"
    out = sw.ship()

    assert out.outcome == "created", out.messages
    block = block_of(sw.pull(issue).body)
    assert "**force-resolve**" in block
    assert "  - `src/web/app.js`: manual" in block
    assert "- `src/web/app.js`: zone web" in block  # also an out-of-zone change


def test_preconditions_exit_5(sw: ShipWorld) -> None:
    issue = sw.task("M1-T01", ["api"])
    with pytest.raises(PreconditionError):
        sw.ship()  # no current task
    sw.claim("alice", issue)
    ctx = sw.ship_ctx()
    as_bob = replace(ctx.sync, github=sw.github("bob"), profile_login="bob")
    with pytest.raises(PreconditionError):
        ship(ShipContext(sync=as_bob))  # the claim is not mine
    assert sw.pulls(issue) == []


def test_sync_lease_rejection_exit_5_is_propagated(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    git = sw.world.git("alice")

    def move_remote(src: str, dst: str) -> None:
        git.before_push = None
        sw.world.push_work("carol", issue, "src/api/z.py", T0 + timedelta(minutes=30))

    git.before_push = move_remote
    out = sw.ship()

    assert out.code == ExitCode.PRECONDITION and out.outcome == "sync_stopped"
    assert out.sync is not None and out.sync.outcome == "lease_rejected"
    assert sw.pulls(issue) == []


# --------------------------------------------------------------------------- reviewers


def test_reviewers_top_up_and_respect_existing_reviews(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    config = ship_config(required_approvals=2)
    assert sw.ship(config=config).requested == ["bob", "carol"]  # rank 1, then rank 2
    pr = sw.pull(issue)

    # bob reviews (his request is cleared); one more is still requested: nothing to add
    sw.github("bob").submit_review(pr.number, "COMMENT", "looks fine")
    out = sw.ship(config=config)
    assert out.requested == []
    assert out.reviewers is not None and set(out.reviewers.existing) == {"bob", "carol"}

    # with three required, dave (no providers declared) tops it up
    out = sw.ship(config=ship_config(required_approvals=3))
    assert out.requested == ["dave"]
    assert set(sw.pull(issue).requested_reviewers) == {"carol", "dave"}


def test_no_team_or_no_candidates_is_a_warning(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")

    out = sw.ship(team=None)

    assert out.code == ExitCode.OK and out.requested == []
    assert any("no review was requested" in m.text for m in out.messages)
    assert sw.pull(issue).requested_reviewers == ()


def test_cross_provider_false_uses_rotated_team_order(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")

    out = sw.ship(config=ship_config(cross_provider=False))

    members = TEAM.members
    rotated = [*members[issue % 4 :], *members[: issue % 4]]
    expected = next(m.github for m in rotated if m.github != "alice")
    assert out.requested == [expected]


class NoAccessReviewers(UserGitHub):
    """Alice's view of the shared fake, where GitHub refuses every review request with 422
    (as it does for a login that is not a collaborator of the repository)."""

    def __init__(self, sw: ShipWorld) -> None:
        super().__init__(sw.fake, "alice", sw.world.lock)
        self.refused: list[list[str]] = []

    def request_reviewers(self, number: int, logins: list[str]) -> None:
        self.refused.append(list(logins))
        raise GitHubError("HTTP 422: Reviews may only be requested from collaborators", status=422)


def test_refused_review_request_is_a_warning_and_the_pr_is_done(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    ctx = sw.ship_ctx()
    github = NoAccessReviewers(sw)
    ctx = ShipContext(sync=replace(ctx.sync, github=github))  # type: ignore[arg-type]

    out = ship(ctx)

    assert out.code == ExitCode.OK and out.outcome == "created", out.messages
    assert github.refused == [["bob"]]
    assert out.requested == []
    assert out.reviewers is not None and out.reviewers.logins == ["bob"]
    warning = next(m for m in out.messages if m.level == "warning" and "bob" in m.text)
    assert "could not be requested" in warning.text and "HTTP 422" in warning.text
    assert warning.hint is not None
    assert "check they have access to the repository, or update .forge/team.yml" in warning.hint
    # everything before the reviewers is done
    pr = sw.pull(issue)
    assert pr.requested_reviewers == ()
    assert IN_REVIEW in sw.labels(issue)
    lease = sw.lease(issue).data
    assert lease is not None and out.heartbeat_at == format_time(lease.heartbeat_at)
    # once access is fixed, a re-run requests them
    again = sw.ship()
    assert again.outcome == "updated" and again.requested == ["bob"]


def test_heartbeat_failure_after_the_pr_says_the_pr_exists_and_rerun_completes(
    sw: ShipWorld,
) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    ctx = sw.ship_ctx()
    github = ctx.sync.github

    def break_the_heartbeat() -> None:
        github.fail["list_comments"] = 1  # type: ignore[attr-defined]

    github.after["create_pull"] = break_the_heartbeat  # type: ignore[attr-defined]
    with pytest.raises(ForgeError) as info:
        ship(ctx)

    pr = sw.pull(issue)
    error = info.value
    assert f"pull request #{pr.number} was opened" in error.message
    assert "heartbeat failed" in error.message and "injected failure" in error.message
    assert error.hint is not None
    assert "the pull request exists" in error.hint
    assert "re-run `forge ship`" in error.hint and "completes the remaining steps" in error.hint
    assert pr.requested_reviewers == ()  # the run stopped before the reviewers

    again = sw.ship()
    assert again.code == ExitCode.OK and again.outcome == "updated"
    assert len(sw.pulls(issue)) == 1
    assert again.requested == ["bob"] and sw.pull(issue).requested_reviewers == ("bob",)
    lease = sw.lease(issue).data
    assert lease is not None and again.heartbeat_at == format_time(lease.heartbeat_at)


# --------------------------------------------------------------------------- delegation


def test_pr_body_records_the_delegation_decision(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    ctx = sw.ship_ctx()
    agent = ctx.sync.agent
    profile = Profile("alice", (agent,), agent.id, delegation="off")

    out = ship(replace(ctx, profile=profile))

    assert out.code == ExitCode.OK, out.messages
    assert "- Delegation: off (requested by profile)" in block_of(sw.pull(issue).body)


def test_pr_body_delegation_without_profile_is_off(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    out = sw.ship()
    assert out.code == ExitCode.OK, out.messages
    assert "- Delegation: off (no profile)" in block_of(sw.pull(issue).body)


def test_pr_body_delegation_missing_template(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    ctx = sw.ship_ctx()
    agent = ctx.sync.agent
    out = ship(replace(ctx, profile=Profile("alice", (agent,), agent.id, delegation="on")))
    assert out.code == ExitCode.OK, out.messages
    assert "- Delegation: off (.forge/agents/implementer.md missing)" in block_of(
        sw.pull(issue).body
    )


def _record_override(sw: ShipWorld, mode: str) -> None:
    root = sw.world.git("alice").root()
    current = read_current(root)
    assert current is not None
    write_current(root, replace(current, delegation_override=mode))  # type: ignore[arg-type]


def test_pr_body_uses_the_override_recorded_in_current_json(sw: ShipWorld) -> None:
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    _record_override(sw, "off")
    ctx = sw.ship_ctx()
    agent = ctx.sync.agent
    out = ship(replace(ctx, profile=Profile("alice", (agent,), agent.id, delegation="on")))
    assert out.code == ExitCode.OK, out.messages
    assert "- Delegation: off (requested by --delegation)" in block_of(sw.pull(issue).body)


def test_delegation_off_everywhere_changes_only_the_delegation_line(
    sw: ShipWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Done-when of ADR 0017: with `off` in the project, the profile and the recorded override,
    the PR block and the messages equal the output with the delegation code path neutralised
    (``delegation_for`` patched to return None, as v0.1.0 had no decision), except for the
    one new line."""
    issue = claimed(sw)
    sw.commit("alice", "src/api/a.py", "a\n")
    _record_override(sw, "off")
    config = replace(ship_config(), delegation=DelegationConfig("off", ("off",)))
    ctx = sw.ship_ctx(config=config)
    agent = ctx.sync.agent
    ctx = replace(ctx, profile=Profile("alice", (agent,), agent.id, delegation="off"))

    def neutral_ship() -> ShipOutcome:
        with monkeypatch.context() as patch:
            patch.setattr(ship_module, "delegation_for", lambda *a, **k: None)
            return ship(ctx)

    # Alternate so that both compared runs rewrite the block (same "updated" messages).
    assert neutral_ship().outcome == "created"
    assert ship(ctx).outcome == "updated"
    neutral = neutral_ship()
    neutral_block = block_of(sw.pull(issue).body)
    real = ship(ctx)
    real_block = block_of(sw.pull(issue).body)

    line = "- Delegation: off (requested by --delegation)"
    assert line in real_block.splitlines()
    assert real_block.replace(line + "\n", "") == neutral_block
    assert [m.to_dict() for m in real.messages] == [m.to_dict() for m in neutral.messages]
    assert real.code == neutral.code and real.outcome == neutral.outcome == "updated"
