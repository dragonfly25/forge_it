# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project scaffold: Python package `forge-it` (import package `forge`) with the `forge` entry point and `forge --version`.
- CI workflow for the framework repository (lint, types and tests on Python 3.11 and 3.12).
- Draft `install.sh` bootstrap installer.
- ADR 0001 (naming and repository) and ADR 0002 (build subagents), plus the ADR template.
- README skeleton.
- Core foundations (Phase 1):
  - Validated loaders for `.forge/config.yml`, `team.yml`, `zones.yml` and the machine profile. Errors name the file, the key and the fix.
  - Model resolution and profile constraint checks, with presets for claude-code, codex and ollama.
  - Zone glob matching and overlap detection.
  - A `Git` wrapper with compare-and-swap lease pushes.
  - The `GitHubClient` protocol, with a `gh`-backed client and an in-memory `FakeGitHub`.
  - The JSON output envelope and the exit codes.
- Global CLI options `--json`, `--agent` (or `FORGE_AGENT`), `--repo`, `--verbose` and `--yes`. `forge --json --version` prints the JSON envelope.
- ADRs 0003–0007.
- Task model (Phase 2):
  - Parsing and rendering of the issue metadata block. Human text outside the markers is preserved byte for byte, and the parser is aware of code fences.
  - A label catalogue and derived task status (ADR 0008).
  - A dependency graph with cycle detection and waves.
- `forge refresh`: recomputes status and mirror labels from issues, task branches (Forge trailers) and open PRs, and unblocks dependents when their dependencies close.
- Claim and lease (Phase 3, ADR 0009):
  - `forge next`: selects a task (handoff first, then milestone, wave, size, number), filtered by tier and by zones held by live claims or open PRs. It claims the task with a compare-and-swap push, checks for an overlapping claim after the push and yields if needed, then assigns the issue, sets labels and writes the lease comment. It creates the worktree and `.forge/local/current.json` and prints the task context.
  - `forge task show`, `forge heartbeat`, `forge release-claim [--delete-branch]` and `forge reclaim <n>`.
  - Interrupted claims are recovered safely, including two runs under the same identity: a pending-claim record, a per-task lock and a unique `Forge-Run` trailer.
