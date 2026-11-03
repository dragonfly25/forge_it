"""Generic adapter: the Forge-managed region of `AGENTS.md` (spec §13, ADR 0014).

Read natively by Codex, Cursor, Gemini CLI, opencode and local agents, and imported by
`CLAUDE.md`. Prompt bodies are referenced, never inlined, so the file stays well under
Codex's 32 KiB `AGENTS.md` limit.
"""

from __future__ import annotations

import re
from pathlib import Path

from forge.core.adapters._common import (
    SKILL_PREFIX,
    GeneratedFile,
    Project,
    md_header,
    normalize_body,
)
from forge.core.prompts import AGENTS_DIR, PROMPTS_DIR, RULES_PATH

AGENTS_MD = Path("AGENTS.md")

_FENCE_LINE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})(\s)")


def shift_headings(markdown: str, levels: int) -> str:
    """Return ``markdown`` with every ATX heading (outside code fences) ``levels`` deeper.

    Headings never go deeper than ``######``.
    """
    out: list[str] = []
    fenced = False
    for line in markdown.split("\n"):
        if _FENCE_LINE.match(line):
            fenced = not fenced
        elif not fenced:
            m = _HEADING.match(line)
            if m is not None:
                depth = min(len(m.group(1)) + levels, 6)
                line = "#" * depth + line[len(m.group(1)) :]
        out.append(line)
    return "\n".join(out)


def cell(text: str) -> str:
    """Escape ``text`` for a Markdown table cell."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def agents_md_region(project: Project) -> str:
    """Return the inner text of `AGENTS.md`'s managed region."""
    lines: list[str] = [
        md_header(".forge/ (config.yml, rules.md, prompts/, agents/)"),
        "",
        "# Forge",
        "",
        f"This project ({project.name}) coordinates people and AI agents with Forge. "
        f"Default branch: `{project.default_branch}`; pull requests merge with "
        f"`{project.merge_method}`. Task state lives in GitHub issues and labels; the `forge` "
        "CLI is the only way to change it. Run `forge <command> --json` and act on the exit "
        "code (0 ok, 1 error, 2 agent action needed, 3 nothing available, 4 escalation "
        "required, 5 precondition failed).",
        "",
        "## Commands",
        "",
        f"When the user invokes `/<cmd>`, `/{SKILL_PREFIX}<cmd>` or asks for it in words, "
        f"read and follow `{PROMPTS_DIR.as_posix()}/<cmd>.md`.",
        "",
    ]
    if "claude" in project.adapters or "codex" in project.adapters:
        tools = []
        if "claude" in project.adapters:
            tools.append(f"Claude Code skills `/{SKILL_PREFIX}<cmd>`")
        if "codex" in project.adapters:
            tools.append(f"Codex skills `${SKILL_PREFIX}<cmd>`")
        lines += [f"The same prompts are also installed as {' and '.join(tools)}.", ""]
    lines += [
        "| Command | Prompt | Description | Arguments | Roles |",
        "|---|---|---|---|---|",
    ]
    for prompt in project.prompts:
        args = f"`{cell(prompt.args)}`" if prompt.args else ""
        lines.append(
            f"| `/{prompt.name}` | `{prompt.source.as_posix()}` | {cell(prompt.description)} "
            f"| {args} | {cell(', '.join(prompt.roles)) or 'all'} |"
        )
    lines += ["", "## Subagents", ""]
    if project.agents:
        lines += [
            f"Defined in `{AGENTS_DIR.as_posix()}/<name>.md`. If your tool supports subagents, "
            f"delegate to `{SKILL_PREFIX}<name>` when a prompt says so; otherwise read the "
            "definition and do the work yourself.",
            "",
            "| Subagent | Tier | Effort | Description |",
            "|---|---|---|---|",
        ]
        for agent in project.agents:
            lines.append(
                f"| `{SKILL_PREFIX}{agent.name}` | {agent.tier} | {agent.effort} "
                f"| {cell(agent.description)} |"
            )
    else:
        lines.append("None defined.")
    lines += [
        "",
        "## Rules",
        "",
        f"From `{RULES_PATH.as_posix()}`. Every agent must follow them.",
        "",
        shift_headings(normalize_body(project.rules), 2),
        "",
    ]
    return "\n".join(lines)


def render(project: Project) -> list[GeneratedFile]:
    """Render the generic adapter's files: the managed region of `AGENTS.md`."""
    return [GeneratedFile(AGENTS_MD, agents_md_region(project), region=True)]


__all__ = ["AGENTS_MD", "agents_md_region", "cell", "render", "shift_headings"]
