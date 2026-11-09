"""``forge plan split`` and ``forge plan assemble``: multi-agent planning (ADR 0020).

* :func:`split_plan` checks a milestone skeleton (``plans/<M>.yml``) like
  ``forge plan validate --skeleton`` and cuts its tasks into balanced batches
  (:func:`~forge.core.batching.balanced_batches`, ``planning.max_tasks_per_agent``).
  :func:`write_plan_split` writes ``.forge/local/plan/<M>/batches.json`` and deletes the
  fragments of an earlier split.
* :func:`assemble_plan` checks the planners' fragments (``part-<k>.yml``), merges their detail
  fields into the skeleton in memory, renders the plan deterministically and runs the full
  :func:`~forge.core.plan.validate_plan` on the rendered data. :func:`write_plan_assembly`
  writes it with a temporary file and an atomic replace.

Both are offline: they read files under the repository root, and ``split`` runs one
``git check-ignore``. Neither writes anything when it reports an error.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from forge.core.batching import balanced_batches, plan_task_key
from forge.core.config import PlanningConfig
from forge.core.errors import ConfigError
from forge.core.output import Message
from forge.core.plan import (
    DETAIL_TASK_KEYS,
    SKELETON_TASK_KEYS,
    TASK_KEYS,
    PlanIssue,
    plan_path,
    validate_plan,
)
from forge.core.spec import MILESTONE_RE
from forge.core.tasks import description_text_problem
from forge.core.team import Team
from forge.core.tiers import Tier
from forge.core.validate import load_yaml_mapping, type_name
from forge.core.zones import ZonesConfig

PLANNER = "planner"
"""Name of the planner definition in ``.forge/agents/`` (rendered ``forge-planner``)."""

PLAN_LOCAL_DIR = Path(".forge/local/plan")
"""Git-ignored directory of the plan batches and fragments, one sub-directory per milestone."""

BATCHES_FILE = "batches.json"
BATCHES_VERSION = 1

FRAGMENT_KEYS: tuple[str, ...] = ("milestone", "batch", "tasks")
"""Top-level keys of a fragment, all required."""

FRAGMENT_TASK_KEYS: tuple[str, ...] = ("id", *DETAIL_TASK_KEYS)
"""Keys of a fragment task, all required: ``id`` plus the detail fields."""

FLOW_LIST_KEYS: frozenset[str] = frozenset({"covers", "depends_on", "zones", "implements", "uses"})
"""Task fields rendered in flow style (``[a, b]``) by :func:`render_plan`."""

PRECONDITION_CODES: frozenset[str] = frozenset({"plan_missing", "not_ignored", "split_missing"})
"""Error codes of ``split`` and ``assemble`` that are failed preconditions (exit 5)."""

IsIgnored = Callable[[Path], bool]
"""Says whether git would ignore a repository-relative path (``git check-ignore``)."""


# --------------------------------------------------------------------------- paths


def plan_split_dir(milestone: str) -> Path:
    """Directory of the batches and fragments of ``milestone``, relative to the repo root."""
    return PLAN_LOCAL_DIR / milestone


def plan_batches_path(milestone: str) -> Path:
    """Path of ``batches.json`` of ``milestone``, relative to the repository root."""
    return plan_split_dir(milestone) / BATCHES_FILE


def plan_part_path(milestone: str, index: int) -> Path:
    """Path of the fragment of batch ``index`` (from 1), relative to the repository root."""
    return plan_split_dir(milestone) / f"part-{index}.yml"


# --------------------------------------------------------------------------- hash


def skeleton_sha256(milestone: str, plan_data: Mapping[str, Any]) -> str:
    """SHA-256 of the skeleton fields of a plan mapping (ADR 0020).

    The hash input is the canonical JSON (sorted keys, no spaces) of
    ``{"milestone": milestone, "tasks": [skeleton fields of each task, in file order]}``.
    Only the keys of :data:`~forge.core.plan.SKELETON_TASK_KEYS` with a non-null value count,
    so comments, formatting, key order and the detail fields do not change it. An item of
    ``tasks`` that is not a mapping is hashed as it is.
    """
    raw_tasks = plan_data.get("tasks")
    tasks: list[Any] = []
    if isinstance(raw_tasks, list):
        for item in raw_tasks:
            if isinstance(item, Mapping):
                tasks.append({k: item[k] for k in SKELETON_TASK_KEYS if item.get(k) is not None})
            else:
                tasks.append(item)
    payload = {"milestone": milestone, "tasks": tasks}
    text = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class PlanBatch:
    """One group of tasks for one ``forge-planner``. ``index`` starts at 1; ``part_path`` is
    repository-relative (posix)."""

    index: int
    task_ids: tuple[str, ...]
    part_path: str

    def to_data(self) -> dict[str, Any]:
        """JSON view: ``{"index", "task_ids", "part_path"}``."""
        return {"index": self.index, "task_ids": list(self.task_ids), "part_path": self.part_path}


def _issue(code: str, message: str, **where: str | None) -> PlanIssue:
    return PlanIssue("error", code, message, **where)


def _invalid_milestone(milestone: str, command: str) -> PlanIssue:
    return _issue(
        "invalid_milestone",
        f"invalid milestone {milestone!r}",
        hint=f"use the form `M<n>`, e.g. `forge plan {command} M1`",
    )


@dataclass(frozen=True)
class PlanSplit:
    """Result of :func:`split_plan`.

    ``errors`` block the split: a failed precondition (:data:`PRECONDITION_CODES`), a skeleton
    error of ``forge plan validate --skeleton``, ``plan_empty`` or ``invalid_milestone``. With
    errors, ``batches`` is empty and nothing is written. ``removed_parts`` is filled by
    :func:`write_plan_split`.
    """

    milestone: str
    file: str
    max_per_agent: int
    batches: tuple[PlanBatch, ...] = ()
    skeleton_sha256: str = ""
    errors: tuple[PlanIssue, ...] = ()
    warnings: tuple[PlanIssue, ...] = ()
    messages: tuple[Message, ...] = ()
    removed_parts: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when there are no blocking errors."""
        return not self.errors

    @property
    def precondition_failed(self) -> bool:
        """True when an error is a failed precondition (exit 5)."""
        return any(e.code in PRECONDITION_CODES for e in self.errors)

    @property
    def agents(self) -> int:
        """Number of planner sessions (the number of batches)."""
        return len(self.batches)

    @property
    def batches_path(self) -> str:
        """Repository-relative path of ``batches.json`` (empty for an invalid milestone)."""
        if MILESTONE_RE.fullmatch(self.milestone) is None:
            return ""
        return plan_batches_path(self.milestone).as_posix()

    def batches_document(self) -> dict[str, Any]:
        """The content of ``batches.json``."""
        return {
            "version": BATCHES_VERSION,
            "milestone": self.milestone,
            "skeleton_sha256": self.skeleton_sha256,
            "max_per_agent": self.max_per_agent,
            "agents": self.agents,
            "batches": [b.to_data() for b in self.batches],
        }

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` of ``forge plan split`` (without the ``subagent`` block)."""
        return {
            "milestone": self.milestone,
            "file": self.file,
            "batches_path": self.batches_path,
            "agents": self.agents,
            "max_per_agent": self.max_per_agent,
            "batches": [b.to_data() for b in self.batches],
            "removed_parts": list(self.removed_parts),
            "errors": [i.to_data() for i in self.errors],
            "warnings": [i.to_data() for i in self.warnings],
        }


# --------------------------------------------------------------------------- split


def split_plan(
    repo_root: Path,
    milestone: str,
    *,
    zones: ZonesConfig,
    planning: PlanningConfig | None = None,
    team: Team | None = None,
    is_ignored: IsIgnored,
) -> PlanSplit:
    """Check the skeleton of ``plans/<milestone>.yml`` and group its tasks (ADR 0020).

    In order: an invalid milestone (``invalid_milestone``); no plan file (``plan_missing``,
    a precondition); ``.forge/local/plan/<M>/`` not ignored by git per ``is_ignored``
    (``not_ignored``, a precondition); the content checks of
    ``validate_plan(..., skeleton=True)`` (any error refuses, warnings are kept); no tasks
    (``plan_empty``). Then the tasks are cut with
    :func:`~forge.core.batching.balanced_batches` (``planning.max_tasks_per_agent``, key
    :func:`~forge.core.batching.plan_task_key`). Reads files only; :func:`write_plan_split`
    writes.
    """
    planning = planning if planning is not None else PlanningConfig()
    max_per_agent = planning.max_tasks_per_agent
    valid = MILESTONE_RE.fullmatch(milestone) is not None
    file = plan_path(milestone).as_posix() if valid else ""
    base = PlanSplit(milestone=milestone, file=file, max_per_agent=max_per_agent)
    if not valid:
        return replace(base, errors=(_invalid_milestone(milestone, "split"),))
    if not (repo_root / file).is_file():
        return replace(
            base,
            errors=(
                _issue(
                    "plan_missing",
                    "plan file not found",
                    file=file,
                    hint=f"write the skeleton first (`/forge-plan {milestone}`)",
                ),
            ),
        )
    local = plan_split_dir(milestone)
    if not is_ignored(local / BATCHES_FILE):
        return replace(
            base,
            errors=(
                _issue(
                    "not_ignored",
                    f"git would not ignore {local.as_posix()}/",
                    file=".gitignore",
                    hint="add `.forge/local/` to `.gitignore`",
                ),
            ),
        )
    validation = validate_plan(
        repo_root, milestone, zones=zones, team=team, planning=planning, skeleton=True
    )
    base = replace(base, warnings=validation.warnings, messages=validation.messages)
    if validation.errors:
        return replace(base, errors=validation.errors)
    plan = validation.plan
    assert plan is not None  # no error, so the plan loaded
    if not plan.tasks:
        return replace(
            base,
            errors=(
                _issue(
                    "plan_empty",
                    "the plan has no tasks",
                    file=file,
                    field="tasks",
                    hint="write the skeleton's tasks, then run `forge plan split` again",
                ),
            ),
        )
    raw = load_yaml_mapping(repo_root / file)
    groups = balanced_batches(plan.tasks, max_per_agent, plan_task_key(plan))
    batches = tuple(
        PlanBatch(
            index=index,
            task_ids=tuple(t.meta.id for t in group),
            part_path=plan_part_path(milestone, index).as_posix(),
        )
        for index, group in enumerate(groups, start=1)
    )
    return replace(base, batches=batches, skeleton_sha256=skeleton_sha256(milestone, raw))


def _atomic_write(path: Path, content: bytes) -> None:
    """Write ``content`` to a temporary file next to ``path``, then ``os.replace`` it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def render_batches(split: PlanSplit) -> str:
    """The bytes of ``batches.json`` as text: sorted keys, two-space indent, final newline."""
    return json.dumps(split.batches_document(), sort_keys=True, indent=2) + "\n"


def write_plan_split(repo_root: Path, split: PlanSplit) -> PlanSplit:
    """Write ``batches.json`` of ``split`` and delete the stale fragments.

    Order (safe to interrupt and re-run): remove the old ``batches.json``, delete every
    ``part-*.yml`` of the directory, then write the new ``batches.json`` atomically. An
    interrupted run leaves no ``batches.json`` or no fragments, which ``assemble`` refuses.
    Returns ``split`` with ``removed_parts`` (repository-relative, sorted). Raises
    :class:`ValueError` when ``split`` has errors.
    """
    if not split.ok:
        raise ValueError("cannot write a plan split with errors")
    rel = plan_split_dir(split.milestone)
    directory = repo_root / rel
    directory.mkdir(parents=True, exist_ok=True)
    (directory / BATCHES_FILE).unlink(missing_ok=True)
    removed: list[str] = []
    for path in sorted(directory.glob("part-*.yml")):
        if path.is_file():
            path.unlink()
            removed.append((rel / path.name).as_posix())
    _atomic_write(directory / BATCHES_FILE, render_batches(split).encode("utf-8"))
    return replace(split, removed_parts=tuple(removed))


# --------------------------------------------------------------------------- rendering


class _Flow(list[Any]):
    """A list rendered in flow style (``[a, b]``)."""


class _PlanDumper(yaml.SafeDumper):
    """Safe dumper that indents block sequences under their key and never emits aliases."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)

    def ignore_aliases(self, data: Any) -> bool:
        """Always True: a value shared in memory (a YAML alias in the input) is written out."""
        return True


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


def _represent_flow(dumper: yaml.SafeDumper, value: _Flow) -> yaml.SequenceNode:
    return dumper.represent_sequence("tag:yaml.org,2002:seq", list(value), flow_style=True)


_PlanDumper.add_representer(str, _represent_str)
_PlanDumper.add_representer(_Flow, _represent_flow)


def _ordered_task(item: Mapping[Any, Any]) -> dict[Any, Any]:
    """A task mapping with keys in :data:`TASK_KEYS` order, then unknown keys; nulls dropped."""
    out: dict[Any, Any] = {}
    for key in TASK_KEYS:
        value = item.get(key)
        if value is None:
            continue
        out[key] = _Flow(value) if key in FLOW_LIST_KEYS and isinstance(value, list) else value
    for key in sorted((k for k in item if k not in TASK_KEYS), key=str):
        if item[key] is not None:
            out[key] = item[key]
    return out


def normalize_plan(plan_data: Mapping[Any, Any]) -> dict[Any, Any]:
    """The plan mapping in the fixed order of :func:`render_plan`.

    Top-level ``milestone``, then ``tasks``, then any other key (sorted); task keys as in
    :func:`_ordered_task`. Keys whose value is null are dropped. Values are not checked.
    """
    out: dict[Any, Any] = {}
    for key in ("milestone", "tasks"):
        value = plan_data.get(key)
        if value is None:
            continue
        if key == "tasks" and isinstance(value, list):
            value = [_ordered_task(t) if isinstance(t, Mapping) else t for t in value]
        out[key] = value
    for key in sorted((k for k in plan_data if k not in ("milestone", "tasks")), key=str):
        if plan_data[key] is not None:
            out[key] = plan_data[key]
    return out


def render_plan(plan_data: Mapping[Any, Any]) -> str:
    """Render a plan mapping as deterministic YAML (ADR 0020).

    Keys in the order of :func:`normalize_plan`; multi-line strings as ``|`` blocks;
    ``done_when`` as a block list; ``covers``, ``depends_on``, ``zones``, ``implements`` and
    ``uses`` in flow style; no line folding. ``yaml.safe_load`` of the result gives
    :func:`normalize_plan` of the input back.
    """
    text: str = yaml.dump(
        normalize_plan(plan_data),
        Dumper=_PlanDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=1_000_000,
        indent=2,
    )
    return text


# --------------------------------------------------------------------------- assemble


@dataclass(frozen=True)
class PlanAssembly:
    """Result of :func:`assemble_plan`.

    ``errors`` block the write: a failed precondition (:data:`PRECONDITION_CODES`),
    ``invalid_milestone``, ``plan_invalid``, ``skeleton_changed``, a fragment error or an error
    of the full validation of the merged plan. Without errors, ``content`` is the rendered plan
    and ``changed`` says whether it differs from the bytes on disk. ``written`` is set by
    :func:`write_plan_assembly`.
    """

    milestone: str
    file: str
    batches: tuple[PlanBatch, ...] = ()
    tasks: tuple[str, ...] = ()
    errors: tuple[PlanIssue, ...] = ()
    warnings: tuple[PlanIssue, ...] = ()
    messages: tuple[Message, ...] = ()
    content: str | None = None
    changed: bool = False
    written: bool = False

    @property
    def ok(self) -> bool:
        """True when there are no errors."""
        return not self.errors

    @property
    def precondition_failed(self) -> bool:
        """True when an error is a failed precondition (exit 5)."""
        return any(e.code in PRECONDITION_CODES for e in self.errors)

    def to_data(self) -> dict[str, Any]:
        """JSON ``data`` of ``forge plan assemble``."""
        return {
            "milestone": self.milestone,
            "file": self.file,
            "written": self.written,
            "changed": self.changed,
            "batches": [b.to_data() for b in self.batches],
            "tasks": list(self.tasks),
            "errors": [i.to_data() for i in self.errors],
            "warnings": [i.to_data() for i in self.warnings],
            "counts": {
                "batches": len(self.batches),
                "tasks": len(self.tasks),
                "errors": len(self.errors),
                "warnings": len(self.warnings),
            },
        }


@dataclass(frozen=True)
class _BatchesFile:
    sha256: str
    batches: tuple[PlanBatch, ...]


def _read_batches(repo_root: Path, milestone: str) -> _BatchesFile | PlanIssue:
    """Read and check ``batches.json``; a :class:`PlanIssue` (``split_missing``) on failure."""
    rel = plan_batches_path(milestone).as_posix()
    hint = f"run `forge plan split {milestone}` first"
    full = repo_root / rel
    if not full.is_file():
        return _issue("split_missing", f"{rel} not found", file=rel, hint=hint)
    try:
        doc = json.loads(full.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return _issue("split_missing", f"{rel} cannot be read: {exc}", file=rel, hint=hint)

    def bad(problem: str) -> PlanIssue:
        return _issue(
            "split_missing",
            f"{rel} is invalid: {problem}",
            file=rel,
            hint=f"run `forge plan split {milestone}` again; do not edit batches.json",
        )

    if not isinstance(doc, dict):
        return bad("not a JSON object")
    if doc.get("version") != BATCHES_VERSION:
        return bad(f"unsupported version {doc.get('version')!r}")
    if doc.get("milestone") != milestone:
        return bad(f"it is the split of milestone {doc.get('milestone')!r}")
    sha = doc.get("skeleton_sha256")
    if not isinstance(sha, str) or not sha:
        return bad("skeleton_sha256 is missing")
    raw_batches = doc.get("batches")
    if not isinstance(raw_batches, list) or not raw_batches:
        return bad("batches is missing or empty")
    batches: list[PlanBatch] = []
    seen: set[str] = set()
    for position, item in enumerate(raw_batches, start=1):
        if not isinstance(item, dict):
            return bad(f"batch {position} is not an object")
        index = item.get("index")
        ids = item.get("task_ids")
        expected = plan_part_path(milestone, position).as_posix()
        if index != position or isinstance(index, bool):
            return bad(f"batch {position} has index {index!r}")
        if (
            not isinstance(ids, list)
            or not ids
            or not all(isinstance(i, str) for i in ids)
            or seen.intersection(ids)
            or len(set(ids)) != len(ids)
        ):
            return bad(f"batch {position} has invalid task_ids")
        if item.get("part_path") != expected:
            return bad(f"batch {position} has part_path {item.get('part_path')!r}")
        seen.update(ids)
        batches.append(PlanBatch(index=position, task_ids=tuple(ids), part_path=expected))
    return _BatchesFile(sha256=sha, batches=tuple(batches))


def _check_detail(
    task: Mapping[Any, Any], where: dict[str, str | None], errors: list[PlanIssue]
) -> None:
    """The plan rules of the detail fields of one fragment task (missing_field, invalid_field)."""

    def err(code: str, message: str, field: str, hint: str) -> None:
        errors.append(_issue(code, message, field=field, hint=hint, **where))

    for key in DETAIL_TASK_KEYS:
        if task.get(key) is None:
            problem = "is empty (null)" if key in task else "required key is missing"
            err("missing_field", problem, key, f"add `{key}` for this task")
    tier = task.get("tier")
    allowed = [t.value for t in Tier]
    if tier is not None and tier not in allowed:
        err(
            "invalid_field",
            f"invalid tier {tier!r}; expected one of: {', '.join(allowed)}",
            "tier",
            "write `tier: standard` (or strong, light)",
        )
    done_when = task.get("done_when")
    if done_when is not None:
        if not isinstance(done_when, list) or not done_when:
            problem = (
                "must be a non-empty list"
                if isinstance(done_when, list)
                else f"expected a list of strings, got {type_name(done_when)}"
            )
            err("invalid_field", problem, "done_when", "write a list of testable behaviours")
        else:
            for i, item in enumerate(done_when):
                if not isinstance(item, str):
                    err(
                        "invalid_field",
                        f"expected a string, got {type_name(item)}",
                        f"done_when[{i}]",
                        "quote the item",
                    )
                elif not item.strip():
                    err(
                        "invalid_field",
                        "empty item",
                        f"done_when[{i}]",
                        "describe a verifiable condition",
                    )
    description = task.get("description")
    if description is not None:
        if not isinstance(description, str):
            err(
                "invalid_field",
                f"expected a string, got {type_name(description)}",
                "description",
                "write `description: |` followed by indented text",
            )
        elif (marker := description_text_problem(description)) is not None:
            err("invalid_field", marker, "description", "remove that line from the description")


def _read_fragment(
    repo_root: Path, milestone: str, batch: PlanBatch, errors: list[PlanIssue]
) -> dict[str, dict[Any, Any]]:
    """Check the fragment of ``batch``; return its detail fields by task id (when usable)."""
    rel = batch.part_path
    full = repo_root / rel

    def err(code: str, message: str, hint: str, **where: str | None) -> None:
        errors.append(_issue(code, message, file=rel, hint=hint, **where))

    if not full.is_file():
        err(
            "fragment_missing",
            f"the fragment of batch {batch.index} does not exist",
            f"write it (tasks {', '.join(batch.task_ids)}), then run assemble again",
        )
        return {}
    try:
        data = load_yaml_mapping(full)
    except ConfigError as exc:
        err("fragment_invalid", exc.problem, exc.hint or "fix the YAML of the fragment")
        return {}
    ok = True
    for key in data:
        if key not in FRAGMENT_KEYS:
            err(
                "fragment_invalid",
                f"unknown top-level key {key!r}",
                f"keep only {', '.join(FRAGMENT_KEYS)}",
                field=str(key),
            )
            ok = False
    for key in FRAGMENT_KEYS:
        if data.get(key) is None:
            err(
                "fragment_invalid",
                "required top-level key is missing",
                f"add `{key}:`",
                field=key,
            )
            ok = False
    if data.get("milestone") is not None and data["milestone"] != milestone:
        err(
            "fragment_mismatch",
            f"the fragment declares milestone {data['milestone']!r}, expected {milestone}",
            f"write `milestone: {milestone}`, or save the fragment under its own batch's path",
            field="milestone",
        )
        ok = False
    raw_batch = data.get("batch")
    if raw_batch is not None and (raw_batch != batch.index or isinstance(raw_batch, bool)):
        err(
            "fragment_mismatch",
            f"the fragment declares batch {raw_batch!r}, expected {batch.index}",
            f"write `batch: {batch.index}`, or save the fragment under its own batch's path",
            field="batch",
        )
        ok = False
    tasks = data.get("tasks")
    if tasks is not None and not isinstance(tasks, list):
        err(
            "fragment_invalid",
            f"expected a list of tasks, got {type_name(tasks)}",
            "write `tasks:` followed by `- id: ...` items",
            field="tasks",
        )
        return {}
    if not ok or tasks is None:
        return {}

    assigned = set(batch.task_ids)
    found: dict[str, dict[Any, Any]] = {}
    for position, item in enumerate(tasks):
        if not isinstance(item, dict):
            err(
                "fragment_invalid",
                f"expected a task mapping, got {type_name(item)}",
                "write each task as `- id: ...` with its detail fields",
                field=f"tasks[{position}]",
            )
            continue
        task_id = item.get("id")
        if not isinstance(task_id, str):
            err(
                "fragment_invalid",
                "the task has no string id" if task_id is None else f"invalid id {task_id!r}",
                "give each task its `id` from the batch",
                field=f"tasks[{position}].id",
            )
            continue
        where: dict[str, str | None] = {"file": rel, "task": task_id}
        if task_id in found:
            errors.append(
                _issue(
                    "duplicate_id",
                    f"task {task_id} appears more than once in the fragment",
                    field="id",
                    hint="list each task of the batch once",
                    **where,
                )
            )
            continue
        if task_id not in assigned:
            errors.append(
                _issue(
                    "unassigned_task",
                    f"task {task_id} is not assigned to batch {batch.index}",
                    field="id",
                    hint=f"remove it; batch {batch.index} has {', '.join(batch.task_ids)}",
                    **where,
                )
            )
            found[task_id] = {}
            continue
        clean = True
        for key in item:
            if key == "id":
                continue
            if key in SKELETON_TASK_KEYS:
                errors.append(
                    _issue(
                        "skeleton_field",
                        f"{key} is a skeleton field: the fragment may not set it",
                        field=str(key),
                        hint=f"remove `{key}`; change the skeleton in {plan_path(milestone)}",
                        **where,
                    )
                )
                clean = False
            elif key not in DETAIL_TASK_KEYS:
                errors.append(
                    _issue(
                        "unknown_field",
                        f"unknown key {key!r}",
                        field=str(key),
                        hint=f"keep only {', '.join(FRAGMENT_TASK_KEYS)}",
                        **where,
                    )
                )
                clean = False
        before = len(errors)
        _check_detail(item, where, errors)
        clean = clean and len(errors) == before
        found[task_id] = {k: item[k] for k in DETAIL_TASK_KEYS} if clean else {}
    for task_id in batch.task_ids:
        if task_id not in found:
            errors.append(
                _issue(
                    "missing_task",
                    f"task {task_id} of batch {batch.index} is absent from the fragment",
                    field="tasks",
                    hint=f"add `- id: {task_id}` with description, done_when and tier",
                    file=rel,
                    task=task_id,
                )
            )
    return {k: v for k, v in found.items() if v and k in assigned}


def _merge(plan_data: Mapping[Any, Any], detail: Mapping[str, Mapping[Any, Any]]) -> dict[Any, Any]:
    """The plan with each task's detail fields replaced by the fragment's (in memory)."""
    merged = dict(plan_data)
    tasks = plan_data.get("tasks")
    if isinstance(tasks, list):
        out: list[Any] = []
        for item in tasks:
            if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                fields = detail.get(item["id"])
                if fields:
                    item = {**item, **fields}
            out.append(item)
        merged["tasks"] = out
    return merged


def assemble_plan(
    repo_root: Path,
    milestone: str,
    *,
    zones: ZonesConfig,
    planning: PlanningConfig | None = None,
    team: Team | None = None,
) -> PlanAssembly:
    """Check the fragments of ``milestone``, merge them into the skeleton and validate (ADR 0020).

    In order: ``invalid_milestone``; no plan (``plan_missing``) or no valid ``batches.json``
    (``split_missing``), both preconditions; a plan that does not parse (``plan_invalid``);
    the skeleton hash differs from the split's (``skeleton_changed``); every fragment error
    (all fragments are checked); then the merged plan is rendered with :func:`render_plan`,
    parsed back and checked with the full :func:`~forge.core.plan.validate_plan` (its errors
    as they are). Reads files only and never writes: :func:`write_plan_assembly` does.
    """
    valid = MILESTONE_RE.fullmatch(milestone) is not None
    file = plan_path(milestone).as_posix() if valid else ""
    base = PlanAssembly(milestone=milestone, file=file)
    if not valid:
        return replace(base, errors=(_invalid_milestone(milestone, "assemble"),))
    full = repo_root / file
    if not full.is_file():
        return replace(
            base,
            errors=(
                _issue(
                    "plan_missing",
                    "plan file not found",
                    file=file,
                    hint=f"write the skeleton first (`/forge-plan {milestone}`)",
                ),
            ),
        )
    read = _read_batches(repo_root, milestone)
    if isinstance(read, PlanIssue):
        return replace(base, errors=(read,))
    base = replace(base, batches=read.batches)
    try:
        current = full.read_bytes()
        plan_data = load_yaml_mapping(full)
    except (OSError, ConfigError) as exc:
        problem = exc.problem if isinstance(exc, ConfigError) else str(exc)
        hint = exc.hint if isinstance(exc, ConfigError) else "check the file"
        return replace(base, errors=(_issue("plan_invalid", problem, file=file, hint=hint),))
    if skeleton_sha256(milestone, plan_data) != read.sha256:
        return replace(
            base,
            errors=(
                _issue(
                    "skeleton_changed",
                    "the skeleton changed after `forge plan split`",
                    file=file,
                    hint=f"run `forge plan split {milestone}` again and redo the detail",
                ),
            ),
        )

    errors: list[PlanIssue] = []
    detail: dict[str, dict[Any, Any]] = {}
    for batch in read.batches:
        detail.update(_read_fragment(repo_root, milestone, batch, errors))
    if errors:
        return replace(base, errors=tuple(errors))

    content = render_plan(_merge(plan_data, detail))
    rendered = yaml.safe_load(content)
    validation = validate_plan(
        repo_root, milestone, zones=zones, team=team, planning=planning, plan_data=rendered
    )
    task_ids = () if validation.plan is None else tuple(t.meta.id for t in validation.plan.tasks)
    result = replace(
        base,
        tasks=task_ids,
        warnings=validation.warnings,
        messages=validation.messages,
    )
    if validation.errors:
        return replace(result, errors=validation.errors)
    return replace(result, content=content, changed=content.encode("utf-8") != current)


def write_plan_assembly(repo_root: Path, assembly: PlanAssembly) -> PlanAssembly:
    """Write the assembled plan to ``plans/<M>.yml`` (temporary file + ``os.replace``).

    Returns ``assembly`` with ``written`` set. Raises :class:`ValueError` when ``assembly``
    has errors. Re-running on the same inputs writes the same bytes.
    """
    if not assembly.ok or assembly.content is None:
        raise ValueError("cannot write a plan assembly with errors")
    _atomic_write(repo_root / assembly.file, assembly.content.encode("utf-8"))
    return replace(assembly, written=True)


__all__ = [
    "BATCHES_FILE",
    "BATCHES_VERSION",
    "FLOW_LIST_KEYS",
    "FRAGMENT_KEYS",
    "FRAGMENT_TASK_KEYS",
    "PLANNER",
    "PLAN_LOCAL_DIR",
    "PRECONDITION_CODES",
    "IsIgnored",
    "PlanAssembly",
    "PlanBatch",
    "PlanSplit",
    "assemble_plan",
    "normalize_plan",
    "plan_batches_path",
    "plan_part_path",
    "plan_split_dir",
    "render_batches",
    "render_plan",
    "skeleton_sha256",
    "split_plan",
    "write_plan_assembly",
    "write_plan_split",
]
