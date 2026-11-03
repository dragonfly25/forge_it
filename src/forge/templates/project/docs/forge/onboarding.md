# Onboarding: from install to your first merged task

This guide takes you from a fresh machine to a merged pull request. It works with any AI agent that can run shell commands and read files.

## How you talk to Forge

Forge has ten commands for your agent. You can invoke each one in three ways:

| Where | How | Example |
|---|---|---|
| Claude Code | slash command, with the `forge-` prefix | `/forge-next` |
| Codex | skill, with the `forge-` prefix | `$forge-next` |
| Any agent | plain words; the agent reads `AGENTS.md` | "take the next task" |

The prefix avoids clashes with built-in commands such as `/status` and `/review`. Whatever you use, the agent reads the same instructions in `.forge/prompts/`.

The agent coordinates through the `forge` CLI. The CLI never calls an AI model. It does the exact work: claims, sync, pull requests, status.

## 1. Install

You need:
- git 2.38 or newer;
- the GitHub CLI `gh`, logged in with repository access (`gh auth login`);
- macOS or Linux (Windows through WSL).

Install the CLI, pinned to the version this project uses:

```bash
uv tool install "git+https://github.com/dragonfly25/forge_it@{{forge_install_ref}}"
```

With pipx instead:

```bash
pipx install "git+https://github.com/dragonfly25/forge_it@{{forge_install_ref}}"
```

Check it works: `forge --version`.

## 2. Clone and set up your machine

```bash
git clone https://github.com/{{repo}}.git
cd <repo>
forge setup
```

`forge setup` does these things:
1. Checks git, `gh`, Python and the Forge version the project needs.
2. Finds the AI tools you have (Claude Code, Codex, a local model runtime).
3. Proposes which model each tool uses for each tier (`strong`, `standard`, `light`). Accept the defaults or edit them. The result is your personal profile, stored outside the repository. It holds labels only, never credentials.
4. Turns on git rerere, which remembers conflict resolutions.
5. Writes the machine-level agent files (the subagents), which git ignores.
6. Runs the project's setup commands.

Run `forge setup --check` any time to see what is wrong. Change a model later with `forge profile set <agent>.<tier>=<model>`. Inside an agent session you can also ask for `/forge-setup`.

## 3. See where the project stands

Open your agent in the repository and run `/forge-status` (Claude), `$forge-status` (Codex) or ask "what is the status?". You see your current task, what the team is doing, the queue and the milestone.

## 4. Take a task

Run `/forge-next`. Forge picks the best available task for you and claims it. The claim is exclusive: nobody else can take the task, or work in its zones, while you hold it.

The agent shows the task, its zones and an implementation outline. Read it. Reply "go" when you agree, or tell it what to change. It does not write code before you confirm.

The task lives in its own worktree, in the folder `<repo>-worktrees/task-<n>`. Work there.

## 5. Build it

Run `/forge-dev`. The agent implements the task inside its zones, writes tests for each `done_when` item, makes small commits and runs the project checks. If it finds work the task does not cover, it files a proposal instead of doing it. The lead decides what happens to proposals.

On a long session, the agent sends heartbeats so your claim does not look stale.

If you run out of tokens or time, run `/forge-handoff`. Anyone can then continue from your note. See the FAQ.

## 6. Stay in sync

Run `/forge-sync` at least once a day, and whenever status reports drift. It rebases your branch on the default branch. Most of the time nothing conflicts. When something does, the agent resolves it so that both changes survive, or escalates when the problem is not yours.

## 7. Ship

Run `/forge-ship`. Forge syncs, runs the checks, and opens a pull request for you. It lists any file outside your zones, lists conflict resolutions, and requests a reviewer, preferring someone whose AI provider differs from yours.

## 8. Review and merge

A teammate runs `/forge-review <pr-number>`. Their agent checks the pull request against the acceptance criteria, `done_when`, zones, contracts and tests, then submits a review. When the pull request is approved and CI is green, merge it with the project's merge method (squash by default). The merge closes the task's issue. The next refresh unblocks the tasks that depended on it.

Then run `/forge-next` again.

## For the lead: adopt Forge in a new project

The lead runs `forge init` once, then:
1. Edits `.forge/config.yml`: repository, checks, setup commands, model constraints.
2. Edits `.forge/team.yml`: one entry per person.
3. Edits `.forge/zones.yml`: the areas of the codebase, with contract zones for shared interfaces.
4. Commits and pushes these files, with `AGENTS.md` and the generated adapter files.
5. Writes the roadmap with `/forge-plan roadmap <spec-file>`, then plans the first milestone with `/forge-plan M1`. Both arrive as pull requests. When a plan merges, a workflow creates the task issues.

See `roles.md` for what the lead does through the project.

## Command reference

| Command | What it does |
|---|---|
| `/forge-setup` | Check the machine, create the profile |
| `/forge-plan` | Lead only. Roadmap and milestone plans |
| `/forge-next` | Claim the next task and load its context |
| `/forge-dev` | Implement the current task |
| `/forge-sync` | Rebase on the default branch and resolve conflicts |
| `/forge-ship` | Open or update the pull request |
| `/forge-review` | Review a pull request |
| `/forge-status` | Where everyone stands |
| `/forge-handoff` | Pass the task to someone else |
| `/forge-release` | Lead only. Release a finished milestone |

`docs/forge/commands.md` has the full generated CLI reference once `forge docs` has been run.
