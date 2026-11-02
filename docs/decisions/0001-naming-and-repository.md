# 0001 — Naming and repository

- Status: accepted
- Date: 2026-11-02

## Context

The spec and `CLAUDE.md` used `<ORG>/forge` as a placeholder for the framework repository and `forge-cli` as the Python distribution name. The user has provided the real values.

## Decision

| Item | Value |
|---|---|
| Framework repository | `dragonfly25/forge_it` (`origin`) |
| Python distribution name | `forge-it` (replaces `forge-cli`) |
| Import package | `forge` |
| CLI command | `forge` |
| Install URL | `git+https://github.com/dragonfly25/forge_it` (pin with `@<tag>`, e.g. `@v0.1.0`) |

Replaced in `CLAUDE.md` and `docs/spec.md` (§5.1, §17, §20). The repository root in the §5.1 tree is now `forge_it/`.

`<ORG>/<REPO>` in the `.forge/config.yml` example (§6.1) is **not** replaced. It stands for the product repository of a project that adopts Forge, not for the framework repository.

The §5.1 tree was also brought in line with the build plan: it now lists `core/models.py`, `core/output.py` and `src/forge/presets/`.

## Consequences

- Work is pushed to `origin` only on `phase/<n>-<name>` branches, with one PR per phase. Nothing is pushed directly to `main`, and phase PRs are merged by the user (recorded in `CLAUDE.md`).
- `install.sh`, the README and the CI install step use the real URL. Releases are git tags (`v<x.y.z>`), because the package is not published to PyPI.
- The command name `forge` collides with Foundry's `forge` (Solidity toolchain). The README warns about this. A user who needs both can install Forge under another name with `uv tool install` and a wrapper, or put one of them earlier on `PATH`.
