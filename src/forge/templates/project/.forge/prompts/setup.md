---
name: setup
description: Check this machine, create or update your personal profile, and run the project setup
args: "[--check]"
roles: [dev, lead]
---
## Goal
Make this machine ready to work on the project: prerequisites installed, `gh` logged in, a valid personal profile (which models your agents use for each tier), git rerere on, machine-level adapter files rendered, and the app running locally.

## Steps
1. Run the checks without changing anything:
   `forge setup --check --json`
   Read every failed check in `data.checks`. Each one has a `name`, a `level`, a message and a fix.
   - A failed `profile` check (no profile yet, or an invalid one) is not a blocker. It means "go to step 3 and create the profile". On a first run it is expected, and the call exits 5 because of it.
   - Any other failed check at level `error` is a machine prerequisite (git, `gh`, `gh` login, Python, gitignore patterns, invalid config). Fix it in step 2 before you go on.
   - Warnings (version pin, rerere, outdated machine files, clock skew) and info items do not block. Report them at the end.
2. Fix the machine prerequisites. For interactive logins (`gh auth login`, `claude`, `codex login`), tell the user the exact command and wait. Do not try to log in for them. Run `forge setup --check --json` again. Go to step 3 only when the only failed `error` check left, if any, is `profile`.
3. Show the user what setup will do, before it does anything:
   - The tools it looks for: Claude Code (`claude`), Codex (`codex`) and a local runtime (`ollama`). Say which of these you can see on this machine.
   - For each tool: the preset model for each tier (`strong`, `standard`, `light`) and the project's recommended models. Read the recommended models from `models.recommended` in `.forge/config.yml` (read only). With an existing profile, also run `forge profile show --json` and show the current tier to model map.
   - The project setup commands: the `setup.commands` list in `.forge/config.yml`. Setup runs them in the repository root.
   - That setup turns on git rerere and writes the machine-level agent files, which git ignores.
4. Create or update the profile and run the project setup. Preferred: ask the user to run `forge setup` in their own terminal. It is interactive: the user accepts or edits each tier, effort and the default agent, and confirms each setup command. Wait for them to say it is done.
   Only when the user has explicitly accepted both the defaults and the setup commands, run:
   `forge setup --yes --json`
   `--yes` accepts every default and runs every setup command without asking.
5. Apply small profile changes the user asks for:
   `forge profile set <agent>.<tier>=<value> --json` (for example `claude-pro-1.strong=opus`)
   `forge profile set <agent>.effort.<level>=<value> --json`
6. Re-render the machine-level files when the profile changed outside `forge setup`:
   `forge adapters generate --local --json`
7. Check again:
   `forge setup --check --json`
8. Confirm the app starts locally. Read the project README or `docs/forge/onboarding.md` for the start command. Run it, check it comes up, then stop it.

## Exit codes
- `0`: the call worked. For `--check`, warnings may remain. Report them. A CLI version outside `forge_version` is only a warning here, but tell the user to install the pinned version: `forge next` and `forge ship` refuse to run until then.
- `1`: a read, write or validation error. Read `messages`, fix the named file or key, run the call again once. A failing setup command in `forge setup` also exits 1: report which command failed and its output.
- `2`: not expected from these calls. Report the output to the user.
- `3`: not expected from these calls. Report the output to the user.
- `4`: not expected from these calls. Report the output to the user.
- `5`: a required check failed. Read `messages`, apply the fix, or ask the user to.
  - Machine prerequisites (git, `gh` missing or not logged in, Python, gitignore patterns, invalid `.forge/config.yml`): stop and fix them. Run `forge setup --check --json` again. Do not go on to the profile step until they pass.
  - A missing or invalid profile: not a stop. Create or repair it in step 4. Then run `forge setup --check --json` again. It must exit 0.
  - After `forge setup`: exit 5 means the profile was written but a prerequisite is still missing. Fix it and run `forge setup --check --json`.
- For `forge profile set`: `1` means an unknown agent, tier or value. Run `forge profile show --json` and correct the key.
- For `forge adapters generate --local`: `5` usually means a rendered file would not be ignored by git. Add the missing pattern from `messages` to `.gitignore`, then run it again.

## Guardrails
- Never write credentials, tokens or keys anywhere. The profile holds labels only.
- Do not edit `.forge/config.yml`, `team.yml` or `zones.yml` here. They belong to the lead.
- Do not edit the profile file by hand. Use `forge profile set` or ask the user to run `forge profile edit`.
- Do not install system packages without telling the user what and why.
- Do not run `forge setup --yes` until the user has seen the detected tools, the preset and recommended models and the `setup.commands` list, and has explicitly accepted the defaults and the commands. Prefer plain `forge setup` in the user's terminal.
- Do not run any `setup.commands` entry yourself outside `forge setup`.

## Subagents
None. Do this work yourself.

## Final report
Tell the user:
- which checks passed, which warnings remain and what each means;
- the active profile: agent ids, tier to model map, default agent;
- whether the app started locally, and the command that works;
- what is still manual (logins, edits) and the next step: `/forge-status`, then `/forge-next`.
