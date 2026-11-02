# 0010 — `forge status`: sources, scope and section rules

- Status: accepted
- Date: 2026-11-02

## Context

Spec §11.3 lists the sections of `forge status` (Me, Team, Queue, Blocked, Milestone, Health) but leaves open:
- which milestone the Milestone section shows when `--milestone` is not given;
- what `--me` keeps;
- how "drift", "pending sync" and "CI state" are computed;
- what counts as an "unknown label" or a "proposal awaiting the lead";
- what makes the command fail.

## Decision

**Read-only, one snapshot.** `forge status` never writes to GitHub or pushes. It fetches the default branch and the `task/*` branches, as `forge next` does. It then builds every task's state with the same code as `forge next`: derived status (ADR 0008) and claim, last activity and staleness (ADR 0009). Labels are never trusted for status. The snapshot logic in `core/claim.py` becomes a public function that both commands share. Status reads the exact last activity of every claim (lease comment included), not the tip-date shortcut that `next` may use.

**Scope.**
- `--milestone M` limits Team, Queue and Blocked to tasks of milestone `M`, and picks `M` for the Milestone section.
- Without it, Team, Queue and Blocked cover all open tasks. The Milestone section shows, in this order:
  1. the milestone of my current task;
  2. otherwise the first open GitHub milestone, in natural order (`M2` before `M10`), that has `forge:task` issues;
  3. otherwise nothing (`null`).
- `--me` keeps only the Me and Queue sections. In JSON, omitted sections are absent from `data.sections`, and their keys are `null`.

**Me.**
- *Who*: the profile's `github` login when a profile exists, otherwise `gh`'s current user. *Agent*: the active agent (`--agent`, `FORGE_AGENT`, `default_agent`), or `null` without a profile.
- *Current task*: found from the working directory (`find_current_task`: `.forge/local/current.json`, or the `task/<n>` branch checked out). Shown with its status, branch, worktree, claim owner/agent, last activity, stale flag and `stale_at`.
- *My other claims*: tasks whose claim names my login, plus my open task PRs. Without a current task, these are what Me shows.
- *Pending sync*: `behind` is the number of commits on `origin/<default>` that are not in the task branch (local branch if present, else `origin/task/<n>`). Pending sync is `behind > 0`. `unpushed` counts local task-branch commits not on `origin/task/<n>`.
- *Drift*: the commits in that `behind` range (merges excluded) that change a file matching a glob of one of the task's zones. The report lists up to 10, newest first, with the matched zones, and gives the total. Unknown zones produce a warning and are ignored.
- *CI*: the state of my task's open PR head commit (`GitHubClient.commit_ci`). It combines check runs and commit statuses into `success | failure | pending | none`. Any failure wins, then anything still running, then `none` when nothing reported.

**Team.**
- *In progress*: derived `in-progress` tasks, with owner, agent, zones, last activity and stale flag. Stale claims stay listed, flagged.
- *In review*: open PRs from task branches, with PR number, URL, author and draft flag.
- *Handoffs waiting*: derived `handoff` tasks, with the newest handoff note's author and date.

**Queue.**
- `ready` is the number of derived `ready` tasks in active milestones.
- `next` holds the first 3 candidates that `forge next` would select for the active agent, with the same rules (tier, zones, holders) and no claim. Without a profile, `next` is empty and a warning says to run `forge setup`.

**Blocked.**
- *By dependencies*: derived `blocked` tasks, with the open dependencies each waits on.
- *By conflict*: open tasks labeled `blocked:conflict`.

**Milestone.**
- `done/total` counts the milestone's tasks. Tasks closed as `not_planned` are `dropped`: they count in neither number and are reported separately.
- *Waves*: the dependency waves of the milestone's tasks. Dependencies on tasks of other milestones count as satisfied when those tasks are closed. Each wave gets `done/total` and its task ids.
- *ACs*:
  - `covered`: the AC ids in the `covers` of done tasks.
  - `total`: the AC ids defined in the spec files that the milestone's tasks reference, plus every AC id any task covers.
  - `uncovered`: the rest.
  - A missing spec file is a warning.

**Health.**
- *Version*: CLI version vs `forge_version`, compared with the base-version rule of ADR 0003. A mismatch is a warning, not exit 5: status is the diagnostic tool.
- *Profile*: no profile → a warning. Otherwise, the violations of `check_profile` (ADR 0005).
- *Presets*: `preset_drift` entries, shown as "newer presets available".
- *Unknown labels*: labels on open `forge:task` issues that start with a managed prefix (`status:`, `size:`, `tier:`, `type:`, `blocked:`) but are not in the label catalogue.
- *Issues without metadata*: open `forge:task` issues whose block is missing or invalid, with the parse error.
- *Proposals*: open issues labeled `status:proposed`, with or without `forge:task`.

**Exit codes.**
- `0` whenever the report is produced, with or without warnings.
- `5` when the repository has no `.forge/config.yml`, `gh` is not authenticated, the clone is shallow, or `--agent`/`FORGE_AGENT` names an agent the profile does not have.
- `1` for other errors, such as a failed fetch.

**Snapshot tests.** Status JSON is compared against committed snapshot files for one seeded scenario: in progress, stale, blocked, in review, handoff, and a ready queue. Volatile values (shas, temp paths) are replaced by stable placeholders before the comparison. `FORGE_UPDATE_SNAPSHOTS=1` rewrites the snapshot files.

**CI read failures.** If reading CI fails, status still succeeds. The PR's `ci` is `null` and a warning names the PR and the error. Fine-grained personal access tokens cannot read check runs, so this case is expected.

## GitHub API notes (checked 2026-11-02, REST API 2022-11-28)

- **Check runs**: `GET /repos/{owner}/{repo}/commits/{ref}/check-runs` (https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference).
  - Forge asks for `filter=latest` and `per_page=100`, merges all pages, and keeps the highest-id run for each (app, check suite, name).
  - `status` values: queued, in_progress, completed, waiting, requested, pending.
  - `conclusion` values: success, failure, neutral, cancelled, skipped, timed_out, action_required, null.
  - Forge also treats `startup_failure` as failing and `stale` as passing. The response schema lists neither value, but GitHub can set them.
  - Only the newest 1000 check suites of a ref are listed.
- **Commit statuses**: `GET /repos/{owner}/{repo}/commits/{ref}/status` (https://docs.github.com/en/rest/commits/statuses#get-the-combined-status-for-a-specific-reference).
  - Forge uses the `statuses` list and keeps the highest id for each context.
  - It ignores the top-level `state`, which is `pending` when there are no statuses at all.
  - Status states: error, failure, pending, success.
- **Combination** (`combine_ci` in `core/github.py`), first match wins:
  1. A failing conclusion or an error/failure status gives `failure`.
  2. An unfinished run or a pending status gives `pending`.
  3. Nothing reported gives `none`.
  4. Otherwise the state is `success`.
- **Permissions**:
  - The `repo` scope covers both endpoints. Default `gh auth login` tokens have it.
  - Fine-grained PATs can read commit statuses ("Commit statuses: read") but have no permission for check runs. That is why read failures degrade instead of failing the command.
- **Renames in drift**: `Git.commits_with_files` uses `--no-renames`, so a renamed file counts under both its old and its new path.

## Consequences

- Status costs one comment read for each in-progress and handoff task, plus one CI read for my PR. That is acceptable at v0 team sizes.
- Because `next` and `status` share the snapshot code, the Queue cannot disagree with what `forge next` would pick.
- Status needs network access to fetch. An offline mode is not part of v0.

## Implementation notes (Phase 4)

- **Queue**: `ready` leaves out tasks labeled `blocked:conflict`, as `forge next` does.
- **CI**: it is read for every open task PR of mine, not only the current task's.
- **Waves**:
  - The milestone's own dependency edges are always kept.
  - A dependency on an open or unknown task outside the milestone counts as unmet.
  - A dependency on a dropped task counts as met.
- **ACs**:
  - Spec files are read from the working tree at the repository root, as `forge task show` does.
  - The ACs of a dropped task stay in the total, so they show as uncovered until another task covers them.
- **Version warning**: a version mismatch is reported in `warnings` even with `--me`, which hides Health.
- **Unpushed commits**: when `origin/task/<n>` is missing, they are counted against `origin/<default>`.
- **Health**: it lists profile violations for every agent in the profile, not only the active one.
- **Proposed tasks**: in the Milestone section they are listed apart, like dropped tasks, and count in neither `done`, `total` nor waves. The lead has not accepted them yet. Their ACs still count in the AC total, as for dropped tasks.
- **Queue without zones**: when `.forge/zones.yml` is missing or invalid, `next` is empty with a warning, because `forge next` would refuse to run.
- **Queue picks**: `next` lists the first 3 candidates, exactly as `forge next --dry-run` would. Two of them can overlap by zone, so claiming one may rule out the other.
- **Check runs**: they are reduced per (app, check suite, name), not per name alone. Two workflows with a job of the same name each count.
- **Rendering**: the human output never reads user text (titles, subjects, labels, paths) as rich markup.
