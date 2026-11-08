---
name: dev
description: Implement the current task inside its zones, with tests, small commits and heartbeats
args: "[--solo|--delegate]"
roles: [dev]
---
## Goal
Implement the claimed task so that every `done_when` item is true, staying inside the task's zones and following the spec, the ADRs and `.forge/rules.md`. Forge decides whether you write the code yourself or act as lead and delegate it to `forge-implementer`. You follow that decision.

## Steps
1. Load the task context. This also finds out which task is current:
   `forge task show --json`
   With `--solo` (the user wants to write the code in this session), add `--delegation off`:
   `forge task show --delegation off --json`
   With `--delegate` (the user wants the code delegated), add `--delegation on`:
   `forge task show --delegation on --json`
   With neither flag, do not pass `--delegation`. An earlier `--solo` or `--delegate` stays in effect for this task (`source: invocation`). Without one, the profile and the project decide. To go back to the profile's mode, pass `--delegation <mode>` explicitly.
   Forge saves the choice in the task's local state until the task is released or handed off. A new `--delegation` replaces it. `forge ship` and `forge status` then report the decision you followed. You do not need to note it anywhere else. Always report the real `source` and `reason` from `data.delegation`.
2. Read `data.delegation` from the context. Use `delegate`, `reason`, `subagent` and `model`. Tell the user the decision in one line, with `reason`. If a message says the requested mode was not allowed or delegation was forced off, pass it on.
3. Read `.forge/rules.md`. Work in the task's worktree (the path is in the context).
4. Implement. Follow the path that `data.delegation.delegate` selects.

   **If `delegate` is false: implement it yourself.**
   1. Work in small steps, contract-first:
      - touch only files inside the task's zones;
      - make one commit per concern, with conventional messages (`feat:`, `fix:`, `test:`, `docs:`, `chore:`);
      - follow the spec sections, acceptance criteria and ADRs shown in the context.
   2. Write or extend tests that prove each `done_when` item. Run them.

   **If `delegate` is true: act as lead.**
   1. Explore. Read the zones, the specs and the ADRs through `forge-explorer`. Keep your own context small.
   2. Split the work into one or more briefs. Each brief has:
      - the goal, in one or two sentences;
      - the files and zones it may touch;
      - the `done_when` items it covers;
      - the constraints: contracts to keep, conventions, what not to touch;
      - the tests to write and run.
      Give parallel briefs disjoint files. If two briefs need the same file, run them one after the other.
   3. Delegate each brief to `forge-implementer`. Pass the brief as written. Do not paste the whole task body.
   4. Verify each result yourself. Read the diff (`git diff`), check it stays inside the brief's files and the task's zones, and run the checks and tests. Do not trust the summary alone.
   5. If the result is wrong or incomplete, send the findings back to the implementer. Do at most two rounds per brief. After that, take the brief over and finish it yourself.
   6. Commit the verified work yourself, one commit per concern, with conventional messages. The implementer does not claim, sync, ship, push or comment.
   7. Tests: either the brief asked the implementer for them, or delegate them to `forge-test-writer` (see Subagents). Review them.
5. Run the project checks listed under `checks:` in `.forge/config.yml` (lint, typecheck, tests). Fix what fails.
6. On long sessions, keep the claim alive, about once an hour and before long operations:
   `forge heartbeat --json`
7. When you find extra work that the task does not cover, do not do it. Write a short body file (what, why, where) and run:
   `forge propose --title "<title>" --body-file <file> --json`
8. When you are near done, self-check against the task (see Subagents), fix findings, then tell the user the task is ready for `/forge-ship`.
9. If you run out of tokens or time, stop and run `/forge-handoff`. Do not leave work unpublished.

## Exit codes
- `forge task show` `0`: use the context. `5`: there is no current task. Run `/forge-next` first. `1`: report `messages`. An invalid `--delegation` mode is a usage error with exit `1`: use only `off`, `auto` or `on` (the prompt flags `--solo` and `--delegate` map to `off` and `on`), and do not retry with a made-up value.
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
- Follow `data.delegation.delegate`. Do not delegate when it is false, and do not skip delegation when it is true, unless the user tells you to. The user's `--solo` or `--delegate` overrides the profile and the project, within what the project allows.
- When you delegate, you stay responsible for the result. Never merge in work you have not read and checked. Do not let an implementer claim, sync, ship, push or comment.
- If subagents are not available, do the work yourself even when `delegate` is true. Say so in the report.

## Subagents
- With `delegate` true:
  - `forge-explorer` reads unfamiliar code and specs for you. Ask for a compact summary.
  - `forge-implementer` writes the code from your briefs. Give it files, `done_when` items, constraints and tests. Verify its work (Steps, lead path).
  - `forge-test-writer` writes and runs the tests for the `done_when` items when you did not put them in an implementer brief. Give it the task id, the `done_when` list and the zones. Review its tests.
  - `forge-reviewer` does the self-check before ship.
- With `delegate` false:
  - `forge-explorer`, if available, reads unfamiliar code and specs for you, with a compact summary.
  - `forge-test-writer`, if available, writes and runs the tests for the `done_when` items. Give it the task id, the `done_when` list and the zones. Review its tests.
  - `forge-reviewer`, if available, does the self-check before ship: ACs, `done_when`, zones, contracts, tests.
  - You (the main agent) write the code. Do not hand it to `forge-implementer`.
- Without subagents, do each of these yourself, including the code, whatever `delegate` says.

## Final report
Tell the user:
- the path used: "solo" or "lead with forge-implementer", with `data.delegation.reason` and the source of the mode (invocation, profile or project);
- what was built and which `done_when` items are proven by which tests;
- if you delegated: how many briefs, how many rounds each needed, and any brief you took over;
- the checks you ran and their results (do not say "passed" unless you ran them);
- proposals you filed (issue numbers);
- open risks or follow-ups;
- the next step: `/forge-ship`, or `/forge-handoff` if you stopped early.
