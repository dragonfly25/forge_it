# Guide for the developer

This guide covers the daily loop: take a task, build it, keep it current, ship it and review other people's work. For the rules behind it, see the [protocol](../protocol.md). For the lead's side, see the [lead guide](lead.md). In a project that uses Forge, `docs/forge/onboarding.md` has the same flow for your team.

Every command runs in your agent as `/forge-<name>` in Claude Code or `$forge-<name>` in Codex. You can also say it in plain words ("take the next task"). The agent calls the `forge` CLI. You can run it yourself too. Add `--json` to get the machine-readable output.

## One-time setup

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
forge setup
```

`forge setup` checks your tools, creates your profile and writes the subagent files. The profile maps the tiers (`strong`, `standard`, `light`) to concrete models for each agent you use. It holds labels only, never credentials. Run `forge setup --check` any time to see what is wrong. Change a model with `forge profile set <agent>.<tier>=<model>`.

## The loop

| Step | Command | What happens |
|---|---|---|
| Take a task | `/forge-next` | Claims the best task for you and opens its worktree. |
| Build | `/forge-dev` | Implements the task inside its zones. |
| Stay current | `/forge-sync` | Rebases on the default branch. |
| Publish | `/forge-ship` | Syncs, runs checks, opens or updates the PR. |
| Review | `/forge-review <pr>` | Reviews a teammate's PR. |
| Pass it on | `/forge-handoff` | Writes a note and gives the task back. |
| Look around | `/forge-status` | Your task, the team, the queue. |

## 1. Take a task

Run `/forge-next`. Forge picks the best available task, using the order: handoff first, then milestone, wave, size, issue number. It skips tasks that need a stronger tier than your agent has, and tasks whose zones are held by someone else. The claim is exclusive for you: nobody else can take the task, or work in its zones, while you hold it.

The agent shows the task context and an implementation outline, then waits. Read it. Reply "go", or tell it what to change. Preview without claiming with `forge next --dry-run`.

The task lives in its own worktree, `<repo>-worktrees/task-<n>`. Work there.

## 2. Read the task context

`forge task show` prints it again at any time. It has:

- the id, title, spec path and the acceptance criteria (`covers`) it must meet;
- `done_when`: the checklist that proves the task is finished;
- `zones`: the paths you may change;
- `depends_on` and the notes of any earlier handoff;
- a `delegation` block, described below;
- an `architecture` block.

The `architecture` block looks like this:

```json
"architecture": {"path": "docs/architecture/M1.md", "component": "auth-service", "implements": [], "uses": ["IF-auth-api"]}
```

| Field | Meaning |
|---|---|
| `path` | The milestone's architecture file, or `null` if the project has none. |
| `component` | The component you are building. Its zones are where you work. |
| `implements` | Interfaces this task must deliver. Only contract tasks have them. |
| `uses` | Interfaces you may rely on. They are already agreed. |

Read `docs/architecture/<M>.md` before you start. It explains the component, the interfaces and the decisions behind them. `docs/architecture/overview.md` shows the whole system. If `component` is `null`, the project has no architecture for this milestone. Follow the zones.

## 3. Build it

Run `/forge-dev`. The agent implements the task, writes tests for each `done_when` item, makes small commits and runs the project checks.

### Stay inside your component and contracts

- **Zones.** Change only files in the task's zones. Files in other zones, or in no zone, are listed in the PR as out-of-zone changes. With `unzoned: error` in the project, files in no zone stop `forge ship`.
- **Component.** The work belongs to the task's `component`. Do not reshape another component.
- **Interfaces you implement.** A contract task delivers exactly the interfaces in `implements`, in their contract zone. Match the summary in `<M>.md`.
- **Interfaces you use.** Treat them as fixed. Do not change a contract you do not own. If you need a change, stop and ask the lead.
- **Extra work.** If you find work the task does not cover, the agent files a proposal with `forge propose` and carries on. The lead decides. Do not do it unprompted.
- **A design gap.** If the architecture is wrong or missing something, do not fix it in the task. Tell the lead. They update it with `/forge-architect <M> --update`.

On a long session the agent sends heartbeats so your claim does not look stale. A claim is stale after `lease_hours` (24 by default) of no activity, and then anyone can reclaim it.

### Who writes the code: delegation

The `delegation` block in the task context says whether your main agent writes the code or acts as lead and hands it to the `forge-implementer` subagent. See [ADR 0017](../decisions/0017-implementer-subagent-and-delegation.md).

| Mode | Meaning |
|---|---|
| `off` | Your main agent writes the code. Fewest tokens. |
| `auto` | Delegate when the task is size `M` or has two or more zones. Default. |
| `on` | Always delegate. |

Change it for one task with `/forge-dev --solo` (`off`) or `/forge-dev --delegate` (`on`). Change it for yourself with `forge profile set delegation=<mode>`. The project sets the default and the allowed modes. It needs Claude Code or Codex, and the subagent files from `forge setup`. Other agents work as `off`.

## 4. Stay in sync

Run `/forge-sync` at least once a day, and whenever `forge status` reports drift. It rebases your branch on the default branch. When files conflict, the agent resolves them so both changes survive. It escalates (exit 4) when the conflict is outside your zones or in a contract you do not own. Then stop and wait for the lead. `forge sync --abort` restores the state before the sync.

## 5. Ship

Run `/forge-ship`. Forge syncs, runs the checks, opens or updates the PR from `task/<n>` and requests a reviewer. The PR lists acceptance criteria, `done_when`, out-of-zone changes, conflict resolutions and the agent you used. It never merges.

Check the out-of-zone list before you ask for review. If it is not empty and not expected, move the change or ask the lead. After approval and green CI, merge the PR. The merge closes the issue and unblocks dependent tasks.

## 6. Review a teammate

Run `/forge-review <pr>`. The agent reads the PR, the task, the diff, the zone check and CI. It checks the acceptance criteria, `done_when`, zone discipline, tests and contracts. It also checks that the change stays inside the task's component and respects the interfaces the task implements and uses. Then it submits an approval, a request for changes or a comment. You cannot approve your own PR.

## 7. Hand off

If you run out of time or tokens, run `/forge-handoff`. The agent writes a note (done, remaining, decisions, gotchas, next step, how to verify), pushes your work and gives the task back. The next `/forge-next` of someone else prefers it and shows your note. Use `forge release-claim` to drop a task without a note.

## Exit codes you will meet

| Code | Meaning | What to do |
|---|---|---|
| 0 | Done | Continue. |
| 2 | Conflicts, failing checks or a hook refused | Fix them, then `forge sync --continue` or re-run. |
| 3 | No task available | Check `/forge-status`, or wait for the next wave. |
| 4 | Escalation | Stop. The lead decides. |
| 5 | A precondition failed (version pin, login, not on a task branch) | Read the hint. It names the fix. |

## Related

- [Protocol](../protocol.md): claims, leases, zones, sync, ship and review in detail.
- [ADR 0017](../decisions/0017-implementer-subagent-and-delegation.md): delegation.
- [ADR 0019](../decisions/0019-milestone-architecture.md): the milestone architecture.
