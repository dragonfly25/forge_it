---
name: sync
description: Rebase the current task on the default branch, resolve conflicts safely and push
args: "[--abort]"
roles: [dev]
---
## Goal
Bring the task branch up to date with the default branch. Both intents must survive: the upstream change (already reviewed) and your task's change. Escalate when the problem is not yours to solve.

## Steps
1. Run:
   `forge sync --json`
2. If it stops with exit 2, read `.forge/local/sync.json`. Its `state` is `conflicts` or `checks_failed`. It lists the conflicted files, their zones, whether they are contracts, the upstream commits, and the PRs and issues behind them.
3. For each conflicted file, resolve it following the rules in Guardrails:
   1. Read the upstream PR, issue or spec that introduced the change (listed in `sync.json`).
   2. Edit the file so that both changes survive. Adapt your code to the upstream version.
   3. Stage it with `git add <file>`. Do not run `git rebase --continue` yourself.
4. For failing checks, fix the failures that come from your own code, commit the fixes (no rebase is in progress then), and do not touch other people's code.
5. Continue:
   `forge sync --continue --json`
   Repeat steps 2 to 5 until it exits 0 or 4.
6. When the failing area or the conflict belongs to someone else, or to a contract you do not own, escalate:
   `forge escalate --reason "<what conflicts, with whom, why you cannot resolve it>" --json`
7. If the user wants to give up this sync, restore the pre-sync state:
   `forge sync --abort --json`

## Exit codes
- `forge sync` and `forge sync --continue`:
  - `0`: pushed, or already up to date. Report what happened, including resolved conflicts.
  - `2`: you must act. Conflicts remain, checks fail, a hook rejected the wip commit or the push, or paths are still unmerged on `--continue`. Follow steps 2 to 5.
  - `4`: escalation. Forge aborted the rebase, labeled the issue `blocked:conflict` and commented on the issues involved. Stop. Tell the user who was mentioned and why. Do not retry. Do not use `--force-resolve` unless the user explicitly says to.
  - `5`: a precondition failed. Read `messages`. Common cases:
    - no current task, or the claim is not yours: `/forge-next`, but ask the user first;
    - your own claim is stale: `forge next --task <n> --json` renews it, but ask the user first;
    - a sync is already in progress: choose `--continue` or `--abort`;
    - dirty tree after failed checks: commit the fixes, then `--continue`;
    - the push lease was rejected (the branch moved, for example someone reclaimed it): stop and tell the user;
    - a rebase that Forge did not start: ask the user.
  - `1`: another git or GitHub failure. Report `messages`.
  - `3`: not expected. Report the output.
- `forge sync --abort`: `0` restored (report the backup ref if one is named); `5` nothing to abort, or the tree is dirty (commit or discard first, ask the user); `1` other error.
- `forge escalate`: `0` escalation done; `5` no current task; `1` other error.

## Guardrails
- Both intents must survive. The default-branch side is already reviewed: adapt your code to it.
- Read the upstream PR, issue or spec before you resolve a conflict.
- Never resolve by taking one side of a whole file.
- Never change a contract the task does not own. Escalate instead.
- Stop and ask the user when the resolution grows beyond the task's zones, or when your confidence is low.
- Do not run `git rebase`, `git push` or `git reset` yourself. Forge does them.
- Do not use `--force-resolve` without an explicit instruction from the user.
- Do not delete or weaken tests to get the checks green.

## Subagents
Conflict resolution stays with you (the main agent), because it needs the whole context. If `forge-explorer` is available, you may delegate reading the upstream PRs and specs to it, and ask for a compact summary. Otherwise read them yourself.

## Final report
Tell the user:
- the result: up to date, pushed, stopped for action, escalated or aborted;
- the classification (none, textual, semantic, out of zone) and the upstream changes that mattered;
- each conflict, how you resolved it and why both intents survive;
- the check results;
- if escalated: who was mentioned, and what the user should do next.
