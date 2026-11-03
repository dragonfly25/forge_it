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

## [x] Phase 2 — Task model
- [x] `core/tasks.py`: metadata block parse/render (§7.1), preserving human text outside markers; label conventions; derived state computation (§7.2).
- [x] `core/graph.py`: dependency graph, cycle detection, waves (topological levels).
- [x] `forge refresh` command (recompute labels, unblock dependents).

Progress notes: ADR 0008 (task model, labels, derived status, branch event trailers `Forge-Claim/Handoff/Release`); shared `commands/_context.py` for all commands.

**Done when:** round-trip tests on issue bodies; cycle detection and wave tests; `forge refresh` against FakeGitHub flips blocked → ready when deps close.

## [x] Phase 3 — Claim and lease
- [x] `core/claim.py`: candidate selection and ordering (§8.1), claim commit with trailers, CAS push (new and resume), post-claim zone verification and rollback, tier filtering by profile.
- [x] `core/lease.py`: last-activity computation, stale detection, lease comment upsert.
- [x] Commands: `forge next`, `forge task show`, `forge heartbeat`, `forge release-claim`, `forge reclaim`.
- [x] Worktree creation and `.forge/local/current.json`.

Progress notes: ADR 0009 (live claim, lease comment format, safe post-claim verification, idempotent own claim, same-identity runs, hooks, exit codes). Extra core modules: `core/workspace.py` (worktree, `current.json`), `core/context.py` (task context), `core/pending.py` (pending-claim records, task lock, machine id).

**Done when:** tests prove (a) two users racing for the same task → exactly one wins, the other gets the next candidate; (b) two users claiming different tasks with overlapping zones → exactly one keeps its claim; (c) stale claim can be reclaimed, fresh one cannot; (d) handoff tasks are preferred; (e) `--json` output matches the envelope.

## [x] Phase 4 — Status
- [x] `core/report.py` + `forge status` with all sections in §11.3 (`--me`, `--milestone`, `--json`), including drift detection on the task's zones, version check and profile-constraint/preset health.

Progress notes: ADR 0010 (sources, scope, section rules, CI combination, exit codes). The snapshot code in `core/claim.py` is now public and shared by `next` and `status`. New infrastructure: `GitHubClient.commit_ci` and `Git.commits_with_files`. Shared test scenarios live in `tests/support/scenarios.py`, and snapshots in `tests/snapshots/`.

**Done when:** snapshot tests of status JSON for a seeded scenario (in-progress, stale, blocked, in-review, handoff, ready queue).

## [x] Phase 5 — Sync and conflicts
- [x] `core/sync.py`: preflight (wip commit, fetch, backup ref), analysis and classification (§10.2), rebase, mechanical resolvers (`regenerate`, `union`), `sync.json` report, `--continue` (checks + push with lease + sync log), `--abort` (restore backup), escalation path (exit 4, label, comments).
- [x] `forge escalate` command.

Progress notes: ADR 0011 (sync state machine, classification, resolvers, escalation, exit codes). It also amends ADR 0009: the lease comment now records the claim's `Forge-Run` id, so a rebased claim keeps its lease. The ADR 0004 amendment makes usage errors exit 1. New modules: `core/escalation.py`, plus git primitives for conflict stages, unions and rerere, and `GitHubClient.commit_pulls`.

**Done when:** scenario tests on temp repos: no-conflict rebase; lockfile conflict auto-resolved by `regenerate`; changelog via `union`; code conflict → exit 2 with correct report, then `--continue` after manual resolution pushes; out-of-zone conflict → exit 4 with rebase aborted; `--abort` restores the exact previous tip; failing check → exit 2.

## [x] Phase 6 — Ship, review, handoff, propose
- [x] `forge ship` (§11.1) with PR body from template, out-of-zone list, conflict resolutions, agent/providers/model map, cross-provider reviewer selection based on `providers` overlap.
- [x] `forge review` (context + `--submit`).
- [x] `forge handoff` (§8.3), `forge propose`.

Progress notes: ADR 0012 covers the PR body block, reviewer selection, the check skip on an already-checked HEAD (B7), review tier enforcement, handoff steps and proposal shape. New modules: `core/ship.py`, `core/pr_body.py`, `core/reviewers.py`, `core/review.py`, `core/handoff.py`, `core/propose.py`.

**Done when:** FakeGitHub tests verify PR body sections, labels, reviewer selection rules, handoff comment + unassign + label, proposal issue shape.

## [x] Phase 7 — Planning
- [x] `core/spec.py`: spec frontmatter + AC extraction + open-questions check.
- [x] `core/plan.py`: roadmap and plan file models, `validate` (all errors/warnings in §9.4, waves, coverage matrix), `apply` (idempotent upsert rules in §9.5).
- [x] Commands: `forge plan validate`, `forge plan apply`.

Progress notes: ADR 0013 covers offline validation, the two extra AC errors, apply's two Forge regions in the issue body, the rules for started tasks, proposal adoption (`proposal: <n>`) and convergence when two applies run at once. Apply is a separate module, `core/plan_apply.py`.

**Done when:** validation test matrix (each error and warning); applying the same plan twice changes nothing; edits to in-progress tasks follow §9.5; removed tasks handled per rules.

## [x] Phase 8 — Init, templates, prompts, adapters, setup
- [x] `src/forge/templates/project/`: every project-level file in §5.2, including `.forge/rules.md`, the 10 prompts (§12) written in full, spec/ADR templates, workflows (§14), issue/PR templates, `docs/forge/*` (onboarding, roles, faq).
- [x] Adapters: `generic` (AGENTS.md), `claude` (verify current format first, ADR), `codex` (verify current mechanism first, ADR).
- [x] Subagents (§12.1): templates for `explorer`, `test-writer`, `reviewer` in `.forge/agents/`; prompts delegate to them when available and work without them.
- [x] Machine-level rendering of subagents per adapter with concrete models (verify current Claude Code / Codex subagent formats first, ADR).
- [x] Commands: `forge init`, `forge adapters generate [--check] [--local]`, `forge setup [--check]` (machine checks, tool detection, interactive profile with presets and project recommendations, constraint check, rerere, machine-level adapters, `setup.commands`), `forge profile show|set|edit`.

Progress notes:
- **ADR 0014** covers the verified Claude Code and Codex formats, the template tree, managed regions, skills named `forge-<cmd>`, machine-level subagents for Claude and Codex, setup, profile, the radar (`forge refresh --radar`) and the workflows.
- **ADR 0004 amendment**: global options are also accepted after the subcommand.

**Done when:** `forge init` on an empty repo produces the expected tree (snapshot test); `forge setup` interactive flow tested with simulated input (accept defaults, edit a tier, local models from a stubbed `ollama list`); changing the profile re-renders `.claude/agents/forge-*.md` with the new models and they stay gitignored; `adapters generate --check` detects drift; `forge setup --check` reports each missing prerequisite with a fix; prompts reviewed against §12 (each states goal, CLI calls, exit-code handling, guardrails, final report).

## [x] Phase 9 — Release, docs, polish
- [x] `forge release` (§11.4).
- [x] `forge docs [--check]` generating `docs/forge/commands.md`.
- [x] Framework docs: `README.md` (install, quickstart), `docs/protocol.md`, `docs/architecture.md`, `docs/adapters.md`; finalize `install.sh`.
- [x] Optional Projects v2 mirroring (§15) if time allows; otherwise record it as v1.

Progress notes: ADR 0015.
- **Release** takes two steps: a release PR carrying the CHANGELOG section, then the tag, GitHub release and milestone close. It never pushes to the default branch.
- **`forge docs`** builds the reference from the Click tree; the framework repo checks `docs/commands.md` in CI.
- **Projects v2** is deferred to v1.

**Done when:** a new developer can follow README + onboarding without other help (checked by reading them end to end); `forge docs --check` passes in CI.

## [ ] Phase 10 — Dogfooding
- Manual end-to-end checklist on a sandbox GitHub repository (provided by the user): init → plan a mini milestone with 4 tasks → two users claim in parallel → one sync with a conflict → ship → cross review → merge → dependents unblock → release.
- Fix what breaks; record findings in `docs/decisions/` or the CHANGELOG.

**Done when:** the checklist passes end to end and v0.1.0 is tagged locally.
