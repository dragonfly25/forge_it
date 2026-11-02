# CLAUDE.md — building Forge

You are building **Forge**, a Python CLI + templates + prompts that coordinate several people and AI agents working in parallel on one GitHub repository.

## Read first, every session
1. `docs/spec.md` — what to build. It is the source of truth.
2. `docs/build-plan.md` — the phase you are in and its definition of done.
3. `docs/decisions/` — decisions already taken during the build.

Work **one phase at a time**, in order. Do not start a phase before the previous one meets its definition of done. At the end of a phase: run the full test suite and linters, tick the phase in `docs/build-plan.md`, commit, and summarize to the user what was built and how to try it.

## Stack
- Python ≥ 3.11, packaged with `pyproject.toml` (hatchling), src layout: `src/forge/`.
- Dependencies (keep minimal): `typer`, `pyyaml`, `rich`, `packaging`. Ask before adding others.
- Dev tooling: `uv`, `pytest`, `ruff` (lint + format), `mypy` (strict on `src/forge/core`).
- Entry point: `forge = "forge.cli:app"`. Distribution name `forge-cli`, import package `forge`.

## Commands
```bash
uv sync                      # install deps
uv run forge --help          # run the CLI
uv run pytest -q             # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src/forge/core
```

## Architecture rules
- `core/` holds all logic and never imports `typer` or `rich`. `commands/` are thin: parse args, call core, render output.
- All git access goes through `core/git.py` (`Git` class, subprocess, no GitPython).
- All GitHub access goes through the `GitHubClient` protocol in `core/github.py`. Production implementation `GhCliClient` shells out to `gh api` / `gh api graphql`. Tests use `FakeGitHub` (in-memory issues, labels, milestones, comments, PRs).
- The CLI never calls an LLM. It is deterministic. Reasoning lives in `.forge/prompts/*.md`.
- Every command supports `--json` with the envelope `{"ok", "code", "data", "messages"}` and uses the exit codes in spec §16.
- Config/YAML loading validates input and produces errors that name file, key and fix.
- Templates for `forge init` live in `src/forge/templates/project/` and are loaded with `importlib.resources`.
- Never write credentials anywhere. Profiles contain labels only.

## Testing rules
- Every core module has unit tests. Git behaviour is tested against real temporary repos: a bare "remote" plus one clone per simulated user (fixture in `tests/conftest.py`).
- Concurrency-sensitive features (claim, reclaim, post-claim zone verification) need tests with two simulated users racing.
- No test touches the network or a real GitHub repo. A live smoke test may exist behind `FORGE_LIVE_REPO` env var and is skipped by default.

## Conventions
- English everywhere: code, comments, docs, prompts, commit messages.
- Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Small, focused modules; type hints everywhere; docstrings on public functions.
- When the spec is ambiguous or wrong, do not guess silently: write a short ADR in `docs/decisions/NNNN-title.md` (context, decision, consequences) and tell the user.
- For third-party formats that may have changed (Claude Code custom commands/skills, Codex prompts/skills, GitHub APIs), check the current official documentation before implementing and record what you found in an ADR.

## Placeholders
- The framework repository is `<ORG>/forge` until the user provides the real one. When they do: `git remote add origin …` and replace the placeholder everywhere.
- Do not push anywhere until a remote is configured by the user.

## Don'ts
- Don't add a server, database or web UI.
- Don't implement v1/v2 roadmap items (spec §19) unless asked.
- Don't weaken a test to make it pass; fix the code or raise the issue.
