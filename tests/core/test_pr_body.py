"""Unit tests for forge.core.pr_body (ADR 0012 PR body block). Pure, no I/O except tmp files."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from forge.core.delegation import Delegation
from forge.core.pr_body import (
    BEGIN_MARKER,
    END_MARKER,
    AgentInfo,
    OutOfZoneFile,
    PrBlock,
    SyncResolutions,
    UpstreamLink,
    ac_text,
    checked_items,
    find_block,
    initial_body,
    read_template,
    render_block,
    resolutions_from_log,
    update_body,
)

AGENT = AgentInfo(
    id="claude-pro-1",
    tool="claude-code",
    providers=("anthropic", "local"),
    tier="standard",
    effective_tier="standard",
    model="sonnet",
    effort="medium",
    effort_value="medium",
    tier_map={"strong": "opus", "standard": "sonnet", "light": "haiku"},
    notes=("tier raised from light to standard by models.min_tier_for.x",),
)


def block(**overrides: Any) -> PrBlock:
    data: dict[str, Any] = {
        "issue": 12,
        "task_id": "M1-T03",
        "spec": "specs/m1/editor.md",
        "criteria": (("AC-1", "User can save the page."), ("AC-2", None)),
        "done_when": ("tests pass", "docs updated"),
        "out_of_zone": (
            OutOfZoneFile("src/web/a.js", ("web",)),
            OutOfZoneFile("README.md"),
        ),
        "resolutions": (
            SyncResolutions(
                at="2026-11-03T10:00:00Z",
                onto="abcdef0123456789",
                force_resolve=True,
                conflicts=(("package-lock.json", "regenerate"), ("src/api/a.py", "manual")),
                pulls=(UpstreamLink(7, "bob"),),
            ),
            SyncResolutions(
                at="2026-11-03T11:00:00Z",
                onto="1234567890abcdef",
                force_resolve=False,
                conflicts=(("src/api/b.py", "rerere"),),
            ),
        ),
        "agent": AGENT,
    }
    data.update(overrides)
    return PrBlock(**data)


def section(text: str, title: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(f"### {title}") + 1
    out = []
    for line in lines[start:]:
        if line.startswith("### ") or line == END_MARKER:
            break
        if line:
            out.append(line)
    return out


def test_block_sections_in_order_and_content() -> None:
    text = render_block(block())
    lines = text.splitlines()
    assert lines[0] == BEGIN_MARKER and lines[-1] == END_MARKER
    assert lines[1] == "Closes #12"
    assert "**Task** `M1-T03` · **Spec** `specs/m1/editor.md`" in lines
    titles = [line for line in lines if line.startswith("### ")]
    assert titles == [
        "### Acceptance criteria",
        "### Done when",
        "### Out-of-zone changes",
        "### Conflict resolutions",
        "### Agent",
    ]
    assert section(text, "Acceptance criteria") == [
        "- [ ] **AC-1**: User can save the page.",
        "- [ ] **AC-2** (not found in the spec)",
    ]
    assert section(text, "Done when") == ["- [ ] tests pass", "- [ ] docs updated"]
    assert section(text, "Out-of-zone changes") == [
        "- `src/web/a.js`: zone web",
        "- `README.md`: unzoned",
    ]
    conflicts = section(text, "Conflict resolutions")
    assert conflicts[0].startswith("- Sync at 2026-11-03T10:00:00Z onto `abcdef012345`")
    assert "**force-resolve**" in conflicts[0]
    assert conflicts[1:4] == [
        "  - `package-lock.json`: resolver `regenerate`",
        "  - `src/api/a.py`: manual",
        "  - Upstream: #7 by `@bob`",
    ]
    assert "force-resolve" not in conflicts[4]
    assert conflicts[5] == "  - `src/api/b.py`: rerere"
    agent = section(text, "Agent")
    assert agent[0] == "- Agent `claude-pro-1` (claude-code), providers: anthropic, local"
    assert agent[1] == "- Task tier standard → `sonnet`, effort medium → `medium`"
    assert agent[2] == "- Tier map: strong → `opus`, standard → `sonnet`, light → `haiku`"
    assert agent[3].startswith("- Note: tier raised")


def test_empty_sections_say_none() -> None:
    text = render_block(block(criteria=(), done_when=(), out_of_zone=(), resolutions=()))
    for title in ("Acceptance criteria", "Done when", "Out-of-zone changes"):
        assert section(text, title) == ["none"]
    assert section(text, "Conflict resolutions") == ["none"]


def test_agent_without_model_or_effort_and_served_by_higher_tier() -> None:
    agent = AgentInfo(
        id="x",
        tool="ollama",
        providers=(),
        tier="light",
        effective_tier="standard",
        model="llama",
        effort="medium",
        effort_value=None,
    )
    lines = section(render_block(block(agent=agent)), "Agent")
    assert lines[0] == "- Agent `x` (ollama), providers: none declared"
    assert lines[1] == (
        "- Task tier light → `llama` (its standard model), effort medium (not mapped)"
    )


def test_ac_text_strips_markers_and_collapses() -> None:
    assert ac_text("AC-1", "- **AC-1**: User can   save\n  the page.") == "User can save the page."
    assert ac_text("AC-3", "### AC-3 — Title\n\nBody text.") == "Title Body text."
    assert ac_text("AC-2", "AC-2. Plain paragraph") == "Plain paragraph"
    long = ac_text("AC-4", "- AC-4: " + "x" * 500)
    assert len(long) == 300 and long.endswith("…")


def test_resolutions_from_log_filters_and_dedupes() -> None:
    prefix = "refs/forge/backup/task-12/"
    conflict = {"path": "a.py", "resolution": "manual", "zones": ["api"]}
    entries = [
        {"id": prefix + "1", "outcome": "pushed", "conflicts": [], "force_resolve": False},
        {
            "id": prefix + "2",
            "at": "T2",
            "onto": "o2",
            "outcome": "pushed",
            "conflicts": [conflict, conflict, {"path": "b.json", "resolution": "union"}],
            "force_resolve": False,
            "upstream_context": {"pulls": [{"number": 4, "author": "bob"}, {"bad": 1}]},
        },
        {"id": prefix + "3", "outcome": "escalated", "conflicts": [conflict]},
        {"id": "refs/forge/backup/task-9/1", "outcome": "pushed", "conflicts": [conflict]},
        {
            "id": prefix + "4",
            "at": "T4",
            "outcome": "pushed",
            "conflicts": [],
            "force_resolve": True,
        },
    ]
    found = resolutions_from_log(entries, 12)
    assert [r.at for r in found] == ["T2", "T4"]
    assert found[0].conflicts == (("a.py", "manual"), ("b.json", "union"))
    assert found[0].pulls == (UpstreamLink(4, "bob"),)
    assert found[1].force_resolve and found[1].conflicts == ()


def test_initial_body_appends_template() -> None:
    text = render_block(block())
    assert initial_body(text, None) == text + "\n"
    assert initial_body(text, "  \n") == text + "\n"
    assert initial_body(text, "## Checklist\n- [ ] x\n") == text + "\n\n## Checklist\n- [ ] x\n"


def test_update_keeps_human_text_byte_for_byte() -> None:
    old_block = render_block(block(done_when=("old item",)))
    before = "Intro by a human\r\nwith CRLF  \n\n"
    after = "\n\n## Notes\n- [x] human checkbox\n\ttabs and trailing   \n"
    old = before + old_block + after
    new_block = render_block(block())
    updated = update_body(old, new_block)
    assert updated.startswith(before)
    assert updated.endswith(after)
    assert updated == before + new_block + after
    assert update_body(updated, new_block) == updated  # idempotent


def test_update_keeps_ticked_boxes_when_text_unchanged() -> None:
    old = (
        render_block(block())
        .replace(
            "- [ ] **AC-1**: User can save the page.", "- [x] **AC-1**: User can save the page."
        )
        .replace("- [ ] docs updated", "- [X] docs updated")
    )
    assert checked_items(old) == {"**AC-1**: User can save the page.", "docs updated"}
    new = render_block(block(done_when=("tests pass", "docs updated", "new item")))
    updated = update_body("pre\n" + old + "\npost", new)
    span = find_block(updated)
    assert span is not None
    inner = updated[span[0] : span[1]]
    assert "- [x] **AC-1**: User can save the page." in inner
    assert "- [x] docs updated" in inner
    assert "- [ ] tests pass" in inner and "- [ ] new item" in inner
    # changed text: the tick is not carried over
    changed = render_block(
        block(criteria=(("AC-1", "User can save the page twice."), ("AC-2", None)))
    )
    assert "- [ ] **AC-1**: User can save the page twice." in update_body(old, changed)


def test_update_without_block_prepends_and_keeps_old_body() -> None:
    new = render_block(block())
    assert update_body("", new) == new + "\n"
    assert update_body("hand written", new) == new + "\n\nhand written"
    dangling = BEGIN_MARKER + "\nbroken"
    assert update_body(dangling, new) == new + "\n\n" + dangling
    assert find_block(update_body(dangling, new)) == (0, len(new))


def test_read_template(tmp_path: Path) -> None:
    assert read_template(tmp_path) is None
    path = tmp_path / ".github" / "pull_request_template.md"
    path.parent.mkdir()
    path.write_text("## Template\n")
    assert read_template(tmp_path) == "## Template\n"


# --------------------------------------------------------------------------- delegation


def _delegation(**overrides: Any) -> Delegation:
    data: dict[str, Any] = {
        "mode": "auto",
        "requested": "auto",
        "source": "project",
        "delegate": True,
        "reason": "size M",
        "model": "sonnet",
    }
    data.update(overrides)
    return Delegation(**data)


def test_agent_section_records_the_delegation_decision() -> None:
    agent = replace(AGENT, delegation=_delegation())
    lines = section(render_block(block(agent=agent)), "Agent")
    assert lines[3] == "- Delegation: auto → delegated (size M) to `forge-implementer` (`sonnet`)"
    assert lines[4].startswith("- Note: tier raised")


def test_delegation_line_forms() -> None:
    cases = {
        _delegation(
            mode="off",
            requested="off",
            source="profile",
            delegate=False,
            reason="requested by profile",
            model=None,
        ): "- Delegation: off (requested by profile)",
        _delegation(delegate=False, reason="1 zone, size S", model=None): (
            "- Delegation: auto → not delegated (1 zone, size S)"
        ),
        _delegation(
            mode="on",
            requested="on",
            source="invocation",
            reason="requested by --delegation",
            model=None,
        ): (
            "- Delegation: on → delegated (requested by --delegation) to `forge-implementer` "
            "(inherits the session's model)"
        ),
    }
    for delegation, expected in cases.items():
        text = render_block(block(agent=replace(AGENT, delegation=delegation)))
        assert expected in section(text, "Agent")


def test_no_delegation_line_without_a_decision() -> None:
    assert not any(
        line.startswith("- Delegation") for line in section(render_block(block()), "Agent")
    )
