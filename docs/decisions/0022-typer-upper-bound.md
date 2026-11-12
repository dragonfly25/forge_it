# 0022 — Upper bound on typer (`<0.29`)

- Status: accepted
- Date: 2026-11-12

## Context

`pyproject.toml` asked for `typer>=0.12` with no upper bound. `uv.lock` pins 0.27.3, but the lock applies only to development (`uv sync`). Users install Forge with `uv tool install` or `pipx` (spec §17, `install.sh`, `install.ps1`), which resolve the newest versions allowed by `pyproject.toml`.

typer 0.29.0 no longer depends on Click. It ships its own parameter classes, and `TyperOption` has no `type` attribute. Forge builds on Click's API:
- `cli.py`: `TyperGroup.parse_args`, `make_context`, `invoke` and Click's `UsageError`;
- `commands/docs.py`: `forge docs` and `forge init` read `param.type`, `param.opts` and `command.collect_usage_pieces`.

The new installer smoke test in CI (ADR 0021) installed Forge with fresh resolution and got typer 0.29.1. `forge --version` worked, but `forge init` failed with `AttributeError: 'TyperOption' object has no attribute 'type'`. Any user who installs Forge today, on any platform, gets the same failure.

Checked on 2026-11-12: `forge init` works with typer 0.27.3 and 0.28.0, and fails with 0.29.0 and 0.29.1. The CLI test modules pass with 0.28.0.

## Decision

Require `typer>=0.12,<0.29` in `pyproject.toml`. The lock stays at 0.27.3.

Porting Forge to typer 0.29's Click-free API is separate work. It touches the root group (usage errors exit 1, global options after the subcommand), the `forge docs` introspection and the generated `docs/commands.md`. The bound is lifted once that port is done and tested.

## Consequences

- New installs get typer 0.28.x at most, and `forge init` and `forge docs` work again.
- The `install-windows` CI job installs with fresh resolution, so it detects the next breaking release of a dependency. The other jobs use the lock.
- Follow-up: port to typer ≥ 0.29, or replace typer with Click directly. Adding Click as a direct dependency needs the user's approval (CLAUDE.md).
