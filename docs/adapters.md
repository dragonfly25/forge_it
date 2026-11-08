# Adapters

Forge writes its prompts and subagent definitions once, in a provider-neutral form. Adapters turn them into the native files of each AI tool. This document explains what each adapter writes, which tool formats it targets, and how to add one. The decisions are in [ADR 0014](decisions/0014-init-adapters-setup.md).

## Sources and outputs

Two kinds of source files live in the project and are committed:

| Source | Content |
|---|---|
| `.forge/prompts/<name>.md` | One command prompt. Frontmatter: `name`, `description`, `args`, `roles`. Body: Goal, Steps, Exit codes, Guardrails, Subagents, Final report. |
| `.forge/agents/<name>.md` | One subagent. Frontmatter: `name`, `description`, `tier` (`strong`, `standard`, `light`, `task`), `effort` (`low`, `medium`, `high`), `tools` (`read`, `edit`, `shell`). Body: the instructions. |
| `.forge/rules.md` | Rules every agent must follow. |

Rendering has two levels.

| Level | Command | Written | Committed | Depends on |
|---|---|---|---|---|
| Project | `forge adapters generate` | `AGENTS.md`, `CLAUDE.md`, `.claude/skills/forge-*/SKILL.md`, `.agents/skills/forge-*/SKILL.md` | yes | the prompts, the rules and `config.yml` |
| Machine | `forge adapters generate --local` | `.claude/agents/forge-*.md`, `.codex/agents/forge-*.toml` | no (gitignored) | the agent definitions and your profile |

Machine-level files hold concrete models, so they differ per person. Forge never commits them.

## What each adapter writes

The adapters enabled in `.forge/config.yml` (`adapters:`) are rendered. `generic` is always on. The defaults are `generic`, `claude` and `codex`.

### generic

Writes the managed region of `AGENTS.md`:

- a project summary (name, default branch, merge method), and how to read the exit codes;
- the instruction: when the user invokes `/<cmd>`, `/forge-<cmd>` or asks for it in words, read and follow `.forge/prompts/<cmd>.md`;
- the command table (name, prompt path, description, arguments, roles);
- the subagent list;
- the rules from `.forge/rules.md`, inlined.

Prompt bodies are referenced, not inlined, so the region stays well under Codex's 32 KiB limit for `AGENTS.md`. Any agent that reads `AGENTS.md` can use Forge this way, including local-model agents.

### claude

- **Skills.** One file per prompt: `.claude/skills/forge-<cmd>/SKILL.md`. Frontmatter: `name: forge-<cmd>`, `description`, and `argument-hint` from `args`. The body is the prompt, followed by a line `Arguments: $ARGUMENTS`. The user types `/forge-next`.
- **`CLAUDE.md`.** The managed region holds `@AGENTS.md` (an import) and a short note that commands are `/forge-<cmd>` and subagents are `forge-*`.
- **Subagents** (machine level). `.claude/agents/forge-<name>.md` with `name`, `description`, `tools`, `model` and `effort`.

Why skills and the `forge-` prefix: the Claude Code docs prefer skills to `.claude/commands/`, and Claude Code has built-in `/status` and `/review`. The prefix avoids clashes and leaves the plain names free for the team's own commands. This amends spec §13.

### codex

- **Skills.** One file per prompt: `.agents/skills/forge-<cmd>/SKILL.md` with `name` and `description`. Codex skills have no argument placeholder, so the body ends with a line telling the agent to take the arguments from the user's message. The user types `$forge-next`.
- **No custom prompts.** Codex custom prompts are deprecated, so none are generated. Codex reads `AGENTS.md` natively.
- **Subagents** (machine level). `.codex/agents/forge-<name>.toml` with `name`, `description`, `developer_instructions`, and when known `model` and `model_reasoning_effort`.
- `.codex/config.toml` is not generated. Model choice stays in your profile and the rendered subagents.

### Subagent rendering

The template set has four subagents: `explorer`, `test-writer`, `implementer` and `reviewer`. `implementer` (tier `standard`, effort `medium`, tools `read`, `edit`, `shell`) is rendered like the others, as `forge-implementer`: `.claude/agents/forge-implementer.md` for Claude and `.codex/agents/forge-implementer.toml` for Codex. Its model comes from the same resolution, so `config.models.min_tier_for: {implementer: strong}` raises it. The `/forge-dev` prompt uses it only when the delegation decision says so ([ADR 0017](decisions/0017-implementer-subagent-and-delegation.md)).

For each tool present in your profile, Forge renders with one agent of that tool: the `default_agent` if it uses the tool, else the first one. The model comes from model resolution (ADR 0005): the declared tier, raised by `config.models.min_tier_for`, then mapped through the agent's `tiers`.

- `tier: task` renders as `model: inherit` for Claude and leaves `model` out for Codex, because the main session already runs the task's tier.
- An agent that cannot serve the tier is rendered without a model, with a warning.
- A model that is not a Claude alias or a `claude-...` id (a local model, say) renders as `model: inherit` for Claude, with a warning.
- The effort is the profile's mapping of the abstract level. Values the tool does not accept are dropped, with a warning.
- Tool mapping for Claude: `read` becomes `Read, Grep, Glob`. `edit` becomes `Edit, Write`. `shell` becomes `Bash`. Codex has no per-agent tool list, so `tools` is not rendered.

Subagents are rendered by `forge setup`, `forge profile set`, `forge profile edit` and `forge adapters generate --local`. They are also written into every task worktree: `forge next` renders them when it creates or reuses a worktree, and the profile commands re-render into all worktrees. `forge adapters generate --local` renders only the current checkout. Rendering refuses with exit 5 to write a file that git would not ignore. Add the pattern to `.gitignore` (the template already has it).

## Managed regions and generated headers

- **Header.** Every generated file says it was generated by `forge adapters generate`, from which source, and not to edit it. It is a Markdown comment, or a `#` comment in TOML. In files that need frontmatter on line 1 (`SKILL.md`, Claude subagents), the header comes right after the frontmatter.
- **Managed region.** `AGENTS.md` and `CLAUDE.md` may hold human text. Forge owns only the text between `<!-- forge:adapter-begin -->` and `<!-- forge:adapter-end -->`. Text outside is kept byte for byte, CRLF included. A file without the region gets it appended. Unbalanced or repeated markers are a configuration error that names the file.
- **Stale files.** `forge adapters generate` deletes generated `forge-*` skill directories whose prompt no longer exists. It only deletes directories that carry the generated header, so hand-written skills are safe. When an adapter is disabled, it removes that adapter's region from `CLAUDE.md`, and deletes the file only when nothing else is left.

## `--check` and `--local`

| Command | Effect |
|---|---|
| `forge adapters generate` | Rewrites the project-level files. |
| `forge adapters generate --check` | Writes nothing. Exits 1 and lists every file that is missing, different or extra. The project's `forge-ci.yml` runs it on every PR. |
| `forge adapters generate --local` | Renders the machine-level subagents from your profile. |
| `forge adapters generate --check --local` | Reports machine-level drift and exits 1. |

`forge setup --check` also reports machine-level files that are missing, outdated, not ignored by git or stale.

Typical flow after editing a prompt: run `forge adapters generate` for the adapter files and `forge docs` for the prompt table in `docs/forge/commands.md`, then commit both. CI checks each with `--check`. Run `forge docs` with the Forge version pinned in `.github/workflows/forge-ci.yml`, because CI runs its `--check` with that version and a file generated by another version can differ. After changing your models: run `forge profile set ...` and the subagents update by themselves.

## Tool formats (verified 2026-11-03)

These are the formats the adapters target, as recorded in ADR 0014. Check the pages again before changing an adapter, because the tools change.

**Claude Code**
- Skills live in `.claude/skills/<name>/SKILL.md`. Frontmatter has `name`, `description`, `argument-hint` and more. Unknown fields are ignored. `$ARGUMENTS` holds the argument string. A skill wins over a command with the same name. `.claude/commands/` still works, but the docs say to prefer skills. ([skills](https://code.claude.com/docs/en/skills.md))
- `CLAUDE.md` imports a file with `@path` on its own line, relative to the importing file, up to four hops. Claude Code reads `AGENTS.md` natively only when no `CLAUDE.md` exists, so Forge uses the `@AGENTS.md` import. ([memory](https://code.claude.com/docs/en/memory.md))
- Subagents live in `.claude/agents/<name>.md` with `name`, `description`, `tools`, `model` (`haiku`, `sonnet`, `opus`, `fable`, `inherit` or a full id) and `effort` (`low`, `medium`, `high`, `xhigh`, `max`, depending on the model). Project files win over user files. ([sub-agents](https://code.claude.com/docs/en/sub-agents.md))

**Codex** (documentation at [learn.chatgpt.com](https://learn.chatgpt.com/docs/), the new home of developers.openai.com/codex)
- `AGENTS.md` is read natively, concatenated from the repository root down to the working directory, with a default limit of 32 KiB in total.
- Custom prompts in `~/.codex/prompts` are deprecated in favor of skills, and are user-level only.
- Skills live in `.agents/skills/<name>/SKILL.md` (project, and parent directories up to the repository root) or `~/.agents/skills` (user). `name` and `description` are required. A skill is invoked with `$name` or `/skills`, or implicitly from its description.
- Subagents are one TOML file per agent in `.codex/agents/` (project) or `~/.codex/agents/`. `name`, `description` and `developer_instructions` are required. `model` and `model_reasoning_effort` are optional. `low`, `medium`, `high` and `xhigh` are safe effort values. The page also lists `max` and `ultra`, depending on the model.

## How to add an adapter

An adapter is one module in `src/forge/core/adapters/` that renders files from a `Project`. Use `generic.py`, `claude.py` and `codex.py` as models.

1. **Verify the tool's current format** in its official documentation. Record what you found, with links and the date, in a new ADR.
2. **Write the module** `src/forge/core/adapters/<name>.py`:
   - `render(project: Project) -> list[GeneratedFile]`: the project-level files. `Project` (from `_common.py`) carries the name, default branch, merge method, enabled adapters, rules, `prompts` (each with `name`, `description`, `args`, `roles`, `body`, `source`) and `agents`.
   - A `GeneratedFile(path, content, region=False)` per file. `path` is relative to the repository root. Set `region=True` when the file is human-owned and `content` is only the inner text of the managed region.
   - Start each file with a header from `md_header` or `hash_header`, so Forge can recognise it as generated. Use `frontmatter` and `normalize_body` from `_common.py`.
   - Keep the output deterministic. `--check` compares it byte for byte.
3. **Add machine-level rendering** when the tool has subagents. Define `TOOL` (the profile `tool` value), `GITIGNORE_PATTERN`, and `render_local(definitions, agent, config, presets) -> tuple[list[GeneratedFile], list[str]]`. Call `resolve_subagent` from `_common.py` to get the model and effort for each definition. The second return value is a list of warnings.
4. **Register it:**
   - add the name to `ADAPTERS` in `core/config.py`;
   - add `render` to `RENDERERS` in `core/adapters/__init__.py`;
   - add the skills directory to `SKILL_DIRS`, and a region file to `REGION_FILES` if you write one;
   - add the module to `LOCAL_ADAPTERS` and its pattern to `LOCAL_PATTERNS` if it renders subagents;
   - add the pattern to the template `.gitignore` in `src/forge/templates/project/`.
5. **Test it** in `tests/core/test_adapters.py`: the rendered files, `--check` drift (missing, different, extra), a re-run that changes nothing, and the managed-region rules for human text.
6. **Document it** here: what it writes, the verified format, and the links. Update the `forge init` snapshot if the template tree changes.

Prompts stay provider-neutral. When a tool changes its format, only the adapter module and its ADR change.
