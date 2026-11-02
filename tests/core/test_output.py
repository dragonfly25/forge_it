"""Tests for forge.core.output (ADR 0004 envelope)."""

from __future__ import annotations

import json

import pytest

from forge.core.errors import ConfigError, ExitCode, ForgeError, PreconditionError
from forge.core.output import Level, Message, Result, dumps, error_result, to_envelope


def test_default_result_is_ok_and_empty() -> None:
    result = Result()
    assert result.ok
    assert to_envelope(result) == {"ok": True, "code": 0, "data": {}, "messages": []}


def test_defaults_are_not_shared() -> None:
    a, b = Result(), Result()
    a.data["x"] = 1
    a.info("hello")
    assert b.data == {} and b.messages == []


def test_helpers_append_messages_in_order() -> None:
    result = Result()
    result.info("one")
    result.warn("two", hint="do this")
    result.error("three")
    assert [m.level for m in result.messages] == [Level.INFO, Level.WARNING, Level.ERROR]
    assert result.messages[1] == Message(Level.WARNING, "two", "do this")
    assert result.ok  # messages never change the code


def test_message_is_frozen() -> None:
    msg = Message(Level.INFO, "x")
    with pytest.raises(AttributeError):
        msg.text = "y"  # type: ignore[misc]


def test_envelope_exact_shape_and_types() -> None:
    result = Result(code=ExitCode.NOTHING_AVAILABLE, data={"n": 1})
    result.warn("careful", hint="fix it")
    env = to_envelope(result)
    assert list(env) == ["ok", "code", "data", "messages"]
    assert env["ok"] is False
    assert env["code"] == 3 and type(env["code"]) is int
    assert env["messages"] == [{"level": "warning", "text": "careful", "hint": "fix it"}]
    assert type(env["messages"][0]["level"]) is str


def test_error_result_from_forge_error() -> None:
    result = error_result(ForgeError("boom", hint="retry"))
    assert result.code == ExitCode.ERROR and not result.ok
    assert result.data == {}
    assert result.messages == [Message(Level.ERROR, "boom", "retry")]


def test_error_result_keeps_subclass_code() -> None:
    assert error_result(PreconditionError("dirty")).code == ExitCode.PRECONDITION
    assert error_result(ForgeError("x", code=ExitCode.ESCALATION)).code == 4


def test_error_result_from_config_error() -> None:
    result = error_result(ConfigError("team.yml", "members[0].github", "missing", "add it"))
    assert result.messages[0].text == "team.yml: members[0].github: missing"
    assert result.messages[0].hint == "add it"


def test_dumps_is_stable_json() -> None:
    result = Result(data={"z": 1, "a": "caffè"})
    text = dumps(result)
    assert json.loads(text) == to_envelope(result)
    assert "caffè" in text  # ensure_ascii=False
    assert text.index('"z"') < text.index('"a"')  # sort_keys=False
    assert all(line == line.rstrip() for line in text.splitlines())
    assert dumps(result) == text
