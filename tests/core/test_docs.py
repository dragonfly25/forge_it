"""Tests for the command reference renderer (ADR 0015). No Click: a hand-built CliDoc."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.core import docs as D
from forge.core.errors import ConfigError, ExitCode, ForgeError, PreconditionError
from forge.core.prompts import parse_prompt

PROMPT = """\
---
name: next
description: Pick | claim a task
args: "[task-id]"
roles: [dev]
---
## Goal
Go.
"""

CLI = D.CliDoc(
    root=D.CommandDoc(
        path=("forge",),
        usage="forge [OPTIONS] COMMAND [ARGS]...",
        help="Root help.",
        params=(
            D.ParamDoc(kind="option", names=("--json",), type="flag", help="JSON out."),
            D.ParamDoc(
                kind="option", names=("--agent",), type="str", envvar="FORGE_AGENT", help="Id."
            ),
        ),
        subcommands=(
            D.CommandDoc(
                path=("forge", "zeta"),
                usage="forge zeta [OPTIONS] PR",
                help="\x1b[1mZeta\x1b[0m help.\fhidden tail",
                params=(
                    D.ParamDoc(kind="argument", names=("PR",), type="integer", required=True),
                    D.ParamDoc(
                        kind="option",
                        names=("--mode",),
                        type="choice",
                        default="fast",
                        choices=("fast", "slow"),
                        help="Mode a|b.",
                    ),
                ),
            ),
            D.CommandDoc(
                path=("forge", "alpha"),
                usage="forge alpha [OPTIONS] COMMAND [ARGS]...",
                subcommands=(D.CommandDoc(path=("forge", "alpha", "show"), usage="u"),),
            ),
        ),
    ),
    after_subcommand=("--json", "--agent"),
)


def render() -> str:
    return D.render_reference(CLI, [parse_prompt(PROMPT, Path(".forge/prompts/next.md"))])


def test_render_is_deterministic_and_marked_generated() -> None:
    text = render()
    assert text == render()
    assert D.GENERATED_MARKER in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_commands_sorted_alphabetically_by_path() -> None:
    text = render()
    order = [text.index(f"### `{p}`") for p in ("forge alpha", "forge alpha show", "forge zeta")]
    assert order == sorted(order)
    assert [c.path for c in D.walk(CLI.root)] == [
        ("forge", "alpha"),
        ("forge", "alpha", "show"),
        ("forge", "zeta"),
    ]


def test_params_tables_global_options_and_prompts() -> None:
    text = render()
    assert "`--json`, `--agent` are also accepted after it" in text
    assert "| `--agent` | str | no | - | Id. Env: `FORGE_AGENT`. |" in text
    assert "| `PR` | integer | yes | - | - |" in text
    assert "| `--mode` | choice | no | `fast` | Mode a\\|b. Choices: `fast`, `slow`. |" in text
    assert "| `next` | Pick \\| claim a task | `[task-id]` | dev |" in text
    assert "- [`forge alpha show`](#forge-alpha-show)" in text


def test_help_is_cleaned() -> None:
    text = render()
    assert "\x1b" not in text
    assert "Zeta help." in text and "hidden tail" not in text
    assert D.clean_text(None) == ""
    assert D.clean_text("  a  \n b \n") == "a\n b"


def test_no_prompts() -> None:
    assert "No prompts found." in D.render_reference(CLI, [])


def test_load_prompts_dir(tmp_path: Path) -> None:
    with pytest.raises(PreconditionError) as err:
        D.load_prompts_dir(tmp_path, Path(".forge/prompts"))
    assert err.value.code == ExitCode.PRECONDITION
    assert str(tmp_path / ".forge/prompts") in err.value.message
    assert err.value.hint and "--prompts" in err.value.hint

    directory = tmp_path / "p"
    directory.mkdir()
    (directory / "next.md").write_text(PROMPT)
    (directory / "notes.txt").write_text("ignored")
    prompts = D.load_prompts_dir(tmp_path, Path("p"))
    assert [p.name for p in prompts] == ["next"]
    assert prompts[0].source == Path("p/next.md")
    assert [p.name for p in D.load_prompts_dir(tmp_path, directory)] == ["next"]

    (directory / "bad.md").write_text("no frontmatter\n")
    with pytest.raises(ConfigError):
        D.load_prompts_dir(tmp_path, Path("p"))


def test_check_and_write(tmp_path: Path) -> None:
    target = tmp_path / "docs/forge/commands.md"
    text = render()
    assert D.check_reference(target, text) == [f"{target.as_posix()} is missing"]
    assert D.write_reference(target, text) is True
    assert D.write_reference(target, text) is False
    assert D.check_reference(target, text) == []

    target.write_text(text.replace("Root help.", "Changed."))
    diff = D.check_reference(target, text)
    assert "-Changed." in diff and "+Root help." in diff

    # A Windows checkout with core.autocrlf=true has CRLF: not drift (ADR 0021).
    target.write_bytes(text.replace("\n", "\r\n").encode())
    assert D.check_reference(target, text) == []
    assert D.write_reference(target, text) is False  # left as checked out

    target.write_bytes(text.rstrip("\n").encode())  # no final newline
    assert D.check_reference(target, text, label=Path("ref.md")) == [
        "ref.md differs in whitespace or line endings"
    ]


def test_diff_summary_is_truncated() -> None:
    current = "\n".join(f"a{i}" for i in range(50))
    expected = "\n".join(f"b{i}" for i in range(50))
    lines = D.diff_summary(Path("x.md"), current, expected)
    assert len(lines) == D.DIFF_LINES + 1
    assert lines[-1].startswith("... (")


def test_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(D, "_provider", None)
    with pytest.raises(ForgeError):
        D.registered_cli()
    D.register_cli(lambda: CLI)
    assert D.registered_cli() is CLI
