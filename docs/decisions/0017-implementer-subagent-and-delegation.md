# 0017 — `implementer` subagent and the delegation switch

- Status: accepted (approved 2026-11-08)
- Date: 2026-11-08

## Context

Today the main agent session writes all task code itself. The three subagents only help: `explorer` reads, `test-writer` writes tests, `reviewer` checks (§12.1, ADR 0014).

Delegating the code to a dedicated development subagent changes the trade-offs:
- **Pros**:
  - The main session acts as lead: it plans, briefs, verifies and keeps a small context on long tasks.
  - Coding can run on a cheaper tier than the lead.
  - Parallel implementers can work on disjoint files.
- **Cons**: more total tokens (briefs, re-reading files, summaries), some detail lost in the handoff, and more latency.

The right choice depends on the task and on each person's plan and budget, so it must be a switch.

The CLI never calls a model and cannot count tokens. It can, however, decide the mode deterministically, tell the agent, and record the decision.

## Decision

### The `implementer` subagent
- **Template**: a fourth template, `.forge/agents/implementer.md`:
  - `tier: standard`, `effort: medium`, `tools: [read, edit, shell]`;
  - rendered as `forge-implementer` by the existing adapters (Claude: `.claude/agents/forge-implementer.md`; Codex: `.codex/agents/forge-implementer.toml`).
- **Tier**: `standard` keeps the lead (usually on `strong`) more expensive per token than the bulk of the code. A project can raise it with `models.min_tier_for: {implementer: strong}` (ADR 0005), as for any subagent.
- **Its instructions**:
  - implement exactly the brief;
  - touch only the files and zones it lists;
  - write or extend the tests the brief asks for and run them;
  - never claim, sync, ship, push or comment;
  - return a summary of at most 15 lines (files changed, tests run, open questions), never file dumps.

### Modes
- `off`: the main session writes the code itself, as today. Fewest tokens.
- `auto` (default): delegate only when the task is big enough: `size: M`, **or** the task has 2 or more zones. Otherwise behave like `off`.
- `on`: always delegate the implementation.

### Where the mode is decided (most specific wins)
1. **Per invocation**: `--delegation off|auto|on` on `forge task show` and `forge next`. The prompt maps `/forge-dev --solo` to `off` and `/forge-dev --delegate` to `on`.
2. **Per person**: a new top-level profile field, `delegation: off|auto|on`, optional. It is set in `forge setup`, where the wizard asks with the project default preselected and `--yes` keeps the default, or with `forge profile set delegation=<mode>`.
3. **Per project**: a new `delegation:` section in `.forge/config.yml`:
   ```yaml
   delegation:
     default: auto              # used when the profile and the invocation say nothing
     allowed: [off, auto, on]   # modes members may choose
   ```
   Both keys are optional; the defaults are shown. Errors name the file, the key and the fix.

A requested mode that the project does not allow falls back to the project default, with a warning that names the setting to change.

### Capability
Delegation needs native subagents. The effective mode is forced to `off`, with an info note, when:
- the active agent's tool is not `claude-code` or `codex` (generic or local-only agents);
- there is no profile;
- the machine-level files are missing. `forge-implementer` is not rendered; the note says to run `forge adapters generate --local`.

### What the CLI outputs
- **Task context**: `forge task show` and `forge next` gain a `delegation` object, with a matching human block, `## Delegation`. Its fields:

  | Field | Meaning |
  |---|---|
  | `mode` | the effective mode |
  | `requested` | the mode that was asked for |
  | `source` | invocation, profile or project |
  | `delegate` | true or false, the decision for this task |
  | `reason` | e.g. "size M", "1 zone, size S", "tool has no subagents" |
  | `subagent` | `forge-implementer` |
  | `model` | resolved for the active agent, as the adapters render it |
- **The `dev` prompt** follows `delegation.delegate`:
  - **When true, the main session acts as lead**:
    1. explore (via `forge-explorer`);
    2. split the work into one or more briefs (goal, files, done_when items, constraints, tests to run), with disjoint files when running in parallel;
    3. delegate each to `forge-implementer`;
    4. verify by reading the diff and running the checks;
    5. iterate on findings, at most two rounds per brief before taking it over itself.
  - **When false**: as today, "implementation stays with you".
  - Both paths keep `forge-test-writer` and `forge-reviewer` as they are.
- **PR body**: `forge ship` adds one line to the Agent section, e.g. `Delegation: auto → delegated (size M) to forge-implementer (sonnet)`. This is the **decision**; Forge cannot verify what the agent actually did. Over time the team can compare modes on real PRs.
- **`forge status`**: the Me section shows the effective mode for the current task.

### Not in scope
- Token counting or budgets. Forge never calls a model.
- The orchestrator that runs agents without a person (spec §19, v2).
- Per-agent (instead of per-profile) delegation settings. They can be added later without breaking the format.

## Implementation notes (Phase 11)

- **Commits**: the lead commits the implementer's work. The implementer commits only when its brief says so.
- **Where the decision is computed**: in the task's worktree. Rendered files and `.forge/agents/implementer.md` are looked up there, and `forge next` renders the subagents into new worktrees (ADR 0014).
- **Which model**: the implementer model reported is the one the adapters render: the tool's default agent, or none when the subagent inherits the session model. It is filled only when the task is delegated.
- **Capability checks**: they run only for `auto` and `on`. A task that is `off` by choice keeps its "requested by …" reason.
- **Missing template**: a missing or invalid `implementer.md` forces `off`, with a note. It never fails the command.
- **YAML booleans**: unquoted `off` and `on` read as booleans in YAML, so both config and profile accept them as `off`/`on`. The profile writes the value quoted.
- **Setup wizard**:
  - It writes nothing when the answer equals the project default and the profile had no value.
  - It does not ask when only one mode is allowed, or when no agent uses claude-code or codex.
- **Status**: `forge status` uses the active agent for the Me section.
- **Ship**: it passes the profile to the PR body so the line reflects the person's setting.
- **Override lasts for the task**: `forge next --delegation X` and `forge task show --delegation X` save the override in the task's local `current.json`. `forge ship` and `forge status` read it, so they report the decision the agent actually followed (`source: invocation`).
- **`task show` never fails because of the profile**: an unknown agent falls back to the default agent with a warning. An invalid profile is treated as no profile, with a note inside `delegation`. With delegation off, the output and exit codes match v0.1.0 apart from the new field.

## Consequences

- **No change for those who opt out**: `off` behaves exactly as v0.1.0. Teams pay for delegation only where they enable it.
- **Template tree**: `forge init` writes one more file (`.forge/agents/implementer.md`). Existing projects get it by copying the template, since `forge upgrade` is v1.
- **New defaults**: the new config section and profile field are optional, so existing files stay valid.
- **Downgrade**: v0.1.0 rejects the new `delegation_override` key in `current.json`. After a downgrade, a task claimed with a newer Forge reports a corrupt local state file, and the error names the fix.

## Plan (Phase 11)

Work is split so parallel agents own disjoint files.

1. **Core and CLI** (implementer): `core/delegation.py` (pure decision function: inputs are the task metadata, config policy, profile, invocation override and active agent; output is mode, decision and reason), the config `delegation` section, the profile field with `profile set`, `--delegation` on `task show`/`next`, the context `delegation` object and render, the PR-body line, the status line, and the setup wizard question. Tests: a decision matrix covering every level, precedence, not-allowed fallback, the capability rules and the `auto` thresholds, plus JSON and human output.
2. **Templates and prompts** (docs-writer): `.forge/agents/implementer.md`; the `dev` prompt's two paths and the `--solo`/`--delegate` mapping; mentions in `next`/`ship` where relevant; the onboarding, FAQ ("how do I spend fewer tokens?") and roles docs; `docs/adapters.md` and `docs/protocol.md`.
3. **Tests that pin the subagent set** (implementer, after 1 and 2): `test_prompts.py`, `test_init.py` (snapshot + new file), `test_adapters.py` (render `forge-implementer` for Claude and Codex, `min_tier_for.implementer`), the prompt-call parser test for the new flags, and `forge docs` regeneration.
4. **Verification and review**: test-runner, reviewer (two passes if needed), then the PR.

**Done when**:
- the decision matrix tests pass;
- `forge init` writes `implementer.md` and `adapters generate --local` renders `forge-implementer` with the profile's model for Claude and Codex;
- `forge task show --json` reports the decision, and `--delegation` overrides it within the project policy;
- the PR body records the decision;
- the `dev` prompt has both paths and passes the prompt checks;
- with `delegation: off` everywhere, the output equals v0.1.0's apart from the new `delegation` field.
