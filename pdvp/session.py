"""Debugging session state: the whole lifetime of the program in one object.

Fields are grouped by lifetime, because that is what decides who resets them:
program-lifetime state deliberately survives run()/stop() cycles, while
everything tied to one pydevd instance is cleared together by
commands._internal._clear_dap_state(). Put a new field under the right heading.
"""
import dataclasses
import threading

from . import launch

from . import dap
from . import model
from . import source


@dataclasses.dataclass
class SessionState:
    # ---- program lifetime: survives run()/stop(), reset by nothing ----
    Breakpoints:    dict[int, model.Breakpoint] = dataclasses.field(default_factory=dict)
    displays:       list[dict] = dataclasses.field(default_factory=list)

    # ---- pydevd core lifetime: cleared by _clear_dap_state() ----
    process: launch.LaunchedProcess | None = None
    client: dap.Client | None = None
    reader_thread: threading.Thread | None = None
    running: bool = False
    current_thread_id: int | None = None
    current_frame_id: int | None = None
    # sourceReferences are only valid for one DAP session (per the spec), so
    # this map cannot outlive the connection.
    sourceMap:      source.SourceMap = dataclasses.field(default_factory=source.SourceMap)

    # ---- frontend lifetime ----
    ptpython_active: bool = False


SESSION = SessionState()
