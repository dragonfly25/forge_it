---
name: architect
description: Write or update a milestone's architecture and the system overview, validate them and open a PR
args: "<milestone> [--update]"
roles: [lead]
---
## Goal
Give the milestone a reviewed design before anyone plans tasks. You write `docs/architecture/<milestone>.md`: the components the milestone adds or changes, the interfaces between them, the data model, the decisions, the risks. You also update `docs/architecture/overview.md`, the map of the whole system. The CLI checks the structure. The result arrives as a PR titled `arch: <milestone>`. After it merges, `/forge-plan <milestone>` turns the design into tasks.

With `--update`, the architecture already exists. Change only what changed in the specs or the code.

## Steps
Check first that the user is a lead (`.forge/team.yml`). If not, say so and stop.

1. Read the inputs:
   - the milestone's specs under `specs/` (their frontmatter `milestone` is the milestone);
   - `docs/architecture/overview.md` and, with `--update`, the existing `docs/architecture/<milestone>.md`;
   - the ADRs in `docs/decisions/`;
   - `.forge/zones.yml`, including which zones are `contract: true`;
   - the code in the areas the specs touch.
2. If a spec has `status: draft`, or open questions that are not marked RESOLVED, list them for the user and stop. Do not design from an unready spec.
3. Propose the design and wait for the user's explicit go. Show:
   - the components: `id` (lowercase letters, digits, `-`), the zones each one owns, one line on what it does;
   - the interfaces: `id` (`IF-` then lowercase letters, digits, `-`), the contract zone it lives in, its `producer`, its `consumers`, one line on what it carries;
   - the data model changes;
   - the decisions worth an ADR;
   - the new zones, if any;
   - the risks and the open questions.
   Reuse components and interfaces from `overview.md` when they fit. Declare an existing interface in `<milestone>.md` only if the milestone changes it. If the user changes the proposal, show the new version and wait again.
4. On a new branch `arch/<milestone>`, write the structure:
   - the frontmatter of `docs/architecture/<milestone>.md`, from `docs/architecture/_template.md`: the components, the interfaces and the milestone's specs in `specs`. Keep `status: draft`. Keep the template's section headings; you fill them in step 5.
   - new zones in `.forge/zones.yml`, if the design needs them. An interface's zone must be a zone with `contract: true`.
5. Write the detail. Split the components into batches:
   `forge arch split <milestone> --json`
   It refuses on frontmatter errors (a missing section is fine at this point) and returns `data.agents`, `data.batches` (each with `index`, `component_ids`, `interface_ids` and `part_path`), `data.unassigned_interfaces` and `data.subagent`.
   - `agents` 0 or 1: single session. Write the body of `<milestone>.md` yourself. `agents: 0` means there is no component to hand out (no components, or only components declared in `overview.md`).
   - `agents` above 1: each batch's sections go into its fragment `part_path`, written by one `forge-arch-writer` per batch or by you (see Subagents). Then integrate the fragments yourself into one coherent body.
   In both cases, finish the files:
   - `docs/architecture/<milestone>.md`: fill every section: Context, Components, Interfaces, Data model, Decisions, Risks, Open questions. Write the interfaces in `data.unassigned_interfaces` yourself.
   - `docs/architecture/overview.md`: add the milestone to `milestones`, and merge the milestone's components and interfaces into `components` and `interfaces`. Update an item that the milestone changes; do not add it twice. Update the body to match.
   - a new ADR in `docs/decisions/` (from `_template.md`) for each durable decision. Link it from the `## Decisions` section.
6. Review the architecture (see Subagents), integrate the findings, then validate, and repeat until there are no errors:
   `forge arch validate <milestone> --json`
   Fix each error in `data.errors`: each one names the file, the field and a hint. Then go through `data.warnings` with the user:
   - `overview_stale`: update `overview.md` in the same PR;
   - `component_unused`: give the component zones, connect it to an interface, or remove it.
7. Ask the user whether the architecture is ready. Set `status: ready` only on their explicit ok. A `ready` file must have no unresolved open question. Validate again after the change.
8. Commit, push the branch `arch/<milestone>` and open a PR titled `arch: <milestone>` with `git` and `gh`. In the body: the components, the interfaces with producer and consumers, the new ADRs and zones, the warnings you accepted, the status, and how many agents wrote the detail. Never push to the default branch.

### `<milestone> --update`
An update stays in one session: no `split` and no `forge-arch-writer`.
1. Read the existing `<milestone>.md` and `plans/<milestone>.yml` if it exists. Find what changed in the specs or the code since the architecture was written.
2. Propose the smallest set of changes and wait for the go. Do not rewrite sections that still hold.
3. Follow steps 4 to 8 on the same branch name `arch/<milestone>`, writing the changes yourself instead of splitting.
4. List which tasks in `plans/<milestone>.yml` the change affects: tasks whose `component` was renamed or removed, and tasks whose `implements` or `uses` name a changed or removed interface. The lead then runs `/forge-plan <milestone> --update` after this PR merges.

## Exit codes
- `forge arch validate`:
  - `0`: no errors. Warnings may remain. Read them (step 6).
  - `1`: errors in `<milestone>.md` or `overview.md` (including a missing `<milestone>.md`), or an invalid `.forge/zones.yml`. Fix them and run the call again.
  - `5`: a precondition failed: no `.forge/config.yml`, or your agent is below the tier that `models.min_tier_for.architecture` requires. For the tier, ask the user to run with an agent of the right tier (`--agent <id>`). Use `--force` only if the user tells you to.
  - A CLI version outside `forge_version` is only a warning. The command is offline: it reads no GitHub data and runs no git.
- `forge arch split`:
  - `0`: batches computed; the fragment directory `.forge/local/arch/<milestone>/` is ready. Read `data.removed_parts`: old fragments it deleted.
  - `1`: errors in the frontmatter of `<milestone>.md` or `overview.md` (every error except `missing_section`), or an invalid `.forge/zones.yml` or milestone argument. Fix the structure and run it again.
  - `5`: a precondition failed: no `.forge/config.yml`, no `<milestone>.md` (`architecture_missing`: write the frontmatter first, step 4), or `.forge/local/` is not ignored by git (`not_ignored`: add it to `.gitignore`; fragments must never be committed).
  - It runs no tier check and is offline.
- `2`, `3` and `4` are not expected from these calls. Report the output to the user.
- `git` and `gh` failures when you push or open the PR (for example `gh` is not logged in): report the message and the fix. Do not work around it.

## Guardrails
- Lead only. Never design from a draft spec or from unresolved open questions.
- Do not write any file before the user gives an explicit go. Do not set `status: ready` without the user's explicit ok.
- Do not edit a spec's acceptance criteria to make the design fit. Ask the user.
- Do not write tasks or edit `plans/*.yml`. That is `/forge-plan`.
- Do not remove a component or an interface from `overview.md` that other milestones still use without asking the user first.
- Every interface lives in a contract zone, has exactly one `producer`, and names its `consumers`. Every component and interface named must be declared in `<milestone>.md` or `overview.md`.
- Do not add keys that the CLI rejects. Unknown keys are errors.
- Never write credentials, tokens or keys in any file.
- Do not commit anything under `.forge/local/`. The fragments are working files; only `<milestone>.md`, `overview.md`, the ADRs and `zones.yml` go in the PR.
- Never push to the default branch. The architecture goes through a PR.

## Subagents
Writing a milestone architecture runs in four phases. Subagents do the reading, the detail sections and the review. The structure, the integration, the user's go and every `forge` call stay with you (the main agent).

1. **Exploration** (step 1): if `forge-explorer` is available, spawn these subagents in parallel: one `forge-explorer` per spec and one per code area the specs touch. Give each its spec or area, `overview.md`, the ADRs and `zones.yml`, and ask for a compact summary of the area, the existing components and interfaces there, and contradictions between specs and code. Otherwise read them yourself and keep notes short.
2. **Structure** (steps 3 and 4): you propose the components and interfaces, wait for the user's go, and write the frontmatter. Never delegate it.
3. **Detail** (step 5): after `forge arch split <milestone> --json`:
   - `agents` 0 or 1: you write the body yourself.
   - `agents` above 1 and `data.subagent.available` is `true`: spawn these subagents in parallel, in a single step: one `forge-arch-writer` subagent per batch. If the tool caps the number of subagents running at once, spawn the rest as earlier ones finish. Wait for all of them.
   - The brief for each writer: the goal (write the sections for these components and interfaces); the milestone and the batch `index`; the batch's `component_ids` and `interface_ids`; `<milestone>.md` (its frontmatter is fixed) and `overview.md`; the specs, the ADRs and `zones.yml`; the code areas; the fragment path (`part_path`) and its sections (`## Components` with one `### <component-id>` each, `## Interfaces` with one `### <IF-id>` each, `## Notes for the main session`); and the prohibition on writing any other file or adding or renaming a component or an interface.
   - Then integrate the fragments yourself: one coherent `## Components` and `## Interfaces` in `<milestone>.md`, the other sections from the writers' notes, the `unassigned_interfaces`, the ADRs and `overview.md`. If a writer reports a structural problem, propose the fix to the user and wait for the go before you change the frontmatter.
4. **Review** (step 6): spawn these subagents in parallel: two `forge-reviewer` subagents. Give both `<milestone>.md`, `overview.md`, the specs, the ADRs and `zones.yml`. Ask one to check spec coverage: every spec and acceptance criterion has a component and, where needed, an interface. Ask the other to check consistency with the code, the ADRs and `overview.md`. Integrate the findings, then run `forge arch validate <milestone> --json`.

**Fallback**: if `data.subagent.available` is `false` (the reason is in `data.subagent.reason`), if your tool has no subagents, or if spawning fails, do the same phases yourself. Write the body one batch at a time, using the same `forge arch split` batches, then integrate and review it yourself. Say in the final report that you used the sequential fallback, and why.

## Final report
Tell the user:
- the PR URL and its title, or why you could not open one;
- the components and interfaces the milestone adds or changes;
- the ADRs and zones you added;
- the validation result: errors fixed, warnings kept and why;
- the number of agents from `forge arch split`, the batches (component ids per batch), and whether you used the sequential fallback and why;
- the `status` of `<milestone>.md` (`draft` or `ready`), and the open questions left;
- with `--update`: the plan tasks the change affects;
- what happens next: merge the PR, then run `/forge-plan <milestone>` (or `/forge-plan <milestone> --update`).
