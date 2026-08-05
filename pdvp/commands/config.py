"""Restoring configuration defaults.

Setting and reading options is plain attribute access on `pdvp.config` --
`config.port = 5678`, `config.port` -- so there is nothing here for that. What
attribute access can't express is "put it back the way it was", which is what
reset() is for.
"""
import dataclasses

from .. import options as _options
from pdvp.session import SESSION
from pdvp.model import Error, Status

__all__ = ["reset"]


def reset(*names: str) -> Status | Error:
    """Restore config fields to their defaults, e.g. reset("port"). No
    arguments resets every field.

    Note that `port` defaults to a fresh random port, so resetting it gives a
    new one rather than the one this session started with.
    """
    fields = {f.name: f for f in dataclasses.fields(SESSION.config)}

    if not names:
        names = tuple(fields)

    unknown = [name for name in names if name not in fields]
    if unknown:
        return Error(f"unknown option{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}")

    lines = []
    for name in names:
        default = _options.default_of(fields[name])
        setattr(SESSION.config, name, default)
        lines.append(f"{name} = {default!r}")

    return Status("\n".join(lines))
