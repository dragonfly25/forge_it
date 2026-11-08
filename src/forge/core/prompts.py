"""Provider-neutral prompts and subagent definitions (spec §12, §12.1, ADR 0014).

- `.forge/prompts/<name>.md`: frontmatter ``name``, ``description``, ``args``, ``roles``.
- `.forge/agents/<name>.md`: frontmatter ``name``, ``description``, ``tier``, ``effort``,
  ``tools``.

Both are Markdown with a YAML frontmatter block. Loading validates every key and reports
problems as :class:`ConfigError` naming the file, the key and a fix.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from forge.core.errors import ConfigError
from forge.core.tiers import EFFORTS, TASK_TIER, TIERS, Effort, Tier
from forge.core.validate import Fields, type_name

PROMPTS_DIR = Path(".forge/prompts")
AGENTS_DIR = Path(".forge/agents")
RULES_PATH = Path(".forge/rules.md")

PROMPT_KEYS = ("name", "description", "args", "roles")
AGENT_KEYS = ("name", "description", "tier", "effort", "tools")
AGENT_TOOLS = ("read", "edit", "shell")
REQUIRED_SECTIONS = ("Goal", "Steps", "Exit codes", "Guardrails", "Subagents", "Final report")
"""Body sections every shipped prompt must have (ADR 0014)."""

COMMAND_ORDER = (
    "setup",
    "configure",
    "plan",
    "next",
    "dev",
    "sync",
    "ship",
    "review",
    "status",
    "handoff",
    "release",
)
"""Display order of the v0 prompts (spec §12) plus ``configure`` (ADR 0018); other prompts
follow alphabetically."""

_NAME_RE = r"[a-z][a-z0-9-]*"
_FRONT_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)", re.DOTALL | re.MULTILINE)


@dataclass(frozen=True)
class Prompt:
    """One command prompt. ``source`` is repository-relative."""

    name: str
    description: str
    args: str
    roles: tuple[str, ...]
    body: str
    source: Path


@dataclass(frozen=True)
class AgentDef:
    """One provider-neutral subagent definition. ``source`` is repository-relative."""

    name: str
    description: str
    tier: Tier | Literal["task"]
    effort: Effort
    tools: tuple[str, ...]
    body: str
    source: Path


def split_frontmatter(text: str, file: Path | str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into its YAML frontmatter mapping and the body that follows.

    Raises :class:`ConfigError` when the frontmatter is missing, invalid YAML or not a mapping.
    """
    match = _FRONT_RE.match(text.lstrip("﻿"))
    if match is None:
        raise ConfigError(
            file,
            None,
            "missing YAML frontmatter",
            "start the file with a `---` line, the keys, and a closing `---` line",
        )
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ConfigError(
            file, None, f"invalid YAML frontmatter: {exc}", "fix the frontmatter syntax"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(
            file,
            None,
            f"frontmatter must be a mapping, got {type_name(data)}",
            "write the frontmatter as `key: value` lines",
        )
    body = text.lstrip("﻿")[match.end() :]
    return {str(k): v for k, v in data.items()}, body


def _name(f: Fields, file: Path) -> str:
    name = f.str_("name", pattern=_NAME_RE)
    if name != file.stem:
        raise f.fail(
            "name",
            f"name {name!r} does not match the file name {file.name!r}",
            f"use `name: {file.stem}` or rename the file to {name}.md",
        )
    return name


def _description(f: Fields) -> str:
    description = f.str_("description")
    if not description.strip():
        raise f.fail("description", "must not be empty", "write one line saying what it does")
    return description.strip()


def parse_prompt(text: str, source: Path) -> Prompt:
    """Parse one prompt file's ``text``; ``source`` is its repository-relative path."""
    data, body = split_frontmatter(text, source)
    f = Fields(data, source)
    f.reject_unknown(PROMPT_KEYS)
    return Prompt(
        name=_name(f, source),
        description=_description(f),
        args=f.str_("args", default="").strip(),
        roles=tuple(f.str_list("roles")),
        body=body,
        source=source,
    )


def parse_agent(text: str, source: Path) -> AgentDef:
    """Parse one subagent definition's ``text``; ``source`` is its repository-relative path."""
    data, body = split_frontmatter(text, source)
    f = Fields(data, source)
    f.reject_unknown(AGENT_KEYS)
    tiers = [str(t) for t in TIERS] + [TASK_TIER]
    tier_raw = f.choice("tier", tiers, default=str(Tier.STANDARD))
    tier: Tier | Literal["task"] = "task" if tier_raw == TASK_TIER else Tier(tier_raw)
    effort = Effort(f.choice("effort", [str(e) for e in EFFORTS], default=str(Effort.MEDIUM)))
    tools = f.str_list("tools")
    for i, tool in enumerate(tools):
        if tool not in AGENT_TOOLS:
            raise f.fail(
                f"tools[{i}]",
                f"unknown tool {tool!r}",
                f"use any of: {', '.join(AGENT_TOOLS)}",
            )
    return AgentDef(
        name=_name(f, source),
        description=_description(f),
        tier=tier,
        effort=effort,
        tools=tuple(dict.fromkeys(tools)),
        body=body,
        source=source,
    )


def _sort_key(name: str) -> tuple[int, str]:
    if name in COMMAND_ORDER:
        return (COMMAND_ORDER.index(name), name)
    return (len(COMMAND_ORDER), name)


def parse_prompts(texts: Mapping[Path, str]) -> tuple[Prompt, ...]:
    """Parse prompt files given as ``{repo-relative path: text}``, in display order."""
    prompts = [parse_prompt(text, path) for path, text in texts.items()]
    return tuple(sorted(prompts, key=lambda p: _sort_key(p.name)))


def parse_agents(texts: Mapping[Path, str]) -> tuple[AgentDef, ...]:
    """Parse subagent definitions given as ``{repo-relative path: text}``, sorted by name."""
    return tuple(sorted((parse_agent(t, p) for p, t in texts.items()), key=lambda a: a.name))


def _read_dir(repo_root: Path, rel_dir: Path) -> dict[Path, str]:
    directory = repo_root / rel_dir
    if not directory.is_dir():
        return {}
    texts: dict[Path, str] = {}
    for path in sorted(directory.glob("*.md")):
        if path.is_file():
            texts[rel_dir / path.name] = read_text(path)
    return texts


def read_text(path: Path) -> str:
    """Read ``path`` as UTF-8 without newline translation (CRLF is kept)."""
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(path, None, f"cannot read file: {exc}", "check the file") from exc


def load_prompts(repo_root: Path) -> tuple[Prompt, ...]:
    """Load and validate every `.forge/prompts/*.md` under ``repo_root``.

    Raises :class:`ConfigError` when the directory is missing or a file is invalid.
    """
    if not (repo_root / PROMPTS_DIR).is_dir():
        raise ConfigError(
            PROMPTS_DIR, None, "directory not found", "run `forge init` to create the prompts"
        )
    return parse_prompts(_read_dir(repo_root, PROMPTS_DIR))


def load_agents(repo_root: Path) -> tuple[AgentDef, ...]:
    """Load and validate every `.forge/agents/*.md` (none when the directory is absent)."""
    return parse_agents(_read_dir(repo_root, AGENTS_DIR))


def load_rules(repo_root: Path) -> str:
    """Return the text of `.forge/rules.md`. Raises :class:`ConfigError` when it is missing."""
    path = repo_root / RULES_PATH
    if not path.is_file():
        raise ConfigError(RULES_PATH, None, "file not found", "run `forge init` to create it")
    return read_text(path)


# -- body checks ----------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_INLINE_RE = re.compile(r"`([^`\n]+)`")
_FORGE_CALL_RE = re.compile(r"(?<![\w./-])forge((?:[ \t]+-[\w-]+)*)[ \t]+([a-z][a-z-]*)")


def headings(body: str) -> list[str]:
    """Return the Markdown heading titles of ``body`` (outside fenced code blocks)."""
    return [m.group(1).strip() for m in _HEADING_RE.finditer(_FENCE_RE.sub("", body))]


def missing_sections(body: str, required: Iterable[str] = REQUIRED_SECTIONS) -> list[str]:
    """Return the required section titles that ``body`` has no heading for (case-insensitive)."""
    titles = {h.lower() for h in headings(body)}
    return [s for s in required if s.lower() not in titles]


def forge_subcommands(body: str) -> list[str]:
    """Return the ``forge <subcommand>`` names used in code spans and fenced blocks of ``body``.

    Leading options (``forge --json status``) are skipped. Order of first use, no duplicates.
    """
    code = [m.group(0) for m in _FENCE_RE.finditer(body)]
    code += [m.group(1) for m in _INLINE_RE.finditer(_FENCE_RE.sub("", body))]
    found: dict[str, None] = {}
    for chunk in code:
        for m in _FORGE_CALL_RE.finditer(chunk):
            found.setdefault(m.group(2), None)
    return list(found)


PLACEHOLDER_SAMPLES: dict[str, str] = {
    "n": "42",
    "M": "M1",
    "milestone": "M1",
    "id": "M1-T07",
    "task": "M1-T07",
    "M1-T07|#42": "M1-T07",
    "pr": "7",
    "file": "notes.md",
    "x.y.z": "1.2.3",
}
"""Sample values substituted for ``<placeholder>`` arguments in prompt invocations."""

PLACEHOLDER_DEFAULT = "sample"
"""Value substituted for a placeholder not in :data:`PLACEHOLDER_SAMPLES`."""

_PLACEHOLDER_RE = re.compile(r"<([^<>\n]+)>")
_INVOCATION_RE = re.compile(r"(?<![\w./-])forge(?=[ \t]|$)(.*)$")
_SHELL_STOP_RE = re.compile(r"[ \t]*(?:\|\|?|&&|;|[ \t]#|\$\(|\)|>)")
_ELLIPSIS = ("...", "…")


def _code_chunks(body: str) -> list[str]:
    """Fenced blocks (minus their fence lines) and inline code spans of ``body``."""
    chunks: list[str] = []
    for m in _FENCE_RE.finditer(body):
        lines = m.group(0).splitlines()
        chunks += lines[1:-1]
    chunks += [m.group(1) for m in _INLINE_RE.finditer(_FENCE_RE.sub("", body))]
    return chunks


def substitute_placeholders(text: str) -> str:
    """Replace each ``<placeholder>`` in ``text`` with a sample (:data:`PLACEHOLDER_SAMPLES`)."""
    return _PLACEHOLDER_RE.sub(
        lambda m: PLACEHOLDER_SAMPLES.get(m.group(1).strip(), PLACEHOLDER_DEFAULT), text
    )


def forge_invocations(body: str) -> list[tuple[str, ...]]:
    """Return every ``forge …`` call written in the code spans and fenced blocks of ``body``.

    Each call is the argument list after ``forge``, split like a shell would, with placeholders
    (``<n>``, ``<milestone>``, ``<file>`` …) replaced by sample values. A call stops at a shell
    operator (``|``, ``&&``, ``;``, `` #``, ``)``, ``>``). Elided calls (``forge … --json``,
    ``--submit ...``) and calls that cannot be split are skipped. Order of first use, no
    duplicates.
    """
    found: dict[tuple[str, ...], None] = {}
    for chunk in _code_chunks(body):
        for line in chunk.splitlines():
            match = _INVOCATION_RE.search(substitute_placeholders(line))
            if match is None:
                continue
            rest = match.group(1)
            stop = _SHELL_STOP_RE.search(rest)
            if stop is not None:
                rest = rest[: stop.start()]
            if any(e in rest for e in _ELLIPSIS):
                continue
            try:
                tokens = tuple(shlex.split(rest))
            except ValueError:
                continue
            if tokens:
                found.setdefault(tokens, None)
    return list(found)


def check_prompt(prompt: Prompt, commands: Collection[str]) -> list[str]:
    """Return the problems of a prompt body: missing sections and unknown ``forge`` commands.

    ``commands`` are the real top-level subcommands of the CLI.
    """
    problems = [f"{prompt.source}: missing section {s!r}" for s in missing_sections(prompt.body)]
    problems += [
        f"{prompt.source}: unknown command `forge {c}`"
        for c in forge_subcommands(prompt.body)
        if c not in commands
    ]
    return problems


__all__ = [
    "AGENTS_DIR",
    "AGENT_KEYS",
    "AGENT_TOOLS",
    "COMMAND_ORDER",
    "PLACEHOLDER_DEFAULT",
    "PLACEHOLDER_SAMPLES",
    "PROMPTS_DIR",
    "PROMPT_KEYS",
    "REQUIRED_SECTIONS",
    "RULES_PATH",
    "AgentDef",
    "Prompt",
    "check_prompt",
    "forge_invocations",
    "forge_subcommands",
    "headings",
    "load_agents",
    "load_prompts",
    "load_rules",
    "missing_sections",
    "parse_agent",
    "parse_agents",
    "parse_prompt",
    "parse_prompts",
    "read_text",
    "split_frontmatter",
    "substitute_placeholders",
]
