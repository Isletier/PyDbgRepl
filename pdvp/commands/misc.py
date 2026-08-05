"""Misc / introspection: modules, pydevd_info."""
from .. import dap as _dap
from ..session import SESSION
from pdvp.model import Error, InfoSections, ModuleList

__all__ = ["modules", "pydevd_info"]


def modules() -> ModuleList | Error:
    """Modules loaded in the debuggee."""
    if SESSION.client is None:
        return Error("not connected")

    try:
        result = SESSION.client.modules()
    except _dap.DAPError as e:
        return Error(str(e))
    return ModuleList(result.get("modules", []))


def pydevd_info() -> InfoSections | Error:
    """pydevd's process/Python/platform info (pydevdSystemInfo)."""
    if SESSION.client is None:
        return Error("not connected")

    try:
        result = SESSION.client.pydevd_system_info()
    except _dap.DAPError as e:
        return Error(str(e))
    return InfoSections(result)
