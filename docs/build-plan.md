# Forge — Build plan (v0)

Phases are executed in order. Each phase ends with: tests green, ruff and mypy clean, this file updated (tick the box), one or more conventional commits, and a short summary to the user with how to try what was built.

Section references (§) point to `docs/spec.md`.

---

## [x] Phase 0 — Scaffold
- [x] `pyproject.toml` (hatchling, src layout, entry point `forge`), `src/forge/{__init__,__main__,cli}.py` with `forge --version`.
- [x] ruff, mypy, pytest configuration; `tests/` with a smoke test.
- [x] `.github/workflows/ci.yml` for the framework repo itself (lint, types, tests on Python 3.11 and 3.12).
- [x] `README.md` skeleton, `CHANGELOG.md`, `docs/decisions/0000-template.md`.
- [x] Draft `install.sh` (§17) — structure only; uses the real repository URL (ADR 0001), version defaults to `main` until the first tag.

**Done when:** `uv run forge --version` prints the version; `uv run pytest` passes; CI workflow is valid YAML.

Also done in Phase 0: naming (ADR 0001), build subagents in `.claude/agents/` (ADR 0002).

## [x] Phase 1 — Core foundations
- [x] `core/config.py`, `core/team.py`, `core/zones.py`, `core/profile.py`: dataclasses + YAML loaders with validation (§6), clear error messages. Profile agents have `providers` (list), `tiers`, `effort`; config has the `models` constraints block.
- [x] `core/models.py`: model resolution (§6.5 point 6) and constraint checking; `src/forge/presets/*.yml` for claude-code, codex, ollama.
- [x] `core/zones.py`: path → zones matching, overlap check between zone sets (names, `conflicts`, glob intersection).
- [x] `core/git.py`: `Git` wrapper (fetch, rev-parse, merge-base, diff names, commit, push with lease, rebase start/continue/abort, worktree add/remove, config).
- [x] `core/github.py`: `GitHubClient` protocol, `GhCliClient` (gh subprocess), `FakeGitHub` for tests.
- [x] `core/output.py`: JSON envelope + exit codes (§16).
- [x] `tests/conftest.py`: fixture creating a bare remote and N user clones with identities.

Progress notes: shared `core/errors.py`, `core/tiers.py`, `core/validate.py`, `core/presets.py`; CLI global options + `--json --version`; ADRs 0003–0007. Open items carried to Phase 3: idempotent re-claim when a claim push landed before an interruption; whether Forge commits/pushes bypass user git hooks (ADR 0007).

**Done when:** loaders reject invalid files with precise messages (tests); model resolution precedence tested (task tier, min_tier_for, raise-only overrides, unsupported parameters); constraint violations detected; zone overlap cases covered; Git wrapper tested on real temp repos; FakeGitHub supports issues, labels, milestones, comments, assignees, PRs.

## [ ] Phase 2 — Task model
- `core/tasks.py`: metadata block parse/render (§7.1), preserving human text outside markers; label conventions; derived state computation (§7.2).
- `core/graph.py`: dependency graph, cycle detection, waves (topological levels).
- `forge refresh` command (recompute labels, unblock dependents).

**Done when:** round-trip tests on issue bodies; cycle detection and wave tests; `forge refresh` against FakeGitHub flips blocked → ready when deps close.

## [ ] Phase 3 — Claim and lease
- `core/claim.py`: candidate selection and ordering (§8.1), claim commit with trailers, CAS push (new and resume), post-claim zone verification and rollback, tier filtering by profile.
- `core/lease.py`: last-activity computation, stale detection, lease comment upsert.
- Commands: `forge next`, `forge task show`, `forge heartbeat`, `forge release-claim`, `forge reclaim`.
- Worktree creation and `.forge/local/current.json`.

**Done when:** tests prove (a) two users racing for the same task → exactly one wins, the other gets the next candidate; (b) two users claiming different tasks with overlapping zones → exactly one keeps its claim; (c) stale claim can be reclaimed, fresh one cannot; (d) handoff tasks are preferred; (e) `--json` output matches the envelope.

## [ ] Phase 4 — Status
- `core/report.py` + `forge status` with all sections in §11.3 (`--me`, `--milestone`, `--json`), including drift detection on the task's zones, version check and profile-constraint/preset health.

**Done when:** snapshot tests of status JSON for a seeded scenario (in-progress, stale, blocked, in-review, handoff, ready queue).

## [ ] Phase 5 — Sync and conflicts
- `core/sync.py`: preflight (wip commit, fetch, backup ref), analysis and classification (§10.2), rebase, mechanical resolvers (`regenerate`, `union`), `sync.json` report, `--continue` (checks + push with lease + sync log), `--abort` (restore backup), escalation path (exit 4, label, comments).
- `forge escalate` command.

**Done when:** scenario tests on temp repos: no-conflict rebase; lockfile conflict auto-resolved by `regenerate`; changelog via `union`; code conflict → exit 2 with correct report, then `--continue` after manual resolution pushes; out-of-zone conflict → exit 4 with rebase aborted; `--abort` restores the exact previous tip; failing check → exit 2.

## [ ] Phase 6 — Ship, review, handoff, propose
- `forge ship` (§11.1) with PR body from template, out-of-zone list, conflict resolutions, agent/providers/model map, cross-provider reviewer selection based on `providers` overlap.
- `forge review` (context + `--submit`).
- `forge handoff` (§8.3), `forge propose`.

**Done when:** FakeGitHub tests verify PR body sections, labels, reviewer selection rules, handoff comment + unassign + label, proposal issue shape.

## [ ] Phase 7 — Planning
- `core/spec.py`: spec frontmatter + AC extraction + open-questions check.
- `core/plan.py`: roadmap and plan file models, `validate` (all errors/warnings in §9.4, waves, coverage matrix), `apply` (idempotent upsert rules in §9.5).
- Commands: `forge plan validate`, `forge plan apply`.

**Done when:** validation test matrix (each error and warning); applying the same plan twice changes nothing; edits to in-progress tasks follow §9.5; removed tasks handled per rules.

## [ ] Phase 8 — Init, templates, prompts, adapters, setup
- `src/forge/templates/project/`: every project-level file in §5.2, including `.forge/rules.md`, the 10 prompts (§12) written in full, spec/ADR templates, workflows (§14), issue/PR templates, `docs/forge/*` (onboarding, roles, faq).
- Adapters: `generic` (AGENTS.md), `claude` (verify current format first, ADR), `codex` (verify current mechanism first, ADR).
- Subagents (§12.1): templates for `explorer`, `test-writer`, `reviewer` in `.forge/agents/`; prompts delegate to them when available and work without them.
- Machine-level rendering of subagents per adapter with concrete models (verify current Claude Code / Codex subagent formats first, ADR).
- Commands: `forge init`, `forge adapters generate [--check] [--local]`, `forge setup [--check]` (machine checks, tool detection, interactive profile with presets and project recommendations, constraint check, rerere, machine-level adapters, `setup.commands`), `forge profile show|set|edit`.

**Done when:** `forge init` on an empty repo produces the expected tree (snapshot test); `forge setup` interactive flow tested with simulated input (accept defaults, edit a tier, local models from a stubbed `ollama list`); changing the profile re-renders `.claude/agents/forge-*.md` with the new models and they stay gitignored; `adapters generate --check` detects drift; `forge setup --check` reports each missing prerequisite with a fix; prompts reviewed against §12 (each states goal, CLI calls, exit-code handling, guardrails, final report).

## [ ] Phase 9 — Release, docs, polish
- `forge release` (§11.4).
- `forge docs [--check]` generating `docs/forge/commands.md`.
- Framework docs: `README.md` (install, quickstart), `docs/protocol.md`, `docs/architecture.md`, `docs/adapters.md`; finalize `install.sh`.
- Optional Projects v2 mirroring (§15) if time allows; otherwise record it as v1.

**Done when:** a new developer can follow README + onboarding without other help (checked by reading them end to end); `forge docs --check` passes in CI.

## [ ] Phase 10 — Dogfooding
- Manual end-to-end checklist on a sandbox GitHub repository (provided by the user): init → plan a mini milestone with 4 tasks → two users claim in parallel → one sync with a conflict → ship → cross review → merge → dependents unblock → release.
- Fix what breaks; record findings in `docs/decisions/` or the CHANGELOG.

**Done when:** the checklist passes end to end and v0.1.0 is tagged locally.
