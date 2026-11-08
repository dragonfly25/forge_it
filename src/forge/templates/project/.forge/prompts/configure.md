---
name: configure
description: Fill the project's config, team and zones from the codebase, validate them and open a PR
args: "[--update]"
roles: [lead]
---
## Goal
Fill `.forge/config.yml`, `.forge/team.yml` and `.forge/zones.yml` so that they match the real project: the checks it runs, the people who work on it and the areas of code that tasks can own. `forge init` only pre-fills the repository, the lead's login and the Forge version. You read the codebase, propose the rest, and write it after the user agrees. The CLI validates the files. The result arrives as a PR for the team to review.

With `--update`, the files already exist and are in use. Change only what drifted, for example after a refactor that moved directories.

## Steps
Check first that the user is a lead (`.forge/team.yml`). Right after `forge init`, the lead is the person who ran it. If the user is not a lead, say so and stop.

1. Get the current state. Run the validator before you change anything:
   `forge config validate --json`
   Read the errors and warnings. They show what is still a placeholder, which zones match no file and how many files have no zone. With `--update`, they also show what drifted.
2. Explore the repository. Read:
   - manifests: `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml` and the like;
   - the `Makefile` or task runner, for the lint, typecheck and test commands;
   - CI under `.github/workflows/`, for the commands CI really runs;
   - the directory layout, one or two levels deep;
   - the README and the docs, for the architecture and for shared interfaces.
   Read the three project files too, and `plans/*.yml` if it exists. Keep the comments in the files.
3. Ask the user what you cannot see, and wait for the answers:
   - the GitHub login, roles (`dev`, `lead`) and AI providers (`anthropic`, `openai`, `local`) of each teammate;
   - which areas are shared interfaces (API schemas, database schema, shared types);
   - which areas must never be changed in parallel.
   Do not invent a login. Leave a teammate out until the user gives one.
4. Propose the changes. Show them as diffs or as concise tables, one for each file:
   - `config.yml`: `checks` (the lint, typecheck and test commands the project really uses, in the order they must run) and `setup.commands` (install steps for a fresh clone). Add `adapters` only if the user wants other tools. Do not touch `models` or `delegation` unless the user asks.
   - `team.yml`: one member per person, with `github`, `roles` and `providers`.
   - `zones.yml`: for each zone, a name (lowercase letters, digits, `-`, `_`, with `/` between parts), its path globs, and `contract: true` for shared interfaces. Add `conflicts` for zones that must not run in parallel although their paths differ. Set the `unzoned` policy (`warn` or `error`).
   Size the zones so that parallel tasks rarely collide. A zone that covers most of the code serializes the team. A zone for every file creates busywork. Aim for areas that one person can change in a day without touching the others. Put code that many areas import into a contract zone.
5. Wait for the user's explicit go. Do not write any file before it. If the user changes the proposal, show the new version and wait again.
6. Write the files. Keep the existing comments and the key order. Then validate, and repeat until there are no errors:
   `forge config validate --json`
   Fix each error. Each message names the file, the key and the fix.
7. Go through the warnings with the user, one group at a time:
   - files with no zone: add them to a zone, add a zone, or accept them and say why;
   - a zone that matches no file: usually a typo in a glob;
   - a placeholder in `github.repo` or in a login;
   - no member with the role `lead`;
   - a CLI version outside `forge_version`.
   Change the files if the user agrees, and validate again.
8. If you changed `adapters` in `.forge/config.yml`, render the tool files again:
   `forge adapters generate --json`
9. Open a PR. Check the state of git first: the default branch is `project.default_branch` in `.forge/config.yml`.
   - Create the branch `forge/configure`. Commit the `.forge/` files, plus the adapter files if you regenerated them. Push the branch and open a PR titled `chore: configure forge` with `gh pr create`. In the body, list the zones, the checks, the team and the warnings you accepted.
   - If the default branch has no commits yet (a brand-new repository), explain that a PR needs a base commit. Let the user decide: make the first commit themselves, or tell you another way. Do not push to the default branch.

## Exit codes
- `forge config validate`:
  - `0`: the three files load. Warnings may remain. Read them (step 7).
  - `1`: one or more files have errors, or a file is missing. `messages` names the file, the key and the fix. Fix them and run the call again.
  - `2`, `3`, `4` and `5`: not expected. Report the output to the user.
  - It is offline and reads no GitHub data.
- `forge adapters generate`:
  - `0`: the files were written, or were already current.
  - `1`: an error, for example an invalid `.forge/config.yml` or a file that cannot be written. Read `messages`, fix the cause (run `forge config validate` for the config) and run it again.
  - `2`, `3` and `4`: not expected. Report the output to the user.
- `git` and `gh` failures when you push or open the PR (for example `gh` is not logged in): report the message and the fix. Do not work around it.

## Guardrails
- Lead only. If the user is not a lead, stop.
- Do not write any file before the user gives an explicit go.
- Never write credentials, tokens or keys in any file. Profiles and project files hold labels only.
- Do not invent GitHub logins, names or providers. Ask.
- Do not edit the user's personal profile. That is for `/forge-setup`.
- Do not change `models` or `delegation` unless the user asks.
- Do not delete or rename a zone that tasks in `plans/*.yml` reference without asking the user first. Say which tasks use it. If you rename or remove a zone, update the plans in the same PR only with the user's agreement.
- Do not remove comments from the files, and do not add keys that the loaders reject.
- Never push to the default branch. The files go through a PR.
- Do not run any `setup.commands` entry yourself. They run in `forge setup`.

## Subagents
- If `forge-explorer` is available, delegate step 2 to it: ask for a compact summary of the manifests, the check commands, the CI jobs and the directory layout, and of the areas that look like shared interfaces. Otherwise read them yourself and keep notes short.
- Proposing, writing and validating stay with you (the main agent).

## Final report
Tell the user:
- what you wrote or changed in each of the three files;
- the validation result: errors fixed, warnings that remain and why you accepted them;
- the PR URL and its title, or why you could not open one;
- what is next: merge the PR, then each person runs `forge setup`, then the lead runs `/forge-plan roadmap <spec-file>`.
