"""`forge config validate`: check the three project files together (ADR 0018).

Loads `.forge/config.yml`, `.forge/team.yml` and `.forge/zones.yml` independently, so a
broken file never hides the results of the others. Each missing or invalid file gives one
error (from the loader's :class:`ConfigError`: file, key, problem, fix). Files that load are
checked for softer problems, reported as warnings:

- the team has no member with role ``lead``;
- tracked files (``git ls-files``) match no zone, ignoring Forge's own files;
- a zone matches no tracked file (usually a typo in a glob);
- the CLI version does not satisfy ``forge_version``.

The init placeholders (``<ORG>/<REPO>`` in ``github.repo``, ``<github>`` as a login) are
already rejected by the loaders, so they surface as errors, not warnings.

Offline and deterministic: no GitHub, no profile.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

from forge.core.config import CONFIG_PATH, ForgeConfig, check_cli_version, load_config
from forge.core.errors import ConfigError, ForgeError
from forge.core.git import Git
from forge.core.team import TEAM_PATH, Team, load_team
from forge.core.zones import ZONES_PATH, ZonesConfig, glob_matches, load_zones

FileStatus = Literal["ok", "error"]

FORGE_OWN_GLOBS: tuple[str, ...] = (
    ".forge/**",
    ".github/**",
    ".claude/**",
    ".agents/**",
    ".codex/**",
    "AGENTS.md",
    "CLAUDE.md",
)
"""Forge's own files: never reported as unzoned (ADR 0018)."""

UNZONED_SAMPLE_SIZE = 20
"""How many unzoned paths are listed in the result (the count is always exact)."""

_T = TypeVar("_T")


@dataclass(frozen=True)
class FileError:
    """One missing or invalid project file. ``file`` is repository-relative."""

    file: str
    key: str | None
    problem: str
    fix: str | None

    @property
    def text(self) -> str:
        """``file: key: problem`` (the key is omitted when the error concerns the whole file)."""
        where = self.file if self.key is None else f"{self.file}: {self.key}"
        return f"{where}: {self.problem}"

    def to_data(self) -> dict[str, Any]:
        """JSON representation."""
        return {"file": self.file, "key": self.key, "problem": self.problem, "fix": self.fix}


@dataclass(frozen=True)
class ConfigWarning:
    """One warning on a file that loaded. ``code`` is stable and machine-readable."""

    code: str
    file: str
    key: str | None
    message: str
    fix: str

    def to_data(self) -> dict[str, Any]:
        """JSON representation."""
        return {
            "code": self.code,
            "file": self.file,
            "key": self.key,
            "message": self.message,
            "fix": self.fix,
        }


@dataclass
class ConfigCheck:
    """Outcome of :func:`check_project`.

    ``leads`` is ``None`` when team.yml did not load; ``unzoned`` (sorted, complete) and
    ``empty_zones`` are ``None`` when zones.yml did not load; ``version_ok`` is ``None``
    when config.yml did not load.
    """

    files: dict[str, FileStatus] = field(default_factory=dict)
    errors: list[FileError] = field(default_factory=list)
    warnings: list[ConfigWarning] = field(default_factory=list)
    leads: list[str] | None = None
    tracked_files: int = 0
    unzoned: list[str] | None = None
    empty_zones: list[str] | None = None
    cli_version: str = ""
    forge_version: str | None = None
    version_ok: bool | None = None

    @property
    def ok(self) -> bool:
        """True when no file has an error (warnings allowed)."""
        return not self.errors

    def to_data(self) -> dict[str, Any]:
        """JSON representation used as the command's ``data``."""
        unzoned = (
            None
            if self.unzoned is None
            else {"count": len(self.unzoned), "sample": self.unzoned[:UNZONED_SAMPLE_SIZE]}
        )
        return {
            "ok": self.ok,
            "files": dict(self.files),
            "errors": [e.to_data() for e in self.errors],
            "warnings": [w.to_data() for w in self.warnings],
            "leads": self.leads,
            "tracked_files": self.tracked_files,
            "unzoned": unzoned,
            "empty_zones": self.empty_zones,
            "version": {
                "cli": self.cli_version,
                "required": self.forge_version,
                "ok": self.version_ok,
            },
            "counts": {"errors": len(self.errors), "warnings": len(self.warnings)},
        }


def _load(
    check: ConfigCheck, rel: Path, loader: Callable[[Path], _T], repo_root: Path
) -> _T | None:
    """Run ``loader``; record the file's status and, on failure, its error."""
    name = rel.as_posix()
    try:
        value = loader(repo_root)
    except ConfigError as exc:
        check.files[name] = "error"
        check.errors.append(FileError(name, exc.key, exc.problem, exc.hint))
        return None
    except ForgeError as exc:
        check.files[name] = "error"
        check.errors.append(FileError(name, None, exc.message, exc.hint))
        return None
    check.files[name] = "ok"
    return value


def _is_forge_own(path: str) -> bool:
    return any(glob_matches(glob, path) for glob in FORGE_OWN_GLOBS)


def _check_version(check: ConfigCheck, config: ForgeConfig, version: str) -> None:
    check.forge_version = str(config.forge_version)
    try:
        check.version_ok = check_cli_version(config.forge_version, version)
    except ValueError:
        check.version_ok = False
    if not check.version_ok:
        check.warnings.append(
            ConfigWarning(
                "version_mismatch",
                CONFIG_PATH.as_posix(),
                "forge_version",
                f"forge {version} does not satisfy forge_version {check.forge_version!r}",
                "install a Forge version matching forge_version, or update forge_version",
            )
        )


def _check_team(check: ConfigCheck, team: Team) -> None:
    check.leads = [m.github for m in team.leads()]
    if not check.leads:
        check.warnings.append(
            ConfigWarning(
                "no_lead",
                TEAM_PATH.as_posix(),
                "members",
                "no member has the role `lead`",
                "add `lead` to the roles of the person who plans milestones, "
                "e.g. `roles: [dev, lead]`",
            )
        )


def _check_zones(check: ConfigCheck, zones: ZonesConfig, tracked: list[str]) -> None:
    file = ZONES_PATH.as_posix()
    candidates = [p for p in tracked if not _is_forge_own(p)]
    check.unzoned = zones.unzoned_paths(candidates)
    if check.unzoned:
        count = len(check.unzoned)
        sample = ", ".join(check.unzoned[:5]) + (", ..." if count > 5 else "")
        check.warnings.append(
            ConfigWarning(
                "unzoned_files",
                file,
                "zones",
                f"{count} tracked file(s) match no zone: {sample}",
                "add zones (or globs to existing zones) that cover these files",
            )
        )
    check.empty_zones = sorted(
        zone.name
        for zone in zones.zones.values()
        if not any(glob_matches(glob, p) for glob in zone.paths for p in tracked)
    )
    for name in check.empty_zones:
        globs = ", ".join(zones.zones[name].paths)
        check.warnings.append(
            ConfigWarning(
                "empty_zone",
                file,
                f"zones.{name}.paths",
                f"zone {name!r} matches no tracked file ({globs})",
                "fix the glob (paths are relative to the repository root) or remove the zone",
            )
        )


def check_project(repo_root: Path, git: Git, *, version: str) -> ConfigCheck:
    """Validate config.yml, team.yml and zones.yml under ``repo_root`` (ADR 0018).

    ``git`` lists the tracked files; ``version`` is the running CLI's version. Never raises
    for a missing or invalid project file: each becomes an error in the result.
    """
    check = ConfigCheck(cli_version=version)
    config = _load(check, CONFIG_PATH, load_config, repo_root)
    team = _load(check, TEAM_PATH, load_team, repo_root)
    zones = _load(check, ZONES_PATH, load_zones, repo_root)
    tracked = git.tracked_files()
    check.tracked_files = len(tracked)
    if config is not None:
        _check_version(check, config, version)
    if team is not None:
        _check_team(check, team)
    if zones is not None:
        _check_zones(check, zones, tracked)
    return check


__all__ = [
    "FORGE_OWN_GLOBS",
    "UNZONED_SAMPLE_SIZE",
    "ConfigCheck",
    "ConfigWarning",
    "FileError",
    "FileStatus",
    "check_project",
]
