# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-13

### Added

- **Native Windows support** (ADR 0021). Forge runs from PowerShell and cmd, without WSL, on Windows 10/11 with Git for Windows:
  - **`install.ps1`**: the PowerShell installer. It checks git and gh (and offers `winget install --id GitHub.cli`), installs `uv`, installs the newest release tag or `FORGE_VERSION`, and prints the next steps.
  - **Claims, releases and handoffs** work on Windows. The per-task lock uses `LockFileEx` (`msvcrt.locking`). The `flock` requirement and its exit-5 refusal are gone.
  - **Automatic takeover** of an interrupted claim works on Windows, through a real process-liveness check.
  - **UTF-8** for every text file, every git and gh call and piped output (`--json`), so non-ASCII titles and paths are safe.
  - **Commit, tag and `gh` input** reaches the child byte for byte, so trailers never gain a `\r` on Windows.
  - **Time zones**: `tzdata` is a dependency on Windows only, so `team.yml` time zones validate there.
  - **CRLF checkouts are not drift**: `adapters generate --check`, `docs --check` and `init` treat CRLF and LF as equal.
  - **`.cmd` shims** such as npm's `claude` and `codex` are found.
  - **Locations**: the profile is `%APPDATA%\forge\profile.yml` and the machine id is `%LOCALAPPDATA%\forge\machine-id`. `XDG_CONFIG_HOME` and `XDG_STATE_HOME` override both on every platform.
  - **`core.longpaths`**: `forge setup` turns it on, and `forge setup --check` warns when it is off.
  - **Claude Code subagents** get `Bash, PowerShell` as tools on Windows.
  - **CI** runs on Windows and macOS, as well as Linux.
  - Checks, resolver commands and `setup.commands` run in `cmd.exe` on Windows. Forge does not translate them.
- **Multi-agent planning** (ADR 0020):
  - **Thresholds**: `planning.max_tasks_per_agent` (default 10) and `planning.max_components_per_agent` (default 3) now take effect. Above the threshold, `ceil(N / max)` agents get balanced groups (12 tasks → 6 + 6, 21 → 7 + 7 + 7). The grouping is deterministic.
  - **`forge plan validate <M> --skeleton`**: skips only the `missing_field` errors for `description`, `done_when` and `tier`. Every other check runs in full.
  - **`forge plan split <M>`**: offline. Checks the skeleton, groups the tasks by component and wave, writes `.forge/local/plan/<M>/batches.json` and deletes stale fragments. Reports whether `forge-planner` is available. Exits 0, 1 and 5.
  - **`forge plan assemble <M>`**: offline and atomic. Checks the fragments `part-<k>.yml`, merges `description`, `done_when` and `tier` into the skeleton, validates the merged plan and rewrites `plans/<M>.yml`. Errors name the fragment, the task and the field. It rewrites the file from data, so YAML comments are lost. Exits 0, 1 and 5.
  - **`forge arch split <M>`**: offline. Groups the components of `<M>.md`, assigns each interface to its producer's group and writes the fragment directory `.forge/local/arch/<M>/`. There is no `arch assemble`: the main session integrates the fragments. Exits 0, 1 and 5.
  - **Two subagents**: `planner` (`forge-planner`) writes the detail fields of one group of tasks. `arch-writer` (`forge-arch-writer`) writes the component and interface sections of one group. There are now six subagents. `reviewer` also reviews a plan group or an architecture.
  - **Prompts**: `/forge-plan` and `/forge-architect` work in four phases (exploration, skeleton or structure, detail, review) and spawn subagents in parallel. Without subagents they do the same work one group at a time. `--update` stays single-session. The prompt count stays 12.
- **Milestone architecture** (ADR 0019):
  - **`docs/architecture/`** in the project: `overview.md` maps the system, and `<M>.md` describes what one milestone adds or changes (components with zones, interfaces in contract zones, decisions, risks, open questions). `forge init` writes `_template.md` and an empty `overview.md`.
  - **`/forge-architect <M>` prompt** (`$forge-architect` in Codex): the lead's agent reads the specs and the code, proposes components and interfaces, writes `<M>.md`, updates `overview.md` and opens the PR `arch: <M>`. With `--update` it changes only what moved. This is the twelfth prompt.
  - **`forge arch validate <M> [--force]`**: checks `<M>.md` and `overview.md` offline. It ignores `planning.architecture`. It enforces `min_tier_for.architecture` unless `--force` or CI. Exits 0, 1 and 5.
  - **Plan fields `component`, `implements`, `uses`**: plan-only, like `proposal`. `plan apply` does not write them to issues.
  - **`forge plan validate` architecture checks**: errors `architecture_missing`, `architecture_invalid`, `architecture_draft`, `unknown_component`, `unknown_interface`, `interface_unimplemented`, `interface_multiply_implemented`, `contract_without_interface` and `uses_without_dependency`. Warnings `zone_outside_component`, `implementer_not_producer` and `architecture_fields_unchecked`.
  - **`planning` block in `.forge/config.yml`**: `architecture: required | optional`, plus `max_tasks_per_agent` and `max_components_per_agent` (the group bounds of `forge plan split` and `forge arch split`). `min_tier_for` accepts `architecture`.
  - **Task context**: `forge task show` and `forge next` gain an `architecture` object (`path`, `component`, `implements`, `uses`) and a matching `## Architecture` block.
- **Guided project configuration** (ADR 0018):
  - **`forge config validate`**: checks `config.yml`, `team.yml` and `zones.yml` together, offline. Each file that is missing or invalid gives one error that names the file, the key and the fix (exit 1). Warnings cover init placeholders, a team without a lead, files that match no zone, zones that match no file and a CLI outside `forge_version`.
  - **`/forge-configure` prompt** (`$forge-configure` in Codex): the lead's agent reads the codebase, proposes `checks`, `setup.commands`, zones and team members, writes them after the lead agrees, validates them and opens a PR. With `--update` it changes only what drifted. This is the eleventh prompt.
- **Development agent and delegation switch** (ADR 0017):
  - **`implementer` subagent**: a fourth subagent, rendered as `forge-implementer` for Claude Code and Codex. It writes the code of a task from a brief written by the main session, which acts as lead.
  - **Delegation modes**: `off`, `auto` (default) or `on`. `auto` delegates tasks of size M or with two or more zones.
  - **Who decides**, most specific first:
    - the task: `forge next`/`forge task show --delegation`, or `/forge-dev --solo` and `--delegate`, saved for the task;
    - the person: the profile field `delegation`, set by `forge setup` or `forge profile set delegation=<mode>`;
    - the project: a `delegation:` policy in `.forge/config.yml`.
  - **Fallback**: tools without native subagents fall back to `off`.
  - **Where it shows**: the task context, the PR body and `forge status` all show the decision.

### Fixed

- **Project workflows** (ADR 0023): `forge init` pinned `astral-sh/setup-uv@v10`, a tag that does not exist, so `forge-ci`, `forge-radar` and `forge-plan-apply` failed before their first step. The templates now pin `astral-sh/setup-uv@v10.3.3`, the version Forge's own CI uses. Existing projects: change the pin in the three workflows by hand, and pin Forge to `v0.2.0` there and in `forge_version` (`">=0.2,<0.3"`).

### Changed

- **Dependencies** (ADR 0022): typer is capped below 0.29. typer 0.29 dropped Click, and fresh installs broke `forge init` and `forge docs`.
- **Windows** (ADR 0021): `forge docs --check` no longer reports a file that differs only in CRLF. `install.sh` points Windows users to `install.ps1` instead of WSL.
- **Multi-agent planning** (ADR 0020): `forge plan assemble` rewrites `plans/<M>.yml` in a fixed format and drops its comments. Existing projects get `planner.md` and `arch-writer.md` by running `forge init` again, then `forge adapters generate --local`. Without them the prompts fall back to sequential work. Old CLIs do not know `split`, `assemble`, `arch split` or `--skeleton`, so the new prompts need the new CLI version in `forge_version`.
- **The plan follows the architecture** (ADR 0019). The documented sequence is now `/forge-plan roadmap` → `/forge-architect M1` → merge the `arch: M1` PR → `/forge-plan M1`. `/forge-plan <M>` assigns `component`, `implements` and `uses` from the architecture and never adds a component or an interface. `/forge-dev`, `/forge-review` and the `explorer` and `reviewer` subagents read the architecture, and the reviewer checks that a change stays inside the task's component.
- **Existing projects**: a `config.yml` without a `planning` block defaults to `architecture: optional`, so existing plans and the `forge-plan-apply` workflow keep working with no edit. New projects get `required` from `forge init`.
- **Upgrade note**: v0.1.0 rejects the `planning` key and the new plan fields as unknown. This release is therefore 0.2.0, and projects that use the new fields must pin `forge_version` to `">=0.2,<0.3"`.
- `forge init` next steps now point the lead to `/forge-configure` and `forge config validate` instead of editing the three project files by hand (ADR 0018). Existing projects get the new prompt by running `forge init` again or by copying `configure.md` and running `forge adapters generate`.

## [0.1.0] - 2026-11-03

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
- `forge status [--me] [--milestone M]` (Phase 4, ADR 0010), read-only. Its sections:
  - Me: current task, last activity, pending sync, drift on my zones, CI of my PRs.
  - Team: in progress with stale flags, in review, handoffs waiting.
  - Queue: ready count and the next 3 picks, identical to `forge next`.
  - Blocked: by dependencies and by `blocked:conflict`.
  - Milestone: done/total, waves, AC coverage.
  - Health: version pin, profile constraints, preset drift, unknown labels, issues without metadata, proposals.
- `forge sync [--continue|--abort] [--force-resolve]` (Phase 5, ADR 0011):
  - Before rebasing: a wip commit if the tree is dirty, a local backup ref, and an analysis that classifies the sync as none, textual, semantic or out-of-zone.
  - It rebases onto the default branch with rerere on. Conflicts in lockfiles go to the `regenerate` resolver and changelog-like files to `union`.
  - Remaining conflicts are left to the agent (exit 2, with `.forge/local/sync.json`). Out-of-zone conflicts, and conflicts in contract zones the task doesn't own, are escalated instead (exit 4).
  - Then it runs the configured checks, pushes with a lease, refreshes the lease and writes `sync-log.json`.
  - `--abort` restores the exact previous state.
- `forge escalate --reason R`: labels the task `blocked:conflict`, comments with the reason and mentions the upstream authors and the leads.
- Usage errors now exit 1 instead of 2, which spec §16 reserves for "agent action needed".
- Ship, review, handoff and propose (Phase 6, ADR 0012):
  - `forge ship [--draft]`: runs sync, then the checks, which are skipped when sync already checked the same HEAD. It lists out-of-zone and unzoned files, then creates or updates the PR. Forge owns one marked block in the PR body, with the closing reference, AC and done-when checklists, out-of-zone changes, conflict resolutions and the agent's model map; human text and ticked boxes are kept. It sets `status:in-review` and requests reviewers, preferring members whose providers differ from the agent's.
  - `forge review <pr>`: prints the review context (task, ACs, diff, zone check, CI, reviews), or submits a review with `--submit`. It refuses agents below `min_tier_for.reviewer` unless `--force`.
  - `forge handoff --note-file F`: pushes the work with a handoff event commit, posts the note, unassigns you and sets `status:handoff`.
  - `forge propose --title T --body-file F`: opens a `status:proposed` issue, or returns the existing one with the same title.
- Planning (Phase 7, ADR 0013):
  - Spec frontmatter, acceptance criteria and open-question checks.
  - Roadmap and plan file models.
  - `forge plan validate <M>`: runs offline and reports every §9.4 error and warning, the waves and the AC coverage matrix. It enforces `min_tier_for.plan` unless `--force` or CI.
  - `forge plan apply <M> [--dry-run]`: idempotent. It creates and updates task issues, and limits started tasks to title, description and done-when, with a warning comment for other changes. It closes removed tasks that were never started, adopts proposals named with `proposal: <n>`, recomputes statuses, and converges when two applies run at once.
- Init, templates, prompts, adapters and setup (Phase 8, ADR 0014):
  - **`forge init`**: scaffolds every project file, including the rules, ten full prompts, three subagent definitions, spec and ADR templates, onboarding, roles and FAQ docs, three GitHub workflows and issue/PR templates. It refuses to overwrite files and merges `.gitignore`/`.gitattributes`.
  - **`forge adapters generate [--check] [--local]`**:
    - Generic: a managed region in `AGENTS.md`.
    - Claude: `.claude/skills/forge-<cmd>/SKILL.md` plus a `CLAUDE.md` region that imports `AGENTS.md`.
    - Codex: `.agents/skills/forge-<cmd>/SKILL.md`.
    - Subagents rendered per machine, with concrete models from the profile: `.claude/agents/forge-*.md` and `.codex/agents/forge-*.toml`, gitignored and also written into task worktrees.
  - **`forge setup [--check] [--yes]`**: machine checks, each with a fix; tool detection; an interactive profile with presets, project recommendations and local Ollama models; constraint warnings; rerere; machine-level adapters; project setup commands.
  - **`forge profile show|set|edit`**.
  - **`forge refresh --radar`**: one comment per in-progress task, for zone drift and stale leases.
- Global options (`--json`, `--agent`, `--repo`, `--verbose`, `--yes`) now work after the subcommand too, as written in the spec.
- Release, docs and polish (Phase 9, ADR 0015):
  - **`forge release <M> --version X.Y.Z [--dry-run]`**: the first run opens a `release/vX.Y.Z` PR with a CHANGELOG section built from the milestone's merged task PRs, grouped by type. After that PR is merged, the second run tags the merge commit, creates the GitHub release and closes the milestone.
  - **`forge docs [--check]`**: a deterministic command reference built from the CLI and the prompt frontmatter. `forge init` writes `docs/forge/commands.md`, and CI checks it.
  - **Docs and installer**: framework docs (`README.md`, `docs/protocol.md`, `docs/architecture.md`, `docs/adapters.md`, `docs/commands.md`); `install.sh` now installs the newest release tag, or `main` when none exists.
  - **Projects v2 mirroring (§15)**: deferred to v1.

### Fixed

Found while dogfooding v0.1.0 on a sandbox GitHub repository (see `docs/dogfooding-v0.1.0.md` and the ADR 0007 amendment):

- **Duplicate task issues**: `forge plan apply` no longer creates them when re-run within seconds. Issue listing now covers GitHub's read-after-write lag.
- **Superseded duplicates**: task issues closed as not planned that duplicate a live issue no longer count as dropped tasks or trigger warnings.
- **Concurrent PR**: `forge ship` re-reads and updates a pull request opened at the same time by another ship (GitHub's 422), instead of failing.
- **Output fixes**:
  - `forge refresh` prints a readable summary instead of a raw dict.
  - `forge setup` no longer repeats warnings it has just fixed.
  - The task context no longer shows the description-region markers.
  - `forge review` prints each acceptance criterion once.
  - Radar comments use correct singular and plural.
- **Project name**: `forge init` takes it from the GitHub repository name when detected.
- **Private Forge repository**: the project workflows can install Forge from it through an optional `FORGE_INSTALL_TOKEN` secret.
