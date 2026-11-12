"""A seeded `forge next` + `forge task show` scenario, runnable against any Forge version.

Used by the ADR 0017 golden test: ``tests/snapshots/v010_*.json`` were captured by running
:func:`run_scenario` with the v0.1.0 code (``main`` before Phase 11) through
``python tests/support/delegation_golden.py <out-dir>`` with ``PYTHONPATH`` pointing at that
code's ``src`` (see the module docstring of ``tests/commands/test_delegation_golden.py``).
The new code runs the same scenario with ``delegation: off`` in the project, the profile and
``--delegation off``; minus the new fields (``delegation``, and ``architecture`` of
ADR 0019), the output must be identical.

Only APIs that exist in v0.1.0 are used here. Output is normalised: the temporary base
directory becomes ``<base>`` and commit shas become ``<sha>``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from support.paths import replace_paths

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

CONFIG = """\
forge_version: ">=0.1,<0.2"
project:
  name: shop
  default_branch: main
github:
  repo: acme/shop
lease_hours: 24
"""
OFF_SECTION = "delegation:\n  default: off\n  allowed: [off]\n"

ZONES_YML = """\
zones:
  api:
    paths: ["src/api/**"]
  web:
    paths: ["src/web/**"]
"""

SPEC = "# Shop\n\n## Requirements\n- AC-1: the API answers.\n"

_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")


def isolate_git(home: Path) -> None:
    """Point git at an empty global config and a fresh HOME (like the test suite's fixture)."""
    home.mkdir(parents=True, exist_ok=True)
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("")
    os.environ["HOME"] = str(home)
    os.environ["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    for var in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE", "GIT_DIR", "GIT_WORK_TREE"):
        os.environ.pop(var, None)
    for var in ("GITHUB_ACTIONS", "FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
        os.environ.pop(var, None)
    os.environ["NO_COLOR"] = "1"
    os.environ["TERM"] = "dumb"
    os.environ["COLUMNS"] = "120"


def normalise(value: Any, base: Path) -> Any:
    """Replace the base directory and commit shas so runs compare across machines."""
    text = json.dumps(replace_paths(value, [base], "<base>"), sort_keys=False)
    return json.loads(_SHA_RE.sub("<sha>", text))


def run_scenario(base: Path, *, delegation_off: bool) -> dict[str, Any]:
    """Seed a project, claim a size-M two-zone task with `next`, then `task show` it.

    With ``delegation_off`` the project, the profile and both commands set ``off`` (the new
    code); without it the run uses only v0.1.0 inputs. Returns the normalised envelopes.
    """
    from typer.testing import CliRunner

    from conftest import GitWorld
    from forge.cli import app
    from forge.commands import _claims, _context
    from forge.core.github_fake import FakeGitHub
    from forge.core.refresh import ensure_labels
    from forge.core.tasks import Size, TaskMetadata, TaskType, upsert_block
    from forge.core.tiers import Tier

    world_dir = base / "world"
    world_dir.mkdir(parents=True)
    world = GitWorld(world_dir)
    lead = world.clone("lead")
    config = CONFIG + (OFF_SECTION if delegation_off else "")
    world.commit_file(lead, ".forge/config.yml", config, "chore: forge config")
    world.commit_file(lead, ".forge/zones.yml", ZONES_YML, "chore: zones")
    world.commit_file(lead, "specs/m1/x.md", SPEC, "docs: spec")
    world.push(lead, "main")

    fake = FakeGitHub(repo="acme/shop")
    ensure_labels(fake)
    meta = TaskMetadata(
        id="M1-T01",
        spec="specs/m1/x.md",
        covers=("AC-1", "AC-9"),  # AC-9 is not in the spec: a warning in both outputs
        depends_on=(),
        zones=("api", "web"),
        size=Size.M,
        tier=Tier.STANDARD,
        type=TaskType.IMPL,
        done_when=("tests pass",),
    )
    issue = fake.create_issue(
        "[M1-T01] task",
        upsert_block("Implement M1-T01.\n", meta),
        labels=["forge:task", "status:ready"],
    )
    fake.fake_set_user("alice")

    xdg = base / "xdg"
    (xdg / "forge").mkdir(parents=True)
    worktrees = base / "worktrees"
    (xdg / "forge" / "profile.yml").write_text(
        "github: alice\n"
        "agents:\n"
        "  - id: claude\n"
        "    tool: claude-code\n"
        "    providers: [anthropic]\n"
        "    tiers: {strong: opus, standard: sonnet, light: haiku}\n"
        "default_agent: claude\n"
        f"worktrees_dir: {worktrees}\n" + ("delegation: off\n" if delegation_off else "")
    )
    os.environ["XDG_CONFIG_HOME"] = str(xdg)
    os.environ.pop("FORGE_AGENT", None)

    saved = (_context.github_factory, _claims.clock, _claims.sleep)
    _context.github_factory = lambda _config: fake  # type: ignore[assignment,return-value]
    _claims.clock = lambda: T0
    _claims.sleep = lambda _seconds: None
    try:
        runner = CliRunner()
        flag = ["--delegation", "off"] if delegation_off else []
        repo = world.base / "alice"
        world.clone("alice")
        nxt = runner.invoke(app, ["--json", "--repo", str(repo), "next", *flag])
        worktree = worktrees / f"task-{issue.number}"
        show = runner.invoke(app, ["--json", "--repo", str(worktree), "task", "show", *flag])
    finally:
        _context.github_factory, _claims.clock, _claims.sleep = saved
    return {
        "next": {"exit_code": nxt.exit_code, **normalise(json.loads(nxt.stdout), base)},
        "task_show": {"exit_code": show.exit_code, **normalise(json.loads(show.stdout), base)},
    }


def main(out_dir: str) -> None:
    """Capture the v0.1.0 golden files (run with the old code on ``PYTHONPATH``)."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        isolate_git(base / "home")
        result = run_scenario(base, delegation_off=False)
    target = Path(out_dir)
    for name, envelope in result.items():
        path = target / f"v010_{name}.json"
        path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False) + "\n")
        print(path)


if __name__ == "__main__":
    main(sys.argv[1])
