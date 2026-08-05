"""Generic registry mapping option names to their backing dataclass field.

`set`/`reset` (in commands.py) operate on whatever dataclass instances are
registered here, regardless of which "kind" of option they are (pydevd CLI
args, pydevd env vars, pydev-repl's own settings, ...). Adding a new group of
options is just one `register()` call -- no new commands needed.
"""

import dataclasses
from typing import Any, Callable

from pdvp import launch

Reflection = Callable[[str], Any]


class OptionGroup:
    def __init__(self, target: object, reflections: dict[str, Reflection] | None = None, name: str | None = None):
        self.target = target
        self.reflections = reflections or {}
        self.name = name


_GROUPS: list[OptionGroup] = []


def register(target: object, reflections: dict[str, Reflection] | None = None, name: str | None = None) -> None:
    """Register a dataclass instance whose fields are settable via set()/reset().

    `name`, if given, lets the whole group be reset at once via reset_group().
    """
    _GROUPS.append(OptionGroup(target, reflections, name))


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


def reset_option(name: str) -> Any:
    """Reset option `name` to its dataclass default. Returns the default value.

    Raises KeyError if `name` is not a known option.
    """
    group = _find_group(name)
    if group is None:
        raise KeyError(name)
    f = _field_for(group.target, name)
    if f.default_factory is not dataclasses.MISSING:
        default = f.default_factory()
    else:
        default = f.default
    setattr(group.target, name, default)
    return default


def get_option(name: str) -> Any:
    """Current value of option `name`. Raises KeyError if `name` is not a known option."""
    group = _find_group(name)
    if group is None:
        raise KeyError(name)
    return getattr(group.target, name)


def get_group(name: str) -> dict[str, Any]:
    """Current value of every option in group `name`, as {name: value}.

    Raises KeyError if `name` is not a registered group.
    """
    for group in _GROUPS:
        if group.name == name:
            return {f.name: getattr(group.target, f.name) for f in dataclasses.fields(group.target)}
    raise KeyError(name)


def group_names() -> list[str]:
    """Names of registered option groups, for reset_group() and tab-completion."""
    return [g.name for g in _GROUPS if g.name is not None]


def reset_group(name: str) -> dict[str, Any]:
    """Reset every option in group `name` to its dataclass default. Returns {name: default}.

    Raises KeyError if `name` is not a registered group.
    """
    for group in _GROUPS:
        if group.name == name:
            result = {}
            for f in dataclasses.fields(group.target):
                if f.default_factory is not dataclasses.MISSING:
                    default = f.default_factory()
                else:
                    default = f.default
                setattr(group.target, f.name, default)
                result[f.name] = default
            return result
    raise KeyError(name)


def list_options() -> list[tuple[str, Any]]:
    """Return (name, current value) for every registered option, in registration order."""
    result = []
    for group in _GROUPS:
        for f in dataclasses.fields(group.target):
            result.append((f.name, getattr(group.target, f.name)))
    return result

