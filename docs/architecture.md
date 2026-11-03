# Forge architecture

This document describes how the `forge` CLI is built. For what it does for users, read [protocol.md](protocol.md). For the decisions behind it, read the [ADRs](decisions/).

## Layers

```
People + agents (Claude Code, Codex, local models...)
        |  slash commands / skills / natural language
        v
Adapters (rendered from .forge/prompts/*.md)         docs/adapters.md
        |  the agent runs shell commands
        v
forge CLI (Python, deterministic, never calls an LLM)
   commands/  thin: parse args, call core, render output
   core/      all logic: no typer, no rich
        |  git + gh
        v
GitHub: issues, milestones, labels, branches, PRs, Actions
```

Three rules hold the design together:

- **`core/` never imports `typer` or `rich`.** It returns a `Result` or raises a `ForgeError`. It never prints.
- **All git access goes through `core/git.py`. All GitHub access goes through the `GitHubClient` protocol.** No other module runs `git` or `gh`.
- **The CLI never calls an LLM.** Reasoning lives in `.forge/prompts/*.md`.

Stack: Python 3.11 or newer, `typer`, `pyyaml`, `rich`, `packaging`. Build with `hatchling`, src layout. Dev tools: `uv`, `pytest`, `ruff`, `mypy` (strict on `src/forge/core`).

## Module map

### `src/forge/`

| Module | Purpose |
|---|---|
| `__init__.py`, `__main__.py` | Version, and `python -m forge`. |
| `cli.py` | The Typer app `app` (entry point `forge`). Registers every command. `ForgeGroup` gives usage errors exit 1 and hoists global options written after the subcommand. |

### `src/forge/commands/` (thin)

Each module defines the Typer function for one command or group. It builds the context, calls core, and passes a `Result` to the renderer.

| Module | Command |
|---|---|
| `_output.py` | `GlobalOptions`, `run_command`, `emit`: JSON envelope or human text, error handling. |
| `_context.py` | `AppContext` (repo root, config, `Git`, lazy GitHub client), `load_context`. |
| `_claims.py` | Shared plumbing of the claim commands: current task, profile and agent, claim context. |
| `init.py` | `forge init` |
| `setup.py` | `forge setup [--check] [--yes]` |
| `profile.py` | `forge profile show / set / edit` |
| `adapters.py` | `forge adapters generate [--check] [--local]` |
| `next.py` | `forge next` |
| `task.py` | `forge task show` |
| `heartbeat.py` | `forge heartbeat` |
| `handoff.py` | `forge handoff` |
| `release_claim.py` | `forge release-claim` |
| `reclaim.py` | `forge reclaim` |
| `sync.py` | `forge sync` |
| `escalate.py` | `forge escalate` |
| `ship.py` | `forge ship` |
| `review.py` | `forge review` |
| `propose.py` | `forge propose` |
| `status.py` | `forge status` |
| `refresh.py` | `forge refresh [--radar]` |
| `plan.py` | `forge plan validate` (and the `plan` group) |
| `plan_apply.py` | `forge plan apply` |
| `release.py` | `forge release` |
| `docs.py` | `forge docs [--check]` |

### `src/forge/core/` (all logic)

Configuration and models:

| Module | Purpose |
|---|---|
| `errors.py` | `ExitCode`, `ForgeError`, `ConfigError`, `PreconditionError`. |
| `output.py` | `Message`, `Result`, `to_envelope`, `error_result`, `dumps`: the JSON envelope. |
| `validate.py` | YAML loading and typed field validation (`Fields`) with precise errors. |
| `config.py` | `.forge/config.yml` loader, `check_cli_version`, adapter names. |
| `team.py` | `.forge/team.yml` loader. |
| `zones.py` | `.forge/zones.yml` loader, glob matching, zone overlap. |
| `profile.py` | The machine profile `~/.config/forge/profile.yml`. |
| `tiers.py` | Abstract tiers and effort levels. |
| `presets.py` | Loads `src/forge/presets/*.yml` (claude-code, codex, ollama). |
| `models.py` | Model resolution, tier serving, profile constraint checks, preset drift. |

Git and GitHub:

| Module | Purpose |
|---|---|
| `git.py` | The `Git` class: typed wrapper over the `git` executable. |
| `github.py` | Data classes, the `GitHubClient` protocol, `GhCliClient`, CI status combination. |
| `github_fake.py` | `FakeGitHub`: in-memory client for tests. |

Task model and state:

| Module | Purpose |
|---|---|
| `tasks.py` | Metadata block parse and render, label catalogue, `derive_status`. |
| `graph.py` | Dependency graph, cycle detection, waves, readiness. |
| `refresh.py` | `forge refresh`: gather state, plan label changes, apply them. |
| `radar.py` | `forge refresh --radar`: zone drift and stale-lease comments. |
| `report.py` | The data model behind `forge status`. |

Claims:

| Module | Purpose |
|---|---|
| `claim.py` | Snapshot, candidate selection, claim commit, CAS push, post-claim verification, release. |
| `lease.py` | Lease comment, last activity, staleness, heartbeat. |
| `pending.py` | Pending-claim records, per-task lock, machine id. |
| `workspace.py` | Worktree, branch checkout and `.forge/local/` state. |
| `context.py` | The task context printed by `forge next` and `forge task show`. |
| `handoff.py` | `forge handoff`. |

Sync, ship and review:

| Module | Purpose |
|---|---|
| `sync.py` | `forge sync`: preflight, analysis, rebase, resolvers, continue, abort. |
| `escalation.py` | `blocked:conflict` label and escalation comments. |
| `ship.py` | `forge ship`: sync, checks, zones, PR, reviewers. |
| `pr_body.py` | The Forge block of the PR body (pure). |
| `reviewers.py` | Reviewer selection (pure, deterministic). |
| `review.py` | Review context and review submission. |
| `propose.py` | `forge propose`. |

Planning:

| Module | Purpose |
|---|---|
| `spec.py` | Spec frontmatter, acceptance criteria, open questions. |
| `plan.py` | Roadmap and plan models, offline `validate`. |
| `plan_apply.py` | `forge plan apply`: idempotent issue upsert. |

Project scaffolding and tool files:

| Module | Purpose |
|---|---|
| `init.py` | `forge init`: render the packaged templates. |
| `managed.py` | Forge-managed regions inside human-edited files. |
| `prompts.py` | Load and parse `.forge/prompts/*.md`, `.forge/agents/*.md` and `rules.md`. |
| `setup.py` | `forge setup` checks, wizard and `profile edit`. |
| `docs.py` | `forge docs`: render the command reference. |
| `adapters/__init__.py` | Project-level and machine-level rendering, drift check, writing. |
| `adapters/_common.py` | `GeneratedFile`, `Project`, headers, subagent model resolution. |
| `adapters/generic.py` | `AGENTS.md` managed region. |
| `adapters/claude.py` | Claude Code skills, `CLAUDE.md` region, subagents. |
| `adapters/codex.py` | Codex skills and subagents. |

### Other directories

- `src/forge/presets/`: tier to model presets per tool (`claude-code.yml`, `codex.yml`, `ollama.yml`). They change with Forge releases.
- `src/forge/templates/project/`: the files `forge init` copies, loaded with `importlib.resources`. This tree includes the ten prompts in `.forge/prompts/`.
- `tests/`: unit tests per core module, command tests, shared test worlds in `tests/support/`, snapshots in `tests/snapshots/`.

## The Git boundary

`core/git.py` defines `Git(cwd)`, built on `subprocess.run` (ADR 0007).

- Every call runs with `LC_ALL=C`, `LANG=C`, `GIT_TERMINAL_PROMPT=0` and `GIT_OPTIONAL_LOCKS=0`, so output can be parsed on any machine.
- A non-zero exit raises `GitError`, except for outcomes that are expected: a push that lost a race, a rebase with conflicts, a missing ref, `is_ancestor` returning false. These return result objects (`PushResult`, `RebaseResult`) or `None` or `False`.
- Claim commits are built with `commit-tree`, so they need no checkout and run no commit hooks.
- Pushes take an explicit `Lease(ref, expected_sha | None)`, which maps to `--force-with-lease=<ref>:<sha>`. `None` means "the ref must not exist". A bare `--force` is never used.
- A `[remote rejected]` push is classified by asking the remote for the ref's value, never by matching error text.
- Forge's own event pushes use `--no-verify`. Pushes that carry user code run the user's hooks.

## The GitHub boundary

`core/github.py` defines `GitHubClient`, a `typing.Protocol` over small frozen data classes: `Issue`, `Comment`, `Label`, `Milestone`, `PullRequest`, `Review`, `CiStatus`.

- **`GhCliClient(repo, runner)`** shells out to `gh api`. Lists use `--paginate --slurp`. Writes use `-X METHOD` with a JSON body on stdin. The `runner` is injectable, so tests never reach the network. `gh` failures become `GitHubError`. An authentication failure becomes `PreconditionError` (exit 5). The minimum `gh` version is 2.48.0.
- **`FakeGitHub`** (`core/github_fake.py`) is in-memory and deterministic: an injectable clock and incrementing ids. It mirrors the GitHub rules that matter to Forge: issue and PR numbers are shared, labels must exist before use, lists come back oldest first.
- Commands get the client from `AppContext.github`, built lazily by `_context.github_factory`. Tests replace the factory with `monkeypatch.setattr(_context, "github_factory", ...)`.
- GitHub Actions workflows use `GH_TOKEN`. Forge never stores credentials.
- GraphQL and Projects v2 are not used in v0 (deferred to v1, [ADR 0015](decisions/0015-release-docs.md)).

## Output envelope

([ADR 0004](decisions/0004-output-envelope-and-errors.md))

Every command returns a `Result`. With `--json`, `commands/_output.py` prints exactly one JSON document on stdout:

```json
{"ok": true, "code": 0, "data": {}, "messages": [{"level": "info", "text": "...", "hint": null}]}
```

- `ok` is `code == 0`. `data` is always an object. `messages` is a list of `{level, text, hint}` with level `info`, `warning` or `error`.
- Without `--json`, data goes to stdout as text, and messages go to stderr (`warning: ...`, then `Fix: ...`).
- An uncaught `ForgeError` becomes an envelope (JSON) or `error: ...` plus `Fix: ...` (text), with its exit code.
- Usage errors (a missing option, an unknown command) exit 1 and are not wrapped in the envelope, because they happen before any command runs.
- Global options are `--json`, `--agent` (or `FORGE_AGENT`), `--repo`, `--verbose` and `--yes`. They work before and after the subcommand, so `forge status --json` equals `forge --json status`. An option that the subcommand defines itself, such as `forge setup --yes`, keeps its own meaning.
- Prompts read only `code` and `data`, and `messages` for explanations.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | error (including usage errors) |
| 2 | agent action needed: conflicts, failing checks |
| 3 | nothing available |
| 4 | escalation required |
| 5 | precondition failed: version mismatch, dirty state, not authenticated, not on a task branch |

How each command uses them:

| Command | Exit codes |
|---|---|
| `next`, `reclaim` | 0 claimed; 3 nothing claimable, attempts exhausted, task not claimable, or a fresh claim; 5 preconditions; 1 other |
| `heartbeat`, `release-claim` | 0 done; 5 no current task, claim not yours, not live; 1 other |
| `status` | 0 report; 5 no config, `gh` not logged in, shallow clone, unknown agent; 1 other |
| `task show` | 0 context; errors follow the same rules (5 preconditions, 1 other) |
| `sync` | 0 pushed or up to date; 2 conflicts, failing checks, hook rejection; 4 escalated; 5 preconditions; 1 other |
| `escalate` | 0 escalated; 5 no current task; 1 other |
| `ship` | 0 PR created or updated; 2 sync conflicts, failing checks, unzoned files with `unzoned: error`; 4 escalation from sync; 5 preconditions; 1 other |
| `handoff` | 0 done; 2 hook rejection or unpublished local work; 5 preconditions (including an open PR); 1 other |
| `review` | 0 context or review done; 5 tier refusal, approving your own PR, version mismatch on `--submit`; 1 usage error or failure |
| `propose` | 0 created or existing; 1 error |
| `refresh` | 0 done; 5 missing default branch, shallow clone; 1 a GitHub update failed |
| `plan validate` | 0 valid; 1 errors in the plan; 5 preconditions, tier refusal |
| `plan apply` | 0 applied or dry run; 1 validation errors or failure; 5 preconditions, tier refusal |
| `init` | 0 written; 5 existing files would be overwritten (use `--force`); 1 other |
| `adapters generate` | 0 done or up to date; 1 `--check` found drift; 5 `--local` would write files git does not ignore |
| `setup` | 0 ok (warnings allowed); 5 a required prerequisite is missing; 1 a setup command failed |
| `profile` | 0 done; 1 invalid input |
| `release` | 0 done, with `data.step` `pr_opened` or `released` (or `dry_run`); 2 a pre-push hook refused the release branch; 5 open tasks, missing or invalid version, unknown milestone, version already released for another milestone, tag on another commit, shallow clone, version pin mismatch; 1 other |
| `docs` | 0 written or current; 1 `--check` found drift; 5 the prompts directory is missing |

## Configuration loading and validation

- Every YAML file loads through `core/validate.py`. `load_yaml_mapping` reads the file and `Fields` reads typed keys: strings, ints, bools, lists, enums. Unknown keys are rejected, with a "did you mean" suggestion.
- Each loader returns frozen dataclasses: `load_config` (`ForgeConfig`), the team, zones and profile loaders, and the roadmap, plan and spec loaders in `plan.py` and `spec.py`.
- A problem raises `ConfigError(file, key, problem, fix)`. The text reads `<file>: <key.path>: <problem>`, then `Fix: <fix>`, usually with a YAML example. Exit code 1.
- `forge_version` is validated as a PEP 440 `SpecifierSet`. `check_cli_version` compares the CLI's base version, so `0.1.0.dev0` satisfies `>=0.1,<0.2` (ADR 0003). Callers decide. `setup`, `status`, `escalate`, `propose`, `plan validate` and `review` without `--submit` warn. `next`, `reclaim`, `sync` (except `--abort`, which only warns), `ship`, `plan apply`, `release` and `review --submit` refuse with exit 5.
- The profile is validated on load. `check_profile` reports constraint violations as warnings. It never changes anything.

## Testing strategy

- **Real git.** Git behaviour is tested against real temporary repositories. The `git_world` fixture in `tests/conftest.py` creates a bare "remote" plus one clone per simulated user. An autouse fixture isolates every test from the developer's git config and `HOME`.
- **FakeGitHub.** No test touches the network or a real repository. Commands run against `FakeGitHub` through the `github_factory` hook. Shared worlds in `tests/support/` (`scenarios.py`, `sync_world.py`, `ship_world.py`, `review_world.py`, `plan_world.py`) seed git and GitHub together.
- **Race tests.** Features that depend on concurrency are tested with two simulated users racing: claim (same task, and overlapping zones), reclaim, post-claim verification, sync push leases, plan apply (interleaved creates). Server-side races use `pre-receive` hooks to be deterministic.
- **Snapshot tests.** The `forge status` JSON is compared with files in `tests/snapshots/`, with volatile values replaced by placeholders. `FORGE_UPDATE_SNAPSHOTS=1` rewrites them. It is refused in CI. The `forge init` tree is pinned by a test.
- **Prompt checks.** A test checks every shipped prompt for its frontmatter and required sections, and parses every `forge ...` call in the prompts against the real CLI. A prompt cannot name an option the CLI rejects.
- **Docs.** `forge docs --check` fails when the generated command reference is out of date. `forge adapters generate --check` does the same for adapter files. In a project, CI runs `forge docs --check` with the Forge version pinned in `forge-ci.yml`, so the reference must be generated with that version. The file holds no Forge version, but the command tree and the prompt table can differ between versions.
- **Gates.** `uv run pytest -q`, `uv run ruff check . && uv run ruff format --check .`, and `uv run mypy src/forge/core`. The framework CI runs them on Python 3.11 and 3.12.
- A live smoke test may exist behind the `FORGE_LIVE_REPO` environment variable. It is skipped by default.

## How to add a command

1. **Check the spec and ADRs.** If the behaviour is ambiguous, write an ADR first (`docs/decisions/NNNN-title.md`).
2. **Write the logic in `core/`.** Add a module or extend one. Take a `Git`, a `GitHubClient` and a config as arguments. Return a `Result` (or a data class with `to_data()`). Raise `ForgeError`, `PreconditionError` or `ConfigError` for failures, with a hint that says how to fix it. Do not import `typer` or `rich`. Add type hints, since mypy is strict here.
3. **Write the thin command in `commands/<name>.py`.**

   ```python
   def heartbeat(ctx: typer.Context) -> None:
       """Refresh the lease of the current task's claim."""
       opts = get_options(ctx)

       def run() -> Result:
           app = load_context(opts)
           ...  # call core
           return Result(data=outcome.to_data())

       run_command(opts, run, _render)  # _render(result) -> str for text mode
   ```

   `run_command` renders the result and exits with its code.
4. **Register it in `cli.py`:** `app.command("name", help="...")(module.function)`. For a group, create a `typer.Typer()` and call `app.add_typer(group, name="...")`.
5. **Use the shared exit codes.** Use 2 only for "the agent must act", 3 for "nothing available", 4 for escalation and 5 for preconditions.
6. **Test it.** Unit tests for the core module. A command test that runs through `typer.testing.CliRunner` with `FakeGitHub` and a `git_world` repository, and checks the envelope. A race test if the command writes shared state.
7. **Document it.**
   - If an agent should call it, update the prompt in `src/forge/templates/project/.forge/prompts/` (goal, exact `forge ... --json` calls, exit codes, guardrails, final report). The prompt test checks the call against the CLI.
   - Add the exit codes to the table above, and the rules to [protocol.md](protocol.md).
   - Regenerate this repository's reference with `uv run forge docs --prompts src/forge/templates/project/.forge/prompts --output docs/commands.md` (the CI check runs the same command with `--check`).
   - Add a line to `CHANGELOG.md`.
