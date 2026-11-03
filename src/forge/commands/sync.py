"""`forge sync [--continue|--abort] [--force-resolve]`: rebase the current task on the default
branch and resolve conflicts (spec §10, ADR 0011).

The work is done by :mod:`forge.core.sync`; this module loads the inputs (config, profile,
zones, team), enforces the ``forge_version`` pin (ADR 0003) and renders the outcome. Git runs
in the current worktree (the cwd or ``--repo``), never in the main clone.
"""

from __future__ import annotations

import io
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.text import Text

from forge.commands import _claims
from forge.commands._context import AppContext, find_repo_root, load_context
from forge.commands._output import GlobalOptions, get_options, run_command
from forge.core.config import load_config
from forge.core.errors import ForgeError, PreconditionError
from forge.core.git import Git
from forge.core.output import Result
from forge.core.sync import (
    AbortContext,
    SyncContext,
    SyncOptions,
    SyncOutcome,
    load_sync_zones,
    sync_abort,
    sync_continue,
)
from forge.core.sync import sync as run_sync
from forge.core.workspace import find_current_task

MAX_COMMITS_SHOWN = 5
"""Upstream commits listed in the human view before ``+N more``."""

MAX_OUTPUT_LINES = 20
"""Lines of a check's (or hook's) output tail shown in the human view."""

_HEADLINES = {
    "pushed": "Synced and pushed",
    "up_to_date": "Already up to date",
    "conflicts": "Sync stopped on conflicts",
    "checks_failed": "Sync stopped: failing checks",
    "push_rejected": "Sync stopped: the pre-push hook rejected the push",
    "lease_rejected": "Sync stopped: the branch moved on origin, nothing was pushed",
    "wip_rejected": "Sync not started: a commit hook rejected the `forge: wip` commit",
    "escalated": "Sync escalated",
    "aborted": "Sync aborted",
}
_STYLES = {
    "pushed": "green",
    "up_to_date": "green",
    "aborted": "yellow",
    "escalated": "bold red",
}


def sync(
    ctx: typer.Context,
    continue_: Annotated[
        bool,
        typer.Option("--continue", help="Resume a stopped sync after resolving and `git add`."),
    ] = False,
    abort: Annotated[
        bool, typer.Option("--abort", help="Abort the sync and restore the pre-sync state.")
    ] = False,
    force_resolve: Annotated[
        bool,
        typer.Option(
            "--force-resolve",
            help="Resolve out-of-zone or contract conflicts yourself instead of escalating.",
        ),
    ] = False,
) -> None:
    """Rebase the current task on the default branch and resolve conflicts (spec §10)."""
    opts = get_options(ctx)

    def run() -> Result:
        _check_flags(continue_=continue_, abort=abort, force_resolve=force_resolve)
        if abort:
            return _abort(opts)
        app = load_context(opts)
        warnings: list[str] = []
        problem = _claims.version_problem(app.config)
        if problem is not None:
            raise PreconditionError(problem, hint=_claims.VERSION_HINT)
        sync_ctx = _sync_context(opts, app, warnings)
        if continue_:
            outcome = sync_continue(sync_ctx)
        else:
            outcome = run_sync(sync_ctx, SyncOptions(force_resolve=force_resolve))
        return _result(outcome, warnings)

    run_command(opts, run, lambda result: render_sync(result.data))


def _check_flags(*, continue_: bool, abort: bool, force_resolve: bool) -> None:
    """``--continue``, ``--abort`` and ``--force-resolve`` exclude each other (exit 1)."""
    if continue_ and abort:
        raise ForgeError(
            "--continue and --abort cannot be used together",
            hint="run `forge sync --continue` or `forge sync --abort`",
        )
    if force_resolve and (continue_ or abort):
        flag = "--continue" if continue_ else "--abort"
        raise ForgeError(
            f"--force-resolve cannot be used with {flag}",
            hint="--continue keeps the --force-resolve value recorded when the sync started; "
            "to change it, run `forge sync --abort`, then `forge sync --force-resolve`",
        )


def _abort(opts: GlobalOptions) -> Result:
    """``--abort`` is a local restore: git of the worktree and the clock, nothing else.

    No profile, no GitHub, no zones. ``.forge/config.yml`` is read only to warn about a
    ``forge_version`` mismatch; when it cannot be read (it may hold conflict markers
    mid-rebase) that is a warning too, never an error.
    """
    root = find_repo_root(opts)
    warnings: list[str] = []
    try:
        problem = _claims.version_problem(load_config(root))
    except ForgeError as exc:
        warnings.append(f"config not loaded ({exc.message}); the abort does not need it")
    else:
        if problem is not None:
            warnings.append(problem)
    outcome = sync_abort(AbortContext(git=Git(root), clock=lambda: _claims.clock()))
    return _result(outcome, warnings)


def _sync_context(opts: GlobalOptions, app: AppContext, warnings: list[str]) -> SyncContext:
    """Profile, agent, zones and team for the core :class:`SyncContext`."""
    location = find_current_task(app.git)
    recorded = None if location is None else _claims.recorded_agent(location)
    profile, agent = _claims.load_profile_and_agent(opts, recorded)
    zones = load_sync_zones(app.root)
    team = _claims.try_load_team(app, warnings)
    return SyncContext(
        config=app.config,
        git=app.git,
        github=app.github,
        zones=zones,
        agent=agent,
        profile_login=profile.github,
        team=team,
        clock=lambda: _claims.clock(),
    )


def _result(outcome: SyncOutcome, warnings: list[str]) -> Result:
    result = outcome.to_result()
    for text in warnings:
        result.warn(text)
    return result


# --------------------------------------------------------------------------- rendering


def _console(buffer: io.StringIO) -> Console:
    """A console that never interprets user text (paths, subjects) as markup or emoji."""
    probe = Console()
    return Console(
        file=buffer,
        force_terminal=probe.is_terminal,
        no_color=probe.no_color,
        width=min(probe.width, 100),
        markup=False,
        emoji=False,
        highlight=False,
        soft_wrap=True,
    )


def render_sync(data: dict[str, Any]) -> str:
    """Human view of the ``forge sync`` data: outcome, classification, upstream context,
    conflicts, failing checks and the next step for the agent."""
    buffer = io.StringIO()
    console = _console(buffer)
    _render_headline(console, data)
    _render_classification(console, data.get("classification"))
    _render_upstream(console, data.get("upstream"))
    _render_conflicts(console, data)
    _render_checks(console, data.get("checks") or [])
    output = data.get("output")
    if output:
        console.print("Hook output (tail):", style="bold")
        _render_tail(console, output)
    _render_next(console, data)
    return buffer.getvalue()


def _short(sha: str | None) -> str:
    return sha[:12] if sha else "-"


def _render_headline(console: Console, data: dict[str, Any]) -> None:
    outcome = data["outcome"]
    line = Text(_HEADLINES.get(outcome, outcome), style=_STYLES.get(outcome, "bold yellow"))
    line.append(f"  {data['id']} #{data['issue']} on {data['branch']}")
    console.print(line)
    details = []
    if data.get("onto"):
        details.append(f"onto {_short(data['onto'])}")
    if data.get("head"):
        details.append(f"head {_short(data['head'])}")
    if data.get("rebased"):
        details.append("rebased")
    if details:
        console.print("  " + " · ".join(details))


def _render_classification(console: Console, classification: dict[str, Any] | None) -> None:
    if classification is None:
        return
    line = Text("Classification: ", style="bold")
    line.append(classification["primary"])
    categories = classification["categories"]
    if categories != [classification["primary"]]:
        line.append(f" ({', '.join(categories)})")
    line.append(
        f" · upstream files {classification['upstream_files']}, "
        f"task files {classification['task_files']}, shared {len(classification['shared'])}"
    )
    console.print(line)
    if classification["out_of_zone"]:
        console.print("  outside the task's zones: " + ", ".join(classification["out_of_zone"]))
    for zone, files in classification["semantic"].items():
        console.print(f"  contract zone {zone} changed upstream: " + ", ".join(files))


def _render_upstream(console: Console, upstream: dict[str, Any] | None) -> None:
    if upstream is None or not upstream["commits"]:
        return
    commits: list[dict[str, Any]] = upstream["commits"]
    pulls = {p["number"]: p for p in upstream["pulls"]}
    console.print(Text(f"Upstream commits ({len(commits)}):", style="bold"))
    for commit in commits[:MAX_COMMITS_SHOWN]:
        refs = []
        for number in commit["pulls"]:
            pr = pulls.get(number)
            if pr is None:
                continue
            issues = ", ".join(f"#{n}" for n in pr["issues"])
            refs.append(f"PR #{number} by {pr['author']}" + (f" → {issues}" if issues else ""))
        where = "; ".join(refs) or "no pull request found"
        console.print(f"  {commit['sha'][:7]} {commit['subject']} ({where})")
    rest = len(commits) - MAX_COMMITS_SHOWN
    if rest > 0:
        console.print(f"  +{rest} more", style="dim")


def _render_conflicts(console: Console, data: dict[str, Any]) -> None:
    conflicts: list[dict[str, Any]] = data.get("conflicts") or []
    unresolved: list[str] = data.get("unresolved") or []
    if not conflicts and not unresolved:
        return
    console.print(Text("Conflicts:", style="bold"))
    seen: set[tuple[str, str | None]] = set()
    for entry in conflicts:
        path, resolution = entry["path"], entry["resolution"]
        if (path, resolution) in seen:
            continue
        seen.add((path, resolution))
        line = Text(f"  {path}  zone {','.join(entry['zones']) or '-'}")
        if entry["outside"]:
            line.append("  outside the task's zones", style="red")
        if entry["contract"]:
            line.append("  contract", style="red")
        line.append("  ")
        if resolution is None:
            line.append("open", style="bold yellow")
        else:
            line.append(f"resolved: {resolution}", style="green")
        if entry.get("detail"):
            line.append(f" ({entry['detail']})")
        console.print(line)
    for path in unresolved:
        if (path, None) not in seen:
            console.print(Text(f"  {path}  ") + Text("open", style="bold yellow"))


def _render_tail(console: Console, output: str) -> None:
    lines = output.rstrip("\n").splitlines()
    if len(lines) > MAX_OUTPUT_LINES:
        console.print(f"    ... {len(lines) - MAX_OUTPUT_LINES} line(s) above", style="dim")
        lines = lines[-MAX_OUTPUT_LINES:]
    for line in lines:
        console.print(f"    {line}")


def _render_checks(console: Console, checks: list[dict[str, Any]]) -> None:
    if not checks:
        return
    failed = [c for c in checks if c["exit_code"] != 0]
    summary = Text("Checks: ", style="bold")
    summary.append(f"{len(checks) - len(failed)}/{len(checks)} passed")
    console.print(summary)
    for check in failed:
        line = Text("  ")
        line.append(check["name"], style="bold red")
        line.append(f" (exit {check['exit_code']}): {check['command']}")
        console.print(line)
        if check["output"]:
            _render_tail(console, check["output"])


def _next_step(data: dict[str, Any]) -> str | None:
    outcome = data["outcome"]
    issue = data["issue"]
    if outcome == "conflicts":
        where = f" (details in {data['state_file']})" if data.get("state_file") else ""
        return (
            "resolve the files following the /sync rules, `git add` them, then run "
            f"`forge sync --continue` (or `forge sync --abort`){where}"
        )
    if outcome == "checks_failed":
        return (
            "fix your own code, commit, then run `forge sync --continue`; if the failing area "
            'belongs to someone else, run `forge escalate --reason "..."`'
        )
    if outcome == "push_rejected":
        return "fix what the hook reports, commit, then run `forge sync --continue`"
    if outcome == "lease_rejected":
        return f"run `forge sync --abort`, then check `forge task show {issue}`"
    if outcome == "wip_rejected":
        return "fix what the hook reports (or commit your changes yourself), then run `forge sync`"
    if outcome == "escalated":
        escalation = data.get("escalation") or {}
        mentioned = escalation.get("mentioned") or []
        who = ", ".join(f"@{m}" for m in mentioned) if mentioned else "the people on the issue"
        saved = data.get("saved_ref")
        branch = (
            "your branch is unchanged"
            if not saved
            else f"your branch was reset to its pre-sync state (your newer commits: {saved})"
        )
        return (
            f"escalated: #{issue} is labelled blocked:conflict and {branch}; "
            f"wait for {who}, or run `forge sync --force-resolve` if you own the change"
        )
    return None


def _render_next(console: Console, data: dict[str, Any]) -> None:
    step = _next_step(data)
    if step is None:
        return
    line = Text("Next: ", style="bold")
    line.append(step)
    console.print(line)
