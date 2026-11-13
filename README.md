# Forge

Forge is a command-line tool, a set of project templates and a set of agent prompts. Together they let several people work in parallel on one GitHub repository, each with their own AI coding agents (Claude Code, OpenAI Codex, local models, and others).

GitHub is the only coordination bus. Tasks are Issues, claims are branches, reviews are PRs. The `forge` CLI is deterministic and never calls an LLM. It handles claims, leases, zones, sync, ship, planning and releases. The reasoning (designing, implementing, resolving a conflict, reviewing) happens in your own agent, guided by prompts in `.forge/prompts/`. There is no server, database or web UI.

## Status

Forge v0.1.0 is the first release. It was dogfooded end to end on a sandbox GitHub repository; see [docs/dogfooding-v0.1.0.md](docs/dogfooding-v0.1.0.md).

## Prerequisites

- git ≥ 2.38.
- GitHub CLI `gh` ≥ 2.48, authenticated with the `repo` scope (`gh auth login`).
- macOS, Linux (WSL included) or Windows 10/11 natively (PowerShell or cmd) with Git for Windows.
- `uv` (the installer below sets it up if it is missing). `uv` provisions Python itself. Forge needs Python ≥ 3.11.

Note: the `forge` command name collides with Foundry's `forge` (the Solidity toolchain). If you use both, put one of them earlier on `PATH`. See [ADR 0001](docs/decisions/0001-naming-and-repository.md).

## Install

Bootstrap script. It checks git and gh, installs `uv` if needed, then installs the newest release tag (or `main` when there is none yet).

macOS and Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.sh | bash
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.ps1 | iex
```

To pin a version, set `FORGE_VERSION`, or use `uv` or `pipx` directly (replace `v0.1.0` with the tag your project uses):

```bash
curl -fsSL https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.sh | FORGE_VERSION=v0.1.0 bash
uv tool install --python 3.12 "git+https://github.com/dragonfly25/forge_it@v0.1.0"
pipx install "git+https://github.com/dragonfly25/forge_it@v0.1.0"
```

On Windows (PowerShell), pin with:

```powershell
$env:FORGE_VERSION = "v0.1.0"; irm https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.ps1 | iex
```

Check it with `forge --version`. Forge is not published to PyPI. Releases are git tags. A project pins the CLI version it needs in `.forge/config.yml` (`forge_version`); see [the protocol](docs/protocol.md#13-cross-cutting-rules) for what a mismatch does.

### Windows notes

- Checks, resolver commands and `setup.commands` run in `cmd.exe` on Windows and in `/bin/sh` elsewhere. Forge does not translate them. A team that mixes platforms writes commands that work in both (for example `uv run pytest -q` or `npm test`), or calls a script.
- Your profile is `%APPDATA%\forge\profile.yml` on Windows. `$XDG_CONFIG_HOME` overrides the location on every platform.
- `forge setup` turns on git's `core.longpaths`, so deep trees such as `node_modules` check out in task worktrees.
- Claude Code subagents get the tools `Bash, PowerShell`, so they have a shell with or without Git Bash.

Details: [ADR 0021](docs/decisions/0021-native-windows.md).

## Quickstart for the lead

You adopt Forge in a product repository once. Everything below runs in a clone of that repository. In Claude Code the commands are `/forge-<name>`. In Codex they are `$forge-<name>`.

Steps 1–3 set up the project. One lead does them, once. A lead who joins later, such as a second lead or the lead of a later milestone, installs Forge and logs in as in step 1, clones the repository and starts from step 4. A lead is a member with `lead` in their `roles` in `.forge/team.yml`. Each milestone names its lead in `plans/roadmap.yml`.

1. **Install** Forge (above) and log in with `gh auth login`.
2. **Scaffold** the project files: `forge init`. It never overwrites a file without `--force` and never commits.
3. **Configure**: run `/forge-configure`. The agent reads your codebase, proposes `.forge/config.yml`, `team.yml` and `zones.yml`, writes them after you agree, validates them with `forge config validate` and opens a PR. Merge it. If the default branch is protected, push `.forge/`, `AGENTS.md`, `CLAUDE.md`, the adapter files, the specs, plans and docs folders and the workflows through a PR too. Forge never pushes to the default branch.
4. **Set up your machine**: `forge setup`.
5. **Write the specs.** Put the full project description in a Markdown file.
6. **Roadmap**: `/forge-plan roadmap <spec-file>`. Merge the PR.
7. **Architecture**: `/forge-architect M1`. Review and merge the `arch: M1` PR.
8. **Task plan**: `/forge-plan M1`. Large milestones are planned by several agents in parallel ([multi-agent planning](docs/guides/lead.md#multi-agent-planning)). Merge the plan PR. The `forge-plan-apply` workflow then creates the GitHub milestone and one issue per task.
9. Developers can now run `/forge-next`. When every task of M1 is closed, run `/forge-release M1`.

Details for each step: [guide for the lead](docs/guides/lead.md).

## Quickstart for developers

1. **Install** Forge (above) and log in with `gh auth login`.
2. **Clone** the project, then run `forge setup` in it.
3. Open your agent in the repository and work through the loop:

   | Step | Claude Code | Codex |
   |---|---|---|
   | Take a task | `/forge-next` | `$forge-next` |
   | Build it | `/forge-dev` | `$forge-dev` |
   | Stay current | `/forge-sync` | `$forge-sync` |
   | Publish | `/forge-ship` | `$forge-ship` |
   | Review a teammate | `/forge-review <pr>` | `$forge-review <pr>` |

   Other commands: `/forge-status`, `/forge-handoff`, `/forge-setup`. Natural language also works: say "take the next task" and the agent follows the same prompt.
4. After the PR is approved and CI is green, merge it.

The agent calls `forge` for you. You can run the commands yourself too, for example `forge status`. Add `--json` for the machine-readable envelope. Details: [guide for developers](docs/guides/developer.md).

## Documentation

- [docs/guides/lead.md](docs/guides/lead.md): the lead's guide, from setup to release, including the milestone architecture and where files live.
- [docs/guides/developer.md](docs/guides/developer.md): the developer's guide, from `/forge-next` to handoff.
- [docs/protocol.md](docs/protocol.md): the coordination rules (claims, leases, zones, waves, sync, ship, review, planning, release).
- [docs/architecture.md](docs/architecture.md): the Forge CLI's module map, JSON envelope, exit codes and how to add a command.
- [docs/adapters.md](docs/adapters.md): how prompts become Claude Code and Codex files.
- [docs/commands.md](docs/commands.md): the CLI reference, generated by `forge docs`.
- [docs/spec.md](docs/spec.md): the specification. Where an ADR differs, the ADR wins.
- [docs/decisions/](docs/decisions/): architecture decision records.
- [docs/build-plan.md](docs/build-plan.md): build phases.
- [CHANGELOG.md](CHANGELOG.md): release notes.

## Development

```bash
uv sync                      # install deps
uv run forge --help          # run the CLI
uv run pytest -q             # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src/forge/core
```

Regenerate the CLI reference, or check that it is current:

```bash
uv run forge docs --prompts src/forge/templates/project/.forge/prompts --output docs/commands.md
uv run forge docs --prompts src/forge/templates/project/.forge/prompts --output docs/commands.md --check
```

Read `CLAUDE.md` and `docs/spec.md` before changing code. Commits follow Conventional Commits.

## License

TBD.
