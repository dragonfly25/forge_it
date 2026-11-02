"""Zones: named groups of path globs used to prevent parallel work on the same code.

Implements spec §6.3 and ADR 0006:

* ``.forge/zones.yml`` loading and validation (:func:`load_zones`, :func:`parse_zones`);
* glob matching with gitignore-like semantics anchored at the repository root
  (:func:`compile_glob`, :func:`glob_matches`);
* path → zones lookup, unzoned detection and out-of-zone detection;
* a conservative glob-intersection test and zone-set overlap with human-readable reasons.

Glob semantics (paths are POSIX, relative to the repository root, without a leading ``./``):

* ``*`` matches any run of characters inside one segment; ``?`` matches one character
  inside one segment. Neither ever matches ``/``.
* ``[...]`` is a character class (``[!...]`` or ``[^...]`` negates it); it never matches
  ``/``. An unterminated ``[`` is a literal character.
* ``**`` as a whole segment matches zero or more whole segments: ``**/x`` matches ``x``
  and ``a/b/x``; ``a/**/x`` matches ``a/x`` and ``a/b/c/x``. A trailing ``a/**`` matches
  everything *under* ``a/`` (one or more segments) but not the file ``a`` itself.
  ``**`` inside a segment (``a**b``) behaves like ``*``.
* Every other character, including ``\\``, is literal.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from forge.core.errors import ForgeError
from forge.core.validate import Fields, load_yaml_mapping, type_name

ZONES_PATH = Path(".forge/zones.yml")
ZONE_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$"
UNKNOWN_ZONE_FIX = f"add it to {ZONES_PATH.as_posix()} or fix the task's zones"

Unzoned = Literal["warn", "error"]

_WILDCARD_CHARS = frozenset("*?[")


# -- data ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Zone:
    """One zone: a name, its normalized path globs and whether it is a contract zone."""

    name: str
    paths: tuple[str, ...]
    contract: bool = False


class OverlapKind(StrEnum):
    """Why two zone sets overlap, in priority order."""

    SHARED = "shared"
    DECLARED_CONFLICT = "declared_conflict"
    GLOB_INTERSECTION = "glob_intersection"


@dataclass(frozen=True)
class Overlap:
    """One reason two zone sets overlap. ``zone_a`` comes from the first set."""

    kind: OverlapKind
    zone_a: str
    zone_b: str
    detail: str


@dataclass(frozen=True)
class ZonesConfig:
    """The parsed content of ``.forge/zones.yml``."""

    zones: dict[str, Zone]
    conflicts: frozenset[frozenset[str]] = frozenset()
    unzoned: Unzoned = "warn"

    def _check_known(self, names: Iterable[str]) -> list[str]:
        """Return ``names`` as a sorted, de-duplicated list; raise on an unknown zone."""
        result = sorted(set(names))
        for name in result:
            if name not in self.zones:
                raise ForgeError(
                    f"unknown zone {name!r}: it is not defined in {ZONES_PATH.as_posix()}",
                    hint=UNKNOWN_ZONE_FIX,
                )
        return result

    def zones_for_path(self, path: str) -> list[str]:
        """Return the sorted names of every zone with a glob matching ``path``."""
        clean = _strip_dot_slash(path)
        return sorted(
            zone.name
            for zone in self.zones.values()
            if any(glob_matches(glob, clean) for glob in zone.paths)
        )

    def unzoned_paths(self, paths: Iterable[str]) -> list[str]:
        """Return the sorted paths that match no zone at all."""
        return sorted({p for p in paths if not self.zones_for_path(p)})

    def contract_zones(self) -> set[str]:
        """Return the names of the zones marked ``contract: true``."""
        return {zone.name for zone in self.zones.values() if zone.contract}

    def files_outside(self, paths: Iterable[str], zone_names: Iterable[str]) -> list[str]:
        """Return the sorted paths not matched by any of ``zone_names`` (unknown → error)."""
        names = self._check_known(zone_names)
        globs = [glob for name in names for glob in self.zones[name].paths]
        return sorted(
            {p for p in paths if not any(glob_matches(g, _strip_dot_slash(p)) for g in globs)}
        )

    def all_overlaps(self, zones_a: Iterable[str], zones_b: Iterable[str]) -> list[Overlap]:
        """Return every reason the two zone sets overlap, in priority order.

        Shared zones come first, then declared conflicts, then glob intersections between
        distinct zones. Within a kind, pairs are sorted by zone name. Unknown zone names
        raise :class:`ForgeError`.
        """
        a = self._check_known(zones_a)
        b = self._check_known(zones_b)
        set_b = set(b)
        shared = [
            Overlap(OverlapKind.SHARED, name, name, f"shared zone {name}")
            for name in a
            if name in set_b
        ]
        declared: list[Overlap] = []
        globbed: list[Overlap] = []
        for x in a:
            for y in b:
                if x == y:
                    continue
                if frozenset((x, y)) in self.conflicts:
                    declared.append(
                        Overlap(OverlapKind.DECLARED_CONFLICT, x, y, f"declared conflict {x}↔{y}")
                    )
                pair = _first_intersecting_globs(self.zones[x].paths, self.zones[y].paths)
                if pair is not None:
                    globbed.append(
                        Overlap(
                            OverlapKind.GLOB_INTERSECTION,
                            x,
                            y,
                            f"globs intersect: {pair[0]} ~ {pair[1]}",
                        )
                    )
        return shared + declared + globbed

    def overlap(self, zones_a: Iterable[str], zones_b: Iterable[str]) -> Overlap | None:
        """Return the highest-priority reason the zone sets overlap, or ``None``.

        Priority: shared zone > declared conflict > glob intersection.
        """
        reasons = self.all_overlaps(zones_a, zones_b)
        return reasons[0] if reasons else None


# -- glob normalization and matching ---------------------------------------------------------


def _strip_dot_slash(path: str) -> str:
    while path.startswith("./"):
        path = path[2:]
    return path


def normalize_glob(glob: str) -> str:
    """Return ``glob`` with any leading ``./`` removed; raise ``ValueError`` if invalid.

    Rejected: empty globs, absolute paths, ``..`` or ``.`` segments, and empty segments
    (``a//b`` or a trailing ``/``; write ``dir/**`` to mean "everything under dir").
    """
    stripped = _strip_dot_slash(glob.strip())
    if not stripped:
        raise ValueError("glob is empty")
    if stripped.startswith("/"):
        raise ValueError("absolute paths are not allowed; globs are relative to the repo root")
    segments = stripped.split("/")
    if ".." in segments:
        raise ValueError("`..` segments are not allowed")
    if "." in segments:
        raise ValueError("`.` segments are not allowed")
    if "" in segments:
        raise ValueError("empty path segment (double or trailing `/`)")
    return stripped


def _segment_regex(segment: str) -> str:
    """Translate one glob segment (no ``/``) into a regular expression fragment."""
    out: list[str] = []
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if c == "*":
            while i + 1 < n and segment[i + 1] == "*":
                i += 1
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            negate = j < n and segment[j] in "!^"
            if negate:
                j += 1
            if j < n and segment[j] == "]":  # a leading `]` is part of the class
                j += 1
            while j < n and segment[j] != "]":
                j += 1
            if j >= n:  # unterminated: literal `[`
                out.append(re.escape(c))
            else:
                body = segment[i + 1 + (1 if negate else 0) : j]
                body = body.replace("\\", "\\\\").replace("[", "\\[")
                out.append(f"[^/{body}]" if negate else f"[{body}]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@lru_cache(maxsize=4096)
def compile_glob(glob: str) -> re.Pattern[str]:
    """Compile a zone glob into a regular expression that must match a whole path."""
    segments = _strip_dot_slash(glob).split("/")
    pieces: list[str] = []
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if seg == "**":
            # trailing: one or more segments; elsewhere: zero or more segments plus `/`
            pieces.append("[^/]+(?:/[^/]+)*" if last else "(?:[^/]+/)*")
        else:
            pieces.append(_segment_regex(seg) + ("" if last else "/"))
    return re.compile("".join(pieces))


@lru_cache(maxsize=65536)
def glob_matches(glob: str, path: str) -> bool:
    """True if the repo-relative POSIX ``path`` is matched by ``glob``."""
    return compile_glob(glob).fullmatch(_strip_dot_slash(path)) is not None


def _is_literal(segment: str) -> bool:
    return not any(c in _WILDCARD_CHARS for c in segment)


def globs_may_intersect(a: str, b: str) -> bool:
    """Conservative test: False only when ``a`` and ``b`` provably match no common path.

    Per ADR 0006 two globs are disjoint only if their literal leading segments diverge
    (``packages/editor/**`` vs ``packages/api/**``), or both are fully literal and differ.
    Everything else counts as intersecting, so ``src/*.py`` vs ``src/*.ts`` is a known
    false positive.
    """
    seg_a = _strip_dot_slash(a).split("/")
    seg_b = _strip_dot_slash(b).split("/")
    if all(map(_is_literal, seg_a)) and all(map(_is_literal, seg_b)):
        return seg_a == seg_b
    for x, y in zip(seg_a, seg_b, strict=False):
        if not (_is_literal(x) and _is_literal(y)):
            return True
        if x != y:
            return False
    return True


def _first_intersecting_globs(
    globs_a: Iterable[str], globs_b: Iterable[str]
) -> tuple[str, str] | None:
    list_b = list(globs_b)
    for ga in globs_a:
        for gb in list_b:
            if globs_may_intersect(ga, gb):
                return ga, gb
    return None


# -- loading ---------------------------------------------------------------------------------


def load_zones(repo_root: Path) -> ZonesConfig:
    """Load and validate ``<repo_root>/.forge/zones.yml``."""
    path = repo_root / ZONES_PATH
    return parse_zones(load_yaml_mapping(path, create_hint="forge init"), path)


def parse_zones(data: Mapping[str, Any], file: Path | str) -> ZonesConfig:
    """Validate the top-level mapping of a zones file and build a :class:`ZonesConfig`."""
    top = Fields(data, file)
    top.reject_unknown(["zones", "conflicts", "unzoned"])

    zones_fields = top.mapping("zones", required=True)
    zones: dict[str, Zone] = {}
    for name in zones_fields.data:
        if re.fullmatch(ZONE_NAME_PATTERN, name) is None:
            raise zones_fields.fail(
                name,
                "invalid zone name",
                "use lowercase letters, digits, `-` and `_`, with `/` between parts "
                "(e.g. `editor/canvas`)",
            )
        zf = zones_fields.mapping(name, required=True)
        zf.reject_unknown(["paths", "contract"])
        raw_paths = zf.str_list("paths", non_empty=True)
        globs: list[str] = []
        for i, raw in enumerate(raw_paths):
            try:
                glob = normalize_glob(raw)
            except ValueError as exc:
                raise zf.fail(
                    f"paths[{i}]",
                    f"invalid glob {raw!r}: {exc}",
                    'write a path relative to the repo root, e.g. "packages/editor/src/**"',
                ) from exc
            if glob not in globs:
                globs.append(glob)
        zones[name] = Zone(name, tuple(globs), zf.bool_("contract", default=False))

    conflicts = _parse_conflicts(top, zones)
    unzoned: Unzoned = (
        "error" if top.choice("unzoned", ["warn", "error"], default="warn") == "error" else "warn"
    )
    return ZonesConfig(zones=zones, conflicts=conflicts, unzoned=unzoned)


def _parse_conflicts(top: Fields, zones: Mapping[str, Zone]) -> frozenset[frozenset[str]]:
    value = top.raw("conflicts")
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise top.fail(
            "conflicts",
            f"expected a list of zone pairs, got {type_name(value)}",
            "write it as `conflicts: [[zone-a, zone-b]]`",
        )
    pairs: set[frozenset[str]] = set()
    for i, item in enumerate(value):
        key = f"conflicts[{i}]"
        if not isinstance(item, list) or len(item) != 2:
            got = f"a list of {len(item)} items" if isinstance(item, list) else type_name(item)
            raise top.fail(
                key,
                f"expected a pair of zone names, got {got}",
                "write it as `- [zone-a, zone-b]`",
            )
        for j, name in enumerate(item):
            if not isinstance(name, str):
                raise top.fail(
                    f"{key}[{j}]",
                    f"expected a zone name, got {type_name(name)}",
                    "write the zone name as a string",
                )
            if name not in zones:
                close = difflib.get_close_matches(name, list(zones), n=1, cutoff=0.6)
                fix = (
                    f"did you mean `{close[0]}`?"
                    if close
                    else "use a zone defined under `zones`, or define it there"
                )
                raise top.fail(f"{key}[{j}]", f"unknown zone {name!r}", fix)
        if item[0] == item[1]:
            raise top.fail(
                key,
                f"a zone cannot conflict with itself ({item[0]!r})",
                "list two different zones",
            )
        pairs.add(frozenset(item))
    return frozenset(pairs)
