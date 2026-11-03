# 0011 — `forge sync`, conflict classification, resolvers and escalation

- Status: accepted
- Date: 2026-11-03

## Context

Spec §10 describes `forge sync` in six steps and `forge escalate` in one line. Several details are left open:
- where sync runs and what it requires;
- what happens to Forge's claim commit when the task branch is rebased;
- how the categories combine;
- what the mechanical resolvers do exactly;
- what `--continue` and `--abort` restore;
- who escalation mentions;
- what clears `blocked:conflict`;
- the exit codes.

## Decision

### Where and who
- `forge sync` runs on the current task (`find_current_task`: the worktree's `current.json` or the checked-out `task/<n>` branch).
- It requires:
  - my own **live** claim on that task (`require_own_claim`, live, ADR 0009);
  - a valid `.forge/zones.yml`;
  - a full (non-shallow) clone.
- If any of these is missing, sync exits 5. A stale own claim hints at `forge next --task <n>`.

### State files (in `<worktree>/.forge/local/`)
- **`sync.json`** exists while a sync is in progress, stopped on conflicts or on failing checks. It holds:
  - task, issue and branch;
  - `started_at`;
  - `backup_ref`, `original_tip` (before the wip commit), `wip` (bool) and `remote_tip` (the `origin/task/<n>` sha at preflight, used as the push lease);
  - `onto` (the `origin/<default>` sha);
  - `force_resolve` (bool);
  - the classification;
  - the upstream context;
  - `conflicts`: path, zone, contract, how it was resolved so far;
  - `state`: `conflicts` or `checks_failed`;
  - `checks`: each check's name, command, exit code and an output tail.
- **`sync-log.json`** is a list with one entry per completed or escalated sync. Each entry holds `at`, `onto`, `head`, `upstream` (count), the classification, the conflicts with their resolution (`<resolver>` or `manual`), `force_resolve`, `checks_passed`, `checked_head` and `outcome` (`pushed`, `up_to_date` or `escalated`). `forge ship` (Phase 6) reads it for the PR body and to skip re-running checks on the same HEAD (B7).

### Preflight
1. If `sync.json` exists, a sync is already in progress: exit 5 with a hint to use `--continue` or `--abort`. A rebase in progress with no `sync.json` is also exit 5: it was not started by Forge.
2. If the tree is dirty (untracked files included; `.forge/local/` is ignored), Forge stages everything and commits `forge: wip`. Commit hooks run, since this is user code. If a hook rejects the commit, sync exits 2 with the hook output.
3. Fetch `origin/<default>` and `origin/task/<n>`.
4. Save the backup ref `refs/forge/backup/task-<n>/<UTC timestamp>` pointing at HEAD. It is local only and never pushed.

### Analysis (no LLM)
- `base` is the merge base of HEAD and `origin/<default>`.
- Upstream commits are those in `base..origin/<default>`, merges excluded, with their files.
- Task files are those changed in `base..HEAD`.
- `I` is the intersection of upstream files and task files.
- The categories (all that apply are reported):
  - `textual`: `I` is not empty.
  - `out_of_zone`: some file in `I` matches none of the task's zones.
  - `semantic`: an upstream file lies in a `contract: true` zone that the task depends on. The task depends on a contract zone if the zone is listed in its own zones, or in the zones of a task named in its `depends_on`.
  - `none`: none of the above.
- **Primary category**, by precedence: `out_of_zone` > `semantic` > `textual` > `none` (kickoff item B6).
- **Upstream context**: up to 20 relevant upstream commits, meaning those touching `I` or a semantic contract zone. Each is mapped to its pull requests (`GitHubClient.commit_pulls`). Each PR is mapped to issues, from closing keywords (`close[sd]?`, `fix(e[sd])?`, `resolve[sd]?` followed by `#n`) and from a `task/<n>` head. A failed lookup is a warning.

### Rebase and the claim commit
- Sync rebases onto `origin/<default>` with rerere on (`-c rerere.enabled=true -c rerere.autoupdate=true`), keeping commits that start empty (`--keep-empty`), with `--committer-date-is-author-date`.
- **Claim commit (amends ADR 0009)**: the rebase rewrites the claim commit, so its sha changes.
  - **Same committer date**: the claim commit keeps its committer date, because claim commits are made with identical author and committer dates. Claim order (ADR 0009) is therefore unchanged.
  - **Lease identity**: the lease comment gains an optional `run` field, the claim's `Forge-Run` trailer.
  - **Matching rule (new)**: a lease names a claim when `lease.claim == claim.sha` or, when both have one, `lease.run == claim.run`. A rebased claim therefore stays "complete", and its heartbeats keep counting, even before the lease is rewritten. After the push, sync rewrites the lease with the new claim sha (keeping `claimed_at`) and refreshes `heartbeat_at`.
  - **`current.json`**: its `claim` field is updated too.
  - **New claims** write `run` from now on. Old lease comments without `run` match by sha only.
- **Behind count zero**: when `origin/<default>` has no new commits, there is no rebase.

### Conflicts
- **Resolvers first**: at each rebase stop, every conflicted path matching a `resolvers` glob is resolved mechanically, in config order, first match wins:
  - `regenerate`: take the upstream side. During a rebase that is stage 2 (`--ours`), i.e. the branch being rebased onto. Then run `command` in the worktree root with a shell, and stage the path. If the command fails, the path stays conflicted and the failure is reported.
  - `union`: `git merge-file --union` over stages 1, 2 and 3. A missing base stage counts as empty. The result is staged.
- When every conflict of a stop is resolved, Forge continues the rebase (no editor) and repeats.
- **Escalation check**: if conflicts remain and any of them is outside the task's zones, or in a `contract: true` zone the task does not own (see the implementation notes), and `--force-resolve` is not set, sync escalates (below) and exits 4.
- **Otherwise**: Forge stops with the rebase in progress, writes `sync.json` (`state: conflicts`) and exits 2. The agent resolves the files following the `/sync` rules, stages them, and runs `forge sync --continue`.

### Escalation (exit 4)
1. Abort the rebase and restore the pre-sync state, as `--abort` does.
2. Add `blocked:conflict` to the task issue.
3. Comment on the task issue (marker `<!-- forge:escalation -->`). The comment lists the conflicted files, the upstream commits, PRs and issues, and mentions the authors of those PRs.
4. Comment on each linked upstream issue, mentioning me.

The sync is logged with `outcome: escalated`, and `sync.json` is removed.

**`forge escalate --reason R`**:
- adds `blocked:conflict` to the current task's issue;
- comments with the reason and the same marker;
- mentions the upstream PR authors from the current `sync.json` (or the last sync-log entry, if any), plus the members with role `lead` in `.forge/team.yml`, excluding me.

It exits 0: the escalation was done. It needs a current task, but not a live claim.

**Clearing**: a sync that pushes removes `blocked:conflict` from the task issue (ADR 0008 lets only sync and escalate touch it).

### `--continue`
1. It requires `sync.json`. Otherwise it exits 5.
2. **Rebase in progress**:
   - Unmerged paths left → exit 2, listing them.
   - Otherwise Forge continues the rebase, and any new stop goes through the resolvers and the escalation check again, honouring the recorded `force_resolve`.
   - Paths that were conflicted and are now resolved without a resolver are recorded as `manual`.
3. **No rebase in progress, `state: checks_failed`**: a dirty tree exits 5 with a hint to commit the fixes.
4. **Verify**: run `config.checks` in config order, with a shell, in the worktree root, and record the output tails. Any failure → `state: checks_failed`, exit 2.
5. **Push** (also reached by a plain sync after a clean rebase): push `task/<n>` with `--force-with-lease` against `remote_tip`.
   - The user's pre-push hooks run.
   - A hook rejection → exit 2.
   - A lease rejection, meaning the branch moved on origin (e.g. reclaimed) → exit 5.
6. **After the push**:
   - refresh the lease (above);
   - remove `blocked:conflict`;
   - append to `sync-log.json`;
   - post a summary comment (marker `<!-- forge:sync -->`), only when conflicts were resolved;
   - remove `sync.json`.
- **Nothing to push**: when HEAD equals `origin/task/<n>` and no rebase happened, checks are skipped. The outcome is `up_to_date`, exit 0.

### `--abort`
1. It requires `sync.json`. Otherwise it exits 5.
2. Abort the rebase, if one is in progress.
3. `reset --hard` to the backup ref, which restores the exact pre-sync tip including the wip commit.
4. If a wip commit was made, `reset --mixed` to `original_tip`: the wip changes come back as uncommitted changes. Untracked files come back untracked. See the implementation notes for the dirty-tree and moved-HEAD rules.
5. Remove `sync.json`.

The backup ref is kept.

### Exit codes
| Code | When |
|---|---|
| 0 | synced and pushed, or already up to date; `forge escalate` done |
| 2 | conflicts left for the agent, failing checks, a hook rejected the wip commit or the push, unmerged paths on `--continue` |
| 4 | escalation: out-of-zone or contract conflict without `--force-resolve` |
| 5 | no current task, claim not mine or not live, invalid zones, shallow clone, sync already in progress, no sync in progress (`--continue`/`--abort`), dirty tree on `--continue` after failed checks, rebase not started by Forge, push lease rejected |
| 1 | other git/GitHub failures |

## GitHub API notes (checked 2026-11-03)

- **Endpoint**: `GET /repos/{owner}/{repo}/commits/{commit_sha}/pulls` (https://docs.github.com/en/rest/commits/commits#list-pull-requests-associated-with-a-commit). The docs page's current API version is 2026-03-10.
- **What it lists**: for a commit on the default branch, only the merged PR that introduced it. For any other commit, the open and merged PRs that contain it.
- **Paging**: `per_page` at most 100; Forge paginates.
- **Response**: "Pull Request Simple" objects. They have no `merged` boolean, so Forge derives it from `merged_at`. `mergeable` is absent.
- **Permissions**: the docs page states none. Forge assumes read access to contents and pull requests, which default `gh` tokens have. A failed lookup is a warning and never fails sync.

## Implementation notes (Phase 5)

- **Hook vs lease rejection**: a pre-push hook rejection raises `PushHookRejected` and exits 2. A lease rejection returns `PushResult(rejected=True)` and exits 5. git runs the hook even when the lease is stale, so a push with both problems reports the hook (exit 2).
- **Rerere**: with rerere on, a rebase can stop with every conflict already resolved and staged by rerere. Sync then continues without involving the agent, unless the escalation check applies to those paths (see below).

- **Recoverable at every step**: `sync.json` is written before the rebase, with the state `running`, so `--continue` or `--abort` can recover an interrupted sync at any point. A pre-push hook rejection sets the state `push_rejected`.
- **Incomplete claim**: sync refuses a claim the lease does not name with exit 5, as heartbeat does. Refreshing the lease after the push must never complete an unverified claim.
- **Diverged branch**: the local branch must contain `origin/task/<n>`. Otherwise sync exits 5, because the forced push would drop remote commits.
- **Sync log**:
  - Each entry has an `id`, the backup ref, so a re-run replaces its entry instead of adding one.
  - Each entry keeps the `upstream_context`, so `forge escalate` finds upstream authors after `sync.json` is gone.
  - An `up_to_date` sync renews the heartbeat but leaves `blocked:conflict` alone.
- **No duplicate comments**:
  - Escalation comments are keyed on the issue, the upstream tip and the original task tip, so repeated syncs don't post twice.
  - `forge escalate` skips the comment when the newest escalation has the same reason and mentions.
- **Resolver limits**: `union` leaves a path conflicted when one side deleted the file. A failed `regenerate` leaves the upstream version in the working tree, still unmerged.
- **Owned contracts**: a conflict in a contract zone escalates unless the task owns that zone. A task owns a contract zone that is one of its zones when it is `type: contract` or sets `allow_contract_change: true`, the same exception as plan validation (spec §9.4). This follows the §10.3 rule "never change a contract the task does not own".
- **Known limit**: if the process is killed between the wip commit and the write of `sync.json`, a window of a few local git calls, `--abort` cannot undo the wip commit. No work is lost; the wip commit stays on the branch.
- **Mixed Forge versions**: lease comments now carry `run`, so mixed-version teams rely on the `forge_version` pin.

- **Command flags**:
  - `--continue` and `--abort` cannot be combined, and `--force-resolve` works only with a plain sync. A wrong combination is a usage error (exit 1).
  - `--continue` uses the `force_resolve` value recorded when the sync started.
- **`--abort` never blocks a restore**: a version mismatch or an unreadable `zones.yml` (it can hold conflict markers mid-rebase) is only a warning there.
- **`forge escalate`** does not load the profile, so `--agent` has no effect on it.

- **Rerere resolutions** are recorded with the resolution `rerere` and go through the same escalation check as any other conflict. A resolution replayed from an earlier `--force-resolve` sync therefore still escalates in a plain sync.
- **Order of steps**: sync does every GitHub read the analysis needs before the wip commit, and writes `sync.json` (state `running`) right after the backup ref. A GitHub failure therefore leaves nothing to undo, or a sync that `--abort` can undo.
- **`--abort` keeps your work**:
  - With no rebase in progress and a dirty tree, `--abort` exits 5 ("commit or discard your changes first").
  - When HEAD has moved past the backup, for example after fix commits, it saves HEAD under `refs/forge/backup/task-<n>/<ts>-aborted` before restoring, and reports that ref.
  - `--abort` needs neither a profile nor a readable `.forge/config.yml`, which can hold conflict markers mid-rebase.
- **Staged changes**: restoring keeps every uncommitted change, but the staged/unstaged split is not preserved.
- **Backup refs**: Forge keeps the 10 newest backup refs per task, and never prunes the one an in-progress sync uses. It never prunes `-aborted` refs: they can be the only ref to commits that an abort or an escalation removed from the branch.
- **Moved HEAD**: abort and escalation save a moved HEAD under an `-aborted` ref even when a rebase was in progress. This covers a rebase restarted by `--continue` on top of new commits. The ref is reported in the outcome and in the escalation comment.
- **Abort mid-rebase**: `--abort` during a rebase warns when it drops edits made while resolving conflicts. The warning lists every path that differs from HEAD, which always includes the stop's conflicted paths.
- **No self-mentions**: escalation never mentions the user running it, including on upstream issues they own.

- **Detecting rerere resolutions**: they are read from git's own output under `LC_ALL=C` (`Staged '<path>' using previous resolution.`). Forge keeps only paths that are staged and no longer unmerged. Diffing the stopped commit cannot tell a rerere resolution from a clean merge.
- **Resolvers before rerere**: rerere also remembers what the resolvers produced. A path matching a resolver therefore gets its conflict back (`git checkout -m`) and the resolver runs again. If git cannot restore the conflict, the path is recorded as `rerere` and does not escalate.
- **Task lookup on `--abort`**: it takes the task from `sync.json`, because the current-task lookup can fail mid-rebase (detached HEAD).
- **Skipped upstream issues**: an issue where I authored a linked PR or am an assignee gets no escalation comment. It is listed in `upstream_skipped`.

## Consequences

- A sync rewrites the task branch, so collaborators who fetched it need to reset. Task branches have one owner, so this is expected.
- Rerere is passed per command, so sync works before `forge setup` (Phase 8) sets it in the repo config. Once `.git/rr-cache` exists and `rerere.enabled` is unset, git turns rerere on for all of the user's merges and rebases in that clone. `forge setup` enables it explicitly anyway.
- `--committer-date-is-author-date` also applies to the user's commits. The tip's committer date can then be older than the sync. The lease heartbeat refreshed right after the push keeps the claim live.
