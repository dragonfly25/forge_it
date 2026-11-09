"""Golden test for ADR 0017's done-when: with `off` everywhere, output equals v0.1.0's.

``tests/snapshots/v010_next.json`` and ``v010_task_show.json`` were captured from the v0.1.0
code (``main`` at ``e72cd50``, before Phase 11) running :mod:`support.delegation_golden`:

    git archive main src | tar -x -C <dir>
    PYTHONPATH=<dir>/src:tests .venv/bin/python tests/support/delegation_golden.py \
        tests/snapshots

The same scenario runs here with the new code and ``delegation: off`` in the project, the
profile and ``--delegation off``. After removing only the new fields (``delegation`` and
``architecture`` (ADR 0019) in the task context, ``delegation_override`` in ``current``), the
full envelopes, including every message, must equal the captured ones. Do not regenerate
these files from the new code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from support.delegation_golden import run_scenario

SNAPSHOTS = Path(__file__).resolve().parent.parent / "snapshots"


# The scenario has no plan file and no architecture: the ADR 0019 field is empty.
NO_ARCHITECTURE = {"path": None, "component": None, "implements": [], "uses": []}


def _strip_new_fields(name: str, envelope: dict[str, Any]) -> dict[str, Any]:
    data = envelope["data"]
    if name == "next":
        assert data["context"]["delegation"]["mode"] == "off"
        assert data["current"]["delegation_override"] == "off"
        assert data["context"]["architecture"] == NO_ARCHITECTURE
        del data["context"]["delegation"]
        del data["context"]["architecture"]
        del data["current"]["delegation_override"]
    else:
        assert data["delegation"]["mode"] == "off"
        assert data["delegation"]["source"] == "invocation"
        assert data["architecture"] == NO_ARCHITECTURE
        del data["delegation"]
        del data["architecture"]
    return envelope


@pytest.fixture
def outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # The scenario sets these itself; registering them here restores them after the test.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "unused"))
    monkeypatch.delenv("FORGE_AGENT", raising=False)
    return run_scenario(tmp_path, delegation_off=True)


@pytest.mark.parametrize("name", ["next", "task_show"])
def test_delegation_off_equals_v010_golden(outputs: dict[str, Any], name: str) -> None:
    golden = json.loads((SNAPSHOTS / f"v010_{name}.json").read_text())
    assert golden["messages"], "the golden scenario must exercise messages"
    assert _strip_new_fields(name, outputs[name]) == golden
