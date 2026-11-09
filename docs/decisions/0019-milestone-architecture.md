# 0019 — Milestone architecture: `docs/architecture/`, `/forge-architect` and `forge arch validate`

- Status: accepted
- Date: 2026-11-09

## Context

Today a milestone goes from specs (§9.1) straight to a task plan (§9.3). The `/plan <milestone>` prompt (§9.6) decides the components and the contracts while it writes tasks. Nobody reviews that design on its own, and nothing records it after the plan is applied. Two milestones can then shape the same area in different ways, and a contract task can appear without a clear owner or consumer.

The user decided:
- every milestone has an architecture in a defined place, reviewed through its own PR, and the task plan follows it;
- the architecture is written by a separate skill, `/forge-architect <M>` (`$forge-architect` in Codex), whose PR is merged before `/forge-plan <M>`;
- the architecture is mandatory, but a project can make it optional: `planning.architecture: required | optional`;
- `docs/architecture/overview.md` maps the whole system, and `/forge-architect` updates it in the same PR;
- the roadmap (`/forge-plan roadmap`) does not change.

Multi-agent planning (batching tasks and components per agent) is phase 14 and ADR 0020. Only its config keys appear here.

This ADR fixes the formats, the checks, the CLI command, the plan schema changes and the prompt changes, and resolves the open points against the current code (`core/plan.py`, `core/spec.py`, `core/config.py`, `core/context.py`, `core/prompts.py`).

## Decision

### Where the architecture lives (project repository)

| Path | Content |
|---|---|
| `docs/architecture/overview.md` | The system as it is today: the result of every milestone included so far. |
| `docs/architecture/<M>.md` | What milestone `<M>` adds or changes. |
| `docs/decisions/NNNN-*.md` | Durable decisions taken while designing. The `## Decisions` section of `<M>.md` links them. |

These paths are in the **project** repository created by `forge init`. The framework repository's own `docs/architecture.md` describes the Forge CLI. They are different repositories, so there is no clash.

Templates: `src/forge/templates/project/docs/architecture/_template.md` and an initial `overview.md` (empty lists). `forge init` writes both. The CLI never reads `_template.md`.

### Format of `docs/architecture/<M>.md`

YAML frontmatter holds the structure, which the CLI checks. The body explains it.

```yaml
---
milestone: M1
status: draft            # draft | ready
specs: [specs/m1/auth.md]
components:
  - id: auth-service     # [a-z][a-z0-9-]*
    zones: [app/auth]    # zones defined in .forge/zones.yml
    summary: Issues and verifies session tokens
interfaces:
  - id: IF-auth-api      # IF-[a-z0-9-]+
    zone: contracts/api  # a zone with contract: true
    producer: auth-service
    consumers: [web-app]
    summary: REST endpoints for login and token refresh
---
```

- **Keys**: `milestone`, `status`, `specs`, `components` and `interfaces` are all required. `components` and `interfaces` may be `[]`. `specs` must list at least one spec. Component keys: `id`, `zones` (may be `[]`), `summary`. Interface keys: `id`, `zone`, `producer`, `consumers` (may be `[]`), `summary`. Unknown keys are errors at every level.
- **Spec paths**: the user's brief wrote `specs/M1-auth.md`. Spec §9.1 fixes `specs/<milestone>/<feature>.md`, and the §9.2/§9.3 examples use a lower-case directory (`specs/m1/editor.md`). This ADR follows the spec. The CLI does not check the path layout. It checks that the file exists and that its frontmatter `milestone` matches.
- **Body**: these level-2 sections are mandatory: `## Context`, `## Components`, `## Interfaces`, `## Data model`, `## Decisions`, `## Risks`, `## Open questions`. Titles are matched case-insensitively, outside fenced code.
- **Open questions** use the spec rule (ADR 0013, `core/spec.open_questions`): an item is resolved when it contains `RESOLVED`. A parenthesized placeholder is never resolved. A `ready` file must have no unresolved item.

### Format of `docs/architecture/overview.md`

```yaml
---
milestones: [M1, M2]   # milestones whose architecture is already included
components: [...]      # same schema as in <M>.md, for the whole system
interfaces: [...]      # same schema as in <M>.md, for the whole system
---
```

The CLI checks the frontmatter only. The body is free text; the template suggests `## Context`, `## Components`, `## Interfaces`, `## Data model` and `## Decisions`.

### Meaning of "the milestone's interfaces" and "earlier interfaces"

- **Milestone interfaces**: the interfaces declared in `<M>.md`. They are new or changed by `<M>`, so each needs exactly one contract task in `plans/<M>.yml`.
- **Existing interfaces**: declared in `overview.md` but not in `<M>.md`. The plan may use them, but it does not implement them.
- **Components**: a name resolves against `<M>.md` first, then `overview.md`.

`overview.md` does not record which milestone introduced an item. "Existing" therefore means "already in the system map", not "from a lower milestone number". See point 4 below.

### Core: `src/forge/core/architecture.py`

The module is mypy-strict and does not import typer or rich. It reuses `spec.split_frontmatter`, `spec.open_questions`, `spec.load_spec`, `validate.Fields` and the `ZonesConfig` API.

- `load_architecture(repo_root, milestone) -> Architecture` raises `ConfigError` (file, key, fix) when the file is missing or unreadable, or when its frontmatter cannot be read as the schema above. A key of the wrong type is one example.
- `load_overview(repo_root) -> Overview | None` returns `None` when the file is missing, and raises `ConfigError` like `load_architecture`.
- `validate_architecture(repo_root, milestone, *, zones) -> ArchitectureValidation` never raises for content problems. It collects every error and warning so that one run shows all of them, as `validate_plan` does. Each item reuses the `PlanIssue` shape (ADR 0013): `level`, `code`, `message`, `file`, `task` (always `null` here), `field`, `hint`. A `ConfigError` from a loader becomes one issue carrying its file, key and fix.

### `forge arch validate <M> [--force] [--json]`

- **Command**: a new `arch` group in `commands/arch.py`. The command is thin.
- **Offline**: it reads files only. No GitHub, no git.
- **Mode**: it ignores `planning.architecture`. Validating a missing file is always an error.
- **Tier**: it checks `models.min_tier_for.architecture` with the logic of `check_plan_tier` (ADR 0013, B4). That function becomes a generic `check_min_tier(models, agent, role, *, force, ci)`, and `check_plan_tier` stays as a thin wrapper. Without a profile, or in GitHub Actions, there is no check, only an info message. A refusal is a `PreconditionError` (exit 5) unless `--force`, which turns it into a warning. `--force` exists for parity with `plan validate`.
- **Version**: a CLI outside `forge_version` gives a warning, as for `plan validate`.
- **Exit codes**: `0` no errors (warnings allowed); `1` errors in the files, or an invalid `zones.yml`; `5` a precondition failed (no `.forge/config.yml`, or the tier refusal). Exits 2, 3 and 4 are never used.
- **`data`**: `milestone`, `file`, `ok`, `status`, `specs`, `components` (ids), `interfaces` (ids), `overview` (`{path, present, listed}`), `errors`, `warnings`, `counts`.
- **`messages`** (ADR 0004 shape `{level, text, hint}`): the tier and version messages, plus one summary error `architecture <M> has N error(s)` when there are errors. Per-item codes live in `data.errors` and `data.warnings`, as in `plan validate`.

Errors (exit 1). "File" is `<M>.md` unless the row says otherwise:

| Code | Condition | Hint |
|---|---|---|
| `invalid_milestone` | the argument is not `M<n>` | use the form `M1` |
| `architecture_missing` | `docs/architecture/<M>.md` not found | run `/forge-architect <M>` or copy `_template.md` |
| `frontmatter_invalid` | frontmatter missing, unclosed, invalid YAML, or not a mapping | the `spec.split_frontmatter` fixes |
| `unknown_field` | unknown key at the top level, in a component or in an interface | remove it; lists the allowed keys |
| `missing_field` | a required key is missing or null | add `<key>: <example>` |
| `invalid_field` | wrong type, `status` not `draft`/`ready`, or an empty `specs` | the expected form |
| `milestone_mismatch` | frontmatter `milestone` differs from the file name | write `milestone: <M>` or rename the file |
| `spec_missing` / `spec_invalid` | a listed spec does not exist, or its frontmatter is invalid | fix the path or the spec |
| `spec_milestone_mismatch` | a listed spec's frontmatter `milestone` is not `<M>` (the path is not checked, as in ADR 0013) | list only specs of `<M>` |
| `invalid_id` | component id not `[a-z][a-z0-9-]*`, or interface id not `IF-[a-z0-9-]+` | the id grammar |
| `duplicate_id` | a component id or an interface id repeated in the same file | give each one a unique id |
| `unknown_zone` | a component zone or an interface zone is not in `.forge/zones.yml` | add the zone or fix the name |
| `interface_zone_not_contract` | an interface's zone has no `contract: true` | mark the zone as a contract or choose a contract zone |
| `unknown_component` | `producer` or a `consumers` item is in neither `<M>.md` nor `overview.md` | declare the component |
| `missing_section` | a mandatory body section is missing | add `## <Section>` |
| `open_questions` | `status: ready` with unresolved open questions | answer each question and mark it RESOLVED, or delete it |
| `overview_invalid` | file `overview.md`: present but its frontmatter is invalid, or breaks the rules above (keys, ids, duplicates, zones, contract zones, producer/consumers among its own components, `milestones` items not `M<n>`). Each problem keeps its own code; `overview_invalid` is used only when the frontmatter cannot be read at all. | fix `overview.md` |

Warnings (exit 0):

| Code | Condition | Hint |
|---|---|---|
| `overview_stale` | `overview.md` is missing; or it does not list `<M>` in `milestones`; or it lists `<M>` but lacks a component or interface id that `<M>.md` declares | update `overview.md` in the same PR |
| `component_unused` | a component of `<M>.md` has no zones and is neither producer nor consumer of any interface (in `<M>.md` or `overview.md`) | give it zones, connect it, or remove it |

A missing `<M>.md` does not hide the `overview.md` errors: both files are checked.

### Config: the `planning` block (`.forge/config.yml`, spec §6.1)

```yaml
planning:
  architecture: required       # required | optional
  max_tasks_per_agent: 10      # used in phase 14 (ADR 0020)
  max_components_per_agent: 3  # used in phase 14 (ADR 0020)
models:
  min_tier_for: {reviewer: strong, plan: strong, architecture: strong}
```

- **Parsing**: `PlanningConfig` in `core/config.py`, with unknown keys rejected and errors naming file, key and fix. The two `max_*` keys are integers ≥ 1. They are validated now and read by nothing until phase 14.
- **Defaults**: the `forge init` template writes `required`. When the `planning` block, or its `architecture` key, is missing, the default is **`optional`**. Existing projects, their existing plans and the `forge-plan-apply` workflow keep working with no edit.
- **`min_tier_for.architecture`**: the template adds it with `strong`. `min_tier_for` already accepts any key.

### Plan schema and `forge plan validate` (`core/plan.py`, spec §9.3–§9.4)

New optional task fields. Like `proposal`, they are plan-only. They are not §7.1 metadata, so `plan apply` never writes them to issues and the two issue regions of ADR 0013 do not change.

| Field | Form | Rule |
|---|---|---|
| `component` | a component id | required when the architecture is **checked** (below) |
| `implements` | list of `IF-…` ids | only on `type: contract` tasks; required on them when checked |
| `uses` | list of `IF-…` ids | optional |

**Checked or not.** The architecture is *checked* when `planning.architecture` is `required`, or when `docs/architecture/<M>.md` exists (whatever the mode). It is *unchecked* only when the mode is `optional` and the file is missing. Then:
- no architecture error is raised and the three fields are optional;
- one **info** message goes to the envelope `messages` (not to `data.errors`/`warnings`, whose levels are only error and warning): `no architecture for <M> (planning.architecture: optional)`;
- if any task still sets `component`, `implements` or `uses`, a warning `architecture_fields_unchecked` names the tasks.

Format rules apply in both cases and give `invalid_field`: a wrong type, a malformed id, or `implements` on a task that is not `type: contract`.

`validate_plan` gains a `planning` parameter (default: optional) so that current callers and tests keep their behaviour. The `data` gains `architecture: {mode, path, present, status, checked, interfaces: {IF-id: implementing task id or null}}`. The human view gets one "Architecture" line.

New errors (exit 1):

| Code | Condition |
|---|---|
| `architecture_missing` | mode `required` and `<M>.md` missing. No other architecture check runs. |
| `architecture_invalid` | `arch validate` reports an error. One issue per relayed error; the message starts with the original code (e.g. `[unknown_zone] …`) and keeps its file, field and hint. If the frontmatter could not be loaded, the task checks below are skipped. |
| `architecture_draft` | `<M>.md` has `status` other than `ready`. The task checks below still run, so the lead sees everything at once. |
| `unknown_component` | `component` is in neither `<M>.md` nor `overview.md` |
| `unknown_interface` | an `implements` id not declared in `<M>.md` (the hint says: to change an existing interface, declare it in `<M>.md`); or a `uses` id in neither `<M>.md` nor `overview.md` |
| `interface_unimplemented` | an interface of `<M>.md` has no contract task implementing it |
| `interface_multiply_implemented` | an interface of `<M>.md` is implemented by two or more tasks (one issue naming all of them) |
| `contract_without_interface` | a `type: contract` task with no `implements` |
| `uses_without_dependency` | a task uses an interface of `<M>.md` but does not depend, directly or transitively within the plan, on the task implementing it. Interfaces only in `overview.md` are exempt. A task that implements the interface itself is exempt. No issue is raised when the interface has no implementer (that is already `interface_unimplemented`) or when the graph has a cycle. |

Only `arch validate` **errors** are relayed. Its warnings (`overview_stale`, `component_unused`) appear only in `forge arch validate`.

New warnings:

| Code | Condition |
|---|---|
| `zone_outside_component` | a task touches a zone that is neither in its component's `zones` nor the `zone` of an interface it `implements` (zones of `uses` interfaces do not count) |
| `implementer_not_producer` | a contract task's `component` is not the `producer` of an interface it `implements` |
| `architecture_fields_unchecked` | unchecked mode, and tasks set `component`, `implements` or `uses` |

`forge plan apply` runs validate first (ADR 0013), so it inherits these errors. Nothing else in apply changes.

### Task context (`core/context.py`)

`TaskContext` gains `architecture`, filled for `forge task show` and for the context inside `forge next`:

```json
"architecture": {"path": "docs/architecture/M1.md", "component": "auth-service", "implements": [], "uses": ["IF-auth-api"]}
```

- **Sources**: `path` is the repository-relative path when the file exists in the checkout, else `null`. The other three come from the task's entry in `plans/<M>.yml` in the same checkout (the task worktree). The issue does not carry them.
- **Missing data**: an unreadable plan, or a task missing from it, gives `component: null` and empty lists. This adds **no warning and no message**, so the ADR 0017 golden test changes only by this field. That test strips `architecture` alongside `delegation` in both `next` and `task_show`; the golden files are not regenerated.
- **Human view**: a `## Architecture` block, shown only when `path` or `component` is set.
- **`forge review`**: its data does not change in this phase. The review prompt reads the task's entry in `plans/<M>.yml` (see point 7).

### Prompts (`.forge/prompts/`, spec §12, §12.1)

- **New `architect.md`**:
  - **Frontmatter**: `name: architect`, `roles: [lead]`, `args: "<milestone> [--update]"`. The prompt has the six required sections (ADR 0014). `COMMAND_ORDER` places it right after `plan`, as the user asked. The adapters render it as `/forge-architect` and `$forge-architect`.
  - **Steps**:
    1. check that the user is a lead in `team.yml`;
    2. read the milestone specs, `overview.md`, the ADRs, `zones.yml` and the code. Delegate the reading to `forge-explorer` when available;
    3. stop if a spec is draft or has unresolved questions;
    4. propose components, interfaces and decisions, and wait for the user's go;
    5. write `<M>.md` from `_template.md`, update `overview.md` (add `<M>` to `milestones` and merge its components and interfaces), and add ADRs or zones when needed;
    6. run `forge arch validate <milestone> --json` until there are no errors, then discuss the warnings;
    7. set `status: ready` only with the user's explicit ok;
    8. commit on branch `arch/<M>` and open a PR titled `arch: <M>`. Never push to the default branch.
  - **`--update`**: changes only what changed in the specs or the code, and reports which plan tasks the change affects.
  - **Exit codes**: `0`, `1` and `5` handled as in `plan`. `2`, `3` and `4` are not expected.
- **`plan.md`**:
  - the `<milestone>` path reads `overview.md` and `<M>.md`, and assigns `component`, `implements` and `uses`;
  - it never introduces a component or an interface. If the architecture is missing, draft or insufficient, it stops and points to `/forge-architect <M>` or `/forge-architect <M> --update`;
  - the documented sequence becomes `/forge-plan roadmap` → `/forge-architect M1` → merge → `/forge-plan M1`;
  - the exit-code section lists the new errors.
- **`dev.md`, `review.md`, `agents/explorer.md`, `agents/reviewer.md`**: they read `<M>.md` and `overview.md`. The reviewer checks that the change stays inside the task's component and respects the interfaces it implements and uses.
- **Count**: 12 prompts (`setup`, `configure`, `plan`, `architect`, `next`, `dev`, `sync`, `ship`, `review`, `status`, `handoff`, `release`). `tests/commands/test_docs_cmd.py` expects 12.

### What each spec section gains

ADR 0018 set the precedent that the ADR wins over the spec. Phase 13 also adds to each section below a one-line note pointing to this ADR, as §12.1 does for ADR 0017.

| Section | Gains |
|---|---|
| §5.2 | `docs/architecture/_template.md` and `docs/architecture/overview.md` (created by init), `docs/architecture/<milestone>.md` (written by `/architect`); prompt count 12. |
| §6.1 (needed, not in the brief's list) | the `planning` block and `min_tier_for.architecture`. |
| §9 | a new subsection "Architecture: `docs/architecture/`" (formats above); §9.3 gains `component`, `implements`, `uses`; §9.4 gains the new errors and warnings; §9.6 gains the sequence roadmap → architect → merge → plan, and the rule that `/plan` never invents components or interfaces. §9.2 (roadmap) and §9.5 (apply) do not change. |
| §12 | a table row `/architect` (lead): write or update `<M>.md` and `overview.md`, run `forge arch validate`, PR `arch: <M>`. The `/plan` row points to the new §9.6. |
| §12.1 | `explorer` is also used by `/architect`; `reviewer` checks component and interface boundaries. |
| §16 | `forge arch validate <M> [--force]`: validate the milestone architecture, offline. |

### Init and templates

- **Files**: `forge init` writes `docs/architecture/_template.md`, `docs/architecture/overview.md` and `.forge/prompts/architect.md`. The config template gains the `planning` block and `architecture: strong` in `min_tier_for`. The init snapshot test pins the new paths.
- **Gitignore**: `.forge/local/` is already ignored by the project template (ADR 0014). Nothing changes there; phase 14 can rely on it.
- **Zones**: the template `docs` zone (`docs/**`) already covers `docs/architecture/`.

### Out of scope

The roadmap, spec §19 items, `forge upgrade`, CODEOWNERS and branch protection. Existing projects get the new templates by re-running `forge init`, which keeps unchanged files and refuses to overwrite changed ones without `--force`. A project's existing `config.yml` therefore keeps no `planning` block and stays `optional` until the lead adds it.

## Consequences

- **Order of work**: with `required`, a milestone needs two merged PRs, `arch: <M>` and then `plan: <M>`. The plan can no longer change the design silently.
- **Old CLIs**: v0.1.0 rejects the `planning` key in `config.yml` and the new plan fields as unknown, with an error naming them. The release that ships this phase should bump the minor version, so that new projects pin it (see point 9).
- **CI**: `forge-plan-apply` runs only when `plans/*.yml` changes. A later architecture change that breaks an applied plan is caught on the next plan PR or by `/forge-plan <M> --update`, not by the workflow (see point 8).
- **Switching modes**: a project that switches to `required` with plans already applied gets `architecture_missing` on the next plan change, until `/forge-architect` writes the file.
- **Phase 14**: ADR 0020 will use `max_tasks_per_agent` and `max_components_per_agent`.

### Phase 13 work and tests

- **Tests**:
  - `tests/core/test_architecture.py`: one test per error and warning above, for both `<M>.md` and `overview.md`;
  - `tests/core/test_plan.py`: one test per new error and warning, plus the `required`, `optional` and missing-key cases;
  - `tests/commands/test_arch_validate_cmd.py`: JSON envelope; exits 0, 1 and 5 (tier refusal, and `--force`);
  - the config tests for `planning`;
  - the init snapshot;
  - the prompt checks (12 prompts, `architect` sections, the `forge arch validate` calls parsed against the CLI);
  - the golden test, which strips `architecture`.
- **Docs**:
  - README lead quickstart;
  - the project templates `docs/forge/onboarding.md`, `roles.md` and `faq.md` (the lead owns the architecture);
  - `docs/protocol.md`;
  - CHANGELOG `[Unreleased]`;
  - `docs/commands.md`, regenerated.

## Points confirmed by the user

The user approved the ADR on 2026-11-09 with the resolutions below.

1. **Spec path in examples**: `specs/m1/auth.md` (spec layout, lower-case directory as in §9.2/§9.3), not `specs/M1-auth.md`. The CLI checks only existence and frontmatter `milestone`, never the path layout.
2. **Spec milestone**: taken from the spec's frontmatter, as `plan validate` does (ADR 0013).
3. **`COMMAND_ORDER`**: `architect` comes right after `plan`, as requested, although the workflow runs `architect` before `plan`.
4. **"Earlier milestone" interfaces**: an interface in `overview.md` and not in `<M>.md` is treated as existing, exempt from `uses_without_dependency` and not implementable. An interface introduced by a later milestone whose architecture merged first would also count as existing. Adding a `milestone` field to overview items would make this exact.
5. **`implements` scope**: it may reference only interfaces of `<M>.md`. To change an existing interface, the architect declares it in `<M>.md`. `component`, by contrast, may come from `overview.md`. A contract task whose `component` is not the interface's `producer` gets the warning `implementer_not_producer`.
6. **Unchecked mode** (optional and no file): the fields are allowed, with the warning `architecture_fields_unchecked`. `component` is required for every task type, including `chore` and `test`, when checked.
7. **`zone_outside_component`**: it counts the contract zones of implemented interfaces as allowed, but not those of used interfaces. `forge review` data does not gain `architecture` in this phase.
8. **`arch validate` details**:
   - it has a `--force` flag for the tier, like `plan validate`, and reuses its exit 5;
   - it ignores the mode (a missing file is always exit 1);
   - it checks `overview.md`'s frontmatter as errors, but not its body;
   - `plan validate` relays only its errors, not its warnings;
   - it is not added to `forge-ci.yml` in this phase.
9. **Version bump**: the minor version should be bumped at release, because v0.1.0 rejects the `planning` key and the new plan fields.
10. **Spec text**: the spec gets one-line ADR notes in §5.2, §6.1, §9, §12, §12.1 and §16 rather than a rewrite. The same pass also adds ADR 0018's `forge config validate` (§16) and `/configure` (§12), which were missing.
11. **Doc names**: the project's `docs/architecture/` and the framework's `docs/architecture.md` live in different repositories. No rename is needed.
