"""`forge init`: scaffold a project from the packaged templates (spec §5.2, ADR 0014).

Templates live in ``forge/templates/project/`` and are read with ``importlib.resources``.
Exactly six ``{{name}}`` placeholders are substituted; GitHub Actions expressions
(``${{ … }}``) are left untouched. Init then renders the project-level adapters.

Existing files are never overwritten without ``force``: init lists the conflicts, writes
nothing and raises :class:`PreconditionError` (exit 5). `.gitignore` and `.gitattributes`
are merged (missing lines appended), and `AGENTS.md`/`CLAUDE.md` only get their managed
region updated. Init never commits.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import yaml
from packaging.version import Version

from forge.core.adapters import (
    GeneratedFile,
    expected_content,
    project_from_texts,
    render_project,
    write_text,
)
from forge.core.config import CONFIG_PATH, normalize_adapters
from forge.core.docs import DEFAULT_OUTPUT, CliDoc, registered_cli, render_reference
from forge.core.errors import PreconditionError
from forge.core.git import Git
from forge.core.prompts import AGENTS_DIR, PROMPTS_DIR, RULES_PATH, parse_prompts, read_text

PLACEHOLDERS = (
    "project_name",
    "repo",
    "default_branch",
    "forge_version",
    "forge_install_ref",
    "github_login",
)
"""The only ``{{…}}`` names substituted in templates (ADR 0014)."""

MERGED_FILES = (Path(".gitignore"), Path(".gitattributes"))
"""Files merged line by line instead of overwritten."""

REPO_PLACEHOLDER = "<ORG>/<REPO>"
LOGIN_PLACEHOLDER = "<github>"

_PLACEHOLDER_RE = re.compile(r"(?<!\$)\{\{\s*(" + "|".join(PLACEHOLDERS) + r")\s*\}\}")
_ADAPTERS_LINE_RE = re.compile(r"^adapters:.*$", re.MULTILINE)
_GITHUB_URL_RE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$"
)
_SKIP_NAMES = {"__init__.py", "__pycache__", ".DS_Store"}


@dataclass(frozen=True)
class InitValues:
    """Values substituted into the templates."""

    project_name: str
    repo: str
    default_branch: str
    forge_version: str
    forge_install_ref: str
    github_login: str

    def as_dict(self) -> dict[str, str]:
        """Placeholder name → value."""
        return {name: str(getattr(self, name)) for name in PLACEHOLDERS}


def version_pin(version: str) -> str:
    """Return the ``forge_version`` pin for a CLI version: ``>=X.Y,<X.(Y+1)``."""
    release = Version(version).release
    major = release[0]
    minor = release[1] if len(release) > 1 else 0
    return f">={major}.{minor},<{major}.{minor + 1}"


def install_ref(version: str) -> str:
    """Return the git ref workflows install for a CLI version: ``v<base version>``."""
    return f"v{Version(version).base_version}"


def github_repo_from_url(url: str) -> str | None:
    """Return ``owner/name`` for a GitHub remote URL, else ``None``."""
    m = _GITHUB_URL_RE.match(url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def detect_default_branch(git: Git) -> str:
    """``origin/HEAD``'s branch, else the current branch, else ``main``."""
    proc = git.run("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    if proc.returncode == 0 and proc.stdout.strip().startswith("origin/"):
        return proc.stdout.strip().removeprefix("origin/")
    return git.current_branch() or "main"


def detect_values(
    repo_root: Path, git: Git, *, version: str, github_login: str | None
) -> InitValues:
    """Compute the placeholder values for ``repo_root`` (ADR 0014).

    The project name is the GitHub repository's name when ``origin`` is a GitHub remote
    (a clone may live in a directory with another name), else the directory name.
    """
    proc = git.run("remote", "get-url", "origin", check=False)
    repo = github_repo_from_url(proc.stdout) if proc.returncode == 0 else None
    return InitValues(
        project_name=repo.split("/", 1)[1] if repo else repo_root.name,
        repo=repo or REPO_PLACEHOLDER,
        default_branch=detect_default_branch(git),
        forge_version=version_pin(version),
        forge_install_ref=install_ref(version),
        github_login=github_login or LOGIN_PLACEHOLDER,
    )


def substitute(text: str, values: Mapping[str, str]) -> str:
    """Replace the known ``{{name}}`` placeholders; ``${{ … }}`` and others are untouched."""
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], text)


def templates_root() -> Traversable:
    """The packaged project template directory."""
    return resources.files("forge") / "templates" / "project"


def _walk(node: Traversable, prefix: Path) -> Iterable[tuple[Path, Traversable]]:
    for child in sorted(node.iterdir(), key=lambda c: c.name):
        if child.name in _SKIP_NAMES:
            continue
        rel = prefix / child.name
        if child.is_dir():
            yield from _walk(child, rel)
        elif child.is_file():
            yield rel, child


def template_texts(root: Traversable | None = None) -> dict[Path, str]:
    """Every template file, as ``{repository-relative path: raw text}`` (sorted)."""
    base = root if root is not None else templates_root()
    return {rel: node.read_bytes().decode("utf-8") for rel, node in _walk(base, Path())}


def set_adapters_line(config_text: str, adapters: Sequence[str]) -> str:
    """Record ``adapters`` in a config text: replace its ``adapters:`` line or append one."""
    line = f"adapters: [{', '.join(adapters)}]"
    if _ADAPTERS_LINE_RE.search(config_text):
        return _ADAPTERS_LINE_RE.sub(line, config_text, count=1)
    sep = "" if config_text.endswith("\n") or not config_text else "\n"
    return f"{config_text}{sep}{line}\n"


def render_templates(
    values: InitValues, adapters: Sequence[str], root: Traversable | None = None
) -> dict[Path, str]:
    """Templates with placeholders substituted and the adapters recorded in `config.yml`."""
    mapping = values.as_dict()
    rendered = {rel: substitute(text, mapping) for rel, text in template_texts(root).items()}
    if CONFIG_PATH in rendered:
        rendered[CONFIG_PATH] = set_adapters_line(rendered[CONFIG_PATH], adapters)
    return rendered


def merge_lines(existing: str, template: str) -> str:
    """Append the non-blank lines of ``template`` missing from ``existing``; remove nothing.

    Existing bytes (and their line endings) are preserved.
    """
    present = {line.strip() for line in existing.splitlines()}
    missing = [
        line for line in template.splitlines() if line.strip() and line.strip() not in present
    ]
    if not missing:
        return existing
    newline = "\r\n" if "\r\n" in existing else "\n"
    prefix = existing
    if prefix and not prefix.endswith("\n"):
        prefix += newline
    return prefix + newline.join(missing) + newline


def _project_section(config_text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(config_text)
    except yaml.YAMLError:
        return {}
    section = data.get("project") if isinstance(data, dict) else None
    return section if isinstance(section, dict) else {}


def adapter_files(
    rendered: Mapping[Path, str], values: InitValues, adapters: Sequence[str]
) -> list[GeneratedFile]:
    """Render the project-level adapter files from the in-memory templates."""
    project = _project_section(rendered.get(CONFIG_PATH, ""))
    merge_method = project.get("merge_method")
    return render_project(
        project_from_texts(
            name=values.project_name,
            default_branch=values.default_branch,
            merge_method=merge_method if isinstance(merge_method, str) else "squash",
            adapters=tuple(adapters),
            rules=rendered.get(RULES_PATH, ""),
            prompts={p: t for p, t in rendered.items() if p.parent == PROMPTS_DIR},
            agents={p: t for p, t in rendered.items() if p.parent == AGENTS_DIR},
        )
    )


def commands_reference(rendered: Mapping[Path, str], cli: CliDoc | None = None) -> str:
    """The `docs/forge/commands.md` text for the rendered templates (same as `forge docs`).

    ``cli`` defaults to the registered CLI description (:func:`registered_cli`).
    """
    prompts = parse_prompts({p: t for p, t in rendered.items() if p.parent == PROMPTS_DIR})
    return render_reference(cli if cli is not None else registered_cli(), prompts)


@dataclass
class InitReport:
    """What ``forge init`` did (repository-relative paths)."""

    adapters: tuple[str, ...]
    values: InitValues
    created: list[Path] = field(default_factory=list)
    overwritten: list[Path] = field(default_factory=list)
    merged: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    next_steps: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "adapters": list(self.adapters),
            "values": self.values.as_dict(),
            "created": [p.as_posix() for p in self.created],
            "overwritten": [p.as_posix() for p in self.overwritten],
            "merged": [p.as_posix() for p in self.merged],
            "unchanged": [p.as_posix() for p in self.unchanged],
            "next_steps": list(self.next_steps),
        }


NEXT_STEPS = (
    "Lead: run `/forge-configure` (Codex: `$forge-configure`) to fill .forge/config.yml, "
    ".forge/team.yml and .forge/zones.yml, or edit them by hand; then run "
    "`forge config validate` until it reports no errors.",
    "Run `forge adapters generate` (and `forge docs` for .forge/prompts/) after editing "
    ".forge/config.yml or .forge/prompts/.",
    "Review the files, then commit them (forge init never commits).",
    "Each person then runs `forge setup` on their machine.",
)


def init_project(
    repo_root: Path,
    values: InitValues,
    *,
    adapters: Iterable[str],
    force: bool = False,
    templates: Traversable | None = None,
    cli: CliDoc | None = None,
) -> InitReport:
    """Write the templates, project-level adapter files and command reference.

    ``adapters`` are validated adapter names (``generic`` is added). The command reference
    (`docs/forge/commands.md`, ADR 0015) describes ``cli``, by default the registered CLI
    (:func:`forge.core.docs.registered_cli`). Raises
    :class:`PreconditionError` listing every conflicting file, before writing anything,
    when a file exists with different content and ``force`` is false.
    """
    chosen = normalize_adapters(adapters)
    rendered = render_templates(values, chosen, templates)
    plain: dict[Path, str] = {}
    for rel, text in rendered.items():
        target = repo_root / rel
        if rel in MERGED_FILES and target.is_file():
            plain[rel] = merge_lines(read_text(target), text)
        else:
            plain[rel] = text
    generated = adapter_files(rendered, values, chosen)
    for file in generated:
        if not file.region:
            plain[file.path] = file.content
        else:
            plain[file.path] = expected_content(repo_root, file)
    region_paths = {f.path for f in generated if f.region}
    plain[DEFAULT_OUTPUT] = commands_reference(rendered, cli)

    conflicts: list[Path] = []
    for rel, text in plain.items():
        target = repo_root / rel
        not_a_file = target.exists() and not target.is_file()
        differs = (
            target.is_file()
            and rel not in MERGED_FILES
            and rel not in region_paths
            and read_text(target) != text
        )
        if not_a_file or (differs and not force):
            conflicts.append(rel)
    if conflicts:
        listed = ", ".join(p.as_posix() for p in sorted(conflicts))
        hint = (
            "move or delete them, or re-run with `--force` to overwrite them"
            if not force
            else "remove these paths: they are not regular files"
        )
        raise PreconditionError(f"files already exist: {listed}", hint=hint)

    report = InitReport(adapters=chosen, values=values, next_steps=NEXT_STEPS)
    for rel in sorted(plain):
        target = repo_root / rel
        text = plain[rel]
        if not target.is_file():
            write_text(target, text)
            report.created.append(rel)
        elif read_text(target) == text:
            report.unchanged.append(rel)
        else:
            write_text(target, text)
            if rel in MERGED_FILES or rel in region_paths:
                report.merged.append(rel)
            else:
                report.overwritten.append(rel)
    return report


__all__ = [
    "LOGIN_PLACEHOLDER",
    "MERGED_FILES",
    "NEXT_STEPS",
    "PLACEHOLDERS",
    "REPO_PLACEHOLDER",
    "InitReport",
    "InitValues",
    "adapter_files",
    "commands_reference",
    "detect_default_branch",
    "detect_values",
    "github_repo_from_url",
    "init_project",
    "install_ref",
    "merge_lines",
    "render_templates",
    "set_adapters_line",
    "substitute",
    "template_texts",
    "templates_root",
]
