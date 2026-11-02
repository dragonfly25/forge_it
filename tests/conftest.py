"""Shared pytest fixtures for the Forge test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def plain_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make CLI output plain and identical locally and in CI.

    Rich (used by Typer for help) forces colours when it detects GitHub Actions
    or ``FORCE_COLOR``. The ANSI codes it adds break substring assertions.
    """
    for var in ("GITHUB_ACTIONS", "FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "120")
