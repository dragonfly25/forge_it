---
name: plan
description: Turn a project spec into a roadmap, or a milestone's specs into a validated task plan, and open a PR
args: "roadmap <spec-file> | <milestone> [--update] [--agent <id>]"
roles: [lead]
---
## Goal
Produce plans that the team can run in parallel: demoable milestones, tasks sized S or M, contract tasks first, every task inside known zones. The plan arrives as a PR for the team to review. The CLI validates and applies it. You do the creative work.

## Steps
Check first that the user is a lead (`.forge/team.yml`). If not, say so and stop.

### `roadmap <spec-file>`
1. Read the spec file, `.forge/zones.yml`, `docs/decisions/` and the existing code.
2. Ask the user clarifying questions first. Wait for the answers. Do not guess scope.
3. Propose milestones as demoable vertical slices (each ends in something that can be shown). Show the list and wait for approval.
4. On a new branch `plan/roadmap`, write `plans/roadmap.yml` (id `M<n>`, title, goal, demo, lead, specs) and split the document into one spec per milestone feature under `specs/<milestone>/<feature>.md`, from `specs/_template.md`. Use `status: draft` while open questions remain. Acceptance criteria are `AC-n`, unique within a milestone.
5. Plan only the first milestone in detail. Plan milestone N+1 when milestone N is about 70% done.
6. Commit, push the branch and open a PR titled `plan: roadmap` with `git` and `gh`. Never push to the default branch.

### `<milestone>`
1. Get context: `forge status --json --milestone <milestone>`
2. Read the milestone's specs, `.forge/zones.yml`, `docs/decisions/` and the code.
3. If a spec has `status: draft` or open questions that are not RESOLVED, list the questions for the user and stop. Do not plan from an unready spec.
4. Write `plans/<milestone>.yml` contract-first:
   - tasks of `type: contract` come first and touch the contract zones;
   - implementation tasks depend on them with `depends_on`;
   - sizes are S or M only; split anything larger;
   - set `tier` by difficulty (`strong` for hard design or risky work, `light` for mechanical work);
   - every task has `covers`, `zones` (all defined in `zones.yml`), a description and `done_when` items that a test can prove;
   - keep tasks of the same wave in different zones.
   If a new zone is needed, add it to `.forge/zones.yml` in the same PR and say so.
5. Validate until there are no errors:
   `forge plan validate <milestone> --json`
   Fix each error. Address each warning, or explain why you keep it in the PR body.
6. Open a PR titled `plan: <milestone>` from the branch `plan/<milestone>`. In the body: waves, AC coverage matrix, warnings you accepted, new zones.

### `<milestone> --update`
1. Diff the specs against the existing `plans/<milestone>.yml`. Make the smallest set of changes.
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
- `forge plan validate`: `0` valid (read the warnings); `1` errors in the plan, specs or zones: fix them and run again; `5` a precondition failed (for example no `.forge/config.yml`, or your agent is below the tier that `min_tier_for.plan` requires). A CLI version outside `forge_version` is only a warning for validate. Validate is offline: it does not read GitHub.
- `forge plan apply`: `0` applied, or a dry run; `1` validation errors or a GitHub or git failure: report `messages`; `5` a precondition failed (CLI version outside `forge_version`, `gh` not logged in, a shallow clone, or the tier refusal above).
- `forge status`: `0` report produced; `5` no config, `gh` not logged in, shallow clone; `1` other error.
- On `5` for the tier: ask the user to run with an agent of the right tier (`--agent <id>`). Use `--force` only if the user tells you to.
- `2`, `3` and `4` are not expected from these calls. Report the output to the user.

## Guardrails
- Lead only. Never plan from a draft spec.
- Do not edit a spec's acceptance criteria to make a plan fit. Ask the user.
- Do not edit issues by hand. `forge plan apply` is the only writer of task issues.
- Do not use `size: L`, and do not touch a contract zone from a task that is not `type: contract` unless the user approves `allow_contract_change: true`.
- Never push to the default branch. Plans go through a PR.
- Do not run `forge plan apply` before the plan is merged, unless the user asks.

## Subagents
- If `forge-explorer` is available, delegate reading the code and ADRs to it and ask for a compact summary of the areas the milestone touches. Otherwise read them yourself.
- Writing the plan stays with you (the main agent).

## Final report
Tell the user:
- the PR URL and its title;
- the waves (tasks per wave) and the AC coverage;
- the validation result: errors fixed, warnings kept and why;
- new zones, or specs still in draft;
- what happens next: merge the PR, the workflow applies the plan, developers run `/forge-next`.
