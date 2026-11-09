"""Milestone architecture: ``docs/architecture/<M>.md`` and ``overview.md`` (ADR 0019).

``<M>.md`` starts with a YAML frontmatter that the CLI checks::

    ---
    milestone: M1
    status: draft            # draft | ready
    specs: [specs/m1/auth.md]
    components:
      - id: auth-service     # [a-z][a-z0-9-]*
        zones: [app/auth]    # zones of .forge/zones.yml
        summary: Issues and verifies session tokens
    interfaces:
      - id: IF-auth-api      # IF-[a-z0-9-]+
        zone: contracts/api  # a zone with contract: true
        producer: auth-service
        consumers: [web-app]
        summary: REST endpoints for login and token refresh
    ---

Its body must have the level-2 sections of :data:`SECTIONS`. ``overview.md`` holds the
whole system map (``milestones``, ``components``, ``interfaces``); only its frontmatter is
checked.

:func:`load_architecture` and :func:`load_overview` raise :class:`ConfigError` on the first
problem; :func:`validate_architecture` never raises for content problems and returns every
error and warning as a :class:`~forge.core.plan.PlanIssue`. The resolution helpers
(:func:`resolve_component`, :func:`resolve_interface`, ...) serve the plan validator.

This module only reads files; it never touches git or GitHub.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

# A module import, not ``from forge.core.plan import PlanIssue``: plan.py will import this
# module back for the architecture checks, and a module import tolerates the cycle.
from forge.core import plan as _plan
from forge.core.batching import balanced_batches, component_key
from forge.core.errors import ConfigError
from forge.core.graph import natural_key
from forge.core.spec import (
    MILESTONE_RE,
    OpenQuestion,
    load_spec,
    open_questions,
    split_frontmatter,
)
from forge.core.validate import type_name
from forge.core.zones import ZONES_PATH, ZonesConfig

if TYPE_CHECKING:
    from forge.core.plan import PlanIssue

ARCHITECTURE_DIR = Path("docs/architecture")
"""Directory of the architecture files, relative to the repository root."""

OVERVIEW_PATH = ARCHITECTURE_DIR / "overview.md"
"""Location of the system map, relative to the repository root."""

ARCHITECTURE_ROLE = "architecture"
"""The ``models.min_tier_for`` key checked by ``forge arch validate``."""

ArchitectureStatus = Literal["draft", "ready"]
ARCHITECTURE_STATUSES: tuple[ArchitectureStatus, ...] = ("draft", "ready")
"""Allowed values of the frontmatter ``status``."""

SECTIONS: tuple[str, ...] = (
    "Context",
    "Components",
    "Interfaces",
    "Data model",
    "Decisions",
    "Risks",
    "Open questions",
)
"""Mandatory level-2 sections of ``<M>.md`` (matched case-insensitively, outside code)."""

COMPONENT_ID_RE = re.compile(r"[a-z][a-z0-9-]*")
"""Component id grammar. Use with ``fullmatch``."""

INTERFACE_ID_RE = re.compile(r"IF-[a-z0-9-]+")
"""Interface id grammar. Use with ``fullmatch``."""

ARCHITECTURE_KEYS: tuple[str, ...] = ("milestone", "status", "specs", "components", "interfaces")
OVERVIEW_KEYS: tuple[str, ...] = ("milestones", "components", "interfaces")
COMPONENT_KEYS: tuple[str, ...] = ("id", "zones", "summary")
INTERFACE_KEYS: tuple[str, ...] = ("id", "zone", "producer", "consumers", "summary")

STRUCTURAL_CODES: frozenset[str] = frozenset(
    {"frontmatter_invalid", "overview_invalid", "unknown_field", "missing_field", "invalid_field"}
)
"""Codes meaning the frontmatter cannot be read as the schema (the loaders raise on them)."""

_EXAMPLES: dict[str, str] = {
    "milestone": "M1",
    "status": "draft",
    "specs": "[specs/m1/feature.md]",
    "components": "[]",
    "interfaces": "[]",
    "milestones": "[M1]",
    "id": "auth-service",
    "zones": "[app/auth]",
    "zone": "contracts/api",
    "producer": "auth-service",
    "consumers": "[web-app]",
    "summary": '"..."',
}
_ID_EXAMPLES: dict[str, str] = {"component": "auth-service", "interface": "IF-auth-api"}
_HEADING2_RE = re.compile(r"^[ ]{0,3}##(?:[ \t]+(.*?))?[ \t#]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Component:
    """A component: an id, the zones it owns and a one-line summary."""

    id: str
    zones: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class Interface:
    """An interface: its contract zone, its producer and consumers (component ids)."""

    id: str
    zone: str
    producer: str
    consumers: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class Architecture:
    """A parsed ``docs/architecture/<M>.md``. ``path`` is repository-relative (posix)."""

    milestone: str
    status: ArchitectureStatus
    specs: tuple[str, ...]
    components: tuple[Component, ...]
    interfaces: tuple[Interface, ...]
    path: str
    open_questions: tuple[OpenQuestion, ...] = ()
    sections: tuple[str, ...] = ()
    """The mandatory sections (as in :data:`SECTIONS`) present in the body."""

    @property
    def unresolved(self) -> tuple[OpenQuestion, ...]:
        """The open questions not marked ``RESOLVED``."""
        return tuple(q for q in self.open_questions if not q.resolved)

    def component(self, component_id: str) -> Component | None:
        """Return the first component with ``component_id``, or ``None``."""
        return next((c for c in self.components if c.id == component_id), None)

    def interface(self, interface_id: str) -> Interface | None:
        """Return the first interface with ``interface_id``, or ``None``."""
        return next((i for i in self.interfaces if i.id == interface_id), None)


@dataclass(frozen=True)
class Overview:
    """A parsed ``docs/architecture/overview.md`` (frontmatter only)."""

    milestones: tuple[str, ...]
    components: tuple[Component, ...]
    interfaces: tuple[Interface, ...]
    path: str = OVERVIEW_PATH.as_posix()

    def component(self, component_id: str) -> Component | None:
        """Return the first component with ``component_id``, or ``None``."""
        return next((c for c in self.components if c.id == component_id), None)

    def interface(self, interface_id: str) -> Interface | None:
        """Return the first interface with ``interface_id``, or ``None``."""
        return next((i for i in self.interfaces if i.id == interface_id), None)


@dataclass(frozen=True)
class ArchitectureValidation:
    """Result of :func:`validate_architecture`.

    ``architecture`` and ``overview`` are ``None`` when the file is missing or its frontmatter
    cannot be read as the schema (an error with a code in :data:`STRUCTURAL_CODES`). ``status``,
    ``specs``, ``components`` and ``interfaces`` are what could be read from ``<M>.md`` even
    then (ids only, in file order).
    """

    milestone: str
    file: str
    architecture: Architecture | None
    overview: Overview | None
    overview_present: bool
    errors: tuple[PlanIssue, ...]
    warnings: tuple[PlanIssue, ...]
    status: str | None = None
    specs: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    interfaces: tuple[str, ...] = ()
    overview_listed: bool = False

    @property
    def ok(self) -> bool:
        """True when there are no errors (warnings are allowed)."""
        return not self.errors

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` payload of ``forge arch validate`` (ADR 0019)."""
        return {
            "milestone": self.milestone,
            "file": self.file,
            "ok": self.ok,
            "status": self.status,
            "specs": list(self.specs),
            "components": list(self.components),
            "interfaces": list(self.interfaces),
            "overview": {
                "path": OVERVIEW_PATH.as_posix(),
                "present": self.overview_present,
                "listed": self.overview_listed,
            },
            "errors": [i.to_data() for i in self.errors],
            "warnings": [i.to_data() for i in self.warnings],
            "counts": {
                "components": len(self.components),
                "interfaces": len(self.interfaces),
                "errors": len(self.errors),
                "warnings": len(self.warnings),
            },
        }


# --------------------------------------------------------------------------- paths


def architecture_path(milestone: str) -> Path:
    """Location of the architecture of ``milestone``, relative to the repository root."""
    return ARCHITECTURE_DIR / f"{milestone}.md"


# --------------------------------------------------------------------------- resolution


def resolve_component(
    architecture: Architecture | None, overview: Overview | None, component_id: str
) -> Component | None:
    """Resolve ``component_id`` against ``<M>.md`` first, then ``overview.md``."""
    for source in (architecture, overview):
        if source is not None and (found := source.component(component_id)) is not None:
            return found
    return None


def resolve_interface(
    architecture: Architecture | None, overview: Overview | None, interface_id: str
) -> Interface | None:
    """Resolve ``interface_id`` against ``<M>.md`` first, then ``overview.md``."""
    for source in (architecture, overview):
        if source is not None and (found := source.interface(interface_id)) is not None:
            return found
    return None


def milestone_interfaces(architecture: Architecture | None) -> dict[str, Interface]:
    """The interfaces declared in ``<M>.md`` (new or changed by the milestone), by id.

    Each needs exactly one contract task in ``plans/<M>.yml``. The first declaration of a
    duplicated id wins.
    """
    result: dict[str, Interface] = {}
    if architecture is not None:
        for item in architecture.interfaces:
            result.setdefault(item.id, item)
    return result


def existing_interfaces(
    architecture: Architecture | None, overview: Overview | None
) -> dict[str, Interface]:
    """The interfaces of ``overview.md`` that ``<M>.md`` does not declare, by id.

    A plan may use them but never implements them (ADR 0019, point 4).
    """
    own = milestone_interfaces(architecture)
    result: dict[str, Interface] = {}
    if overview is not None:
        for item in overview.interfaces:
            if item.id not in own:
                result.setdefault(item.id, item)
    return result


def interface_producer(
    architecture: Architecture | None, overview: Overview | None, interface_id: str
) -> str | None:
    """The ``producer`` of ``interface_id`` (``<M>.md`` first), or ``None`` when unknown."""
    found = resolve_interface(architecture, overview, interface_id)
    return None if found is None else found.producer


def component_zones(
    architecture: Architecture | None, overview: Overview | None, component_id: str
) -> tuple[str, ...] | None:
    """The ``zones`` of ``component_id`` (``<M>.md`` first), or ``None`` when unknown."""
    found = resolve_component(architecture, overview, component_id)
    return None if found is None else found.zones


# --------------------------------------------------------------------------- issues


class _Issues:
    """Collects errors and warnings in detection order."""

    def __init__(self) -> None:
        self.errors: list[PlanIssue] = []
        self.warnings: list[PlanIssue] = []

    def error(
        self,
        code: str,
        message: str,
        *,
        file: str | None,
        field: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.errors.append(
            _plan.PlanIssue("error", code, message, file=file, field=field, hint=hint)
        )

    def warn(
        self,
        code: str,
        message: str,
        *,
        file: str | None,
        field: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.warnings.append(
            _plan.PlanIssue("warning", code, message, file=file, field=field, hint=hint)
        )

    def from_config(self, code: str, exc: ConfigError) -> None:
        self.error(code, exc.problem, file=exc.file, field=exc.key, hint=exc.hint)

    def structural(self, file: str) -> list[PlanIssue]:
        """The errors of ``file`` that mean its frontmatter cannot be read as the schema."""
        return [i for i in self.errors if i.file == file and i.code in STRUCTURAL_CODES]


# --------------------------------------------------------------------------- lenient parsing


class _Reader:
    """Reads one mapping of a frontmatter, recording every schema problem as an issue."""

    def __init__(self, data: Mapping[str, Any], file: str, prefix: str, issues: _Issues) -> None:
        self.data = data
        self.file = file
        self.prefix = prefix
        self.issues = issues

    def path(self, key: str) -> str:
        return f"{self.prefix}.{key}" if self.prefix else key

    def invalid(self, key: str, problem: str, hint: str) -> None:
        self.issues.error("invalid_field", problem, file=self.file, field=self.path(key), hint=hint)

    def check_keys(self, allowed: tuple[str, ...]) -> None:
        """Report unknown keys and required keys that are missing or null (all are required)."""
        for key in self.data:
            if key not in allowed:
                matches = difflib.get_close_matches(key, list(allowed), n=1, cutoff=0.6)
                guess = f"did you mean `{matches[0]}`? " if matches else ""
                self.issues.error(
                    "unknown_field",
                    f"unknown key {key!r}",
                    file=self.file,
                    field=self.path(key),
                    hint=f"{guess}remove it; allowed keys: {', '.join(allowed)}",
                )
        for key in allowed:
            if self.data.get(key) is None:
                example = _EXAMPLES.get(key, '"..."')
                if key in self.data:
                    problem, hint = "is empty (null)", f"give it a value: `{key}: {example}`"
                else:
                    problem, hint = "required key is missing", f"add `{key}: {example}`"
                self.issues.error(
                    "missing_field", problem, file=self.file, field=self.path(key), hint=hint
                )

    def string(self, key: str) -> str | None:
        """The string at ``key``; ``None`` when absent (reported already) or of another type."""
        value = self.data.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            self.invalid(
                key,
                f"expected a string, got {type_name(value)}",
                f"write it as `{key}: {_EXAMPLES.get(key, '...')}`",
            )
            return None
        return value

    def strings(self, key: str) -> list[str] | None:
        """The list of strings at ``key``; non-string items are reported and skipped."""
        value = self.data.get(key)
        if value is None:
            return None
        if not isinstance(value, list):
            self.invalid(
                key,
                f"expected a list of strings, got {type_name(value)}",
                f"write it as `{key}: {_EXAMPLES.get(key, '[...]')}`",
            )
            return None
        result: list[str] = []
        for i, item in enumerate(value):
            if isinstance(item, str):
                result.append(item)
            else:
                self.invalid(
                    f"{key}[{i}]",
                    f"expected a string, got {type_name(item)}",
                    f'quote the item: `- "{item}"`',
                )
        return result

    def mappings(self, key: str) -> list[_Reader]:
        """One reader per mapping item of the list at ``key``; other items are reported."""
        value = self.data.get(key)
        if value is None:
            return []
        if not isinstance(value, list):
            self.invalid(
                key,
                f"expected a list of mappings, got {type_name(value)}",
                f"write it as `{key}: []` or a list of `- id: ...` items",
            )
            return []
        readers: list[_Reader] = []
        for i, item in enumerate(value):
            item_key = f"{key}[{i}]"
            if not isinstance(item, dict):
                self.invalid(
                    item_key,
                    f"expected a mapping, got {type_name(item)}",
                    "write each item as `- id: ...` followed by its keys",
                )
                continue
            data = {str(k): v for k, v in item.items()}
            readers.append(_Reader(data, self.file, self.path(item_key), self.issues))
        return readers


@dataclass
class _Items:
    """Components and interfaces read leniently, with the field path of each one."""

    components: list[tuple[Component, str]] = field(default_factory=list)
    interfaces: list[tuple[Interface, str]] = field(default_factory=list)


def _read_items(top: _Reader) -> _Items:
    """Read ``components`` and ``interfaces``; an item is kept when its ``id`` is a string."""
    items = _Items()
    for reader in top.mappings("components"):
        reader.check_keys(COMPONENT_KEYS)
        cid = reader.string("id")
        zones = reader.strings("zones")
        summary = reader.string("summary")
        if cid is not None:
            component = Component(cid, tuple(zones or ()), summary or "")
            items.components.append((component, reader.prefix))
    for reader in top.mappings("interfaces"):
        reader.check_keys(INTERFACE_KEYS)
        iid = reader.string("id")
        zone = reader.string("zone")
        producer = reader.string("producer")
        consumers = reader.strings("consumers")
        summary = reader.string("summary")
        if iid is not None:
            interface = Interface(
                iid, zone or "", producer or "", tuple(consumers or ()), summary or ""
            )
            items.interfaces.append((interface, reader.prefix))
    return items


def _read_text(root: Path, rel: str) -> str:
    """Read ``root/rel``; raises :class:`ConfigError` when it cannot be read."""
    try:
        return (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(rel, None, f"cannot read file: {exc}", "check the file") from exc


def _relative_path_problem(value: str) -> str | None:
    path = PurePosixPath(value)
    if not value.strip() or path.is_absolute() or ".." in path.parts or "\\" in value:
        return f"{value!r} is not a relative path"
    return None


@dataclass
class _ArchRead:
    """``<M>.md`` read leniently."""

    milestone: str | None
    status: str | None
    specs: list[tuple[str, str]]
    items: _Items
    body: str


def _read_architecture(root: Path, rel: str, issues: _Issues) -> _ArchRead | None:
    """Read the frontmatter of ``<M>.md`` leniently; ``None`` when it cannot be read at all."""
    try:
        data, body = split_frontmatter(_read_text(root, rel), rel)
    except ConfigError as exc:
        issues.from_config("frontmatter_invalid", exc)
        return None
    top = _Reader(data, rel, "", issues)
    top.check_keys(ARCHITECTURE_KEYS)
    milestone = top.string("milestone")
    status = top.string("status")
    if status is not None and status not in ARCHITECTURE_STATUSES:
        top.invalid(
            "status",
            f"invalid value {status!r}; expected one of: {', '.join(ARCHITECTURE_STATUSES)}",
            "write it as `status: draft` or `status: ready`",
        )
    specs: list[tuple[str, str]] = []
    raw_specs = top.strings("specs")
    if raw_specs is not None:
        if not top.data.get("specs"):
            top.invalid(
                "specs",
                "must list at least one spec",
                "add the milestone specs: `specs: [specs/m1/feature.md]`",
            )
        for i, spec in enumerate(top.data["specs"]):
            if not isinstance(spec, str):
                continue
            problem = _relative_path_problem(spec)
            if problem is not None:
                top.invalid(
                    f"specs[{i}]",
                    problem,
                    "write a path relative to the repo root, e.g. `specs/m1/feature.md`",
                )
                continue
            specs.append((spec, f"specs[{i}]"))
    return _ArchRead(milestone, status, specs, _read_items(top), body)


@dataclass
class _OverviewRead:
    """``overview.md`` read leniently."""

    milestones: list[str]
    items: _Items


def _read_overview(root: Path, rel: str, issues: _Issues) -> _OverviewRead | None:
    """Read the frontmatter of ``overview.md`` leniently; ``None`` when it cannot be read."""
    try:
        data, _body = split_frontmatter(_read_text(root, rel), rel)
    except ConfigError as exc:
        issues.from_config("overview_invalid", exc)
        return None
    top = _Reader(data, rel, "", issues)
    top.check_keys(OVERVIEW_KEYS)
    milestones: list[str] = []
    for i, value in enumerate(top.strings("milestones") or []):
        if MILESTONE_RE.fullmatch(value) is None:
            top.invalid(f"milestones[{i}]", f"invalid milestone {value!r}", "use the form `M1`")
        else:
            milestones.append(value)
    return _OverviewRead(milestones, _read_items(top))


# --------------------------------------------------------------------------- checks


def _check_items(
    items: _Items,
    known_components: set[str],
    zones: ZonesConfig,
    file: str,
    issues: _Issues,
) -> None:
    """Check ids, duplicates, zones, contract zones and producer/consumers of one file."""
    zones_file = ZONES_PATH.as_posix()
    contract = zones.contract_zones()
    for kind, entries, regex in (
        ("component", [(c.id, p) for c, p in items.components], COMPONENT_ID_RE),
        ("interface", [(i.id, p) for i, p in items.interfaces], INTERFACE_ID_RE),
    ):
        seen: set[str] = set()
        for item_id, prefix in entries:
            if regex.fullmatch(item_id) is None:
                issues.error(
                    "invalid_id",
                    f"invalid {kind} id {item_id!r}",
                    file=file,
                    field=f"{prefix}.id",
                    hint=f"use the form `{regex.pattern}`, e.g. `{_ID_EXAMPLES[kind]}`",
                )
            if item_id in seen:
                issues.error(
                    "duplicate_id",
                    f"duplicate {kind} id {item_id!r}",
                    file=file,
                    field=f"{prefix}.id",
                    hint=f"give each {kind} a unique id",
                )
            seen.add(item_id)
    for component, prefix in items.components:
        for i, zone in enumerate(component.zones):
            if zone not in zones.zones:
                issues.error(
                    "unknown_zone",
                    f"unknown zone {zone!r}: it is not defined in {zones_file}",
                    file=file,
                    field=f"{prefix}.zones[{i}]",
                    hint=f"add the zone to {zones_file} or fix the name",
                )
    for interface, prefix in items.interfaces:
        if interface.zone:
            if interface.zone not in zones.zones:
                issues.error(
                    "unknown_zone",
                    f"unknown zone {interface.zone!r}: it is not defined in {zones_file}",
                    file=file,
                    field=f"{prefix}.zone",
                    hint=f"add the zone to {zones_file} or fix the name",
                )
            elif interface.zone not in contract:
                issues.error(
                    "interface_zone_not_contract",
                    f"interface {interface.id} is in zone {interface.zone!r}, "
                    "which is not a contract zone",
                    file=file,
                    field=f"{prefix}.zone",
                    hint=f"mark the zone `contract: true` in {zones_file} "
                    "or choose a contract zone",
                )
        refs = [("producer", "producer", interface.producer)] if interface.producer else []
        refs += [(f"consumers[{i}]", "consumer", c) for i, c in enumerate(interface.consumers)]
        for key, role, name in refs:
            if name not in known_components:
                issues.error(
                    "unknown_component",
                    f"interface {interface.id} names unknown component {name!r} as {role}",
                    file=file,
                    field=f"{prefix}.{key}",
                    hint="declare the component in `components`",
                )


def _level2_headings(body: str) -> set[str]:
    """Case-folded titles of the level-2 headings of ``body``, fenced code ignored."""
    titles: set[str] = set()
    fence: str | None = None
    for line in body.splitlines():
        match = _FENCE_RE.match(line)
        if fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            continue
        if match:
            fence = match.group(1)
            continue
        heading = _HEADING2_RE.match(line)
        if heading is not None:
            titles.add((heading.group(1) or "").strip().casefold())
    return titles


def _check_specs(
    root: Path, milestone: str, specs: Iterable[tuple[str, str]], file: str, issues: _Issues
) -> None:
    for path, key in specs:
        try:
            spec = load_spec(root, path)
        except ConfigError as exc:
            code = "spec_missing" if exc.problem == "spec file not found" else "spec_invalid"
            issues.error(
                code,
                f"{path}: {exc.problem}",
                file=file,
                field=key,
                hint=exc.hint,
            )
            continue
        if spec.milestone != milestone:
            issues.error(
                "spec_milestone_mismatch",
                f"{path} belongs to milestone {spec.milestone}, not {milestone}",
                file=file,
                field=key,
                hint=f"list only specs of {milestone}",
            )


def _check_body(read: _ArchRead, file: str, issues: _Issues) -> None:
    """Check the mandatory sections and, for a ready file, the open questions."""
    present = _level2_headings(read.body)
    for section in SECTIONS:
        if section.casefold() not in present:
            issues.error(
                "missing_section",
                f"missing section `## {section}`",
                file=file,
                field=section,
                hint=f"add `## {section}`",
            )
    unresolved = [q for q in open_questions(read.body) if not q.resolved]
    if read.status == "ready" and unresolved:
        listed = "; ".join(q.text for q in unresolved[:3])
        more = f" (+{len(unresolved) - 3} more)" if len(unresolved) > 3 else ""
        issues.error(
            "open_questions",
            f"status is ready with {len(unresolved)} unresolved open question(s): {listed}{more}",
            file=file,
            field="Open questions",
            hint="answer each question and mark it RESOLVED, or delete it",
        )


def _warn_stale(
    milestone: str,
    read: _ArchRead | None,
    overview: _OverviewRead | None,
    present: bool,
    issues: _Issues,
) -> None:
    hint = "update overview.md in the same PR"
    over = OVERVIEW_PATH.as_posix()
    if not present:
        issues.warn("overview_stale", f"{over} is missing", file=over, hint=hint)
        return
    if overview is None:
        return  # unreadable: already an error
    if milestone not in overview.milestones:
        issues.warn(
            "overview_stale",
            f"{over} does not list {milestone} in `milestones`",
            file=over,
            field="milestones",
            hint=hint,
        )
        return
    if read is None:
        return
    have_c = {c.id for c, _ in overview.items.components}
    have_i = {i.id for i, _ in overview.items.interfaces}
    missing = [c.id for c, _ in read.items.components if c.id not in have_c]
    missing += [i.id for i, _ in read.items.interfaces if i.id not in have_i]
    if missing:
        unique = list(dict.fromkeys(missing))
        issues.warn(
            "overview_stale",
            f"{over} lists {milestone} but lacks {', '.join(unique)}",
            file=over,
            hint=hint,
        )


def _warn_unused(
    read: _ArchRead, overview: _OverviewRead | None, file: str, issues: _Issues
) -> None:
    linked: set[str] = set()
    interfaces = [i for i, _ in read.items.interfaces]
    if overview is not None:
        interfaces += [i for i, _ in overview.items.interfaces]
    for interface in interfaces:
        linked.add(interface.producer)
        linked.update(interface.consumers)
    for component, prefix in read.items.components:
        if not component.zones and component.id not in linked:
            issues.warn(
                "component_unused",
                f"component {component.id} has no zones and no interface",
                file=file,
                field=prefix,
                hint="give it zones, connect it to an interface, or remove it",
            )


# --------------------------------------------------------------------------- building


def _build_architecture(read: _ArchRead, rel: str) -> Architecture:
    present = _level2_headings(read.body)
    sections = tuple(s for s in SECTIONS if s.casefold() in present)
    questions = tuple(open_questions(read.body))
    status: ArchitectureStatus = "ready" if read.status == "ready" else "draft"
    return Architecture(
        milestone=read.milestone or "",
        status=status,
        specs=tuple(p for p, _ in read.specs),
        components=tuple(c for c, _ in read.items.components),
        interfaces=tuple(i for i, _ in read.items.interfaces),
        path=rel,
        open_questions=questions,
        sections=sections,
    )


def _raise_first(issues: list[PlanIssue]) -> None:
    if issues:
        first = issues[0]
        raise ConfigError(first.file or "", first.field, first.message, first.hint or "")


def load_architecture(repo_root: Path, milestone: str) -> Architecture:
    """Load ``docs/architecture/<milestone>.md`` under ``repo_root``.

    Raises :class:`ConfigError` (file, key and fix) when the milestone is not ``M<n>``, when
    the file is missing or unreadable, or when its frontmatter cannot be read as the schema
    (missing, unknown or mistyped key, invalid ``status``, empty ``specs``). Ids, zones,
    specs, sections and open questions are not checked here: use
    :func:`validate_architecture`.
    """
    rel = architecture_path(milestone).as_posix()
    if MILESTONE_RE.fullmatch(milestone) is None:
        raise ConfigError(rel, None, f"invalid milestone {milestone!r}", "use the form `M1`")
    if not (repo_root / rel).is_file():
        raise ConfigError(
            rel,
            None,
            "architecture file not found",
            f"run `/forge-architect {milestone}` or copy "
            f"{ARCHITECTURE_DIR.as_posix()}/_template.md",
        )
    issues = _Issues()
    read = _read_architecture(repo_root, rel, issues)
    _raise_first(issues.structural(rel))
    assert read is not None
    return _build_architecture(read, rel)


def load_overview(repo_root: Path) -> Overview | None:
    """Load ``docs/architecture/overview.md``; ``None`` when the file does not exist.

    Raises :class:`ConfigError` like :func:`load_architecture` when the frontmatter cannot be
    read as the schema. Ids, zones and references are checked by
    :func:`validate_architecture`.
    """
    rel = OVERVIEW_PATH.as_posix()
    if not (repo_root / rel).is_file():
        return None
    issues = _Issues()
    read = _read_overview(repo_root, rel, issues)
    _raise_first(issues.structural(rel))
    assert read is not None
    return _overview_from(read)


def _overview_from(read: _OverviewRead) -> Overview:
    return Overview(
        milestones=tuple(read.milestones),
        components=tuple(c for c, _ in read.items.components),
        interfaces=tuple(i for i, _ in read.items.interfaces),
    )


# --------------------------------------------------------------------------- validation


def validate_architecture(
    repo_root: Path, milestone: str, *, zones: ZonesConfig
) -> ArchitectureValidation:
    """Validate ``docs/architecture/<milestone>.md`` and ``overview.md`` offline (ADR 0019).

    Collects every error and warning of both files; a missing ``<M>.md`` does not hide the
    problems of ``overview.md``. ``zones`` is the parsed ``.forge/zones.yml``. Never raises
    for content problems.
    """
    issues = _Issues()
    if MILESTONE_RE.fullmatch(milestone) is None:
        issues.error(
            "invalid_milestone",
            f"invalid milestone {milestone!r}",
            file=None,
            hint="use the form `M1`, e.g. `forge arch validate M1`",
        )
        return ArchitectureValidation(
            milestone=milestone,
            file="",
            architecture=None,
            overview=None,
            overview_present=False,
            errors=tuple(issues.errors),
            warnings=(),
        )

    rel = architecture_path(milestone).as_posix()
    over_rel = OVERVIEW_PATH.as_posix()

    overview_present = (repo_root / over_rel).is_file()
    overview_read = _read_overview(repo_root, over_rel, issues) if overview_present else None
    if overview_read is not None:
        own = {c.id for c, _ in overview_read.items.components}
        _check_items(overview_read.items, own, zones, over_rel, issues)

    read: _ArchRead | None = None
    if not (repo_root / rel).is_file():
        issues.error(
            "architecture_missing",
            "architecture file not found",
            file=rel,
            hint=f"run `/forge-architect {milestone}` or copy "
            f"{ARCHITECTURE_DIR.as_posix()}/_template.md",
        )
    else:
        read = _read_architecture(repo_root, rel, issues)

    architecture: Architecture | None = None
    if read is not None:
        if read.milestone is not None and read.milestone != milestone:
            issues.error(
                "milestone_mismatch",
                f"the file declares milestone {read.milestone!r} but it is the architecture "
                f"of {milestone}",
                file=rel,
                field="milestone",
                hint=f"write `milestone: {milestone}` or rename the file",
            )
        _check_specs(repo_root, milestone, read.specs, rel, issues)
        known = {c.id for c, _ in read.items.components}
        if overview_read is not None:
            known |= {c.id for c, _ in overview_read.items.components}
        _check_items(read.items, known, zones, rel, issues)
        _check_body(read, rel, issues)
        if not issues.structural(rel):
            architecture = _build_architecture(read, rel)

    _warn_stale(milestone, read, overview_read, overview_present, issues)
    if read is not None:
        _warn_unused(read, overview_read, rel, issues)

    overview: Overview | None = None
    if overview_read is not None and not issues.structural(over_rel):
        overview = _overview_from(overview_read)

    return ArchitectureValidation(
        milestone=milestone,
        file=rel,
        architecture=architecture,
        overview=overview,
        overview_present=overview_present,
        errors=tuple(issues.errors),
        warnings=tuple(issues.warnings),
        status=None if read is None else read.status,
        specs=() if read is None else tuple(p for p, _ in read.specs),
        components=() if read is None else tuple(c.id for c, _ in read.items.components),
        interfaces=() if read is None else tuple(i.id for i, _ in read.items.interfaces),
        overview_listed=overview_read is not None and milestone in overview_read.milestones,
    )


# --------------------------------------------------------------------------- split (ADR 0020)

ARCH_WRITER = "arch-writer"
"""Name of the architecture writer definition in ``.forge/agents/`` (``forge-arch-writer``)."""

ARCH_LOCAL_DIR = Path(".forge/local/arch")
"""Git-ignored directory of the architecture fragments, one sub-directory per milestone."""

SPLIT_TOLERATED_CODES: frozenset[str] = frozenset({"missing_section"})
"""Error codes ``forge arch split`` tolerates: the body is still being written."""

SPLIT_PRECONDITION_CODES: frozenset[str] = frozenset({"architecture_missing", "not_ignored"})
"""Error codes of ``forge arch split`` that are failed preconditions (exit 5)."""

_FIRST_CODES: frozenset[str] = frozenset({"invalid_milestone", "architecture_missing"})
"""Errors ``forge arch split`` reports before the ``not_ignored`` check."""


def arch_split_dir(milestone: str) -> Path:
    """Directory of the fragments of ``milestone``, relative to the repository root."""
    return ARCH_LOCAL_DIR / milestone


def arch_part_path(milestone: str, index: int) -> Path:
    """Path of the fragment of batch ``index`` (from 1), relative to the repository root."""
    return arch_split_dir(milestone) / f"part-{index}.md"


@dataclass(frozen=True)
class ArchBatch:
    """One group of components for one ``forge-arch-writer``, with the interfaces they produce.

    ``index`` starts at 1; ``part_path`` is repository-relative (posix).
    """

    index: int
    component_ids: tuple[str, ...]
    interface_ids: tuple[str, ...]
    part_path: str

    def to_data(self) -> dict[str, Any]:
        """JSON view: ``{"index", "component_ids", "interface_ids", "part_path"}``."""
        return {
            "index": self.index,
            "component_ids": list(self.component_ids),
            "interface_ids": list(self.interface_ids),
            "part_path": self.part_path,
        }


@dataclass(frozen=True)
class ArchSplit:
    """Result of :func:`split_architecture`.

    ``errors`` are the blocking errors of :func:`validate_architecture` (every error except
    :data:`SPLIT_TOLERATED_CODES`); when there are any, ``batches`` is empty.
    ``unassigned_interfaces`` are the interfaces of ``<M>.md`` whose producer is declared only
    in ``overview.md``: the main session writes them. ``removed_parts`` is filled by
    :func:`write_arch_split`.
    """

    milestone: str
    file: str
    max_per_agent: int
    batches: tuple[ArchBatch, ...] = ()
    unassigned_interfaces: tuple[str, ...] = ()
    errors: tuple[PlanIssue, ...] = ()
    warnings: tuple[PlanIssue, ...] = ()
    removed_parts: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when there are no blocking errors."""
        return not self.errors

    @property
    def agents(self) -> int:
        """Number of writer sessions (the number of batches)."""
        return len(self.batches)

    @property
    def missing(self) -> bool:
        """True when ``<M>.md`` does not exist (a precondition, exit 5)."""
        return any(e.code == "architecture_missing" for e in self.errors)

    @property
    def precondition_failed(self) -> bool:
        """True when an error is a failed precondition (exit 5)."""
        return any(e.code in SPLIT_PRECONDITION_CODES for e in self.errors)

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` of ``forge arch split`` (without the ``subagent`` block)."""
        return {
            "milestone": self.milestone,
            "file": self.file,
            "agents": self.agents,
            "max_per_agent": self.max_per_agent,
            "batches": [b.to_data() for b in self.batches],
            "unassigned_interfaces": list(self.unassigned_interfaces),
            "removed_parts": list(self.removed_parts),
            "errors": [i.to_data() for i in self.errors],
            "warnings": [i.to_data() for i in self.warnings],
        }


def split_architecture(
    repo_root: Path,
    milestone: str,
    *,
    zones: ZonesConfig,
    max_per_agent: int,
    is_ignored: Callable[[Path], bool],
) -> ArchSplit:
    """Group the components of ``docs/architecture/<milestone>.md`` for the detail phase.

    Runs :func:`validate_architecture`. An invalid milestone or a missing ``<M>.md`` is
    reported first; then ``.forge/local/arch/<M>/`` not ignored by git per ``is_ignored``
    (``not_ignored``, a precondition: fragments must never be committed); then any other
    error except ``missing_section`` refuses (``errors`` set, no batches). Otherwise the
    components are cut with :func:`~forge.core.batching.balanced_batches` (``max_per_agent`` is
    ``planning.max_components_per_agent``, key: the component id). Each interface of
    ``<M>.md`` goes to the batch of its ``producer``, or to ``unassigned_interfaces`` when the
    producer is declared only in ``overview.md``. Interface ids are sorted in natural order
    (``IF-2`` before ``IF-10``). No component to distribute gives no batch (``agents`` 0).
    Reads files only; :func:`write_arch_split` does the writing. Raises :class:`ValueError`
    when ``max_per_agent < 1``.
    """
    validation = validate_architecture(repo_root, milestone, zones=zones)
    errors = tuple(e for e in validation.errors if e.code not in SPLIT_TOLERATED_CODES)
    base = ArchSplit(
        milestone=milestone,
        file=validation.file,
        max_per_agent=max_per_agent,
        errors=errors,
        warnings=validation.warnings,
    )
    if any(e.code in _FIRST_CODES for e in errors):
        return replace(base, errors=tuple(e for e in errors if e.code in _FIRST_CODES))
    local = arch_split_dir(milestone)
    if not is_ignored(arch_part_path(milestone, 1)):
        return replace(
            base,
            errors=(
                _plan.PlanIssue(
                    "error",
                    "not_ignored",
                    f"git would not ignore {local.as_posix()}/",
                    file=".gitignore",
                    hint="add `.forge/local/` to `.gitignore`",
                ),
            ),
        )
    if errors:
        return base
    architecture = validation.architecture
    assert architecture is not None  # no error, so the frontmatter was read
    groups = balanced_batches(architecture.components, max_per_agent, lambda c: component_key(c.id))
    owner = {c.id: index for index, group in enumerate(groups, start=1) for c in group}
    by_batch: dict[int, list[str]] = {}
    unassigned: list[str] = []
    for item in milestone_interfaces(architecture).values():
        index = owner.get(item.producer)
        if index is None:
            unassigned.append(item.id)
        else:
            by_batch.setdefault(index, []).append(item.id)
    batches = tuple(
        ArchBatch(
            index=index,
            component_ids=tuple(c.id for c in group),
            interface_ids=tuple(sorted(by_batch.get(index, []), key=natural_key)),
            part_path=arch_part_path(milestone, index).as_posix(),
        )
        for index, group in enumerate(groups, start=1)
    )
    return replace(
        base, batches=batches, unassigned_interfaces=tuple(sorted(unassigned, key=natural_key))
    )


def write_arch_split(repo_root: Path, split: ArchSplit) -> ArchSplit:
    """Prepare ``.forge/local/arch/<M>/`` for the fragments of ``split``.

    Creates the directory and deletes the stale ``part-*.md`` files left there by an earlier
    split (old fragments would not match the new groups). Writes nothing else. Returns
    ``split`` with ``removed_parts`` (repository-relative, sorted). Raises
    :class:`ValueError` when ``split`` has errors.
    """
    if not split.ok:
        raise ValueError("cannot write an architecture split with errors")
    rel = arch_split_dir(split.milestone)
    directory = repo_root / rel
    directory.mkdir(parents=True, exist_ok=True)
    removed: list[str] = []
    for path in sorted(directory.glob("part-*.md")):
        if path.is_file():
            path.unlink()
            removed.append((rel / path.name).as_posix())
    return replace(split, removed_parts=tuple(removed))


__all__ = [
    "ARCHITECTURE_DIR",
    "ARCHITECTURE_KEYS",
    "ARCHITECTURE_ROLE",
    "ARCHITECTURE_STATUSES",
    "ARCH_LOCAL_DIR",
    "ARCH_WRITER",
    "COMPONENT_ID_RE",
    "COMPONENT_KEYS",
    "INTERFACE_ID_RE",
    "INTERFACE_KEYS",
    "OVERVIEW_KEYS",
    "OVERVIEW_PATH",
    "SECTIONS",
    "SPLIT_PRECONDITION_CODES",
    "SPLIT_TOLERATED_CODES",
    "STRUCTURAL_CODES",
    "ArchBatch",
    "ArchSplit",
    "Architecture",
    "ArchitectureStatus",
    "ArchitectureValidation",
    "Component",
    "Interface",
    "Overview",
    "arch_part_path",
    "arch_split_dir",
    "architecture_path",
    "component_zones",
    "existing_interfaces",
    "interface_producer",
    "load_architecture",
    "load_overview",
    "milestone_interfaces",
    "resolve_component",
    "resolve_interface",
    "split_architecture",
    "validate_architecture",
    "write_arch_split",
]
