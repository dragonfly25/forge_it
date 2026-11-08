# Forge

Forge is a command-line tool, a set of project templates and a set of agent prompts. Together they let several people work in parallel on one GitHub repository, each with their own AI coding agents (Claude Code, OpenAI Codex, local models, and others).

GitHub is the only coordination bus. Tasks are Issues, claims are branches, reviews are PRs. The `forge` CLI is deterministic and never calls an LLM. It handles claims, leases, zones, sync, ship, planning and releases. The reasoning (implementing, resolving a conflict, reviewing) happens in your own agent, guided by prompts in `.forge/prompts/`. There is no server, database or web UI.

Delegation is a switch: your main agent can write the code itself, or act as lead and delegate it to a `forge-implementer` subagent. Choose `off`, `auto` or `on` for this task (saved locally until release or handoff), per person or per project. See [docs/protocol.md](docs/protocol.md) and [ADR 0017](docs/decisions/0017-implementer-subagent-and-delegation.md).

## Status

Forge v0.1.0 is the first release. It was dogfooded end to end on a sandbox GitHub repository; see [docs/dogfooding-v0.1.0.md](docs/dogfooding-v0.1.0.md) for the checklist, the findings and what was not exercised.

## Prerequisites

- git ≥ 2.38.
- GitHub CLI `gh` ≥ 2.48, authenticated with the `repo` scope (`gh auth login`).
- macOS or Linux. On Windows, use WSL.
- `uv` (the installer below sets it up if it is missing). `uv` provisions Python itself. Forge needs Python ≥ 3.11.

Note: the `forge` command name collides with Foundry's `forge` (the Solidity toolchain). If you use both, put one of them earlier on `PATH`. See [ADR 0001](docs/decisions/0001-naming-and-repository.md).

## Install

Bootstrap script. It checks git and gh, installs `uv` if needed, then installs the newest release tag. When the repository has no release tag yet, it installs `main` and says so:

```bash
curl -fsSL https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.sh | bash
```

To pin a version with the script, set `FORGE_VERSION`:

```bash
curl -fsSL https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.sh | FORGE_VERSION=v0.1.0 bash
```

With `uv` directly, pinned to a release tag (replace `v0.1.0` with the tag your project uses):

```bash
uv tool install --python 3.12 "git+https://github.com/dragonfly25/forge_it@v0.1.0"
```

With `pipx`:

```bash
pipx install "git+https://github.com/dragonfly25/forge_it@v0.1.0"
```

Check it with `forge --version`. Forge is not published to PyPI. Releases are git tags.

A project pins the CLI version it needs in `.forge/config.yml` (`forge_version`). `forge setup`, `forge status`, `forge escalate`, `forge propose`, `forge plan validate`, `forge sync --abort` and `forge review` (without `--submit`) warn on a mismatch. `forge next`, `forge reclaim`, `forge sync` (except `--abort`), `forge ship`, `forge plan apply`, `forge release` and `forge review --submit` refuse to run with exit 5.

## Quickstart for the lead

You adopt Forge in a product repository once. Everything below runs in a clone of that repository.

1. **Install** Forge (above) and log in with `gh auth login`.
2. **Scaffold** the project files:
   ```bash
   forge init                       # add --adapters generic,claude,codex to choose tools
   ```
   Init never overwrites a file without `--force` and never commits.
3. **Edit** the three files that describe your project:
   - `.forge/config.yml`: the GitHub repository, `checks` (lint, typecheck, test), `setup.commands`, `resolvers` and model constraints.
   - `.forge/team.yml`: one entry per person, with GitHub login, roles (`dev`, `lead`) and AI providers.
   - `.forge/zones.yml`: the areas of your codebase as path globs. Mark shared interfaces as `contract: true`.
4. **Commit and push** the `.forge/` files, `AGENTS.md`, `CLAUDE.md`, the generated adapter files, the specs/plans folders, the docs and the three workflows in `.github/workflows/`. If the default branch is protected, push a branch and open a PR for these files, and merge it before the next steps. Forge itself never pushes to the default branch.
5. **Set up your machine**: `forge setup`. It checks your tools, creates your profile, and renders your subagents.
6. **Write a spec.** Put the full project description in a Markdown file, or fill `specs/_template.md` for a milestone. In your agent, run `/forge-plan roadmap <spec-file>`, then `/forge-plan M1`. In Claude Code the commands are `/forge-plan`. In Codex they are `$forge-plan`.
7. **Merge the plan PRs.** When `plans/M1.yml` reaches the default branch, the `forge-plan-apply` workflow runs `forge plan validate` and `forge plan apply`. It creates the GitHub milestone and one issue per task.
8. Developers can now run `/forge-next`.
9. When all tasks of a milestone are closed, run `/forge-release M1`. It opens a release PR, and after the merge it tags and closes the milestone.

## Quickstart for developers

1. **Install** Forge (above) and log in with `gh auth login`.
2. **Clone** the project and set up your machine:
   ```bash
   git clone https://github.com/<owner>/<repo>.git
   cd <repo>
   forge setup                      # checks, profile, rerere, subagents, setup commands
   ```
3. Open your agent in the repository and work through the loop:

   | Step | Claude Code | Codex | What happens |
   |---|---|---|---|
   | Take a task | `/forge-next` | `$forge-next` | Claims the best task for you and opens its worktree. The agent shows the context and an outline, then waits for your go. |
   | Build it | `/forge-dev` | `$forge-dev` | Implements the task inside its zones, with tests and small commits. |
   | Stay current | `/forge-sync` | `$forge-sync` | Rebases on the default branch and resolves conflicts. |
   | Publish | `/forge-ship` | `$forge-ship` | Syncs, runs checks, opens or updates the PR, requests a reviewer. |
   | Review a teammate | `/forge-review <pr>` | `$forge-review <pr>` | Checks the PR against the task and submits a review. |

   Other commands: `/forge-status`, `/forge-handoff` (pass your task on), `/forge-setup`.
4. Natural language works in any agent. Say "take the next task" or "sync my branch". The agent reads `AGENTS.md` and follows the same prompt.
5. After the PR is approved and CI is green, merge it. The merge closes the issue and unblocks dependent tasks.

The agent calls `forge` for you. You can also run the commands yourself, for example `forge status` or `forge next --dry-run`. Add `--json` to any command to get the machine-readable envelope.

## Where things live

| What | Where | Committed |
|---|---|---|
| Project settings, team, zones, rules | `.forge/config.yml`, `team.yml`, `zones.yml`, `rules.md` | yes |
| Command prompts and subagent definitions | `.forge/prompts/`, `.forge/agents/` | yes |
| Tool files rendered from them | `AGENTS.md`, `CLAUDE.md`, `.claude/skills/forge-*/`, `.agents/skills/forge-*/` | yes |
| Specs, roadmap and task plans | `specs/`, `plans/roadmap.yml`, `plans/<milestone>.yml` | yes |
| Workflows | `.github/workflows/forge-*.yml` | yes |
| Your profile (tiers to models, agent labels, no credentials) | `~/.config/forge/profile.yml` | no |
| Rendered subagents with your models | `.claude/agents/forge-*.md`, `.codex/agents/forge-*.toml` | no (gitignored) |
| Local task state | `<worktree>/.forge/local/` | no (gitignored) |
| Worktrees | `<parent-of-repo>/<repo>-worktrees/task-<n>/` | no |

Task state itself lives on GitHub: issues, milestones, labels, branches `task/<n>` and PRs.

## Documentation

- [docs/protocol.md](docs/protocol.md): how people and agents coordinate (claims, leases, zones, waves, sync, ship, review, planning, release).
- [docs/architecture.md](docs/architecture.md): module map, the Git and GitHub boundaries, the JSON envelope and exit codes, testing, how to add a command.
- [docs/adapters.md](docs/adapters.md): how prompts become Claude Code and Codex files, and how to add an adapter.
- [docs/commands.md](docs/commands.md): the CLI reference. It is generated by `forge docs`.
- [docs/spec.md](docs/spec.md): the specification. Where an ADR differs, the ADR wins.
- [docs/decisions/](docs/decisions/): architecture decision records (ADRs). Start with [0015](docs/decisions/0015-release-docs.md) for the release flow and [0014](docs/decisions/0014-init-adapters-setup.md) for the tool formats.
- [docs/build-plan.md](docs/build-plan.md): build phases and their definition of done.
- [CHANGELOG.md](CHANGELOG.md): release notes.

A project that adopts Forge gets its own guides in `docs/forge/`: `onboarding.md`, `roles.md`, `faq.md` and the generated `commands.md`. In a project, re-run `forge docs` after editing a prompt, and run it with the Forge version pinned in `.github/workflows/forge-ci.yml`. CI runs `forge docs --check` with that version, and a file generated by another version can differ.

## Development

```bash
uv sync                      # install deps
uv run forge --help          # run the CLI
uv run pytest -q             # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src/forge/core
```

Regenerate the CLI reference of this repository, or check that it is current. `uv run` uses the source in this checkout, which is also what this repository's CI runs:

```bash
uv run forge docs --prompts src/forge/templates/project/.forge/prompts --output docs/commands.md
uv run forge docs --prompts src/forge/templates/project/.forge/prompts --output docs/commands.md --check
```

Read `CLAUDE.md` and `docs/spec.md` before changing code. Commits follow Conventional Commits.

## License

TBD.
