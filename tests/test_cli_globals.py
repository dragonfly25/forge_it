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
