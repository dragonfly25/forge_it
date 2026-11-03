---
name: release
description: Release a finished milestone with a changelog, a tag and a closed GitHub milestone
args: "<milestone> [--version <x.y.z>]"
roles: [lead]
---
## Goal
Cut the release of a finished milestone: confirm every task is closed, confirm the version with the user, generate the changelog section, tag the release, close the GitHub milestone and post a summary.

`forge release` arrives in a later Forge release. If the CLI says the command is unknown, tell the user the installed Forge does not have it yet, and stop. Do not do the steps by hand.

## Steps
Check first that the user is a lead for this milestone (`.forge/team.yml`, `plans/roadmap.yml`). If not, warn them and ask whether to continue.
1. Check the milestone state:
   `forge status --json --milestone <milestone>`
   Every task must be closed. Open tasks, `blocked:conflict` tasks and open proposals for the milestone are reasons to stop and ask.
2. Ask the user for the version, following semantic versioning (`x.y.z`). Suggest one from the merged PR types (breaking, `feat`, `fix`). Do not choose silently.
3. Run the release with the confirmed version:
   `forge release <milestone> --version <x.y.z> --json`
   The CLI verifies all tasks are closed (or lists the open ones and stops), builds the CHANGELOG section from merged PR titles grouped by type, creates the tag `v<x.y.z>`, closes the milestone and posts a summary.
4. Show the user the changelog section, the tag and the summary.

## Exit codes
- `forge status`: `0` report; `5` precondition failed; `1` other error.
- `forge release`: follow the standard codes.
  - `0`: released. Report the tag, the changelog section and the milestone.
  - `1`: an error or unmet condition. If it lists open tasks, report them and do not tag. Read `messages`.
  - `5`: a precondition failed (CLI version outside `forge_version`, `gh` not logged in, shallow clone, dirty or wrong branch). Read `messages`, tell the user the fix.
  - `2`, `3` and `4`: not expected. Report the output to the user and stop.

## Guardrails
- Lead only.
- Do not release with open tasks.
- Do not pick the version without the user's confirmation.
- Never push to the default branch by hand, and never create or move tags by hand. Only `forge release` tags.
- Do not edit `CHANGELOG.md` by hand for the release.

## Subagents
None. Do this work yourself.

## Final report
Tell the user:
- the milestone and the version released, or why you stopped;
- the tag name and the changelog section;
- that the GitHub milestone is closed;
- what is next: `/forge-plan <next-milestone>` if the next milestone is not planned yet.
