"""Generic option get/set, e.g. set("port", 5678) or set("vm_type", "jython")."""
from .. import launch as _launch
from .. import options as _options

__all__ = ["set", "unset"]


def set(name: str, value) -> None:
    """Set an option, e.g. set("port", 5678) or set("vm_type", "jython")."""
    try:
        result = _options.set_option(name, value)
    except KeyError:
        print(f"error: unknown option '{name}'")
        return
    except (_launch.LaunchError, ValueError) as e:
        print(f"error: {e}")
        return
    print(f"{name} = {result!r}")


def unset(name: str) -> None:
    """Reset an option to its default value."""
    try:
        default = _options.unset_option(name)
    except KeyError:
        print(f"error: unknown option '{name}'")
        return
    print(f"{name} = {default!r}")
