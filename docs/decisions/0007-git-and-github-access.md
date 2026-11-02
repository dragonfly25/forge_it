# 0007 — Git wrapper and GitHub client

- Status: accepted
- Date: 2026-11-02

## Context

All git access goes through `core/git.py`, and all GitHub access goes through the `GitHubClient` protocol (`CLAUDE.md`). Later phases need claim CAS pushes, ref listing, rebase control, worktrees, issues, labels, milestones, comments and PRs. Git's output is localized on many machines; for example, the maintainer's git prints Italian messages.

## Decision

**Git** (`core/git.py`):
- A `Git(cwd)` class built on `subprocess.run`. Every call runs with `LC_ALL=C`, `LANG=C`, `GIT_TERMINAL_PROMPT=0` and `GIT_OPTIONAL_LOCKS=0`, so output can be parsed. A non-zero exit raises `GitError(args, returncode, stderr)`, except for operations whose failure is an expected outcome: pushes rejected by a lease, rebases with conflicts, and `rev_parse` of a missing ref. Those return result objects.
- Claim commits are created with `git commit-tree` (no checkout and no working-tree change), so a CAS can be prepared from any state.
- Pushes take an explicit lease `(ref, expected_sha | None)`. `None` means "must not exist" and maps to `--force-with-lease=<ref>:`. A bare `--force` is never used.

**GitHub** (`core/github.py`):
- `GitHubClient` is a `typing.Protocol` that works on small frozen dataclasses: `Issue`, `Comment`, `Label`, `Milestone`, `PullRequest`, `Review`.
- `GhCliClient(repo, runner)` shells out to `gh api` (REST, `--paginate` for lists, JSON bodies via `--input -`). The `runner` is injectable so tests never touch the network. `gh` errors map to `GitHubError`. An authentication failure maps to `PreconditionError` (exit 5).
- `FakeGitHub` lives in `src/forge/core/github_fake.py`, not in `tests/`. Command tests in later phases import it, and mypy strict covers it. It is in-memory, deterministic (an injectable clock, incrementing ids) and mirrors GitHub's rules that matter to Forge: issue numbers are shared with PRs, closing with `state_reason`, and labels must exist before use.

## Consequences

- Tests use real temporary git repositories (a bare remote plus clones) through the `git_world` fixture in `tests/conftest.py`, and `FakeGitHub` for GitHub.
- GraphQL (Projects v2, §15) is added to the protocol only when Phase 9 needs it.

## Implementation notes (Phase 1)

- `gh api` flags were checked against `gh api --help` (gh 2.92). Lists use `--paginate --slurp`. Writes use `-X METHOD` with a JSON body via `--input -`. GET query parameters go in the endpoint URL, because `-f` would switch the request to POST.
- `remove_label` treats a 404 as "already absent".
- `Git.push` uses `--porcelain`. Lease rejections and remote rejections caused by a lost race (`cannot lock ref`, `reference already exists`, `but expected`) return `rejected=True`. Other remote rejections, such as hooks or protected branches, raise `GitError`.
- Open, to decide in Phase 3/5: whether Forge's own wip and claim commits and pushes should bypass the user's git hooks (`--no-verify`). Claim commits made with `commit-tree` never run commit hooks.
