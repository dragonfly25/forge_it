"""Tests for forge.core.team (`.forge/team.yml`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.core.errors import ConfigError
from forge.core.team import TEAM_PATH, Member, load_team

SPEC_EXAMPLE = """\
members:
  - github: vincenzo-s
    name: Vincenzo
    roles: [dev, lead]
    providers: [anthropic, openai]
    timezone: Europe/Tirane
    capacity_hours_week: 20        # optional
"""

TWO = """\
members:
  - github: vincenzo-s
    roles: [dev, lead]
  - github: Ada-L
    roles: [dev]
"""


def write(root: Path, text: str) -> Path:
    path = root / TEAM_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def team_error(root: Path, text: str) -> ConfigError:
    path = write(root, text)
    with pytest.raises(ConfigError) as info:
        load_team(root)
    err = info.value
    assert err.file == str(path)
    assert err.hint
    return err


def test_spec_example_loads(tmp_path: Path) -> None:
    write(tmp_path, SPEC_EXAMPLE)
    team = load_team(tmp_path)
    assert team.members == (
        Member(
            github="vincenzo-s",
            name="Vincenzo",
            roles=frozenset({"dev", "lead"}),
            providers=("anthropic", "openai"),
            timezone="Europe/Tirane",
            capacity_hours_week=20,
        ),
    )


def test_defaults_and_helpers(tmp_path: Path) -> None:
    write(tmp_path, TWO)
    team = load_team(tmp_path)
    ada = team.member("ada-l")
    assert ada is not None and ada.github == "Ada-L"
    assert ada.name == "Ada-L"
    assert ada.providers == ()
    assert ada.timezone is None
    assert ada.capacity_hours_week is None
    assert team.member("VINCENZO-S") is team.members[0]
    assert team.member("nobody") is None
    assert [m.github for m in team.leads()] == ["vincenzo-s"]
    assert team.logins() == ("vincenzo-s", "Ada-L")


def test_missing_file_hints_forge_init(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_team(tmp_path)
    assert info.value.file == str(tmp_path / TEAM_PATH)
    assert info.value.hint is not None and "forge init" in info.value.hint


def test_duplicate_login_names_both_indices(tmp_path: Path) -> None:
    text = TWO + "  - github: VINCENZO-S\n    roles: [dev]\n"
    err = team_error(tmp_path, text)
    assert err.key == "members[2].github"
    assert "members[0]" in err.problem and "members[2]" in err.problem
    assert err.hint is not None and "members[0]" in err.hint


def _member(body: str) -> str:
    return "members:\n  - github: ok-user\n    roles: [dev]\n  - " + body + "\n"


@pytest.mark.parametrize(
    ("text", "key", "fix_part"),
    [
        ("other: 1\n", "other", "members"),
        ("members: []\n", "members", "add at least one member"),
        ("members:\n", "members", "members"),
        ("members: {a: 1}\n", "members", "members"),
        (_member("roles: [dev]"), "members[1].github", "github"),
        (_member("github: -bad\n    roles: [dev]"), "members[1].github", "vincenzo-s"),
        (_member("github: bad-\n    roles: [dev]"), "members[1].github", "vincenzo-s"),
        (_member("github: a--b\n    roles: [dev]"), "members[1].github", "vincenzo-s"),
        (_member("github: a_b\n    roles: [dev]"), "members[1].github", "vincenzo-s"),
        (_member("github: " + "a" * 40 + "\n    roles: [dev]"), "members[1].github", "1-39"),
        (_member("github: 123\n    roles: [dev]"), "members[1].github", "github"),
        (_member("github: x\n    roles: []"), "members[1].roles", "at least one"),
        (_member("github: x"), "members[1].roles", "at least one"),
        (_member("github: x\n    roles: [dev, admin]"), "members[1].roles[1]", "roles: [dev]"),
        (
            _member("github: x\n    roles: [dev]\n    timezone: Mars/Olympus"),
            "members[1].timezone",
            "Europe/Rome",
        ),
        (
            _member("github: x\n    roles: [dev]\n    timezone: europe/rome"),
            "members[1].timezone",
            "Europe/Rome",
        ),
        (
            _member("github: x\n    roles: [dev]\n    capacity_hours_week: lots"),
            "members[1].capacity_hours_week",
            "capacity_hours_week: 20",
        ),
        (
            _member("github: x\n    roles: [dev]\n    capacity_hours_week: -1"),
            "members[1].capacity_hours_week",
            "capacity_hours_week: 20",
        ),
        (
            _member("github: x\n    roles: [dev]\n    providers: anthropic"),
            "members[1].providers",
            "[",
        ),
        (_member("github: x\n    roles: [dev]\n    name: ''"), "members[1].name", "name"),
        (_member("github: x\n    roles: [dev]\n    role: lead"), "members[1].role", "roles"),
        ("members:\n  - just-a-login\n", "members[0]", "key: value"),
    ],
)
def test_validation_rule(tmp_path: Path, text: str, key: str, fix_part: str) -> None:
    err = team_error(tmp_path, text)
    assert err.key == key
    assert err.hint is not None and fix_part in err.hint
    assert str(err).startswith(f"{tmp_path / TEAM_PATH}: {key}: ")


def test_utc_timezone_accepted(tmp_path: Path) -> None:
    write(tmp_path, _member("github: x\n    roles: [lead]\n    timezone: UTC"))
    assert load_team(tmp_path).members[1].timezone == "UTC"
