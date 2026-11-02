"""Tests for GhCliClient with a recording fake runner (no network, no real gh)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from forge.core.errors import ExitCode, PreconditionError
from forge.core.github import (
    MIN_GH_VERSION,
    CiStatus,
    GhCliClient,
    GitHubClient,
    GitHubError,
    parse_gh_version,
)

REPO = "acme/shop"
TS = "2026-03-01T10:00:00Z"


@dataclass
class Call:
    argv: list[str]
    stdin: str | None

    @property
    def body(self) -> Any:
        return json.loads(self.stdin) if self.stdin is not None else None


@dataclass
class FakeRunner:
    """Returns canned responses in order and records every call."""

    responses: list[tuple[int, str, str]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def add(self, data: Any, code: int = 0, err: str = "") -> FakeRunner:
        self.responses.append((code, "" if data is None else json.dumps(data), err))
        return self

    def __call__(self, argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        self.calls.append(Call(list(argv), stdin))
        return self.responses.pop(0)


def issue_json(number: int, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "number": number,
        "title": f"issue {number}",
        "body": None,
        "state": "open",
        "state_reason": None,
        "labels": [{"name": "forge:task"}],
        "assignees": [{"login": "bob"}],
        "milestone": {"number": 3, "title": "M1"},
        "user": {"login": "alice"},
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "created_at": TS,
        "updated_at": TS,
        "closed_at": None,
    }
    data.update(extra)
    return data


def pull_json(number: int, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "number": number,
        "title": "p",
        "body": "Closes #1",
        "head": {"ref": "task/1", "sha": "abc123"},
        "base": {"ref": "main"},
        "state": "open",
        "merged_at": None,
        "draft": False,
        "user": {"login": "alice"},
        "requested_reviewers": [{"login": "carol"}],
        "labels": [{"name": "forge:task"}],
        "assignees": [{"login": "bob"}],
        "html_url": f"https://github.com/{REPO}/pull/{number}",
    }
    data.update(extra)
    return data


def client(runner: FakeRunner) -> GhCliClient:
    return GhCliClient(REPO, runner=runner)


def test_satisfies_protocol() -> None:
    assert isinstance(client(FakeRunner()), GitHubClient)


def test_invalid_repo() -> None:
    with pytest.raises(GitHubError):
        GhCliClient("not-a-repo")


def test_current_user() -> None:
    runner = FakeRunner().add({"login": "alice"})
    assert client(runner).current_user() == "alice"
    assert runner.calls[0].argv == ["gh", "api", "user"]


def test_list_labels_paginates_and_flattens() -> None:
    pages = [[{"name": "a", "color": "fff", "description": None}], [{"name": "b", "color": "000"}]]
    runner = FakeRunner().add(pages)
    labels = client(runner).list_labels()
    assert [lb.name for lb in labels] == ["a", "b"]
    assert labels[0].description == ""
    assert runner.calls[0].argv == [
        "gh",
        "api",
        "--paginate",
        "--slurp",
        "repos/acme/shop/labels?per_page=100",
    ]
    assert runner.calls[0].stdin is None


def test_list_labels_sorted_by_name() -> None:
    runner = FakeRunner().add([[{"name": "b"}, {"name": "A"}, {"name": "c"}]])
    assert [lb.name for lb in client(runner).list_labels()] == ["A", "b", "c"]


def test_create_and_update_label() -> None:
    runner = FakeRunner().add({"name": "x", "color": "00ff00", "description": "d"})
    runner.add({"name": "status:ready", "color": "111111", "description": ""})
    gh = client(runner)
    gh.create_label("x", "#00ff00", "d")
    gh.update_label("status:ready", color="111111")
    create, update = runner.calls
    assert create.argv == ["gh", "api", "-X", "POST", "repos/acme/shop/labels", "--input", "-"]
    assert create.body == {"name": "x", "color": "00ff00", "description": "d"}
    assert update.argv[2:5] == ["-X", "PATCH", "repos/acme/shop/labels/status%3Aready"]
    assert update.body == {"color": "111111"}


def test_milestones() -> None:
    runner = FakeRunner().add([[{"number": 3, "title": "M1", "state": "open"}]])
    runner.add({"number": 3, "title": "M1", "state": "closed", "description": "x"})
    gh = client(runner)
    assert gh.list_milestones()[0].title == "M1"
    closed = gh.close_milestone(3)
    assert closed.state == "closed"
    assert runner.calls[0].argv[-1] == "repos/acme/shop/milestones?state=all&per_page=100"
    assert runner.calls[1].argv[2:5] == ["-X", "PATCH", "repos/acme/shop/milestones/3"]
    assert runner.calls[1].body == {"state": "closed"}


def test_list_milestones_sorted_by_number_with_due_on() -> None:
    rows = [
        {"number": 5, "title": "M2", "due_on": "2026-06-30T07:00:00Z"},
        {"number": 2, "title": "M1"},
    ]
    runner = FakeRunner().add([rows])
    milestones = client(runner).list_milestones()
    assert [m.number for m in milestones] == [2, 5]
    assert milestones[0].due_on is None
    assert milestones[1].due_on == datetime(2026, 6, 30, 7, tzinfo=UTC)


def test_update_milestone() -> None:
    updated = {"number": 3, "title": "M1b", "state": "open", "due_on": "2026-07-01T10:00:00Z"}
    runner = FakeRunner().add(updated).add(updated)
    gh = client(runner)
    due = datetime(2026, 7, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    milestone = gh.update_milestone(3, title="M1b", description="d", state="open", due_on=due)
    assert milestone.due_on == datetime(2026, 7, 1, 10, tzinfo=UTC)
    call = runner.calls[0]
    assert call.argv == ["gh", "api", "-X", "PATCH", "repos/acme/shop/milestones/3", "--input", "-"]
    assert call.body == {
        "title": "M1b",
        "description": "d",
        "state": "open",
        "due_on": "2026-07-01T10:00:00Z",
    }
    gh.update_milestone(3, title="M1b")
    assert runner.calls[1].body == {"title": "M1b"}


def test_update_milestone_rejects_naive_due_on() -> None:
    runner = FakeRunner()
    with pytest.raises(GitHubError, match="naive"):
        client(runner).update_milestone(3, due_on=datetime(2026, 7, 1))
    assert runner.calls == []


def test_list_issues_filters_out_pulls_and_maps_fields() -> None:
    rows = [issue_json(1), {**issue_json(2), "pull_request": {"url": "x"}}]
    runner = FakeRunner().add([rows])
    issues = client(runner).list_issues(labels=["forge:task", "status:ready"])
    assert [i.number for i in issues] == [1]
    issue = issues[0]
    assert issue.labels == ("forge:task",)
    assert issue.assignees == ("bob",)
    assert issue.milestone == "M1"
    assert issue.author == "alice"
    assert issue.body == ""
    assert issue.created_at.tzinfo is not None
    assert runner.calls[0].argv == [
        "gh",
        "api",
        "--paginate",
        "--slurp",
        "repos/acme/shop/issues?state=open&sort=created&direction=asc&per_page=100"
        "&labels=forge:task,status:ready",
    ]


def test_list_issues_resolves_milestone_title_to_number() -> None:
    runner = FakeRunner()
    runner.add([[{"number": 2, "title": "M0", "state": "closed"}, {"number": 3, "title": "M1"}]])
    runner.add([[issue_json(1)]])
    client(runner).list_issues(state="all", milestone="M1")
    assert runner.calls[0].argv[-1] == "repos/acme/shop/milestones?state=all&per_page=100"
    assert runner.calls[1].argv[-1] == (
        "repos/acme/shop/issues?state=all&sort=created&direction=asc&per_page=100&milestone=3"
    )


def test_list_issues_unknown_milestone_returns_empty() -> None:
    runner = FakeRunner().add([[]])
    assert client(runner).list_issues(milestone="M9") == []
    assert len(runner.calls) == 1


def test_get_issue_rejects_pull_request() -> None:
    runner = FakeRunner().add({**issue_json(5), "pull_request": {}})
    with pytest.raises(GitHubError, match="pull request"):
        client(runner).get_issue(5)


def test_create_issue_body_json() -> None:
    runner = FakeRunner().add([[{"number": 3, "title": "M1"}]]).add(issue_json(7))
    issue = client(runner).create_issue(
        "[M1-T01] x", "body", labels=["forge:task"], milestone="M1", assignees=["bob"]
    )
    assert issue.number == 7
    call = runner.calls[1]
    assert call.argv == ["gh", "api", "-X", "POST", "repos/acme/shop/issues", "--input", "-"]
    assert call.body == {
        "title": "[M1-T01] x",
        "body": "body",
        "labels": ["forge:task"],
        "assignees": ["bob"],
        "milestone": 3,
    }


def test_create_issue_unknown_milestone_fails() -> None:
    runner = FakeRunner().add([[]])
    with pytest.raises(GitHubError, match="M9"):
        client(runner).create_issue("t", "b", milestone="M9")


def test_update_issue_patch_with_state_reason() -> None:
    closed = issue_json(4, state="closed", state_reason="not_planned", closed_at=TS)
    runner = FakeRunner().add(closed)
    issue = client(runner).update_issue(4, state="closed", state_reason="not_planned")
    assert (issue.state, issue.state_reason) == ("closed", "not_planned")
    assert issue.closed_at is not None
    call = runner.calls[0]
    assert call.argv == ["gh", "api", "-X", "PATCH", "repos/acme/shop/issues/4", "--input", "-"]
    assert call.body == {"state": "closed", "state_reason": "not_planned"}


def test_update_issue_clears_milestone() -> None:
    runner = FakeRunner().add(issue_json(4, milestone=None))
    assert client(runner).update_issue(4, milestone=None).milestone is None
    assert runner.calls[0].body == {"milestone": None}


def test_label_and_assignee_mutations() -> None:
    runner = FakeRunner()
    for _ in range(5):
        runner.add([])
    gh = client(runner)
    gh.add_labels(4, ["status:ready"])
    gh.set_labels(4, ["a", "b"])
    gh.remove_label(4, "status:ready")
    gh.add_assignees(4, ["bob"])
    gh.remove_assignees(4, ["bob"])
    methods = [(c.argv[3], c.argv[4]) for c in runner.calls]
    assert methods == [
        ("POST", "repos/acme/shop/issues/4/labels"),
        ("PUT", "repos/acme/shop/issues/4/labels"),
        ("DELETE", "repos/acme/shop/issues/4/labels/status%3Aready"),
        ("POST", "repos/acme/shop/issues/4/assignees"),
        ("DELETE", "repos/acme/shop/issues/4/assignees"),
    ]
    assert runner.calls[0].body == {"labels": ["status:ready"]}
    assert runner.calls[2].stdin is None
    assert runner.calls[4].body == {"assignees": ["bob"]}


def test_remove_absent_label_is_not_an_error() -> None:
    runner = FakeRunner().add(None, code=1, err="gh: Label does not exist (HTTP 404)")
    client(runner).remove_label(4, "status:ready")


def test_remove_label_other_errors_propagate() -> None:
    runner = FakeRunner().add(None, code=1, err="gh: Server Error (HTTP 500)")
    with pytest.raises(GitHubError) as exc:
        client(runner).remove_label(4, "x")
    assert exc.value.status == 500


def test_comments() -> None:
    comment = {
        "id": 11,
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/4",
        "body": "hi",
        "user": {"login": "alice"},
        "created_at": TS,
        "updated_at": "2026-03-01T11:00:00Z",
    }
    runner = FakeRunner().add([[comment]]).add(comment).add(comment)
    gh = client(runner)
    listed = gh.list_comments(4)
    assert listed[0].issue_number == 4
    assert listed[0].updated_at > listed[0].created_at
    gh.create_comment(4, "hi")
    gh.update_comment(11, "edited")
    assert runner.calls[0].argv[-1] == "repos/acme/shop/issues/4/comments?per_page=100"
    assert runner.calls[1].argv[2:5] == ["-X", "POST", "repos/acme/shop/issues/4/comments"]
    assert runner.calls[2].argv[2:5] == ["-X", "PATCH", "repos/acme/shop/issues/comments/11"]
    assert runner.calls[2].body == {"body": "edited"}


def test_delete_comment() -> None:
    runner = FakeRunner().add(None)
    client(runner).delete_comment(11)
    call = runner.calls[0]
    assert call.argv == ["gh", "api", "-X", "DELETE", "repos/acme/shop/issues/comments/11"]
    assert call.stdin is None


def test_list_pulls_head_filter_prefixes_owner() -> None:
    runner = FakeRunner().add([[pull_json(9, merged_at=TS, state="closed")]])
    pulls = client(runner).list_pulls(state="all", head="task/1", base="main")
    assert runner.calls[0].argv[-1] == (
        "repos/acme/shop/pulls?state=all&sort=created&direction=asc&per_page=100"
        "&head=acme:task/1&base=main"
    )
    pr = pulls[0]
    assert (pr.head, pr.base, pr.merged, pr.state) == ("task/1", "main", True, "closed")
    assert pr.requested_reviewers == ("carol",)
    assert (pr.head_sha, pr.labels, pr.assignees) == ("abc123", ("forge:task",), ("bob",))
    assert pr.merged_at == datetime(2026, 3, 1, 10, tzinfo=UTC)
    assert pr.mergeable is None


def test_pull_mergeable_and_open_fields() -> None:
    runner = FakeRunner().add(pull_json(9, mergeable=False)).add(pull_json(9, mergeable=True))
    gh = client(runner)
    first = gh.get_pull(9)
    assert (first.mergeable, first.merged, first.merged_at) == (False, False, None)
    assert gh.get_pull(9).mergeable is True


def test_pull_create_update_reviewers() -> None:
    runner = FakeRunner().add(pull_json(9)).add(pull_json(9)).add(pull_json(9))
    gh = client(runner)
    gh.create_pull("p", "Closes #1", "task/1", "main", draft=True)
    gh.update_pull(9, body="new")
    gh.request_reviewers(9, ["bob"])
    create, update, req = runner.calls
    assert create.argv == ["gh", "api", "-X", "POST", "repos/acme/shop/pulls", "--input", "-"]
    assert create.body == {
        "title": "p",
        "body": "Closes #1",
        "head": "task/1",
        "base": "main",
        "draft": True,
    }
    assert update.argv[2:5] == ["-X", "PATCH", "repos/acme/shop/pulls/9"]
    assert update.body == {"body": "new"}
    assert req.argv[4] == "repos/acme/shop/pulls/9/requested_reviewers"
    assert req.body == {"reviewers": ["bob"]}


def test_reviews_and_files() -> None:
    review = {"id": 5, "user": {"login": "bob"}, "state": "APPROVED", "body": "ok"}
    runner = FakeRunner().add(review).add([[review]]).add([[{"filename": "a.py"}], []])
    gh = client(runner)
    submitted = gh.submit_review(9, "APPROVE", "ok")
    assert (submitted.pr_number, submitted.state, submitted.author) == (9, "APPROVED", "bob")
    assert runner.calls[0].body == {"event": "APPROVE", "body": "ok"}
    assert gh.list_reviews(9)[0].id == 5
    assert gh.pull_files(9) == ["a.py"]
    assert runner.calls[2].argv[-1] == "repos/acme/shop/pulls/9/files?per_page=100"


def test_get_pull_merged_flag() -> None:
    runner = FakeRunner().add(pull_json(9, merged=True, merged_at=TS))
    assert client(runner).get_pull(9).merged is True


# ---------------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    "stderr",
    [
        "To get started with GitHub CLI, please run:  gh auth login",
        "gh: Requires authentication (HTTP 401)",
    ],
)
def test_auth_failure_is_precondition(stderr: str) -> None:
    runner = FakeRunner().add(None, code=4, err=stderr)
    with pytest.raises(PreconditionError) as exc:
        client(runner).current_user()
    assert exc.value.code == ExitCode.PRECONDITION
    assert exc.value.hint == "gh auth login"


def test_generic_failure_is_github_error() -> None:
    runner = FakeRunner().add(None, code=1, err="gh: Validation Failed (HTTP 422)")
    with pytest.raises(GitHubError) as exc:
        client(runner).create_label("x", "000000")
    assert exc.value.status == 422
    assert exc.value.code == ExitCode.ERROR
    assert "Validation Failed" in str(exc.value)


def test_missing_gh_is_precondition() -> None:
    def runner(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        raise FileNotFoundError("gh")

    with pytest.raises(PreconditionError) as exc:
        GhCliClient(REPO, runner=runner).current_user()
    assert exc.value.code == ExitCode.PRECONDITION
    assert exc.value.hint is not None and "cli.github.com" in exc.value.hint


def test_invalid_json_is_github_error() -> None:
    runner = FakeRunner()
    runner.responses.append((0, "not json", ""))
    with pytest.raises(GitHubError, match="invalid JSON"):
        client(runner).current_user()


# ---------------------------------------------------------------- gh version


def test_parse_gh_version() -> None:
    assert parse_gh_version("gh version 2.92.0 (2026-04-28)\nhttps://github.com/...") == (
        2,
        92,
        0,
    )
    assert parse_gh_version("gh version 2.92.0 (2026-04-28)") >= MIN_GH_VERSION
    assert parse_gh_version("gh version 2.47.1 (2024-04-01)") < MIN_GH_VERSION


def test_min_gh_version_supports_slurp() -> None:
    assert MIN_GH_VERSION == (2, 48, 0)


def test_parse_gh_version_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="gh version"):
        parse_gh_version("command not found: gh")


# --------------------------------------------------------------------------- commit_ci

SHA = "a" * 40


def run_json(
    name: str, status: str = "completed", conclusion: str | None = "success", *, id: int = 1
) -> dict[str, Any]:
    return {"id": id, "name": name, "status": status, "conclusion": conclusion}


def status_json(context: str, state: str, *, id: int = 1) -> dict[str, Any]:
    return {"id": id, "context": context, "state": state}


def ci(
    runs: list[dict[str, Any]] | list[list[dict[str, Any]]],
    statuses: list[dict[str, Any]],
    *,
    combined_state: str = "pending",
) -> tuple[CiStatus, FakeRunner]:
    """Run commit_ci against canned pages. ``runs`` is one page, or a list of pages."""
    run_pages = runs if runs and isinstance(runs[0], list) else [runs]
    runner = FakeRunner()
    runner.add(
        [{"total_count": sum(len(p) for p in run_pages), "check_runs": p} for p in run_pages]
    )
    runner.add([{"state": combined_state, "total_count": len(statuses), "statuses": statuses}])
    return client(runner).commit_ci(SHA), runner


def test_commit_ci_calls_both_endpoints_paginated() -> None:
    _, runner = ci([], [])
    assert runner.calls[0].argv == [
        "gh",
        "api",
        "--paginate",
        "--slurp",
        f"repos/acme/shop/commits/{SHA}/check-runs?filter=latest&per_page=100",
    ]
    assert runner.calls[1].argv == [
        "gh",
        "api",
        "--paginate",
        "--slurp",
        f"repos/acme/shop/commits/{SHA}/status?per_page=100",
    ]


def test_commit_ci_nothing_is_none_despite_combined_pending() -> None:
    status, _ = ci([], [], combined_state="pending")
    assert status == CiStatus("none", 0, (), ())


def test_commit_ci_all_passing_is_success() -> None:
    runs = [
        run_json("build", id=1),
        run_json("lint", conclusion="neutral", id=2),
        run_json("deploy", conclusion="skipped", id=3),
        run_json("old", conclusion="stale", id=4),
    ]
    status, _ = ci(runs, [status_json("ci/legacy", "success")], combined_state="success")
    assert status == CiStatus("success", 5, (), ())


@pytest.mark.parametrize(
    "conclusion", ["failure", "timed_out", "cancelled", "action_required", "startup_failure"]
)
def test_commit_ci_failing_conclusions(conclusion: str) -> None:
    runs = [run_json("build", conclusion=conclusion, id=1), run_json("lint", id=2)]
    status, _ = ci(runs, [])
    assert status == CiStatus("failure", 2, ("build",), ())


@pytest.mark.parametrize("state", ["failure", "error"])
def test_commit_ci_failing_status(state: str) -> None:
    status, _ = ci([run_json("build")], [status_json("ci/jenkins", state)])
    assert status == CiStatus("failure", 2, ("ci/jenkins",), ())


@pytest.mark.parametrize("run_status", ["queued", "in_progress", "waiting", "requested", "pending"])
def test_commit_ci_unfinished_check_run_is_pending(run_status: str) -> None:
    status, _ = ci([run_json("build", run_status, None), run_json("lint")], [])
    assert status == CiStatus("pending", 2, (), ("build",))


def test_commit_ci_pending_status() -> None:
    status, _ = ci([run_json("build")], [status_json("ci/jenkins", "pending")])
    assert status == CiStatus("pending", 2, (), ("ci/jenkins",))


def test_commit_ci_failure_wins_over_pending_and_names_are_sorted() -> None:
    runs = [
        run_json("zeta", conclusion="failure", id=1),
        run_json("alpha", "in_progress", None, id=2),
        run_json("beta", conclusion="timed_out", id=3),
    ]
    status, _ = ci(runs, [status_json("ci/a", "error"), status_json("ci/b", "pending")])
    assert status == CiStatus("failure", 5, ("beta", "ci/a", "zeta"), ("alpha", "ci/b"))


def test_commit_ci_latest_check_run_per_name_wins() -> None:
    runs = [
        run_json("build", conclusion="failure", id=10),
        run_json("build", conclusion="success", id=12),  # re-run, newest
        run_json("lint", "in_progress", None, id=30),  # newest lint, still running
        run_json("lint", conclusion="success", id=20),
    ]
    status, _ = ci(runs, [])
    assert status == CiStatus("pending", 2, (), ("lint",))


def test_commit_ci_same_name_in_two_suites_are_distinct_checks() -> None:
    def suite_run(suite: int, app: int, conclusion: str, id: int) -> dict[str, Any]:
        return {
            **run_json("test", conclusion=conclusion, id=id),
            "app": {"id": app},
            "check_suite": {"id": suite},
        }

    runs = [
        suite_run(100, 1, "failure", 5),  # suite 100 (e.g. one workflow) fails
        suite_run(200, 1, "success", 9),  # suite 200 (another workflow), newer id, passes
        suite_run(300, 2, "success", 7),  # another app's "test"
    ]
    status, _ = ci(runs, [])
    assert status == CiStatus("failure", 3, ("test",), ())


def test_commit_ci_rerun_in_the_same_suite_still_replaces_the_old_run() -> None:
    runs = [
        {
            **run_json("test", conclusion="failure", id=5),
            "app": {"id": 1},
            "check_suite": {"id": 9},
        },
        {**run_json("test", id=6), "app": {"id": 1}, "check_suite": {"id": 9}},
    ]
    status, _ = ci(runs, [])
    assert status == CiStatus("success", 1, (), ())


def test_commit_ci_merges_check_run_pages() -> None:
    pages = [
        [run_json("a", id=1), run_json("b", id=2)],
        [run_json("c", conclusion="failure", id=3), run_json("a", "queued", None, id=4)],
    ]
    status, _ = ci(pages, [])
    assert status == CiStatus("failure", 3, ("c",), ("a",))


def test_commit_ci_merges_status_pages() -> None:
    runner = FakeRunner()
    runner.add([{"total_count": 0, "check_runs": []}])
    runner.add(
        [
            {"state": "failure", "statuses": [status_json("s1", "success", id=1)]},
            {"state": "failure", "statuses": [status_json("s2", "failure", id=2)]},
        ]
    )
    assert client(runner).commit_ci(SHA) == CiStatus("failure", 2, ("s2",), ())


def test_commit_ci_error_propagates() -> None:
    runner = FakeRunner().add(None, code=1, err="gh: Not Found (HTTP 404)")
    with pytest.raises(GitHubError) as info:
        client(runner).commit_ci(SHA)
    assert info.value.status == 404
