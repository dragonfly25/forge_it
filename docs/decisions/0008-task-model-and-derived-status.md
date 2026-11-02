# 0008 — Task model, labels and derived status

- Status: accepted
- Date: 2026-11-02

## Context

Spec §7 defines tasks as issues with a metadata block, status labels as a *view*, and a truth that is recomputed. It leaves these points open:
- the exact grammar of the block and of task ids;
- which labels Forge owns and what a closed issue looks like;
- the order of precedence when deriving a status;
- how handoff and release are visible on the task branch. This is B1 from the kickoff: after a handoff, or a `release-claim` without `--delete-branch`, the branch still exists and is fresh, yet the task must be claimable again.

## Decision

### Metadata block (`core/tasks.py`)
- The markers are `<!-- forge:begin -->` and `<!-- forge:end -->` on their own lines. Between them sits exactly one fenced code block (```` ```yaml ```` … ```` ``` ````). Nothing else is allowed inside the markers apart from whitespace.
- **Parse**: no markers → `None` (not a Forge task body). Exactly one well-formed block → `TaskMetadata`. Any other case is a `TaskBodyError` with a reason: a begin without an end, two blocks, invalid YAML, or invalid fields.
- **Render** replaces only the text between the markers. Everything outside them is preserved **byte for byte**. If the body has no block, one is appended after a blank line. Rendering is deterministic, with keys in spec §7.1 order and `done_when` as a block list. Round-tripping a rendered block gives the same text.
- **Fields** (§7.1 plus §9.4):
  - `id` must match `^M\d+-T\d+$`, and its milestone prefix must equal the issue's milestone when the issue has one.
  - `spec`: a relative path.
  - `covers`: AC ids `^AC-\d+$`.
  - `depends_on`: task ids, with no self-reference and no duplicates.
  - `zones`: names. They are validated against `zones.yml` by the callers that load it, not by the parser.
  - `size`: `S` or `M`. `L` is rejected with "split the task" (§9.4).
  - `tier`: a Tier.
  - `type`: `contract`, `impl`, `test` or `chore`.
  - `done_when`: a non-empty list of strings.
  - `allow_contract_change`: a bool, default false.
  - Unknown keys are rejected.

### Labels
- **Owned by Forge**:
  - `forge:task`;
  - `status:proposed|ready|blocked|in-progress|handoff|in-review|done`;
  - `size:S|M`, `tier:strong|standard|light`, `type:contract|impl|test|chore`;
  - `blocked:conflict`.
  `core/tasks.py` holds the catalogue with colours and descriptions. `ensure_labels` creates missing ones and never deletes user labels.
- An issue has **at most one** `status:*` label. The `size`/`tier`/`type` labels mirror the metadata.
- `blocked:conflict` is set and cleared only by `escalate` and `sync` (Phase 5). `refresh` leaves it alone.

### Branch events (trailers), resolving B1
- Commits that Forge makes on `task/<n>` carry trailers. `Forge-Task: <id>` is always present, plus exactly one event trailer:
  - `Forge-Claim: <github>` (with `Forge-Agent: <agent id>`);
  - `Forge-Handoff: <github>`;
  - `Forge-Release: <github>`.
  Handoff and release commits are empty if there is nothing to save.
- The **last Forge event** of a branch is the event trailer on the newest commit that carries one, among the commits between the tip and its merge-base with `origin/<default>`. Only the **first-parent** history is walked, and only commits whose `Forge-Task` equals the task's id count. Events from merged-in or cherry-picked commits of other tasks are ignored. A *live claim* requires that this event is `Forge-Claim` (Phase 3 adds staleness).

### Derived status (`derive_status`, pure)
The inputs are the issue (state, state_reason, labels), its metadata, the closed/open state of each dependency, the branch state (exists, last event) and whether an open PR from `task/<n>` exists. The first matching rule wins:
1. Issue closed. `state_reason = not_planned` → no status label. Otherwise → `done`.
2. The issue carries `status:proposed` → `proposed`. This is sticky: only the lead's plan apply changes it.
3. An open PR exists from `task/<n>` → `in-review`.
4. The branch exists and its last event is `claim` → `in-progress`.
5. The branch exists and its last event is `handoff` → `handoff`.
6. Any dependency is not closed (an unknown id counts as not closed, with a warning) → `blocked`.
7. Otherwise → `ready`. This covers a released branch whose dependencies are closed.

### `forge refresh`
- The pure planning step lives in `core/refresh.py`, which also gathers state through `Git` (fetch, remote task branches, their last event) and `GitHubClient` (all `forge:task` issues, open and closed, plus open PRs).
- It applies label diffs: the status, the metadata mirrors, and `forge:task` kept present.
- Issues without a block, or with an invalid one, are reported as warnings and left alone.
- It is idempotent: a second run changes nothing.
- `data` = `{"changes": [{"issue", "id", "from", "to", "added", "removed"}], "warnings": [...], "counts": {...}}`. The warnings are repeated as envelope messages.

## Consequences

- Handoff and release are visible to anyone who can fetch, without reading comments, so claim logic (Phase 3) and refresh agree.
- Phase 3 must write these trailers. Phase 6's handoff writes `Forge-Handoff`.
- Projects v2 mirroring (§15) will hook into the same desired-status result.

## Implementation notes (Phase 2)

- `refresh` data: `counts = {issues, tasks, skipped, changed, warnings, statuses}`, where `statuses` maps each status to the number of tasks in it. When two issues share a task id, the lowest issue number is used for dependency resolution, and both are still relabelled. A warning is emitted.
- Task branches must be named exactly `task/<n>` with no leading zeros. Other branches under `task/` are ignored. If `origin/<default>` is missing locally or on the remote, refresh stops with exit 5 and names `project.default_branch`. A shallow clone also stops with exit 5, with the hint `fetch-depth: 0` / `git fetch --unshallow`. A task branch with no common history becomes a warning and is treated as absent.
- `refresh` does not check the CLI version against `forge_version`. It is a safe, idempotent relabel that also runs in CI. The check is added to `next` and `ship`, which refuse with exit 5, and to `setup` and `status`, which warn (Phase 3, 4, 6 and 8).
- The radar workflow template (Phase 8) must check out with `fetch-depth: 0`, so that merge-bases exist.
- `core/refresh.run_refresh` takes a `RefreshContext` protocol (`config`, `git`, `github`), because core cannot import `commands/`. `commands/_context.AppContext` satisfies it and is the shared context for every command. Tests swap the GitHub client with `monkeypatch.setattr(_context, "github_factory", ...)`.
- A dependency closed as `not_planned` still counts as closed (the literal reading of spec §7.2), so its dependents are not blocked by it. `refresh` emits a warning on each open, non-proposed dependent that names the dependent and the dropped dependency, and suggests removing it from `depends_on` or reopening it.
- `refresh` lists issues and PRs **before** fetching branches, so a claim that lands mid-run is never reverted. One harmless window remains. A claim can land after the fetch while refresh, working from its older snapshot, adds a different status (e.g. blocked → ready). The issue then briefly carries two `status:*` labels, and the next refresh resolves it.
- `refresh` fetches `+refs/heads/<default>` and `+refs/heads/task/*` explicitly, with prune, so single-branch clones work. Per-issue GitHub failures are reported as warnings, the run continues, and the exit code is 1.
- Marker detection follows CommonMark fences. To quote an example Forge block in an issue, use a fence longer than the example's own ```` ``` ```` (e.g. ````` ```` `````) or `~~~`. `upsert_block` refuses to overwrite a block that contains anything other than the YAML fence.
- Label names are compared case-insensitively, as on GitHub.
