---
name: dev
description: Implement the current task inside its zones, with tests, small commits and heartbeats
args: ""
roles: [dev]
---
## Goal
Implement the claimed task so that every `done_when` item is true, staying inside the task's zones and following the spec, the ADRs and `.forge/rules.md`.

## Steps
1. Load the task context. This also finds out which task is current:
   `forge task show --json`
2. Read `.forge/rules.md`. Work in the task's worktree (the path is in the context).
3. Implement in small steps, contract-first:
   - touch only files inside the task's zones;
   - make one commit per concern, with conventional messages (`feat:`, `fix:`, `test:`, `docs:`, `chore:`);
   - follow the spec sections, acceptance criteria and ADRs shown in the context.
4. Write or extend tests that prove each `done_when` item. Run them.
5. Run the project checks listed under `checks:` in `.forge/config.yml` (lint, typecheck, tests). Fix what fails.
6. On long sessions, keep the claim alive, about once an hour and before long operations:
   `forge heartbeat --json`
7. When you find extra work that the task does not cover, do not do it. Write a short body file (what, why, where) and run:
   `forge propose --title "<title>" --body-file <file> --json`
8. When you are near done, self-check against the task (see Subagents), fix findings, then tell the user the task is ready for `/forge-ship`.
9. If you run out of tokens or time, stop and run `/forge-handoff`. Do not leave work unpublished.

## Exit codes
- `forge task show` `0`: use the context. `5`: there is no current task. Run `/forge-next` first. `1`: report `messages`.
- `forge heartbeat` `0`: lease refreshed. `5`: the claim is not yours, is stale or is incomplete. Stop pushing. If the message says stale, tell the user to run `forge reclaim <n>`. If it says incomplete, run `forge next --task <n> --json`. `1`: report.
- `forge propose` `0`: created, or already exists (`created: false`). Say which. `1`: report.
- `2`, `3` and `4` are not expected from these calls. Report the output to the user.

## Guardrails
- Stay in the zones. If a change needs a file outside them, stop and ask the user. If it is new work, use `forge propose`. If it belongs to someone else's work or to a contract you do not own, run `forge escalate --reason "<reason>" --json`.
- Never change a contract zone the task does not own.
- Never push to the default branch. Do not push by hand: `/forge-sync` and `/forge-ship` publish your work.
- Never write credentials or secrets into files, commits or issues.
- Do not weaken or delete a test to make it pass. Fix the code or tell the user.
- Do not start another task while this one is open.
- Sync at least once a day: run `/forge-sync`.

## Subagents
- If `forge-explorer` is available, delegate reading unfamiliar code and specs to it, and ask for a compact summary.
- If `forge-test-writer` is available, delegate writing and running the tests for the `done_when` items to it. Give it the task id, the `done_when` list and the zones. Review its tests.
- If `forge-reviewer` is available, delegate the self-check before ship to it: ACs, `done_when`, zones, contracts, tests.
- Without subagents, do each of these yourself. Implementation always stays with you (the main agent).

## Final report
Tell the user:
- what you built and which `done_when` items are proven by which tests;
- the checks you ran and their results (do not say "passed" unless you ran them);
- proposals you filed (issue numbers);
- open risks or follow-ups;
- the next step: `/forge-ship`, or `/forge-handoff` if you stopped early.
