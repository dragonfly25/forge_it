---
name: next
description: Pick and claim the next available task, then load its context
args: "[task-id] [--reclaim] [--agent <id>]"
roles: [dev]
---
## Goal
Claim the next task that fits you, load its full context, and agree an implementation outline with the user before any code is written.

## Steps
1. Optional preview. Use it when the user asks what is available, or you want to check before claiming:
   `forge next --dry-run --json`
2. Claim a task. Pass the task id only when the user named one:
   `forge next --json`
   `forge next --task M1-T07 --json` (or `--task 42`, or `--task "#42"` with quotes: an unquoted `#` starts a shell comment)
   Add `--reclaim` only when the user wants to take over a stale task. Add `--agent <id>` only when the user names an agent.
3. Load the full context (task metadata, issue body, spec sections for the covered ACs, zones and their paths, dependency summaries, ADRs, and the latest handoff note if there is one):
   `forge task show --json`
4. Read the code the task touches. Keep your own context small (see Subagents).
5. Summarize for the user: the task id and title, what must be true when done (`done_when`), the zones, the dependencies, the handoff note if any, and the worktree path to work in.
6. Propose an implementation outline: files to change, order of steps, tests to write, risks.
7. Ask the user to confirm the outline. Do not write code before they confirm.

## Exit codes
- `forge next` `0`: claimed, resumed, or "you already hold #n". Continue with step 3. Work in the worktree path from `data`.
- `forge next` `3`: nothing to claim. Read the counts of exclusions in `data` and say why in plain words (blocked by dependencies, zones held by others, tier mismatch, in review). If stale claims exist, offer `--reclaim`. If tasks need a tier you cannot serve, offer `--any-tier` only if the user agrees. Otherwise suggest `/forge-status` or asking the lead. Do not retry in a loop.
- `forge next` `5`: a precondition failed. Common causes and actions:
  - CLI version outside `forge_version`: ask the user to install the pinned version.
  - no profile, or the profile login differs from the `gh` login: run `/forge-setup`.
  - an incomplete claim of yours on #n: run `forge next --task <n> --json` to complete it.
  - dirty working tree or a diverged local branch: show `messages`, ask the user. Do not stash, reset or delete anything yourself.
- `forge next` `1`: a GitHub or git failure. Report `messages`.
- `forge task show` `0`: use the context. `5`: no current task (claim one first). `1`: report.
- `2` and `4` are not expected from these calls. Report the output to the user.

## Guardrails
- Claim at most one task at a time. If you already hold a task, tell the user and suggest `/forge-dev`, `/forge-ship` or `/forge-handoff`.
- Do not write code before the user confirms the outline.
- Do not claim by creating branches or pushing by hand. Only `forge next` claims.
- Do not use `--any-tier` or `--reclaim` unless the user agreed.
- If the context is unclear or the task looks wrong, ask the user. Do not guess.

## Subagents
- If `forge-explorer` is available, delegate step 4 to it: ask for a compact summary of the relevant code, specs and ADRs for the task's zones. Otherwise read them yourself and keep notes short.
- Writing the outline stays with you (the main agent).

## Final report
Tell the user:
- the task claimed (id, title, issue number, tier, size), or why nothing was available;
- the zones and the worktree path;
- the outline you propose, and the question: "Shall I start?";
- any handoff note and any warning from the call.
