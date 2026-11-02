"""YAML loading and typed field validation for Forge's configuration files.

Every problem is reported as a :class:`ConfigError` naming the file, the dotted key path
(with list indices, e.g. ``members[2].github``), the problem and a concrete fix.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeVar, overload

import yaml

from forge.core.errors import ConfigError

E = TypeVar("E", bound=Enum)


def type_name(value: object) -> str:
    """Return a YAML-flavoured name for the type of ``value`` (used in error messages)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "mapping"
    return type(value).__name__


def load_yaml_mapping(path: Path, *, create_hint: str | None = None) -> dict[str, Any]:
    """Load ``path`` with ``yaml.safe_load`` and return its top-level mapping.

    ``create_hint`` names the command that creates the file (e.g. ``"forge init"``); it is
    used in the fix when the file is missing.
    """
    if not path.is_file():
        fix = f"run `{create_hint}` to create it" if create_hint else "create the file"
        raise ConfigError(path, None, "file not found", fix)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(path, None, f"cannot read file: {exc}", "check the file") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise ConfigError(
            path,
            None,
            f"invalid YAML{where}: {problem}",
            "fix the YAML syntax (check indentation, colons and quotes)",
        ) from exc
    if loaded is None:
        raise ConfigError(path, None, "file is empty", "add the required keys to the file")
    if not isinstance(loaded, dict):
        raise ConfigError(
            path,
            None,
            f"top level must be a mapping, got {type_name(loaded)}",
            "write the file as `key: value` pairs at the top level",
        )
    return {str(k): v for k, v in loaded.items()}


class Fields:
    """Typed, validating reader over one mapping of a configuration file."""

    def __init__(self, data: Mapping[str, Any], file: Path | str, prefix: str = "") -> None:
        self.data = data
        self.file = str(file)
        self.prefix = prefix

    # -- helpers ---------------------------------------------------------------------------

    def path(self, key: str) -> str:
        """Return the dotted key path of ``key`` under this reader's prefix."""
        return f"{self.prefix}.{key}" if self.prefix else key

    def fail(self, key: str | None, problem: str, fix: str) -> ConfigError:
        """Build a :class:`ConfigError` for ``key`` (``None`` means this mapping itself)."""
        key_path = self.path(key) if key is not None else (self.prefix or None)
        return ConfigError(self.file, key_path, problem, fix)

    def _wrong_type(self, key: str, expected: str, value: object, example: str) -> ConfigError:
        return self.fail(
            key,
            f"expected {expected}, got {type_name(value)}",
            f"write it as {_yaml_example(key, example)}",
        )

    def _missing(self, key: str, example: str) -> ConfigError:
        """Error for a required key that is absent, or present with a null value."""
        if key in self.data:
            return self.fail(key, "is empty (null)", f"give it a value: `{key}: {example}`")
        return self.fail(key, "required key is missing", f"add `{key}: {example}`")

    def has(self, key: str) -> bool:
        """True if ``key`` is present (even with a null value)."""
        return key in self.data

    def raw(self, key: str) -> Any:
        """Return the raw value of ``key`` (``None`` when absent)."""
        return self.data.get(key)

    # -- getters ---------------------------------------------------------------------------

    @overload
    def str_(
        self,
        key: str,
        *,
        required: Literal[True] = ...,
        default: str | None = ...,
        pattern: str | None = ...,
    ) -> str: ...

    @overload
    def str_(
        self,
        key: str,
        *,
        required: bool = ...,
        default: str,
        pattern: str | None = ...,
    ) -> str: ...

    @overload
    def str_(
        self,
        key: str,
        *,
        required: Literal[False],
        default: None = ...,
        pattern: str | None = ...,
    ) -> str | None: ...

    def str_(
        self,
        key: str,
        *,
        required: bool = True,
        default: str | None = None,
        pattern: str | None = None,
    ) -> str | None:
        """Return a string value. Missing → ``default`` if not required, else error."""
        value = self.data.get(key)
        if value is None:
            if required and default is None:
                raise self._missing(key, '"..."')
            return default
        if not isinstance(value, str):
            raise self._wrong_type(key, "a string", value, f'"{value}"')
        if pattern is not None and re.fullmatch(pattern, value) is None:
            raise self.fail(
                key,
                f"value {value!r} does not match the pattern {pattern}",
                f"use a value matching {pattern}",
            )
        return value

    def int_(self, key: str, *, default: int, min: int | None = None) -> int:
        """Return an integer value (booleans are rejected), or ``default`` when absent."""
        value = self.data.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._wrong_type(key, "an integer", value, str(default))
        if min is not None and value < min:
            raise self.fail(key, f"must be >= {min}, got {value}", f"write it as `{key}: {min}`")
        return value

    def bool_(self, key: str, *, default: bool) -> bool:
        """Return a boolean value (integers are rejected), or ``default`` when absent."""
        value = self.data.get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise self._wrong_type(key, "a boolean (true or false)", value, "true")
        return value

    def str_list(
        self, key: str, *, default: list[str] | None = None, non_empty: bool = False
    ) -> list[str]:
        """Return a list of strings, or a copy of ``default`` (``[]``) when absent."""
        value = self.data.get(key)
        if value is None:
            result = list(default) if default is not None else []
        else:
            if not isinstance(value, list):
                raise self._wrong_type(key, "a list of strings", value, '["a", "b"]')
            for i, item in enumerate(value):
                if not isinstance(item, str):
                    raise self.fail(
                        f"{key}[{i}]",
                        f"expected a string, got {type_name(item)}",
                        f'quote the item: `- "{item}"`',
                    )
            result = list(value)
        if non_empty and not result:
            raise self.fail(
                key, "must be a non-empty list", f'add at least one item: `{key}: ["..."]`'
            )
        return result

    def mapping(self, key: str, *, required: bool = False) -> Fields:
        """Return a nested reader for the mapping at ``key`` (empty when absent, not required)."""
        value = self.data.get(key)
        if value is None:
            if required:
                raise self._missing(key, "{...}")
            return Fields({}, self.file, self.path(key))
        if not isinstance(value, dict):
            raise self._wrong_type(key, "a mapping", value, "{name: value}")
        return Fields({str(k): v for k, v in value.items()}, self.file, self.path(key))

    def list_of_mappings(self, key: str) -> list[Fields]:
        """Return one reader per item of the list at ``key`` (``[]`` when absent)."""
        value = self.data.get(key)
        if value is None:
            return []
        if not isinstance(value, list):
            raise self._wrong_type(key, "a list of mappings", value, "[{name: value}]")
        readers: list[Fields] = []
        for i, item in enumerate(value):
            item_path = f"{key}[{i}]"
            if not isinstance(item, dict):
                raise self.fail(
                    item_path,
                    f"expected a mapping, got {type_name(item)}",
                    "write each item as `- key: value`",
                )
            readers.append(
                Fields({str(k): v for k, v in item.items()}, self.file, self.path(item_path))
            )
        return readers

    def choice(self, key: str, choices: Iterable[str], *, default: str) -> str:
        """Return a string that must be one of ``choices``, or ``default`` when absent."""
        options = list(choices)
        value = self.data.get(key)
        if value is None:
            return default
        if not isinstance(value, str) or value not in options:
            raise self.fail(
                key,
                f"invalid value {value!r}; expected one of: {', '.join(options)}",
                f"write it as `{key}: {_suggest(str(value), options) or options[0]}`",
            )
        return value

    def enum(self, key: str, enum_cls: type[E], *, default: E) -> E:
        """Return a member of ``enum_cls`` parsed by value, or ``default`` when absent."""
        values = [str(m.value) for m in enum_cls]
        raw = self.choice(key, values, default=str(default.value))
        return enum_cls(raw)

    def str_map(self, key: str, *, key_enum: type[Enum] | None = None) -> dict[str, str]:
        """Return a mapping of strings to strings (``{}`` when absent).

        With ``key_enum``, every key must be one of that enum's values (e.g. ``Tier``).
        """
        value = self.data.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise self._wrong_type(key, "a mapping of strings", value, '{name: "value"}')
        allowed = [str(m.value) for m in key_enum] if key_enum is not None else None
        result: dict[str, str] = {}
        for k, v in value.items():
            sub = f"{key}.{k}"
            if allowed is not None and k not in allowed:
                hint = _suggest(str(k), allowed)
                fix = f"did you mean `{hint}`?" if hint else f"use one of: {', '.join(allowed)}"
                raise self.fail(sub, f"unknown key; expected one of: {', '.join(allowed)}", fix)
            if not isinstance(v, str):
                raise self._wrong_type(sub, "a string", v, f'"{v}"')
            result[str(k)] = v
        return result

    def reject_unknown(self, allowed: Iterable[str]) -> None:
        """Raise on the first key not in ``allowed``, with a did-you-mean suggestion."""
        known = list(allowed)
        for k in self.data:
            if k not in known:
                hint = _suggest(str(k), known)
                fix = (
                    f"did you mean `{hint}`?"
                    if hint
                    else f"remove it; allowed keys: {', '.join(known) or '(none)'}"
                )
                raise self.fail(str(k), "unknown key", fix)


def _yaml_example(key: str, example: str) -> str:
    """Render ``key: example`` as pasteable YAML; a dotted key nests under its parent.

    For example ``tiers.strong`` with ``"x"`` renders as: `strong: "x"` under `tiers:`.
    """
    parent, sep, leaf = key.rpartition(".")
    if not sep:
        return f"`{key}: {example}`"
    return f"`{leaf}: {example}` under `{parent}:`"


def _suggest(word: str, options: list[str]) -> str | None:
    """Return the closest match to ``word`` among ``options``, if any is close enough."""
    matches = difflib.get_close_matches(word, options, n=1, cutoff=0.6)
    return matches[0] if matches else None
