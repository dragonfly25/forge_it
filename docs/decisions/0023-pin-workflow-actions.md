# 0023 — Pin the project workflows' actions to refs that exist

- Status: accepted
- Date: 2026-11-13

## Context

ADR 0014 pinned the actions in the project workflow templates (`forge-ci.yml`, `forge-radar.yml`, `forge-plan-apply.yml`) to major tags: `actions/checkout@v7` and `astral-sh/setup-uv@v10`. `actions/checkout` publishes a `v7` tag. `astral-sh/setup-uv` publishes only full versions (`v10.0.0` … `v10.3.3`) and has no `v10` tag.

GitHub resolves actions before the first step, so every workflow that `forge init` wrote failed with "Unable to resolve action `astral-sh/setup-uv@v10`". A real project created with Forge saw every `forge-ci` and `forge-radar` run fail this way from its first push. Forge's own CI did not catch it: it already uses `astral-sh/setup-uv@v10.3.3`, and no test looks at the refs in the templates.

## Decision

- The templates pin `astral-sh/setup-uv@v10.3.3`. `actions/checkout@v7` stays.
- Every action used in a template must be pinned to the same ref as in Forge's own CI (`.github/workflows/ci.yml`). That CI runs on every pull request, so it proves the ref resolves. `tests/test_workflow_templates.py` checks this.
- To move an action to a newer version, change Forge's CI and the templates together.

## Consequences

- New projects get workflows that start.
- Existing projects must edit the pin in their three workflows by hand. `forge init` never overwrites a file without `--force`. The CHANGELOG for 0.2.0 says so.
- A full version does not pick up fixes on its own. Updates are deliberate, and the test keeps the templates and Forge's CI in step.
