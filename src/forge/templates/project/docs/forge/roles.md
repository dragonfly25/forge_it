# Roles: lead and dev

`.forge/team.yml` gives each person one or both roles. Every member can do the developer work. The lead has extra duties.

## Dev

A dev takes tasks and delivers them.

- Set up the machine: `/forge-setup`.
- Claim a task: `/forge-next`.
- Implement it inside its zones: `/forge-dev`.
- Keep the branch current: `/forge-sync`.
- Open the pull request: `/forge-ship`.
- Review other people's pull requests: `/forge-review <pr>`.
- Hand a task off when blocked or out of tokens: `/forge-handoff`.
- Propose work the plan missed. The agent files it as a proposal; the dev does not do it unprompted.
- Escalate when a conflict or failing check belongs to someone else.
- Choose how the code is written: `off`, `auto` or `on` (delegation), within what the project allows. See `onboarding.md`.

A dev does not change the plan, the zones or the project config on their own.

## Lead

The lead is the person responsible for one milestone. The role rotates between milestones.

- **Configure**: fill `.forge/config.yml`, `team.yml` and `zones.yml` with `/forge-configure`, then check them with `forge config validate`. Run `/forge-configure --update` when the codebase changes shape. The agent proposes, and writes only after the lead agrees. The changes arrive as a pull request.
- **Plan**: turn specs into a roadmap and a task plan with `/forge-plan`. Plans arrive as pull requests for the team to read.
- **Approve the plan**: merging the plan pull request makes the `forge-plan-apply` workflow create the task issues. The lead can run `forge plan apply <milestone>` by hand if needed.
- **Own the zones**: add or change zones in `.forge/zones.yml` when the codebase grows (`/forge-configure --update` helps). Mark interface areas as contract zones.
- **Triage proposals**: each proposal is an issue labeled `status:proposed`. The lead accepts it by adding a task with `proposal: <issue number>` to the plan, or rejects it by closing the issue with a comment.
- **Decide escalations**: when a task is labeled `blocked:conflict`, the lead helps decide who changes what.
- **Watch the health**: `/forge-status` shows stale claims, proposals waiting, version mismatches and profile problems.
- **Release**: when every task of the milestone is closed, run `/forge-release <milestone>`.
- **Own the project config**: `.forge/config.yml` (checks, resolvers, model constraints, delegation policy) and `.forge/team.yml`.
- **Set the delegation policy**: the `delegation` section of `.forge/config.yml` sets the project default and the modes members may choose (`default: auto`, `allowed: [off, auto, on]`). Devs pick their own mode inside it, in their profile or for one task. The lead can raise the tier of `forge-implementer` with `models.min_tier_for: {implementer: strong}`.

## Who may do what

| Action | Dev | Lead |
|---|---|---|
| Claim, build, sync, ship, review | yes | yes |
| Propose new work | yes | yes |
| Write and apply plans | no | yes |
| Accept proposals | no | yes |
| Edit zones, config and team | no | yes |
| Release a milestone | no | yes |

The table is a convention. Forge does not check the lead role for planning: `forge plan validate` and `forge plan apply` run for anyone with access, and `forge-plan-apply` runs them in CI. Only `forge release` checks the role, and its check is soft: it warns when the person is neither the milestone's `lead` in `plans/roadmap.yml` nor a team member with the role `lead`, and it carries on. Treat the warning as a stop.

`forge release <milestone> --version X.Y.Z` runs in two steps, and the lead runs the same command both times:
1. When every task of the milestone is closed, it opens a release pull request `chore(release): vX.Y.Z` that adds the changelog section. Forge never pushes to the default branch. The team reviews and merges the pull request like any other.
2. After the merge, the same command tags the merged commit `vX.Y.Z`, creates the GitHub release and closes the GitHub milestone.

It stops with exit code 5 when tasks are still open (it lists them), when `--version` is missing (the hint suggests the next version), or when the version was already released for another milestone.
