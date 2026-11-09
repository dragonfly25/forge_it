# 0020 — Multi-agent planning: `forge plan split`, `forge plan assemble` and `forge arch split`

- Status: accepted
- Date: 2026-11-09

## Context

Today one agent session writes a whole milestone plan (`/forge-plan <M>`, §9.6) and a whole milestone architecture (`/forge-architect <M>`, ADR 0019). A large milestone fills that session's context, and the quality of the last tasks drops. Phase 13 added the config keys that bound the work per agent but nothing reads them yet:

```yaml
planning:
  max_tasks_per_agent: 10      # ADR 0019, integer >= 1, default 10
  max_components_per_agent: 3  # ADR 0019, integer >= 1, default 3
```

The user decided:
- **Balancing**: only tasks are counted. Up to `max_tasks_per_agent` tasks, one session does the detail. Above it, `ceil(N / max)` agents get balanced groups: 12 → 6+6, 20 → 10+10, 21 → 7+7+7, 25 → 9+8+8. The architecture uses the same formula, with components as the unit and `max_components_per_agent` as the threshold.
- **Plan phases**: exploration, skeleton, detail, review. The CLI splits the skeleton into groups and assembles the fragments that the planner subagents write.
- **Architecture phases**: exploration, structure, detail, review. The CLI splits the components into groups. The main session integrates the fragments by hand.
- **Two new subagents** for the detail work, and a sequential fallback when subagents are not available.

This ADR fixes the core function, the commands, the file formats, the prompt changes and the subagents. It resolves the open points against the current code (`core/plan.py`, `core/tasks.py`, `core/models.py`, `core/architecture.py`, `core/prompts.py`, `core/adapters/`).

### Tool verification (2026-11-09)

Checked against the official documentation on 2026-11-09.

**Claude Code** ([sub-agents](https://code.claude.com/docs/en/sub-agents.md)):
- **Parallelism**: several subagents can run in parallel. The fan-out is driven by the prompt. Subagents run in the background by default. A session runs up to 20 at once; `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` changes the cap. Nesting goes up to 3 levels.
- **Filesystem**: a subagent starts in the parent's working directory and shares its filesystem. A subagent with edit tools can write files.
- **Isolation**: the optional `isolation: worktree` creates a worktree that branches from the default branch, not from `HEAD`. The skeleton lives on the `plan/<M>` branch, so a worktree subagent would not see it. Forge does not render `isolation` and must not.
- **Concurrent writes**: Claude Code does not handle conflicts between subagents that write. Parallel writers need disjoint sets of files.
- **Definition fields**: the fields Forge renders (`name`, `description`, `tools`, `model`, `effort`) match the documentation. No adapter change is needed.

**Codex** ([subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents); `developers.openai.com/codex/subagents` redirects there):
- **Enabled by default** (`agents.enabled`). Custom subagents are TOML files in `.codex/agents/`, which is what Forge renders.
- **Spawning**: Codex spawns subagents only when asked explicitly ("spawn one subagent per …"), or when a skill or `AGENTS.md` tells it to. Spawned subagents run in parallel, up to `agents.max_concurrent_threads_per_session`.
- **Sandbox**: subagents inherit the parent's sandbox. There is no per-agent tool list and no per-agent read-only sandbox. "Write only your fragment" is therefore an instruction, not an enforced limit. The documentation warns about parallel workflows that write a lot.

**Consequences for Forge**:
- every parallel writer writes its own fragment file, so the files are disjoint and parallel work is safe in both tools;
- the prompts must say explicitly "spawn one subagent per group, in parallel";
- when spawning fails, or the tool has no subagents (generic adapter, local models), the main session does the same work one group at a time.

## Decision

### Core: `src/forge/core/batching.py`

A pure, deterministic module (mypy-strict, no typer, no rich, no I/O).

```python
def balanced_batches(
    items: Sequence[T], max_per_agent: int, key: Callable[[T], SupportsLessThan]
) -> list[list[T]]
```

1. `max_per_agent < 1` raises `ValueError`. The config loader already rejects such values, so this is only a programming guard.
2. `N = len(items)`. `N = 0` returns `[]`.
3. `k = 1` when `N <= max_per_agent`, else `k = ceil(N / max_per_agent)`.
4. Sort the items by `key` (a stable sort).
5. Cut the sorted list into `k` contiguous groups. Every group has `N // k` items, and the first `N % k` groups get one more.

The keys:
- **Tasks**: `(component or "", wave number, natural_key(id))`. The wave is the task's topological level in the skeleton (`TaskGraph.waves`, dependencies on other milestones count as satisfied, as in ADR 0013). A task without a `component` sorts first (unchecked architecture, ADR 0019). Task ids are unique, so the order is total.
- **Components**: the component id.

Sorting by component keeps a component's tasks together. Sorting by wave inside a component puts contracts before the work that depends on them.

Group sizes (`max_per_agent = 10`):

| N | Groups |
|---|---|
| 0 | none (`[]`) |
| 10 | 10 |
| 11 | 6, 5 |
| 12 | 6, 6 |
| 20 | 10, 10 |
| 21 | 7, 7, 7 |
| 25 | 9, 8, 8 |
| 30 | 10, 10, 10 |
| 31 | 8, 8, 8, 7 |

### Words used in this ADR

- **Skeleton fields** of a task: `id`, `title`, `type`, `spec`, `covers`, `depends_on`, `zones`, `size`, `allow_contract_change`, `proposal`, `component`, `implements`, `uses`. The main session writes them in phase 2. The user's list did not name `allow_contract_change` and `proposal`. They are added here because the `contract_zone` check depends on `allow_contract_change`, and `proposal` decides which issue `plan apply` adopts. Neither is detail work.
- **Detail fields**: `description`, `done_when`, `tier`. The planners write them in phase 3.
- These are not the "structural" changes of §9.5. That section decides what `plan apply` may change on a started task, and there `title` and `description` are non-structural while `tier` is structural. This ADR does not change §9.5.

### `forge plan validate <M> --skeleton`

- **Skipped**: only the `missing_field` errors for `description`, `done_when` and `tier`. The rest of the code does not depend on these three fields:
  - `tier` is checked only as a value of the enum `strong | standard | light` (`tasks.parse_metadata_fields`);
  - `models.min_tier_for` is keyed by role or command (`plan`, `architecture`, `reviewer`, a subagent name). It is applied in `models.resolve` (step 2) and `models.check_min_tier` to the agent that runs a command, and never to a task's `tier`. A task's tier is used later, by `forge next` (tier filtering) and by model resolution;
  - `done_when` is checked only as a non-empty list of non-empty strings;
  - `description` is checked only for its type and for Forge markers (`description_text_problem`).
- **Present detail fields** are still checked, as without the flag. A skeleton may carry them, for example from an earlier run.
- **Everything else runs in full**: unknown fields, ids, specs, zones, contract zones, the graph and the waves, coverage, parallelism and the architecture checks of ADR 0019. A draft architecture is still an error, so the skeleton cannot get past a draft architecture.
- **Implementation note**: `TaskMetadata` requires `tier` and `done_when`. In skeleton mode the loader fills internal placeholders before it builds the metadata. The placeholders are never written and never reported.
- **Same command otherwise**: the same `min_tier_for.plan` check, the same `--force`, the same exit codes `0`, `1` and `5`. `data` gains `"skeleton": true`.
- `forge plan apply` has no `--skeleton` flag.

### `forge plan split <M> [--json]`

The command is offline. It reads files and runs one local `git check-ignore`. It never calls GitHub.

1. **Preconditions** (exit 5): `.forge/config.yml` exists and loads. `plans/<M>.yml` exists (code `plan_missing`: "write the skeleton first"). `.forge/local/plan/<M>/` would be ignored by git (code `not_ignored`: "add `.forge/local/` to `.gitignore`"), the same rule as subagent rendering (ADR 0014).
2. **Skeleton check** (exit 1): it runs the content checks of `forge plan validate <M> --skeleton`, without the tier check. The command refuses on any error and returns them in `data.errors`. Warnings are returned and do not block. Waves need an acyclic graph and grouping needs valid components, so splitting a broken skeleton would give meaningless groups. An invalid milestone argument, a broken `zones.yml` and a skeleton with no tasks (code `plan_empty`) also give exit 1.
3. **Grouping**: `balanced_batches(tasks, planning.max_tasks_per_agent, key)` with the task key above. When the `planning` block is missing, the default 10 applies.
4. **Writes** `.forge/local/plan/<M>/batches.json` and creates the directory. It deletes any `part-*.yml` file left in that directory by an earlier split and lists them in `data.removed_parts`. Old fragments could otherwise be assembled against new groups.

`batches.json`:

```json
{
  "version": 1,
  "milestone": "M1",
  "skeleton_sha256": "…",
  "max_per_agent": 10,
  "agents": 2,
  "batches": [
    {"index": 1, "task_ids": ["M1-T01", "M1-T02", "…"], "part_path": ".forge/local/plan/M1/part-1.yml"},
    {"index": 2, "task_ids": ["…"], "part_path": ".forge/local/plan/M1/part-2.yml"}
  ]
}
```

- `index` starts at 1. Paths are relative to the repository root. The file is written with sorted keys and a final newline, so the same skeleton gives the same bytes.
- `skeleton_sha256` is the SHA-256 of the canonical JSON (sorted keys, no spaces) of `{"milestone": M, "tasks": [skeleton fields of each task, in file order]}`. Only skeleton fields count. Comments, formatting and detail fields do not change it.
- **Output** `data`: `milestone`, `file` (`plans/<M>.yml`), `batches_path`, `agents`, `max_per_agent`, `batches` (as in the file), `removed_parts`, `subagent` (below), `warnings`.
- **`agents: 1`** means a single session. The main session writes the detail fields straight into `plans/<M>.yml` and does not use `part_path` or `assemble`. The batch still has a `part_path`, so the format has one shape.
- **No `--max-per-agent` flag**: the bound is project policy in `config.yml`, reviewed through a PR. A flag would let two runs of the same plan disagree, and the prompts have no reason to change it.
- **`subagent`**: `{"name": "forge-planner", "available": bool, "reason": "…"}`. It uses the capability rule of ADR 0017: the active agent's tool is `claude-code` or `codex`, a profile exists, and the rendered file exists (`.claude/agents/forge-planner.md` or `.codex/agents/forge-planner.toml`). The prompt reads it instead of guessing. It still falls back when spawning fails at run time.

### Fragment format: `.forge/local/plan/<M>/part-<k>.yml`

```yaml
milestone: M1
batch: 2
tasks:
  - id: M1-T07
    tier: standard
    description: |
      …
    done_when:
      - …
```

- **Top-level keys**: `milestone`, `batch` and `tasks`, all required. `milestone` and `batch` catch a fragment saved under the wrong path.
- **Task keys**: `id`, `description`, `done_when` and `tier`, all required. The rules are the ones of the full plan validation.
- Only the tasks of that batch, each once, in any order.

### `forge plan assemble <M> [--json]`

The command is offline and atomic: on any error it writes nothing.

1. **Preconditions** (exit 5): `.forge/config.yml` exists. `plans/<M>.yml` exists. `batches.json` exists and parses (code `split_missing`: "run `forge plan split <M>` first").
2. **Skeleton unchanged** (exit 1, `skeleton_changed`): it recomputes the hash of the current `plans/<M>.yml` and compares it with `skeleton_sha256`. The hint is "run `forge plan split <M>` again and redo the detail".
3. **Fragments** (exit 1). Every error names the fragment file, the task and the field:

| Code | Condition |
|---|---|
| `fragment_missing` | `part-<k>.yml` of a batch does not exist |
| `fragment_invalid` | not valid YAML, not a mapping, or a top-level key missing or unknown |
| `fragment_mismatch` | `milestone` or `batch` does not match the file's batch |
| `unassigned_task` | a task id not assigned to that batch (including ids of other batches and unknown ids) |
| `missing_task` | an id assigned to that batch is absent |
| `duplicate_id` | an id repeated within the fragment |
| `skeleton_field` | a fragment task sets a skeleton field other than `id` (for example `zones`) |
| `unknown_field` | any other key |
| `missing_field` / `invalid_field` | a detail field is missing, or breaks the plan rules (tier not in the enum, empty `done_when` or empty item, description with a Forge marker) |

4. **Merge in memory**: for each task of the skeleton, the fragment's `description`, `done_when` and `tier` replace any value already there. Skeleton fields and task order do not change.
5. **Full validation** of the merged plan in memory: the same content checks as `forge plan validate <M>`, without the tier check. Its errors are reported as they are (exit 1) and nothing is written. Its warnings are returned in `data.warnings` and do not block.
6. **Write**: the plan is rendered to a temporary file in `plans/`, then moved over `plans/<M>.yml` (`os.replace`).
7. **Output** `data`: `milestone`, `file`, `written`, `changed` (false when the bytes are identical), `batches`, `tasks`, `errors`, `warnings`, `counts`.
8. Fragments and `batches.json` stay in place: they are git-ignored, and keeping them helps to inspect a run. The next `split` clears the fragments.

**YAML rendering.** Assemble rewrites the whole file from data. YAML comments in the skeleton are lost, and formatting changes. Keeping them would need a round-trip YAML library, which is not an allowed dependency. The output is deterministic:
- top-level keys `milestone`, then `tasks`;
- task keys in `TASK_KEYS` order, which follows the §9.3 example, then the optional fields; absent optional keys are left out;
- multi-line strings as `|` blocks; `done_when` as a block list; `covers`, `depends_on`, `zones`, `implements` and `uses` in flow style (`[a, b]`);
- reading the file back gives the same data.

The `plan` prompt tells the main session not to put comments in the skeleton. Reasons go in the PR body.

**Determinism and re-runs.** The same skeleton and fragments give the same bytes. The hash covers only skeleton fields, so running `assemble` again after a success finds the same hash, rewrites the same file and reports `changed: false`. Nothing is shared through GitHub, so no test with two racing users is needed.

**Exit codes** (ADR 0004, §16), for `split` and `assemble`: `0` done; `1` errors in the skeleton, the fragments or the merged plan, or an invalid argument; `5` a precondition failed (no config, no skeleton, no `batches.json`, the local directory not ignored). `2`, `3` and `4` are never used. A CLI version outside `forge_version` gives a warning, as for `plan validate`.

### `forge arch split <M> [--json]`

The command is analogous to `plan split`, with no assemble step.

- **Preconditions** (exit 5): `.forge/config.yml` exists; `docs/architecture/<M>.md` exists (code `architecture_missing`: "write the frontmatter first").
- **Structure check** (exit 1): it runs `validate_architecture` and refuses on any error except `missing_section`. The body is still being written in this phase. `open_questions` cannot fire, because the file is `draft`. Like `arch validate`, it ignores `planning.architecture`. It runs no tier check.
- **Grouping**: `balanced_batches(components of <M>.md, planning.max_components_per_agent, key=id)`. An interface goes to the batch of its `producer`. An interface of `<M>.md` whose producer is declared only in `overview.md` goes to `unassigned_interfaces`, and the main session writes it.
- **Writes** only the directory `.forge/local/arch/<M>/`, and deletes stale `part-*.md` files there. It writes no `batches.json`, because nothing reads it later.
- **Output** `data`: `milestone`, `file`, `agents`, `max_per_agent`, `batches: [{index, component_ids, interface_ids, part_path}]` with `part_path` `.forge/local/arch/<M>/part-<k>.md`, `unassigned_interfaces`, `removed_parts`, `subagent` (`forge-arch-writer`, same capability rule).
- **Exit codes**: `0`, `1` and `5`, as above.
- **No `forge arch assemble`**: the user did not ask for one. The body of `<M>.md` is free Markdown under fixed level-2 sections, and the main session has to integrate the fragments: write one coherent `## Components` and `## Interfaces`, and update `overview.md`. A mechanical merge would add a format to maintain and save little. `forge arch validate` checks the result.

Architecture fragment (`part-<k>.md`), not read by the CLI: a `## Components` section with one `### <component-id>` subsection per assigned component; a `## Interfaces` section with one `### <IF-id>` subsection per assigned interface; and a `## Notes for the main session` section with the data model changes, decisions, risks and open questions the writer found.

### Plan phases (`/forge-plan <M>`, `plan.md`)

1. **Exploration**: in parallel, one `forge-explorer` per milestone spec and one per component involved (from `<M>.md`; in unchecked mode, one per code area). Each returns a compact summary.
2. **Skeleton**: the main session writes `plans/<M>.yml` with skeleton fields only, then runs `forge plan validate <M> --skeleton --json` until there are no errors.
3. **Detail**: `forge plan split <M> --json`.
   - `agents: 1`: the main session writes the detail fields into `plans/<M>.yml` itself.
   - `agents > 1` and `subagent.available`: spawn one `forge-planner` per batch, **in parallel**, in a single step. When the tool caps the number of concurrent subagents, spawn the rest as earlier ones finish. Then run `forge plan assemble <M> --json`.
4. **Review**: in parallel, one `forge-reviewer` per batch (one when `agents: 1`). Each checks its tasks against the specs, `<M>.md` and the rest of the plan. Then the main session checks the whole plan, fixes `plans/<M>.yml` directly (the fragments are no longer used), and runs `forge plan validate <M> --json` until there are no errors.

**Planner brief**: the goal; the milestone and batch index; the task ids; the skeleton path `plans/<M>.yml`; `docs/architecture/<M>.md` and `overview.md`; the specs and the AC each task covers; the fragment path and its format; the rules for the detail fields (testable `done_when` items, `tier` by difficulty, no Forge markers in the description); and a prohibition on writing any other file or any skeleton field.

**`--update`** stays single-session: it makes the smallest change set, usually far below the threshold. `split` and `assemble` are for writing a milestone plan.

### Architecture phases (`/forge-architect <M>`, `architect.md`)

1. **Exploration**: in parallel, one `forge-explorer` per spec and one per code area the specs touch.
2. **Structure**: the main session proposes the frontmatter (components, interfaces, key decisions) and waits for the user's go, as today. Then it writes the frontmatter of `<M>.md`.
3. **Detail**: `forge arch split <M> --json`. With `agents: 1`, the main session writes the body. With `agents > 1`, it spawns one `forge-arch-writer` per batch, in parallel. Each writer writes only its `part-<k>.md`. The main session then integrates the fragments into the body of `<M>.md`, writes the other sections and the `unassigned_interfaces`, and updates `overview.md`.
4. **Review**: two `forge-reviewer` subagents in parallel. One checks spec coverage. The other checks consistency with the code, the ADRs and `overview.md`. The main session integrates the findings and runs `forge arch validate <M> --json` until there are no errors.

The user's go and the `status: ready` rule of ADR 0019 do not change.

### Fallback

Without subagents (generic adapter, local models, `subagent.available: false`), or when spawning fails, the main session runs the same phases one group at a time:
- **Plan**: it writes `part-1.yml` … `part-k.yml` itself, using the same `split`, then runs `assemble`.
- **Architecture**: it writes the body group by group, using the same `arch split` groups.

The final report says that the sequential fallback was used, and why.

### New subagents (`.forge/agents/`)

| File | Rendered as | Tier / effort | Tools | Used by | Purpose |
|---|---|---|---|---|---|
| `planner.md` | `forge-planner` | standard / medium | read, edit | `/plan` | Write `description`, `done_when` and `tier` for one batch, into its fragment only |
| `arch-writer.md` | `forge-arch-writer` | strong / high | read, edit | `/architect` | Write the component and interface sections for one batch, into its fragment only |

- The existing adapters render them. Claude maps the tools to `Read, Grep, Glob, Edit, Write`. Codex has no tool list. The generic adapter lists them in each project's `AGENTS.md`.
- No `shell`: neither subagent runs `forge`, `git` or tests.
- **Rules in both bodies**: write only the given fragment path; never edit `plans/`, `docs/`, `specs/` or `.forge/`; never set a skeleton field (planner); never add or rename a component or interface (arch-writer); stop and report when the skeleton or the architecture looks wrong; return a summary of at most 10 lines.
- **Name**: the user proposed `architect.md`, rendered as `forge-architect`. The rendered files do not collide: the skill is `.claude/skills/forge-architect/SKILL.md` and `.agents/skills/forge-architect/SKILL.md`, the subagent is `.claude/agents/forge-architect.md` and `.codex/agents/forge-architect.toml`. The two tools invoke skills and subagents through different mechanisms, but the documentation checked above does not say whether one name may serve both. Three places would still be ambiguous:
  - the generated `AGENTS.md` lists `/architect` and `/forge-architect` as a command and `forge-architect` as a subagent;
  - the `CLAUDE.md` region says that commands are `/forge-<cmd>` and subagents are `forge-*`;
  - the `/forge-architect` skill would tell the model to "spawn `forge-architect`", which can be read as invoking itself.

  This ADR therefore names the subagent **`arch-writer`** (`forge-arch-writer`). See point 1.
- **Tier raising**: `resolve_subagent` passes the subagent's name as its role, so `models.min_tier_for.planner` and `models.min_tier_for.arch-writer` can raise these subagents. `min_tier_for.plan` and `min_tier_for.architecture` do not raise them: those keys gate the agent that runs `forge plan validate` or `forge arch validate`, which is the main session. See point 11.
- **`reviewer.md`**: it gains one paragraph for reviewing a plan batch or an architecture, with no branch or PR involved. It checks the given files against the specs, the architecture and the rules, and reports in the same form. `explorer.md` does not change.
- **Six subagents**: `explorer`, `test-writer`, `reviewer`, `implementer`, `planner`, `arch-writer`.

### Prompts

- **`plan.md` and `architect.md`**: the `## Subagents` section describes the four phases, the parallel spawn wording ("spawn one `forge-planner` subagent per batch, in parallel"), the briefs and the fallback. `## Steps` calls the new commands. `## Exit codes` covers `split`, `assemble` and `--skeleton`. `## Final report` adds the number of agents, the batches, and whether the fallback ran.
- **Prompt checks**: the `forge` calls in the prompts are parsed against the CLI, so the commands must exist before the prompts use them. The prompt count stays 12.

### Spec notes

As in ADR 0019, each section gets a one-line note pointing to this ADR:

| Section | Gains |
|---|---|
| §9 (§9.4, §9.6, §9.7) | `--skeleton`; the four plan phases with `split` and `assemble`; the four architecture phases with `arch split` |
| §12 | the `/plan` and `/architect` rows mention multi-agent planning |
| §12.1 | `planner` and `arch-writer` in the table; `explorer` also used by `/plan`; `reviewer` also used by `/plan` and `/architect` |
| §16 | `forge plan validate <M> [--skeleton] [--force]`, `forge plan split <M>`, `forge plan assemble <M>`, `forge arch split <M>` |
| §5.3 (needed, not in the brief's list) | `.forge/local/plan/<M>/` and `.forge/local/arch/<M>/` under local state |

### Out of scope

The roadmap path (`/forge-plan roadmap`), spec §19, `forge upgrade`, CODEOWNERS, branch protection, an `arch assemble` command, and multi-agent `--update`.

## Consequences

- **Context**: large milestones are planned in bounded contexts. A plan above the threshold costs more total tokens (briefs, re-reading the skeleton), as ADR 0017 noted for delegation.
- **Two commands per run**: `split` and `assemble` are deterministic and offline, so they can be tested without GitHub. The reasoning stays in the prompts.
- **Formatting**: `assemble` reformats `plans/<M>.yml` and drops its comments. The first plan PR shows the rendered form.
- **Existing projects**: they get `planner.md` and `arch-writer.md` by re-running `forge init`, then `forge adapters generate --local`. Without them, `subagent.available` is false and the prompts fall back to sequential work. A project without a `planning` block uses the defaults 10 and 3.
- **Old CLIs**: they do not know `split`, `assemble`, `arch split` or `--skeleton`. Prompts from a new `forge init` need the new CLI version, which is pinned in `forge_version`.

### Phase 14 work and tests

- **Code**:
  - `core/batching.py`;
  - `--skeleton` in `core/plan.py`;
  - a new `core/plan_split.py` for split, assemble, the hash and the rendering;
  - `forge arch split` in `core/architecture.py` (or a small `core/arch_split.py`);
  - commands in `commands/plan.py` and `commands/arch.py`;
  - templates `planner.md` and `arch-writer.md`, and the `reviewer.md` paragraph;
  - the `plan.md` and `architect.md` prompts.
- **Tests**:
  - `tests/core/test_batching.py`: the matrix above, `ValueError` for 0, and determinism (shuffled input gives the same output);
  - `--skeleton`: the three fields may be missing, present detail fields are still checked, every other error still fires;
  - split: happy path, every exit 1 and exit 5 case, determinism, the removal of stale fragments, and the `subagent` block;
  - assemble: happy path, every error code in the table, atomicity (the plan bytes do not change on any error), `skeleton_changed`, a re-run (`changed: false`), and two assembles of the same input giving identical bytes;
  - arch split: grouping, `unassigned_interfaces`, refusal on frontmatter errors, `missing_section` tolerated, exit codes;
  - tests that pin the subagent set: `test_prompts.py`, `test_adapters.py`, `test_init.py` and the init snapshot (`AGENTS.md`);
  - the prompt checks (12 prompts).
- **Docs**:
  - README: at most one line;
  - `docs/guides/lead.md`: a multi-agent planning section;
  - `docs/protocol.md`;
  - `docs/adapters.md`: six subagents;
  - CHANGELOG `[Unreleased]`;
  - the spec notes above;
  - `docs/commands.md`, regenerated.

## Points confirmed by the user

The user approved the ADR on 2026-11-09 with the recommendation given for each point below.

1. **Subagent name**: `arch-writer` (`forge-arch-writer`) instead of `architect`. Rendered files would not collide, but `AGENTS.md`, the `CLAUDE.md` note and the skill's own text would use one name for a command and a subagent. *Recommendation: rename.*
2. **Skeleton fields** include `allow_contract_change` and `proposal`, besides the user's list. *Recommendation: yes; both are decisions of the main session, and a fragment that sets them fails with `skeleton_field`.*
3. **Hash over skeleton fields only**, not the raw file bytes. Reformatting or adding detail does not count as "skeleton changed", and `assemble` can be re-run. *Recommendation: yes.*
4. **Comments are lost** when `assemble` rewrites `plans/<M>.yml`, and the file is reformatted deterministically. *Recommendation: accept; the prompt keeps comments out of the skeleton.*
5. **`split` refuses a skeleton with errors** (exit 1). *Recommendation: yes; waves and groups need a valid graph and components.*
6. **No `--max-per-agent` flag**: the bound comes only from `config.yml`. *Recommendation: no flag in this phase.*
7. **Exit codes**: no skeleton, no `batches.json` or a local directory that git does not ignore is a precondition (5). `skeleton_changed`, fragment errors and validation errors are content errors (1). *Recommendation: as written.*
8. **`--update` stays single-session**. *Recommendation: yes; revisit if large updates appear.*
9. **Fragment top-level keys** `milestone` and `batch` are required. *Recommendation: yes; they catch a fragment saved under the wrong path at little cost.*
10. **`arch split`**: JSON analogous to `plan split`, no `batches.json`, no `arch assemble`. Interfaces whose producer is only in `overview.md` stay with the main session. Every frontmatter error except `missing_section` blocks. *Recommendation: as written.*
11. **Tier of the new subagents**: `min_tier_for.plan` does not raise `forge-planner`; a project raises it with `min_tier_for.planner`. *Recommendation: keep the existing rule (the subagent's own name is the role), and document it in `docs/adapters.md`.*
12. **`subagent` capability block** in the output of `split` and `arch split` (ADR 0017's rule). *Recommendation: yes; the prompt then does not guess whether `forge-planner` exists.*
13. **`reviewer.md`** gains a paragraph for reviewing a plan or an architecture, instead of adding a seventh subagent. *Recommendation: yes.*
14. **`split` deletes stale fragments** of the milestone and lists them. *Recommendation: yes; old fragments could otherwise be assembled against new groups.*
15. **No framework-level `AGENTS.md`**: the brief lists "AGENTS.md list" as a file to update. The framework repository has no `AGENTS.md`. Each project's `AGENTS.md` is generated by the generic adapter from `.forge/agents/`, so the list updates by itself and only the init snapshot changes. *Recommendation: update the snapshot, `docs/adapters.md` and spec §12.1 instead.*
16. **Empty skeleton**: `split` on a plan with no tasks exits 1 (`plan_empty`). *Recommendation: yes.*
