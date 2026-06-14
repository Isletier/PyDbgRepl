"""Generic option get/set, e.g. set("port", 5678) or set("vm_type", "jython")."""
from .. import launch as _launch
from .. import options as _options
from ..session import SESSION
from ._display import Error, Status

__all__ = ["set", "reset"]

# Group names resettable as a unit via reset(), beyond _options.group_names()
# ("args_opt", "env", "repl"): the inferior's argv, which lives directly on
# RunContext rather than in an OptionGroup.
_ARGS_GROUP = "args"


def set(name: str, value) -> Status | Error:
    """Set an option, e.g. set("port", 5678) or set("vm_type", "jython")."""
    try:
        result = _options.set_option(name, value)
    except KeyError:
        return Error(f"unknown option '{name}'")
    except (_launch.LaunchError, ValueError) as e:
        return Error(str(e))
    return Status(f"{name} = {result!r}")


def reset(name: str) -> Status | Error:
    """Reset an option, or a whole group of options, to defaults.

    `name` is either a single option name (e.g. "port") or a group name:
    "args_opt"/"env"/"repl" (the corresponding RunContext/ReplOptions
    dataclasses), or "args" (the inferior's argv, reset to []).
    """
    if name == _ARGS_GROUP:
        SESSION.run_ctx.args = []
        return Status("args = []")

    try:
        defaults = _options.reset_group(name)
    except KeyError:
        pass
    else:
        return Status("\n".join(f"{opt_name} = {value!r}" for opt_name, value in defaults.items()))

    try:
        default = _options.reset_option(name)
    except KeyError:
        return Error(f"unknown option or group '{name}'")
    return Status(f"{name} = {default!r}")
