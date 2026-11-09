"""Tests for prompt and subagent definition loading (spec §12, §12.1, ADR 0014).

Includes the checks on the prompts shipped in the project templates: frontmatter keys,
required sections, and that only real ``forge`` subcommands are named.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from forge.core.errors import ConfigError
from forge.core.init import template_texts
from forge.core.prompts import (
    AGENT_KEYS,
    COMMAND_ORDER,
    PROMPT_KEYS,
    check_prompt,
    forge_invocations,
    forge_subcommands,
    load_agents,
    load_prompts,
    load_rules,
    missing_sections,
    parse_agent,
    parse_prompt,
    parse_prompts,
    split_frontmatter,
    substitute_placeholders,
)
from forge.core.tiers import Effort, Tier

PROMPT = """\
---
name: next
description: Pick the next task
args: "[task-id]"
roles: [dev]
---
## Goal
Do it.
"""

AGENT = """\
---
name: explorer
description: Reads code
tier: light
effort: low
tools: [read]
---
Explore.
"""


def cli_commands() -> set[str]:
    """Top-level commands registered on the real CLI."""
    from forge.cli import app

    group = typer.main.get_command(app)
    return set(group.commands)  # type: ignore[attr-defined]


def test_phase9_commands_are_registered() -> None:
    # `release` and `docs` (Phase 9) are registered, so prompts may name them like any other.
    assert {"release", "docs"} <= cli_commands()


# -- parsing ----------------------------------------------------------------------------------


def test_parse_prompt() -> None:
    prompt = parse_prompt(PROMPT, Path(".forge/prompts/next.md"))
    assert prompt.name == "next"
    assert prompt.description == "Pick the next task"
    assert prompt.args == "[task-id]"
    assert prompt.roles == ("dev",)
    assert prompt.body == "## Goal\nDo it.\n"


def test_parse_prompt_crlf() -> None:
    prompt = parse_prompt(PROMPT.replace("\n", "\r\n"), Path(".forge/prompts/next.md"))
    assert prompt.name == "next"
    assert prompt.body.startswith("## Goal\r\n")


def test_parse_agent() -> None:
    agent = parse_agent(AGENT, Path(".forge/agents/explorer.md"))
    assert (agent.tier, agent.effort, agent.tools) == (Tier.LIGHT, Effort.LOW, ("read",))
    task = parse_agent(AGENT.replace("tier: light", "tier: task"), Path("a/explorer.md"))
    assert task.tier == "task"


@pytest.mark.parametrize(
    ("text", "key", "fix_part"),
    [
        ("no frontmatter\n", None, "---"),
        ("---\n- a\n---\n", None, "key: value"),
        ("---\nname: [\n---\n", None, "syntax"),
        (PROMPT.replace("name: next", "name: other"), "name", "name: next"),
        (PROMPT.replace("roles: [dev]", "roles: [dev]\nmodel: x"), "model", "remove"),
        (PROMPT.replace("description: Pick the next task", "description: ' '"), "description", ""),
        (PROMPT.replace('args: "[task-id]"', "args: [1]"), "args", ""),
    ],
)
def test_prompt_errors_name_file_key_and_fix(text: str, key: str | None, fix_part: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_prompt(text, Path(".forge/prompts/next.md"))
    err = info.value
    assert err.file == ".forge/prompts/next.md"
    assert err.key == key
    assert err.hint is not None and fix_part in err.hint


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ("tier: light", "tier: huge", "tier"),
        ("effort: low", "effort: max", "effort"),
        ("tools: [read]", "tools: [read, web]", "tools[1]"),
    ],
)
def test_agent_errors(old: str, new: str, key: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_agent(AGENT.replace(old, new), Path(".forge/agents/explorer.md"))
    assert info.value.key == key


def test_prompts_sorted_in_command_order() -> None:
    texts = {
        Path(f".forge/prompts/{n}.md"): PROMPT.replace("name: next", f"name: {n}")
        for n in ("zeta", "status", "next", "setup")
    }
    assert [p.name for p in parse_prompts(texts)] == ["setup", "next", "status", "zeta"]


def test_load_from_disk(tmp_path: Path) -> None:
    (tmp_path / ".forge/prompts").mkdir(parents=True)
    (tmp_path / ".forge/prompts/next.md").write_text(PROMPT)
    (tmp_path / ".forge/rules.md").write_text("# Rules\n")
    assert [p.name for p in load_prompts(tmp_path)] == ["next"]
    assert load_agents(tmp_path) == ()
    assert load_rules(tmp_path) == "# Rules\n"


def test_load_missing_dirs(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_prompts(tmp_path)
    assert "forge init" in str(info.value.hint)
    with pytest.raises(ConfigError):
        load_rules(tmp_path)


def test_split_frontmatter_body() -> None:
    data, body = split_frontmatter("---\na: 1\n---\nbody\n", "f.md")
    assert data == {"a": 1}
    assert body == "body\n"


# -- body checks ------------------------------------------------------------------------------


def test_missing_sections_ignores_code_blocks() -> None:
    body = "## Goal\n```\n## Steps\n```\n### exit codes\n"
    assert missing_sections(body) == ["Steps", "Guardrails", "Subagents", "Final report"]


def test_forge_subcommands_in_code_only() -> None:
    body = (
        "Prose about forge things.\n"
        "Run `forge next --json`, then `forge --json task show 3`.\n"
        "```bash\nforge sync --continue\nuv run forge bogus\n```\n"
    )
    assert forge_subcommands(body) == ["sync", "bogus", "next", "task"]


def test_check_prompt_reports_problems() -> None:
    prompt = parse_prompt(PROMPT + "Run `forge nope`.\n", Path(".forge/prompts/next.md"))
    problems = check_prompt(prompt, {"next"})
    assert any("missing section 'Steps'" in p for p in problems)
    assert any("forge nope" in p for p in problems)


# -- shipped templates ------------------------------------------------------------------------


def _shipped(prefix: str) -> dict[Path, str]:
    return {p: t for p, t in template_texts().items() if p.parent == Path(prefix)}


def test_shipped_prompts_are_complete() -> None:
    texts = _shipped(".forge/prompts")
    assert sorted(p.stem for p in texts) == sorted(COMMAND_ORDER)
    commands = cli_commands()
    problems: list[str] = []
    for path, text in texts.items():
        data, _ = split_frontmatter(text, path)
        assert sorted(data) == sorted(PROMPT_KEYS), path
        problems += check_prompt(parse_prompt(text, path), commands)
    assert problems == []


def test_shipped_agents_are_valid() -> None:
    texts = _shipped(".forge/agents")
    assert sorted(p.stem for p in texts) == [
        "arch-writer",
        "explorer",
        "implementer",
        "planner",
        "reviewer",
        "test-writer",
    ]
    agents = {p.stem: parse_agent(t, p) for p, t in texts.items()}
    for path, text in texts.items():
        data, _ = split_frontmatter(text, path)
        assert sorted(data) == sorted(AGENT_KEYS), path
    assert (agents["explorer"].tier, agents["explorer"].effort) == (Tier.LIGHT, Effort.LOW)
    assert (agents["test-writer"].tier, agents["test-writer"].effort) == ("task", Effort.MEDIUM)
    assert (agents["reviewer"].tier, agents["reviewer"].effort) == (Tier.STRONG, Effort.HIGH)
    assert (agents["implementer"].tier, agents["implementer"].effort) == (
        Tier.STANDARD,
        Effort.MEDIUM,
    )
    assert agents["implementer"].tools == ("read", "edit", "shell")
    assert (agents["planner"].tier, agents["planner"].effort) == (Tier.STANDARD, Effort.MEDIUM)
    assert (agents["arch-writer"].tier, agents["arch-writer"].effort) == (Tier.STRONG, Effort.HIGH)
    # The detail writers only read and write their own fragment: no shell.
    assert agents["planner"].tools == ("read", "edit")
    assert agents["arch-writer"].tools == ("read", "edit")


def test_shipped_prompts_reference_rendered_subagent_names() -> None:
    for path, text in _shipped(".forge/prompts").items():
        for name in (
            "explorer",
            "test-writer",
            "reviewer",
            "implementer",
            "planner",
            "arch-writer",
        ):
            # A bare `explorer` reference in code must use the rendered `forge-` name.
            assert f"`{name}`" not in text, f"{path}: use `forge-{name}`"


# -- full invocations parsed by the real CLI ---------------------------------------------------


def test_substitute_placeholders() -> None:
    assert substitute_placeholders("forge reclaim <n>") == "forge reclaim 42"
    assert substitute_placeholders("--task <M1-T07|#42> <pr> <file>") == "--task M1-T07 7 notes.md"
    assert substitute_placeholders('--reason "<why, with whom>"') == '--reason "sample"'


def test_forge_invocations_extracts_full_calls() -> None:
    body = (
        "Prose: forge next --bogus is not code.\n"
        "Run `forge next --task <n> --json`, then `forge --json task show <id>`.\n"
        '`forge escalate --reason "<what, why>" --json` and `forge ... --json`.\n'
        "`forge review <pr> --submit ...` is elided. `forge-next` is a skill.\n"
        "```bash\nforge status --json | jq .data\nuv run forge plan validate <milestone>\n```\n"
    )
    assert forge_invocations(body) == [
        ("status", "--json"),
        ("plan", "validate", "M1"),
        ("next", "--task", "42", "--json"),
        ("--json", "task", "show", "M1-T07"),
        ("escalate", "--reason", "sample", "--json"),
    ]


def _usage_error() -> type[Exception]:
    from forge.cli import _UsageError

    return _UsageError


class _Usage(Exception):
    """A usage error reported by click while parsing a prompt invocation."""


def parse_only(argv: tuple[str, ...]) -> None:
    """Parse ``forge <argv>`` with the real Click app down to the leaf command; run nothing.

    Raises :class:`_Usage` with click's message on a usage error. A call that names no option
    and only lacks a required parameter (a prose mention such as `forge plan apply`) passes.
    """
    from forge.cli import app

    group = typer.main.get_command(app)
    _UsageError = _usage_error()
    mention = not any(token.startswith("-") for token in argv)
    try:
        ctx = group.make_context("forge", list(argv))
        command: object = group
        while hasattr(command, "commands"):
            rest = [*ctx._protected_args, *ctx.args]
            if not rest:
                break
            name, sub, sub_args = command.resolve_command(ctx, rest)  # type: ignore[attr-defined]
            ctx = sub.make_context(name, sub_args, parent=ctx)
            command = sub
    except _UsageError as exc:
        if type(exc).__name__ == "MissingParameter" and mention:
            return
        raise _Usage(f"{type(exc).__name__}: {exc}") from exc


def test_parse_only_detects_usage_errors() -> None:
    parse_only(("status", "--json"))
    parse_only(("plan", "apply"))  # a mention: the milestone is only missing
    for argv in [
        ("status", "--bogus"),
        ("plan", "nope"),
        ("escalate", "--json"),
        ("next", "--task"),
    ]:
        with pytest.raises(_Usage):
            parse_only(argv)


def _shipped_invocations() -> list[tuple[str, tuple[str, ...]]]:
    texts = _shipped(".forge/prompts")
    texts.update(_shipped(".forge"))  # rules.md
    calls: list[tuple[str, tuple[str, ...]]] = []
    for path, text in sorted(texts.items()):
        if path.suffix != ".md":
            continue
        calls += [(path.as_posix(), argv) for argv in forge_invocations(text)]
    return calls


def test_shipped_invocations_parse_with_the_real_cli() -> None:
    calls = _shipped_invocations()
    assert len(calls) > 40
    problems: list[str] = []
    for source, argv in calls:
        try:
            parse_only(argv)
        except _Usage as exc:
            problems.append(f"{source}: `forge {' '.join(argv)}`: {exc}")
    assert problems == []
