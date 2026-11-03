"""`forge docs [--check] [--output PATH] [--prompts DIR]` (ADR 0015).

Walks the Click command tree (never the rendered help) into the neutral
:class:`forge.core.docs.CliDoc` and renders it with the prompt table. ``--check`` writes
nothing and exits 1 with a short diff summary when the file is missing or different.
A missing prompts directory exits 5 (precondition). No profile or GitHub access is needed.

Importing this module registers :func:`describe_cli` with :mod:`forge.core.docs`, so
``forge init`` can write the same reference.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Annotated, Any

import typer

from forge.commands._context import find_repo_root
from forge.commands._output import GlobalOptions, get_options, run_command
from forge.core import docs as core_docs
from forge.core.errors import ExitCode, PreconditionError
from forge.core.output import Result


def _type_text(param: Any) -> str:
    """The parameter's type as stable text (``flag``, ``text``, ``integer (x>=1)``…)."""
    if getattr(param, "is_flag", False) or getattr(param, "count", False):
        return "count" if getattr(param, "count", False) else "flag"
    ptype = param.type
    text = str(getattr(ptype, "name", "") or type(ptype).__name__)
    describe = getattr(ptype, "_describe_range", None)
    if callable(describe):
        text = f"{text.removesuffix(' range')} ({describe()})"
    if getattr(param, "multiple", False) or (isinstance(param.nargs, int) and param.nargs > 1):
        text += ", repeatable"
    return text


def _value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, enum.Enum):
        return str(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (list, tuple)):
        return ", ".join(_value_text(v) for v in value)
    return str(value)


def _default_text(param: Any) -> str | None:
    if param.required:
        return None
    default = param.default
    if callable(default):
        return "(computed)"
    if default is None or default is Ellipsis:
        return None
    if isinstance(default, (list, tuple)) and not default:
        return None
    if getattr(param, "is_flag", False) and default is False:
        return None  # an unset flag needs no default
    return _value_text(default)


def describe_param(param: Any) -> core_docs.ParamDoc:
    """Describe one Click parameter."""
    is_option = getattr(param, "param_type_name", "") == "option"
    if is_option:
        names = (*param.opts, *param.secondary_opts)
    else:
        names = (str(param.human_readable_name).upper(),)
    choices = getattr(param.type, "choices", None) or ()
    envvar = param.envvar if isinstance(param.envvar, str) else None
    if isinstance(param.envvar, (list, tuple)) and param.envvar:
        envvar = ", ".join(param.envvar)
    return core_docs.ParamDoc(
        kind="option" if is_option else "argument",
        names=tuple(names),
        type=_type_text(param),
        required=bool(param.required),
        default=_default_text(param),
        choices=tuple(_value_text(c) for c in choices),
        help=core_docs.clean_text(getattr(param, "help", None)),
        envvar=envvar,
    )


def describe_command(
    command: Any, path: tuple[str, ...], parent: Any = None
) -> core_docs.CommandDoc:
    """Describe ``command`` and, for a group, its visible subcommands (recursively)."""
    ctx = command.context_class(command, info_name=path[-1], parent=parent)
    pieces = [str(p) for p in command.collect_usage_pieces(ctx)]
    usage = core_docs.clean_text(" ".join([*path, *pieces]))
    params = tuple(describe_param(p) for p in command.params if not getattr(p, "hidden", False))
    subs: list[core_docs.CommandDoc] = []
    for name, sub in sorted(getattr(command, "commands", {}).items()):
        if getattr(sub, "hidden", False):
            continue
        subs.append(describe_command(sub, (*path, name), ctx))
    return core_docs.CommandDoc(
        path=path,
        usage=usage,
        help=core_docs.clean_text(command.help),
        params=params,
        subcommands=tuple(subs),
    )


def describe_cli(root: Any = None) -> core_docs.CliDoc:
    """Describe the Forge CLI (or the given root Click group)."""
    from forge import cli  # late import: forge.cli imports this module

    if root is None:
        root = typer.main.get_command(cli.app)
    hoisted = cli.GLOBAL_FLAGS | cli.GLOBAL_VALUE_OPTIONS
    after: list[str] = []
    for param in root.params:
        if getattr(param, "param_type_name", "") != "option":
            continue
        after += [n for n in (*param.opts, *param.secondary_opts) if n in hoisted]
    return core_docs.CliDoc(root=describe_command(root, ("forge",)), after_subcommand=tuple(after))


core_docs.register_cli(describe_cli)


def _start_dir(opts: GlobalOptions) -> Path:
    """The repository root, or the ``--repo``/current directory outside a git repository."""
    try:
        return find_repo_root(opts)
    except PreconditionError:
        return (opts.repo if opts.repo is not None else Path.cwd()).expanduser()


def _render(result: Result) -> str:
    data = result.data
    if data.get("check"):
        if not data["diff"]:
            return f"{data['output']} is up to date."
        return "\n".join([f"{data['output']} is out of date:", *(f"  {d}" for d in data["diff"])])
    verb = "Wrote" if data["written"] else "Unchanged:"
    return f"{verb} {data['output']} ({data['commands']} commands, {data['prompts']} prompts)."


def docs_cmd(
    ctx: typer.Context,
    check: Annotated[
        bool,
        typer.Option("--check", help="Write nothing; exit 1 when the file is missing or differs."),
    ] = False,
    output: Annotated[
        Path,
        typer.Option("--output", help="File to write, relative to the repository root."),
    ] = core_docs.DEFAULT_OUTPUT,
    prompts: Annotated[
        Path,
        typer.Option("--prompts", help="Prompts directory, relative to the repository root."),
    ] = core_docs.DEFAULT_PROMPTS,
) -> None:
    """Generate the command reference from the command tree and the prompts."""
    opts = get_options(ctx)

    def run() -> Result:
        root = _start_dir(opts)
        loaded = core_docs.load_prompts_dir(root, prompts)
        cli_doc = describe_cli(ctx.find_root().command)
        text = core_docs.render_reference(cli_doc, loaded)
        target = output if output.is_absolute() else root / output
        data: dict[str, Any] = {
            "output": output.as_posix(),
            "check": check,
            "commands": sum(1 for _ in core_docs.walk(cli_doc.root)),
            "prompts": len(loaded),
        }
        if check:
            diff = core_docs.check_reference(target, text, label=output)
            result = Result(data={**data, "diff": diff, "up_to_date": not diff})
            if diff:
                result.code = ExitCode.ERROR
                result.error(
                    f"{output.as_posix()} is out of date",
                    "run `forge docs` and commit the result",
                )
            return result
        written = core_docs.write_reference(target, text)
        return Result(data={**data, "written": written})

    run_command(opts, run, _render)
