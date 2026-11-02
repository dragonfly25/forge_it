"""Team roster: `.forge/team.yml` (spec §6.2).

``github`` is the identity key used everywhere; logins compare case-insensitively, as on
GitHub.
"""

from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from forge.core.validate import Fields, load_yaml_mapping, type_name

TEAM_PATH = Path(".forge/team.yml")
"""Location of the team file, relative to the repository root."""

Role = Literal["dev", "lead"]
ROLES: tuple[Role, ...] = ("dev", "lead")

LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
"""GitHub login: alphanumerics or single hyphens, 1-39 chars, no leading/trailing hyphen."""

_MEMBER_KEYS = ("github", "name", "roles", "providers", "timezone", "capacity_hours_week")


@dataclass(frozen=True)
class Member:
    """One team member. ``name`` defaults to the login when not given."""

    github: str
    name: str
    roles: frozenset[Role]
    providers: tuple[str, ...] = ()
    timezone: str | None = None
    capacity_hours_week: int | None = None

    @property
    def is_lead(self) -> bool:
        """True if the member has the `lead` role."""
        return "lead" in self.roles


@dataclass(frozen=True)
class Team:
    """The whole `.forge/team.yml`."""

    members: tuple[Member, ...]

    def member(self, login: str) -> Member | None:
        """Return the member with GitHub ``login`` (case-insensitive), or ``None``."""
        key = login.casefold()
        for m in self.members:
            if m.github.casefold() == key:
                return m
        return None

    def leads(self) -> tuple[Member, ...]:
        """Return the members with the `lead` role, in file order."""
        return tuple(m for m in self.members if m.is_lead)

    def logins(self) -> tuple[str, ...]:
        """Return every member's GitHub login, in file order, as written in the file."""
        return tuple(m.github for m in self.members)


def load_team(repo_root: Path) -> Team:
    """Load and validate `.forge/team.yml` under ``repo_root``.

    Raises :class:`ConfigError` when the file is missing or invalid.
    """
    path = repo_root / TEAM_PATH
    data = load_yaml_mapping(path, create_hint="forge init")
    return parse_team(Fields(data, path))


def parse_team(f: Fields) -> Team:
    """Build a :class:`Team` from the top-level reader of a team file."""
    f.reject_unknown(("members",))
    raw = f.raw("members")
    if raw is None:
        raise f.fail(
            "members",
            "required key is missing",
            "add `members:` with at least one `- github: <login>` entry",
        )
    readers = f.list_of_mappings("members")
    if not readers:
        raise f.fail(
            "members",
            "must be a non-empty list",
            "add at least one member, e.g. `- github: <login>` with `roles: [dev]`",
        )
    members: list[Member] = []
    seen: dict[str, int] = {}
    for i, r in enumerate(readers):
        m = _member(r)
        key = m.github.casefold()
        if key in seen:
            first = seen[key]
            raise r.fail(
                "github",
                f"duplicate login {m.github!r}: members[{first}] and members[{i}] "
                "(logins are case-insensitive)",
                f"remove one of the entries members[{first}] or members[{i}]",
            )
        seen[key] = i
        members.append(m)
    return Team(members=tuple(members))


def _member(f: Fields) -> Member:
    f.reject_unknown(_MEMBER_KEYS)
    login = f.str_("github")
    if LOGIN_RE.fullmatch(login) is None:
        raise f.fail(
            "github",
            f"invalid GitHub login {login!r}",
            "use the GitHub username: 1-39 letters, digits or single hyphens, "
            "not starting or ending with a hyphen (e.g. `github: vincenzo-s`)",
        )
    name = f.str_("name", default=login)
    if not name.strip():
        raise f.fail("name", "must not be empty", f"write it as `name: {login}` or remove it")
    roles = f.str_list("roles", non_empty=True)
    for j, role in enumerate(roles):
        if role not in ROLES:
            raise f.fail(
                f"roles[{j}]",
                f"invalid role {role!r}; expected one of: {', '.join(ROLES)}",
                "write it as `roles: [dev]` or `roles: [dev, lead]`",
            )
    providers = f.str_list("providers")
    timezone = f.str_("timezone", required=False)
    if timezone is not None and timezone not in _available_timezones():
        raise f.fail(
            "timezone",
            f"unknown IANA time zone {timezone!r}",
            "use an IANA zone name, e.g. `timezone: Europe/Rome`",
        )
    return Member(
        github=login,
        name=name,
        roles=frozenset(cast(Role, r) for r in roles),
        providers=tuple(providers),
        timezone=timezone,
        capacity_hours_week=_optional_int(f, "capacity_hours_week"),
    )


def _optional_int(f: Fields, key: str) -> int | None:
    """Return an optional non-negative integer (``None`` when absent or null)."""
    value = f.raw(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise f.fail(
            key,
            f"expected an integer or null, got {type_name(value)}",
            f"write it as `{key}: 20` or remove it",
        )
    if value < 0:
        raise f.fail(key, f"must be >= 0, got {value}", f"write it as `{key}: 20`")
    return value


@lru_cache(maxsize=1)
def _available_timezones() -> frozenset[str]:
    """IANA zone names known to this system (exact case, unlike file lookups on macOS)."""
    return frozenset(zoneinfo.available_timezones())
