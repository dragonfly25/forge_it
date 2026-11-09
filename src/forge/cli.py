"""Forge command-line entry point (Typer app ``app``)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from forge import __version__
from forge.commands import adapters as adapters_cmd
from forge.commands import arch as arch_cmd
from forge.commands import config as config_cmd
from forge.commands import docs as docs_cmd
from forge.commands import escalate as escalate_cmd
from forge.commands import handoff as handoff_cmd
from forge.commands import heartbeat as heartbeat_cmd
from forge.commands import init as init_cmd
from forge.commands import next as next_cmd
from forge.commands import plan as plan_cmd
from forge.commands import plan_apply as plan_apply_cmd
from forge.commands import profile as profile_cmd
from forge.commands import propose as propose_cmd
from forge.commands import reclaim as reclaim_cmd
from forge.commands import refresh as refresh_cmd
from forge.commands import release as release_mod
from forge.commands import release_claim as release_cmd
from forge.commands import review as review_cmd
from forge.commands import setup as setup_cmd
from forge.commands import ship as ship_cmd
from forge.commands import status as status_cmd
from forge.commands import sync as sync_cmd
from forge.commands._output import GlobalOptions, run_command
from forge.commands.plan import plan_app
from forge.commands.task import task_app
from forge.core.output import Result


def _usage_error_class(bad_parameter: type[Exception]) -> type[Exception]:
    """Click's ``UsageError``, the direct base class of ``bad_parameter``.

    Typer vendors click and exports only ``BadParameter``. If a Typer release changes that
    hierarchy, fail loudly at import instead of silently letting usage errors exit 2.
    """
    base = bad_parameter.__mro__[1]
    if base.__name__ != "UsageError" or not issubclass(base, Exception):
        raise ImportError(
            f"forge.cli expects {bad_parameter.__qualname__}'s base class to be click's "
            f"UsageError, found {base.__module__}.{base.__qualname__}; this Typer version is "
            "not supported"
        )
    return base


_UsageError: type[Exception] = _usage_error_class(typer.BadParameter)


GLOBAL_FLAGS: frozenset[str] = frozenset({"--json", "--verbose", "-v", "--yes", "-y"})
"""Global flags (§16) also accepted after the subcommand."""

GLOBAL_VALUE_OPTIONS: frozenset[str] = frozenset({"--agent", "--repo"})
"""Global options taking one value, also accepted after the subcommand."""


def _option_arity(command: Any) -> dict[str, int]:
    """Map every option name of ``command`` (plus ``--help``) to the values it consumes."""
    arity: dict[str, int] = {"--help": 0}
    for param in getattr(command, "params", ()):
        if getattr(param, "param_type_name", None) != "option":
            continue
        takes = 0 if (param.is_flag or param.count) else max(int(param.nargs), 1)
        for name in [*param.opts, *param.secondary_opts]:
            arity[name] = takes
    return arity


def _skip_group_options(command: Any, args: list[str], i: int) -> int | None:
    """Index of the first non-option token of a group's ``args`` from ``i``.

    ``None`` when the group's options cannot be walked safely (``--``, an unknown option or a
    missing value): the arguments are then left exactly as given.
    """
    arity = _option_arity(command)
    while i < len(args):
        token = args[i]
        if token == "--":
            return None
        if not token.startswith("-") or token == "-":
            return i
        name, eq, _ = token.partition("=")
        if name not in arity:
            return None
        i += 1
        if not eq:
            i += arity[name]
    return None


def hoist_global_options(root: Any, ctx: Any, args: list[str]) -> list[str]:
    """Move global options written after the subcommand in front of it (spec §12, §16).

    ``forge status --json`` becomes ``forge --json status``. The subcommand path is resolved
    through nested groups (``forge task show 42 --json``). In the leaf command's arguments the
    flags in :data:`GLOBAL_FLAGS` and the options in :data:`GLOBAL_VALUE_OPTIONS` (``--agent X``
    or ``--agent=X``) are hoisted unless the leaf defines an option with that name itself
    (``forge setup --yes``). Nothing after ``--`` moves, and the value of one of the leaf's own
    value-taking options is never taken for a flag (``--title --json``), and a global option
    whose next token is another option keeps no value (click reports it). Hoisted options are
    inserted right after the root options, so when a global is repeated the one written after
    the subcommand still wins (click keeps the last value). Anything that cannot be parsed
    safely is left untouched, so click reports it as before.
    """
    i = _skip_group_options(root, args, 0)
    if i is None:
        return args
    root_end = i  # hoisted options go here, after the root options, keeping their order
    command: Any = root
    while hasattr(command, "commands"):
        if i is None or i >= len(args):
            return args
        sub = command.get_command(ctx, args[i])
        if sub is None:
            return args
        command = sub
        i += 1
        if hasattr(command, "commands"):
            i = _skip_group_options(command, args, i)
    assert i is not None
    own = _option_arity(command)
    head, tail = args[:i], args[i:]
    hoisted: list[str] = []
    kept: list[str] = []
    j = 0
    while j < len(tail):
        token = tail[j]
        if token == "--":
            kept += tail[j:]
            break
        name, eq, _ = token.partition("=")
        if name in own:
            take = 0 if eq else own[name]
            kept += tail[j : j + 1 + take]
            j += 1 + take
            continue
        if name in GLOBAL_FLAGS and not eq:
            hoisted.append(token)
            j += 1
            continue
        if name in GLOBAL_VALUE_OPTIONS:
            if eq:
                hoisted.append(token)
                j += 1
                continue
            nxt = tail[j + 1].partition("=")[0] if j + 1 < len(tail) else None
            if nxt is not None and not (
                nxt in GLOBAL_FLAGS or nxt in GLOBAL_VALUE_OPTIONS or nxt in own
            ):
                hoisted += tail[j : j + 2]
                j += 2
                continue
        kept.append(token)
        j += 1
    if not hoisted:
        return args
    return [*head[:root_end], *hoisted, *head[root_end:], *kept]


class ForgeGroup(TyperGroup):
    """Root command group.

    - Usage errors exit 1, because exit 2 means "agent action" (§16).
    - Global options are accepted before and after the subcommand
      (:func:`hoist_global_options`).
    """

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        """Hoist trailing global options in front of the subcommand, then parse."""
        return super().parse_args(ctx, hoist_global_options(self, ctx, list(args)))

    def make_context(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().make_context(*args, **kwargs)
        except _UsageError as exc:
            exc.exit_code = 1  # type: ignore[attr-defined]
            raise

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except _UsageError as exc:
            exc.exit_code = 1  # type: ignore[attr-defined]
            raise


app = typer.Typer(
    cls=ForgeGroup,
    name="forge",
    help="Coordinate several people and AI agents working in parallel on one GitHub repository.",
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", "-V", help="Show the Forge version and exit."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON envelope on stdout (ADR 0004)."),
    ] = False,
    agent: Annotated[
        str | None,
        typer.Option("--agent", envvar="FORGE_AGENT", help="Active agent id from the profile."),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Path to the repository (default: current directory)."),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Print more detail."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Answer yes to confirmations."),
    ] = False,
) -> None:
    """Forge: coordinate people and AI agents on one GitHub repository."""
    opts = GlobalOptions(json=json_output, agent=agent, repo=repo, verbose=verbose, yes=yes)
    ctx.obj = opts
    if version:
        if opts.json:
            run_command(opts, lambda: Result(data={"version": __version__}))
        typer.echo(f"forge {__version__}")
        raise typer.Exit(code=0)
    if ctx.invoked_subcommand is None:
        help_text = ctx.get_help()  # rich help prints itself and returns ""
        if help_text:
            typer.echo(help_text)
        raise typer.Exit(code=0)


app.command("refresh", help="Recompute task status labels and unblock dependents.")(
    refresh_cmd.refresh
)
app.command("next", help="Select and claim a task (spec §8.1).")(next_cmd.next_task)
app.add_typer(task_app, name="task")
app.command("heartbeat", help="Refresh the lease of the current task.")(heartbeat_cmd.heartbeat)
app.command("release-claim", help="Release the current task's claim.")(release_cmd.release_claim)
app.command("reclaim", help="Take over a task whose claim went stale.")(reclaim_cmd.reclaim)
app.command("status", help="Show me, team, queue, blocked, milestone and health (§11.3).")(
    status_cmd.status
)
app.command(
    "sync", help="Rebase the current task on the default branch and resolve conflicts (§10)."
)(sync_cmd.sync)
app.command(
    "escalate", help="Escalate the current task: label blocked:conflict and mention people."
)(escalate_cmd.escalate)
app.command("ship", help="Sync, run checks and open or update the task's pull request (§11.1).")(
    ship_cmd.ship
)
app.command("review", help="Print a pull request's review context, or submit a review (§11.2).")(
    review_cmd.review
)
app.command("handoff", help="Hand the current task off with a note (§8.3).")(handoff_cmd.handoff)
app.command("propose", help="Propose new work as a status:proposed issue.")(propose_cmd.propose)
plan_app.command("validate", help="Validate a milestone plan offline (§9.4).")(plan_cmd.validate)
plan_app.command(
    "split", help="Group a plan skeleton's tasks for parallel planners (offline, ADR 0020)."
)(plan_cmd.split)
plan_app.command(
    "assemble", help="Merge the planners' fragments into the plan (offline, ADR 0020)."
)(plan_cmd.assemble)
plan_app.command("apply", help="Create or update the milestone's task issues (§9.5).")(
    plan_apply_cmd.apply
)
app.add_typer(plan_app, name="plan")
arch_cmd.arch_app.command("validate", help="Validate a milestone architecture offline (ADR 0019).")(
    arch_cmd.validate
)
app.add_typer(arch_cmd.arch_app, name="arch")
app.command("init", help="Scaffold Forge project files from templates (§5.2).")(init_cmd.init)
adapters_cmd.adapters_app.command(
    "generate", help="Regenerate adapter files; --check fails on drift (§13)."
)(adapters_cmd.generate)
app.add_typer(adapters_cmd.adapters_app, name="adapters")
config_cmd.config_app.command(
    "validate", help="Validate config.yml, team.yml and zones.yml offline (ADR 0018)."
)(config_cmd.validate)
app.add_typer(config_cmd.config_app, name="config")
app.command("setup", help="Check this machine and create or update the profile (§6.5).")(
    setup_cmd.setup
)
profile_cmd.profile_app.command("show", help="Show the profile and resolved models.")(
    profile_cmd.show
)
profile_cmd.profile_app.command("set", help="Set <agent>.<tier|effort.level>=<value>.")(
    profile_cmd.set_
)
profile_cmd.profile_app.command("edit", help="Edit the profile in $VISUAL or $EDITOR.")(
    profile_cmd.edit
)
app.add_typer(profile_cmd.profile_app, name="profile")
app.command("release", help="Release a milestone: changelog PR, then tag and close (§11.4).")(
    release_mod.release_cmd
)
app.command("docs", help="Generate the command reference; --check fails on drift.")(
    docs_cmd.docs_cmd
)
