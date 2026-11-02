# 0003 — CLI version compatibility with `forge_version`

- Status: accepted
- Date: 2026-11-02

## Context

`.forge/config.yml` pins the CLI with a PEP 440 specifier such as `forge_version: ">=0.1,<0.2"` (spec §6.1). Development builds carry versions like `0.1.0.dev0`. Under PEP 440, `0.1.0.dev0 < 0.1`, so a dev build would fail its own project's pin. Pre-releases are also excluded by default.

## Decision

- `forge_version` is validated as a `packaging.specifiers.SpecifierSet` when the config loads. If it is invalid, a `ConfigError` names the key and an example fix.
- The compatibility check compares the CLI's **base version** (`Version(__version__).base_version`, so `0.1.0.dev0` and `0.1.0rc1` both become `0.1.0`) with `prereleases=True`. Dev and pre-release builds of 0.1.0 therefore satisfy `>=0.1,<0.2`.
- The check is a pure function in `core/config.py`, `check_cli_version(spec, version) -> bool`. Callers decide on the consequence: `setup`/`status` warn, `next`/`ship` refuse with exit 5 (spec §17).

## Consequences

- A `0.2.0.dev0` build counts as 0.2.0 and is refused by `<0.2`. That is intended.
- Tests cover release, dev, rc and out-of-range versions.
