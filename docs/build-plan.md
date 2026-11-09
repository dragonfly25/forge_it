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

## [x] Phase 10 — Dogfooding
- [x] Manual end-to-end checklist on a sandbox GitHub repository (provided by the user): init → plan a mini milestone with 4 tasks → two users claim in parallel → one sync with a conflict → ship → cross review → merge → dependents unblock → release.
- [x] Fix what breaks; record findings in `docs/decisions/` or the CHANGELOG.

Progress notes:
- **Run**: on `dragonfly25/forge_sandbox`, with two simulated users under one account. Every checklist step passed; the report is `docs/dogfooding-v0.1.0.md`.
- **Fixes**: ten findings, all fixed. The main one was GitHub's issue-list lag (ADR 0007 amendment).
- **Version and tag**: the version is bumped to `0.1.0`. The `v0.1.0` tag is created locally on `main` once this phase's PR is merged.

**Done when:** the checklist passes end to end and v0.1.0 is tagged locally.

## [x] Phase 11 — Development agent and delegation switch (v0.2)
- [x] `implementer` subagent template, rendered as `forge-implementer` (Claude, Codex).
- [x] Delegation modes `off | auto | on`, decided per invocation (`--delegation`), per person (profile) and per project (`config.yml` `delegation:` policy); capability fallback to `off`.
- [x] `core/delegation.py` decision; `delegation` in the task context, PR body and status; setup wizard question; `forge profile set delegation=…`.
- [x] `dev` prompt with a lead path (brief → `forge-implementer` → verify) and the current solo path; docs.

See ADR 0017.

Progress notes:
- **Reviews**: two passes; every high and medium finding fixed.
- **Equivalence with v0.1.0**: with `delegation: off`, `next` and `task show` are compared against golden files captured from v0.1.0's code.
- **Override scope**: the per-invocation override is saved for the task in `current.json`, and `ship` and `status` read it.

**Done when:**
- the decision matrix tests pass;
- `forge init` writes `implementer.md` and `adapters generate --local` renders `forge-implementer` with the profile's model;
- `task show --json` reports the decision and `--delegation` overrides it within the project policy;
- the PR body records the decision;
- the `dev` prompt has both paths and passes the prompt checks;
- with `delegation: off` the output equals v0.1.0's apart from the new `delegation` field.

## [x] Phase 12 — Guided project configuration
- [x] `forge config validate [--json]`: offline check of `config.yml`, `team.yml` and `zones.yml` together, with placeholder, lead, unzoned-file and empty-zone warnings (`core/config_check.py`).
- [x] `configure` prompt (role `lead`), rendered as `/forge-configure` and `$forge-configure`.
- [x] `forge init` next steps point to `/forge-configure`; README, project docs and CHANGELOG updated.

See ADR 0018.

**Done when:**
- every error and warning of `forge config validate` has a test, and a broken file does not hide the results of the others;
- `forge init` writes `configure.md` and the adapters render the `forge-configure` skill;
- the `configure` prompt passes the prompt checks;
- `forge docs --check` passes.

## [ ] Phase 13 — Milestone architecture
- [ ] `core/architecture.py`: load and validate `docs/architecture/<M>.md` and `overview.md` (frontmatter components and interfaces, mandatory body sections, open questions), with every error and warning of ADR 0019.
- [ ] `forge arch validate <M> [--force] [--json]` (`commands/arch.py`), offline, with the `models.min_tier_for.architecture` check.
- [ ] Config `planning` block (`architecture: required | optional`, defaulting to `optional` when missing; `max_tasks_per_agent` and `max_components_per_agent` validated for phase 14).
- [ ] Plan tasks gain `component`, `implements`, `uses`; `forge plan validate` gains the architecture errors and warnings. `plan apply` and the issue regions do not change.
- [ ] `architecture` in the task context of `forge task show` and `forge next`.
- [ ] `architect` prompt (role `lead`), rendered as `/forge-architect` and `$forge-architect`. `plan`, `dev`, `review`, `explorer` and `reviewer` read the architecture.
- [ ] Templates `docs/architecture/_template.md` and `overview.md`, written by `forge init`.
- [ ] Docs: README lead quickstart, project `docs/forge/{onboarding,roles,faq}.md`, `docs/protocol.md`, CHANGELOG, regenerated `docs/commands.md`.

See ADR 0019.

**Done when:**
- `tests/core/test_architecture.py` has one test per error and warning;
- `tests/core/test_plan.py` has one test per new error and warning, plus the `required`, `optional` and missing-key cases;
- the `arch validate` command tests cover the JSON envelope and exit codes 0, 1 and 5;
- `forge init` creates `docs/architecture/`, and the init snapshot and prompt checks (12 prompts) pass;
- the `architect` prompt passes the prompt checks;
- a plan without an architecture fails with `planning.architecture: required` and passes when the key is missing;
- the ADR 0017 golden test passes with only the `architecture` field added;
- `forge docs --check` passes.

## [ ] Phase 14 — Multi-agent planning
Placeholder. This phase will let several agents plan one milestone in parallel. The batches will be bounded by `planning.max_tasks_per_agent` and `planning.max_components_per_agent`, which phase 13 already validates. ADR 0020 will specify the scope, the steps and the definition of done before work starts.
