---
name: release
description: Release a finished milestone in two steps, a changelog PR and then a tag with a GitHub release
args: "<milestone> [--version <x.y.z>]"
roles: [lead]
---
## Goal
Cut the release of a finished milestone. `forge release` works in two steps, and you run the same command for both:
1. It opens a release PR that adds the changelog section. Never push to the default branch: the PR is reviewed and merged first.
2. After the merge it tags the merge commit, creates the GitHub release and closes the GitHub milestone.

You confirm the version with the user and you carry the release PR through review. The CLI does the rest.

## Steps
Check first that the user is a lead for this milestone (`.forge/team.yml`, `plans/roadmap.yml`). The CLI only warns when they are not. If the user is not a lead, say so and ask whether to continue.
1. Check the milestone state:
   `forge status --json --milestone <milestone>`
   Every task must be closed. Open tasks, `blocked:conflict` tasks and open proposals for the milestone are reasons to stop and ask.
2. Ask the user for the version, following semantic versioning (`x.y.z`, no `v` prefix). Suggest one from the merged PR types (breaking, `feat`, `fix`). Do not choose silently.
3. Optional preview. It writes nothing:
   `forge release <milestone> --version <x.y.z> --dry-run --json`
4. Run the release with the confirmed version:
   `forge release <milestone> --version <x.y.z> --json`
5. Read `data.step`:
   - `pr_opened`: the release PR `chore(release): v<x.y.z>` is open. Read its URL from `data.pull.url` and the changelog section from `data.section`. Show both to the user. Ask them to get the PR reviewed and merged, with the project's merge method. When the user says it is merged, run the same command again (step 4). Do not merge the PR yourself.
   - `released`: the tag `v<x.y.z>` exists, the GitHub release is created and the milestone is closed. Read the tag from `data.tag` and the GitHub release URL from `data.release.url`.
6. Show the user what the CLI reported.

## Exit codes
- `forge status`: `0` report; `5` precondition failed; `1` other error.
- `forge release`:
  - `0`: done. Check `data.step` (see step 5). Warnings go into your report. Two matter:
    - the lead warning (the user's login is neither the milestone's lead in `plans/roadmap.yml` nor a team member with role lead): stop and ask the user whether to continue;
    - the release PR was edited by hand, so Forge kept the branch and listed merged task PRs that the changelog section lacks: tell the user, and let them decide whether the section is right.
  - `1`: an error, for example a GitHub or git failure. Read `messages` and report them. Running the command again is safe, because both steps are idempotent.
  - `2`: a pre-push hook refused the push of the release branch `release/v<x.y.z>`. Read the hook output in `messages`. Tell the user what it reports. Fix only what is in your power (for example a failing local check), then run the command again. Do not bypass the hook.
  - `5`: a precondition failed. Do not work around it. Read `messages` and `hint`:
    - open tasks are listed: report them to the user. Do not tag. The milestone is not finished.
    - the version is missing or invalid: the hint gives the suggested next version. Ask the user, then run again with `--version`.
    - the version is already released for another milestone ("already released for <other milestone>"): tell the user. They must pick another version.
    - the tag `v<x.y.z>` already exists on another commit: stop and tell the user. Never move or delete the tag.
    - the merged release PR's commit is not on the default branch (for example the default branch was rewritten): stop and tell the user. The recovery is for the user to find the default-branch commit that carries the `## [<x.y.z>]` section and tag it, with the command from the hint: `git tag -a v<x.y.z> -m "v<x.y.z> (<milestone>)" <commit> && git push origin v<x.y.z>`. Run it only if the user asks you to and confirms the commit. Then run the release again; it accepts that tag and finishes.
    - a local tag `v<x.y.z>` that is lightweight or names another milestone: tell the user. The hint gives the fix (`git tag -d v<x.y.z>`); run it only with their confirmation.
    - the release PR is merged but the default branch has no section (it was reverted): tell the user. They release another version or restore the section through a PR.
    - the milestone does not exist on GitHub: tell the user. The lead creates it with `forge plan apply <milestone>`.
    - other preconditions (CLI version outside `forge_version`, `gh` not logged in, shallow clone): tell the user the fix.
  - `3` and `4`: not expected. Report the output to the user and stop.

## Guardrails
- Lead only. The CLI warns instead of refusing, so you must stop and ask if the user is not a lead.
- Do not release with open tasks.
- Do not pick the version without the user's confirmation.
- Never push to the default branch by hand, and never create, move or delete tags by hand. Only `forge release` tags. The single exception is the recovery tag named in an exit-5 hint, and only when the user explicitly asks for it.
- Do not edit `CHANGELOG.md` by hand for the release, and do not merge the release PR yourself.
- Run the second step only after the user confirms the release PR is merged.

## Subagents
None. Do this work yourself.

## Final report
Tell the user:
- the milestone and the version, and which step you reached: `pr_opened` or `released`;
- after `pr_opened`: the release PR URL, the changelog section, and that they must get it merged and then ask you to run the release again;
- after `released`: the tag name, the GitHub release URL and that the GitHub milestone is closed;
- why you stopped, if you did, and the fix;
- what is next: if the next milestone is not planned yet, `/forge-architect <next-milestone>`, merge its PR, then `/forge-plan <next-milestone>`.
