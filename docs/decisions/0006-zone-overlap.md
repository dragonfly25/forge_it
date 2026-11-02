# 0006 — Zone matching and overlap

- Status: accepted
- Date: 2026-11-02

## Context

Spec §6.3: two tasks overlap if they share a zone, if a pair of their zones is listed under `conflicts`, or if their zones' path globs intersect. Deciding exactly whether two globs (`**`, `*`, `?`, `[...]`) can match a common path is costly and easy to get wrong. A false negative (missed overlap) defeats conflict prevention. A false positive only reduces parallelism.

## Decision

- Glob semantics are gitignore-like but anchored at the repository root. `**` matches zero or more path segments. `*` and `?` never cross `/`. `[...]` is a character class. Paths are POSIX, relative, with no leading `./`. Matching is implemented in `core/zones.py` by compiling globs to regular expressions. `fnmatch` is not used, because its `*` crosses `/`.
- **Path → zones**: `zones_for_path(path)` returns every zone with a matching glob, since zones may overlap. A changed file that matches no zone is *unzoned*, and `unzoned: warn|error` decides the severity.
- **Glob intersection** is conservative. Two globs intersect unless they can be proven disjoint, which takes one of these two checks:
  1. The literal (wildcard-free) leading directory segments diverge. Example: `packages/editor/**` vs `packages/api/**`.
  2. Both globs are fully literal paths and they differ.
  In every other case the globs are treated as intersecting.
- Zone-set overlap returns a reason that people can read: `shared zone X`, `declared conflict X↔Y`, or `globs intersect: A ~ B`.
- `zones.yml` validation: zone names match `^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$`, `paths` is a non-empty list, `conflicts` pairs reference known zones, and `unzoned` is `warn` or `error`.

## Consequences

- Some globs that don't really intersect are reported as overlapping, for example `src/*.py` vs `src/*.ts`. Teams fix this by splitting directories or by accepting the warning. The behaviour is documented in `docs/forge/faq.md` (Phase 8).
- A smarter intersection can replace this later without changing the API.

## Implementation notes (Phase 1)

- A glob without wildcards (e.g. `contracts/openapi`) matches **only that exact path**, unlike gitignore, where a bare directory name matches its contents. Write `contracts/openapi/**` to cover a directory. Globs ending in `/` are rejected with that fix. The Phase 8 FAQ and the `zones.yml` template comments must say this.
- `a/**` matches everything under `a/`, but not a file named `a`.
- Known false positives of the conservative intersection: `src/*.py` vs `src/*.ts`, and `a` vs `a/b/**`.
