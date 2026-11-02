"""Command results and the JSON envelope (ADR 0004).

Core functions return or build a :class:`Result`; the CLI layer renders it. This module never
prints and never imports typer or rich.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from forge.core.errors import ExitCode, ForgeError


class Level(StrEnum):
    """Severity of a user-facing message."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Message:
    """One user-facing message, with an optional hint on how to fix the problem."""

    level: Level
    text: str
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the envelope representation ``{"level", "text", "hint"}``."""
        return {"level": str(self.level), "text": self.text, "hint": self.hint}


@dataclass
class Result:
    """Outcome of a command: exit code, command-specific data and messages."""

    code: ExitCode = ExitCode.OK
    data: dict[str, Any] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the exit code is 0."""
        return self.code == ExitCode.OK

    def info(self, text: str, hint: str | None = None) -> None:
        """Append an info message."""
        self.messages.append(Message(Level.INFO, text, hint))

    def warn(self, text: str, hint: str | None = None) -> None:
        """Append a warning message."""
        self.messages.append(Message(Level.WARNING, text, hint))

    def error(self, text: str, hint: str | None = None) -> None:
        """Append an error message (does not change the exit code)."""
        self.messages.append(Message(Level.ERROR, text, hint))


def to_envelope(result: Result) -> dict[str, Any]:
    """Return the ADR 0004 envelope ``{"ok", "code", "data", "messages"}`` for ``result``."""
    return {
        "ok": result.ok,
        "code": int(result.code),
        "data": result.data,
        "messages": [m.to_dict() for m in result.messages],
    }


def error_result(exc: ForgeError) -> Result:
    """Build the result for an uncaught :class:`ForgeError`: its code and one error message."""
    return Result(code=exc.code, messages=[Message(Level.ERROR, exc.message, exc.hint)])


def dumps(result: Result) -> str:
    """Serialize ``result`` as a stable JSON envelope (key order preserved, UTF-8 kept)."""
    return json.dumps(to_envelope(result), ensure_ascii=False, sort_keys=False, indent=2)
