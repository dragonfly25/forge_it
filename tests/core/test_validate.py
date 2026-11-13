"""Tests for forge.core.validate (YAML loading and typed fields)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge.core.errors import ConfigError
from forge.core.tiers import Effort, Tier
from forge.core.validate import Fields, load_yaml_mapping

F = "cfg.yml"


def fields(data: dict[str, Any], prefix: str = "") -> Fields:
    return Fields(data, F, prefix)


def error_of(fn: Any, *args: Any, **kwargs: Any) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        fn(*args, **kwargs)
    assert info.value.file == F or info.value.file.endswith(".yml")
    return info.value


# -- load_yaml_mapping -----------------------------------------------------------------------


def test_load_valid_mapping(tmp_path: Path) -> None:
    p = tmp_path / "c.yml"
    p.write_text("a: 1\nb: [x]\n", encoding="utf-8")
    assert load_yaml_mapping(p) == {"a": 1, "b": ["x"]}


def test_load_missing_file_names_create_command(tmp_path: Path) -> None:
    p = tmp_path / "config.yml"
    err = error_of(load_yaml_mapping, p, create_hint="forge init")
    assert err.key is None
    assert err.file == str(p)
    assert "not found" in err.problem
    assert err.hint is not None and "forge init" in err.hint


def test_load_syntax_error_reports_line(tmp_path: Path) -> None:
    p = tmp_path / "c.yml"
    p.write_text("a: 1\nb: [unclosed\nc: 3\n", encoding="utf-8")
    err = error_of(load_yaml_mapping, p)
    assert "invalid YAML" in err.problem
    assert "line " in err.problem and "column " in err.problem


def test_load_syntax_error_line_number_is_one_based(tmp_path: Path) -> None:
    p = tmp_path / "c.yml"
    p.write_text("a: 1\nb: 2\n  c: 3\n", encoding="utf-8")
    err = error_of(load_yaml_mapping, p)
    assert "line 3" in err.problem


@pytest.mark.parametrize("content", ["", "\n", "# only a comment\n"])
def test_load_empty_file(tmp_path: Path, content: str) -> None:
    p = tmp_path / "c.yml"
    p.write_text(content, encoding="utf-8")
    assert "empty" in error_of(load_yaml_mapping, p).problem


@pytest.mark.parametrize(("content", "kind"), [("- a\n- b\n", "list"), ("hello\n", "string")])
def test_load_non_mapping_top_level(tmp_path: Path, content: str, kind: str) -> None:
    p = tmp_path / "c.yml"
    p.write_text(content, encoding="utf-8")
    err = error_of(load_yaml_mapping, p)
    assert "mapping" in err.problem and kind in err.problem


# -- str_ -------------------------------------------------------------------------------------


def test_str_happy_and_default() -> None:
    f = fields({"name": "x"})
    assert f.str_("name") == "x"
    assert f.str_("other", required=False) is None
    assert f.str_("other", required=False, default="d") == "d"
    assert f.str_("other", default="d") == "d"


def test_str_missing_required() -> None:
    err = error_of(fields({}).str_, "name")
    assert err.key == "name" and "missing" in err.problem
    assert err.hint is not None and "name:" in err.hint


def test_str_null_is_reported_as_empty() -> None:
    err = error_of(fields({"name": None}).str_, "name")
    assert err.key == "name" and err.problem == "is empty (null)"
    assert err.hint is not None and "`name:" in err.hint


def test_mapping_null_is_reported_as_empty() -> None:
    err = error_of(fields({"m": None}).mapping, "m", required=True)
    assert err.key == "m" and err.problem == "is empty (null)"


def test_nested_wrong_type_fix_is_pasteable_yaml() -> None:
    err = error_of(fields({"tiers": {"strong": 3}}, "models").str_map, "tiers")
    assert err.key == "models.tiers.strong"
    assert err.hint == 'write it as `strong: "3"` under `tiers:`'
    assert "tiers.strong:" not in err.hint


def test_str_wrong_type() -> None:
    err = error_of(fields({"name": 3}).str_, "name")
    assert "expected a string, got integer" in err.problem
    assert err.hint is not None and 'name: "3"' in err.hint


def test_str_pattern() -> None:
    f = fields({"id": "Bad Id"})
    err = error_of(f.str_, "id", pattern=r"[a-z]+")
    assert "does not match" in err.problem
    assert fields({"id": "ok"}).str_("id", pattern=r"[a-z]+") == "ok"


# -- int_ / bool_ -----------------------------------------------------------------------------


def test_int_happy_default_and_min() -> None:
    assert fields({"n": 5}).int_("n", default=1) == 5
    assert fields({}).int_("n", default=1) == 1
    err = error_of(fields({"n": 0}).int_, "n", default=1, min=1)
    assert ">= 1" in err.problem


@pytest.mark.parametrize(("value", "kind"), [(True, "boolean"), ("5", "string"), (1.5, "number")])
def test_int_wrong_type(value: object, kind: str) -> None:
    err = error_of(fields({"n": value}).int_, "n", default=1)
    assert f"expected an integer, got {kind}" in err.problem
    assert err.hint is not None and "n: 1" in err.hint


def test_bool_happy_and_default() -> None:
    assert fields({"b": False}).bool_("b", default=True) is False
    assert fields({}).bool_("b", default=True) is True


@pytest.mark.parametrize(("value", "kind"), [(1, "integer"), ("yes", "string")])
def test_bool_wrong_type(value: object, kind: str) -> None:
    err = error_of(fields({"b": value}).bool_, "b", default=False)
    assert f"got {kind}" in err.problem and "boolean" in err.problem
    assert err.hint is not None and "b: true" in err.hint


# -- str_list ---------------------------------------------------------------------------------


def test_str_list_happy_and_default() -> None:
    assert fields({"l": ["a", "b"]}).str_list("l") == ["a", "b"]
    assert fields({}).str_list("l") == []
    default = ["x"]
    got = fields({}).str_list("l", default=default)
    assert got == ["x"] and got is not default


def test_str_list_wrong_type_and_bad_item() -> None:
    err = error_of(fields({"l": "a"}).str_list, "l")
    assert "expected a list of strings, got string" in err.problem
    err = error_of(fields({"l": ["a", 2]}, "zones.ui").str_list, "l")
    assert err.key == "zones.ui.l[1]"


def test_str_list_non_empty() -> None:
    err = error_of(fields({"l": []}).str_list, "l", non_empty=True)
    assert "non-empty" in err.problem
    assert "non-empty" in error_of(fields({}).str_list, "l", non_empty=True).problem


# -- mapping / list_of_mappings ---------------------------------------------------------------


def test_mapping_nested_key_paths() -> None:
    f = fields({"models": {"tiers": {"strong": 3}}})
    inner = f.mapping("models").mapping("tiers")
    assert inner.prefix == "models.tiers"
    err = error_of(inner.str_, "strong")
    assert err.key == "models.tiers.strong"
    assert str(err).startswith(f"{F}: models.tiers.strong: ")


def test_mapping_missing_and_wrong_type() -> None:
    assert fields({}).mapping("m").data == {}
    err = error_of(fields({}).mapping, "m", required=True)
    assert err.key == "m" and "missing" in err.problem
    err = error_of(fields({"m": [1]}).mapping, "m")
    assert "expected a mapping, got list" in err.problem


def test_list_of_mappings_indices() -> None:
    f = fields({"members": [{"github": "a"}, {"github": "b"}, {"github": 7}]})
    items = f.list_of_mappings("members")
    assert [i.prefix for i in items] == ["members[0]", "members[1]", "members[2]"]
    err = error_of(items[2].str_, "github")
    assert err.key == "members[2].github"


def test_list_of_mappings_missing_wrong_type_and_bad_item() -> None:
    assert fields({}).list_of_mappings("members") == []
    err = error_of(fields({"members": {"a": 1}}).list_of_mappings, "members")
    assert "expected a list of mappings, got mapping" in err.problem
    err = error_of(fields({"members": [{"a": 1}, "x"]}).list_of_mappings, "members")
    assert err.key == "members[1]" and "got string" in err.problem


# -- choice / enum / str_map ------------------------------------------------------------------


def test_choice() -> None:
    assert fields({"u": "error"}).choice("u", ["warn", "error"], default="warn") == "error"
    assert fields({}).choice("u", ["warn", "error"], default="warn") == "warn"
    err = error_of(fields({"u": "eror"}).choice, "u", ["warn", "error"], default="warn")
    assert "warn, error" in err.problem
    assert err.hint is not None and "u: error" in err.hint
    err = error_of(fields({"u": 3}).choice, "u", ["warn", "error"], default="warn")
    assert "expected one of" in err.problem


def test_enum_with_tiers() -> None:
    assert fields({"t": "strong"}).enum("t", Tier, default=Tier.STANDARD) is Tier.STRONG
    assert fields({}).enum("e", Effort, default=Effort.LOW) is Effort.LOW
    err = error_of(fields({"t": "strng"}).enum, "t", Tier, default=Tier.STANDARD)
    assert "light, standard, strong" in err.problem
    assert err.hint is not None and "t: strong" in err.hint


def test_str_map() -> None:
    f = fields({"tiers": {"strong": "opus", "light": "haiku"}})
    assert f.str_map("tiers", key_enum=Tier) == {"strong": "opus", "light": "haiku"}
    assert fields({}).str_map("tiers") == {}
    assert fields({"m": {"anything": "x"}}).str_map("m") == {"anything": "x"}


def test_str_map_errors() -> None:
    err = error_of(fields({"tiers": ["a"]}, "agents[0]").str_map, "tiers")
    assert err.key == "agents[0].tiers" and "got list" in err.problem
    err = error_of(fields({"tiers": {"strog": "x"}}).str_map, "tiers", key_enum=Tier)
    assert err.key == "tiers.strog"
    assert err.hint is not None and "strong" in err.hint
    err = error_of(fields({"tiers": {"strong": 1}}).str_map, "tiers")
    assert err.key == "tiers.strong" and "expected a string, got integer" in err.problem


# -- raw / reject_unknown ---------------------------------------------------------------------


def test_raw() -> None:
    f = fields({"x": [1, {"y": 2}]})
    assert f.raw("x") == [1, {"y": 2}]
    assert f.raw("missing") is None


def test_reject_unknown_with_suggestion() -> None:
    f = fields({"github": "a", "rolse": ["dev"]}, "members[0]")
    err = error_of(f.reject_unknown, ["github", "roles", "name"])
    assert err.key == "members[0].rolse" and "unknown key" in err.problem
    assert err.hint is not None and "did you mean `roles`" in err.hint


def test_reject_unknown_without_suggestion_lists_allowed() -> None:
    err = error_of(fields({"zzzz": 1}).reject_unknown, ["github", "roles"])
    assert err.hint is not None and "github, roles" in err.hint


def test_reject_unknown_passes_when_all_known() -> None:
    fields({"github": "a"}).reject_unknown(["github", "roles"])


def test_fields_name_relative_files_with_slashes_on_every_platform(tmp_path: Path) -> None:
    """Messages and JSON show `.forge/x.yml` on Windows too; absolute paths stay native."""
    relative = Fields({}, Path(".forge") / "config.yml")
    assert relative.file == ".forge/config.yml"
    assert relative.fail("k", "bad", "fix it").file == ".forge/config.yml"
    absolute = Fields({}, tmp_path / "config.yml")
    assert absolute.file == str(tmp_path / "config.yml")
