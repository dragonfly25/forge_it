"""Codex adapter (spec §13, ADR 0014).

Project level (committed): `.agents/skills/forge-<cmd>/SKILL.md` per prompt. Codex reads
`AGENTS.md` natively; custom prompts are deprecated, so none are generated.

Machine level (gitignored): `.codex/agents/forge-<name>.toml` per subagent definition.
Codex has no per-agent tool list, so ``tools`` is not rendered.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from forge.core.adapters._common import (
    SKILL_PREFIX,
    GeneratedFile,
    Project,
    frontmatter,
    hash_header,
    md_header,
    normalize_body,
    resolve_subagent,
)
from forge.core.config import ForgeConfig
from forge.core.presets import Preset
from forge.core.profile import AgentProfile
from forge.core.prompts import AgentDef, Prompt

TOOL = "codex"
"""Profile ``tool`` value whose agents this adapter renders subagents for."""

SKILLS_DIR = Path(".agents/skills")
AGENTS_OUT_DIR = Path(".codex/agents")
GITIGNORE_PATTERN = ".codex/agents/forge-*.toml"
EFFORT_VALUES = ("low", "medium", "high", "xhigh")


def skill_path(name: str) -> Path:
    """Repository-relative path of the skill rendered for prompt ``name``."""
    return SKILLS_DIR / f"{SKILL_PREFIX}{name}" / "SKILL.md"


def render_skill(prompt: Prompt) -> GeneratedFile:
    """Render one prompt as a Codex skill."""
    meta = {"name": f"{SKILL_PREFIX}{prompt.name}", "description": prompt.description}
    if prompt.args:
        note = (
            "Arguments: Codex skills have no argument placeholder; take the arguments "
            f"(`{prompt.args}`) from the user's message."
        )
    else:
        note = "Arguments: none; ignore any extra text in the user's message."
    content = (
        frontmatter(meta)
        + md_header(prompt.source.as_posix())
        + "\n\n"
        + normalize_body(prompt.body)
        + f"\n\n{note}\n"
    )
    return GeneratedFile(skill_path(prompt.name), content)


def render(project: Project) -> list[GeneratedFile]:
    """Render the project-level Codex files (skills)."""
    return [render_skill(p) for p in project.prompts]


def subagent_path(name: str) -> Path:
    """Repository-relative path of the rendered subagent ``name``."""
    return AGENTS_OUT_DIR / f"{SKILL_PREFIX}{name}.toml"


def toml_string(value: str) -> str:
    """Return ``value`` as a TOML basic string (one line, escaped)."""
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def toml_multiline(value: str) -> str:
    """Return ``value`` as a TOML multi-line basic string (``\"\"\"`` delimited)."""
    out: list[str] = []
    quotes = 0
    for ch in value:
        code = ord(ch)
        if ch == '"':
            quotes += 1
            # Escape every third quote in a run, so `"""` never appears unescaped.
            out.append('\\"' if quotes % 3 == 0 else '"')
            continue
        quotes = 0
        if ch == "\\":
            out.append("\\\\")
        elif ch in ("\n", "\t"):
            out.append(ch)
        elif ch == "\r":
            out.append("\\r")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(ch)
    text = "".join(out)
    if text.endswith('"'):
        text = text[:-1] + '\\"'
    return f'"""\n{text}"""'


def render_subagent(
    definition: AgentDef,
    agent: AgentProfile,
    config: ForgeConfig,
    presets: Mapping[str, Preset] | None,
) -> tuple[GeneratedFile, list[str]]:
    """Render one subagent for ``agent``; returns the file and its warnings."""
    resolved = resolve_subagent(
        definition, agent, config, presets, allowed_efforts=EFFORT_VALUES, tool_label="Codex"
    )
    source = f"{definition.source.as_posix()} for agent {agent.id}"
    lines = [
        hash_header(source, local=True),
        f"name = {toml_string(SKILL_PREFIX + definition.name)}",
        f"description = {toml_string(definition.description)}",
    ]
    if resolved.model is not None:
        lines.append(f"model = {toml_string(resolved.model)}")
    if resolved.effort is not None:
        lines.append(f"model_reasoning_effort = {toml_string(resolved.effort)}")
    body = normalize_body(definition.body) + "\n"
    lines.append(f"developer_instructions = {toml_multiline(body)}")
    content = "\n".join(lines) + "\n"
    return GeneratedFile(subagent_path(definition.name), content), list(resolved.warnings)


def render_local(
    definitions: tuple[AgentDef, ...],
    agent: AgentProfile,
    config: ForgeConfig,
    presets: Mapping[str, Preset] | None,
) -> tuple[list[GeneratedFile], list[str]]:
    """Render every subagent definition for ``agent``."""
    files: list[GeneratedFile] = []
    warnings: list[str] = []
    for definition in definitions:
        file, notes = render_subagent(definition, agent, config, presets)
        files.append(file)
        warnings += notes
    return files, warnings


__all__ = [
    "AGENTS_OUT_DIR",
    "EFFORT_VALUES",
    "GITIGNORE_PATTERN",
    "SKILLS_DIR",
    "TOOL",
    "render",
    "render_local",
    "render_skill",
    "render_subagent",
    "skill_path",
    "subagent_path",
    "toml_multiline",
    "toml_string",
]
