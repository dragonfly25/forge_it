# Guide for the lead

This guide takes the lead from an empty repository to a released milestone. The [protocol](../protocol.md) describes the rules the CLI enforces. The [spec](../spec.md) is the source of truth, and an ADR wins where it differs. For the developer's side, see the [developer guide](developer.md).

Every command below runs in your agent as `/forge-<name>` in Claude Code or `$forge-<name>` in Codex. The agent calls the `forge` CLI for you. You can also run the CLI commands yourself.

## The workflow at a glance

| Step | Command | Result |
|---|---|---|
| 1. Set up | `forge init`, `/forge-configure`, `forge setup` | project files, config PR merged, your machine ready |
| 2. Specs | write `specs/<m>/<feature>.md` | specs with `status: ready` |
| 3. Roadmap | `/forge-plan roadmap <spec-file>` | PR `plan: roadmap` |
| 4. Architecture | `/forge-architect M1` | PR `arch: M1`, merged |
| 5. Task plan | `/forge-plan M1` | PR `plan: M1`, merged |
| 6. Apply | the `forge-plan-apply` workflow | GitHub milestone and one issue per task |
| 7. Build | developers run `/forge-next` | merged task PRs |
| 8. Release | `/forge-release M1` | release PR, tag, GitHub release |

## 1. Set up the project

Steps 1–3 set up the project. One lead does them, once, in a clone of the product repository. Step 4 is for every lead's machine.

1. Install Forge and log in with `gh auth login`. See the README.
2. `forge init` scaffolds the project files. It never overwrites a file without `--force` and never commits.
3. `/forge-configure` reads the codebase and proposes `.forge/config.yml`, `.forge/team.yml` and `.forge/zones.yml`. It writes them after you agree, runs `forge config validate` and opens a PR. Merge it. You can edit the files by hand and run `forge config validate` instead. See [ADR 0018](../decisions/0018-guided-project-configuration.md).
4. `forge setup` prepares your machine and your profile.

Run `/forge-configure --update` after a refactor that moves directories.

### Joining as a lead

A project can have more than one lead. A lead who joins a project that already uses Forge skips `forge init` and `/forge-configure`:

1. Install Forge and log in with `gh auth login`.
2. Clone the repository and run `forge setup` (step 4).
3. Check that `.forge/team.yml` lists you with `roles: [dev, lead]`. If it does not, add yourself in a PR.
4. Continue from [Specs](#2-specs), or from the step your milestone has reached. The milestones you lead have your login as `lead` in `plans/roadmap.yml`.

Forge does not lock planning to one lead. Every lead can configure, plan and release. `forge release` warns when you are neither the milestone's `lead` nor a member with the role `lead`. Escalations mention every lead.

Good zones make the later steps work. Cut zones along the lines you want parallel work to follow. Mark shared interfaces (API schemas, database migrations, shared types) as `contract: true`. Architecture interfaces must live in contract zones.

### Project config you will use later

```yaml
planning:
  architecture: required       # required | optional
models:
  min_tier_for: {reviewer: strong, plan: strong, architecture: strong}
```

`forge init` writes `required`. See section 4 for what `required` and `optional` mean, and what existing projects get.

## 2. Specs

A spec is `specs/<milestone>/<feature>.md` (copy `specs/_template.md`). Its frontmatter has `milestone`, `status: draft|ready` and `owner`. Its requirements are acceptance criteria `AC-1`, `AC-2` and so on. Every ID is unique within a milestone.

A spec is `ready` when every open question is marked `RESOLVED`, or there are none. `/forge-architect` and `/forge-plan` stop on a draft spec.

## 3. Roadmap

Write the whole project description in one Markdown file. Then run `/forge-plan roadmap <spec-file>`.

The agent asks questions, proposes milestones as demoable vertical slices, splits the document into one spec per milestone and opens the PR `plan: roadmap`. The roadmap is `plans/roadmap.yml`. Review it like code and merge it.

Plan milestone N+1 in detail when milestone N is about 70% done.

## 4. Milestone architecture

Run `/forge-architect M1` after the specs of M1 are `ready`. The agent reads the specs, `docs/architecture/overview.md`, the ADRs, `zones.yml` and the code. It proposes components, interfaces and decisions and waits for your go. Then it writes the files, validates them and opens the PR `arch: M1`. The PR is the review: read the design, change it, merge it.

The architecture comes before the plan, so the plan can no longer change the design silently. See [ADR 0019](../decisions/0019-milestone-architecture.md) for the full formats and checks.

### The files

| File | Content |
|---|---|
| `docs/architecture/overview.md` | The system as it is today, across all milestones included so far. |
| `docs/architecture/<M>.md` | What milestone `<M>` adds or changes. |
| `docs/decisions/NNNN-*.md` | Durable decisions. The `## Decisions` section of `<M>.md` links them. |

These files are in your project. They are not the same as the Forge repository's own `docs/architecture.md`.

### Frontmatter of `<M>.md`

```yaml
---
milestone: M1
status: draft            # draft | ready
specs: [specs/m1/auth.md]
components:
  - id: auth-service     # [a-z][a-z0-9-]*
    zones: [app/auth]    # zones from .forge/zones.yml
    summary: Issues and verifies session tokens
interfaces:
  - id: IF-auth-api      # IF-[a-z0-9-]+
    zone: contracts/api  # a zone with contract: true
    producer: auth-service
    consumers: [web-app]
    summary: REST endpoints for login and token refresh
---
```

| Key | Rule |
|---|---|
| `milestone` | Same as the file name. |
| `status` | `draft` or `ready`. |
| `specs` | At least one existing spec of this milestone. |
| `components` | `id`, `zones` (may be empty), `summary`. May be `[]`. |
| `interfaces` | `id`, `zone`, `producer`, `consumers` (may be empty), `summary`. May be `[]`. |

All keys are required. Unknown keys are errors. The body needs these sections: Context, Components, Interfaces, Data model, Decisions, Risks, Open questions.

`overview.md` has the keys `milestones`, `components` and `interfaces`, with the same schema. The architect adds `M1` to `milestones` and merges the new components and interfaces in the same PR.

### Statuses and open questions

- `draft`: the design is still moving. `forge plan validate` rejects a draft architecture.
- `ready`: the design is agreed. The agent sets it only with your explicit ok.

An open question is resolved when its text contains `RESOLVED`. A `ready` file has no unresolved question.

### Validate

```bash
forge arch validate M1
```

It is offline. It checks both files and shows every error and warning in one run. Exit `0` means no errors, `1` means errors in the files and `5` means a precondition failed (no config, or your agent is below `min_tier_for.architecture`; `--force` overrides the tier). The agent runs it for you until there are no errors.

Warnings: `overview_stale` (update `overview.md` in the same PR) and `component_unused` (a component with no zones that no interface connects).

### Required or optional

`planning.architecture` in `.forge/config.yml` sets the mode.

| Mode | Meaning |
|---|---|
| `required` | `plans/<M>.yml` needs `docs/architecture/<M>.md`. Without it, `forge plan validate` fails with `architecture_missing`. |
| `optional` | The plan is checked against the architecture only if `<M>.md` exists. Without the file, the architecture checks are skipped. |

`forge init` writes `required`. **Existing projects**: a config without the `planning` block or the `architecture` key behaves as `optional`. Their plans and the `forge-plan-apply` workflow keep working with no edit. To adopt the architecture, add `planning: {architecture: required}` and run `/forge-architect` for the next milestone. Switching to `required` with plans already applied gives `architecture_missing` on the next plan change, until the file exists.

A project that upgrades gets the new templates by running `forge init` again. It keeps unchanged files and refuses to overwrite changed ones without `--force`. The project also needs a Forge version that knows the `planning` key (see the CHANGELOG).

### Update an architecture

Run `/forge-architect M1 --update` when the specs or the code changed. The agent edits only what moved, and reports which plan tasks the change affects. Then run `/forge-plan M1 --update` if the plan must follow.

## 5. Task plan

Merge the `arch: M1` PR first. Then run `/forge-plan M1`. The agent reads the specs, the architecture, the zones and the code, writes `plans/M1.yml` contract-first, runs `forge plan validate M1` until there are no errors and opens the PR `plan: M1`.

The plan follows the architecture. The agent never adds a component or an interface. If the architecture is missing, in draft or too thin, it stops and tells you to run `/forge-architect M1` (or `--update`).

### Plan fields from the architecture

| Field | Form | Rule |
|---|---|---|
| `component` | a component id | Required on every task when the architecture is checked. |
| `implements` | list of `IF-…` ids | Only on `type: contract` tasks. Required on them when checked. Only interfaces declared in `<M>.md`. |
| `uses` | list of `IF-…` ids | Optional. Interfaces from `<M>.md` or `overview.md`. |

The architecture is checked when the mode is `required`, or when `<M>.md` exists. These fields are plan-only. `plan apply` does not write them to issues.

Each interface in `<M>.md` needs exactly one contract task that implements it. To change an existing interface (one only in `overview.md`), declare it in `<M>.md` first.

### What `forge plan validate` adds

Errors stop the plan (exit 1). Warnings do not.

| Code | Level | Meaning |
|---|---|---|
| `architecture_missing` | error | Mode `required` and no `<M>.md`. |
| `architecture_invalid` | error | `arch validate` reports an error. The message starts with the original code. |
| `architecture_draft` | error | `<M>.md` is not `status: ready`. |
| `unknown_component` | error | `component` is in neither `<M>.md` nor `overview.md`. |
| `unknown_interface` | error | `implements` names an interface not in `<M>.md`, or `uses` names one in neither file. |
| `interface_unimplemented` | error | An interface of `<M>.md` has no contract task. |
| `interface_multiply_implemented` | error | Two or more tasks implement the same interface. |
| `contract_without_interface` | error | A `type: contract` task has no `implements`. |
| `uses_without_dependency` | error | A task uses an interface of `<M>.md` without depending on the task that implements it. |
| `zone_outside_component` | warning | A task touches a zone outside its component and the zones of the interfaces it implements. |
| `implementer_not_producer` | warning | A contract task's component is not the interface's `producer`. |
| `architecture_fields_unchecked` | warning | Mode `optional`, no file, and tasks still set the architecture fields. |

With mode `optional` and no file, validate prints one info line: `no architecture for <M> (planning.architecture: optional)`.

### Multi-agent planning

A large milestone fills one agent's context, and the last tasks get worse. When a milestone is large, `/forge-plan <M>` and `/forge-architect <M>` share the detail work between several subagents that run in parallel. You run the same commands. The agent decides from the size. See [ADR 0020](../decisions/0020-multi-agent-planning.md) for the formats and the decisions.

**When it starts.** Two keys in `.forge/config.yml` set the thresholds:

```yaml
planning:
  max_tasks_per_agent: 10        # integer >= 1, default 10
  max_components_per_agent: 3    # integer >= 1, default 3
```

- **Plan**: only tasks are counted. Up to `max_tasks_per_agent` tasks, one session writes the detail. Above it, `ceil(N / max)` agents each get a balanced group: 12 tasks → 6 + 6, 21 → 7 + 7 + 7, 25 → 9 + 8 + 8.
- **Architecture**: the same formula, with components as the unit and `max_components_per_agent` as the threshold.
- A project without a `planning` block uses the defaults. The bound comes only from the config, so a change goes through a PR. There is no command-line flag.
- Tasks are grouped by component, then by wave, so a component's tasks stay together and contracts come before the work that depends on them.

**The four phases of a plan.**

1. **Exploration**: in parallel, one `forge-explorer` per spec and one per component involved. Each returns a short summary.
2. **Skeleton**: the main session writes `plans/<M>.yml` with the skeleton fields only (ids, titles, types, specs, `covers`, dependencies, zones, sizes, `component`, `implements`, `uses`). It runs `forge plan validate <M> --skeleton` until there are no errors.
3. **Detail**: `forge plan split <M>` makes the groups. One `forge-planner` per group writes `description`, `done_when` and `tier` for its tasks, into its own fragment. Then `forge plan assemble <M>` merges the fragments into `plans/<M>.yml`.
4. **Review**: in parallel, one `forge-reviewer` per group checks its tasks against the specs and the architecture. The main session checks the whole plan, fixes `plans/<M>.yml` directly and runs `forge plan validate <M>` until there are no errors.

**The four phases of an architecture.**

1. **Exploration**: in parallel, one `forge-explorer` per spec and one per code area the specs touch.
2. **Structure**: the main session proposes the frontmatter (components, interfaces, key decisions) and waits for your go. Then it writes the frontmatter of `<M>.md`.
3. **Detail**: `forge arch split <M>` makes the groups of components. One `forge-arch-writer` per group writes the component and interface sections for its group, into its own fragment. The main session integrates the fragments into the body of `<M>.md`, writes the other sections and updates `overview.md`. There is no `arch assemble`: the body is free text under fixed sections, so a person or the main session has to make it one coherent document.
4. **Review**: two `forge-reviewer` subagents in parallel. One checks that the specs are covered. The other checks consistency with the code, the ADRs and `overview.md`. The main session integrates the findings and runs `forge arch validate <M>` until there are no errors.

Your go, the `status: ready` rule and the PRs `arch: <M>` and `plan: <M>` do not change. `--update` stays single-session.

**Commands.** All are offline and print the envelope with `--json`. Exit codes: `0` done, `1` errors in the content, `5` a precondition failed.

| Command | What it does | Main errors |
|---|---|---|
| `forge plan validate <M> --skeleton` | Same checks as `forge plan validate`, except the `missing_field` errors for `description`, `done_when` and `tier`. Fields that are present are still checked. | Exit 1: any other plan error. Exit 5: as for `plan validate` (`min_tier_for.plan`; `--force` overrides). |
| `forge plan split <M>` | Checks the skeleton, groups the tasks, writes `batches.json` and deletes old fragments. Reports whether `forge-planner` is available. | Exit 1: skeleton errors, `plan_empty`. Exit 5: no config, `plan_missing`, `not_ignored` (`.forge/local/` is not in `.gitignore`). |
| `forge plan assemble <M>` | Checks the fragments, merges them, validates the merged plan and rewrites `plans/<M>.yml`. Writes nothing on any error. | Exit 1: `skeleton_changed`, `fragment_missing`, `fragment_invalid`, `fragment_mismatch`, `unassigned_task`, `missing_task`, `duplicate_id`, `skeleton_field`, `unknown_field`, `missing_field`, `invalid_field`, and any error of the merged plan. Exit 5: no config, no plan, `split_missing`. |
| `forge arch split <M>` | Checks the frontmatter, groups the components, assigns each interface to its producer's group and deletes old fragments. | Exit 1: frontmatter errors (a missing section is allowed at this point). Exit 5: no config, `architecture_missing`, `not_ignored`. |

Every fragment error names the fragment file, the task and the field. If `assemble` says `skeleton_changed`, you or the agent edited the skeleton after `split`. Run `forge plan split <M>` again and redo the detail.

**Working files.** They are git-ignored, in your worktree:

| Path | Content |
|---|---|
| `.forge/local/plan/<M>/batches.json` | The groups, the hash of the skeleton and the fragment paths. |
| `.forge/local/plan/<M>/part-<k>.yml` | The fragment of group `k`: `description`, `done_when` and `tier` per task. |
| `.forge/local/arch/<M>/part-<k>.md` | The fragment of group `k`: component and interface sections, and notes for the main session. |

Each writer writes only its own file, so parallel subagents never touch the same file. `split` and `arch split` refuse to run if `.forge/local/` is not ignored by git.

**Without subagents.** If the tool has no subagents (the generic adapter, local models), or the agent cannot start them, the main session does the same phases one group at a time. It uses the same `split` groups, writes `part-1.yml`, `part-2.yml` and so on itself, and then runs `assemble`. The final report says that the sequential fallback ran, and why. `split` and `arch split` report whether the subagent is available. Existing projects get `planner.md` and `arch-writer.md` by running `forge init` again and then `forge adapters generate --local`. Until then the agent works sequentially.

**`assemble` rewrites `plans/<M>.yml` without comments.** It builds the whole file from data, in a fixed key order, so YAML comments in the skeleton are lost and the formatting changes. The same input always gives the same bytes. Put your reasons in the PR body, not in the file. The first plan PR shows the rewritten form.

To raise the tier of these subagents, use `models.min_tier_for.planner` or `models.min_tier_for.arch-writer`. The keys `plan` and `architecture` apply to the session that runs the validate command, not to them.

## 6. Apply

Merging `plans/M1.yml` to the default branch runs the `forge-plan-apply` workflow. It runs `forge plan validate` and `forge plan apply`. It creates the GitHub milestone and one issue per task. You can run `forge plan apply M1 --dry-run` first, or `forge plan apply M1` by hand.

Apply is idempotent. Closed issues are never changed. A started task only gets title, description and `done_when` updates. For anything else, apply posts a warning comment to the assignee.

The workflow runs only when `plans/*.yml` changes. A later change to the architecture does not trigger it. A broken link between plan and architecture shows up on the next plan PR or on `/forge-plan M1 --update`.

Proposals from developers appear as issues labeled `status:proposed`. Accept one by adding a task with `proposal: <issue number>`, or close it with a comment.

## 7. Release

When every task of the milestone is closed, run `/forge-release M1`. It runs `forge release M1 --version X.Y.Z` twice:

1. The first run opens the PR `chore(release): vX.Y.Z` with a changelog section built from the merged task PRs.
2. After you merge it, the second run tags the commit, creates the GitHub release and closes the milestone.

Exit 5 means open tasks, a missing `--version` or a version already released for another milestone. Treat the lead warning as a stop.

## 8. Reviews

Review the `arch:`, `plan:` and `chore(release):` PRs like code. A task PR gets a reviewer requested by `forge ship`. Use `/forge-review <pr>` to review one. Then merge it when it is approved and CI is green. The merge closes the issue and unblocks dependent tasks.

## Where the architecture boundary is enforced

The architecture is not a runtime sandbox. It is enforced in three places, each with limits.

| Where | What it checks | Limit |
|---|---|---|
| `forge plan validate` | Tasks name known components and interfaces. Contract tasks implement interfaces. Dependencies exist. Warns when a task touches a zone outside its component (`zone_outside_component`). | Reads the plan, not the code. A zone warning is not an error. |
| `forge ship` | Lists changed files outside the task's zones in the PR block. With `unzoned: error` in `zones.yml`, files in no zone make ship exit 2. `unzoned: warn` (default) only lists them. | Checks zones, not components. A file in another component's zone is listed as out-of-zone but not refused. |
| The reviewer | `/forge-review` and the `reviewer` subagent read the task's entry in `plans/<M>.yml` and `docs/architecture/` and check that the change stays inside the task's component and respects the interfaces it implements and uses. | It is a judgment, made by an agent and a human. |

What is not enforced:
- no CI job runs `forge arch validate` (the workflows are unchanged);
- the architecture is not re-checked when it changes after the plan was applied;
- no hook blocks a developer from editing a file outside the component.

## Where things live

| What | Where | Committed |
|---|---|---|
| Project settings, team, zones, rules | `.forge/config.yml`, `team.yml`, `zones.yml`, `rules.md` | yes |
| Command prompts and subagent definitions | `.forge/prompts/`, `.forge/agents/` | yes |
| Tool files rendered from them | `AGENTS.md`, `CLAUDE.md`, `.claude/skills/forge-*/`, `.agents/skills/forge-*/` | yes |
| Specs, roadmap and task plans | `specs/`, `plans/roadmap.yml`, `plans/<milestone>.yml` | yes |
| Architecture | `docs/architecture/overview.md`, `docs/architecture/<milestone>.md` | yes |
| Decisions | `docs/decisions/` | yes |
| Workflows | `.github/workflows/forge-*.yml` | yes |
| A developer's profile (tiers to models, agent labels, no credentials) | `~/.config/forge/profile.yml` (Windows: `%APPDATA%\forge\profile.yml`) | no |
| Rendered subagents with the developer's models | `.claude/agents/forge-*.md`, `.codex/agents/forge-*.toml` | no (gitignored) |
| Local task state, and the planning fragments (`plan/<M>/`, `arch/<M>/`) | `<worktree>/.forge/local/` | no (gitignored) |
| Worktrees | `<parent-of-repo>/<repo>-worktrees/task-<n>/` | no |

Task state itself lives on GitHub: issues, milestones, labels, branches `task/<n>` and PRs.

## Reference for setup details

- **Adapters.** `forge init` writes the generic adapter plus Claude and Codex files. Choose tools with `forge init --adapters generic,claude,codex`. After you edit a prompt in `.forge/prompts/`, run `forge adapters generate` and commit the result. CI runs `forge adapters generate --check`.
- **Generated docs.** `docs/forge/commands.md` is generated. After editing a prompt, run `forge docs` with the Forge version pinned in `.github/workflows/forge-ci.yml`, and commit the result. CI runs `forge docs --check` with that version, and a file generated by another version can differ.
- **Version pin.** `forge_version` in `.forge/config.yml` is the specifier the CLI must satisfy. Some commands warn on a mismatch and others refuse with exit 5. The list is in [protocol section 13](../protocol.md#13-cross-cutting-rules).
- **Docs for your team.** The project gets its own `docs/forge/onboarding.md`, `roles.md` and `faq.md` from `forge init`.

## Related

- [Protocol](../protocol.md), section 9 for planning.
- [ADR 0013](../decisions/0013-planning.md) for plan validate and apply.
- [ADR 0019](../decisions/0019-milestone-architecture.md) for the architecture.
- [ADR 0020](../decisions/0020-multi-agent-planning.md) for multi-agent planning.
- [ADR 0018](../decisions/0018-guided-project-configuration.md) for `/forge-configure`.
