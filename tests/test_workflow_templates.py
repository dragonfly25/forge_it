"""The project workflow templates use only action refs that Forge's own CI uses (ADR 0023).

Forge's CI runs on every pull request, so its refs are known to resolve. A ref that GitHub
cannot resolve (such as ``astral-sh/setup-uv@v10``) fails every workflow before its first
step, in every project created with ``forge init``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import forge

ROOT = Path(__file__).resolve().parents[1]
FORGE_CI = ROOT / ".github" / "workflows" / "ci.yml"
TEMPLATES = Path(forge.__file__).parent / "templates" / "project" / ".github" / "workflows"
_USES = re.compile(r"^[ \t]*(?:-[ \t]*)?uses:[ \t]*([\w.-]+/[\w.-]+)@([^\s#]+)", re.MULTILINE)


def _uses(path: Path) -> list[tuple[str, str]]:
    return _USES.findall(path.read_text(encoding="utf-8"))


def test_forge_ci_is_readable() -> None:
    assert dict(_uses(FORGE_CI)), f"no `uses:` found in {FORGE_CI}"


@pytest.mark.parametrize("workflow", sorted(TEMPLATES.glob("*.yml")), ids=lambda p: p.name)
def test_template_actions_use_the_refs_of_forges_ci(workflow: Path) -> None:
    ci = dict(_uses(FORGE_CI))
    used = _uses(workflow)
    assert used, f"{workflow.name} uses no action"
    for action, ref in used:
        assert action in ci, f"{workflow.name}: {action} is not used by Forge's CI"
        assert ref == ci[action], (
            f"{workflow.name}: {action}@{ref}, but Forge's CI uses @{ci[action]}; "
            "pin the same ref, which is known to resolve"
        )


def test_the_check_catches_a_ref_that_does_not_exist(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text("    steps:\n      - uses: astral-sh/setup-uv@v10\n")
    ci = dict(_uses(FORGE_CI))
    assert ("astral-sh/setup-uv", "v10") in _uses(bad)
    assert ci["astral-sh/setup-uv"] != "v10"
