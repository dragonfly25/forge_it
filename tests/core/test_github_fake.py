"""Tests for the in-memory FakeGitHub."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from forge.core.github import CiStatus, GitHubClient, GitHubError
from forge.core.github_fake import FakeGitHub


@pytest.fixture
def gh() -> FakeGitHub:
    fake = FakeGitHub(user="alice")
    for name in ("forge:task", "status:ready", "status:in-progress", "size:S"):
        fake.create_label(name, "ededed", "")
    fake.create_milestone("M1", "first")
    return fake


def test_satisfies_protocol() -> None:
    assert isinstance(FakeGitHub(), GitHubClient)


def test_injected_clock_is_used() -> None:
    times = iter([datetime(2026, 5, 1, tzinfo=UTC) + timedelta(minutes=i) for i in range(10)])
    gh = FakeGitHub(clock=lambda: next(times))
    issue = gh.create_issue("t", "b")
    assert issue.created_at == datetime(2026, 5, 1, tzinfo=UTC)
    comment = gh.create_comment(issue.number, "x")
    updated = gh.update_comment(comment.id, "y")
    assert updated.created_at == comment.created_at
    assert updated.updated_at > comment.updated_at


# ---------------------------------------------------------------- labels


def test_labels_create_list_update(gh: FakeGitHub) -> None:
    label = gh.create_label("type:impl", "#00ff00", "impl")
    assert label.color == "00ff00"
    assert "type:impl" in [lb.name for lb in gh.list_labels()]
    updated = gh.update_label("type:impl", color="111111", description="d")
    assert (updated.color, updated.description) == ("111111", "d")


def test_list_labels_sorted_by_name() -> None:
    gh = FakeGitHub()
    for name in ("b", "A", "c"):
        gh.create_label(name, "000000")
    assert [lb.name for lb in gh.list_labels()] == ["A", "b", "c"]


def test_rename_label_renames_on_pulls(gh: FakeGitHub) -> None:
    pr = gh.create_pull("p", "", "task/1", "main")
    gh.add_labels(pr.number, ["size:S"])
    gh.update_label("size:S", new_name="size:small")
    assert gh.get_pull(pr.number).labels == ("size:small",)


def test_duplicate_label_is_rejected_case_insensitively(gh: FakeGitHub) -> None:
    with pytest.raises(GitHubError):
        gh.create_label("Forge:Task", "000000")


def test_rename_label_renames_on_issues(gh: FakeGitHub) -> None:
    issue = gh.create_issue("t", "b", labels=["size:S"])
    gh.update_label("size:S", new_name="size:s-small")
    assert gh.get_issue(issue.number).labels == ("size:s-small",)


def test_update_missing_label_fails(gh: FakeGitHub) -> None:
    with pytest.raises(GitHubError):
        gh.update_label("nope", color="000000")


# ---------------------------------------------------------------- milestones


def test_milestones(gh: FakeGitHub) -> None:
    m2 = gh.create_milestone("M2")
    assert [m.title for m in gh.list_milestones()] == ["M1", "M2"]
    closed = gh.close_milestone(m2.number)
    assert closed.state == "closed"
    assert [m.title for m in gh.list_milestones("open")] == ["M1"]
    assert [m.title for m in gh.list_milestones("closed")] == ["M2"]
    with pytest.raises(GitHubError):
        gh.create_milestone("M1")
    with pytest.raises(GitHubError):
        gh.close_milestone(99)
    assert gh.list_milestones()[0].due_on is None


def test_update_milestone(gh: FakeGitHub) -> None:
    issue = gh.create_issue("t", "b", milestone="M1")
    due = datetime(2026, 7, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    m = gh.update_milestone(1, title="M1b", description="d", due_on=due)
    assert (m.title, m.description, m.state) == ("M1b", "d", "open")
    assert m.due_on == datetime(2026, 7, 1, 10, tzinfo=UTC)
    assert gh.get_issue(issue.number).milestone == "M1b"
    assert gh.update_milestone(1, state="closed").due_on == m.due_on
    assert gh.list_milestones("closed")[0].title == "M1b"
    gh.create_milestone("M2")
    with pytest.raises(GitHubError, match="already exists"):
        gh.update_milestone(1, title="M2")
    with pytest.raises(GitHubError):
        gh.update_milestone(1, due_on=datetime(2026, 7, 1))
    with pytest.raises(GitHubError):
        gh.update_milestone(99, title="x")


# ---------------------------------------------------------------- issues


def test_create_issue_fields(gh: FakeGitHub) -> None:
    issue = gh.create_issue(
        "[M1-T01] x",
        "body",
        labels=["forge:task", "status:ready"],
        milestone="M1",
        assignees=["bob"],
    )
    assert issue.number == 1
    assert issue.state == "open"
    assert issue.labels == ("forge:task", "status:ready")
    assert issue.milestone == "M1"
    assert issue.assignees == ("bob",)
    assert issue.author == "alice"
    assert issue.closed_at is None
    assert issue.url.endswith("/issues/1")
    assert gh.get_issue(1) == issue


def test_create_issue_with_unknown_label_fails(gh: FakeGitHub) -> None:
    with pytest.raises(GitHubError, match="does not exist"):
        gh.create_issue("t", "b", labels=["status:nope"])


def test_create_issue_with_unknown_milestone_fails(gh: FakeGitHub) -> None:
    with pytest.raises(GitHubError):
        gh.create_issue("t", "b", milestone="M9")


def test_get_missing_issue_fails(gh: FakeGitHub) -> None:
    with pytest.raises(GitHubError) as exc:
        gh.get_issue(42)
    assert exc.value.status == 404


def test_close_and_reopen(gh: FakeGitHub) -> None:
    issue = gh.create_issue("t", "b")
    closed = gh.update_issue(issue.number, state="closed", state_reason="not_planned")
    assert closed.state == "closed"
    assert closed.state_reason == "not_planned"
    assert closed.closed_at is not None
    default = gh.update_issue(gh.create_issue("u", "b").number, state="closed")
    assert default.state_reason == "completed"
    reopened = gh.update_issue(issue.number, state="open")
    assert reopened.state == "open"
    assert reopened.closed_at is None
    assert reopened.state_reason == "reopened"


def test_invalid_state_reason(gh: FakeGitHub) -> None:
    issue = gh.create_issue("t", "b")
    with pytest.raises(GitHubError):
        gh.update_issue(issue.number, state="closed", state_reason="done")


def test_update_issue_milestone_semantics(gh: FakeGitHub) -> None:
    issue = gh.create_issue("t", "b", milestone="M1")
    assert gh.update_issue(issue.number, title="new").milestone == "M1"
    assert gh.update_issue(issue.number, milestone=None).milestone is None
    gh.create_milestone("M2")
    updated = gh.update_issue(issue.number, milestone="M2", body="nb")
    assert (updated.milestone, updated.body, updated.title) == ("M2", "nb", "new")
    with pytest.raises(GitHubError):
        gh.update_issue(issue.number, milestone="M9")


def test_issue_labels_add_remove_set(gh: FakeGitHub) -> None:
    issue = gh.create_issue("t", "b", labels=["forge:task"])
    gh.add_labels(issue.number, ["status:ready", "forge:task"])
    assert gh.get_issue(issue.number).labels == ("forge:task", "status:ready")
    gh.remove_label(issue.number, "status:ready")
    gh.remove_label(issue.number, "status:ready")  # absent: no error
    assert gh.get_issue(issue.number).labels == ("forge:task",)
    gh.set_labels(issue.number, ["status:in-progress", "size:S"])
    assert gh.get_issue(issue.number).labels == ("status:in-progress", "size:S")
    with pytest.raises(GitHubError):
        gh.add_labels(issue.number, ["unknown"])
    with pytest.raises(GitHubError):
        gh.set_labels(issue.number, ["unknown"])


def test_assignees(gh: FakeGitHub) -> None:
    issue = gh.create_issue("t", "b")
    gh.add_assignees(issue.number, ["bob", "carol"])
    gh.add_assignees(issue.number, ["bob"])
    assert gh.get_issue(issue.number).assignees == ("bob", "carol")
    gh.remove_assignees(issue.number, ["bob", "dave"])
    assert gh.get_issue(issue.number).assignees == ("carol",)


def test_list_issue_filters(gh: FakeGitHub) -> None:
    gh.create_milestone("M2")
    a = gh.create_issue("a", "", labels=["forge:task", "status:ready"], milestone="M1")
    b = gh.create_issue("b", "", labels=["forge:task"], milestone="M2")
    c = gh.create_issue("c", "", labels=["forge:task", "status:ready"], milestone="M1")
    gh.update_issue(c.number, state="closed")

    def nums(**kw: object) -> list[int]:
        return [i.number for i in gh.list_issues(**kw)]  # type: ignore[arg-type]

    assert nums() == [a.number, b.number]
    assert nums(state="all") == [a.number, b.number, c.number]
    assert nums(state="closed") == [c.number]
    assert nums(labels=["forge:task", "status:ready"]) == [a.number]
    assert nums(milestone="M2") == [b.number]
    assert nums(milestone="M9") == []


# ---------------------------------------------------------------- comments


def test_comments(gh: FakeGitHub) -> None:
    issue = gh.create_issue("t", "b")
    other = gh.create_issue("u", "b")
    first = gh.create_comment(issue.number, "<!-- forge:lease -->\nalice")
    gh.fake_set_user("bob")
    second = gh.create_comment(issue.number, "hi")
    gh.create_comment(other.number, "elsewhere")
    assert [c.id for c in gh.list_comments(issue.number)] == [first.id, second.id]
    assert first.author == "alice"
    assert second.author == "bob"
    edited = gh.update_comment(first.id, "<!-- forge:lease -->\nbob")
    assert edited.body.endswith("bob")
    assert edited.updated_at > first.updated_at
    assert gh.list_comments(issue.number)[0].body.endswith("bob")
    with pytest.raises(GitHubError):
        gh.update_comment(999, "x")
    with pytest.raises(GitHubError):
        gh.create_comment(999, "x")


def test_delete_comment(gh: FakeGitHub) -> None:
    issue = gh.create_issue("t", "b")
    first = gh.create_comment(issue.number, "a")
    second = gh.create_comment(issue.number, "b")
    gh.delete_comment(first.id)
    assert [c.id for c in gh.list_comments(issue.number)] == [second.id]
    with pytest.raises(GitHubError) as exc:
        gh.delete_comment(first.id)
    assert exc.value.status == 404


# ---------------------------------------------------------------- pull requests


def test_issue_and_pr_numbers_are_shared(gh: FakeGitHub) -> None:
    issue = gh.create_issue("t", "b")
    pr = gh.create_pull("p", "Closes #1", "task/1", "main")
    nxt = gh.create_issue("u", "b")
    assert (issue.number, pr.number, nxt.number) == (1, 2, 3)
    assert [i.number for i in gh.list_issues()] == [1, 3]
    with pytest.raises(GitHubError, match="pull request"):
        gh.get_issue(pr.number)


def test_pull_create_update_list(gh: FakeGitHub) -> None:
    pr = gh.create_pull("p", "body", "task/1", "main", draft=True)
    assert (pr.state, pr.merged, pr.draft, pr.author) == ("open", False, True, "alice")
    assert pr.url.endswith(f"/pull/{pr.number}")
    gh.create_pull("q", "body", "task/2", "develop")
    assert [p.head for p in gh.list_pulls(head="task/1")] == ["task/1"]
    assert [p.head for p in gh.list_pulls(base="develop")] == ["task/2"]
    assert gh.list_pulls(state="closed") == []
    updated = gh.update_pull(pr.number, body="new body")
    assert (updated.title, updated.body) == ("p", "new body")
    assert gh.get_pull(pr.number) == updated


def test_duplicate_open_pull_is_rejected(gh: FakeGitHub) -> None:
    gh.create_pull("p", "", "task/1", "main")
    with pytest.raises(GitHubError, match="already exists"):
        gh.create_pull("p2", "", "task/1", "main")
    with pytest.raises(GitHubError):
        gh.create_pull("p3", "", "main", "main")


def test_pull_labels_and_comments(gh: FakeGitHub) -> None:
    pr = gh.create_pull("p", "", "task/1", "main")
    gh.add_labels(pr.number, ["status:ready"])
    assert gh.get_pull(pr.number).labels == ("status:ready",)
    gh.remove_label(pr.number, "status:ready")
    assert gh.get_pull(pr.number).labels == ()
    gh.create_comment(pr.number, "on pr")
    assert [c.body for c in gh.list_comments(pr.number)] == ["on pr"]


def test_pull_assignees(gh: FakeGitHub) -> None:
    pr = gh.create_pull("p", "", "task/1", "main")
    gh.add_assignees(pr.number, ["bob", "carol"])
    gh.remove_assignees(pr.number, ["carol"])
    assert gh.get_pull(pr.number).assignees == ("bob",)
    with pytest.raises(GitHubError):
        gh.add_assignees(99, ["bob"])


def test_pull_new_fields(gh: FakeGitHub) -> None:
    pr = gh.create_pull("p", "", "task/1", "main")
    other = gh.create_pull("q", "", "task/2", "main")
    assert len(pr.head_sha) == 40 and pr.head_sha != other.head_sha
    assert (pr.mergeable, pr.merged_at, pr.labels, pr.assignees) == (True, None, (), ())
    assert gh.fake_set_head_sha(pr.number, "f" * 40).head_sha == "f" * 40
    assert gh.get_pull(pr.number).head_sha == "f" * 40
    assert gh.fake_set_mergeable(pr.number, None).mergeable is None


def test_lists_are_ordered_by_creation(gh: FakeGitHub) -> None:
    numbers = [gh.create_issue(f"i{n}", "").number for n in range(3)]
    pulls = [gh.create_pull(f"p{n}", "", f"task/{n}", "main").number for n in range(3)]
    assert [i.number for i in gh.list_issues()] == numbers
    assert [p.number for p in gh.list_pulls()] == pulls
    assert [m.number for m in gh.list_milestones()] == [1]


def test_reviewers_and_reviews(gh: FakeGitHub) -> None:
    pr = gh.create_pull("p", "", "task/1", "main")
    gh.request_reviewers(pr.number, ["bob", "carol"])
    assert gh.get_pull(pr.number).requested_reviewers == ("bob", "carol")
    with pytest.raises(GitHubError):
        gh.request_reviewers(pr.number, ["alice"])
    with pytest.raises(GitHubError, match="own pull request"):
        gh.submit_review(pr.number, "APPROVE", "lgtm")
    gh.submit_review(pr.number, "COMMENT", "self note")
    gh.fake_set_user("bob")
    review = gh.submit_review(pr.number, "REQUEST_CHANGES", "fix it")
    assert (review.author, review.state, review.pr_number) == ("bob", "CHANGES_REQUESTED", 1)
    assert gh.get_pull(pr.number).requested_reviewers == ("carol",)
    gh.fake_set_user("carol")
    gh.submit_review(pr.number, "APPROVE", "ok")
    assert [r.state for r in gh.list_reviews(pr.number)] == [
        "COMMENTED",
        "CHANGES_REQUESTED",
        "APPROVED",
    ]


def test_pull_files(gh: FakeGitHub) -> None:
    pr = gh.create_pull("p", "", "task/1", "main")
    assert gh.pull_files(pr.number) == []
    gh.fake_add_pull_files(pr.number, ["api/a.py", "db/b.sql"])
    assert gh.pull_files(pr.number) == ["api/a.py", "db/b.sql"]
    with pytest.raises(GitHubError):
        gh.pull_files(99)


def test_merge_closes_linked_issues(gh: FakeGitHub) -> None:
    one = gh.create_issue("one", "")
    two = gh.create_issue("two", "")
    untouched = gh.create_issue("three", "")
    pr = gh.create_pull("p", f"Closes #{one.number}\nfixes #{two.number}\nsee #3", "task/1", "main")
    merged = gh.fake_merge_pull(pr.number)
    assert (merged.state, merged.merged) == ("closed", True)
    assert merged.merged_at is not None
    for n in (one.number, two.number):
        issue = gh.get_issue(n)
        assert (issue.state, issue.state_reason) == ("closed", "completed")
        assert issue.closed_at is not None
    assert gh.get_issue(untouched.number).state == "open"
    assert gh.list_pulls(state="closed")[0].number == pr.number
    with pytest.raises(GitHubError):
        gh.fake_merge_pull(pr.number)


def test_missing_pull_fails(gh: FakeGitHub) -> None:
    with pytest.raises(GitHubError):
        gh.get_pull(5)


def test_merge_into_non_default_branch_keeps_issues_open() -> None:
    gh = FakeGitHub(default_branch="develop")
    issue = gh.create_issue("one", "")
    pr = gh.create_pull("p", f"Closes #{issue.number}", "task/1", "main")
    gh.fake_merge_pull(pr.number)
    assert gh.get_issue(issue.number).state == "open"
    pr2 = gh.create_pull("p2", f"Closes #{issue.number}", "task/2", "develop")
    gh.fake_merge_pull(pr2.number)
    assert gh.get_issue(issue.number).state == "closed"


def test_merge_refuses_draft_and_unmergeable(gh: FakeGitHub) -> None:
    draft = gh.create_pull("p", "", "task/1", "main", draft=True)
    with pytest.raises(GitHubError, match="draft") as exc:
        gh.fake_merge_pull(draft.number)
    assert exc.value.status == 405
    assert gh.get_pull(draft.number).state == "open"
    gh.fake_set_draft(draft.number, False)
    gh.fake_set_mergeable(draft.number, False)
    with pytest.raises(GitHubError, match="not mergeable"):
        gh.fake_merge_pull(draft.number)
    gh.fake_set_mergeable(draft.number, True)
    assert gh.fake_merge_pull(draft.number).merged is True


def test_list_pulls_does_not_compute_mergeable(gh: FakeGitHub) -> None:
    pr = gh.create_pull("p", "", "task/1", "main")
    assert gh.get_pull(pr.number).mergeable is True
    assert [p.mergeable for p in gh.list_pulls()] == [None]


# ---------------------------------------------------------------- CI


def test_commit_ci_defaults_to_none(gh: FakeGitHub) -> None:
    assert gh.commit_ci("a" * 40) == CiStatus("none", 0, (), ())


def test_fake_set_ci(gh: FakeGitHub) -> None:
    failing = CiStatus("failure", 3, ("build",), ("lint",))
    gh.fake_set_ci("a" * 40, failing)
    assert gh.commit_ci("a" * 40) == failing
    assert gh.commit_ci("b" * 40) == CiStatus("none", 0, (), ())
    passing = CiStatus("success", 1, (), ())
    gh.fake_set_ci("a" * 40, passing)
    assert gh.commit_ci("a" * 40) == passing
