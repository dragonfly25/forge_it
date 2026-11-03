# 0012 — `forge ship`, `forge review`, `forge handoff`, `forge propose`

- Status: accepted
- Date: 2026-11-03

## Context

Spec §11.1, §11.2, §8.3 and §12 describe these four commands briefly. Several points are open:
- how the PR body is built and kept up to date;
- which reviewers are requested, and how many;
- when checks run twice (kickoff item B7);
- how `unzoned: error` affects ship;
- what a handoff does to the branch, the lease and the local state;
- the exact shape of a proposal;
- how `min_tier_for.reviewer` is enforced.

## Decision

### `forge ship [--draft]`
Ship requires a current task with my own **live** claim, like sync (ADR 0011). It then runs these steps:
1. **Sync**: run the sync core (ADR 0011). Exit codes 2, 4 and 5 are returned as they are, and ship stops. `pushed` or `up_to_date` continue.
2. **Checks (B7)**: skip them when the newest `sync-log.json` entry has `checks_passed: true` and `checked_head` equals HEAD. Otherwise run `config.checks`; a failure exits 2, with the same report format as sync.
3. **Zones**: the changed files are those in `merge-base(HEAD, origin/<default>)..HEAD`.
   - **Out of zone**: a file outside the task's zones but inside another zone. These are listed in the PR as "Out-of-zone changes", with a warning.
   - **Unzoned**: a file matching no zone at all. With `unzoned: warn` (the default) it is listed and warned about. With `unzoned: error`, ship exits 2 before touching the PR.
4. **PR**: Forge creates or updates the PR from `task/<n>` into the default branch. Title: the issue title. Body: see the next section. `--draft` applies only on creation; ship never changes the draft state of an existing PR.
5. **Issue**: set `status:in-review` with the existing label-diff helper, and refresh the lease heartbeat.
6. **Reviewers**: see the selection rules below. Ship requests them only when the PR has fewer requested or already-reviewing members than `review.required_approvals`. It tops up to that number and never removes anyone.

Exit codes:
- `0`: PR created or updated.
- `2`: sync conflicts, failing checks or unzoned files with `unzoned: error`.
- `4`: escalation from sync.
- `5`: preconditions.
- `1`: other errors.

### PR body
Forge owns a block between `<!-- forge:pr-begin -->` and `<!-- forge:pr-end -->`, and rewrites only that block on later ships. Text outside the markers belongs to humans and is kept byte for byte.
- **On creation**: the body is the Forge block, followed by the repository's `.github/pull_request_template.md` when it exists (read from the working tree).
- **Block sections, in order**:
  1. `Closes #<n>`, the task id and the spec path.
  2. **Acceptance criteria**: a checklist, one item per `covers` id, with the AC text from the spec when it is found (`extract_acceptance_criteria`).
  3. **Done when**: a checklist from `done_when`.
  4. **Out-of-zone changes**: one item per file with the zone it belongs to, or "unzoned". It says "none" when empty.
  5. **Conflict resolutions**: from the `sync-log.json` entries of this branch that had conflicts: path and resolution (resolver name, `rerere` or `manual`). It also flags `force_resolve` syncs and links the upstream PRs. It says "none" when empty.
  6. **Agent**: the agent id, tool and `providers`. It also gives the resolved tier → model map for the task's tier (`models.resolve` with `declared_tier="task"`), including effort and any notes.
- **Checkbox state**: on update, an item that was checked stays checked when its text is unchanged. Reviewers tick items in the PR; ship must not untick them.

### Reviewer selection (pure, `select_reviewers`)
- **Candidates**: the members of `.forge/team.yml`, except me (my login, compared case-insensitively), anyone already requested, and anyone who already reviewed.
- **Ranking when `review.cross_provider` is true**:
  1. no provider overlap with my active agent's `providers`;
  2. partial overlap;
  3. a member with no `providers` declared;
  4. full overlap, meaning their providers are a subset of mine.

  When `cross_provider` is false, all candidates rank equally.
- **Ties**: within a rank, the order is team order rotated by the issue number, so requests spread across the team. The result is deterministic for a given issue.
- **Count**: as many as needed to reach `review.required_approvals`. With no candidates there is a warning and no request.
- **Traceability**: the selected reviewers and their rank reasons are in the JSON output.

### `forge review <pr> [--submit approve|request-changes|comment --body-file F] [--force]`
- **Context** (no `--submit`):
  - the PR's metadata and its linked task. The task is the issue `n` of a `task/<n>` head, else the first `Closes #n` in the body.
  - the task metadata and the spec AC excerpts;
  - the files and the diff;
  - a zone check: out-of-zone and unzoned files, against the task's zones;
  - CI (`commit_ci`, which degrades to `null` with a warning, as in ADR 0010);
  - the existing reviews.

  Forge fetches `refs/pull/<n>/head` into `refs/forge/review/<n>` and diffs `merge-base(origin/<default>, that ref)..that ref` with git. The diff is capped at 300 000 bytes, with `diff_truncated: true` when cut. A PR that is not linked to a task still gets a context, with `task: null` and a warning.
- **Submit**: wraps `submit_review`, mapping `approve` → `APPROVE`, `request-changes` → `REQUEST_CHANGES` and `comment` → `COMMENT`. Forge appends a footer to the body: `Reviewed with Forge — agent <id> (<providers>), model <model>`.
  - Approving or requesting changes on my own PR exits 5. Commenting on it is allowed.
  - `--body-file` is required with `--submit`.
- **Tier (spec §6.5.4)**: the agent is resolved with `roles=["reviewer"]`. If it cannot serve the tier that `min_tier_for.reviewer` requires, review exits 5, for both context and submit, unless `--force`. With `--force` the outcome carries a warning, and the footer says the tier was forced.
- **No profile**: the context works without a profile (no tier check, with a warning). Submit needs a profile.

### `forge handoff --note-file F`
Handoff needs a current task and my own claim, live or stale while the branch's last event is still my claim, as for release-claim (ADR 0009). An incomplete claim is refused. The note must not be empty.
1. **Branch**: if the tree is dirty, commit `forge: wip` with hooks. Then add an empty event commit on top, with the trailers `Forge-Handoff: <login>`, `Forge-Agent`, `Forge-Task` and `Forge-Run`.
2. **Push**: push both commits in one push with `--force-with-lease`, the expected value being the `origin/task/<n>` tip at fetch time.
   - Hooks run, because the push may carry user code.
   - A hook rejection exits 2.
   - A lease rejection exits 5.
   - The local branch must contain `origin/task/<n>`, as for sync.
   - A push with no new user commits still pushes the event commit.
3. **Comment**: post the note under `<!-- forge:handoff -->`. A header gives the author, agent, time, branch and tip sha. Forge warns, without refusing, when the note lacks any of the §8.3 headings: Done, Remaining, Decisions, Gotchas, Next step, How to verify (case-insensitive).
4. **Issue**: unassign me and set `status:handoff`.
5. **Lease**: set `released_at` in the lease comment. This is the completion marker, as in release-claim.
6. **Local state**: clear `current.json`. The worktree is kept.

A re-run after an interruption completes the GitHub steps without pushing a second event commit. It notices that the branch's last event is already my handoff for this run, or that the lease shows it.

### `forge propose --title T --body-file F`
- **Issue shape**:
  - Title: `T`.
  - Body: the file's content, then a footer under `<!-- forge:proposal -->`. The footer reads "Proposed by @me (agent X)", plus "while working on <task id> (#n)" when there is a current task.
  - Label: `status:proposed` only. It is not `forge:task`, because there is no metadata block until the lead adds the task to a plan.
  - Milestone: the current task's milestone, if any.
- **Idempotency**: when an open issue labeled `status:proposed` has the same title (compared case-insensitively, whitespace trimmed), Forge returns it with `created: false` and exit 0, and posts nothing.
- **Requirements**: GitHub access. A profile is optional; without one, the footer omits the agent.

## Implementation notes (Phase 6)

**Ship**
- **Missing markers**: a PR body without a complete marker block, for example opened by hand, gets the block prepended. The old body is kept unchanged.
- **Approvals count**: every requested or reviewing login except mine counts toward `required_approvals`, team member or not.
- **Conflict resolutions**: only pushed syncs of `task/<n>` that had conflicts or used `--force-resolve` are listed. Escalated syncs are not.
- **Upstream authors**: they appear in backticks (`` `@bob` ``), so updating the PR body never notifies them.
- **Rotation**: the team list is rotated left by `issue % len(team)` and then stable-sorted by rank.
- **Stopped sync**: ship never runs `sync --continue`. A stopped sync is the agent's to finish.

**Review**
- **Footer model**: it comes from the §12.1 reviewer declaration (strong tier, high effort, `roles=["reviewer"]`). Only `min_tier_for.reviewer` can refuse, with exit 5.
- **Version mismatch**: it exits 5 for `--submit`, and is only a warning for the context, as in `status` and `escalate`.
- **Usage errors (exit 1)**: `--body-file` without `--submit`, an unknown `--submit` value, and an empty proposal title.
- **Files**: the list comes from the git diff, so it always matches the diff. A file outside every zone is listed only as `unzoned`.

**Handoff**
- **Rejected push**: a hook or lease rejection undoes the wip commit, so the user's changes come back uncommitted.
- **Ownership guard**: labels and `released_at` are written only while `task/<n>` still ends with my handoff. The status label comes from `derive_status`, so an open PR gives `in-review`.
- **No duplicate note**: the note comment is keyed by the event commit's sha, so a re-run never posts it twice.
- **Refusals**:
  - a sync or rebase in progress, or `task/<n>` not checked out;
  - a note over 65 536 characters, refused before anything changes;
  - Windows: like release-claim, handoff needs `flock`.
- **Known limits**:
  - A claim by someone else can land between my last check and my lease write. My write then overwrites their lease; `forge next --task <n>` completes their claim again.
  - If my run crashes after the push and someone resumes the task before I re-run, the note is never posted.

- **Open PR**: handoff refuses (exit 5) while the task has an open PR. The task would be derived as `in-review`, which `forge next` never claims, so nobody could resume it. The author addresses review comments themselves, or closes the PR and then hands off; the next person ships a new PR.
- **Local work kept**: a re-run that recovers an interrupted handoff exits 2 with the outcome `unpublished_work` when local commits or changes stay unpublished. It lists them and keeps `current.json`; the GitHub steps still complete.
  - The first run reports `unpublished_work` too when a commit lands in the worktree during the push. The local branch is updated with a compare-and-swap, so that commit is kept.
  - A re-run where `task/<n>` is not checked out reports the working tree as unchecked (exit 2) instead of skipping it.
  - Re-runs refuse an operation in progress (sync, rebase, merge, cherry-pick, revert) like the first run.
  - Staged content is never reset away unless the index matches HEAD or the handoff tip.
- **Any push failure**: it undoes the wip commit, not only hook or lease rejections.
- **Git operations in progress**: a merge, cherry-pick or revert in progress is refused, like a sync or rebase.

**Ship (more)**
- **Reviewer request failure**: a failed reviewer request, for example a team member without repository access, is a warning naming the logins. Ship still exits 0, since the PR is already done.
- **Heartbeat failure**: if the heartbeat fails after the PR is created, the error hint says that re-running `forge ship` completes the remaining steps.

**Review (more)**
- **Identity**: review checks the `gh` login against the profile (`check_identity`) before submitting, as ship and handoff do.
- **Stacked PRs**: the diff is computed against the PR's base branch, not the default branch, so a stacked PR shows its own changes. When that base cannot be fetched (deleted), review falls back to the default branch with a warning.
- **Diff format**: git runs with `--no-textconv --no-ext-diff` and fixed `a/` and `b/` prefixes, so user config cannot change the output.
- **Non-UTF-8 diffs**: they are decoded with replacement characters and a warning, and `diff_bytes` reports git's real size. The 300 000-byte cap applies to the decoded text.

**Propose**
- **Labels first**: propose ensures the Forge labels exist before creating the issue.
- **Known limit**: two concurrent proposals with the same title can both be created, because the duplicate check runs before the create.

## Consequences

- Ship can be re-run safely: it updates one block, keeps human edits and ticked boxes, and only tops up reviewers.
- Reviewer requests rotate deterministically, which tests can rely on.
- A handoff is visible on the branch (the event commit), on the issue (the comment, the label, no assignee) and in the lease, so `forge next` prefers the task and prints the note (ADR 0009).
