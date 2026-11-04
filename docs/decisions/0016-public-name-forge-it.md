# 0016 — Public repository `dragonfly25/forge_it`

- Status: accepted
- Date: 2026-11-04

## Context

Forge was built in a private repository whose name referred to a company. For the public release the owner wants no such reference and no personal email in the history. GitHub keeps the head commits of merged pull requests forever, so rewriting the private repository's history and making it public would still expose those commits.

## Decision

- **New repository**: Forge is published as the new public repository **`dragonfly25/forge_it`**. The original private repository is kept as an archive, with its pull requests, issues and original history.
- **Names**:
  - The Python distribution name becomes **`forge-it`**.
  - The import package stays `forge` and the CLI stays `forge`.
  - The install URL becomes `git+https://github.com/dragonfly25/forge_it`.
- **Clean history**: the public history is a rewritten copy of the private `main` with the same final tree. Author and committer emails become the GitHub noreply address, and the old repository and distribution names are replaced in file contents and commit messages.
- **Tag**: `v0.1.0` is created on the last commit of that clean copy.
- **Future commits**: they use the noreply address (repository-local `user.email`).

## Consequences

- ADR 0001's naming is superseded for the repository and distribution names. Earlier ADRs and history refer to the new names after the rewrite.
- Pull request discussions are not in the public repository; they stay in the private archive.
