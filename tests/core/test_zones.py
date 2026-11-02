"""Tests for forge.core.zones (zones.yml validation, glob matching, overlap)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from forge.core.errors import ConfigError, ForgeError
from forge.core.zones import (
    ZONES_PATH,
    OverlapKind,
    Zone,
    ZonesConfig,
    compile_glob,
    glob_matches,
    globs_may_intersect,
    load_zones,
    normalize_glob,
    parse_zones,
)

F = "zones.yml"

SPEC_EXAMPLE = """\
zones:
  editor/canvas:
    paths: ["packages/editor/src/canvas/**"]
  editor/blocks:
    paths: ["packages/editor/src/blocks/**"]
  editor/inspector:
    paths: ["packages/editor/src/inspector/**"]
  contracts/api:
    paths: ["contracts/openapi/**"]
    contract: true
  db/schema:
    paths: ["packages/wp-plugin/migrations/**"]
    contract: true
conflicts:
  - [editor/blocks, editor/inspector]
unzoned: warn
"""


def write_zones(root: Path, text: str) -> Path:
    path = root / ZONES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def config_error(data: dict[str, Any]) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        parse_zones(data, F)
    assert info.value.file == F
    return info.value


def make(zones: dict[str, list[str]], conflicts: list[list[str]] | None = None) -> ZonesConfig:
    data: dict[str, Any] = {"zones": {k: {"paths": v} for k, v in zones.items()}}
    if conflicts is not None:
        data["conflicts"] = conflicts
    return parse_zones(data, F)


@pytest.fixture
def spec_zones(tmp_path: Path) -> ZonesConfig:
    write_zones(tmp_path, SPEC_EXAMPLE)
    return load_zones(tmp_path)


# -- matching --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("glob", "path", "expected"),
    [
        # ** at the start
        ("**/x.py", "x.py", True),
        ("**/x.py", "a/x.py", True),
        ("**/x.py", "a/b/c/x.py", True),
        ("**/x.py", "a/yx.py", False),
        ("**", "anything/at/all.txt", True),
        ("**", "file", True),
        # ** in the middle
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/x/b", True),
        ("a/**/b", "a/x/y/b", True),
        ("a/**/b", "a/xb", False),
        ("a/**/b", "ab", False),
        ("a/**/b", "a/b/c", False),
        # ** at the end: everything under a/, but not the file `a`
        ("a/**", "a/x", True),
        ("a/**", "a/x/y", True),
        ("a/**", "a", False),
        ("a/**", "ab/x", False),
        ("a/**", "b/a/x", False),
        # ** inside a segment behaves like *
        ("a**b", "axyb", True),
        ("a**b", "ax/yb", False),
        # *
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/.py", True),
        ("src/*.py", "src/a/b.py", False),
        ("*.py", "a.py", True),
        ("*.py", "src/a.py", False),
        ("src/*", "src/a", True),
        ("src/*", "src/a/b", False),
        ("src*/x", "src-old/x", True),
        # ?
        ("a?c", "abc", True),
        ("a?c", "ac", False),
        ("a?c", "a/c", False),
        ("a?c", "abbc", False),
        # classes
        ("v[0-9].txt", "v3.txt", True),
        ("v[0-9].txt", "vx.txt", False),
        ("v[!0-9].txt", "vx.txt", True),
        ("v[!0-9].txt", "v3.txt", False),
        ("v[^0-9].txt", "vx.txt", True),
        ("a[!x]b", "a/b", False),
        ("[]]x", "]x", True),
        ("a[bc", "a[bc", True),  # unterminated class is literal
        # dots are literal
        ("a.b", "a.b", True),
        ("a.b", "axb", False),
        (".github/**", ".github/workflows/ci.yml", True),
        # special regex characters are literal
        ("a+b(c)$/{x}|^y", "a+b(c)$/{x}|^y", True),
        ("a+b", "aab", False),
        ("a\\b", "a\\b", True),
        # literal paths are exact
        ("src/main.py", "src/main.py", True),
        ("src/main.py", "src/main.pyc", False),
        ("src/main.py", "x/src/main.py", False),
        # leading ./ is ignored
        ("./src/*.py", "src/a.py", True),
        ("src/*.py", "./src/a.py", True),
    ],
)
def test_glob_matches(glob: str, path: str, expected: bool) -> None:
    assert glob_matches(glob, path) is expected


def test_compile_glob_is_cached() -> None:
    assert compile_glob("a/**") is compile_glob("a/**")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("./a/**", "a/**"), ("././a", "a"), ("  a/b  ", "a/b"), ("a/.hidden", "a/.hidden")],
)
def test_normalize_glob_ok(raw: str, expected: str) -> None:
    assert normalize_glob(raw) == expected


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("", "empty"),
        ("./", "empty"),
        ("/etc/**", "absolute"),
        ("a/../b", ".."),
        ("../a", ".."),
        ("a/./b", "`.`"),
        ("a//b", "empty path segment"),
        ("a/", "empty path segment"),
    ],
)
def test_normalize_glob_rejects(raw: str, problem: str) -> None:
    with pytest.raises(ValueError, match=re.escape(problem)):
        normalize_glob(raw)


# -- loading ---------------------------------------------------------------------------------


def test_load_spec_example(spec_zones: ZonesConfig) -> None:
    assert set(spec_zones.zones) == {
        "editor/canvas",
        "editor/blocks",
        "editor/inspector",
        "contracts/api",
        "db/schema",
    }
    assert spec_zones.zones["contracts/api"] == Zone(
        "contracts/api", ("contracts/openapi/**",), True
    )
    assert spec_zones.zones["editor/canvas"].contract is False
    assert spec_zones.conflicts == frozenset({frozenset({"editor/blocks", "editor/inspector"})})
    assert spec_zones.unzoned == "warn"
    assert spec_zones.contract_zones() == {"contracts/api", "db/schema"}


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_zones(tmp_path)
    assert info.value.file.endswith("zones.yml")
    assert info.value.hint is not None and "forge init" in info.value.hint


def test_defaults_and_normalization() -> None:
    cfg = parse_zones({"zones": {"a": {"paths": ["./src/**", "src/**", "lib/x.py"]}}}, F)
    assert cfg.zones["a"].paths == ("src/**", "lib/x.py")
    assert cfg.conflicts == frozenset()
    assert cfg.unzoned == "warn"
    assert parse_zones({"zones": {}, "unzoned": "error"}, F).unzoned == "error"


@pytest.mark.parametrize(
    ("data", "key", "problem"),
    [
        ({"zones": {}, "extra": 1}, "extra", "unknown key"),
        ({}, "zones", "required key is missing"),
        ({"zones": ["a"]}, "zones", "expected a mapping"),
        ({"zones": {"Editor": {"paths": ["a"]}}}, "zones.Editor", "invalid zone name"),
        ({"zones": {"a//b": {"paths": ["a"]}}}, "zones.a//b", "invalid zone name"),
        ({"zones": {"-a": {"paths": ["a"]}}}, "zones.-a", "invalid zone name"),
        ({"zones": {"a/": {"paths": ["a"]}}}, "zones.a/", "invalid zone name"),
        ({"zones": {"a": None}}, "zones.a", "required key is missing"),
        ({"zones": {"a": "x/**"}}, "zones.a", "expected a mapping"),
        (
            {"zones": {"editor/canvas": {"paths": ["a"], "path": 1}}},
            "zones.editor/canvas.path",
            "unknown key",
        ),
        ({"zones": {"editor/canvas": {}}}, "zones.editor/canvas.paths", "non-empty"),
        ({"zones": {"editor/canvas": {"paths": []}}}, "zones.editor/canvas.paths", "non-empty"),
        (
            {"zones": {"editor/canvas": {"paths": "src/**"}}},
            "zones.editor/canvas.paths",
            "expected a list of strings",
        ),
        (
            {"zones": {"editor/canvas": {"paths": ["ok/**", 3]}}},
            "zones.editor/canvas.paths[1]",
            "expected a string",
        ),
        (
            {"zones": {"editor/canvas": {"paths": ["/abs/**"]}}},
            "zones.editor/canvas.paths[0]",
            "absolute",
        ),
        (
            {"zones": {"editor/canvas": {"paths": ["a/../b"]}}},
            "zones.editor/canvas.paths[0]",
            "`..`",
        ),
        (
            {"zones": {"editor/canvas": {"paths": ["a", "a//b"]}}},
            "zones.editor/canvas.paths[1]",
            "empty path segment",
        ),
        (
            {"zones": {"editor/canvas": {"paths": ["a"], "contract": "yes"}}},
            "zones.editor/canvas.contract",
            "expected a boolean",
        ),
        ({"zones": {"a": {"paths": ["a"]}}, "conflicts": "a"}, "conflicts", "list of zone pairs"),
        (
            {"zones": {"a": {"paths": ["a"]}}, "conflicts": [["a"]]},
            "conflicts[0]",
            "list of 1 items",
        ),
        (
            {"zones": {"a": {"paths": ["a"]}}, "conflicts": ["a"]},
            "conflicts[0]",
            "pair of zone names, got string",
        ),
        (
            {"zones": {"a": {"paths": ["a"]}, "b": {"paths": ["b"]}}, "conflicts": [["a", 1]]},
            "conflicts[0][1]",
            "expected a zone name",
        ),
        (
            {"zones": {"a": {"paths": ["a"]}}, "conflicts": [["a", "nope"]]},
            "conflicts[0][1]",
            "unknown zone 'nope'",
        ),
        (
            {"zones": {"a": {"paths": ["a"]}}, "conflicts": [["a", "a"]]},
            "conflicts[0]",
            "cannot conflict with itself",
        ),
        ({"zones": {}, "unzoned": "ignore"}, "unzoned", "invalid value"),
    ],
)
def test_validation_errors(data: dict[str, Any], key: str, problem: str) -> None:
    err = config_error(data)
    assert err.key == key
    assert problem in err.problem
    assert err.hint


def test_unknown_conflict_zone_suggests_close_name() -> None:
    err = config_error(
        {
            "zones": {"editor/blocks": {"paths": ["a"]}, "editor/canvas": {"paths": ["b"]}},
            "conflicts": [["editor/blocks", "editor/canvs"]],
        }
    )
    assert err.hint is not None and "editor/canvas" in err.hint


def test_error_text_names_file_key_and_fix(tmp_path: Path) -> None:
    write_zones(tmp_path, "zones:\n  editor/canvas:\n    paths: []\n")
    with pytest.raises(ConfigError) as info:
        load_zones(tmp_path)
    text = str(info.value)
    assert "zones.yml: zones.editor/canvas.paths:" in text
    assert "Fix:" in text


# -- path lookup -----------------------------------------------------------------------------


def test_zones_for_path_with_overlapping_zones() -> None:
    cfg = make(
        {
            "web": ["packages/web/**"],
            "web/components": ["packages/web/src/components/**"],
            "styles": ["**/*.css"],
        }
    )
    assert cfg.zones_for_path("packages/web/src/components/a.css") == [
        "styles",
        "web",
        "web/components",
    ]
    assert cfg.zones_for_path("packages/web/index.ts") == ["web"]
    assert cfg.zones_for_path("./packages/web/index.ts") == ["web"]
    assert cfg.zones_for_path("README.md") == []


def test_unzoned_paths(spec_zones: ZonesConfig) -> None:
    paths = [
        "packages/editor/src/canvas/a.ts",
        "README.md",
        "contracts/openapi/api.yml",
        "packages/editor/package.json",
        "README.md",
    ]
    assert spec_zones.unzoned_paths(paths) == ["README.md", "packages/editor/package.json"]
    assert spec_zones.unzoned_paths([]) == []


def test_files_outside(spec_zones: ZonesConfig) -> None:
    paths = [
        "packages/editor/src/canvas/a.ts",
        "packages/editor/src/blocks/b.ts",
        "contracts/openapi/api.yml",
        "README.md",
    ]
    assert spec_zones.files_outside(paths, ["editor/canvas", "contracts/api"]) == [
        "README.md",
        "packages/editor/src/blocks/b.ts",
    ]
    assert spec_zones.files_outside(paths, []) == sorted(paths)


def test_files_outside_unknown_zone(spec_zones: ZonesConfig) -> None:
    with pytest.raises(ForgeError) as info:
        spec_zones.files_outside(["a"], ["editor/nope"])
    assert "editor/nope" in info.value.message
    assert ".forge/zones.yml" in info.value.message
    assert info.value.hint == "add it to .forge/zones.yml or fix the task's zones"


# -- intersection ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        # divergent literal prefixes → disjoint
        ("packages/editor/**", "packages/api/**", False),
        ("packages/editor/src/canvas/**", "packages/editor/src/blocks/**", False),
        ("docs/**", "src/**", False),
        ("a/*.py", "b/*.py", False),
        # same prefix with wildcards → intersect
        ("packages/editor/**", "packages/editor/src/**", True),
        ("src/**", "src/*.py", True),
        ("src/**", "src/**", True),
        # wildcard segment before any divergence → intersect
        ("packages/*/src/**", "packages/editor/src/x.ts", True),
        ("packages/*/src/**", "packages/api/test/**", True),
        ("**/*.css", "packages/web/**", True),
        ("src*/**", "lib/**", True),  # `src*` is not literal
        ("v[12]/**", "v3/**", True),
        ("a?/**", "b/**", True),
        # literal vs literal
        ("src/main.py", "src/main.py", True),
        ("src/main.py", "src/other.py", False),
        ("a/b", "a/b/c", False),
        # documented false positives (ADR 0006)
        ("src/*.py", "src/*.ts", True),
        ("a", "a/b/**", True),
        # leading ./ is ignored
        ("./src/**", "src/x", True),
    ],
)
def test_globs_may_intersect(a: str, b: str, expected: bool) -> None:
    assert globs_may_intersect(a, b) is expected
    assert globs_may_intersect(b, a) is expected


def test_intersection_never_misses_a_real_match() -> None:
    """Sanity: whenever both globs match a common path, the test says they intersect."""
    globs = [
        "packages/editor/**",
        "packages/*/src/**",
        "**/*.ts",
        "packages/editor/src/x.ts",
        "packages/api/**",
        "src/*.py",
    ]
    paths = ["packages/editor/src/x.ts", "packages/api/src/y.ts", "src/a.py", "x.ts"]
    for a in globs:
        for b in globs:
            if any(glob_matches(a, p) and glob_matches(b, p) for p in paths):
                assert globs_may_intersect(a, b), (a, b)


# -- overlap ---------------------------------------------------------------------------------


def test_overlap_shared(spec_zones: ZonesConfig) -> None:
    ov = spec_zones.overlap(["editor/canvas", "contracts/api"], ["contracts/api"])
    assert ov is not None
    assert ov.kind is OverlapKind.SHARED
    assert (ov.zone_a, ov.zone_b) == ("contracts/api", "contracts/api")
    assert ov.detail == "shared zone contracts/api"


def test_overlap_declared_conflict_spec_example(spec_zones: ZonesConfig) -> None:
    ov = spec_zones.overlap(["editor/blocks"], ["editor/inspector"])
    assert ov is not None
    assert ov.kind is OverlapKind.DECLARED_CONFLICT
    assert (ov.zone_a, ov.zone_b) == ("editor/blocks", "editor/inspector")
    assert ov.detail == "declared conflict editor/blocks↔editor/inspector"
    reverse = spec_zones.overlap(["editor/inspector"], ["editor/blocks"])
    assert reverse is not None and reverse.kind is OverlapKind.DECLARED_CONFLICT


def test_no_overlap_in_spec_example(spec_zones: ZonesConfig) -> None:
    assert spec_zones.overlap(["editor/canvas"], ["editor/blocks"]) is None
    assert spec_zones.overlap(["editor/canvas"], ["db/schema", "contracts/api"]) is None
    assert spec_zones.overlap([], ["editor/canvas"]) is None
    assert spec_zones.all_overlaps(["editor/canvas"], ["editor/blocks"]) == []


def test_overlap_glob_intersection() -> None:
    cfg = make({"web": ["packages/web/**", "shared/**"], "styles": ["**/*.css"]})
    ov = cfg.overlap(["web"], ["styles"])
    assert ov is not None
    assert ov.kind is OverlapKind.GLOB_INTERSECTION
    assert (ov.zone_a, ov.zone_b) == ("web", "styles")
    assert ov.detail == "globs intersect: packages/web/** ~ **/*.css"


def test_overlap_priority_and_all_overlaps() -> None:
    cfg = make(
        {
            "a": ["src/a/**"],
            "b": ["src/b/**"],
            "c": ["src/**"],
        },
        conflicts=[["a", "b"]],
    )
    reasons = cfg.all_overlaps(["a", "c"], ["a", "b"])
    assert [(r.kind, r.zone_a, r.zone_b) for r in reasons] == [
        (OverlapKind.SHARED, "a", "a"),
        (OverlapKind.DECLARED_CONFLICT, "a", "b"),
        (OverlapKind.GLOB_INTERSECTION, "c", "a"),
        (OverlapKind.GLOB_INTERSECTION, "c", "b"),
    ]
    first = cfg.overlap(["a", "c"], ["a", "b"])
    assert first == reasons[0]
    declared = cfg.overlap(["a"], ["b"])
    assert declared is not None and declared.kind is OverlapKind.DECLARED_CONFLICT
    assert [r.kind for r in cfg.all_overlaps(["c"], ["b"])] == [OverlapKind.GLOB_INTERSECTION]


def test_overlap_unknown_zone(spec_zones: ZonesConfig) -> None:
    for a, b in ((["editor/nope"], ["editor/canvas"]), (["editor/canvas"], ["nope"])):
        with pytest.raises(ForgeError) as info:
            spec_zones.overlap(a, b)
        assert "nope" in info.value.message
        assert info.value.hint == "add it to .forge/zones.yml or fix the task's zones"
