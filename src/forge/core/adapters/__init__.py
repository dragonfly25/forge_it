"""Adapters: render `.forge/` prompts and subagents into tool-native files (spec §13, ADR 0014).

Project level (committed, ``forge adapters generate [--check]``):
:func:`render_project`, :func:`check`, :func:`generate`.

Machine level (gitignored, ``forge adapters generate --local``, ``forge setup``,
``forge profile``): :func:`render_local`, :func:`write_local`, :func:`local_status`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from forge.core.adapters import claude, codex, generic
from forge.core.adapters._common import (
    GENERATED_MARK,
    SKILL_PREFIX,
    GeneratedFile,
    Project,
    is_generated,
    load_project,
    project_from_texts,
)
from forge.core.config import ForgeConfig
from forge.core.errors import ForgeError, PreconditionError
from forge.core.git import Git, GitError
from forge.core.managed import find_region, newline_of, upsert_region
from forge.core.presets import Preset, load_presets
from forge.core.profile import AgentProfile, Profile
from forge.core.prompts import AgentDef, load_agents, read_text
from forge.core.system import same_text

RENDERERS: dict[str, Callable[[Project], list[GeneratedFile]]] = {
    "generic": generic.render,
    "claude": claude.render,
    "codex": codex.render,
}
"""Project-level renderer of each adapter name (see ``config.ADAPTERS``)."""

SKILL_DIRS: tuple[Path, ...] = (claude.SKILLS_DIR, codex.SKILLS_DIR)
"""Directories holding generated ``forge-*`` skills, scanned for stale ones."""

REGION_FILES: dict[str, Path] = {"claude": claude.CLAUDE_MD}
"""Files whose managed region belongs to an optional adapter (``generic`` is always on)."""

LOCAL_ADAPTERS = (claude, codex)
"""Adapters with machine-level subagent rendering, keyed by their profile ``TOOL``."""

LOCAL_PATTERNS: tuple[str, ...] = (claude.GITIGNORE_PATTERN, codex.GITIGNORE_PATTERN)
"""`.gitignore` patterns that must cover the rendered subagents."""

DriftKind = Literal["missing", "different", "extra"]


# -- project level ----------------------------------------------------------------------------


def render_project(project: Project) -> list[GeneratedFile]:
    """Render the project-level files of every adapter in ``project.adapters``, in order."""
    files: list[GeneratedFile] = []
    for name in project.adapters:
        files += RENDERERS[name](project)
    return files


def _read(path: Path) -> str | None:
    return read_text(path) if path.is_file() else None


def expected_content(repo_root: Path, file: GeneratedFile) -> str:
    """Return the full text ``file`` should have on disk (managed regions merged in)."""
    if not file.region:
        return file.content
    return upsert_region(_read(repo_root / file.path), file.content, file.path)


@dataclass(frozen=True)
class FileDrift:
    """One project-level file that is not up to date."""

    path: Path
    kind: DriftKind

    def to_data(self) -> dict[str, str]:
        """JSON form: ``{"path", "kind"}``."""
        return {"path": self.path.as_posix(), "kind": self.kind}


def stale_skills(repo_root: Path, expected: Iterable[Path]) -> list[Path]:
    """Return generated ``forge-*`` skill files that no rendered file accounts for.

    Only files carrying the generated header count; hand-written skills are never reported.
    """
    keep = set(expected)
    stale: list[Path] = []
    for skills in SKILL_DIRS:
        directory = repo_root / skills
        if not directory.is_dir():
            continue
        for skill_dir in sorted(directory.glob(f"{SKILL_PREFIX}*")):
            rel = skills / skill_dir.name / "SKILL.md"
            target = repo_root / rel
            if rel in keep or not target.is_file():
                continue
            if is_generated(read_text(target)):
                stale.append(rel)
    return stale


def stale_regions(repo_root: Path, project: Project) -> list[Path]:
    """Files holding the managed region of an adapter that ``project`` no longer enables.

    Only the region is Forge's: the text around it belongs to people.
    """
    stale: list[Path] = []
    for adapter, rel in REGION_FILES.items():
        if adapter in project.adapters:
            continue
        text = _read(repo_root / rel)
        if text is not None and find_region(text, rel) is not None:
            stale.append(rel)
    return stale


def remove_region(text: str, file: Path | str = "<text>") -> str:
    """Return ``text`` without its managed region (and the blank line added before it).

    Text outside the region is kept byte for byte; a text without a region is unchanged.
    """
    region = find_region(text, file)
    if region is None:
        return text
    newline = newline_of(text)
    before, after = text[: region.start], text[region.end :]
    if after.startswith(newline):
        after = after[len(newline) :]
    if not after and before.endswith(newline + newline):
        before = before[: -len(newline)]
    return before + after


def check(repo_root: Path, project: Project) -> list[FileDrift]:
    """Compare the project-level files on disk with a fresh render. Writes nothing."""
    files = render_project(project)
    drifts: list[FileDrift] = []
    for file in files:
        current = _read(repo_root / file.path)
        if current is None:
            drifts.append(FileDrift(file.path, "missing"))
        elif not same_text(current, expected_content(repo_root, file)):
            drifts.append(FileDrift(file.path, "different"))
    drifts += [FileDrift(p, "extra") for p in stale_skills(repo_root, (f.path for f in files))]
    drifts += [FileDrift(p, "extra") for p in stale_regions(repo_root, project)]
    return drifts


@dataclass
class GenerateReport:
    """What :func:`generate` changed (repository-relative paths)."""

    written: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)

    def to_data(self) -> dict[str, list[str]]:
        """JSON form."""
        return {
            "written": [p.as_posix() for p in self.written],
            "unchanged": [p.as_posix() for p in self.unchanged],
            "removed": [p.as_posix() for p in self.removed],
        }


def write_text(path: Path, content: str) -> None:
    """Write ``content`` as UTF-8 without newline translation, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def write_files(repo_root: Path, files: Iterable[GeneratedFile], report: GenerateReport) -> None:
    """Write ``files`` (managed regions merged), recording each as written or unchanged."""
    for file in files:
        target = repo_root / file.path
        content = expected_content(repo_root, file)
        if same_text(_read(target), content):
            report.unchanged.append(file.path)
        else:
            write_text(target, content)
            report.written.append(file.path)


def _remove(repo_root: Path, rel: Path) -> None:
    target = repo_root / rel
    target.unlink()
    parent = target.parent
    if parent != repo_root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()


def generate(repo_root: Path, project: Project) -> GenerateReport:
    """Rewrite the project-level files and remove what disabled or deleted sources left.

    Stale generated ``forge-*`` skills are deleted. The managed region of a disabled adapter
    (``CLAUDE.md`` without ``claude``) is removed; the file is deleted only when nothing else
    is left in it (listed in ``removed``); otherwise the rest is kept (listed in ``written``).
    """
    report = GenerateReport()
    files = render_project(project)
    write_files(repo_root, files, report)
    for rel in stale_skills(repo_root, (f.path for f in files)):
        _remove(repo_root, rel)
        report.removed.append(rel)
    for rel in stale_regions(repo_root, project):
        target = repo_root / rel
        remaining = remove_region(read_text(target), rel)
        if remaining.strip():
            write_text(target, remaining)
            report.written.append(rel)
        else:
            target.unlink()
            report.removed.append(rel)
    return report


# -- machine level ----------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalRender:
    """Machine-level files rendered from a profile, plus warnings (e.g. tier not served)."""

    files: tuple[GeneratedFile, ...]
    warnings: tuple[str, ...] = ()


def agent_for_tool(profile: Profile, tool: str) -> AgentProfile | None:
    """The agent whose models render ``tool``'s subagents: the default agent if it uses
    ``tool``, else the first agent of that tool, else ``None``."""
    candidates = [a for a in profile.agents if a.tool == tool]
    for agent in candidates:
        if agent.id == profile.default_agent:
            return agent
    return candidates[0] if candidates else None


def render_local_from(
    definitions: tuple[AgentDef, ...],
    profile: Profile,
    config: ForgeConfig,
    presets: Mapping[str, Preset] | None,
) -> LocalRender:
    """Render the machine-level subagents of ``definitions`` for every tool in ``profile``."""
    files: list[GeneratedFile] = []
    warnings: list[str] = []
    for adapter in LOCAL_ADAPTERS:
        agent = agent_for_tool(profile, adapter.TOOL)
        if agent is None:
            continue
        rendered, notes = adapter.render_local(definitions, agent, config, presets)
        files += rendered
        warnings += notes
    return LocalRender(files=tuple(files), warnings=tuple(warnings))


def render_local(
    repo_root: Path,
    profile: Profile,
    config: ForgeConfig,
    *,
    presets: Mapping[str, Preset] | None = None,
) -> LocalRender:
    """Render `.forge/agents/*.md` into machine-level subagent files (ADR 0014).

    For each tool present in the profile (``claude-code``, ``codex``) one agent is used:
    the default agent if it uses that tool, else the first one. ``presets`` defaults to the
    shipped presets (used to know whether a tool supports effort). Writes nothing.
    """
    effective = load_presets() if presets is None else presets
    return render_local_from(load_agents(repo_root), profile, config, effective)


def is_ignored(git: Git, rel: Path) -> bool:
    """True if git would ignore ``rel`` (``git check-ignore``; tracked files are not ignored)."""
    proc = git.run("check-ignore", "-q", "--", rel.as_posix(), check=False)
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise GitError(("check-ignore", "-q", "--", rel.as_posix()), proc.returncode, proc.stderr)


def _pattern_for(rel: Path) -> str:
    for pattern in LOCAL_PATTERNS:
        if rel.parent.as_posix() == pattern.rsplit("/", 1)[0]:
            return pattern
    return rel.as_posix()


def stale_local(repo_root: Path, expected: Iterable[Path]) -> list[Path]:
    """Generated machine-level subagent files that the current render does not produce."""
    keep = set(expected)
    stale: list[Path] = []
    for pattern in LOCAL_PATTERNS:
        directory, glob = pattern.rsplit("/", 1)
        base = repo_root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.glob(glob)):
            rel = Path(directory) / path.name
            if rel not in keep and path.is_file() and is_generated(read_text(path)):
                stale.append(rel)
    return stale


def write_local(repo_root: Path, render: LocalRender, git: Git) -> list[Path]:
    """Write the machine-level files of ``render`` and remove stale generated ones.

    Refuses (:class:`PreconditionError`, exit 5) before writing anything when git would not
    ignore one of the files. Returns the repository-relative paths written or removed
    (files already up to date are not listed).
    """
    exposed = [f.path for f in render.files if not is_ignored(git, f.path)]
    if exposed:
        patterns = sorted({_pattern_for(p) for p in exposed})
        raise PreconditionError(
            "refusing to write machine-level files git would not ignore: "
            + ", ".join(p.as_posix() for p in exposed),
            hint="add " + ", ".join(f"`{p}`" for p in patterns) + " to .gitignore "
            "(and untrack the files if they are committed)",
        )
    changed: list[Path] = []
    for file in render.files:
        target = repo_root / file.path
        if _read(target) != file.content:
            write_text(target, file.content)
            changed.append(file.path)
    for rel in stale_local(repo_root, (f.path for f in render.files)):
        (repo_root / rel).unlink()
        changed.append(rel)
    return changed


def write_local_best_effort(
    worktree: Path,
    profile: Profile,
    config: ForgeConfig,
    *,
    presets: Mapping[str, Preset] | None = None,
) -> tuple[list[Path], list[str]]:
    """Render and write the machine-level files into ``worktree`` without ever raising.

    Used for task worktrees (``forge next``, and the re-render after a profile change): the
    subagent definitions are read from the worktree itself, ``config`` comes from the caller.
    Returns the absolute paths written or removed and the warnings, including the reason when
    nothing could be written (for example a ``.gitignore`` without the patterns).
    """
    try:
        render = render_local(worktree, profile, config, presets=presets)
        written = write_local(worktree, render, Git(worktree))
    except Exception as exc:  # best effort by contract: a claim must never fail here
        hint = f" ({exc.hint})" if isinstance(exc, ForgeError) and exc.hint else ""
        message = exc.message if isinstance(exc, ForgeError) else str(exc) or type(exc).__name__
        return [], [f"machine-level adapters not rendered in {worktree}: {message}{hint}"]
    return [worktree / p for p in written], list(render.warnings)


def local_status(repo_root: Path, profile: Profile, config: ForgeConfig, git: Git) -> list[str]:
    """Drift messages for the machine-level files (``forge setup --check``).

    Reports files that are missing, outdated, not ignored by git, or stale. Empty when
    everything is up to date.
    """
    render = render_local(repo_root, profile, config)
    messages: list[str] = []
    for file in render.files:
        current = _read(repo_root / file.path)
        if current is None:
            messages.append(f"missing: {file.path.as_posix()}")
        elif current != file.content:
            messages.append(f"outdated: {file.path.as_posix()}")
        if not is_ignored(git, file.path):
            messages.append(
                f"not ignored by git: {file.path.as_posix()} "
                f"(add `{_pattern_for(file.path)}` to .gitignore)"
            )
    messages += [
        f"stale: {p.as_posix()}" for p in stale_local(repo_root, (f.path for f in render.files))
    ]
    return messages


__all__ = [
    "GENERATED_MARK",
    "LOCAL_PATTERNS",
    "REGION_FILES",
    "RENDERERS",
    "SKILL_DIRS",
    "FileDrift",
    "GenerateReport",
    "GeneratedFile",
    "LocalRender",
    "Project",
    "agent_for_tool",
    "check",
    "expected_content",
    "generate",
    "is_ignored",
    "load_project",
    "local_status",
    "project_from_texts",
    "remove_region",
    "render_local",
    "render_local_from",
    "render_project",
    "stale_local",
    "stale_regions",
    "stale_skills",
    "write_files",
    "write_local",
    "write_local_best_effort",
    "write_text",
]
