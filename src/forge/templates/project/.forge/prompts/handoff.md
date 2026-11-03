---
name: handoff
description: Write a handoff note and pass the current task to another person, account or provider
args: ""
roles: [dev]
---
## Goal
Let someone else resume the current task without asking you anything: publish your work, post a structured note, and free the task.

## Steps
1. Load the task context so the note matches the task:
   `forge task show --json`
2. Collect the facts from this session: `git log` and `git status` in the worktree, what tests you ran and their results, and the decisions you took.
3. Write the note to a file, for example `.forge/local/handoff.md`, with these headings:
   - `## Done`: what is finished, with commits or files.
   - `## Remaining`: what is left, ordered.
   - `## Decisions`: choices taken and why.
   - `## Gotchas`: traps, flaky tests, things that look wrong but are fine.
   - `## Next step`: the one thing to do first.
   - `## How to verify`: the exact commands to run the checks and tests.
4. Re-read the note as a stranger would. Remove anything secret. Say honestly what is failing.
5. Hand off:
   `forge handoff --note-file <file> --json`
   The CLI commits work in progress, pushes the branch, posts the note, unassigns you and sets `status:handoff`.
6. If you have made no progress and only want to free the task, use this instead of a handoff:
   `forge release-claim --json`
   Add `--delete-branch` only if the user asks.

## Exit codes
- `forge handoff`:
  - `0`: handed off. Continue with the final report.
  - `2`: you must act. Two cases:
    - A hook rejected the push. Fix what the hook reports (without weakening tests) and run it again. The wip commit is undone, so your changes are back in the tree.
    - The outcome is `unpublished_work`: the GitHub steps are done, but some local commits or changes were not published. List them for the user and keep the worktree. Do not delete anything.
  - `5`: a precondition failed. Causes: the task has an open PR (handoff is refused: address the review comments yourself, or close the PR first and then hand off), the claim is not yours or is incomplete, a sync or rebase is in progress (finish it with `/forge-sync` or `forge sync --abort`), no current task, a note that is too long, or the push lease was rejected. Read `messages` and tell the user.
  - `1`: another failure. Report `messages`. If a re-run is advised, run it once.
  - `3` and `4`: not expected. Report the output.
- `forge release-claim`: `0` released; `5` no current task or the claim is not yours; `1` other error.

## Guardrails
- Never put credentials, tokens or keys in the note.
- Do not hand off with a sync in progress.
- Do not drop or reset work to make the handoff pass. Nothing may be lost.
- Do not claim something is done unless you ran it.
- The CLI formats and posts the note. Do not post the comment yourself.

## Subagents
None. The note needs your whole session context, so write it yourself.

## Final report
Tell the user:
- the task id and issue number, and that it is now `status:handoff`;
- the branch tip that was pushed;
- a three-line summary of the note (done, remaining, next step);
- any unpublished work or warnings;
- who can continue: anyone, with `/forge-next` (handoff tasks come first).
