"""Claude Code adapter (spec §13, ADR 0014).

Project level (committed):
- `.claude/skills/forge-<cmd>/SKILL.md` per prompt (skills, not `.claude/commands/`).
- the managed region of `CLAUDE.md`: an `@AGENTS.md` import plus a short Claude note.

Machine level (gitignored): `.claude/agents/forge-<name>.md` per subagent definition, with
the concrete model and effort of one profile agent.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from forge.core.adapters._common import (
    SKILL_PREFIX,
    GeneratedFile,
    Project,
    frontmatter,
    md_header,
    normalize_body,
    resolve_subagent,
)
from forge.core.config import ForgeConfig
from forge.core.presets import Preset
from forge.core.profile import AgentProfile
from forge.core.prompts import AgentDef, Prompt

TOOL = "claude-code"
"""Profile ``tool`` value whose agents this adapter renders subagents for."""

CLAUDE_MD = Path("CLAUDE.md")
SKILLS_DIR = Path(".claude/skills")
AGENTS_OUT_DIR = Path(".claude/agents")
GITIGNORE_PATTERN = ".claude/agents/forge-*.md"

TOOL_MAP: dict[str, tuple[str, ...]] = {
    "read": ("Read", "Grep", "Glob"),
    "edit": ("Edit", "Write"),
    "shell": ("Bash",),
}
"""Abstract subagent tools → Claude Code tool names (B9)."""

EFFORT_VALUES = ("low", "medium", "high", "xhigh", "max")

MODEL_ALIASES = ("haiku", "sonnet", "opus", "fable")
"""Model aliases Claude Code accepts in a subagent's ``model`` field (besides ``inherit``)."""


def is_claude_model(model: str) -> bool:
    """True for a Claude Code alias (:data:`MODEL_ALIASES`) or a full ``claude-…`` model id."""
    return model in MODEL_ALIASES or model.startswith("claude-")


def skill_path(name: str) -> Path:
    """Repository-relative path of the skill rendered for prompt ``name``."""
    return SKILLS_DIR / f"{SKILL_PREFIX}{name}" / "SKILL.md"


def render_skill(prompt: Prompt) -> GeneratedFile:
    """Render one prompt as a Claude Code skill."""
    meta: dict[str, str] = {
        "name": f"{SKILL_PREFIX}{prompt.name}",
        "description": prompt.description,
    }
    if prompt.args:
        meta["argument-hint"] = prompt.args
    content = (
        frontmatter(meta)
        + md_header(prompt.source.as_posix())
        + "\n\n"
        + normalize_body(prompt.body)
        + "\n\nArguments: $ARGUMENTS\n"
    )
    return GeneratedFile(skill_path(prompt.name), content)


def claude_md_region(project: Project) -> str:
    """Return the inner text of `CLAUDE.md`'s managed region."""
    names = ", ".join(f"`{SKILL_PREFIX}{a.name}`" for a in project.agents) or "none"
    return "\n".join(
        [
            md_header(".forge/ (claude adapter)"),
            "@AGENTS.md",
            "",
            f"Claude Code: Forge commands are the skills `/{SKILL_PREFIX}<cmd>` (for example "
            f"`/{SKILL_PREFIX}next`); Forge subagents are named `{SKILL_PREFIX}*` ({names}).",
            "",
        ]
    )


def render(project: Project) -> list[GeneratedFile]:
    """Render the project-level Claude Code files."""
    files = [GeneratedFile(CLAUDE_MD, claude_md_region(project), region=True)]
    files += [render_skill(p) for p in project.prompts]
    return files


def subagent_path(name: str) -> Path:
    """Repository-relative path of the rendered subagent ``name``."""
    return AGENTS_OUT_DIR / f"{SKILL_PREFIX}{name}.md"


def claude_tools(tools: tuple[str, ...]) -> list[str]:
    """Map abstract tools to Claude Code tool names, in order, without duplicates."""
    return list(dict.fromkeys(name for tool in tools for name in TOOL_MAP[tool]))


def render_subagent(
    definition: AgentDef,
    agent: AgentProfile,
    config: ForgeConfig,
    presets: Mapping[str, Preset] | None,
) -> tuple[GeneratedFile, list[str]]:
    """Render one subagent for ``agent``; returns the file and its warnings."""
    resolved = resolve_subagent(
        definition,
        agent,
        config,
        presets,
        allowed_efforts=EFFORT_VALUES,
        tool_label="Claude Code",
    )
    meta: dict[str, str] = {
        "name": f"{SKILL_PREFIX}{definition.name}",
        "description": definition.description,
    }
    if definition.tools:
        meta["tools"] = ", ".join(claude_tools(definition.tools))
    warnings = list(resolved.warnings)
    model = resolved.model
    if model is not None and not is_claude_model(model):
        warnings.append(
            f"subagent {definition.name} (Claude Code, agent {agent.id}): {model!r} is not a "
            "Claude model; rendered with `model: inherit` so it uses the main session's model"
        )
        model = None
    meta["model"] = model if model is not None else "inherit"
    if resolved.effort is not None:
        meta["effort"] = resolved.effort
    source = f"{definition.source.as_posix()} for agent {agent.id}"
    content = (
        frontmatter(meta)
        + md_header(source, local=True)
        + "\n\n"
        + normalize_body(definition.body)
        + "\n"
    )
    return GeneratedFile(subagent_path(definition.name), content), warnings


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
    "CLAUDE_MD",
    "EFFORT_VALUES",
    "GITIGNORE_PATTERN",
    "MODEL_ALIASES",
    "SKILLS_DIR",
    "TOOL",
    "TOOL_MAP",
    "claude_md_region",
    "claude_tools",
    "is_claude_model",
    "render",
    "render_local",
    "render_skill",
    "render_subagent",
    "skill_path",
    "subagent_path",
]
