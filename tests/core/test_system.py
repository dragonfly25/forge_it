"""Tests for forge.core.system: operating-system differences (ADR 0021)."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from forge.core import system
from forge.core.system import (
    config_home,
    default_editor,
    editor_hint,
    is_windows,
    replace_file,
    resolve_argv,
    same_text,
    state_home,
    utf8_stdio,
)


def test_is_windows() -> None:
    assert is_windows("win32")
    assert not is_windows("linux") and not is_windows("darwin") and not is_windows("cygwin")
    assert is_windows() == (sys.platform == "win32")


# --------------------------------------------------------------------------- directories


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_xdg_wins_on_every_platform(tmp_path: Path, platform: str) -> None:
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "HOME": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
    }
    assert config_home(env, platform) == tmp_path / "cfg"
    assert state_home(env, platform) == tmp_path / "state"


@pytest.mark.parametrize("xdg", ["", "relative"])
def test_posix_defaults(tmp_path: Path, xdg: str) -> None:
    env = {"XDG_CONFIG_HOME": xdg, "XDG_STATE_HOME": xdg, "HOME": str(tmp_path)}
    assert config_home(env, "linux") == tmp_path / ".config"
    assert state_home(env, "darwin") == tmp_path / ".local" / "state"


def test_posix_defaults_without_home() -> None:
    assert config_home({}, "linux") == Path.home() / ".config"
    assert state_home({}, "linux") == Path.home() / ".local" / "state"


def test_windows_defaults(tmp_path: Path) -> None:
    env = {
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "HOME": "/ignored/on/windows",
    }
    assert config_home(env, "win32") == tmp_path / "Roaming"
    assert state_home(env, "win32") == tmp_path / "Local"
    fallback = {"APPDATA": "relative", "LOCALAPPDATA": ""}
    assert config_home(fallback, "win32") == Path.home() / "AppData" / "Roaming"
    assert state_home(fallback, "win32") == Path.home() / "AppData" / "Local"


# --------------------------------------------------------------------------- executables


def test_resolve_argv_finds_a_program_through_path(tmp_path: Path) -> None:
    resolved = resolve_argv([Path(sys.executable).name, "--version"])
    assert Path(resolved[0]).is_absolute() and resolved[1:] == ["--version"]


def test_resolve_argv_finds_cmd_shims_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What makes ``codex``/``claude`` installed by npm (``codex.cmd``) visible on Windows."""
    shim = tmp_path / ("codex.cmd" if is_windows() else "codex")
    shim.write_text("@echo off\n" if is_windows() else "#!/bin/sh\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    program, *rest = resolve_argv(["codex", "--version"])
    assert os.path.normcase(program) == os.path.normcase(str(shim))  # PATHEXT may be upper-case
    assert rest == ["--version"]


def test_resolve_argv_leaves_unknown_programs_and_paths_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_argv(["no-such-tool", "x"]) == ["no-such-tool", "x"]
    explicit = str(tmp_path / "bin" / "tool")
    assert resolve_argv([explicit]) == [explicit]
    assert resolve_argv([]) == []


def test_default_editor_and_hint() -> None:
    assert default_editor("win32") == ["notepad"]
    assert default_editor("linux") == ["vi"] == default_editor("darwin")
    assert "$env:EDITOR" in editor_hint("win32")
    assert "export EDITOR" in editor_hint("linux")


# --------------------------------------------------------------------------- files


def test_same_text_ignores_only_crlf() -> None:
    assert same_text("a\nb\n", "a\r\nb\r\n")
    assert same_text("a\r\nb\n", "a\nb\r\n")
    assert not same_text("a\nb\n", "a\nb")
    assert not same_text("a\nb\n", "a\nc\n")
    assert same_text(None, None)
    assert not same_text(None, "") and not same_text("", None)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_replace_file(tmp_path: Path, platform: str) -> None:
    source, target = tmp_path / "new", tmp_path / "old"
    source.write_text("new")
    target.write_text("old")
    replace_file(source, target, platform)
    assert target.read_text() == "new" and not source.exists()


def test_replace_file_retries_while_windows_holds_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "new", tmp_path / "old"
    source.write_text("new")
    real = os.replace
    failures = [PermissionError(13, "in use"), PermissionError(13, "in use")]

    def flaky(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> None:
        if failures:
            raise failures.pop()
        real(a, b)

    monkeypatch.setattr(system.os, "replace", flaky)
    replace_file(source, target, "win32")
    assert target.read_text() == "new" and not failures


def test_replace_file_gives_up_after_the_retry_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(a: object, b: object) -> None:
        raise PermissionError(13, "in use")

    monkeypatch.setattr(system.os, "replace", busy)
    monkeypatch.setattr(system, "REPLACE_RETRY_SECONDS", 0.1)
    with pytest.raises(PermissionError):
        replace_file(tmp_path / "a", tmp_path / "b", "win32")
    calls: list[int] = []

    def once(a: object, b: object) -> None:
        calls.append(1)
        raise PermissionError(13, "in use")

    monkeypatch.setattr(system.os, "replace", once)
    with pytest.raises(PermissionError):
        replace_file(tmp_path / "a", tmp_path / "b", "linux")
    assert calls == [1]  # never retried off Windows


# --------------------------------------------------------------------------- streams


def _stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


def test_utf8_stdio_reconfigures_on_windows_only(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _stream(), _stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    utf8_stdio("linux")
    assert out.encoding == "cp1252"
    utf8_stdio("win32")
    assert out.encoding == "utf-8" and err.encoding == "utf-8"
    out.write("→ ✓ 日本\n")  # would raise UnicodeEncodeError with cp1252
    out.flush()
    assert out.buffer.getvalue() == "→ ✓ 日本\n".encode()


def test_utf8_stdio_leaves_streams_without_reconfigure_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = io.StringIO()
    monkeypatch.setattr(sys, "stdout", plain)
    utf8_stdio("win32")
    assert sys.stdout is plain


def test_same_path(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    assert system.same_path(tmp_path / "a", tmp_path / "a" / ".." / "a")
    assert not system.same_path(tmp_path / "a", tmp_path / "b")
    if is_windows():
        assert system.same_path(str(tmp_path / "a").upper(), str(tmp_path / "a").lower())
        assert system.same_path((tmp_path / "a").as_posix(), str(tmp_path / "a"))
