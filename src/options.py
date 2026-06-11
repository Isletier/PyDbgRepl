"""Generic registry mapping option names to their backing dataclass field.

`set`/`unset` (in commands.py) operate on whatever dataclass instances are
registered here, regardless of which "kind" of option they are (pydevd CLI
args, pydevd env vars, pydev-repl's own settings, ...). Adding a new group of
options is just one `register()` call -- no new commands needed.
"""
import dataclasses
from typing import Any, Callable

from . import launch

Reflection = Callable[[str], Any]


class OptionGroup:
    def __init__(self, target: object, reflections: dict[str, Reflection] | None = None):
        self.target = target
        self.reflections = reflections or {}


_GROUPS: list[OptionGroup] = []


def register(target: object, reflections: dict[str, Reflection] | None = None) -> None:
    """Register a dataclass instance whose fields are settable via set()/unset()."""
    _GROUPS.append(OptionGroup(target, reflections))


def _find_group(name: str) -> OptionGroup | None:
    for group in _GROUPS:
        if hasattr(group.target, name):
            return group
    return None


def _field_for(target: object, name: str) -> dataclasses.Field:
    for f in dataclasses.fields(target):
        if f.name == name:
            return f
    raise KeyError(name)


def _coerce(group: OptionGroup, name: str, value: str) -> Any:
    if name in group.reflections:
        return group.reflections[name](value)
    kind = launch._unwrap_optional(_field_for(group.target, name).type)
    if kind is bool:
        return launch.parse_bool(value)
    if kind is int:
        return int(value)
    if kind is float:
        return float(value)
    return value


def set_option(name: str, value: Any) -> Any:
    """Set option `name` to `value`, coercing strings to the field's type. Returns the new value.

    Raises KeyError if `name` is not a known option.
    """
    group = _find_group(name)
    if group is None:
        raise KeyError(name)
    if isinstance(value, str):
        value = _coerce(group, name, value)
    setattr(group.target, name, value)
    return getattr(group.target, name)


def unset_option(name: str) -> Any:
    """Reset option `name` to its dataclass default. Returns the default value.

    Raises KeyError if `name` is not a known option.
    """
    group = _find_group(name)
    if group is None:
        raise KeyError(name)
    default = _field_for(group.target, name).default
    setattr(group.target, name, default)
    return default


def list_options() -> list[tuple[str, Any]]:
    """Return (name, current value) for every registered option, in registration order."""
    result = []
    for group in _GROUPS:
        for f in dataclasses.fields(group.target):
            result.append((f.name, getattr(group.target, f.name)))
    return result
