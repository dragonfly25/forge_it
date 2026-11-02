# Forge

Forge is a command-line tool, a set of project templates and a set of agent prompts. Together they let several people work in parallel on one GitHub repository, each with their own AI coding agents (Claude Code, OpenAI Codex, local models, and others).

GitHub is the only coordination bus: tasks are Issues, claims are branches, reviews are PRs. The `forge` CLI is deterministic and never calls an LLM. It handles claims, leases, zones, sync, ship and planning. The reasoning (implementing, resolving a conflict, reviewing) happens in your own agent, guided by prompts in `.forge/prompts/`. There is no server, database or web UI.

## Status

v0 under construction — see [docs/build-plan.md](docs/build-plan.md). Version `0.1.0.dev0`, unreleased. Only `forge --version` works today.

## Prerequisites

- git ≥ 2.38.
- GitHub CLI `gh`, authenticated with the `repo` scope.
- macOS or Linux. On Windows, use WSL.
- `uv` (the installer below sets it up if it is missing).

Note: the `forge` command name collides with Foundry's `forge` (the Solidity toolchain). If you use both, put one of them earlier on `PATH`. See [ADR 0001](docs/decisions/0001-naming-and-repository.md).

## Install

Bootstrap script. It checks git and gh, installs `uv` if needed, then installs Forge:

```bash
curl -fsSL https://raw.githubusercontent.com/dragonfly25/forge_it/main/install.sh | bash
```

With `uv` directly, pinned to a release tag (replace `<version>`, for example `v0.1.0`; no release exists yet):

```bash
uv tool install --python 3.12 "git+https://github.com/dragonfly25/forge_it@<version>"
```

With `pipx`:

```bash
pipx install "git+https://github.com/dragonfly25/forge_it@<version>"
```

## Quickstart

Available from later phases. These commands are not implemented yet.

```bash
forge setup    # check your machine, create your profile
forge next     # claim the next ready task and open its worktree
forge sync     # rebase on the main branch and handle conflicts
forge ship     # run checks, push and open the PR
```

## Development

```bash
uv sync                      # install deps
uv run forge --help          # run the CLI
uv run pytest -q             # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src/forge/core
```

## Documentation

- [docs/spec.md](docs/spec.md): the specification and source of truth.
- [docs/build-plan.md](docs/build-plan.md): build phases and their definition of done.
- [docs/decisions/](docs/decisions/): architecture decision records (ADRs).
- [CHANGELOG.md](CHANGELOG.md): release notes.

## License

TBD.
