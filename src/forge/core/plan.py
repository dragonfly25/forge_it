"""Roadmap and plan files, and the offline ``forge plan validate`` (spec §9.2-9.4, ADR 0013).

* ``plans/roadmap.yml`` (optional) lists the milestones; :func:`load_roadmap` parses it.
* ``plans/<milestone>.yml`` holds the milestone's tasks; :func:`validate_plan` loads it and
  checks it against the zones, the specs, the roadmap, the other plan files and the team.

Validation never touches GitHub or git: it reads files under the repository root only, so it
runs in CI and on a fresh clone. Every problem is a :class:`PlanIssue` naming the file, the
task id and the field where they apply, with a fix.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml

from forge.core.claim import zones_overlap
from forge.core.config import ModelsConfig
from forge.core.errors import ConfigError, PreconditionError
from forge.core.graph import TaskGraph, natural_key
from forge.core.models import can_serve
from forge.core.output import Level, Message
from forge.core.profile import AgentProfile
from forge.core.spec import MILESTONE_RE, Spec, load_spec
from forge.core.tasks import (
    METADATA_KEYS,
    TASK_ID_RE,
    TaskMetadata,
    TaskType,
    description_text_problem,
    parse_metadata_fields,
)
from forge.core.team import Team
from forge.core.validate import Fields, load_yaml_mapping, type_name
from forge.core.zones import ZONES_PATH, ZonesConfig

PLANS_DIR = Path("plans")
"""Directory of the roadmap and the plan files, relative to the repository root."""

ROADMAP_PATH = PLANS_DIR / "roadmap.yml"
"""Location of the roadmap, relative to the repository root."""

PLAN_ROLE = "plan"
"""The ``models.min_tier_for`` key checked by ``forge plan validate`` and ``apply``."""

TASK_KEYS: tuple[str, ...] = (
    "id",
    "title",
    "type",
    "spec",
    "covers",
    "depends_on",
    "zones",
    "size",
    "tier",
    "description",
    "done_when",
    "allow_contract_change",
    "proposal",
)
"""Fields of a plan task: the §7.1 metadata fields plus ``title``, ``description`` and the
optional ``proposal`` (the number of a ``forge propose`` issue the task adopts)."""

OPTIONAL_TASK_KEYS: tuple[str, ...] = ("allow_contract_change", "proposal")

REQUIRED_TASK_KEYS: tuple[str, ...] = tuple(k for k in TASK_KEYS if k not in OPTIONAL_TASK_KEYS)

MAX_ISSUE_TITLE = 256
"""GitHub's limit on an issue title; ``[<id>] <title>`` must fit in it."""

_PLAN_KEYS = ("milestone", "tasks")
_ROADMAP_KEYS = ("milestones",)
_ROADMAP_MILESTONE_KEYS = ("id", "title", "goal", "demo", "lead", "specs")
_EXAMPLES = {
    "id": "M1-T01",
    "title": '"..."',
    "type": "impl",
    "spec": "specs/m1/feature.md",
    "covers": "[AC-1]",
    "depends_on": "[]",
    "zones": "[zone-name]",
    "size": "S",
    "tier": "standard",
    "description": '"..."',
    "done_when": '["..."]',
    "milestone": "M1",
    "tasks": "[...]",
}

IssueLevel = Literal["error", "warning"]


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class PlanIssue:
    """One validation error or warning, located by file, task id and field when they apply."""

    level: IssueLevel
    code: str
    message: str
    file: str | None = None
    task: str | None = None
    field: str | None = None
    hint: str | None = None

    def to_data(self) -> dict[str, Any]:
        """JSON view of the issue."""
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "file": self.file,
            "task": self.task,
            "field": self.field,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class PlanTask:
    """One task of a plan file. ``meta.milestone`` is the plan's milestone."""

    meta: TaskMetadata
    title: str
    description: str
    proposal: int | None = None


@dataclass(frozen=True)
class PlanFile:
    """A loaded ``plans/<milestone>.yml``; only the tasks that parsed cleanly are listed."""

    milestone: str
    tasks: tuple[PlanTask, ...]
    path: Path

    def find(self, task_id: str) -> PlanTask | None:
        """Return the task with ``task_id``, or ``None``."""
        return next((t for t in self.tasks if t.meta.id == task_id), None)


@dataclass(frozen=True)
class RoadmapMilestone:
    """One milestone of ``plans/roadmap.yml``."""

    id: str
    title: str
    goal: str | None = None
    demo: str | None = None
    lead: str | None = None
    specs: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        """JSON view of the milestone."""
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "demo": self.demo,
            "lead": self.lead,
            "specs": list(self.specs),
        }


@dataclass(frozen=True)
class Roadmap:
    """The parsed ``plans/roadmap.yml``."""

    milestones: tuple[RoadmapMilestone, ...]
    path: Path

    def find(self, milestone_id: str) -> RoadmapMilestone | None:
        """Return the milestone with ``milestone_id``, or ``None``."""
        return next((m for m in self.milestones if m.id == milestone_id), None)


@dataclass(frozen=True)
class PlanValidation:
    """Result of :func:`validate_plan`.

    ``plan`` is ``None`` when the plan file cannot be loaded at all. ``waves`` are the
    topological levels (empty when the graph has a cycle); ``coverage`` maps every milestone
    AC to the ids of the tasks covering it (``()`` when uncovered).
    """

    milestone: str
    plan: PlanFile | None
    errors: tuple[PlanIssue, ...]
    warnings: tuple[PlanIssue, ...]
    waves: tuple[tuple[str, ...], ...]
    coverage: Mapping[str, tuple[str, ...]]
    roadmap: Roadmap | None
    file: str = ""
    specs: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        """True when there are no errors (warnings are allowed)."""
        return not self.errors

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` payload of ``forge plan validate``."""
        roadmap_entry = None if self.roadmap is None else self.roadmap.find(self.milestone)
        return {
            "milestone": self.milestone,
            "ok": self.ok,
            "file": self.file,
            "tasks": [] if self.plan is None else [t.meta.id for t in self.plan.tasks],
            "specs": list(self.specs),
            "waves": [list(w) for w in self.waves],
            "coverage": {ac: list(ids) for ac, ids in self.coverage.items()},
            "errors": [i.to_data() for i in self.errors],
            "warnings": [i.to_data() for i in self.warnings],
            "roadmap": {
                "present": self.roadmap is not None,
                "milestone": None if roadmap_entry is None else roadmap_entry.to_data(),
            },
            "counts": {
                "tasks": 0 if self.plan is None else len(self.plan.tasks),
                "errors": len(self.errors),
                "warnings": len(self.warnings),
            },
        }


# --------------------------------------------------------------------------- helpers


def _rel(path: Path) -> str:
    return path.as_posix()


def plan_path(milestone: str) -> Path:
    """Location of the plan file of ``milestone``, relative to the repository root."""
    return PLANS_DIR / f"{milestone}.yml"


def _relative_path_problem(value: str) -> str | None:
    path = PurePosixPath(value)
    if not value.strip() or path.is_absolute() or ".." in path.parts or "\\" in value:
        return f"{value!r} is not a relative path"
    return None


class _Issues:
    """Collects errors and warnings in detection order."""

    def __init__(self) -> None:
        self.errors: list[PlanIssue] = []
        self.warnings: list[PlanIssue] = []

    def error(self, code: str, message: str, **where: str | None) -> None:
        self.errors.append(PlanIssue("error", code, message, **where))

    def warn(self, code: str, message: str, **where: str | None) -> None:
        self.warnings.append(PlanIssue("warning", code, message, **where))

    def from_config(self, code: str, exc: ConfigError, *, task: str | None = None) -> None:
        issue = PlanIssue(
            "error", code, exc.problem, file=exc.file, task=task, field=exc.key, hint=exc.hint
        )
        self.errors.append(issue)


# --------------------------------------------------------------------------- roadmap


def parse_roadmap(data: Mapping[str, Any], path: Path, file: Path | str | None = None) -> Roadmap:
    """Parse the roadmap mapping. Raises :class:`ConfigError` naming file, key and fix."""
    f = Fields(data, file if file is not None else path)
    f.reject_unknown(_ROADMAP_KEYS)
    if f.raw("milestones") is None:
        raise f.fail("milestones", "required key is missing", "add `milestones:` with a list")
    milestones: list[RoadmapMilestone] = []
    seen: set[str] = set()
    for item in f.list_of_mappings("milestones"):
        item.reject_unknown(_ROADMAP_MILESTONE_KEYS)
        mid = item.str_("id")
        if MILESTONE_RE.fullmatch(mid) is None:
            raise item.fail("id", f"invalid milestone id {mid!r}", "use the form `M<n>`, e.g. `M1`")
        if mid in seen:
            raise item.fail("id", f"duplicate milestone {mid}", "list each milestone once")
        seen.add(mid)
        title = item.str_("title")
        if not title.strip():
            raise item.fail("title", "is empty", 'give it a title: `title: "..."`')
        specs = item.str_list("specs")
        for i, spec in enumerate(specs):
            problem = _relative_path_problem(spec)
            if problem is not None:
                raise item.fail(
                    f"specs[{i}]",
                    problem,
                    "write a path relative to the repo root, e.g. `specs/m1/editor.md`",
                )
        milestones.append(
            RoadmapMilestone(
                id=mid,
                title=title,
                goal=item.str_("goal", required=False),
                demo=item.str_("demo", required=False),
                lead=item.str_("lead", required=False),
                specs=tuple(specs),
            )
        )
    return Roadmap(milestones=tuple(milestones), path=path)


def load_roadmap(root: Path) -> Roadmap | None:
    """Load ``plans/roadmap.yml`` under ``root``; ``None`` when the file does not exist.

    Raises :class:`ConfigError` when it exists but is invalid.
    """
    full = root / ROADMAP_PATH
    if not full.is_file():
        return None
    data = load_yaml_mapping(full)
    return parse_roadmap(data, full, _rel(ROADMAP_PATH))


# --------------------------------------------------------------------------- plan file


@dataclass
class _Loaded:
    """A plan file as loaded: built tasks plus ids that were declared but did not parse."""

    plan: PlanFile
    declared: set[str]


def _missing(f: Fields, key: str) -> ConfigError:
    example = _EXAMPLES.get(key, '"..."')
    if f.has(key):
        return f.fail(key, "is empty (null)", f"give it a value: `{key}: {example}`")
    return f.fail(key, "required key is missing", f"add `{key}: {example}`")


def _unknown_key(f: Fields, key: str, allowed: tuple[str, ...]) -> ConfigError:
    """The :meth:`Fields.reject_unknown` error (with its did-you-mean fix) for ``key``."""
    try:
        Fields({key: None}, f.file, f.prefix).reject_unknown(allowed)
    except ConfigError as exc:
        return exc
    raise AssertionError(f"{key} is a known key")  # pragma: no cover


def _task(
    reader: Fields, index: int, milestone: str, issues: _Issues, declared: set[str]
) -> PlanTask | None:
    """Parse one task mapping; record its problems and return the task when it is usable.

    Fields are named relative to the task when it has a string id, else with the
    ``tasks[<index>]`` prefix.
    """
    raw_id = reader.raw("id")
    task_id = raw_id if isinstance(raw_id, str) else None
    f = Fields(reader.data, reader.file, "" if task_id is not None else f"tasks[{index}]")
    where: dict[str, str | None] = {"file": f.file, "task": task_id}
    ok = True

    for key in f.data:
        if key not in TASK_KEYS:
            issues.from_config("unknown_field", _unknown_key(f, key, TASK_KEYS), task=task_id)
            ok = False
    for key in REQUIRED_TASK_KEYS:
        if f.raw(key) is None:
            issues.from_config("missing_field", _missing(f, key), task=task_id)
            ok = False

    if task_id is not None:
        if TASK_ID_RE.fullmatch(task_id) is None or task_id.partition("-")[0] != milestone:
            issues.error(
                "invalid_id",
                f"invalid task id {task_id!r} in the plan of milestone {milestone}",
                field="id",
                hint=f"use the form `{milestone}-T<n>`, e.g. `{milestone}-T01`",
                **where,
            )
            ok = False
        else:
            declared.add(task_id)
    elif raw_id is not None:
        issues.error(
            "invalid_id",
            f"expected a string id, got {type_name(raw_id)}",
            field=f.path("id"),
            hint=f"write it as `id: {milestone}-T01`",
            **where,
        )
        ok = False

    raw_size = f.raw("size")
    if raw_size is not None and raw_size not in ("S", "M"):
        too_large = isinstance(raw_size, str) and raw_size.strip().upper() not in ("S", "M")
        issues.error(
            "size_too_large" if too_large else "invalid_field",
            f"size {raw_size!r} is not allowed: tasks are S or M",
            field=f.path("size"),
            hint="split the task into S or M tasks (§9.4)" if too_large else "write `size: S`",
            **where,
        )
        ok = False

    title = f.raw("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        problem = (
            "is empty" if isinstance(title, str) else f"expected a string, got {type_name(title)}"
        )
        issues.error(
            "invalid_field", problem, field=f.path("title"), hint='write `title: "..."`', **where
        )
        ok = False
    elif isinstance(title, str) and ("\n" in title or "\r" in title):
        issues.error(
            "invalid_field",
            "the title spans several lines",
            field=f.path("title"),
            hint="write the title on one line; put details in the description",
            **where,
        )
        ok = False
    elif isinstance(title, str) and len(f"[{task_id or ''}] {title.strip()}") > MAX_ISSUE_TITLE:
        issues.error(
            "invalid_field",
            f"the issue title `[<id>] <title>` would exceed {MAX_ISSUE_TITLE} characters",
            field=f.path("title"),
            hint="shorten the title; put details in the description",
            **where,
        )
        ok = False
    proposal = f.raw("proposal")
    if proposal is not None and (
        isinstance(proposal, bool) or not isinstance(proposal, int) or proposal <= 0
    ):
        issues.error(
            "invalid_field",
            f"expected a positive issue number, got {proposal!r}",
            field=f.path("proposal"),
            hint="write `proposal: 42`, the number of the `forge propose` issue this task adopts",
            **where,
        )
        ok = False
    description = f.raw("description")
    if description is not None and not isinstance(description, str):
        issues.error(
            "invalid_field",
            f"expected a string, got {type_name(description)}",
            field=f.path("description"),
            hint="write `description: |` followed by indented text",
            **where,
        )
        ok = False
    elif isinstance(description, str) and (marker := description_text_problem(description)):
        issues.error(
            "invalid_field",
            marker,
            field=f.path("description"),
            hint="remove that line from the description",
            **where,
        )
        ok = False

    if not ok:
        return None
    meta_data = {k: v for k, v in f.data.items() if k in METADATA_KEYS}
    try:
        meta = parse_metadata_fields(Fields(meta_data, f.file, f.prefix), None)
    except ConfigError as exc:
        issues.from_config("invalid_field", exc, task=task_id)
        return None
    assert isinstance(title, str) and isinstance(description, str)
    assert proposal is None or isinstance(proposal, int)
    return PlanTask(meta=meta, title=title, description=description, proposal=proposal)


def _load_plan(root: Path, milestone: str, issues: _Issues) -> _Loaded | None:
    rel = plan_path(milestone)
    full = root / rel
    if not full.is_file():
        issues.error(
            "plan_missing",
            "plan file not found",
            file=_rel(rel),
            hint=f"write the plan with `/plan {milestone}` or create {_rel(rel)}",
        )
        return None
    try:
        data = load_yaml_mapping(full)
    except ConfigError as exc:
        issues.error("plan_invalid", exc.problem, file=_rel(rel), hint=exc.hint)
        return None
    f = Fields(data, _rel(rel))
    for key in data:
        if key not in _PLAN_KEYS:
            issues.from_config("unknown_field", _unknown_key(f, key, _PLAN_KEYS))
    for key in _PLAN_KEYS:
        if f.raw(key) is None:
            issues.from_config("missing_field", _missing(f, key))
    file_milestone = f.raw("milestone")
    if file_milestone is not None and file_milestone != milestone:
        issues.error(
            "milestone_mismatch",
            f"the file declares milestone {file_milestone!r} but it is the plan of {milestone}",
            file=_rel(rel),
            field="milestone",
            hint=f"write `milestone: {milestone}` or rename the file",
        )
    try:
        readers = f.list_of_mappings("tasks")
    except ConfigError as exc:
        issues.from_config("invalid_field", exc)
        readers = []
    tasks: list[PlanTask] = []
    declared: set[str] = set()
    seen: set[str] = set()
    for index, reader in enumerate(readers):
        raw_id = reader.raw("id")
        if isinstance(raw_id, str) and raw_id in seen:
            issues.error(
                "duplicate_id",
                f"duplicate task id {raw_id}",
                file=_rel(rel),
                task=raw_id,
                field="id",
                hint="give each task a unique id",
            )
            continue
        if isinstance(raw_id, str):
            seen.add(raw_id)
        task = _task(reader, index, milestone, issues, declared)
        if task is not None:
            tasks.append(task)
    return _Loaded(PlanFile(milestone=milestone, tasks=tuple(tasks), path=full), declared)


def _other_plan_ids(root: Path, milestone: str) -> set[str]:
    """Task ids of the other ``plans/*.yml`` files (read leniently; bad files are skipped)."""
    ids: set[str] = set()
    plans = root / PLANS_DIR
    if not plans.is_dir():
        return ids
    for path in sorted(plans.glob("*.yml")):
        if path.name in (ROADMAP_PATH.name, f"{milestone}.yml"):
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            tid = task.get("id") if isinstance(task, dict) else None
            if (
                isinstance(tid, str)
                and TASK_ID_RE.fullmatch(tid)
                and not tid.startswith(f"{milestone}-")
            ):
                ids.add(tid)
    return ids


# --------------------------------------------------------------------------- validation


def _check_specs(
    root: Path,
    milestone: str,
    plan: PlanFile,
    roadmap_specs: Iterable[str],
    issues: _Issues,
) -> tuple[dict[str, Spec], list[str]]:
    """Load the milestone specs and check them; return the loaded specs and the spec order."""
    users: dict[str, list[str]] = {}
    for task in plan.tasks:
        users.setdefault(task.meta.spec, []).append(task.meta.id)
    order = list(users)
    for listed in roadmap_specs:
        if listed not in users and listed not in order:
            order.append(listed)
    loaded: dict[str, Spec] = {}
    for path in order:
        who = (
            f"referenced by {', '.join(users[path])}"
            if path in users
            else f"listed for {milestone} in {_rel(ROADMAP_PATH)}"
        )
        try:
            spec = load_spec(root, path)
        except ConfigError as exc:
            code = "spec_missing" if exc.problem == "spec file not found" else "spec_invalid"
            issues.error(code, f"{exc.problem} ({who})", file=path, field=exc.key, hint=exc.hint)
            continue
        loaded[path] = spec
        for key in spec.unknown_keys:
            issues.warn(
                "spec_unknown_key",
                f"unknown frontmatter key {key!r}",
                file=path,
                field=key,
                hint="remove it; allowed keys: milestone, status, owner",
            )
        if spec.milestone != milestone:
            issues.error(
                "spec_milestone_mismatch",
                f"spec belongs to milestone {spec.milestone}, not {milestone} ({who})",
                file=path,
                field="milestone",
                hint=f"set `milestone: {milestone}` or reference a spec of {milestone}",
            )
        if spec.status == "draft":
            issues.error(
                "spec_draft",
                f"spec has status: draft ({who})",
                file=path,
                field="status",
                hint="finish the spec and set `status: ready`",
            )
        unresolved = spec.unresolved
        if unresolved:
            listed = "; ".join(q.text for q in unresolved[:3])
            more = f" (+{len(unresolved) - 3} more)" if len(unresolved) > 3 else ""
            issues.error(
                "spec_open_questions",
                f"{len(unresolved)} unresolved open question(s): {listed}{more}",
                file=path,
                field="Open questions",
                hint="answer each question and mark it RESOLVED, or delete it",
            )
    return loaded, order


def _check_tasks(
    plan: PlanFile,
    zones: ZonesConfig,
    specs: Mapping[str, Spec],
    file: str,
    issues: _Issues,
) -> None:
    contract = zones.contract_zones()
    proposals: dict[int, str] = {}
    for task in plan.tasks:
        meta = task.meta
        if task.proposal is not None:
            first = proposals.setdefault(task.proposal, meta.id)
            if first != meta.id:
                issues.error(
                    "duplicate_proposal",
                    f"proposal #{task.proposal} is also adopted by {first}",
                    file=file,
                    task=meta.id,
                    field="proposal",
                    hint="a proposal becomes exactly one task: remove `proposal` from one of them",
                )
        for i, zone in enumerate(meta.zones):
            if zone not in zones.zones:
                issues.error(
                    "unknown_zone",
                    f"unknown zone {zone!r}: it is not defined in {ZONES_PATH.as_posix()}",
                    file=file,
                    task=meta.id,
                    field=f"zones[{i}]",
                    hint=f"add it to {ZONES_PATH.as_posix()} or fix the task's zones",
                )
        touched = sorted(z for z in meta.zones if z in contract)
        if touched and meta.type is not TaskType.CONTRACT and not meta.allow_contract_change:
            issues.error(
                "contract_zone",
                f"task of type {meta.type.value} touches contract zone(s) {', '.join(touched)}",
                file=file,
                task=meta.id,
                field="type",
                hint="make it a `type: contract` task, or set `allow_contract_change: true`",
            )
        spec = specs.get(meta.spec)
        if spec is not None:
            defined = set(spec.acceptance_criteria)
            for i, ac in enumerate(meta.covers):
                if ac not in defined:
                    issues.error(
                        "unknown_ac",
                        f"{ac} is not defined in {meta.spec}",
                        file=file,
                        task=meta.id,
                        field=f"covers[{i}]",
                        hint=f"define {ac} in {meta.spec} or fix the id",
                    )


def _check_graph(
    plan: PlanFile,
    declared: set[str],
    external: set[str],
    file: str,
    issues: _Issues,
) -> tuple[tuple[str, ...], ...]:
    deps = {t.meta.id: t.meta.depends_on for t in plan.tasks}
    # Ids declared in the plan that failed to parse are reported already; do not cascade.
    satisfied = (declared - set(deps)) | external
    graph = TaskGraph(deps, external_done=satisfied)
    for node, unknown in graph.unknown_dependencies().items():
        task = plan.find(node)
        for dep in unknown:
            index = task.meta.depends_on.index(dep) if task is not None else None
            prefix = dep.partition("-")[0]
            other = _rel(plan_path(prefix))
            hint = (
                f"add {dep} to this plan or fix the id"
                if prefix == plan.milestone
                else f"add {dep} to {other} or fix the id"
            )
            issues.error(
                "unknown_dependency",
                f"unknown dependency {dep}",
                file=file,
                task=node,
                field=None if index is None else f"depends_on[{index}]",
                hint=hint,
            )
    if graph.has_cycle():
        for cycle in graph.cycles():
            path = " -> ".join([*cycle, cycle[0]])
            issues.error(
                "dependency_cycle",
                f"dependency cycle: {path}",
                file=file,
                task=cycle[0],
                field="depends_on",
                hint="remove one depends_on edge from the cycle",
            )
        return ()
    return tuple(tuple(w) for w in graph.waves(ignore_unknown=True))


def _check_waves(
    plan: PlanFile,
    waves: tuple[tuple[str, ...], ...],
    zones: ZonesConfig,
    team: Team | None,
    file: str,
    issues: _Issues,
) -> None:
    for number, wave in enumerate(waves, start=1):
        for i, a in enumerate(wave):
            task_a = plan.find(a)
            for b in wave[i + 1 :]:
                task_b = plan.find(b)
                if task_a is None or task_b is None:
                    continue
                why = zones_overlap(zones, task_a.meta.zones, task_b.meta.zones)
                if why is not None:
                    issues.warn(
                        "zone_overlap",
                        f"{a} and {b} are both in wave {number} and overlap: {why}",
                        file=file,
                        task=a,
                        field="zones",
                        hint=f"add a dependency between {a} and {b}, or split the zones",
                    )
    if team is None:
        return
    devs = sum(1 for m in team.members if "dev" in m.roles)
    for number, wave in enumerate(waves, start=1):
        if len(wave) < devs:
            issues.warn(
                "low_parallelism",
                f"wave {number} has {len(wave)} task(s) for {devs} active team member(s)",
                file=file,
                hint="split tasks or remove dependencies to give everyone work",
            )


def _coverage(
    plan: PlanFile, specs: Mapping[str, Spec], order: list[str], issues: _Issues
) -> dict[str, tuple[str, ...]]:
    defined_in: dict[str, str] = {}
    for path in order:
        spec = specs.get(path)
        if spec is None:
            continue
        for ac in spec.duplicate_acceptance_criteria:
            issues.error(
                "duplicate_ac",
                f"{ac} is defined more than once in {path}",
                file=path,
                field=ac,
                hint="AC ids are unique within a milestone: renumber one of them",
            )
        for ac in spec.acceptance_criteria:
            first = defined_in.get(ac)
            if first is None:
                defined_in[ac] = path
            elif first != path:
                issues.error(
                    "duplicate_ac",
                    f"{ac} is defined in both {first} and {path}",
                    file=path,
                    field=ac,
                    hint="AC ids are unique within a milestone: renumber one of them",
                )
    covering: dict[str, list[str]] = {ac: [] for ac in defined_in}
    for task in plan.tasks:
        for ac in task.meta.covers:
            if ac in covering and task.meta.id not in covering[ac]:
                covering[ac].append(task.meta.id)
    coverage = {
        ac: tuple(sorted(ids, key=natural_key))
        for ac, ids in sorted(covering.items(), key=lambda kv: natural_key(kv[0]))
    }
    for ac, ids in coverage.items():
        if not ids:
            issues.warn(
                "uncovered_ac",
                f"{ac} is not covered by any task",
                file=defined_in[ac],
                field=ac,
                hint=f"add {ac} to a task's covers, or add a task for it",
            )
    return coverage


def validate_plan(
    root: Path, milestone: str, *, zones: ZonesConfig, team: Team | None = None
) -> PlanValidation:
    """Validate ``plans/<milestone>.yml`` under ``root`` offline (spec §9.4, ADR 0013).

    Reads the plan, the other plan files (for cross-milestone dependencies), the optional
    roadmap and the milestone specs. ``zones`` is the parsed ``.forge/zones.yml``; ``team``
    (optional) enables the parallelism warning. Never raises for content problems: they are
    returned as errors and warnings.
    """
    issues = _Issues()
    file = _rel(plan_path(milestone)) if MILESTONE_RE.fullmatch(milestone) else ""

    def result(
        plan: PlanFile | None = None,
        roadmap: Roadmap | None = None,
        waves: tuple[tuple[str, ...], ...] = (),
        coverage: Mapping[str, tuple[str, ...]] | None = None,
        specs: tuple[str, ...] = (),
    ) -> PlanValidation:
        return PlanValidation(
            milestone=milestone,
            plan=plan,
            errors=tuple(issues.errors),
            warnings=tuple(issues.warnings),
            waves=waves,
            coverage=dict(coverage or {}),
            roadmap=roadmap,
            file=file,
            specs=specs,
        )

    if not file:
        issues.error(
            "invalid_milestone",
            f"invalid milestone {milestone!r}",
            hint="use the form `M<n>`, e.g. `forge plan validate M1`",
        )
        return result()

    roadmap: Roadmap | None = None
    roadmap_specs: tuple[str, ...] = ()
    try:
        roadmap = load_roadmap(root)
    except ConfigError as exc:
        issues.from_config("roadmap_invalid", exc)
    if roadmap is not None:
        entry = roadmap.find(milestone)
        if entry is None:
            issues.error(
                "roadmap_missing_milestone",
                f"milestone {milestone} is not listed in the roadmap",
                file=_rel(ROADMAP_PATH),
                field="milestones",
                hint=f"add `- id: {milestone}` with a title to {_rel(ROADMAP_PATH)}",
            )
        else:
            roadmap_specs = entry.specs

    loaded = _load_plan(root, milestone, issues)
    if loaded is None:
        return result(roadmap=roadmap)
    plan = loaded.plan
    specs, order = _check_specs(root, milestone, plan, roadmap_specs, issues)
    _check_tasks(plan, zones, specs, file, issues)
    waves = _check_graph(plan, loaded.declared, _other_plan_ids(root, milestone), file, issues)
    _check_waves(plan, waves, zones, team, file, issues)
    coverage = _coverage(plan, specs, order, issues)
    return result(plan, roadmap, waves, coverage, tuple(order))


# --------------------------------------------------------------------------- tier check


def check_plan_tier(
    models: ModelsConfig,
    agent: AgentProfile | None,
    *,
    force: bool = False,
    ci: bool = False,
) -> list[Message]:
    """Check ``models.min_tier_for.plan`` for the active agent (ADR 0013, B4).

    Without an agent (no profile) or in CI (``ci``: ``GITHUB_ACTIONS`` is set) nothing is
    checked and an info message says so. An agent that cannot serve the required tier raises
    :class:`PreconditionError` (exit 5), unless ``force``, which turns it into a warning.
    Returns the messages to show; empty when no minimum is configured or the agent serves it.
    """
    required = models.min_tier_for.get(PLAN_ROLE)
    if required is None:
        return []
    key = f"models.min_tier_for.{PLAN_ROLE}"
    if ci:
        return [Message(Level.INFO, f"{key} not checked: running in GitHub Actions")]
    if agent is None:
        return [Message(Level.INFO, f"{key} not checked: no Forge profile")]
    if can_serve(agent, required):
        return []
    if not force:
        raise PreconditionError(
            f"agent {agent.id} cannot serve tier {required}, required for planning by {key}",
            hint=f"plan with an agent that maps tier {required} or stronger (--agent <id>), "
            "or pass --force",
        )
    return [
        Message(
            Level.WARNING,
            f"agent {agent.id} cannot serve tier {required} required by {key}; "
            "continuing because of --force",
        )
    ]


__all__ = [
    "PLANS_DIR",
    "PLAN_ROLE",
    "ROADMAP_PATH",
    "TASK_KEYS",
    "PlanFile",
    "PlanIssue",
    "PlanTask",
    "PlanValidation",
    "Roadmap",
    "RoadmapMilestone",
    "check_plan_tier",
    "load_roadmap",
    "parse_roadmap",
    "plan_path",
    "validate_plan",
]
