# 0013 — Planning: specs, roadmap, `forge plan validate` and `forge plan apply`

- Status: accepted
- Date: 2026-11-03

## Context

Spec §9 defines the spec, roadmap and plan formats, the validation rules (§9.4) and the apply rules (§9.5). Several points are left open:
- which specs belong to a milestone, and how `AC-n` references resolve;
- whether validation reads GitHub;
- where the task description lives in the issue body, since §7.1 says humans own text outside the Forge block while §9.5 says apply updates the description;
- what "in-progress" and "structural" mean for apply;
- how apply stays idempotent and safe when two runs overlap;
- the `min_tier_for.plan` rule (kickoff item B4).

## Decision

### Specs (`core/spec.py`)
- **Frontmatter**: a spec starts with a YAML block between `---` lines. `milestone` is required. `status` is required, `draft` or `ready`. `owner` is optional. Unknown keys are a warning. Errors name the file, the key and the fix.
- **ACs**: AC definitions use the existing rule (`context.defined_acceptance_criteria`, which uses the same pattern as `extract_acceptance_criteria`).
- **Open questions**: the list items of the `## Open questions` section, up to the next heading of the same or a higher level. An item is resolved when it contains the word `RESOLVED`, in upper case. A `ready` spec needs every item resolved, or no items. Placeholder items such as the template's "(must be empty…)" count as unresolved, so the lead deletes them.

### Roadmap (`plans/roadmap.yml`)
- **Format**: a list of `milestones`, each with an `id` (`M<n>`), a `title` and an optional `goal`, `demo`, `lead` and `specs`. Unknown keys are errors.
- **Optional file**: the roadmap may be missing.
- **When present**: the milestone being validated must be listed in it (error), and its `specs` count as milestone specs.

### Plan files (`plans/<milestone>.yml`)
- **Format**: `milestone` plus `tasks`. Each task has `id`, `title`, `type`, `spec`, `covers`, `depends_on`, `zones`, `size`, `tier`, `description`, `done_when`, the optional `allow_contract_change`, and the optional `proposal`, an issue number (see "Accepting proposals"). These are the §7.1 metadata fields plus `title`, `description` and `proposal`.
- **Model**: each task becomes `PlanTask(meta: TaskMetadata, title, description)`.
- **Errors** (exit 1), the §9.4 list made exact:
  - **Fields and files**: an unknown field; a missing required field; the file's `milestone` differing from `<milestone>`.
  - **Ids**: a task id not of the form `<milestone>-T<n>`; duplicate ids.
  - **Zones and dependencies**: an unknown zone (from `.forge/zones.yml`); an unknown `depends_on`; dependency cycles.
  - **Size**: `size: L` (or any size other than S and M), with "split the task".
  - **Specs**:
    - a spec that is missing or has invalid frontmatter;
    - a spec whose frontmatter `milestone` differs from the plan's;
    - a spec with `status: draft`, or with unresolved open questions.
  - **ACs**:
    - **Unknown AC (added)**: a `covers` id not defined in the task's own spec. The coverage matrix cannot be built otherwise.
    - **Duplicate AC (added)**: the same AC id defined in two specs of the milestone. §9.1 says AC ids are unique within a milestone.
  - **Contract zones**: a task touching a `contract` zone without `type: contract`, unless it sets `allow_contract_change: true`.
- **Warnings** (exit 0):
  - two tasks in the same wave that overlap by zone (same rule as claims: shared zone, a `conflicts` pair, or intersecting globs), suggesting a dependency or a zone split;
  - milestone ACs that no task covers;
  - waves with fewer tasks than active team members, meaning members of `.forge/team.yml` with role `dev` (skipped without a team file);
  - unknown spec frontmatter keys.
- **Dependencies on other milestones**: they resolve against the other `plans/*.yml` files. Validation is **offline**: it never reads GitHub, so it can run in CI and on a fresh clone. For wave computation, these external dependencies count as satisfied.
- **Milestone specs**: the specs referenced by the plan's tasks, plus the roadmap's `specs` for the milestone. These define the ACs used for coverage and duplicate checks.
- **Output**: the waves (topological levels) with task ids, the coverage matrix AC → task ids, and errors and warnings, each with file, task id and field where they apply.

### `min_tier_for.plan` (B4)
- **Scope**: `forge plan validate` and `forge plan apply` check the active agent when a profile exists and `GITHUB_ACTIONS` is not set. An agent that cannot serve the required tier gets exit 5, unless `--force`.
- **No check**: without a profile, or in CI, there is no check, only an info message.

### `forge plan apply <milestone> [--dry-run] [--force]`
1. **Validate first**: run validate; errors → exit 1, nothing written.
2. **Compute**: compute a pure **apply plan** from the plan file, all `forge:task` issues (open and closed), the task branches (fetched as `forge next` does) and the open PRs. `--dry-run` prints the plan and writes nothing.
3. **Labels**: ensure the labels exist.
4. **Milestone**: ensure the GitHub milestone exists. Its title is the milestone id (`M1`), which Forge uses everywhere to match. The description is the roadmap title and goal when present.
5. **Upsert**: one issue per task, matched by the metadata `id` across all states. When two issues share an id, the lowest number wins, with a warning (as in refresh).
   - **Missing**: create the issue with title `[<id>] <title>`, the body below, the milestone, and the labels `forge:task`, mirror labels and derived status.
   - **Closed**: never modified.
   - **Not started**: everything is updated as needed: title, body (description region and metadata block), milestone, mirror labels.
   - **Started**: the derived status is `in-progress`, `handoff` or `in-review`, i.e. the branch has a Forge claim or handoff event, or there is an open PR. Only the title, the description region and `done_when` are updated.
   - **Other changes to a started task**: any other metadata change is **not applied**, whether zones, `depends_on`, size, tier, type, spec, `covers` or `allow_contract_change`. It is listed in a warning comment that mentions the assignees, under the marker `<!-- forge:plan-warning -->` and keyed by a hash of the pending changes, so the same warning is never posted twice.
6. **Removed tasks**: open `forge:task` issues of this milestone whose id is not in the plan.
   - **Not started**: closed as `not_planned` with a comment.
   - **Started**: a warning comment (same marker and key rule), left open.
7. **Status**: recompute status labels for all tasks with the refresh logic (ready/blocked, unblocking dependents).

**Accepting proposals (§9.6).** A `forge propose` issue has no Forge block and no `forge:task` label (ADR 0012). The lead accepts it by adding a plan task with `proposal: <issue number>`.
- **Adoption**: apply turns that issue into the task:
  - title `[<id>] <title>`;
  - the description region and metadata block inserted, with the proposal's own text kept outside them;
  - `forge:task` and the mirror labels added, and `status:proposed` removed;
  - the milestone and the derived status set.
- **Bad reference**: the referenced issue must be open and labeled `status:proposed`, or already be the task's issue. An open issue that already carries this task's block, left by an interrupted adoption, also counts. Otherwise apply exits 1 before any write.
- **Adoption interrupted mid-write**: if the title or body write fails, the labels are left alone, so the next run can still adopt the issue. A proposal whose `status:proposed` label is removed during the run is not adopted.
- **Later applies**: once adopted, the issue matches by its metadata id like any other.

**Issue body.** Apply owns two regions:
- the description, between `<!-- forge:description-begin -->` and `<!-- forge:description-end -->`, at the top;
- the metadata block (`<!-- forge:begin -->`…`<!-- forge:end -->`, §7.1).

Text outside both regions belongs to humans and is kept byte for byte. On an issue with no description region (created by hand), the region is inserted at the top. This reconciles §7.1 with §9.5.

**Idempotency.**
- Every write is computed as a diff against the current state, so applying the same plan twice writes nothing.
- A test runs apply twice and asserts zero GitHub writes on the second run.
- Two applies running at once can create the same task twice. After creating an issue, apply lists the issues again. If more than one open issue carries the id, it keeps the lowest number and closes the others as `not_planned` with a "duplicate of #n" comment. Both runs converge on the same result.

**Exit codes.**
- `0`: applied, or a dry run.
- `1`: validation errors, or a GitHub or git failure.
- `5`: preconditions: version mismatch, `gh` not authenticated, a shallow clone, or the tier refusal.

### Commands
`forge plan validate <milestone> [--force]` and `forge plan apply <milestone> [--dry-run] [--force]`, under a `plan` command group. Both print waves and coverage in human form and support `--json`.

## Implementation notes (Phase 7)

**Validate**
- **Placeholder questions**: an open question written entirely in parentheses is never resolved, even if it contains `RESOLVED`. The template's placeholder therefore stays unresolved.
- **Question scope**: an open question is one top-level list item with its sub-items and continuation lines.
- **Required list fields**: `covers`, `depends_on` and `zones` must be present, even as `[]`.
- **Broken tasks**: a task with field errors is left out of the graph, but its id still counts, so its dependents get no extra "unknown dependency" errors.
- **Other plan files**: when resolving cross-milestone dependencies, they are read leniently; one that cannot be parsed is skipped.
- **Marker lines**: a description containing a Forge marker line is an error. The check runs in validate, so apply never refuses halfway.
- **Version mismatch**: it is a warning for validate, as for status.

**Apply**
- **Duplicate cleanup**: it runs on every apply, not only right after a create. A started duplicate is kept open with a warning.
- **Closed issues**: apply never writes to an issue that was closed when it started, labels included. `refresh` owns those labels.
- **Proposals in the plan**: a matching issue labeled `status:proposed` has the label cleared, because the lead accepted it by adding it to the plan. A removed proposal is left open with a warning.
- **Unreadable blocks**: an issue whose Forge block cannot be read, but whose title starts with `[<id>]`, blocks that id from being created, to avoid a duplicate.
- **Comments**: each warning comment is re-read just before posting. This narrows, but cannot close, the window for a concurrent duplicate post.
- **Milestone description**: it is set to "title: goal", and only updated when the roadmap has an entry for the milestone.
- **Stable keys**: plan paths in comments and warning keys are repository-relative (`plans/<milestone>.yml`), so they are the same across clones.
- **Claim after the fetch**: right before writing a not-started task, apply checks `task/<n>` on the remote again. It does the same before closing one, removed or duplicate. A task found started turns the close into the "keep" warning. If a claim landed in the meantime, the task takes the started path (title, description and `done_when` only, plus the warning).
- **Fresh body**: apply reads the issue again right before the write and rebuilds the body from that text, so human edits made during the run are kept.
- **Known limit**: a window of one API round-trip remains between that last check and the write. A claim, or a body edit, landing in that window is not seen. GitHub has no compare-and-swap on issue bodies, so this cannot be closed.
- **Closing right after a re-check**: a close re-checks the task branch tip right before the close call itself, not only before its labels and comment.
  - **Branch moved**: if the task is now started, the issue stays open with the warning. Such an issue can carry both the "closed as not planned" comment and the "left open" warning, because the comment is posted before the final check.
  - **Moved without a start**: the issue is left for the next run to close.
- **Duplicates**: before closing a duplicate, apply confirms the winning issue still carries the id.
- **Label-only and title/description updates**: they do not re-check the branch. Their status labels may be stale for a task claimed mid-run; the next `forge refresh` (radar workflow) repairs them.
- **Per-issue comment keys**: the "removed" and "duplicate" comments are keyed per issue, not per removal. An issue removed, re-added and removed again is closed without a second comment.
- **Proposal checks**: a proposal referenced by two tasks is a validate error. Apply reads proposals before the fetch and refuses before any write.
- **Re-added tasks**: when the issue matching a plan task was closed as `not_planned` (removed, then put back in the plan), apply warns and names the fix: reopen the issue, then re-run. A closed issue can win the duplicate rule, and closed issues are never modified.
- **More validate errors**:
  - an AC defined twice in the same spec;
  - a title containing a newline;
  - a title whose `[<id>] <title>` exceeds GitHub's 256-character limit, which would break idempotency.

## Consequences

- Validation needs no network, so the CI workflow can run it on plan PRs before merge.
- Humans can annotate task issues anywhere outside the two Forge regions without apply overwriting them.
- The lead changes a started task's structure by talking to the assignee: apply only warns.
