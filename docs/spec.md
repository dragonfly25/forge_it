# Forge — Specification (v0)

Forge is a framework that lets several people, each using their own AI coding agents (Claude Code, OpenAI Codex, local models, …), work in parallel on the same GitHub-hosted project without stepping on each other. It handles environment setup, task assignment, synchronization, conflict prevention and resolution, pull requests, reviews, planning and releases.

This document is the source of truth for building Forge. When something here is ambiguous, record the decision in `docs/decisions/` (ADR format) rather than guessing silently.

---

## 1. Goals and non-goals

### Goals
- Multiple people × multiple agents × multiple AI providers on one repository, in parallel.
- Provider-agnostic: any agent that can run shell commands and read files can participate.
- Conflicts are **prevented** by design (planning with zones, atomic claims, contract-first) and the residual ones are **handled** by a guided `sync`.
- Token budgets spread across accounts: work can be handed off between accounts, providers or people mid-task.
- Zero central server: GitHub is the coordination bus and the only source of truth.
- Simple for developers: a handful of commands, sensible defaults.

### Non-goals (v0)
- No hosted service, database or web dashboard.
- No direct LLM API calls from the CLI. The CLI is deterministic; reasoning happens in the user's agent.
- No autonomous orchestrator running headless agents (planned for v2, see §19).
- No support for hosts other than GitHub.

---

## 2. Principles

1. **GitHub is the source of truth.** Task state lives in Issues, Milestones, branches and PRs. Rules, specs and plans live in the repository.
2. **The CLI executes, the agent reasons.** Anything that must be correct every time (claims, locks, branch operations, state, validation) is code in the `forge` CLI. Prompts tell the agent *when* to call the CLI and *how* to do the creative work (implement, resolve a conflict, review, decompose a spec).
3. **Thin adapters.** Command prompts are written once, provider-neutral, in `.forge/prompts/`. Adapters render them into each tool's native format.
4. **Everything reviewable.** Plans arrive as PRs; conflict resolutions are listed in the PR; handoffs are issue comments.
5. **Machine-readable first.** Every CLI command supports `--json` so agents can parse results reliably.

---

## 3. Glossary

| Term | Meaning |
|---|---|
| Task | A unit of work = one GitHub issue labeled `forge:task`, with a Forge metadata block |
| Milestone | A demoable vertical slice of the product = a GitHub milestone (`M0`, `M1`, …) |
| Zone | A named area of the codebase (set of path globs) declared in `.forge/zones.yml` |
| Contract zone | A zone holding interfaces others depend on (API schemas, DB schema, shared types) |
| Claim | Taking ownership of a task, made atomic through a compare-and-swap push on the task branch |
| Lease | How long a claim stays valid without activity (default 24h) |
| Wave | A set of tasks that can run in parallel (same depth in the dependency graph) |
| Lead | The milestone lead: plans, approves the plan, triages proposals, decides escalations. Rotates per milestone |
| Tier | The model strength a task needs: `strong`, `standard`, `light` |
| Handoff | A structured note that lets another person/account/provider resume a task |
| Subagent | A role-named helper agent (explorer, test-writer, implementer, reviewer) defined once in `.forge/agents/`, rendered per machine (§12.1) |
| Profile | Per-machine file mapping abstract tiers/effort to concrete models for each of the user's agents (§6.4, §6.5) |
| Preset | Default tier → model mapping per tool, shipped with Forge and proposed during setup (§6.5) |

---

## 4. Architecture

```
People + agents (Claude Code, Codex, local models…)
        │  slash commands / natural language
        ▼
Adapters (rendered from .forge/prompts/*.md)
        │  agent runs shell commands
        ▼
forge CLI (Python, deterministic)
        │  git + gh
        ▼
GitHub: issues, milestones, labels, branches, PRs, Actions
```

- The CLI talks to git via a `Git` wrapper (subprocess) and to GitHub via a `GitHubClient` protocol whose production implementation shells out to the `gh` CLI (`gh api`, `gh api graphql`). This reuses the user's existing `gh auth` and avoids handling tokens. In GitHub Actions, `GH_TOKEN` is used.
- No component other than the CLI writes Forge state to GitHub.

---

## 5. Where things live

### 5.1 Framework repository (`dragonfly25/forge_it`)

```
forge_it/
├─ pyproject.toml
├─ README.md
├─ CLAUDE.md                       # rules for building forge itself
├─ .claude/agents/                 # subagents used to build forge itself (ADR 0002)
├─ install.sh                      # bootstrap installer (macOS/Linux)
├─ docs/
│  ├─ spec.md                      # this file
│  ├─ build-plan.md
│  ├─ protocol.md                  # user-facing protocol description (generated/maintained)
│  ├─ architecture.md
│  ├─ adapters.md
│  └─ decisions/                   # ADRs for the framework itself
├─ src/forge/
│  ├─ __init__.py, __main__.py, cli.py
│  ├─ commands/                    # one module per CLI command group
│  ├─ core/                        # config, team, zones, profile, models, output, git, github,
│  │                               # tasks, graph, lease, claim, sync, resolvers, plan, spec, report
│  ├─ adapters/                    # claude.py, generic.py, codex.py
│  ├─ presets/                     # tier → model presets per tool (§6.5)
│  └─ templates/project/           # files copied by `forge init` (package data)
├─ tests/
└─ CHANGELOG.md
```

### 5.2 Project level (committed in the product repository, created by `forge init`)

```
.forge/
├─ config.yml                      # project settings (§6.1)
├─ team.yml                        # who works on the project (§6.2)
├─ zones.yml                       # codebase zones (§6.3)
├─ rules.md                        # rules every agent must follow
├─ prompts/                        # 10 provider-neutral command prompts (§12)
└─ agents/                         # provider-neutral subagent definitions (§12.1)
AGENTS.md                          # entry point for Codex, Cursor, Gemini CLI, opencode, local agents
CLAUDE.md                          # thin: points to AGENTS.md (+ Claude specifics)
.claude/commands/*.md              # generated by the Claude adapter (committed)
specs/_template.md                 # spec template (§9.1)
plans/roadmap.yml                  # milestones and leads (§9.2)
plans/<milestone>.yml              # task plans (§9.3)
docs/decisions/_template.md        # ADR template
docs/forge/                        # onboarding.md, commands.md, roles.md, faq.md
.github/workflows/forge-ci.yml           # checks on PRs
.github/workflows/forge-plan-apply.yml   # applies plans merged to main
.github/workflows/forge-radar.yml        # conflict radar + dependency refresh
.github/ISSUE_TEMPLATE/forge-proposal.yml
.github/pull_request_template.md
.gitattributes                     # e.g. merge=union for CHANGELOG
.gitignore                         # adds .forge/local/, .env.local, .claude/agents/forge-*.md
.env.example
```

### 5.3 Machine level (never committed)

| Item | Location | Created by |
|---|---|---|
| forge CLI | `uv tool` / `pipx` environment | `install.sh` |
| Personal profile | `$XDG_CONFIG_HOME/forge/profile.yml` (default `~/.config/forge/profile.yml`) | `forge setup` |
| Tool logins | each tool's own store (`gh auth login`, `claude`, `codex login`) | the user |
| User-level adapter files | e.g. Codex user prompts/skills, if the tool requires user-level files | `forge setup` |
| Rendered subagents | e.g. `<repo>/.claude/agents/forge-*.md` (gitignored), with concrete models from the profile | `forge setup`, `forge adapters generate --local` |
| Local task state | `<worktree>/.forge/local/` (gitignored) | `forge next`, `forge sync` |
| Secrets | `<repo>/.env.local` (gitignored) | the user / `forge setup` copies `.env.example` |
| Worktrees | `<parent-of-repo>/<repo>-worktrees/task-<n>/` (configurable) | `forge next` |
| git settings | repo-local `git config rerere.enabled true`, `rerere.autoupdate true` | `forge setup` |

**Credentials are never stored by Forge.** The profile holds only labels (e.g. `claude-pro-1`) used for traceability.

---

## 6. Configuration files

All YAML files are validated on load; errors must name the file, the key and the fix.

### 6.1 `.forge/config.yml`

```yaml
forge_version: ">=0.1,<0.2"        # PEP 440 specifier the CLI must satisfy
project:
  name: ai-cms
  default_branch: main
  merge_method: squash             # squash recommended: claim commits disappear
github:
  repo: <ORG>/<REPO>
  project_number: null             # GitHub Projects v2 board (optional, §15)
lease_hours: 24
claim:
  max_attempts: 5
worktrees:
  enabled: true
setup:
  commands:                        # run by `forge setup` after machine checks
    - cp -n .env.example .env.local || true
    - docker compose pull
checks:                            # run by `forge sync --continue` and `forge ship`
  lint: "npm run lint"
  typecheck: "npm run typecheck"
  test: "npm test"
resolvers:                         # deterministic conflict resolvers (§10.4)
  - glob: "package-lock.json"
    strategy: regenerate
    command: "npm install --package-lock-only"
  - glob: "CHANGELOG.md"
    strategy: union
review:
  cross_provider: true             # prefer a reviewer whose agent uses a different provider
  required_approvals: 1
models:                            # project constraints on model choices (decided by the lead)
  recommended: {strong: opus, standard: sonnet}   # shown as suggestions during setup
  min_tier_for: {reviewer: strong, plan: strong}  # subagents/commands that must not run below a tier
  allowed_providers: [anthropic, openai, local]   # warn when a profile uses others
```

### 6.2 `.forge/team.yml`

```yaml
members:
  - github: vincenzo-s
    name: Vincenzo
    roles: [dev, lead]
    providers: [anthropic, openai]
    timezone: Europe/Tirane
    capacity_hours_week: 20        # optional
```

`github` is the identity key used everywhere (assignee, claim trailer, reviews).

### 6.3 `.forge/zones.yml`

```yaml
zones:
  editor/canvas:
    paths: ["packages/editor/src/canvas/**"]
  editor/blocks:
    paths: ["packages/editor/src/blocks/**"]
  contracts/api:
    paths: ["contracts/openapi/**"]
    contract: true
  db/schema:
    paths: ["packages/wp-plugin/migrations/**"]
    contract: true
conflicts:                         # optional: zones that must never run in parallel
  - [editor/blocks, editor/inspector]
unzoned: warn                      # warn | error when a changed file matches no zone
```

Two tasks **overlap** if they share a zone name, or a pair of their zones is listed under `conflicts`, or (computed) their zones' path globs intersect.

### 6.4 `~/.config/forge/profile.yml`

```yaml
github: vincenzo-s
agents:
  - id: claude-pro-1               # free-form label, appears in claims and handoffs
    tool: claude-code
    providers: [anthropic]
    tiers: {strong: opus, standard: sonnet, light: haiku}
    effort: {low: low, medium: medium, high: high}   # abstract → tool-specific value
  - id: openai-team
    tool: codex
    providers: [openai]
    tiers: {standard: gpt-5}
  - id: vincenzo-hybrid            # orchestrator + local model as executor
    tool: claude-code
    providers: [anthropic, local]
    tiers: {strong: opus, standard: sonnet, light: llama-local}
default_agent: claude-pro-1
worktrees_dir: null                # null = <parent-of-repo>/<repo>-worktrees
```

`providers` lists every provider whose models the agent may use (values: `anthropic`, `openai`, `google`, `local`, or any free-form string). Hybrid setups (e.g. a Claude orchestrator delegating to a local Llama model) declare all of them; Forge does not care which model actually runs, only what is declared for traceability and review rules.

The active agent is chosen with `--agent <id>` or `FORGE_AGENT` env var, falling back to `default_agent`.

### 6.5 Model resolution — who decides, when, how

Repository files speak in **abstract** terms (`tier`, `effort`); the machine profile translates them into **concrete** models. Responsibilities: the framework proposes, the user chooses, the project constrains.

1. **Presets (framework).** Forge ships presets per known tool (`claude-code`, `codex`, `ollama`/local), e.g. claude-code → `strong: opus, standard: sonnet, light: haiku`. Presets live in `src/forge/presets/*.yml` and are updated with Forge releases because models change.
2. **Choice (user), at first `forge setup` on a machine.** Setup detects installed tools (`claude`, `codex`, `ollama list`, …), proposes the preset for each, shows project recommendations from `config.models.recommended`, and asks the user to confirm or edit each tier and the default agent. For local runtimes it lists available models and asks which to use per tier (or none). The result is written to the profile. Accepting everything takes a few keystrokes.
3. **Changes (user), any time.** `forge profile show`, `forge profile set <agent>.<tier|effort.level>=<value>`, `forge profile edit` (opens `$EDITOR`), or re-running `forge setup`. Changing the profile re-renders machine-level adapter files (rendered subagents).
4. **Constraints (project).** `config.models` (§6.1). `forge setup`, `forge status` (Health) and `forge next` warn when the active profile violates them (e.g. `strong` mapped to a small local model, provider not allowed). Violations are warnings, never silent changes; `min_tier_for` violations make `forge review`/`/plan` refuse to run with that agent unless `--force`.
5. **Prompts to revisit.** Setup/status remind the user when: a new tool is detected, a Forge release brings new presets that differ from the profile, or project constraints changed.
6. **Resolution at runtime.** For a subagent or command: `tier: task` → the task's tier; otherwise the declared tier; then `min_tier_for` may raise it; manual `--tier`/`--effort` overrides may only raise. The profile maps the resulting tier/effort to the concrete model/parameter of the active agent's tool. If the tool does not support a parameter (e.g. effort on a local model) it is ignored and setup reports it once.
7. **Traceability.** Claim commits carry `Forge-Agent`; the PR body records the agent id, its `providers` and the resolved tier → model map used, so the team can later see which mappings work best.

---

## 7. Task model

### 7.1 Issue structure

Each task is a GitHub issue with:
- **Title**: `[M1-T07] Save/load page endpoint`
- **Milestone**: the GitHub milestone (`M1`)
- **Labels**: `forge:task`, one `status:*` label, `size:S|M`, `tier:strong|standard|light`, `type:contract|impl|test|chore`
- **Body**: human-readable description followed by a machine block that Forge owns:

````markdown
<!-- forge:begin -->
```yaml
id: M1-T07
spec: specs/m1/page-builder.md
covers: [AC-3, AC-4]
depends_on: [M1-T01, M1-T02]
zones: [api/pages, db/pages]
size: M
tier: standard
type: impl
done_when:
  - integration tests for save/load pass
  - OpenAPI contract respected
```
<!-- forge:end -->
````

Forge only rewrites content between the markers; humans may edit the rest.

### 7.2 Status labels and transitions

```
proposed ──(lead accepts)──▶ ready ◀──(deps closed)── blocked
ready ──(forge next)──▶ in-progress ──(forge ship)──▶ in-review ──(merge)──▶ done (issue closed)
in-progress ──(forge handoff)──▶ handoff ──(forge next)──▶ in-progress
in-progress ──(lease expired)──▶ (shown as stale; reclaimable)
any ──(conflict escalation)──▶ + label blocked:conflict
```

- Labels are a **view** for humans. Forge always recomputes truth from: issue open/closed, metadata block, task branch existence and tip, assignee, and comments. `forge refresh` (also run by the radar workflow) re-aligns labels.
- `blocked` = at least one `depends_on` task is not closed. Closing an issue (merge with `Closes #n`) unblocks dependents on the next refresh.

### 7.3 Branch naming

- Task branch: `task/<issue-number>` (e.g. `task/42`). The branch is also the lock.
- Backups: local refs only, `refs/forge/backup/task-<n>/<timestamp>` (never pushed).

---

## 8. Claim, lease, handoff

### 8.1 Atomic claim (`forge next`)

Selection:
1. `git fetch`; load open `forge:task` issues of the active milestones.
2. Candidates = not `proposed`, deps all closed, no live claim (no branch, or branch whose claim is stale and `--reclaim` was passed), zones not overlapping any in-progress task.
3. Order: `handoff` tasks first; then milestone order (roadmap), wave, size (S before M), issue number. Skip tasks whose `tier` the active agent cannot serve unless `--any-tier`.
4. `--task <id|#n>` claims a specific task (still subject to the checks).

Claim (compare-and-swap, works on any git remote):
1. Create an empty **claim commit** whose parent is the expected tip:
   - new task: parent = `origin/<default_branch>`;
   - handoff/reclaim: parent = the current tip of `task/<n>`.
   Message: `forge: claim M1-T07` with trailers `Forge-Claim: <github>`, `Forge-Agent: <agent id>`, `Forge-Task: M1-T07`.
2. Push with an explicit lease:
   - new: `git push origin <sha>:refs/heads/task/<n> --force-with-lease=refs/heads/task/<n>:` (empty expected value = ref must not exist);
   - resume: `--force-with-lease=refs/heads/task/<n>:<expected tip>`.
3. Rejected push = somebody else won → try next candidate (up to `claim.max_attempts`), exit code 3 if none.

Post-claim zone verification (closes the race between two people claiming *different* tasks with overlapping zones at the same time):
- After a successful push, re-list live claims. If another live claim overlaps by zone and its claim commit is earlier (tie-break: lower issue number), release own claim (new task: delete the branch with lease; resumed task: push the previous tip back with lease) and continue with the next candidate.

After the claim:
- assign the issue to the user, set `status:in-progress`, post/update the single **Forge lease comment** (`<!-- forge:lease -->`, edited in place, never spammed) with who, agent, time;
- add the issue to the Project board if configured;
- create the worktree (if enabled) at `<worktrees_dir>/task-<n>` checked out on `task/<n>`;
- write `.forge/local/current.json` (task id, issue number, branch, base sha, agent);
- print the **task context** (also `forge task show`): metadata, issue body, the spec sections for the covered ACs, zones and their paths, dependency summaries, relevant ADRs.

### 8.2 Lease and heartbeat

- Last activity = max(committer date of the branch tip, timestamp in the lease comment).
- `forge heartbeat` updates the lease comment. `forge sync`, `forge ship` and pushes refresh activity implicitly.
- A claim is **stale** when last activity is older than `lease_hours`. Stale claims are flagged by `forge status` and the radar workflow (comment mentioning the owner). `forge next --reclaim` or `forge reclaim <n>` takes over a stale task through the resume CAS above, posting a comment.

### 8.3 Handoff

`forge handoff` (driven by the `/handoff` prompt):
1. Commits work in progress (`forge: wip`) and pushes the branch.
2. Posts a structured comment (`<!-- forge:handoff -->`): done, remaining, decisions taken, gotchas, next step, how to run/verify. The agent writes the content; the CLI formats and posts it (`--note-file`).
3. Unassigns the user, sets `status:handoff`.

`forge next` prefers handoff tasks and prints the latest handoff note as part of the task context.

### 8.4 Release a claim

`forge release-claim [--delete-branch]`: unassign, set `status:ready`, post comment. Branch is kept unless `--delete-branch` and there are no non-claim commits.

---

## 9. Planning

### 9.1 Specs: `specs/<milestone>/<feature>.md`

```markdown
---
milestone: M1
status: draft            # draft | ready
owner: vincenzo-s
---
# Page builder — editor base
## Goal
## In scope / Out of scope
## Requirements
- **AC-1** A user can drag a block from the palette onto the canvas.
- **AC-2** …
## Constraints
## Open questions
- (must be empty, or every item marked RESOLVED, for status: ready)
```

Acceptance criterion ids (`AC-n`) are unique within a milestone.

### 9.2 Roadmap: `plans/roadmap.yml`

```yaml
milestones:
  - id: M0
    title: Foundations
    goal: monorepo, CI, devcontainer, plugin/hub skeleton, auth contract
    demo: the WP plugin authenticates against the Hub
    lead: vincenzo-s
    specs: [specs/m0/foundations.md]
  - id: M1
    title: Editor base
    demo: create and save a page
    lead: <github>
```

Produced by the `/plan roadmap` prompt from a full project spec document: the agent asks clarifying questions first, then proposes milestones as **demoable vertical slices**, splits the big document into per-milestone specs, and opens a PR. Rule: milestone N+1 is planned in detail when milestone N is about 70% done.

### 9.3 Task plan: `plans/<milestone>.yml`

```yaml
milestone: M1
tasks:
  - id: M1-T01
    title: Page JSON schema and shared types
    type: contract
    spec: specs/m1/editor.md
    covers: [AC-1]
    depends_on: []
    zones: [shared/types]
    size: S
    tier: strong
    description: |
      …
    done_when:
      - schema published in shared/types with unit tests
```

### 9.4 `forge plan validate <milestone>` (deterministic)

Errors (exit 1):
- unknown fields, duplicate ids, unknown zones, unknown `depends_on`, dependency cycles;
- `size: L` (must be split);
- spec referenced with `status: draft` or unresolved open questions;
- tasks touching a `contract` zone that are not `type: contract`, unless explicitly `allow_contract_change: true`.

Warnings:
- two tasks in the same wave overlapping by zone (suggest: add a dependency or split zones);
- acceptance criteria in the milestone specs not covered by any task;
- waves with fewer tasks than active team members (parallelism report).

Output: the waves (topological levels) with task ids, and the coverage matrix AC → tasks.

### 9.5 `forge plan apply <milestone>` (idempotent)

- Ensures labels and the GitHub milestone exist.
- Upserts one issue per task, matched by the `id` in the metadata block.
- Closed issues are never modified. For in-progress issues, only non-structural fields (title, description, done_when) are updated; structural changes (zones, deps) produce a warning comment for the assignee.
- Tasks removed from the plan: if not started → closed as "not planned" with a comment; if started → comment + warning, left open.
- Recomputes `ready`/`blocked`.
- Normally executed by the `forge-plan-apply` workflow when a change to `plans/*.yml` is merged to main; the lead can also run it manually.

### 9.6 The `/plan` prompt

- `/plan roadmap <spec-file>` → questions → roadmap + per-milestone specs → PR `plan: roadmap`.
- `/plan <milestone>` → read specs, zones, ADRs, code → if the spec is not ready, list questions and stop → write `plans/<milestone>.yml` contract-first → run `forge plan validate` until no errors → address or explain warnings → open PR `plan: <milestone>`.
- `/plan <milestone> --update` → diff the specs against existing tasks → minimal change set → PR.
- Proposals: during `/dev`, discovered work becomes `forge propose` (issue with `status:proposed`); the lead accepts it by adding it to the plan file.

---

## 10. Sync and conflict management

### 10.1 When
Manually; automatically at the start of `forge ship`; when `forge status` reports drift (default branch commits touching the task's zones); at least daily (the prompt reminds).

### 10.2 `forge sync` steps
1. **Preflight**: if the tree is dirty, commit `forge: wip`. `git fetch`. Save backup ref.
2. **Analysis** (no LLM): commits on `origin/<default>` since the merge base; files changed upstream ∩ files changed by the task. Classification:
   - `none` — no intersection, no contract change in zones the task depends on;
   - `textual` — same files changed;
   - `semantic` — a `contract: true` zone changed upstream and the task depends on it (by zone or by `depends_on`);
   - `out_of_zone` — intersecting files fall outside the task's zones.
3. **Rebase** onto `origin/<default>` (rerere enabled).
4. **Conflicts**:
   - run configured mechanical resolvers (§10.4) on matching paths;
   - if conflicts remain → stop with the rebase in progress, write `.forge/local/sync.json` (files, classification, upstream commits → PRs → issues for context), print the report, **exit code 2**. The agent resolves following `/sync` rules, then runs `forge sync --continue`.
   - `out_of_zone` or contract files in conflict → **exit code 4 (escalation)** unless `--force-resolve`: the CLI aborts the rebase, labels the issue `blocked:conflict`, comments on both issues mentioning the other author.
5. **Verify** (`--continue` or after a clean rebase): run `checks` from config. Failures → exit 2 with the report; the agent fixes own-code failures, or escalates (`forge escalate`) if the failing area belongs to someone else.
6. **Push** `--force-with-lease` on `task/<n>` only; refresh lease; append to `.forge/local/sync-log.json` the conflicts and how they were resolved (used by `forge ship` for the PR body); post a short sync summary comment only when conflicts were resolved.

`forge sync --abort` restores the backup ref.

### 10.3 Agent rules for resolving conflicts (in `/sync` prompt and `.forge/rules.md`)
- Both intents must survive. The default-branch side is already reviewed: adapt **your** code to it.
- Before resolving, read the upstream PR/issue/spec that introduced the change (paths given in `sync.json`).
- Never resolve by taking one side wholesale for a whole file.
- Never change a contract the task does not own; escalate instead.
- Stop and ask the user if the resolution grows beyond the task's zones or the confidence is low.

### 10.4 Mechanical resolvers
- `regenerate`: take upstream version, run `command`, stage the result (lockfiles).
- `union`: line union (changelog-like files; also set via `.gitattributes`).
- Others may be added later; `ours`/`theirs` for whole files are deliberately not offered.

### 10.5 Prevention summary (documented for users)
Zones in planning → no overlapping claims → contract-first waves → frequent sync → merge queue (if available on the repo's plan) or required "up to date" status check.

---

## 11. Ship, review, status, release

### 11.1 `forge ship`
1. Runs `forge sync` (stops on its exit codes).
2. Runs `checks`.
3. Computes files outside the task's zones → listed in the PR as "Out-of-zone changes" (warning).
4. Creates or updates the PR from `task/<n>` with the project PR template: `Closes #<n>`, task id, AC checklist from `covers`, `done_when` checklist, out-of-zone changes, conflict resolutions from the sync log, agent id, its providers and the resolved tier → model map (§6.5).
5. Sets `status:in-review`, requests reviewers: when `review.cross_provider` is true, prefer members (from `team.yml`) whose providers do not overlap with the author's active agent `providers`, then members with partial overlap; otherwise any other member. The PR body records the author agent's `providers`.

### 11.2 `forge review <pr>`
Prints the review context (diff, task metadata, spec excerpts, zone check, CI status) for the agent. The `/review` prompt checks ACs, `done_when`, zone discipline, tests, contract respect, then submits via `forge review <pr> --submit approve|request-changes --body-file <f>` (wrapper over `gh pr review`).

### 11.3 `forge status [--me] [--milestone M] [--json]`
Sections:
- **Me**: current task, branch, last activity, drift (upstream commits touching my zones), pending sync, CI state of my PR.
- **Team**: in-progress tasks (who, agent, zones, last activity, stale flag), in review, handoffs waiting.
- **Queue**: ready tasks count and the next 3 `forge next` would pick for me.
- **Blocked**: by dependencies, by `blocked:conflict`.
- **Milestone**: done/total, waves progress, ACs covered by merged tasks.
- **Health**: CLI version vs `forge_version`, profile vs `config.models` constraints, newer presets available, unknown labels, issues without metadata, proposals awaiting the lead.

### 11.4 `forge release <milestone>`
Lead-only (soft check against `team.yml`/roadmap, warning if not lead). Verifies all tasks closed (or lists open ones and stops), generates a CHANGELOG section from merged PR titles grouped by type, creates tag `v<x.y.z>` (asks or `--version`), closes the GitHub milestone, posts a summary.

---

## 12. Agent commands (prompts)

Source files: `.forge/prompts/<name>.md` with frontmatter:

```markdown
---
name: next
description: Pick and claim the next available task, then load its context
args: "[task-id] [--agent <id>]"
roles: [dev]
---
<prompt body>
```

Each prompt must: state its goal; list the exact `forge` calls with `--json`; describe how to interpret exit codes; list guardrails; say what to report to the user at the end. Prompts never duplicate logic that the CLI performs.

| Command | Who | Agent behaviour (summary) |
|---|---|---|
| `/setup` | all | Run `forge setup`; fix what it reports (guide logins, profile creation); run project setup; confirm the app starts locally |
| `/plan` | lead | §9.6 |
| `/next` | all | `forge next --json`; summarize the task context; propose an implementation outline; ask the user to confirm before coding |
| `/dev` | all | Implement the current task within its zones following specs, ADRs and `rules.md`; small commits; write/extend tests for `done_when`; run checks; `forge propose` for discovered extra work; `forge heartbeat` on long sessions |
| `/sync` | all | §10 — run, interpret exit codes, resolve per §10.3, `--continue`, escalate when required |
| `/ship` | all | `forge ship`; summarize the PR; report CI |
| `/review` | all | §11.2 |
| `/status` | all | `forge status --json`; present a concise human summary with suggested next actions |
| `/handoff` | all | Write the handoff note (§8.3) from the session's work; `forge handoff --note-file` |
| `/release` | lead | `forge release`; confirm version; report |

Natural language must also work: `AGENTS.md` maps phrases like "take the next task" or "sync my branch" to the same prompts.

### 12.1 Subagents

Few, role-named (never model-named), optional. Defined once in `.forge/agents/<name>.md`:

```markdown
---
name: test-writer
description: Writes tests that prove the task's done_when criteria
tier: light            # strong | standard | light | task (inherit the task's tier)
effort: medium         # low | medium | high
tools: [read, edit, shell]
---
<instructions>
```

v0 ships these subagents:

| Subagent | Tier / effort | Used by | Purpose |
|---|---|---|---|
| `explorer` | light / low | `/next`, `/dev` | Read code, specs, ADRs; return a compact context summary to keep the main agent's context clean |
| `test-writer` | task / medium | `/dev` | Write and run tests for `done_when` |
| `reviewer` | strong / high | `/review`, self-check before `/ship` | Review against ACs, zones, contracts, tests |
| `implementer` | standard / medium | `/dev` (when delegation is on) | Implement one brief from the lead agent inside the listed files and zones (added in v0.2, see [ADR 0017](decisions/0017-implementer-subagent-and-delegation.md)) |

Note: v0.1 shipped the first three. A fourth subagent, `implementer`, was added in v0.2 for delegation. The delegation switch (`off`, `auto`, `on`) is defined in ADR 0017, which amends this section.

Rules:
- Every command must work **without** subagents (tools/models that do not support them): prompts say "if subagents are available, delegate X to `<name>`; otherwise do it yourself".
- Conflict resolution stays in the main agent (needs the whole context).
- Rendering is **machine-level**: adapters turn `.forge/agents/*.md` + the profile (§6.5) into the tool's native subagent files with concrete models, e.g. `.claude/agents/forge-<name>.md` (gitignored, prefix `forge-` so teams can still commit their own agents). Tools without native subagents get the definitions listed in `AGENTS.md`.

---

## 13. Adapters

- **generic** (always): renders `AGENTS.md` with: project summary, rules from `.forge/rules.md`, the command table, and the instruction "when the user invokes `/<cmd>` or asks for it in words, read and follow `.forge/prompts/<cmd>.md`". Works for any agent that reads `AGENTS.md`, including local-model agents.
- **claude**: renders `.claude/commands/<name>.md` (description and argument hint from frontmatter, body = prompt) and a thin `CLAUDE.md` that imports/points to `AGENTS.md`. *Verify the current Claude Code format for custom commands/skills at build time and target it.*
- **codex**: Codex reads `AGENTS.md` natively. If the current Codex version supports user-level custom prompts/skills, `forge setup` renders them into the user-level location. *Verify the current mechanism at build time; fall back to generic.*
- Subagents (§12.1) are rendered per machine by each adapter that supports them (Claude: `.claude/agents/forge-*.md` with frontmatter `name`, `description`, `tools`, `model` — alias `haiku|sonnet|opus|fable`, a full model id, or `inherit` — and `effort` — `low|medium|high|xhigh|max`, availability depends on the model; verified against the Claude Code subagents docs in Nov 2026. Project `.claude/agents/` takes precedence over `~/.claude/agents/`. Thinking is inherited from the main session; there is no per-subagent thinking field). Codex/others: per the tool's current mechanism, else listed in `AGENTS.md`.
- `forge adapters generate [--check]`: regenerates project-level adapter files; `--check` fails if they are out of date (used in CI). `--local` regenerates machine-level files (rendered subagents, user-level prompts) from the current profile.

Adding an adapter = one module implementing `render(prompts, config) -> list[GeneratedFile]` plus docs in `docs/adapters.md`.

---

## 14. GitHub workflows (project templates)

- `forge-ci.yml`: on PR → install forge (pinned), `forge adapters generate --check`, project `checks`.
- `forge-plan-apply.yml`: on push to default branch touching `plans/*.yml` → `forge plan validate` + `forge plan apply`.
- `forge-radar.yml`: on push to default branch → `forge refresh` (labels, unblock dependents) and comment on in-progress tasks whose zones were touched ("sync recommended"); on schedule (hourly) → stale lease detection.

Workflows use `GITHUB_TOKEN` with `issues: write`, `pull-requests: write`, `contents: read`.

---

## 15. GitHub Projects v2 (optional)

If `github.project_number` is set, `plan apply`, `next`, `ship`, `refresh` add issues to the project and set its `Status` single-select field to mirror the status label (Ready, In progress, Handoff, In review, Done, Blocked). The board is a view; labels/metadata stay authoritative.

---

## 16. CLI reference (v0)

Global options: `--json`, `--agent <id>`, `--repo <path>`, `--verbose`, `--yes`.

| Command | Purpose |
|---|---|
| `forge --version` | Version |
| `forge init [--adapters claude,generic]` | Scaffold project files from templates (refuses to overwrite without `--force`) |
| `forge setup [--check]` | Machine checks (git ≥ 2.38, gh authenticated, Python, version pin), tool detection, interactive profile creation/update with presets and project recommendations (§6.5), constraint check, git rerere, machine-level adapters, run `setup.commands` |
| `forge profile show|set|edit` | Inspect or change the machine profile (§6.5); re-renders machine-level adapter files |
| `forge next [--task X] [--reclaim] [--any-tier] [--no-worktree] [--dry-run]` | Select and claim (§8.1) |
| `forge task show [X]` | Task context |
| `forge heartbeat` | Refresh lease |
| `forge handoff --note-file F` | §8.3 |
| `forge release-claim [--delete-branch]` | §8.4 |
| `forge reclaim <n>` | Take over a stale task |
| `forge sync [--continue|--abort] [--force-resolve]` | §10 |
| `forge escalate --reason R` | Label `blocked:conflict`, comment, mention relevant people |
| `forge ship [--draft]` | §11.1 |
| `forge review <pr> [--submit …]` | §11.2 |
| `forge propose --title T --body-file F` | Create a `status:proposed` issue |
| `forge status [--me] [--milestone M]` | §11.3 |
| `forge refresh` | Recompute labels, unblock dependents |
| `forge plan validate <M>` / `forge plan apply <M>` | §9 |
| `forge release <M> [--version V]` | §11.4 |
| `forge adapters generate [--check] [--local]` | §13 |
| `forge docs [--check]` | Generate `docs/forge/commands.md` from CLI help + prompt frontmatter |

Exit codes: `0` ok · `1` error · `2` agent action needed (conflicts / failing checks) · `3` nothing available · `4` escalation required · `5` precondition failed (version mismatch, dirty state, not authenticated, not on a task branch).

JSON output: every command returns `{"ok": bool, "code": int, "data": {...}, "messages": [...]}`.

---

## 17. Installation

Prerequisites (documented in README): git ≥ 2.38, GitHub CLI `gh` authenticated with the repo scope, macOS or Linux (Windows via WSL in v0).

`install.sh` (bootstrap):
1. Checks git and gh; on macOS with Homebrew offers `brew install gh`; otherwise prints instructions.
2. Installs `uv` with the official installer if missing.
3. `uv tool install --python 3.12 "git+https://github.com/dragonfly25/forge_it@<version>"` (uv provisions Python itself).
4. Prints next steps: `gh auth login`, `cd <project> && forge setup`.

Alternative: `pipx install "git+https://github.com/dragonfly25/forge_it@<version>"`.

Project adoption: the lead runs `forge init` once, edits `config.yml`, `team.yml`, `zones.yml`, commits. Each developer: install, clone, `forge setup`.

Upgrades (v1): `forge upgrade` re-renders templates and opens a PR; mismatching CLI versions are reported by `setup`/`status` and refused by `next`/`ship` (exit 5).

---

## 18. Documentation deliverables

Framework repo: `README.md` (what, install, quickstart), `docs/protocol.md`, `docs/architecture.md`, `docs/adapters.md`, `CHANGELOG.md`.

Project templates: `docs/forge/onboarding.md` (from install to first merged task), `docs/forge/commands.md` (generated by `forge docs`), `docs/forge/roles.md` (lead vs dev), `docs/forge/faq.md` (blocked conflict, tokens ran out, stale task, version mismatch, adding a zone).

Language: English everywhere.

---

## 19. Roadmap after v0

- **v1**: `forge upgrade`; Windows support; affected-only checks by zone; richer resolvers (migration renaming); `/spec` prompt (idea → ready spec); GitHub native issue dependencies/sub-issues mirroring if available; token/usage notes per task.
- **v2**: optional orchestrator that runs headless agents on ready tasks (claim → dev → ship) within a budget, with a human approval gate on plans and merges.

---

## 20. Open points to confirm during the build

- Exact current formats for Claude Code custom commands/skills and for Codex user-level prompts/skills (§13).
- Which subagent fields Codex currently accepts and where its subagent files live (§12.1, §13). Claude Code is confirmed: `model` and `effort` frontmatter fields (§13).
- Whether the target repositories' GitHub plan offers merge queue; otherwise require "branch up to date before merge".
- ~~GitHub org and repository name for the framework~~ RESOLVED: `dragonfly25/forge_it`, distribution `forge-it` (ADR 0001).
