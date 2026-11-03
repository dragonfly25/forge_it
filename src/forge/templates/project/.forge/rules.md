# Rules for every agent

These rules apply to every person and every AI agent working on this project, whatever the tool or provider.

## Use the CLI for coordination
- Claims, locks, sync, ship, review, handoff and status are done by the `forge` CLI. Never do them by hand with `git` or `gh`.
- Call `forge ... --json`. Read `ok`, `code`, `data` and `messages` from the envelope.
- Obey the exit codes: `0` ok, `1` error, `2` you must act (conflicts or failing checks), `3` nothing available, `4` escalation required, `5` precondition failed. Do not retry a failing call in a loop. Read `messages`, fix the cause or ask the user.
- When the user asks for a command in words ("take the next task", "sync my branch"), read and follow the matching prompt in `.forge/prompts/`.

## Stay in your zones
- Work only on the task you claimed, in its zones. `forge task show` lists them with their paths.
- Do not edit files outside those zones. If you must, stop and tell the user. Then either `forge propose` the extra work or `forge escalate`.
- Never change a contract zone that the task does not own. Escalate instead.

## Contract first
- Interfaces (API schemas, database schema, shared types) land before the code that uses them.
- A task of `type: contract` changes a contract zone. Other tasks read it.

## Work in small steps
- Make small commits with conventional messages (`feat:`, `fix:`, `docs:`, `test:`, `chore:`). One concern per commit.
- Write or extend tests for every `done_when` item.
- Run the project checks from `.forge/config.yml` before you ship.
- Never push to the default branch. Push only your task branch `task/<n>`. Forge does this for you.
- Never rewrite history that others may use. Rebasing your own task branch is done by `forge sync`.

## Keep in sync
- Run `forge sync` at least once a day, and whenever `forge status` reports drift. `forge ship` syncs first.
- When you resolve a conflict:
  - Both intents must survive. The default-branch side is already reviewed, so adapt your code to it.
  - Read the upstream PR, issue or spec that introduced the change before you resolve. `sync.json` names them.
  - Never resolve by taking one side of a whole file.
  - Never change a contract the task does not own. Escalate instead.
  - Stop and ask the user when the resolution grows beyond the task's zones, or when you are not confident.

## Escalate and propose
- Escalate (`forge escalate --reason ...`) when a conflict or failing check belongs to someone else's work or to a contract you do not own.
- When you discover work the task does not cover, do not do it silently. Run `forge propose` with a clear title and body. The lead decides.

## Long sessions and handoff
- Run `forge heartbeat` during long sessions. A claim with no activity for the lease time becomes stale and can be reclaimed.
- When you run out of tokens or time, or someone else must continue, write a handoff note and run `forge handoff`. Do not leave work unpublished.

## Secrets
- Never write credentials, tokens or keys into files, commits, issues, PR text or handoff notes.
- Secrets live in `.env.local`, which git ignores. `.env.example` holds names only.
- Profiles hold labels only, never credentials.

## Be honest in reports
- Say what you did, what you did not do, and what failed. Do not claim a check passed unless you ran it.
