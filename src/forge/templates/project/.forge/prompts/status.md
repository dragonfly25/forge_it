---
name: status
description: Show where you, the team and the milestone stand, and suggest the next action
args: "[--me] [--milestone <id>]"
roles: [dev, lead]
---
## Goal
Give the user a short, accurate picture: what they are working on, what the team is doing, what is queued or blocked, how the milestone is going, and what needs attention.

## Steps
1. Get the report:
   `forge status --json`
   Add `--me` when the user only cares about their own work (it keeps the Me and Queue sections). Add `--milestone <id>` to look at one milestone.
2. Read the sections: Me, Team, Queue, Blocked, Milestone, Health.
3. Write a concise summary in plain words. Lead with what needs action. Then list the rest in a few lines.
4. Suggest next actions, based on what you see:
   - drift or a pending sync on my task: `/forge-sync`;
   - my claim is stale: `forge reclaim <n>` (ask first);
   - my PR has failing CI: look at the failure, then `/forge-dev`;
   - a PR is waiting for review and I am a reviewer candidate: `/forge-review <pr>`;
   - handoffs are waiting, or the queue has ready tasks: `/forge-next`;
   - proposals await the lead: `/forge-plan` (lead only);
   - `blocked:conflict` tasks: say who must act;
   - health warnings (CLI version, profile constraints, newer presets): `/forge-setup`.

## Exit codes
- `0`: the report was produced, with or without warnings. Include the warnings that matter.
- `5`: a precondition failed (no `.forge/config.yml`, `gh` not logged in, shallow clone, or `--agent` names an agent the profile does not have). Read `messages` and tell the user the fix. `/forge-setup` helps with logins and profiles.
- `1`: another error, such as a failed fetch. Report `messages`.
- `2`, `3` and `4` are not expected. Report the output.

## Guardrails
- Read-only. Do not run any command that changes state to "fix" what you see. Suggest it and wait.
- Report what the CLI says. Do not guess task states from labels.
- Keep it short. Do not paste the whole JSON.

## Subagents
None. Do this work yourself.

## Final report
Tell the user, in this order:
- what needs action now;
- my current task, with last activity and CI state;
- the team: who works on what, stale claims, PRs in review, handoffs waiting;
- the queue: how many tasks are ready, and the next three you would claim;
- the milestone: done and total, and uncovered acceptance criteria;
- the suggested next action, as one command.
