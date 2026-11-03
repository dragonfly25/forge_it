"""Tests for Forge-managed regions (ADR 0014)."""

from __future__ import annotations

import pytest

from forge.core.errors import ConfigError
from forge.core.managed import (
    BEGIN,
    END,
    find_region,
    newline_of,
    region_inner,
    render_region,
    upsert_region,
)


def test_new_file_gets_only_the_region() -> None:
    assert upsert_region(None, "hello\n") == f"{BEGIN}\nhello\n{END}\n"
    assert upsert_region("", "hello") == f"{BEGIN}\nhello\n{END}\n"


def test_region_appended_after_human_text() -> None:
    human = "# My project\n\nOur own notes.\n"
    out = upsert_region(human, "generated")
    assert out.startswith(human)
    assert out == human + f"\n{BEGIN}\ngenerated\n{END}\n"


def test_append_when_file_has_no_trailing_newline() -> None:
    out = upsert_region("notes", "gen")
    assert out == f"notes\n\n{BEGIN}\ngen\n{END}\n"


def test_replace_preserves_text_outside_byte_for_byte() -> None:
    before = "  leading  \n\n# Title\t\n"
    after = "\n\ntrailing   text\n\n\n"
    text = f"{before}{BEGIN}\nold content\n{END}{after}"
    out = upsert_region(text, "new content\n")
    assert out == f"{before}{BEGIN}\nnew content\n{END}{after}"
    region = find_region(out)
    assert region is not None
    assert out[: region.start] == before
    assert out[region.end :] == after


def test_crlf_file_keeps_crlf_everywhere() -> None:
    before = "# Title\r\n\r\nHuman line\r\n"
    after = "\r\nMore human text\r\n"
    text = f"{before}{BEGIN}\r\nold\r\n{END}{after}"
    out = upsert_region(text, "line one\nline two\n")
    assert out == f"{before}{BEGIN}\r\nline one\r\nline two\r\n{END}{after}"
    assert "\n" not in out.replace("\r\n", "")


def test_crlf_append() -> None:
    out = upsert_region("a\r\nb\r\n", "x")
    assert out == f"a\r\nb\r\n\r\n{BEGIN}\r\nx\r\n{END}\r\n"


def test_upsert_is_idempotent() -> None:
    once = upsert_region("human\n", "gen\nmore\n")
    assert upsert_region(once, "gen\nmore\n") == once


def test_region_inner_and_newline_of() -> None:
    assert region_inner("no region") is None
    assert region_inner(f"x\n{BEGIN}\nabc\n{END}\n") == "\nabc\n"
    assert newline_of("a\r\nb") == "\r\n"
    assert newline_of("a\nb") == "\n"
    assert render_region("") == f"{BEGIN}\n{END}"


@pytest.mark.parametrize(
    "text",
    [
        f"{BEGIN}\nno end\n",
        f"no begin\n{END}\n",
        f"{END}\n{BEGIN}\n",
        f"{BEGIN}\n{END}\n{BEGIN}\n{END}\n",
    ],
)
def test_malformed_regions_name_the_file(text: str) -> None:
    with pytest.raises(ConfigError) as info:
        upsert_region(text, "x", "AGENTS.md")
    assert info.value.file == "AGENTS.md"
    assert info.value.hint and BEGIN in info.value.hint
