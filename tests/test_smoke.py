"""Phase 0 smoke tests: CLI version/help, CI workflow shape, core import rule."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from forge import __version__
from forge.cli import app

REPO_ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def test_version_long_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"forge {__version__}"


def test_version_short_flag() -> None:
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert result.output.strip() == f"forge {__version__}"


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--version" in result.output


def test_python_dash_m_forge_version() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "forge", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"forge {__version__}"


def test_ci_workflow_shape() -> None:
    workflow: dict[Any, Any] = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["checks"]
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    runs = [step["run"] for step in job["steps"] if "run" in step]
    for expected in (
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy src/forge/core",
        "uv run pytest -q",
    ):
        assert expected in runs, f"missing CI step: {expected}"


_FORBIDDEN_IMPORT = re.compile(r"^\s*(?:import|from)\s+(typer|rich)\b", re.MULTILINE)


def test_core_never_imports_typer_or_rich() -> None:
    core_dir = REPO_ROOT / "src" / "forge" / "core"
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted(core_dir.rglob("*.py"))
        if _FORBIDDEN_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"forge.core must not import typer/rich: {offenders}"
