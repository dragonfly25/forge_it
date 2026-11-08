---
name: ship
description: Sync, run the checks, and open or update the task's pull request
args: "[--draft]"
roles: [dev]
---
## Goal
Publish the finished task as a pull request: synced with the default branch, checks green, out-of-zone changes listed, reviewers requested.

## Steps
1. Self-check before shipping (see Subagents). Fix what you find. Commit the fixes.
2. Ship. Add `--draft` only when the user asks for a draft PR:
   `forge ship --json`
   `forge ship --draft --json`
   The CLI syncs first, runs the checks (unless the last sync already passed them on this commit), computes out-of-zone changes, creates or updates the PR, sets `status:in-review` and requests reviewers. The PR records the delegation decision in the Agent section (for example ``Delegation: auto → delegated (size M) to `forge-implementer` (`sonnet`)``). It is the decision the agent followed, taken from the task's local state, which keeps your `--delegation` choice. It is not proof of what the agent did. Do not edit that line.
3. If it exits 2, follow `/forge-sync` (read `.forge/local/sync.json`, resolve, `forge sync --continue --json`, escalate when needed). Then run ship again. If it exits 4, stop: sync escalated the conflict. Do not run ship again. Tell the user who was mentioned.
4. When it exits 0, read the PR URL, the out-of-zone changes, the conflict resolutions and the requested reviewers from `data`.
5. Report the CI state of the PR:
   `forge status --me --json`
   CI may still be running. Say so. Do not wait in a loop.

## Exit codes
- `forge ship`:
  - `0`: PR created or updated. Continue with step 4. Warnings (for example reviewers that could not be requested) go into your report.
  - `2`: you must act. Sync conflicts, failing checks, or unzoned files with `unzoned: error`. Read `messages` and `.forge/local/sync.json`. Fix your own code and run ship again. For unzoned files, tell the user that a zone is missing in `.forge/zones.yml` (the lead adds it) or that the file does not belong in this task.
  - `4`: escalation from sync. Stop. Tell the user who was mentioned.
  - `5`: a precondition failed (no current task, claim not yours or stale, CLI version outside `forge_version`, shallow clone, sync already in progress: finish it with `/forge-sync`). Read `messages`, fix or ask the user. Do not work around it.
  - `1`: another failure. Report `messages`. If it says that re-running completes the remaining steps, run ship once more.
  - `3`: not expected. Report the output.
- `forge status`: `0` report; `5` precondition failed; `1` other error. If it fails, still report the PR URL from ship.

## Guardrails
- Never merge the PR. Reviewers and the team merge.
- Do not ship with failing checks. Do not skip or edit the checks to get a pass.
- Do not edit the block between `<!-- forge:pr-begin -->` and `<!-- forge:pr-end -->` in the PR body. Text outside it is yours.
- Explain every out-of-zone change in the PR text outside the Forge block, or remove it from the branch.
- Do not push by hand and do not create the PR with `gh`. Only `forge ship` does.

## Subagents
- If `forge-reviewer` is available, delegate the self-check to it: acceptance criteria, `done_when`, zones, contracts and tests. Fix its blocking findings first.
- Without it, do the same checks yourself against the task context from `forge task show --json`.

## Final report
Tell the user:
- the PR URL, title and whether it is a draft;
- the delegation line in the PR body;
- reviewers requested;
- out-of-zone changes (or "none") and conflict resolutions (or "none");
- the check results and the CI state, noting if CI is still running;
- anything left for the user (a missing zone, an unrequested reviewer).
