"""Misc / introspection: modules, pydevd_info."""
from .. import dap as _dap
from ..session import SESSION
from pdvp.model import Error, ErrorKind, InfoSections, ModuleList, PydevdRefused

__all__ = ["modules", "pydevd_info"]


def modules() -> ModuleList | Error:
    """Modules loaded in the debuggee."""
    if SESSION.client is None:
        return Error("not connected", kind=ErrorKind.NOT_CONNECTED)

    try:
        result = SESSION.client.modules()
    except _dap.DAPError as e:
        return PydevdRefused(str(e), cause=e)
    return ModuleList(result.body.modules)


def pydevd_info() -> InfoSections | Error:
    """pydevd's process/Python/platform info (pydevdSystemInfo)."""
    if SESSION.client is None:
        return Error("not connected", kind=ErrorKind.NOT_CONNECTED)

    try:
        result = SESSION.client.pydevd_system_info()
    except _dap.DAPError as e:
        return PydevdRefused(str(e), cause=e)
    return InfoSections(result.body.to_dict())
