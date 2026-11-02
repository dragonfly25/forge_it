# 0002 — Subagents used to build Forge

- Status: accepted
- Date: 2026-11-02

## Context

Forge is built by an orchestrating Claude Code session that delegates well-scoped work to project subagents. This keeps the main session's context small. The user specified the roster, models and efforts. `CLAUDE.md` requires checking third-party formats against current documentation.

These subagents build Forge itself. They are separate from the subagents Forge ships to its users (spec §12.1, rendered as gitignored `.claude/agents/forge-*.md`). These ones are committed in the framework repository and have no `forge-` prefix.

## Format verified (Claude Code docs, https://code.claude.com/docs/en/sub-agents, 2026-11-02)

- Files live in `.claude/agents/<name>.md`, with YAML frontmatter followed by a system prompt body.
- `name` and `description` are required. `tools`, `model`, `effort` and `disallowedTools` are optional.
- `tools` is a comma-separated string or a YAML list. **Omitting `tools` inherits all tools.**
- `model` is an alias (`haiku | sonnet | opus | fable`), a full model id, or `inherit`.
- `effort` is `low | medium | high | xhigh | max`, and availability depends on the model. A per-invocation `effort` overrides it, and the `CLAUDE_CODE_EFFORT_LEVEL` env var takes precedence.
- Agent files are hot-reloaded, unless the `.claude/agents/` directory did not exist when the session started. In that case a restart is needed.

## Decision

| name | model | effort | tools |
|---|---|---|---|
| explorer | sonnet | low | Read, Grep, Glob |
| test-runner | sonnet | low | Bash, Read, Grep, Glob |
| implementer | opus | medium | (omitted = all) |
| implementer-critical | opus | high | (omitted = all) |
| docs-writer | sonnet | high | Read, Write, Edit, Grep, Glob |
| reviewer | opus | high | Read, Grep, Glob, Bash |

The reviewer must never edit files. A tools list cannot enforce this while Bash is allowed, so its prompt restricts Bash to read-only git and quality-gate commands.

Orchestration rules:
- The orchestrator plans each phase, writes briefs, delegates, verifies and integrates. It writes code itself only for small glue or fixes.
- `implementer` is the default. `implementer-critical` handles claim, lease, sync and plan apply, and any task the implementer has failed twice.
- Parallel implementers own disjoint files.
- `test-runner` runs after every delegation. `reviewer` runs before every phase PR, and all high and medium findings are fixed.
- Subagents return summaries, never file dumps.
- Progress is tracked in `docs/build-plan.md`.

## Consequences

- During Phase 0 the directory was created mid-session, so the agents were not yet registered. Phase 0 delegations ran as `general-purpose` agents with the same instructions and the same model and effort. From Phase 1 onward, the named agents are used directly.
- When Claude Code changes the subagent format, update these files and this ADR.
