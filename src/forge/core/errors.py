"""Exit codes (spec §16) and the exception hierarchy shared by all core modules."""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path


class ExitCode(IntEnum):
    """Process exit codes defined in spec §16."""

    OK = 0
    ERROR = 1
    AGENT_ACTION = 2  # conflicts or failing checks the agent must handle
    NOTHING_AVAILABLE = 3
    ESCALATION = 4
    PRECONDITION = 5  # version mismatch, dirty state, not authenticated, not on a task branch


class ForgeError(Exception):
    """Base class for every error Forge reports to the user.

    ``hint`` tells the user how to fix the problem; ``code`` is the exit code the CLI uses.
    """

    code: ExitCode = ExitCode.ERROR

    def __init__(self, message: str, *, hint: str | None = None, code: ExitCode | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        if code is not None:
            self.code = code

    def __str__(self) -> str:
        return self.message if self.hint is None else f"{self.message}\nFix: {self.hint}"


class ConfigError(ForgeError):
    """Invalid or missing configuration file. Always names the file, the key and the fix."""

    def __init__(self, file: Path | str, key: str | None, problem: str, fix: str):
        self.file = str(file)
        self.key = key
        self.problem = problem
        where = self.file if key is None else f"{self.file}: {key}"
        super().__init__(f"{where}: {problem}", hint=fix)


class PreconditionError(ForgeError):
    """A precondition for the command is not met (exit code 5)."""

    code = ExitCode.PRECONDITION
