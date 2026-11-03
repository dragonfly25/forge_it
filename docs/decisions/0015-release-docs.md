# 0015 — `forge release`, `forge docs`, framework docs, Projects v2

- Status: accepted
- Date: 2026-11-03

## Context

Spec §11.4 says `forge release <milestone>` has five steps:
1. a soft lead check;
2. all tasks closed, or a list of the open ones and a stop;
3. a CHANGELOG section from merged PR titles grouped by type;
4. a tag `v<x.y.z>` and closing the milestone;
5. posting a summary.

`forge docs [--check]` generates `docs/forge/commands.md`. Several things are left open:
- **CHANGELOG:** how it reaches the default branch, given that Forge never pushes to it (`rules.md`, CLAUDE.md) and branch protection is common.
- **Tag target:** which commit the tag points to.
- **Summary:** where it goes.
- **`forge docs` scope:** what the generated file contains, and how it stays deterministic.
- **Framework repository:** how `forge docs` works there, since it has no `.forge/`.
- **Projects v2:** whether the optional §15 mirroring ships in v0.

## Decision

### `forge release <milestone> [--version X.Y.Z] [--dry-run]`: two steps, never a push to the default branch
1. **Checks**:
   - **Lead check (soft)**: when my login is neither the milestone's `lead` in `plans/roadmap.yml` nor a team member with role `lead`, Forge warns and continues.
   - **Version**: `X.Y.Z` must be a valid PEP 440 release, without a `v` prefix. It is required: without `--version`, release exits 5 and the hint gives the next patch or minor version after the latest `v*` tag.
   - **Open tasks**: if open `forge:task` issues remain in the milestone, release lists them and exits 5.
   - **Clone**: a shallow clone exits 5.
2. **Release PR**, when the default branch has no `## [X.Y.Z]` section in `CHANGELOG.md`:
   - **Section**: built from the PRs merged into the default branch whose head is `task/<n>`, for tasks `n` of the milestone. Each line is `- <task title without the [id] prefix> (#<pr>)`.
   - **Grouping**: by the task's metadata `type`, under `### Contracts`, `### Features` (impl), `### Tests` and `### Chores`.
   - **Placement**: the section `## [X.Y.Z] - <YYYY-MM-DD>` goes right below `## [Unreleased]`, or below the title when there is no Unreleased section. A missing `CHANGELOG.md` is created with a Keep a Changelog header.
   - **Branch and PR**: Forge commits on a branch `release/vX.Y.Z` made from `origin/<default>`, pushes it with a lease (new branch), and opens the PR `chore(release): vX.Y.Z` with the section as its body. It exits 0 with `step: pr_opened`.
   - **Re-runs**: an existing release branch or PR is reused, and the section is regenerated if it differs.
3. **Tag**, when the default branch already has the `## [X.Y.Z]` section (the release PR is merged):
   - **Tag**: Forge creates the annotated tag `vX.Y.Z` on the commit that landed the section: the merged release PR's `merge_commit_sha`, which is the merge, squash or last rebased commit. See the implementation notes for the manual-merge fallback. It pushes it with a lease that expects the tag to be absent.
   - **Existing tag**: one on the same commit is fine, so re-runs are idempotent. One on another commit exits 5.
   - **GitHub release**: Forge creates a GitHub release for the tag, titled `vX.Y.Z — <milestone title>`, with the section as its notes (new `GitHubClient.create_release` / `get_release_by_tag`). This is the "summary" of §11.4.
   - **Milestone**: Forge closes it. It exits 0 with `step: released`.
- **`--dry-run`**: prints the section and the step that would run. It writes nothing.
- **Tag pushes**: hooks are not run, because the tag carries no new code.

### `forge docs [--check] [--output PATH] [--prompts DIR]`
- **Content**, deterministic and independent of terminal width:
  - **Command reference**: built from the Click command tree, not from rendered help. For every command and subcommand: the usage line, the help text, and the arguments and options with their help, defaults and choices.
  - **Prompt table**: name, description, args and roles from the frontmatter of `.forge/prompts/*.md`.
  - A generated header.
- **Defaults**: `--output docs/forge/commands.md` and `--prompts .forge/prompts`.
- **`--check`**: writes nothing, and exits 1 with a short diff summary when the file is missing or differs.
- **Init and CI**:
  - `forge init` also writes `docs/forge/commands.md` (this updates ADR 0014's template tree).
  - The template `forge-ci.yml` runs `forge docs --check`.
- **Framework repository**: it keeps its own `docs/commands.md`, generated with `--prompts src/forge/templates/project/.forge/prompts --output docs/commands.md`. Its CI runs the same command with `--check`.

### Framework docs
- `README.md` covers what Forge is, prerequisites, install, a quickstart for the lead and for developers, and development.
- `docs/protocol.md` is the coordination protocol: tasks, claims, leases, zones, sync, ship, review and planning, linking the ADRs.
- `docs/architecture.md` covers the module map, `core/` vs `commands/`, the Git and GitHub boundaries, testing and the exit codes.
- `docs/adapters.md` covers how prompts become tool files, the verified formats, and how to add an adapter.
- **Final `install.sh`**:
  - **Version choice**: it installs the newest `v*` tag (from `git ls-remote --tags`), or `main` when there is no tag yet. `FORGE_VERSION` overrides both.

### Projects v2 (§15)
Deferred to v1. It is optional in the spec, and the board is a view while labels stay authoritative. It needs a GraphQL surface that v0 does not otherwise use. `github.project_number` is still accepted in `config.yml` and ignored, with an info message from `forge setup --check`.

## Implementation notes (Phase 9)

**GitHub API (checked 2026-11-03)**
- **Create**: `POST /repos/{owner}/{repo}/releases` takes `tag_name`, `target_commitish` (ignored when the tag exists), `name`, `body`, `draft`, `prerelease` and `make_latest`. A duplicate is a generic 422.
- **Read**: `GET /repos/{owner}/{repo}/releases/tags/{tag}` returns published releases only, 404 otherwise; Forge maps 404 to "no release". A draft release for the tag blocks the release, with a hint to publish or delete it.

**Release**
- **Manual-merge fallback**: used only when no merged release PR exists. Forge does a first-parent walk of `origin/<default>` (`git log --first-parent --diff-merges=first-parent -G`). Each candidate is confirmed by reading `CHANGELOG.md` at its parent. A merge yields the merge commit, a squash the squash commit. A later edit of the heading's date is ignored, and a section removed then re-added counts from its latest addition. Plain `-S` would skip merges and return the commit on the release branch.
- **Release commit**: built with `commit-tree`, without touching HEAD, the index or the working tree. It carries `Forge-Release-Notes`, `Forge-Milestone` and `Forge-Section-Sha256`, but not `Forge-Release`, which is the release-claim trailer.
- **Forge's own commit**: a branch tip counts as Forge's only when its trailers, its section hash and its tree all match. An amended commit counts as a hand edit.
- **Manual tag recovery**: when the merged PR's commit is not on the default branch, Forge accepts an existing annotated tag `vX.Y.Z` for this milestone on a default-branch commit that carries the section. The hint gives the exact `git tag -a` command.
- **Milestone ids**: they must have the form `M<n>` (exit 5 otherwise).
- **Hooks**: the release-branch push runs the user's pre-push hook (a rejection exits 2). The tag push skips hooks.
- **`[Unreleased]` content**: it is never moved. The new section goes below it, with a warning.
- **Release title**: it uses the roadmap title, falling back to the milestone id. The PR body and the release notes are the whole section, heading included.
- **Section date**: re-runs keep the date of the first run, so a re-run never changes the section.
- **Versions**: strictly `X.Y.Z`. Pre-releases, dev releases, post-releases, epochs and local versions are rejected.
- **Races**: they converge.
  - Two leads opening the release branch: the second push is rejected, and the run re-fetches and reuses the first lead's PR.
  - Two runs creating the PR: the 422 resolves to the existing PR.
  - A tag pushed concurrently on the same commit is accepted. One on another commit exits 5.

- **Tag target (review fix)**:
  - Forge tags the merged release PR's merge commit (`merge_commit_sha`): the merge commit, the squash commit, or the last commit of a rebase-merge.
  - **Fallback**: the first-parent walk is used only when no merged release PR exists, i.e. a manual merge.
  - **One commit**: the release branch always holds a single Forge commit, rebuilt on `origin/<default>` and pushed with a lease on its current tip. A rebase-merge therefore cannot put an outdated changelog section on the default branch.
- **Hand edits**:
  - Forge's release commit carries `Forge-Release-Notes: vX.Y.Z`. When the branch tip is not such a commit, someone edited the section through the PR.
  - In that case Forge keeps the branch, warns, and lists merged task PRs missing from the section.
- **Version reused**: an existing tag, heading or GitHub release is accepted as "done" only when it names this milestone. Otherwise release exits 5 ("already released for <other>").
  - **Where the milestone is recorded**: in the annotated tag message (`vX.Y.Z (M1)`), in the release commit's `Forge-Milestone` trailer, and in a hidden `<!-- forge:release-notes milestone=M1 -->` line at the end of the PR body and of the release notes.
  - **Timing**: all of them are checked before any write.
  - **Body content**: the PR body and the release notes are therefore the section plus that one comment line.
  - **Tag commit check**: the merged PR's commit is read with `get_pull`, which includes `merge_commit_sha`. It must be on the default branch and carry the heading, otherwise release exits 5.
- **Version checks**: a version that is not newer than the latest `v*` tag gets a warning. A missing `--version` exits 5 but keeps the warnings already collected.
- **Latest release**: GitHub releases are created with `make_latest: legacy`, so a backport does not become "latest".

**Docs**
- **Order**: commands are listed alphabetically by full path, and parameters keep their declaration order.
- **Paths**: `--output` and `--prompts` resolve against the repository root.
- **Missing prompts directory**: it exits 5 and names the path.
- **No version**: the generated file contains no Forge version, so a version bump never makes `--check` fail.
- **How init gets the command tree**: `forge init` passes the CLI description explicitly (`describe_cli()`). `core/` cannot import the CLI.

## Consequences

- Releasing takes two runs of the same command around a normal PR review. Branch protection on the default branch never blocks it.
- The tag points at exactly the reviewed commit that carries the changelog.
- `forge docs --check` keeps the command reference honest in CI.
