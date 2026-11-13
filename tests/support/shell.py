"""Shell commands that behave the same in ``/bin/sh`` and in ``cmd.exe`` (ADR 0021).

Checks, ``regenerate`` resolvers and setup commands run with the system shell: ``/bin/sh``
on macOS and Linux, ``cmd.exe`` on Windows. Tests that run real commands use these, so
they pass on every platform. ``exit <n>`` works in both shells; anything else goes through
the Python that runs the tests (no ``$``, backticks or double quotes inside ``code``).
"""

from __future__ import annotations

import sys

PASS = "exit 0"
FAIL = "exit 1"


def python(code: str) -> str:
    """A command running ``code`` with the test interpreter."""
    assert '"' not in code and "$" not in code and "`" not in code, code
    return f'"{sys.executable}" -c "{code}"'


def file_contains(path: str, text: str) -> str:
    """Exit 0 when ``path`` (relative to the working directory) contains ``text``, else 1."""
    return python(f"import sys; sys.exit(0 if {text!r} in open({path!r}).read() else 1)")


def append_line(path: str, line: str) -> str:
    """Append ``line`` and an LF to ``path``."""
    return python(f"open({path!r}, 'a', newline='').write({line!r} + chr(10))")
