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
