# Dogfooding report: Forge v0.1.0

- **Date**: 2026-11-03
- **Sandbox**: `dragonfly25/forge_sandbox` (private, created empty for this run)
- **Forge**: `0.1.0.dev0` from branch `phase/10-dogfood`, run with `uv run --project`
- **Machine**: macOS, git 2.40, gh 2.92, Python 3.12, Claude Code 2.1.301, Ollama

## Setup of the run

The run had one GitHub account (`dragonfly25`). Two users, "alice" and "bob", were simulated with two clones of the sandbox. Each had its own `XDG_CONFIG_HOME`/`XDG_STATE_HOME`, so each had its own profile and machine state, and its own agent id (`claude-alice`, `claude-bob`). Under one login this is the "same identity, different agent" case of ADR 0009.

Two gaps follow from having one account:
- A real cross-account approval was not possible: GitHub refuses an approval on your own PR, and Forge refuses it first (exit 5). Reviews were submitted as comments instead.
- Reviewer requests could not be exercised, because `team.yml` had one member.

Every command was run against real GitHub through `gh`.

## Checklist

| Step | Result |
|---|---|
| `forge init` on an empty repo | ✅ Tree written. Repository, branch and login detected. |
| `forge setup --check`, then `forge setup --yes` (twice, one per user) | ✅ Checks with fixes. Tools detected (Claude Code, Ollama). Profile written with labels only. rerere enabled. Subagents rendered. `setup.commands` run. |
| Spec + `plans/M1.yml` (4 tasks, 3 waves); `forge plan validate M1` | ✅ 0 errors; waves and coverage correct. |
| `forge plan apply M1` (dry run, real, re-runs) | ⚠️ → ✅ Created labels, milestone and 4 issues. **A re-run 2 s later created duplicates** (finding 1). The duplicate cleanup then converged, and a later re-run made 0 writes. |
| `forge status`, `forge next --dry-run` | ⚠️ → ✅ Closed duplicates showed as "dropped" (finding 2). |
| `forge next` (alice): claim T01, worktree, task context | ✅ Lease comment, assignee, `status:in-progress`, worktree `repo-worktrees/task-1`. |
| `forge ship` (T01) | ✅ Sync, checks skipped on an already-checked HEAD, PR opened with the Forge block. |
| `forge review` (bob) | ✅ Context and diff correct. `--submit approve` on own-login PR refused (exit 5). `--submit comment` posted with the footer. |
| Merge, then `forge refresh` | ✅ T01 done, T02 and T03 ready. |
| **Two users claim in parallel** (`forge next` at the same time) | ✅ alice won T02. bob lost the race on #2 and claimed T03. |
| **Sync with a conflict** (hotfix on `main` vs T02) | ✅ Exit 2 with the report and `sync.json`. Manual resolution, then `--continue`: checks passed, pushed, lease kept, summary comment posted. |
| Ship T02 and T03, cross review, merge | ✅ |
| Dependents unblock (`forge refresh`) | ✅ T04 ready once T02 and T03 closed. |
| `forge refresh --radar` | ✅ One radar comment on T04 for an upstream commit in its zone. |
| `forge handoff` (alice), then `forge next` (bob) | ✅ Draft saved as `forge: wip`, event commit pushed, note posted, unassigned, `status:handoff`. bob resumed T04 with the note printed. |
| `forge sync` then `forge ship` (bob, T04), merge | ✅ Milestone M1 4/4 done, all ACs covered. |
| `forge release M1` (no version, dry run, step 1, merge, step 2, re-run) | ✅ Missing version: exit 5 with a hint. Release PR #15 with the grouped changelog. After the squash merge: tag `v0.1.0` on the squash commit, GitHub release "v0.1.0 — Hello API", milestone closed. The re-run was idempotent. |

## Findings and fixes

1. **Duplicate task issues on a fast re-run (high)**:
   - **Cause**: GitHub's issue lists lag 1–2 s, and up to 5 s for `direction=desc`, behind newly created issues, while `GET /issues/{n}` is immediate.
   - **Fixed**: `GhCliClient.list_issues` now fetches the issue numbers above the newest listed one, so recent creations are never missed. `FakeGitHub` can reproduce the lag (`fake_set_list_lag`). Recorded in the ADR 0007 amendment.
   - **Measured**: label changes lag about 7 s in filtered lists; comment and PR lists did not lag.
   - **Verified live after the fix**: a one-task M2 plan applied twice back to back created issue #16 once. The second run made 0 writes.
   - **Hardened after the Phase 10 review**: the guard also keeps issues that only the newest-issues request saw, and its number fetches are bounded. Re-verified live: an M3 plan applied twice back to back created issue #17 once, with 0 writes on the second run.
2. **Closed duplicates counted as dropped tasks and warned on every command (medium)**: fixed in `parse_records`. A task closed as not planned whose id is carried by another issue is a superseded duplicate and is ignored.
3. **`forge ship` on a PR opened concurrently (medium, found while measuring finding 1)**: GitHub answers 422. Ship now re-reads the PR and updates it.
4. **`forge refresh` printed a raw Python dict (low)**: it now has a proper renderer.
5. **`forge setup` repeated the "rerere not enabled" warning after enabling rerere (low)**: the checks now run after setup's own fixes.
6. **`forge init` named the project after the clone folder ("repo") (low)**: it now uses the GitHub repository name when detected.
7. **Task context printed the description-region markers (low)**: fixed.
8. **`forge review` printed each AC with a duplicated prefix (low)**: fixed.
9. **Radar comment grammar ("1 commit … touch") (low)**: fixed.
10. **Workflows cannot install Forge when the Forge repository is private (medium)**: every `forge-ci` run in the sandbox failed at the install step. The templates now accept an optional `FORGE_INSTALL_TOKEN` secret, documented in the onboarding and FAQ.

## Not exercised

- **Real cross-account approval and reviewer requests**: they need a second GitHub account. The unit and FakeGitHub tests cover the selection rules.
- **Codex adapter on a machine with Codex**: Codex is not installed here, so only the project-level skills were generated.
- **Workflows running end to end in GitHub Actions**: they need finding 10 resolved in the sandbox (a token secret), plus a published `v0.1.0` tag of Forge.
- **Projects v2**: deferred to v1 (ADR 0015).

## Known limits confirmed

- **Radar comments on closed tasks**: a radar comment stays on a task issue after it closes, because only open issues are scanned (ADR 0014).
- **Filtered lists after label or state changes**: they can lag a few seconds (ADR 0007 amendment).
