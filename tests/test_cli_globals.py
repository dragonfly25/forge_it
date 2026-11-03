"""Tests for the root callback's global options and the command output helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from forge import __version__
from forge.cli import app, main
from forge.commands._output import GlobalOptions, emit, get_options, run_command
from forge.core.errors import ExitCode, ForgeError, PreconditionError
from forge.core.output import Result

runner = CliRunner()


def make_probe_app(captured: list[GlobalOptions]) -> typer.Typer:
    """An app reusing the real root callback, with one command that records ctx.obj."""
    probe = typer.Typer(add_completion=False)
    probe.callback(invoke_without_command=True)(main)

    @probe.command()
    def show(ctx: typer.Context) -> None:
        captured.append(get_options(ctx))

    @probe.command()
    def other() -> None:  # a second command keeps Typer in group mode
        pass

    return probe


@pytest.mark.parametrize(
    "args", [["--json", "--version"], ["--version", "--json"], ["--json", "-V"]]
)
def test_json_version_envelope_any_order(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "ok": True,
        "code": 0,
        "data": {"version": __version__},
        "messages": [],
    }


def test_plain_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"forge {__version__}"


def test_help_lists_global_options() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for opt in ("--json", "--agent", "--repo", "--verbose", "--yes", "--version"):
        assert opt in result.output


def test_no_args_shows_usage_and_exits_zero() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_ctx_obj_populated() -> None:
    captured: list[GlobalOptions] = []
    args = ["--json", "--agent", "claude", "--repo", "/tmp/x", "-v", "-y", "show"]
    result = runner.invoke(make_probe_app(captured), args)
    assert result.exit_code == 0, result.output
    assert captured == [
        GlobalOptions(json=True, agent="claude", repo=Path("/tmp/x"), verbose=True, yes=True)
    ]


def test_ctx_obj_defaults() -> None:
    captured: list[GlobalOptions] = []
    result = runner.invoke(make_probe_app(captured), ["show"], env={"FORGE_AGENT": None})
    assert result.exit_code == 0, result.output
    assert captured == [GlobalOptions()]


def test_agent_from_env_and_flag_wins() -> None:
    captured: list[GlobalOptions] = []
    probe = make_probe_app(captured)
    runner.invoke(probe, ["show"], env={"FORGE_AGENT": "codex"})
    runner.invoke(probe, ["--agent", "claude", "show"], env={"FORGE_AGENT": "codex"})
    assert [c.agent for c in captured] == ["codex", "claude"]


# -- emit / run_command ----------------------------------------------------------------------


def run_in_app(fn: object, opts: GlobalOptions) -> tuple[int, str, str]:
    """Run ``run_command(opts, fn)`` inside a throwaway Typer command."""
    probe = typer.Typer(add_completion=False)

    @probe.command()
    def cmd() -> None:
        run_command(opts, fn)  # type: ignore[arg-type]

    res = CliRunner().invoke(probe, [])
    if res.exception and not isinstance(res.exception, SystemExit):
        raise res.exception
    return res.exit_code, res.stdout, res.stderr


def test_run_command_json_success() -> None:
    def fn() -> Result:
        r = Result(data={"n": 1})
        r.warn("careful", hint="look")
        return r

    code, out, err = run_in_app(fn, GlobalOptions(json=True))
    assert code == 0 and err == ""
    env = json.loads(out)
    assert env["data"] == {"n": 1}
    assert env["messages"] == [{"level": "warning", "text": "careful", "hint": "look"}]


def test_run_command_forge_error_json() -> None:
    def fn() -> Result:
        raise PreconditionError("dirty tree", hint="commit first")

    code, out, _ = run_in_app(fn, GlobalOptions(json=True))
    assert code == ExitCode.PRECONDITION
    env = json.loads(out)
    assert env["ok"] is False and env["code"] == 5
    assert env["messages"] == [{"level": "error", "text": "dirty tree", "hint": "commit first"}]


def test_run_command_forge_error_text() -> None:
    def fn() -> Result:
        raise ForgeError("boom", hint="retry")

    code, out, err = run_in_app(fn, GlobalOptions())
    assert code == 1
    assert out == ""
    assert "error: boom" in err and "Fix: retry" in err


def test_run_command_result_code_used() -> None:
    code, _, _ = run_in_app(lambda: Result(code=ExitCode.NOTHING_AVAILABLE), GlobalOptions())
    assert code == 3


def test_run_command_unexpected_exception_propagates() -> None:
    def fn() -> Result:
        raise ValueError("bug")

    with pytest.raises(ValueError, match="bug"):
        run_in_app(fn, GlobalOptions())


def test_emit_text_mode(capsys: pytest.CaptureFixture[str]) -> None:
    r = Result(data={"version": "1.0"})
    r.info("hello")
    emit(r, GlobalOptions())
    captured = capsys.readouterr()
    assert "version" in captured.out and "1.0" in captured.out
    assert "info: hello" in captured.err
    assert "hello" not in captured.out


@pytest.mark.parametrize(
    "args",
    [["no-such-command"], ["--no-such-option"], ["escalate"], ["task", "show", "--bogus"]],
    ids=["unknown-command", "unknown-root-option", "missing-option", "nested-bad-option"],
)
def test_usage_errors_exit_1_not_the_agent_action_code(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == ExitCode.ERROR


def test_usage_error_base_class_is_checked_at_import() -> None:
    """forge.cli takes click's UsageError from Typer's BadParameter MRO and fails loudly if
    a Typer release changes that hierarchy."""
    from forge import cli

    assert cli._UsageError.__name__ == "UsageError"
    assert issubclass(typer.BadParameter, cli._UsageError)
    assert cli._usage_error_class(typer.BadParameter) is cli._UsageError

    class SomethingElse(Exception):
        pass

    class BadParameter(SomethingElse):
        pass

    with pytest.raises(ImportError, match=r"UsageError.*SomethingElse"):
        cli._usage_error_class(BadParameter)


# -- global options after the subcommand (spec §12, §16) --------------------------------------

Captured = list[tuple[GlobalOptions, dict[str, object]]]


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Captured:
    """Stop the real commands right after parsing and record their options and params."""
    from forge.commands import _output
    from forge.commands import next as next_mod
    from forge.commands import plan as plan_mod
    from forge.commands import propose as propose_mod
    from forge.commands import setup as setup_mod
    from forge.commands import status as status_mod
    from forge.commands import task as task_mod

    records: Captured = []

    def record(ctx: typer.Context) -> GlobalOptions:
        opts = _output.get_options(ctx)
        records.append((opts, dict(ctx.params)))
        raise typer.Exit(0)

    for module in (next_mod, plan_mod, propose_mod, setup_mod, status_mod, task_mod):
        monkeypatch.setattr(module, "get_options", record)
    return records


def invoke(args: list[str], captured: Captured) -> tuple[GlobalOptions, dict[str, object]]:
    result = runner.invoke(app, args, env={"FORGE_AGENT": None})
    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["status", "--json"], GlobalOptions(json=True)),
        (["status", "--verbose"], GlobalOptions(verbose=True)),
        (["status", "-v"], GlobalOptions(verbose=True)),
        (["status", "--yes"], GlobalOptions(yes=True)),
        (["status", "-y"], GlobalOptions(yes=True)),
        (["status", "--agent", "a"], GlobalOptions(agent="a")),
        (["status", "--repo", "/tmp/r"], GlobalOptions(repo=Path("/tmp/r"))),
        (["next", "--agent", "a", "--json"], GlobalOptions(json=True, agent="a")),
        (["next", "--agent=a", "--repo=/tmp/r"], GlobalOptions(agent="a", repo=Path("/tmp/r"))),
        (["plan", "validate", "M1", "--json"], GlobalOptions(json=True)),
        (["task", "show", "42", "--json", "-v"], GlobalOptions(json=True, verbose=True)),
        (["task", "show", "--agent", "b", "42"], GlobalOptions(agent="b")),
        (["--json", "status", "-v"], GlobalOptions(json=True, verbose=True)),
    ],
)
def test_global_options_after_the_subcommand(
    args: list[str], expected: GlobalOptions, captured: Captured
) -> None:
    opts, _ = invoke(args, captured)
    assert opts == expected


def test_nested_command_keeps_its_positional_argument(captured: Captured) -> None:
    opts, params = invoke(["task", "show", "42", "--json"], captured)
    assert opts.json and params["task"] == "42"
    opts, params = invoke_again(["plan", "validate", "M1", "--agent=x"], captured)
    assert opts.agent == "x" and params["milestone"] == "M1"


def invoke_again(args: list[str], captured: Captured) -> tuple[GlobalOptions, dict[str, object]]:
    captured.clear()
    return invoke(args, captured)


def test_leaf_own_option_is_not_hoisted(captured: Captured) -> None:
    # `forge setup` defines its own --yes/-y: it stays the command's option.
    opts, params = invoke(["setup", "--yes", "--json"], captured)
    assert opts == GlobalOptions(json=True)
    assert params["yes"] is True
    opts, params = invoke_again(["setup", "-y"], captured)
    assert opts == GlobalOptions() and params["yes"] is True


def test_value_of_a_leaf_option_that_looks_like_a_flag(captured: Captured) -> None:
    opts, params = invoke(["propose", "--title", "--json", "--body-file", "b.md"], captured)
    assert opts == GlobalOptions()
    assert params["title"] == "--json"
    opts, params = invoke_again(["propose", "--title=--agent", "--body-file", "b.md"], captured)
    assert opts == GlobalOptions() and params["title"] == "--agent"


def test_nothing_after_double_dash_is_hoisted() -> None:
    from forge.cli import hoist_global_options

    group = typer.main.get_command(app)
    ctx = group.make_context("forge", ["--version"])
    assert hoist_global_options(group, ctx, ["task", "show", "--", "--json"]) == [
        "task",
        "show",
        "--",
        "--json",
    ]
    assert hoist_global_options(group, ctx, ["next", "--json", "--", "-v"]) == [
        "--json",
        "next",
        "--",
        "-v",
    ]


def test_double_dash_value_reaches_the_command(captured: Captured) -> None:
    opts, params = invoke(["task", "show", "--json", "--", "--agent"], captured)
    assert opts == GlobalOptions(json=True)
    assert params["task"] == "--agent"


@pytest.mark.parametrize(
    "args",
    [
        ["status", "--bogus", "--json"],
        ["task", "show", "--bogus"],
        ["next", "--agent"],  # no value: not hoisted, click reports it
        ["plan", "nope", "--json"],
        ["status", "-vy"],  # clusters are not split after the subcommand
    ],
)
def test_unknown_options_still_usage_error_exit_1(args: list[str], captured: Captured) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == ExitCode.ERROR
    assert captured == []


def test_before_the_subcommand_is_unchanged(captured: Captured) -> None:
    opts, _ = invoke(["--json", "--agent", "a", "-v", "-y", "next"], captured)
    assert opts == GlobalOptions(json=True, agent="a", verbose=True, yes=True)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--agent", "a", "next", "--agent", "b"], GlobalOptions(agent="b")),
        (["--repo", "/x", "status", "--repo", "/y"], GlobalOptions(repo=Path("/y"))),
        (["--agent", "a", "task", "show", "42", "--agent=b"], GlobalOptions(agent="b")),
    ],
)
def test_a_repeated_global_written_after_the_subcommand_wins(
    args: list[str], expected: GlobalOptions, captured: Captured
) -> None:
    opts, _ = invoke(args, captured)
    assert opts == expected


def test_global_value_option_never_takes_another_option_as_its_value() -> None:
    from forge.cli import hoist_global_options

    group = typer.main.get_command(app)
    ctx = group.make_context("forge", ["--version"])
    assert hoist_global_options(group, ctx, ["next", "--agent", "--json"]) == [
        "--json",
        "next",
        "--agent",
    ]
    assert hoist_global_options(group, ctx, ["next", "--agent", "--task", "4"]) == [
        "next",
        "--agent",
        "--task",
        "4",
    ]
