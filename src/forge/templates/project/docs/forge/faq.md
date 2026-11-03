# FAQ

## Why `/forge-next` and not `/next`?

Claude Code has built-in commands such as `/status` and `/review`. The `forge-` prefix keeps Forge's commands from clashing with them, and keeps your own command names free. In Codex the same commands are skills: `$forge-next`.

Every agent can also work from plain words. `AGENTS.md` maps phrases like "take the next task" or "sync my branch" to the same instructions in `.forge/prompts/`. If you type `/next` as plain text in an agent without that command, the agent still finds the right prompt.

## My task is blocked by a conflict. What now?

Forge labels a task `blocked:conflict` when a sync finds a conflict it cannot safely resolve: a file outside your zones, or a contract you do not own. Forge aborts the rebase, so your branch is as it was. It comments on your issue and on the upstream issues, and mentions the people involved.

What to do:
1. Read the comment on your issue. It lists the files and the upstream pull requests.
2. Talk to the author of the upstream change, or the lead.
3. When the way forward is agreed, run `/forge-sync` again. A sync that pushes clears the label.

You can also escalate by hand: `forge escalate --reason "<what and why>"`.

## I ran out of tokens in the middle of a task.

Run `/forge-handoff` before the session ends, if you can. The agent writes a note (done, remaining, decisions, gotchas, next step, how to verify). Forge commits your work in progress, pushes it, posts the note on the issue and frees the task.

Anyone can continue with `/forge-next`. Tasks in handoff come first, and the note is printed with the task context. It can be another account, another provider or another person.

If you cannot run the command at all, the lease runs out after 24 hours (the default `lease_hours`). The task then shows as stale, and anyone can take it over (see the next question). Work you committed but never pushed stays on your machine.

## A task looks stale. How do I take it over?

A claim is stale when it has had no activity for `lease_hours` (24 by default). `/forge-status` and the radar workflow flag stale tasks.

Take it over with `forge reclaim <issue-number>`, or `forge next --reclaim`. A fresh claim of someone else cannot be taken: the command says when it becomes stale. Taking over posts a comment that names the previous owner, and you continue from the branch they left.

To avoid going stale yourself, let your agent run `forge heartbeat` on long sessions, or run `/forge-sync` regularly.

## Forge says my version does not match. What do I do?

The project pins the CLI version in `.forge/config.yml` (`forge_version`). If yours is outside the range, `forge next`, `forge ship` and similar commands stop with exit code 5. `forge status` and `forge setup --check` only warn, so you can still diagnose.

Install the version the project uses:

```bash
uv tool install --force "git+https://github.com/dragonfly25/forge_it@{{forge_install_ref}}"
```

If the lead bumped `forge_version` in a pull request, the new range is in that file. Install a version in it. If the range is too old for your needs, ask the lead to update it.

## How do I add a zone?

Zones are owned by the lead. Open a pull request that edits `.forge/zones.yml`:

1. Add the zone under `zones:` with its `paths` (globs relative to the repository root). Names use lowercase letters, digits, `-` and `_`, with `/` between parts.
2. Mark it `contract: true` if it holds interfaces others depend on (API schemas, database schema, shared types).
3. If two zones must never run in parallel although their paths differ, list them under `conflicts:`.
4. Merge it. New tasks can use the zone from then on.

`/forge-ship` warns about files that match no zone. `unzoned: error` in the same file turns that warning into a failure.

## Where is my profile, and does it hold secrets?

Your profile is in `~/.config/forge/profile.yml` (or `$XDG_CONFIG_HOME/forge/profile.yml`). It maps tiers to models for each of your agents. It holds labels only. Forge never stores credentials. Your tools keep their own logins.

Show it with `forge profile show`. Change a value with `forge profile set <agent>.<tier>=<model>`.

## A review asks me to change something after I shipped.

Make the changes in your worktree, then run `/forge-ship` again. It updates the same pull request, keeps the boxes reviewers ticked and does not add reviewers beyond what is needed.
