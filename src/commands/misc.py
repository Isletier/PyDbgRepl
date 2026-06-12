"""Misc / introspection: modules, pydevd_info."""
from .. import dap as _dap
from ..session import SESSION

__all__ = ["modules", "pydevd_info"]


def modules() -> None:
    """List modules loaded in the debuggee."""
    if SESSION.dap is None:
        print("error: not connected")
        return

    try:
        result = SESSION.dap.modules()
    except _dap.DAPError as e:
        print(f"error: {e}")
        return
    for m in result.get("modules", []):
        path = m.get("path", "")
        print(f"{m.get('id')}: {m.get('name')}{' (' + path + ')' if path else ''}")


def pydevd_info() -> None:
    """Print pydevd's process/Python/platform info (pydevdSystemInfo)."""
    if SESSION.dap is None:
        print("error: not connected")
        return

    try:
        result = SESSION.dap.pydevd_system_info()
    except _dap.DAPError as e:
        print(f"error: {e}")
        return
    for section, values in result.items():
        print(f"{section}:")
        if isinstance(values, dict):
            for k, v in values.items():
                print(f"  {k} = {v}")
        else:
            print(f"  {values}")
