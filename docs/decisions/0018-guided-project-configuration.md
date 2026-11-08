# 0018 — Guided project configuration: `/forge-configure` and `forge config validate`

- Status: accepted (approved 2026-11-08)
- Date: 2026-11-08

## Context

After `forge init` (spec §5.2, ADR 0014) the lead must fill `.forge/config.yml`, `.forge/team.yml` and `.forge/zones.yml` by hand. Init pre-fills only what it can detect: the GitHub repository, the lead's login and the Forge version.

`forge setup` (§6.5) is guided, but it covers the machine and the personal profile only. Its prompt says explicitly that the three project files belong to the lead.

The hard part of the project files needs reasoning about the codebase:
- which areas become zones;
- which zones are contracts;
- which zones must never run in parallel;
- which lint, typecheck and test commands the project really uses.

A deterministic interactive `forge init` could only ask for globs and commands, which is no better than editing the files. The CLI must never call a model (spec §2, CLAUDE.md), so that reasoning belongs in a prompt.

Today there is also no single command that validates the three files together. Each loader raises on use, for example in `forge next` or `forge plan validate`.

## Decision

### `forge config validate [--json]`
A new deterministic, offline command (no GitHub). It is the agent's feedback loop.

- **Load**: loads `config.yml`, `team.yml` and `zones.yml` independently.
- **Errors**: one error per file that is missing or invalid. Each error names the file, the key and the fix, using the existing `ConfigError` of each loader.
- **Warnings**, on files that load:
  - the team has no member with role `lead`;
  - tracked files (`git ls-files`) match no zone. The count and a sample are reported; Forge's own files (`.forge/`, `.github/`, `.claude/`, `.agents/`, `.codex/`, `AGENTS.md`, `CLAUDE.md`) are ignored;
  - a zone matches no tracked file, which is usually a typo in a glob;
  - the installed CLI does not satisfy `forge_version` (a warning, as in `forge plan validate`).
- **Placeholders**: an init placeholder left in `github.repo` or in a team login is already rejected by the loaders. It is reported as an error on that key, not as a warning.
- **Exit code**: `1` if any file has an error, `0` otherwise (warnings included). It is not on the list of commands that refuse to run on a version mismatch (README).
- **Code layout**: the logic lives in `core/config_check.py`, and `commands/config.py` is thin.

### `/forge-configure` (`$forge-configure` in Codex)
A new prompt, `.forge/prompts/configure.md`, with role `lead`, rendered by the existing adapters like the other ten prompts. The agent:
1. checks that the user is a lead in `team.yml`. Right after `forge init`, that is the person who ran it;
2. explores the repository, with the `explorer` subagent when available: manifests, the CI, the directory layout and the existing docs;
3. proposes `checks`, `setup.commands`, the zones (with `contract` and `conflicts`) and the team (logins, roles, providers). It asks the user for what it cannot see, such as teammates' logins;
4. waits for the user's go before writing anything;
5. writes the files and runs `forge config validate --json` until there are no errors, then discusses the warnings with the user;
6. runs `forge adapters generate` when it changed `adapters` or the prompts;
7. commits on a branch and opens a PR. It never pushes to the default branch.

The prompt can be run again later, for example after a refactor that moves directories. With `--update` it changes only what drifted.

### `forge init` stays
`forge init` still scaffolds the files and installs the skills, including `/forge-configure`. Only its "next steps" change: they point the lead to `/forge-configure` and to `forge config validate`.

## Consequences

- **Prompt count**: eleven prompts instead of ten. `COMMAND_ORDER` places `configure` right after `setup`. The init snapshot and the generated command references change.
- **Spec**: §16 gains `forge config validate`, and §12 gains the `configure` prompt. Where this ADR differs from the spec, the ADR wins.
- **Out of scope**: protecting the lead-only files with CODEOWNERS and branch protection. It is a separate change.
- **Upgrades**: existing projects get the new skill by re-running `forge init` (it merges and keeps files that did not change) or by copying `configure.md` and running `forge adapters generate`.
