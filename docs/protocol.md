# The Forge coordination protocol

This document describes how people and agents coordinate through Forge, from planning to release. It is the user-facing view of the rules the `forge` CLI enforces. The [spec](spec.md) is the source of truth for intent. Where an ADR amends the spec, the ADR wins. Each section links the ADRs that hold the details.

## 1. Principles

- **GitHub is the source of truth.** Task state lives in issues, milestones, labels, branches and PRs. Rules, specs and plans live in the repository. There is no server.
- **The CLI executes, the agent reasons.** Claims, locks, branch operations, state and validation are code in `forge`. Prompts in `.forge/prompts/` tell the agent when to call the CLI and how to do the creative work.
- **Labels are a view.** Forge recomputes the truth from the issue state, its metadata block, the task branch and open PRs. `forge refresh` re-aligns the labels.
- **Everything is reviewable.** Plans arrive as PRs. Conflict resolutions are listed in the PR. Handoffs are issue comments.
- **Forge never pushes to the default branch.** Work reaches it only through PRs.

## 2. Task model and derived status

([ADR 0008](decisions/0008-task-model-and-derived-status.md))

A **task** is a GitHub issue labeled `forge:task`. Its title is `[M1-T07] Title`. It belongs to the GitHub milestone `M1`. Its body ends with a machine block that Forge owns:

````markdown
<!-- forge:begin -->
```yaml
id: M1-T07
spec: specs/m1/page-builder.md
covers: [AC-3, AC-4]
depends_on: [M1-T01]
zones: [api/pages]
size: M
tier: standard
type: impl
done_when:
  - integration tests for save/load pass
```
<!-- forge:end -->
````

- `size` is `S` or `M`. A larger task must be split.
- `tier` is `strong`, `standard` or `light`: the model strength the task needs.
- `type` is `contract`, `impl`, `test` or `chore`.
- Forge rewrites only the text between the markers. Everything else in the issue belongs to humans.

**Labels** owned by Forge: `forge:task`, one `status:*` label (`proposed`, `ready`, `blocked`, `in-progress`, `handoff`, `in-review`, `done`), `size:*`, `tier:*`, `type:*` and `blocked:conflict`.

**Branch events.** The branch `task/<n>` is the lock. Commits that Forge makes on it carry the trailers `Forge-Task: <id>` and exactly one event trailer: `Forge-Claim`, `Forge-Handoff` or `Forge-Release`. The newest event on the branch's first-parent history, since the merge base with the default branch, is the **last event**. This is how a handoff or a release becomes visible to everyone who can fetch.

**Derived status.** The first matching rule wins:

| # | Condition | Status |
|---|---|---|
| 1 | Issue closed (not planned: no status label) | `done` |
| 2 | Issue has `status:proposed` | `proposed` |
| 3 | An open PR exists from `task/<n>` | `in-review` |
| 4 | The branch exists and its last event is a claim | `in-progress` |
| 5 | The branch exists and its last event is a handoff | `handoff` |
| 6 | Any dependency is not closed | `blocked` |
| 7 | Otherwise | `ready` |

A claim whose lease has expired still derives `in-progress`. It is shown as **stale** and can be reclaimed (section 3).

`forge refresh` applies the derived labels, and the radar workflow runs it on every push to the default branch. It is idempotent and never touches `blocked:conflict`.

## 3. Claims, leases and verification

([ADR 0009](decisions/0009-claim-lease-and-verification.md), [ADR 0007](decisions/0007-git-and-github-access.md))

### Selection

`forge next` fetches, then lists candidates: tasks whose derived status is `ready` or `handoff`. With `--reclaim`, stale `in-progress` tasks count too. It excludes a task when:

- an open PR exists from its branch;
- it carries `blocked:conflict`;
- its zones overlap a **zone holder**: any task with a live claim or an open PR (section 5).

Order: handoff tasks first, then milestone, wave, size (S before M), issue number. Tasks whose tier the agent cannot serve are skipped unless `--any-tier`. `--task <M1-T07|#42|42>` restricts the choice to one task, under the same checks.

### The claim

A claim is a compare-and-swap push. It works on any git remote.

1. Forge creates an **empty claim commit** with `git commit-tree`, with the trailers `Forge-Claim: <login>`, `Forge-Agent: <agent id>`, `Forge-Task: <id>` and a unique `Forge-Run`.
   - New task: the parent is `origin/<default>`.
   - Handoff, release or reclaim: the parent is the current tip of `task/<n>`.
2. Forge pushes it with an explicit lease. A new task uses `--force-with-lease=refs/heads/task/<n>:` (the ref must not exist). A resume expects the old tip. A bare `--force` is never used.
3. A rejected push means someone else won. Forge tries the next candidate, up to `claim.max_attempts`. If none is left, it exits 3.

### Post-claim zone verification

Two people can claim different tasks with overlapping zones at the same time. After a successful push, Forge lists the live claims again. For every other claim that overlaps my zones:

- if it comes before mine (claim commit date, then issue number), I **yield** at once;
- otherwise I **wait** for it to disappear, polling every 2 seconds, at most 5 times. If it is still live, I yield.

Of two overlapping claims, the one pushed later always sees the earlier one, so at most one keeps the zones. No clock accuracy is needed for safety. To yield, a new claim deletes the branch with a lease, and a resumed claim pushes the previous tip back with a lease. Forge then tries the next candidate.

### After the claim

Forge assigns the issue to you, sets `status:in-progress`, writes the **lease comment**, creates the worktree at `<repo>-worktrees/task-<n>`, writes `.forge/local/current.json` and prints the task context. The lease comment is written last. It marks the claim as complete.

### Lease, activity and staleness

- The **lease comment** (`<!-- forge:lease -->`) is edited in place, never spammed. Its last line carries machine data: task, owner, agent, claim sha and run id, `claimed_at`, `heartbeat_at`, `released_at`.
- **Last activity** is the later of the committer date of the branch tip and `heartbeat_at`. A heartbeat only counts when it belongs to the current claim.
- A claim is **stale** when last activity is older than `lease_hours` (default 24).
- `forge heartbeat` refreshes the lease. `forge sync`, `forge ship` and pushes refresh it too.
- The radar workflow comments on stale tasks. `forge status` flags them.

### Interrupted claims

If a run stops after the push, the claim is incomplete: the lease comment does not name it. `forge next --task <n>` completes it. A plain `forge next` takes over only when a pending-claim record proves that the earlier run is gone. A per-task lock and the `Forge-Run` trailer keep two runs under the same identity apart. Claims and releases use an OS lock that dies with its process: `flock` on macOS and Linux, `LockFileEx` (`msvcrt.locking`) on Windows. They work on all three ([ADR 0021](decisions/0021-native-windows.md)).

## 4. Handoff, release and reclaim

([ADR 0009](decisions/0009-claim-lease-and-verification.md), [ADR 0012](decisions/0012-ship-review-handoff-propose.md))

| Command | What it does | Branch | Issue |
|---|---|---|---|
| `forge handoff --note-file F` | Commits wip, pushes, posts the structured note | wip commit plus an empty `Forge-Handoff` commit | unassigned, `status:handoff` |
| `forge release-claim [--delete-branch]` | Gives the task back | empty `Forge-Release` commit on the remote tip | unassigned, back to `ready` or `blocked` |
| `forge reclaim <n>` | Takes over a stale claim | resume claim on the current tip | reassigned, comment names the old owner |

- **Handoff** needs your own claim (live, or stale while the last event is still your claim). The note (`<!-- forge:handoff -->`) should have the headings Done, Remaining, Decisions, Gotchas, Next step and How to verify. Forge warns when one is missing. The next `forge next` prefers the task and prints the latest note. Handoff refuses while the task has an open PR.
- **Release-claim** pushes only what is already on the remote. With `--delete-branch` it deletes the branch only when every commit since the merge base is an empty Forge event commit.
- **Reclaim** works like `forge next --task <n> --reclaim`. A fresh claim held by someone else exits 3.

This lets work move between accounts, providers and people when tokens run out.

## 5. Zones and overlap

([ADR 0006](decisions/0006-zone-overlap.md))

A **zone** is a named set of path globs in `.forge/zones.yml`. A **contract zone** (`contract: true`) holds interfaces that others depend on.

- Globs are anchored at the repository root. `**` matches zero or more path segments. `*` and `?` never cross `/`. A glob without wildcards matches only that exact path, so write `contracts/openapi/**` for a directory.
- Two tasks **overlap** when they share a zone, when a pair of their zones is listed under `conflicts`, or when their zones' globs may intersect.
- The glob check is conservative. Two globs intersect unless the literal leading directories differ, or both are different literal paths. A false positive only costs parallelism. A false negative would defeat conflict prevention.
- A changed file that matches no zone is **unzoned**. `unzoned: warn|error` sets the severity.
- Overlap decides claims (section 3), planning warnings (section 9) and `out_of_zone` sync escalation (section 7).

## 6. Waves

([ADR 0008](decisions/0008-task-model-and-derived-status.md), [ADR 0013](decisions/0013-planning.md))

A **wave** is a set of tasks that can run in parallel: the tasks at the same depth of the dependency graph. Wave 0 has no dependencies. Wave 1 depends only on wave 0, and so on.

- `forge plan validate` prints the waves. It warns when two tasks of one wave overlap by zone.
- `forge next` uses the wave as a sort key, after the milestone.
- `forge status` shows done/total for each wave.
- A dependency on a task of another milestone counts as satisfied for wave computation when that task is closed (status) or when validating offline (plan).
- Plans put `contract` tasks first. Implementation tasks depend on them, so the contract exists before anyone builds on it.

## 7. Sync

([ADR 0011](decisions/0011-sync-and-conflicts.md))

`forge sync` rebases the current task onto the default branch. It needs your own live claim, a valid `zones.yml` and a full clone. Run it at least daily, when `forge status` reports drift, and at the start of `forge ship`.

**Steps**
1. **Preflight.** A dirty tree becomes a `forge: wip` commit (hooks run). Forge fetches and saves a local backup ref `refs/forge/backup/task-<n>/<timestamp>`. It writes `.forge/local/sync.json` first, so any step can be continued or aborted.
2. **Analysis** (no LLM). Forge intersects the files changed upstream with the files changed by the task, then classifies:

   | Class | Meaning |
   |---|---|
   | `textual` | the same files changed |
   | `out_of_zone` | some shared file is outside the task's zones |
   | `semantic` | an upstream file is in a contract zone the task depends on |
   | `none` | none of the above |

   All that apply are reported. The primary class is the first of `out_of_zone`, `semantic`, `textual`, `none`.
3. **Rebase** with rerere on. There is no rebase when upstream has no new commits.
4. **Resolvers.** Each conflicted path that matches a `resolvers` glob is resolved mechanically, in config order:
   - `regenerate`: take the upstream version, run `command`, stage the result (lockfiles);
   - `union`: line union of both sides plus the base (changelog-like files).
   `ours` and `theirs` for whole files are deliberately not offered.
5. **Remaining conflicts.**
   - Any conflict outside the task's zones, or in a contract zone the task does not own, **escalates**: exit 4. A task owns a contract zone when the zone is its own and the task is `type: contract` or sets `allow_contract_change: true`. `--force-resolve` skips this check.
   - Otherwise Forge stops with the rebase in progress, writes the report to `sync.json` and exits 2. The agent resolves the files, stages them and runs `forge sync --continue`.
6. **Verify.** Forge runs `config.checks`. A failure exits 2.
7. **Push** with `--force-with-lease` on `task/<n>` only. Then Forge refreshes the lease, clears `blocked:conflict`, appends to `sync-log.json` and, if conflicts were resolved, posts one summary comment.

**Escalation (exit 4).** Forge aborts the rebase, restores the earlier state, labels the issue `blocked:conflict`, comments on the task issue and on each upstream issue, and mentions the authors of the upstream PRs. `forge escalate --reason R` does the same on request, and also mentions the leads.

**Agent rules** (in the `/forge-sync` prompt and `.forge/rules.md`): both intents must survive; adapt your code to the default branch, which is already reviewed; read the upstream PR and issue first; never take one side of a whole file; never change a contract you do not own; stop and ask when the resolution grows beyond your zones.

**`--abort`** restores the exact pre-sync tip, including the wip commit, then turns the wip changes back into uncommitted changes. It saves any moved HEAD under a `-aborted` ref first.

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | synced and pushed, or already up to date |
| 2 | conflicts for the agent, failing checks, a rejected hook, unmerged paths on `--continue` |
| 4 | escalation: out-of-zone or contract conflict |
| 5 | no current task, claim not yours or not live, shallow clone, sync already in progress, nothing to continue, push lease rejected |
| 1 | other git or GitHub failure |

## 8. Ship and review

([ADR 0012](decisions/0012-ship-review-handoff-propose.md))

### `forge ship [--draft]`

1. Run sync. Exit codes 2, 4 and 5 stop ship and are returned as they are.
2. Run `config.checks`, unless the last sync already passed them on this exact HEAD.
3. Compute the files outside the task's zones. A file in another zone is an **out-of-zone change**. A file in no zone is **unzoned**. With `unzoned: error`, ship exits 2 before touching the PR.
4. Create or update the PR from `task/<n>`. `--draft` applies only on creation.
5. Set `status:in-review`, refresh the lease and request reviewers.

**PR block.** Forge owns the text between `<!-- forge:pr-begin -->` and `<!-- forge:pr-end -->`. Text outside the markers is human and is kept. Ticked checkboxes stay ticked when their text is unchanged. The block holds, in order:

1. `Closes #<n>`, the task id and the spec path;
2. acceptance criteria checklist (from `covers`);
3. `done_when` checklist;
4. out-of-zone changes (or none);
5. conflict resolutions from the sync log (or none);
6. the agent id, its tool and providers, and the resolved tier to model map.

Ship can be re-run safely. It never merges the PR.

**Reviewer selection.** The candidates are the `team.yml` members except you, anyone already requested and anyone who already reviewed. Ship requests enough to reach `review.required_approvals`, and never removes anyone. With `review.cross_provider: true`, the ranking is:

1. no provider overlap with your active agent;
2. partial overlap;
3. no providers declared;
4. full overlap.

Within a rank, the team order is rotated by the issue number, so requests spread out. The result is deterministic.

### `forge review <pr>`

Without `--submit`, it prints the review context: the PR, the linked task and its ACs, the diff (capped at 300 000 bytes), a zone check, CI state and existing reviews. The `/forge-review` prompt checks ACs, `done_when`, zone discipline, tests and contracts. It also reads the task's entry in `plans/<M>.yml` and `docs/architecture/` and checks that the change stays inside the task's component and respects the interfaces it implements and uses. It then submits with `--submit approve|request-changes|comment --body-file F`. Forge adds a footer with the agent, its providers and the model. You cannot approve or request changes on your own PR. An agent below `min_tier_for.reviewer` gets exit 5 unless `--force`.

## 9. Planning

([ADR 0013](decisions/0013-planning.md))

**Inputs.** A spec (`specs/<milestone>/<feature>.md`) has frontmatter (`milestone`, `status: draft|ready`, `owner`) and acceptance criteria `AC-n`, unique within a milestone. A `ready` spec needs every open question marked `RESOLVED`, or none. `plans/roadmap.yml` lists milestones as demoable vertical slices, with a lead. A plan `plans/<milestone>.yml` lists the tasks.

**The `/forge-plan` prompt** asks questions first, proposes milestones, splits the spec, writes the plan contract-first and runs validate until there are no errors. Both the roadmap and each plan arrive as PRs (`plan: roadmap`, `plan: M1`). Milestone N+1 is planned in detail when milestone N is about 70% done.

**Sequence.** The lead runs `/forge-plan roadmap`, then `/forge-architect M1`, merges the `arch: M1` PR, and only then runs `/forge-plan M1`. The architecture comes before the task plan.

### Architecture

([ADR 0019](decisions/0019-milestone-architecture.md))

Each milestone has an architecture in the project repository, reviewed through its own PR.

- `docs/architecture/overview.md` maps the system as it is today.
- `docs/architecture/<M>.md` describes what milestone `<M>` adds or changes: components (each with zones), interfaces (each in a contract zone, with a producer and consumers), a data model, decisions, risks and open questions. Its frontmatter `status` is `draft` or `ready`.
- `/forge-architect <M>` writes both files and opens the PR `arch: <M>`. `/forge-architect <M> --update` changes only what moved. Durable decisions go to ADRs in `docs/decisions/`.
- `forge arch validate <M>` checks the files offline.

**The plan follows the architecture.** A plan task names its `component`. A `type: contract` task `implements` one interface. Any task can `use` interfaces. `/forge-plan` never adds a component or an interface. If the architecture is missing, in draft or too thin, it stops and sends the lead back to `/forge-architect <M>`.

**Mode.** `planning.architecture` in `.forge/config.yml` is `required` or `optional`. `forge init` writes `required`. A config without the key is `optional`. With `optional` and no `<M>.md` file, `forge plan validate` skips the architecture checks.

### `forge plan validate <milestone>`

Validation is offline. It never reads GitHub, so it runs in CI and on a fresh clone.

- **Errors (exit 1):** unknown fields, duplicate ids, unknown zones or dependencies, dependency cycles, `size: L`, a spec with `status: draft` or unresolved questions, a `covers` id that the spec does not define, an AC defined twice, a task touching a contract zone without `type: contract` (unless `allow_contract_change: true`).
- **Architecture errors** (when the architecture is checked, see above): the file is missing, invalid or still `draft`; a task has no `component` (required whenever the architecture is checked); a task has an unknown `component`, `implements` or `uses` id; an interface of the milestone has no contract task, or has more than one; a contract task implements nothing; a task uses an interface without depending on the task that implements it.
- **Warnings:** two tasks of one wave overlapping by zone, ACs that no task covers, waves narrower than the number of `dev` members, a task touching a zone outside its component, a contract task whose component is not the interface's producer.
- **Output:** the waves and the AC to task coverage matrix.

The `min_tier_for.plan` rule applies to validate and apply when a profile exists and `GITHUB_ACTIONS` is not set. An agent below the tier gets exit 5 unless `--force`. `forge arch validate` applies the same rule with `min_tier_for.architecture`.

`forge plan validate <milestone> --skeleton` skips only the `missing_field` errors for `description`, `done_when` and `tier`. Every other check runs in full, and a detail field that is present is still checked. `forge plan apply` has no such flag.

### Multi-agent planning

([ADR 0020](decisions/0020-multi-agent-planning.md))

Large milestones are planned by several agents in parallel. The CLI does the grouping and the merging. The agents do the reasoning. `forge plan split`, `forge plan assemble` and `forge arch split` are offline: they never call GitHub.

**Thresholds.** `planning.max_tasks_per_agent` (default 10) and `planning.max_components_per_agent` (default 3) in `.forge/config.yml`. Up to the threshold, one session does the detail. Above it, `ceil(N / max)` agents get balanced groups of contiguous items: 12 tasks give 6 + 6, 21 give 7 + 7 + 7, 25 give 9 + 8 + 8. Plans count tasks. Architectures count components. Tasks are sorted by component, then wave, then id. Components are sorted by id. The same input always gives the same groups.

**Plan, four phases** (`/forge-plan <M>`):

1. **Exploration.** In parallel, one `forge-explorer` per milestone spec and one per component involved.
2. **Skeleton.** The main session writes `plans/<M>.yml` with the skeleton fields only: `id`, `title`, `type`, `spec`, `covers`, `depends_on`, `zones`, `size`, `allow_contract_change`, `proposal`, `component`, `implements`, `uses`. It puts no comments in the file. It runs `forge plan validate <M> --skeleton` until there are no errors.
3. **Detail.** `forge plan split <M>` refuses a skeleton with errors, writes `.forge/local/plan/<M>/batches.json` and deletes fragments of an earlier split. With one agent, the main session writes `description`, `done_when` and `tier` into the plan itself. With more, it spawns one `forge-planner` per group, in parallel. Each writes `part-<k>.yml` and nothing else. Then `forge plan assemble <M>`.
4. **Review.** In parallel, one `forge-reviewer` per group. The main session then checks the whole plan, edits `plans/<M>.yml` directly and runs `forge plan validate <M>` until there are no errors.

**Architecture, four phases** (`/forge-architect <M>`):

1. **Exploration.** In parallel, one `forge-explorer` per spec and one per code area.
2. **Structure.** The main session proposes the frontmatter and waits for the lead's go. Then it writes it.
3. **Detail.** `forge arch split <M>` groups the components and assigns each interface to its producer's group. An interface whose producer is only in `overview.md` goes to `unassigned_interfaces`, and the main session writes it. One `forge-arch-writer` per group writes `.forge/local/arch/<M>/part-<k>.md`. The main session integrates the fragments into `<M>.md` and `overview.md`. There is no `arch assemble`.
4. **Review.** Two `forge-reviewer` subagents in parallel: spec coverage, and consistency with the code, the ADRs and `overview.md`. Then `forge arch validate <M>` until there are no errors.

**Rules.**

- **Disjoint writes.** Every parallel writer writes only its own fragment. It never edits `plans/`, `docs/`, `specs/` or `.forge/`, and the planner never sets a skeleton field. Claude Code does not resolve conflicts between subagents that write to the same file, and Codex subagents inherit the parent's sandbox, so the disjoint paths are the safeguard.
- **Local directories.** The fragments live in `.forge/local/plan/<M>/` and `.forge/local/arch/<M>/`. Both commands exit 5 if git would not ignore the directory.
- **`split` is deterministic.** It writes `batches.json` with sorted keys. It stores `skeleton_sha256`, a hash of the skeleton fields only. Comments, formatting and detail fields do not change it. There is no `--max-per-agent` flag.
- **`assemble` is atomic.** It refuses with exit 1 when the skeleton changed since `split` (`skeleton_changed`), when a fragment is missing or invalid, or when the merged plan fails validation. On any error it writes nothing. Otherwise it renders the plan to a temporary file and moves it over `plans/<M>.yml`. Running it again gives the same bytes.
- **Comments are lost.** `assemble` rewrites `plans/<M>.yml` from data, in a fixed key order, so YAML comments are lost.
- **Fallback.** Without subagents, or when spawning fails, the main session runs the same phases one group at a time, using the same `split` groups. `split` and `arch split` report `subagent.available` for `forge-planner` and `forge-arch-writer`. The final report says that the fallback ran.
- **`--update` stays single-session.**
- **Exit codes** for `split` and `assemble`: `0` done, `1` errors in the skeleton, the fragments or the merged plan, `5` a precondition failed (no config, no skeleton, no `batches.json`, directory not ignored). `arch split` uses `0`, `1` and `5` in the same way. None of them uses `2`, `3` or `4`.

### `forge plan apply <milestone> [--dry-run]`

Apply validates first, then makes GitHub match the plan. It is idempotent. Applying the same plan twice writes nothing.

- It creates the labels and the GitHub milestone, and creates or updates one issue per task, matched by the `id` in the metadata block.
- It owns two regions of the issue body: the description (`<!-- forge:description-begin -->` and `-end`) and the metadata block. Text outside them is kept.
- **Closed issues** are never modified.
- **Started tasks** (`in-progress`, `handoff` or `in-review`) get only title, description and `done_when` updates. Any other change (zones, dependencies, size, tier, type, spec, `covers`) is not applied. A warning comment mentions the assignees.
- **Removed tasks:** not started means closed as "not planned" with a comment. Started means a warning comment, and the issue stays open.
- Finally, it recomputes statuses with the refresh logic.
- Two applies at once converge: duplicates are closed in favor of the lowest issue number.

The `forge-plan-apply` workflow runs validate and apply when a change to `plans/M<n>.yml` reaches the default branch. The lead can also run apply by hand.

### Proposals

During `/forge-dev`, discovered work becomes `forge propose --title T --body-file F`. It creates an issue labeled `status:proposed` (not `forge:task`). Proposing the same open title again returns the existing issue. The lead accepts a proposal by adding a plan task with `proposal: <issue number>`. Apply then turns that issue into the task. To reject one, comment and close it.

## 10. Status and radar

([ADR 0010](decisions/0010-status-report.md), [ADR 0014](decisions/0014-init-adapters-setup.md))

`forge status [--me] [--milestone M]` is read-only. It builds every task's state with the same code as `forge next`, so the queue cannot disagree with the next claim. Sections:

| Section | Content |
|---|---|
| Me | current task, last activity, pending sync (commits behind), drift on my zones, CI of my PR |
| Team | in progress (owner, agent, zones, stale flag), in review, handoffs waiting |
| Queue | ready count and the next 3 picks for me |
| Blocked | by dependencies, by `blocked:conflict` |
| Milestone | done/total, waves, AC coverage |
| Health | CLI vs `forge_version`, profile constraints, newer presets, unknown labels, issues without metadata, proposals awaiting the lead |

It exits 0 whenever the report is produced. It exits 5 without `.forge/config.yml`, without `gh` login, on a shallow clone, or with an unknown `--agent`.

**Drift** is the list of upstream commits, merges excluded, that touch a file in one of the task's zones.

**Radar.** `forge refresh --radar` runs after the normal refresh, from the `forge-radar` workflow on each push to the default branch and hourly. For each in-progress task it keeps at most one comment (`<!-- forge:radar -->`). The comment lists upstream commits that touched the task's zones ("sync recommended") and a stale lease ("reclaimable with `forge reclaim <n>`"). Forge updates it only when its content changes and deletes it when nothing is left to report. It never mentions anyone.

## 11. Release

([ADR 0015](decisions/0015-release-docs.md))

`forge release <milestone> --version X.Y.Z [--dry-run]` runs in two steps. You run the same command both times. Forge never pushes to the default branch.

**Checks.** The version must be a valid release number without a `v` prefix. Without `--version`, the command exits 5 and the hint suggests the next version. Open tasks in the milestone are listed and exit 5. A shallow clone exits 5. An unknown milestone exits 5. A version that already exists as a tag, a changelog heading or a GitHub release for another milestone exits 5 ("already released for <other>"). A user who is not the milestone's lead gets a warning only.

**Step 1: release PR.** When `CHANGELOG.md` on the default branch has no `## [X.Y.Z]` section, Forge builds one from the PRs merged from `task/<n>` branches of the milestone. Lines are grouped by task type (`### Contracts`, `### Features`, `### Tests`, `### Chores`). Forge commits it on `release/vX.Y.Z`, opens the PR `chore(release): vX.Y.Z` and exits 0 with `data.step` set to `pr_opened`. The release branch always holds one Forge commit, marked with the trailer `Forge-Release-Notes: vX.Y.Z`. Re-runs reuse the branch and PR and regenerate the section if it differs. If someone edited the section through the PR, the branch tip is no longer a marked Forge commit: Forge keeps the branch, warns, and lists merged task PRs that the section lacks. Forge pushes the release branch with your pre-push hook enabled. A hook that refuses the push exits 2.

**Step 2: tag.** After the PR is reviewed and merged, the same command finds the section. It creates the annotated tag `vX.Y.Z` on the commit that landed the section: the release PR's merge commit, its squash commit, or the last commit of a rebase-merge. If no merged release PR exists (a manual merge), it finds the commit where the heading first appeared on the default branch. It creates a GitHub release with the section as its notes, closes the GitHub milestone and exits 0 with `data.step` set to `released`. A tag that already exists on that commit is fine. A tag on another commit exits 5.

## 12. Delegation

([ADR 0017](decisions/0017-implementer-subagent-and-delegation.md))

Delegation decides whether the main agent writes a task's code itself or acts as lead and hands the code to the `forge-implementer` subagent (tier `standard` by default). The CLI never calls a model. It decides the mode, tells the agent and records the decision.

- **Modes.** `off`: the main agent writes the code. `auto` (default): delegate when the task has `size: M` or two or more zones. `on`: always delegate.
- **Where the mode comes from**, most specific first:
  1. `--delegation off|auto|on` on `forge task show` and `forge next`. The `/forge-dev` flags `--solo` and `--delegate` map to `off` and `on`. The choice is saved in the worktree's `.forge/local/current.json` and applies to the task until it is released or handed off. A new `--delegation` replaces it.
  2. The profile field `delegation`, set with `forge profile set delegation=<mode>` or by `forge setup`.
  3. The `delegation` section of `.forge/config.yml` (`default`, `allowed`).

  A mode the project does not allow falls back to the project default, with a warning.
- **Capability.** The mode is forced to `off`, with a note, when the active agent's tool is not `claude-code` or `codex`, when there is no profile, or when the machine-level subagent files are missing (`forge adapters generate --local`).
- **Output.** The task context has a `delegation` object (`mode`, `requested`, `source`, `delegate`, `reason`, `subagent`, `model`). `forge ship` writes one line in the PR block's Agent section. `forge status` shows the mode for the current task. The PR line records the decision. Forge cannot verify what the agent did.
- **Lead path.** When `delegate` is true, the `/forge-dev` prompt makes the main agent explore, split the work into briefs (goal, files, `done_when` items, constraints, tests; disjoint files when parallel), delegate each brief, verify the diff and the checks, and take a brief over after two failed rounds. The implementer never claims, syncs, ships, pushes or comments.

## 13. Cross-cutting rules

- **Version pin.** `.forge/config.yml` sets `forge_version`, a PEP 440 specifier. Forge compares the CLI's base version, so `0.1.0.dev0` satisfies `>=0.1,<0.2` ([ADR 0003](decisions/0003-version-compatibility.md)). A mismatch is a warning in `setup`, `status`, `escalate`, `propose`, `plan validate`, `sync --abort` and `review` without `--submit`. It is exit 5 in `next`, `reclaim`, `sync` (except `--abort`), `ship`, `plan apply`, `release` and `review --submit`.
- **Models.** Repository files speak in tiers and effort. The profile in `~/.config/forge/profile.yml` (`%APPDATA%\forge\profile.yml` on Windows) maps them to concrete models. A task's tier picks the model, `config.models.min_tier_for` can raise it, and manual overrides can only raise it. An agent serves a tier with the lowest mapped tier at or above it ([ADR 0005](decisions/0005-model-resolution-and-constraints.md)).
- **Identity.** The claim login is the authenticated GitHub user. If the profile's `github` differs from the `gh` login, claims stop with exit 5.
- **Credentials.** Forge never stores credentials. The profile holds labels only.
- **Exit codes.** `0` ok, `1` error, `2` agent action needed, `3` nothing available, `4` escalation required, `5` precondition failed. The table per command is in [architecture.md](architecture.md#exit-codes).
