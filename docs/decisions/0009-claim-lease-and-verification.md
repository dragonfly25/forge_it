# 0009 — Claim, lease and post-claim zone verification

- Status: accepted
- Date: 2026-11-02

## Context

Spec §8.1–§8.4 define the claim (a compare-and-swap push to `task/<n>`), the lease, the
post-claim zone verification, reclaim and release. Building them exposes these gaps:

1. The verification rule in §8.1 ("if another live claim overlaps by zone and its claim commit
   is earlier, release") is **unsafe**. Take A, which pushes and verifies before B pushes, so A
   never sees B. B then pushes and sees A. If B's claim commit has the earlier timestamp, B keeps
   its claim. A has already kept its own, so both now hold overlapping zones.
2. "Active milestones" and "milestone order (roadmap)" are mentioned, but the roadmap model only
   arrives in Phase 7.
3. ADR 0007 left two questions open:
   - Re-running a claim whose push landed before an interruption looks like a lost race.
   - Should Forge's own commits and pushes bypass user git hooks?
4. Several points are unspecified:
   - the format of the lease comment, and which timestamp counts as activity;
   - which login goes into `Forge-Claim`;
   - exit codes for the different "cannot claim" cases;
   - what `release-claim` writes on the branch;
   - whether in-review tasks hold their zones.

## Decision

### Live claim and activity
- A task has a **live claim** when its branch exists, its last Forge event (ADR 0008) is
  `claim`, and the claim is not stale.
- The **claim commit** is the commit that carries that last `Forge-Claim` event.
- **Last activity** is the later of:
  - the committer date of the branch tip;
  - `heartbeat_at` in the lease comment, but only when the lease comment's `claim` field equals
    the claim commit sha. A heartbeat left over from an earlier claim never extends a later one.
- **Stale** means last activity is more than `lease_hours` ago.
- The clock is injectable, and all timestamps are UTC ISO 8601 with a `Z` suffix.

### Lease comment
- There is one comment per issue. It starts with `<!-- forge:lease -->`, followed by a short
  human summary (owner, agent, claimed, last heartbeat, branch).
- Machine data sits on a final line, `<!-- forge:lease-data {json} -->`, with the keys `task`,
  `owner`, `agent`, `claim` (sha), `claimed_at`, `heartbeat_at` and `released_at` (null unless
  released).
- When several comments carry the marker, the **newest** one is authoritative. Forge edits it in
  place. If the edit fails (for example a 403 on another user's comment), Forge creates a new
  comment, which then becomes the newest.

### Who claims
- The claim login is the authenticated GitHub user (`GitHubClient.current_user()`). If the
  profile's `github` field differs from it, Forge stops with exit 5, naming both logins.
- `Forge-Agent` is the active agent id from the profile (`--agent` or `default_agent`).
- `forge next` and `reclaim` check the CLI version against `forge_version` and exit 5 on a
  mismatch.
- Profile violations of `config.models` are reported as warnings and never block a claim.

### Selection (`forge next`)
- **Active milestones** are the open GitHub milestones. An issue with no milestone counts as
  active, and its task id gives its milestone.
- **Candidates** are tasks whose derived status (ADR 0008) is `ready` or `handoff`. With
  `--reclaim`, an `in-progress` task whose claim is stale is a candidate too. A task is excluded
  when:
  - an open PR exists from its branch;
  - it carries `blocked:conflict`;
  - its zones overlap a **zone holder**, which is any task with a live claim or an open PR.
    In-review work is unmerged, so it keeps its zones even after its lease goes stale.
- **Order**:
  1. `handoff` before everything else;
  2. milestone, in natural order of the id prefix (M0 < M1 < M10). The roadmap order replaces
     this in Phase 7.
  3. wave (the topological level over all parsed tasks; on a cycle, all waves count as 0, with
     a warning);
  4. size, S before M;
  5. issue number.
- Tasks whose tier the agent cannot serve (ADR 0005) are skipped unless `--any-tier` is given.
- `--task <M1-T07|#42|42>` restricts selection to that task, under the same checks. If the task
  is not claimable, Forge exits 3 with the reason.
- With no candidate left, or after `claim.max_attempts` lost races, Forge exits 3. The `data`
  then counts the exclusions by reason.
- `--dry-run` prints the ordered candidates and pushes nothing.

### Claim push
- Forge builds the claim commit with `commit-tree`. It is empty: its tree is the parent's tree.
- **New claim**: the parent is `origin/<default>`, and the lease expects that the ref does not
  exist.
- **Resume** (handoff, release or reclaim): the parent is the current tip, and the lease
  expects that tip.
- The message and trailers follow §8.1.
- Forge takes the pre-push snapshot of live claims (see below) **before** it creates the claim
  commit.

### Idempotent own claim (ADR 0007 open item)
- When the last event of a live claim names **my login and my agent id**, the claim is mine.
- If its lease comment does not yet reference that claim commit, a previous run stopped after the
  push. `forge next` then resumes that task first: it skips the push and completes the
  post-claim steps.
- If the lease comment already references the claim commit, the claim is deliberate and complete,
  and `next` does not offer the task again. It says "you already hold #n". `next --task` on that
  task completes the post-claim steps again, which is idempotent, and exits 0.
- If a push reports a lost race but the remote tip is my claim commit, the claim is treated as
  won. `Git.push` already does this (ADR 0007).

### Post-claim zone verification (safe variant of §8.1)
After a successful push, Forge lists the live claims again (snapshot S2). Each claim C there that
belongs to another task and overlaps my zones is a conflict. The **order** between two claims is
the committer date of the claim commit, then the issue number.
- If C comes before my claim in that order, I **yield** at once.
- If my claim comes first, I **wait for C to go away**. Forge polls every 2 s, five times at most
  (10 s in total; the interval and limit are injectable). C has gone away when it is no longer a
  live claim on that commit. If C is still live after the last poll, I yield.

This is safe without any timing assumption. Of two overlapping claims, the one pushed later
always sees the earlier one. If the later one comes first in the order, it keeps its claim only
once the earlier one has disappeared. Otherwise it yields. So at most one keeps the zones. When
both see each other, exactly one keeps: the other yields at once, and the first one's wait ends.

To **yield**:
- a new claim deletes the branch with a lease on my claim sha;
- a resumed claim pushes the previous tip back, with a lease on my claim sha.

After yielding, Forge moves on to the next candidate. The yielded task is not retried in the same
run.

The lease comment, assignment and labels are written only **after** verification succeeds.

### Post-claim steps
1. Assign the issue to me. On a reclaim, also unassign the previous claimant.
2. Set the labels for `in-progress`.
3. Upsert the lease comment. `claim` is set to the claim sha, and `claimed_at` and
   `heartbeat_at` are set to now.
4. On a reclaim, post a comment that names the previous claimant and when their claim went stale.
5. Create the worktree, or check out the branch with `--no-worktree` or when
   `worktrees.enabled: false`.
6. Write `.forge/local/current.json`.

The worktree path is `<profile.worktrees_dir or <parent-of-repo>/<repo>-worktrees>/task-<n>`.
- An existing worktree that is already on `task/<n>` is reused.
- If a local `task/<n>` already exists and is not an ancestor of the claimed tip, Forge leaves it
  untouched and exits 5. The claim stays held, so re-running `next --task` completes the steps.
- Checking out in the main working tree requires it to be clean (exit 5 otherwise).

`.forge/local/` gets its own `.gitignore` containing `*`, so it is ignored even before
`forge init` (Phase 8) adds the rule.

`current.json` holds `{task, issue, branch, base, claim, agent, claimed_at, worktree}`.
- For a new claim, `base` is the `origin/<default>` sha at the time of the claim.
- On a resume, `base` is the merge-base.

### Heartbeat, release, reclaim
- **`forge heartbeat`** applies to the current task, which is found from `current.json`, or from
  the current branch `task/<n>`. It requires a live claim that is mine (exit 5 otherwise) and sets
  `heartbeat_at`.
- **`forge release-claim`** also requires a live claim that is mine. It:
  - pushes an empty `Forge-Release` commit on top of the **remote** tip, with a lease. Local
    commits that were never pushed are not published; Forge warns with how many there are.
  - unassigns me and sets the derived status (`ready` or `blocked`);
  - posts a comment, and sets `released_at` in the lease comment;
  - removes `current.json`.

  With `--delete-branch`, Forge deletes the branch with a lease, but only if every commit since
  the merge-base is a Forge event commit. Otherwise it keeps the branch and warns.
- **`forge reclaim <n>`** behaves like `forge next --task <n> --reclaim`. A fresh claim held by
  someone else exits 3, and the message says when the claim becomes stale.

### Hooks (ADR 0007 open item)
- Forge's event commits (claim, handoff, release) are created with `commit-tree`, so commit hooks
  never run.
- Their pushes use `--no-verify` (`Git.push(..., no_verify=True)`). They carry no user code: the
  tree is the parent's tree.
- Pushes that carry user code, such as the wip commit in handoff, sync and ship, run the user's
  hooks. Phases 5 and 6 revisit this.

### Exit codes
| Code | When |
|---|---|
| 0 | claimed, resumed, heartbeat or release done |
| 3 | nothing claimable, `max_attempts` exhausted, the requested task is not claimable, or reclaim of a fresh claim |
| 5 | version mismatch, profile missing or invalid, profile/gh login mismatch, shallow clone, dirty tree for an in-place checkout, no current task, the claim is not mine, a local branch that diverged |
| 1 | anything else (GitHub/git failures) |

## Consequences

- Concurrency safety does not depend on clocks. Clocks affect only staleness and which of two
  simultaneous claimants keeps its claim.
- A losing claimant can wait up to about 10 s before moving on. Tests inject the sleep.
- The roadmap order (Phase 7), the `forge status` stale view (Phase 4) and `forge handoff`
  (Phase 6) build on this. Handoff must write `Forge-Handoff` with `no_verify` on the event push.
- A pull request that stays open keeps its zones reserved however long it waits for review.

## Implementation notes (Phase 3)

- The lease comment is written **last** and marks the post-claim steps as complete. On a reclaim,
  the reclaim comment is posted before the lease comment, so a re-run after an interruption still
  posts it.
- A claim that a previous run pushed but never finished is checked against overlapping claims
  again before its post-claim steps are completed. Otherwise a claim pushed but never verified
  could keep zones that overlap another kept claim.
- `release-claim --delete-branch` also requires each Forge event commit to be empty and not a
  merge, because a handoff commit (ADR 0008) may carry work.
- Claims left on closed issues do not hold zones.
- Claim and release commits take their committer date from the injected clock
  (`Git.commit_tree(date=...)`).
- `release-claim` also accepts **my own stale claim** when the branch's last event is still my
  claim, and warns. Releasing never adds a zone holder, so this is safe.
- `heartbeat` **refuses** in two cases, both with exit 5:
  - my own stale claim. While it was stale it held no zones, so someone may since have claimed an
    overlapping task. The hint is `forge reclaim <n>`, which renews the claim through the CAS push
    and verification.
  - a claim whose post-claim steps are not complete (the lease comment does not name its sha). A
    heartbeat would mark it complete without verification. The hint is `forge next --task <n>`.

### Amendment: two runs under the same identity (review of Phase 3)
Login and agent id alone cannot tell an interrupted run from **another run that is still alive**
under the same identity. Two sessions of the same tool with the same account can run at once.
- Before pushing a claim, `next` writes a **pending-claim record** under the git common dir:
  `<git-common-dir>/forge/pending/task-<n>.json` with `{claim, previous, pid, host, started_at}`.
  It removes the record once the post-claim steps are done, or after yielding.
- `forge next` without `--task` takes over my incomplete claim **only** when such a record exists on
  this host, names that claim sha, and its run is gone (its pid is no longer running, or the record
  is older than 15 minutes). Otherwise the task is excluded,
  with the message "an incomplete claim of yours on #n may still be in progress; run
  `forge next --task <n>` to complete it".
- `forge next --task <n>` (an explicit request) always completes my incomplete claim.
- Completing an incomplete claim **always** runs the post-claim verification. If the claim conflicts
  and the branch carries commits after the claim commit, Forge does not roll back, because that work
  must not be lost. It stops with exit 5, names the conflicting task, and leaves the claim
  incomplete. It also drops the pending-claim record, so a plain `forge next` excludes the task
  as `incomplete_own` instead of stopping again. Without such commits it yields as usual.
- Records live per clone. Claims need macOS, Linux or WSL (see the re-review amendment).

### Amendment: re-review of the same-identity rules
- **Unique event commits.** Claim and release commits carry a `Forge-Run: <uuid4 hex>` trailer,
  one id per run. Otherwise two runs under one identity, on one parent and in the same second,
  build byte-identical claim commits, and both "win" the push.
- **Task lock.** Every read-then-write of a pending-claim record holds
  `<git-common-dir>/forge/pending/task-<n>.lock`: the takeover, the write before a fresh claim
  push, removal and pruning.
  - The lock is a `flock` on that file, which holds `{pid, host}` for diagnostics and is removed
    on release. The kernel drops a `flock` when its process dies, so a crashed holder never
    leaves a held lock. An `O_EXCL` file would need a stale-lock break, and that cannot be made
    atomic without a second lock.
  - Under the lock, an automatic takeover re-reads the record and requires that it still names
    the dead pid and the same sha. A run that loses this check excludes the task as
    `incomplete_own`.
  - A fresh claim whose task already has a record from a live run of this clone is not pushed.
    The task counts as a lost race.
  - `next --task` on a task whose lock another run holds exits 3 with "another forge run is
    working on #n right now; retry in a moment".
- **Explicit takeover of a live record.** `next --task` still takes over, and warns with the
  recorded pid, `started_at` and the record's file path.
- **When a run counts as gone.** Its pid is no longer running, or its record is older than
  15 minutes (`MAX_RUN_AGE`), whatever the pid says: a claim run takes seconds plus at most
  about 10 s of verification, and after that the pid may have been reused. Messages about a
  live record name its file.
- **Pruning.** Before claiming, `forge next` (not `--dry-run`) removes records whose sha is not
  the claim commit on the remote branch and whose run is gone, or that come from another machine.
- **Machine id.** `host` in a record is a machine id (uuid4, created once and atomically), not
  the hostname, which can change with the network. It lives outside the clone, in
  `$XDG_STATE_HOME/forge/machine-id` (default `~/.local/state/forge/machine-id`; a relative
  `XDG_STATE_HOME` is ignored). Pids only make sense inside one pid namespace: a container that
  shares the clone but has its own home gets its own id, so the host never reads a live
  container run's pid as dead, and the other way round.
- **Rollback target.** The record also stores `previous`: the remote tip the claim push
  replaced, or `null` when the push created the branch. A run that takes the claim over keeps
  it, and on a yield restores that tip or deletes the branch.
  - Only a record without `previous` (written by an older Forge) falls back to a guess: the
    claim commit's first parent, restored when it is on the task branch, or when its
    first-parent history has a Forge event of the task; otherwise the branch is deleted. The
    guess is wrong when the task's events reached the default branch through a fast-forward
    or rebase merge before a new claim.
- **Platforms.** Claims and releases need `flock`, so they need macOS, Linux or WSL. On Windows
  `forge next` (except `--dry-run`) and `release-claim` stop with exit 5 before changing
  anything. There is no `O_EXCL` fallback.
- **Re-check before writing.** The post-claim steps check that the branch's last Forge event is
  still my claim, both before the first GitHub write and right before the lease comment.
  `ls-remote` answers when the tip is the claim; otherwise Forge fetches. If the claim changed,
  Forge exits 5 and the lease comment is not written. Labels may already be set when the change
  lands between the two checks.

### Known limits
- Staleness relies on client clocks: committer dates and lease timestamps. A machine whose clock
  runs ahead by more than `lease_hours` makes its claims look fresh to everyone until that time
  passes. `forge setup --check` (Phase 8) should warn when the local clock is more than a few
  minutes off the `Date` header of a GitHub API response.
- A claim pushed but never verified, whose run crashed and was never re-run, holds its task and
  zones until it goes stale.
- Two pid namespaces that share both the clone and the state directory (a container that mounts
  the host's home, say) get the same machine id, and Forge cannot tell them apart. A run on one
  side may then read a live run on the other side as dead and take its claim over or prune its
  record. Give such containers their own `XDG_STATE_HOME`, or complete incomplete claims there
  only with `forge next --task <n>` after checking that no other run is working on the task.
