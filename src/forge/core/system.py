"""Operating-system differences, kept in one place (ADR 0021).

Forge runs natively on macOS, Linux and Windows (PowerShell or cmd, no WSL needed). Every
place where the platforms differ goes through this module:

- per-user directories (:func:`config_home`, :func:`state_home`);
- finding executables, including the ``.cmd`` shims that npm installs on Windows
  (:func:`resolve_argv`);
- the default editor (:func:`default_editor`);
- whether a process is still running (:func:`pid_running`);
- UTF-8 standard streams (:func:`utf8_stdio`).

Functions that depend on the platform take an optional ``platform`` argument
(``sys.platform`` by default), so tests can exercise every branch on any machine.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

WINDOWS = "win32"
"""``sys.platform`` on Windows (also for 64-bit Python)."""

TEXT_ENCODING = "utf-8"
"""Encoding of every text file Forge reads or writes and of git and gh output (ADR 0021)."""


def is_windows(platform: str | None = None) -> bool:
    """True on native Windows. ``platform`` defaults to ``sys.platform``.

    WSL reports ``linux`` and is treated as Linux.
    """
    return (platform if platform is not None else sys.platform) == WINDOWS


def _absolute(value: str | None) -> Path | None:
    """``value`` as a path when it is set and absolute; ``None`` otherwise."""
    if value and Path(value).is_absolute():
        return Path(value)
    return None


def config_home(env: Mapping[str, str] = os.environ, platform: str | None = None) -> Path:
    """Base directory for per-user configuration (Forge adds ``forge/``).

    ``$XDG_CONFIG_HOME`` wins on every platform when it is set and absolute (the XDG spec
    says relative values must be ignored). Otherwise: ``%APPDATA%`` on Windows (falling back
    to ``~\\AppData\\Roaming``), ``$HOME/.config`` elsewhere.
    """
    xdg = _absolute(env.get("XDG_CONFIG_HOME"))
    if xdg is not None:
        return xdg
    if is_windows(platform):
        return _absolute(env.get("APPDATA")) or Path.home() / "AppData" / "Roaming"
    home = env.get("HOME")
    return (Path(home) if home else Path.home()) / ".config"


def state_home(env: Mapping[str, str] = os.environ, platform: str | None = None) -> Path:
    """Base directory for per-user state (Forge adds ``forge/``).

    ``$XDG_STATE_HOME`` wins on every platform when it is set and absolute. Otherwise:
    ``%LOCALAPPDATA%`` on Windows (state is per machine, so not the roaming profile;
    falling back to ``~\\AppData\\Local``), ``$HOME/.local/state`` elsewhere.
    """
    xdg = _absolute(env.get("XDG_STATE_HOME"))
    if xdg is not None:
        return xdg
    if is_windows(platform):
        return _absolute(env.get("LOCALAPPDATA")) or Path.home() / "AppData" / "Local"
    home = env.get("HOME")
    return (Path(home) if home else Path.home()) / ".local" / "state"


def resolve_argv(argv: Sequence[str]) -> list[str]:
    """``argv`` with its program resolved through ``PATH`` (and ``PATHEXT`` on Windows).

    On Windows ``subprocess`` finds only ``.exe`` files by bare name, so a tool installed by
    npm (``codex.cmd``, ``claude.cmd``) would look missing. Resolving the program first finds
    it. A program that is not found is left as is, so the caller still gets
    ``FileNotFoundError``; paths (with a separator) are left alone.
    """
    args = list(argv)
    if not args:
        return args
    program = args[0]
    if os.sep in program or (os.altsep is not None and os.altsep in program):
        return args
    found = shutil.which(program)
    if found is not None:
        args[0] = found
    return args


def default_editor(platform: str | None = None) -> list[str]:
    """The editor used when neither ``$VISUAL`` nor ``$EDITOR`` is set."""
    return ["notepad"] if is_windows(platform) else ["vi"]


def editor_hint(platform: str | None = None) -> str:
    """How to set the editor, in the syntax of the platform's usual shell."""
    if is_windows(platform):
        return 'set $EDITOR to a working editor, e.g. `$env:EDITOR = "notepad"` in PowerShell'
    return "set $EDITOR to a working editor, e.g. `export EDITOR=nano`"


# --------------------------------------------------------------------------- files

REPLACE_RETRY_SECONDS = 1.0
"""How long :func:`replace_file` retries on Windows while another process has the target open."""


def replace_file(
    source: str | os.PathLike[str], target: str | os.PathLike[str], platform: str | None = None
) -> None:
    """``os.replace(source, target)``, retried briefly on Windows.

    On Windows the rename fails with ``PermissionError`` while another process (an editor,
    an antivirus scanner, another Forge run reading the file) has ``target`` open. The
    window is short, so the rename is retried for :data:`REPLACE_RETRY_SECONDS` before the
    error is raised. Elsewhere the rename is atomic and never retried.
    """
    if not is_windows(platform):
        os.replace(source, target)
        return
    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """True when ``a`` and ``b`` name the same location (symlinks resolved).

    On Windows paths are case-insensitive and git may print ``C:/x`` where Python has
    ``c:\\x``; ``os.path.normcase`` folds both.
    """
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def same_text(a: str | None, b: str | None) -> bool:
    """True when ``a`` and ``b`` differ at most in line endings (CRLF versus LF).

    Forge writes LF. On Windows git may check the same file out with CRLF
    (``core.autocrlf=true``); that is not drift and must not count as a change.
    """
    if a is None or b is None:
        return a is b
    return a == b or a.replace("\r\n", "\n") == b.replace("\r\n", "\n")


# --------------------------------------------------------------------------- processes


def run_text(
    argv: Sequence[str], *, input: str | None = None, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` with UTF-8 text in and out, and stdin passed byte for byte.

    Text mode would translate ``\\n`` to ``\\r\\n`` in what Windows writes to the child's stdin,
    so a commit message or an issue body sent that way would carry CRLF (and git trailers a
    trailing ``\\r``). Here stdin is encoded as given; stdout and stderr are decoded like text
    mode does (UTF-8, universal newlines). ``kwargs`` go to ``subprocess.run`` (``cwd``,
    ``env``, ``capture_output``, ``check``, ``timeout``, ...).
    """
    proc = subprocess.run(
        list(argv),
        input=None if input is None else input.encode(TEXT_ENCODING),
        **kwargs,
    )
    return subprocess.CompletedProcess(
        proc.args, proc.returncode, _decode(proc.stdout), _decode(proc.stderr)
    )


def _decode(data: bytes | None) -> str:
    """Decode child output like ``text=True`` would: UTF-8 and universal newlines."""
    if not data:
        return ""
    return io.TextIOWrapper(io.BytesIO(data), encoding=TEXT_ENCODING, newline=None).read()


def pid_running(pid: int) -> bool:
    """True when a process with ``pid`` exists on this machine.

    POSIX: ``os.kill(pid, 0)``; ``ProcessLookupError`` means gone, ``PermissionError`` means
    it runs under another user. Windows: ``OpenProcess`` plus ``GetExitCodeProcess``
    (``os.kill`` there would terminate the process). Any doubt answers True, the safe side:
    a run that looks alive is never taken over automatically.
    """
    if pid <= 0:
        return False
    if sys.platform == WINDOWS:
        return _pid_running_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: another user's process; anything else: unsure
        return True
    return True


if sys.platform == "win32":  # literal, so mypy checks this branch only on Windows
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5

    def _pid_running_windows(pid: int) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # No such process gives ERROR_INVALID_PARAMETER; access denied means it runs.
            return bool(ctypes.get_last_error() == _ERROR_ACCESS_DENIED)
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            # A process that exited with code 259 looks alive: the safe side.
            return bool(code.value == _STILL_ACTIVE)
        finally:
            kernel32.CloseHandle(handle)

else:

    def _pid_running_windows(pid: int) -> bool:  # pragma: no cover - never called off Windows
        raise NotImplementedError("Windows only")


# --------------------------------------------------------------------------- standard streams


def utf8_stdio(platform: str | None = None) -> None:
    """Make stdout and stderr UTF-8 on Windows; a no-op elsewhere.

    Agents read Forge's output through pipes. On Windows a piped stream uses the ANSI code
    page (cp1252, ...), which cannot encode every character of an issue title or a path and
    would garble the JSON envelope. A console is already UTF-8, so reconfiguring is safe.
    Streams that cannot be reconfigured (test runners' buffers) are left alone.
    """
    if not is_windows(platform):
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(AttributeError, ValueError, OSError):
            reconfigure(encoding=TEXT_ENCODING, errors="replace")


__all__ = [
    "TEXT_ENCODING",
    "WINDOWS",
    "config_home",
    "default_editor",
    "editor_hint",
    "is_windows",
    "pid_running",
    "replace_file",
    "resolve_argv",
    "run_text",
    "same_path",
    "same_text",
    "state_home",
    "utf8_stdio",
]
