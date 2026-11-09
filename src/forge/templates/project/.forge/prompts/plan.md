---
name: plan
description: Turn a project spec into a roadmap, or a milestone's specs into a validated task plan, and open a PR
args: "roadmap <spec-file> | <milestone> [--update] [--agent <id>]"
roles: [lead]
---
## Goal
Produce plans that the team can run in parallel: demoable milestones, tasks sized S or M, contract tasks first, every task inside known zones and inside a component that the milestone's architecture declares. The plan arrives as a PR for the team to review. The CLI validates and applies it. You do the creative work.

## Steps
Check first that the user is a lead (`.forge/team.yml`). If not, say so and stop.

### `roadmap <spec-file>`
1. Read the spec file, `.forge/zones.yml`, `docs/decisions/` and the existing code.
2. Ask the user clarifying questions first. Wait for the answers. Do not guess scope.
3. Propose milestones as demoable vertical slices (each ends in something that can be shown). Show the list and wait for approval.
4. On a new branch `plan/roadmap`, write `plans/roadmap.yml` (id `M<n>`, title, goal, demo, lead, specs) and split the document into one spec per milestone feature under `specs/<milestone>/<feature>.md`, from `specs/_template.md`. Use `status: draft` while open questions remain. Acceptance criteria are `AC-n`, unique within a milestone.
5. Plan only the first milestone in detail. Plan milestone N+1 when milestone N is about 70% done.
6. Commit, push the branch and open a PR titled `plan: roadmap` with `git` and `gh`. Never push to the default branch.

### The sequence for a milestone
`/forge-plan roadmap <spec-file>` → `/forge-architect M1` → merge the `arch: M1` PR → `/forge-plan M1`.
The architecture (`docs/architecture/<milestone>.md`) decides the components and the interfaces. The plan only turns them into tasks. With `planning.architecture: optional` in `.forge/config.yml` and no `<milestone>.md`, you may plan without it; `forge plan validate` then says so with an info message.

### `<milestone>`
Writing a milestone plan has four phases: exploration (steps 1 to 4), skeleton (steps 5 and 6), detail (step 7) and review (step 8). The section Subagents says who does each phase.
1. Get context: `forge status --json --milestone <milestone>`
2. Read the milestone's specs, `docs/architecture/overview.md`, `docs/architecture/<milestone>.md`, `.forge/zones.yml`, `docs/decisions/` and the code (phase 1, exploration).
3. If a spec has `status: draft` or open questions that are not RESOLVED, list the questions for the user and stop. Do not plan from an unready spec.
4. Check the architecture. Stop and point the user to `/forge-architect <milestone>` when `<milestone>.md` is missing and `planning.architecture` is `required` (the default for new projects). Stop and point to `/forge-architect <milestone>` when it has `status: draft`, and to `/forge-architect <milestone> --update` when the plan would need a component or an interface that neither `<milestone>.md` nor `overview.md` declares. Never invent a component or an interface in the plan.
5. On a new branch `plan/<milestone>`, write the skeleton of `plans/<milestone>.yml` contract-first. The skeleton has, for each task, the fields `id`, `title`, `type`, `spec`, `covers`, `depends_on`, `zones`, `size` and, when they apply, `allow_contract_change`, `proposal`, `component`, `implements` and `uses`. It has no `description`, `done_when` or `tier` yet. Do not put YAML comments in it: `forge plan assemble` rewrites the file and drops them. Reasons go in the PR body. Rules:
   - when the architecture applies (mode `required`, or `<milestone>.md` exists), every task has `component`: a component id from `<milestone>.md` or `overview.md`. Its `zones` stay within that component's zones, plus the contract zones of the interfaces it implements;
   - each interface declared in `<milestone>.md` has exactly one task of `type: contract` that lists it in `implements`. That task's `component` is the interface's `producer`. Only contract tasks use `implements`, and only with interfaces of `<milestone>.md`;
   - a task that consumes an interface lists it in `uses`. If the interface is in `<milestone>.md`, the task depends, directly or through other tasks, on the task that implements it;
   - tasks of `type: contract` come first and touch the contract zones;
   - implementation tasks depend on them with `depends_on`;
   - sizes are S or M only; split anything larger;
   - every task has `covers` and `zones` (all defined in `zones.yml`);
   - keep tasks of the same wave in different zones.
   If a new zone is needed, add it to `.forge/zones.yml` in the same PR and say so. A component's zones are set in the architecture: if the new zone belongs to a component, stop and point to `/forge-architect <milestone> --update` instead.
6. Validate the skeleton until there are no errors:
   `forge plan validate <milestone> --skeleton --json`
   It runs every check except the missing `description`, `done_when` and `tier` errors.
7. Write the detail (phase 3). Split the skeleton into batches:
   `forge plan split <milestone> --json`
   It writes `.forge/local/plan/<milestone>/batches.json` and returns `data.agents`, `data.batches` (each with `index`, `task_ids` and `part_path`) and `data.subagent`.
   - `agents: 1`: write the detail fields straight into `plans/<milestone>.yml` yourself. Do not use `part_path` or `assemble`.
   - `agents` above 1: each batch's detail goes into its fragment `part_path`, written by one `forge-planner` per batch or by you (see Subagents). Then merge the fragments:
     `forge plan assemble <milestone> --json`
   The detail fields, for every task:
   - `description`: what to build and where, in plain sentences;
   - `done_when`: items that a test can prove, covering every criterion in `covers`;
   - `tier` by difficulty (`strong` for hard design or risky work, `light` for mechanical work).
   Do not change a skeleton field in this phase. If one must change, fix the skeleton, run steps 6 and 7 again, and redo the detail.
8. Review the plan (phase 4, see Subagents), fix `plans/<milestone>.yml` directly, then validate until there are no errors:
   `forge plan validate <milestone> --json`
   Fix each error. Address each warning, or explain why you keep it in the PR body. `data.architecture` shows the mode, the file and which task implements each interface.
9. Open a PR titled `plan: <milestone>` from the branch `plan/<milestone>`. In the body: waves, AC coverage matrix, warnings you accepted, new zones, and how many agents wrote the detail.

### `<milestone> --update`
An update stays in one session: no `split` and no `assemble`.
1. Diff the specs and `docs/architecture/<milestone>.md` against the existing `plans/<milestone>.yml`. Make the smallest set of changes. If the specs changed in a way the architecture does not cover, stop and point to `/forge-architect <milestone> --update` first.
2. See what applying would do to the existing issues:
   `forge plan apply <milestone> --dry-run --json`
3. Validate, then open a PR titled `plan: <milestone>`. List in the body every structural change to a task that is already started. The CLI does not apply those. It warns the assignee, so the lead must talk to them.

### Proposals
- `forge status --json` lists proposals awaiting the lead in the Health section.
- To accept a proposal, add a task to the plan with `proposal: <issue number>`. Apply then turns that issue into the task.
- To reject one, comment on the issue and close it. Do not add it to the plan.

### After the PR merges
The `forge-plan-apply` workflow runs validate and apply. If the user asks you to apply by hand, confirm first, then run:
`forge plan apply <milestone> --json`

## Exit codes
- `forge plan validate`: `0` valid (read the warnings); `1` errors in the plan, specs, zones or architecture: fix them and run again; `5` a precondition failed (for example no `.forge/config.yml`, or your agent is below the tier that `min_tier_for.plan` requires). A CLI version outside `forge_version` is only a warning for validate. Validate is offline: it does not read GitHub. With `--skeleton` the codes are the same; only the missing `description`, `done_when` and `tier` errors are skipped, and `data.skeleton` is `true`.
- `forge plan split`: `0` batches written (read `data.removed_parts`: old fragments it deleted); `1` the skeleton has errors (`data.errors`, the same as `--skeleton` validation), the plan has no tasks (`plan_empty`), or the milestone argument or `zones.yml` is invalid: fix the skeleton and run it again; `5` a precondition failed: no config, no `plans/<milestone>.yml` (`plan_missing`: write the skeleton first), or `.forge/local/` is not ignored by git (`not_ignored`: add it to `.gitignore`). Split is offline.
- `forge plan assemble`: `0` the plan is written (`data.changed` is `false` when the bytes did not change); `1` nothing was written: `skeleton_changed` (the skeleton changed after the split: run `forge plan split <milestone> --json` again and redo the detail), a fragment error (`fragment_missing`, `fragment_invalid`, `fragment_mismatch`, `unassigned_task`, `missing_task`, `duplicate_id`, `skeleton_field`, `unknown_field`, `missing_field`, `invalid_field`: each names the fragment, the task and the field; fix that fragment, or have its planner redo it, and run assemble again), or a validation error in the merged plan (fix the skeleton or the fragment it comes from); `5` a precondition failed: no config, no plan, or no `batches.json` (`split_missing`: run `forge plan split <milestone> --json` first). Assemble is offline.
- Architecture errors from `forge plan validate` (exit `1`):
  - `architecture_missing` (mode `required`, no `<milestone>.md`), `architecture_invalid` (the file has errors that `forge arch validate` reports; the message starts with the original code) and `architecture_draft` (`status` is not `ready`): stop and point the user to `/forge-architect <milestone>` or `/forge-architect <milestone> --update`. Do not edit the architecture from this prompt.
  - `unknown_component` and `unknown_interface`: use an id that `<milestone>.md` or `overview.md` declares. If none fits, stop and point to `/forge-architect <milestone> --update`. To change an existing interface, the architecture must declare it in `<milestone>.md`.
  - `interface_unimplemented`, `interface_multiply_implemented` and `contract_without_interface`: give each interface of `<milestone>.md` exactly one contract task with `implements`.
  - `uses_without_dependency`: add a `depends_on` path to the task that implements the interface.
- Architecture warnings (exit `0`): `zone_outside_component` (a task touches a zone outside its component), `implementer_not_producer` (a contract task's `component` is not the interface's `producer`) and `architecture_fields_unchecked` (mode `optional`, no `<milestone>.md`, but tasks set `component`, `implements` or `uses`). Fix them, or explain in the PR body why you keep them.
- `forge plan apply`: `0` applied, or a dry run; `1` validation errors or a GitHub or git failure: report `messages`; `5` a precondition failed (CLI version outside `forge_version`, `gh` not logged in, a shallow clone, or the tier refusal above).
- `forge status`: `0` report produced; `5` no config, `gh` not logged in, shallow clone; `1` other error.
- On `5` for the tier: ask the user to run with an agent of the right tier (`--agent <id>`). Use `--force` only if the user tells you to.
- `2`, `3` and `4` are not expected from these calls. Report the output to the user.

## Guardrails
- Lead only. Never plan from a draft spec.
- Never invent a component or an interface. The architecture declares them, through `/forge-architect` and its own PR. Never plan from a `draft` architecture.
- Do not edit a spec's acceptance criteria to make a plan fit. Ask the user.
- Do not edit issues by hand. `forge plan apply` is the only writer of task issues.
- Do not use `size: L`, and do not touch a contract zone from a task that is not `type: contract` unless the user approves `allow_contract_change: true`.
- Never push to the default branch. Plans go through a PR.
- Do not run `forge plan apply` before the plan is merged, unless the user asks.
- Do not edit `batches.json` or commit anything under `.forge/local/`. Do not change the batches by hand; the bound is `planning.max_tasks_per_agent` in `.forge/config.yml`.

## Subagents
Writing a milestone plan runs in four phases. Subagents do the reading, the detail and the review. The skeleton, the fixes and every `forge` call stay with you (the main agent).

1. **Exploration**: if `forge-explorer` is available, spawn these subagents in parallel: one `forge-explorer` per milestone spec and one per component in `<milestone>.md` (with no architecture, one per code area). Give each its spec or component, `overview.md`, the ADRs and `zones.yml`, and ask for a compact summary. Otherwise read them yourself and keep notes short.
2. **Skeleton**: you write it and validate it (steps 5 and 6). Never delegate it.
3. **Detail**: after `forge plan split <milestone> --json` (step 7):
   - `agents: 1`: you write the detail fields yourself.
   - `agents` above 1 and `data.subagent.available` is `true`: spawn these subagents in parallel, in a single step: one `forge-planner` subagent per batch. If the tool caps the number of subagents running at once, spawn the rest as earlier ones finish. Wait for all of them, then run `forge plan assemble <milestone> --json`.
   - The brief for each planner: the goal (write `description`, `done_when` and `tier` for these tasks); the milestone and the batch `index`; the batch's `task_ids`; the skeleton path `plans/<milestone>.yml`; `docs/architecture/<milestone>.md` and `overview.md`; the specs and the acceptance criteria each task covers; the fragment path (`part_path`) and its format (top-level `milestone`, `batch`, `tasks`; each task only `id`, `description`, `done_when`, `tier`); the rules for the detail fields (testable `done_when` items, `tier` by difficulty, no Forge marker lines in the description); and the prohibition on writing any other file or any skeleton field.
   - If a planner reports a skeleton problem, fix the skeleton, then run steps 6 and 7 again.
4. **Review**: spawn these subagents in parallel: one `forge-reviewer` per batch (one for the whole plan when `agents: 1`). Give each the plan file, its `task_ids`, the specs, `<milestone>.md` and `overview.md`, and ask it to check its tasks against the specs, the architecture and the rest of the plan. Then check the whole plan yourself, fix `plans/<milestone>.yml` directly (the fragments are no longer used) and run step 8.

**Fallback**: if `data.subagent.available` is `false` (the reason is in `data.subagent.reason`), if your tool has no subagents, or if spawning fails, do the same phases yourself, one batch at a time. For the detail, write each fragment `part_path` yourself in the planner's format, with the same split, then run `forge plan assemble <milestone> --json`. Do the review yourself, batch by batch. Say in the final report that you used the sequential fallback, and why.

`<milestone> --update` and `roadmap` stay in one session: no split, no planners.

## Final report
Tell the user:
- the PR URL and its title;
- the waves (tasks per wave) and the AC coverage;
- the architecture used (`<milestone>.md` and its status, or "none, mode optional") and which task implements each interface;
- the validation result: errors fixed, warnings kept and why;
- for a milestone plan: the number of agents from `forge plan split`, the batches (task ids per batch), and whether you used the sequential fallback and why;
- new zones, or specs still in draft;
- what happens next: after `roadmap`, run `/forge-architect M1`; after a milestone plan, merge the PR, the workflow applies the plan, developers run `/forge-next`.
- if you stopped for the architecture: the reason, and the command to run (`/forge-architect <milestone>` or `/forge-architect <milestone> --update`).
