# 0005 — Model resolution, tier serving and profile constraints

- Status: accepted
- Date: 2026-11-02

## Context

Spec §6.5 describes model resolution, but several details are left open:
- which tiers an agent can serve (§8.1 "skip tasks whose tier the active agent cannot serve");
- where the manual `--tier`/`--effort` overrides live (§16 does not list them);
- what "strong mapped to a small local model" means in a deterministic CLI;
- what happens when a tool does not support a parameter.

## Decision

**Resolution**, `resolve(...)` in `core/models.py`. All steps are pure and deterministic.
1. Base tier: if the declared tier is `task`, use the task's tier (`standard` when no task is given). Otherwise use the declared tier.
2. Every role or command name passed in (`roles`, e.g. `["reviewer"]` or `["plan"]`) is looked up in `config.models.min_tier_for`. The highest matching minimum raises the tier if it is higher, and the note names the key that raised it.
3. A manual override (`--tier`, `--effort`) is applied only if it is higher than the current value. A lower override is ignored, with a warning note.
4. Serving: the agent serves tier T with the model mapped to the **lowest tier ≥ T present in its profile** (e.g. an agent with only `strong` serves `standard` tasks with its strong model). If no mapped tier is ≥ T, the agent **cannot serve** T. Resolution returns `served=False`, and `forge next` skips the task unless `--any-tier`, in which case the highest mapped tier is used with a warning.
5. Effort: the abstract effort (`low|medium|high`, from the declaration, raised by any override) is mapped through the agent's `effort` table. If the tool's preset declares `supports_effort: false`, or the agent has no mapping for that level, the effort value is `None` and a note says it was ignored.
6. The result records `tier`, `effective_tier` (the tier whose model is used), `model`, `effort`, `effort_value`, `served` and `notes`. The PR body and claim traceability use it (§6.5.7).

`--tier`/`--effort` will be global-ish options on `next`, `task show`, `review` and `ship` (added with those commands). They only affect the resolved map that is printed and recorded.

**Presets**: `src/forge/presets/<tool>.yml`, loaded with `importlib.resources`. Fields: `tool`, `provider`, `updated` (date), `supports_effort` (bool), `tiers` (tier → model), `effort` (abstract → tool value), `known_models` (models the preset author considers valid for each tier). Shipped: `claude-code`, `codex`, `ollama`. The `ollama` preset has no tiers: models come from `ollama list` during setup.

**Constraint checks**, `check_profile(profile, constraints, presets) -> list[Violation]`. Each violation has a level, an agent id, a message and a fix. Violations are warnings and never change anything.
- A provider of an agent is not in `allowed_providers` (when that list is set).
- A role in `min_tier_for` that the agent cannot serve (rule 4).
- **Unverified strong model**: the agent's `strong` model does not appear in the `known_models.strong` list of any preset for a non-`local` provider. This is the deterministic proxy for "strong mapped to a small local model".
- Info only: a tier's model differs from `config.models.recommended[tier]` when the agent's tool is the recommendation's tool. Recommendations are compared by alias.
- **Preset drift**, `preset_drift(profile, presets)`: lists the tiers whose model differs from the current preset for that tool. Phase 4 Health and setup use it to propose updates.

## Consequences

- Hybrid agents (Claude orchestrator plus local executor) get a warning only if they map `strong` to a local model.
- Preset data goes stale as models change. `updated` makes this visible, and Forge releases refresh the presets.

## Implementation notes (Phase 1)

- **Alias comparison** (`same_alias`): two model names are the same when they are equal, or when one is a hyphen-separated token of the other (`opus` ~ `claude-opus-5-5`). A recommendation applies to an agent's tool when that tool's preset lists the recommended model in `known_models`.
- **`any_tier`**: `served` stays `False`, but `model` and `effective_tier` are filled with the highest mapped tier. Callers test `served or any_tier`.
- **`presets=None`** in `resolve` means "no preset information": effort support is assumed, and only the agent's own mapping is used.
- **`preset_drift`** compares only the tiers the agent maps. A tier left unmapped on purpose is not drift.
- **Codex preset**: checked on 2026-11-02 against https://learn.chatgpt.com/docs/models. Mapping: strong `gpt-6-astra`, standard `gpt-6.1-sol`, light `gpt-6-luna`. Effort maps to `model_reasoning_effort`. The spec's §6.4 example (`standard: gpt-5`) therefore shows up as preset drift, which is the intended signal.
