"""Static portability checks on ``src/forge`` (ADR 0021).

Forge runs on macOS, Linux and native Windows. On Windows Python's text defaults differ
(ANSI code page, CRLF line endings) and some calls behave differently (``os.kill`` kills,
``os.replace`` fails on open files). These checks keep every call that depends on those
defaults explicit, so the code behaves the same on all three platforms.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import forge

SRC = Path(forge.__file__).parent

SYSTEM_MODULE = SRC / "core" / "system.py"
LOCK_MODULE = SRC / "core" / "pending.py"
_SUBPROCESS = {"run", "Popen", "check_output", "check_call", "call"}


def _calls() -> Iterator[tuple[Path, ast.Call]]:
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_bytes().decode("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                yield path, node


def _where(path: Path, node: ast.AST) -> str:
    return f"{path.relative_to(SRC.parent).as_posix()}:{getattr(node, 'lineno', 0)}"


def _keywords(call: ast.Call) -> set[str]:
    return {k.arg for k in call.keywords if k.arg is not None}


def _attr(call: ast.Call) -> str | None:
    return call.func.attr if isinstance(call.func, ast.Attribute) else None


def _is_os(call: ast.Call, name: str) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == name
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    )


def _mode(call: ast.Call, position: int) -> str | None:
    if len(call.args) > position and isinstance(call.args[position], ast.Constant):
        value = call.args[position].value
        return value if isinstance(value, str) else None
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            return value if isinstance(value, str) else None
    return None


def test_text_reads_name_the_encoding() -> None:
    bad = [
        _where(path, call)
        for path, call in _calls()
        if _attr(call) == "read_text" and not call.args and "encoding" not in _keywords(call)
    ]
    assert not bad, "read_text() without encoding=TEXT_ENCODING: " + ", ".join(bad)


def test_text_writes_name_the_encoding_and_line_ending() -> None:
    bad = [
        _where(path, call)
        for path, call in _calls()
        if _attr(call) == "write_text" and not {"encoding", "newline"} <= _keywords(call)
    ]
    assert not bad, 'write_text() needs encoding= and newline="\\n": ' + ", ".join(bad)


def test_text_opens_name_the_encoding() -> None:
    bad = []
    for path, call in _calls():
        if _is_os(call, "open"):
            continue  # os.open returns a file descriptor: always binary
        name = _attr(call) or (call.func.id if isinstance(call.func, ast.Name) else None)
        if name == "open":
            mode = _mode(call, 0 if isinstance(call.func, ast.Attribute) else 1)
        elif name == "fdopen":
            mode = _mode(call, 1)
        else:
            continue
        if (mode is None or "b" not in mode) and "encoding" not in _keywords(call):
            bad.append(_where(path, call))
    assert not bad, "text-mode open() without encoding=: " + ", ".join(bad)


def test_text_subprocesses_name_the_encoding() -> None:
    bad = [
        _where(path, call)
        for path, call in _calls()
        if _attr(call) in _SUBPROCESS
        and any(k.arg in ("text", "universal_newlines") for k in call.keywords)
        and "encoding" not in _keywords(call)
    ]
    assert not bad, "subprocess text mode without encoding=: " + ", ".join(bad)


def test_os_replace_goes_through_replace_file() -> None:
    """Windows refuses the rename while another process has the target open."""
    bad = [
        _where(path, call)
        for path, call in _calls()
        if _is_os(call, "replace") and path != SYSTEM_MODULE
    ]
    assert not bad, "use forge.core.system.replace_file instead of os.replace: " + ", ".join(bad)


def test_os_kill_is_confined_to_the_system_module() -> None:
    """On Windows ``os.kill(pid, 0)`` terminates the process instead of probing it."""
    bad = [
        _where(path, call)
        for path, call in _calls()
        if _is_os(call, "kill") and path != SYSTEM_MODULE
    ]
    assert not bad, "use forge.core.system.pid_running: " + ", ".join(bad)


def test_platform_lock_modules_are_confined_to_the_lock() -> None:
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_bytes().decode("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if {"fcntl", "msvcrt"} & set(names) and path != LOCK_MODULE:
                bad.append(_where(path, node))
    assert not bad, "fcntl/msvcrt belong in forge.core.pending: " + ", ".join(bad)
