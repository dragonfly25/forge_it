---
name: review
description: Review a task's pull request against its acceptance criteria, zones, contracts and tests
args: "<pr-number> [--force]"
roles: [dev, lead]
---
## Goal
Give an honest, specific review of one pull request: do the changes meet the task's acceptance criteria and `done_when` items, stay inside the zones and the task's component, respect the contracts and the interfaces of the architecture, and carry tests that prove them?

## Steps
1. Get the review context (PR metadata, linked task, spec excerpts, diff, zone check, CI, existing reviews):
   `forge review <pr> --json`
2. Read the architecture. Find the task's entry in `plans/<milestone>.yml` (its `component`, `implements` and `uses`). If `docs/architecture/<milestone>.md` exists, read it and `docs/architecture/overview.md`.
3. Review the diff against the task. Check each item:
   - every `covers` acceptance criterion is met;
   - every `done_when` item is true and proven by a test;
   - changes stay inside the task's zones. Judge each out-of-zone and unzoned file;
   - contract zones are changed only by a task that owns them;
   - changes stay inside the task's component, the interfaces in `implements` match their declaration in the architecture, and the interfaces in `uses` are used as declared;
   - tests exist, are meaningful and were not weakened;
   - the code is correct, clear and consistent with the ADRs and `.forge/rules.md`;
   - CI state from the context. A failing or missing CI is a finding.
   If `diff_truncated` is true, say so and read the remaining files in the PR before you decide.
4. Write the review body to a file, for example `.forge/local/review-<pr>.md`. Be specific: file, line, what is wrong, what to do. Separate blocking findings from suggestions.
5. Choose the verdict: `approve`, `request-changes` or `comment`.
6. Show the user the verdict and the body, unless they already asked you to submit. Then submit:
   `forge review <pr> --submit approve --body-file <file> --json`
   `forge review <pr> --submit request-changes --body-file <file> --json`
   `forge review <pr> --submit comment --body-file <file> --json`

## Exit codes
- `forge review <pr>` (context):
  - `0`: use the context. Warnings (no linked task, CI unreadable, no profile) go into your report.
  - `5`: a precondition failed. If your agent is below the tier that `min_tier_for.reviewer` requires, ask the user to run with a stronger agent (`--agent <id>`). Use `--force` only if the user says so. Other causes: `gh` not logged in, shallow clone, no config.
  - `1`: another failure, or a usage error. Report `messages`.
- `forge review <pr> --submit ...`:
  - `0`: submitted. Report the verdict.
  - `5`: you cannot approve or request changes on your own PR (use `comment`), there is no profile (submit needs one: run `/forge-setup`), the `gh` login does not match the profile, the CLI version is outside `forge_version`, or the tier refusal above.
  - `1`: `--body-file` missing, an unknown verdict or another failure. Fix the call.
- `2`, `3` and `4` are not expected from these calls. Report the output to the user.

## Guardrails
- Never edit code, push, or fix the PR yourself. You only review.
- Never approve your own PR, and never approve with failing checks without stating it in the review.
- Do not approve what you did not read. If the diff is too large, say which parts you did not review.
- Be specific and kind. No vague comments.
- Do not include secrets in the review body.
- Do not submit an approval or a change request without the user's go-ahead, unless they asked you to submit.

## Subagents
- If `forge-reviewer` is available, delegate the first pass to it. Give it the PR number, the context and the task's `component`, `implements` and `uses`. Check its findings yourself and decide the verdict.
- Without it, do the full review yourself.

## Final report
Tell the user:
- the PR number, the task id and the verdict you submitted (or propose);
- blocking findings and suggestions, each with file and line;
- the CI state and the out-of-zone files you judged;
- anything you could not check.
