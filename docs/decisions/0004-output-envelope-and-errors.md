# 0004 — JSON envelope, messages and error reporting

- Status: accepted
- Date: 2026-11-02

## Context

Spec §16 requires every command to support `--json` with `{"ok", "code", "data", "messages"}` and the exit codes 0–5. The spec does not define the shape of `messages`, how errors map to the envelope, or how `--version` behaves with `--json`.

## Decision

- `messages` is a list of objects `{"level": "info" | "warning" | "error", "text": str, "hint": str | null}`.
- `ok` is `code == 0`. `data` is always an object (`{}` when there is nothing to return).
- `core/errors.py` defines `ExitCode` and `ForgeError(message, hint, code)`. Subclasses: `ConfigError(file, key, problem, fix)` and `PreconditionError` (exit 5). Core raises them. Core never prints and never imports typer or rich.
- `core/output.py` holds `Message`, `Result` and `to_envelope(result) -> dict`, plus `error_result(exc: ForgeError) -> Result`.
- In the CLI, `commands/_output.py` owns rendering. JSON mode prints exactly one JSON document on stdout and nothing else. Text mode prints human output with rich, and sends warnings and errors to stderr. Any uncaught `ForgeError` becomes the envelope (JSON) or `error: …` / `Fix: …` (text), with its exit code.
- Global options (§16) are parsed in the root callback and stored as `GlobalOptions` on `ctx.obj`: `--json`, `--agent`, `--repo`, `--verbose`, `--yes`. They are accepted **before** the subcommand (`forge --json status`).
- `forge --json --version` prints `{"ok": true, "code": 0, "data": {"version": "…"}, "messages": []}`. Plain `forge --version` prints `forge <version>`.
- Config error text reads `<file>: <key.path>: <problem>`, followed by `Fix: <fix>`.

## Consequences

- Agents can parse every command the same way. Prompts reference only `code` and `data`.
- Later phases add their command-specific `data` schemas to `docs/forge/commands.md` (Phase 9).

## Amendment (Phase 5): usage errors exit 1

Command-line usage errors exited 2 through Typer and click: a missing required option, an unknown option or command, a bad value. Spec §16 reserves exit 2 for "agent action needed", so an agent could take a typo for a conflict to resolve. The root command group (`ForgeGroup` in `cli.py`) now gives every usage error exit 1. The rich error message stays as it is. Usage errors are not wrapped in the JSON envelope even with `--json`, because they happen before any command runs.

## Amendment (Phase 8): global options after the subcommand

Spec §12 and §16 write calls as `forge status --json` and `forge next --agent X --json`, and the shipped prompts follow that form. The root command group (`ForgeGroup`) therefore also accepts the global options `--json`, `--agent`, `--repo`, `--verbose`/`-v` and `--yes`/`-y` **after** the subcommand. It hoists them in front of the subcommand before parsing, with three exceptions:
- An option the subcommand defines itself is left in place. For example, `forge setup --yes` uses setup's own `--yes`.
- Nothing after a `--` separator is hoisted.
- A token that is the value of one of the subcommand's own options is never hoisted.

Both positions mean the same thing. When a global option is written in both places, the one after the subcommand wins: hoisted options are inserted right after the root options, and click keeps the last value.

Two limits:
- Global options between the names of a nested command (`forge task --json show 42`) are not supported.
- A global option followed by another option gets no value, and click reports the missing value. A test parses every `forge …` call in the shipped prompts against the real CLI, so a prompt cannot name an option the CLI rejects.
