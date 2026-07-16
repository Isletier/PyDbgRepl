"""Debugging session state: pydevd launch config plus our own REPL options."""
import dataclasses
import threading
import pathlib

from . import launch
from . import options as _options

from . import dap
from . import model


@dataclasses.dataclass
class ReplOptions:
    """Options for pydev-repl itself."""

    dap_host: str = "127.0.0.1"
    # REPL frontend: "auto" (ptpython if installed, else plain readline),
    # "ptpython", or "readline".
    ui: str = "auto"
    # Tab-completion mode (ptpython only): "debugger" (command-aware) or
    # "classical" (ptpython's normal jedi-based completion). Read live on
    # every completion request -- no restart needed.
    completion: str = "debugger"
    # If True (default), start_eval() drops into an interactive prompt
    # (ptpython or readline) once the script body finishes. If False
    # (--batch), the process just exits after the script body runs -- for
    # unattended automation scenarios. See doc/scenario_mode.md.
    interactive: bool = True

@dataclasses.dataclass
class SessionState:
    run_ctx: launch.RunContext = dataclasses.field(default_factory=launch.RunContext)
    options: ReplOptions = dataclasses.field(default_factory=ReplOptions)
    process: launch.LaunchedProcess | None = None
    reader_thread: threading.Thread | None = None
    client: dap.Client | None = None
    running: bool = False
    current_thread_id: int | None = None
    current_frame_id: int | None = None

    sourceMap:      model.SourceMap | None = None
    Breakpoints:    dict[int, model.Breakpoint] | None = None

    # `initialize` response, e.g. for `exceptionBreakpointFilters` (used by
    # catch()'s tab-completion).
    capabilities: dict = dataclasses.field(default_factory=dict)
    displays: list[dict] = dataclasses.field(default_factory=list)
    ptpython_active: bool = False


SESSION = SessionState()

_options.register(SESSION.run_ctx.args_opt, {
    "vm_type": launch.vm_type_reflection,
    "log_level": launch.log_level_reflection,
    "qt_support": launch.qt_support_reflection,
}, name="args_opt")
_options.register(SESSION.run_ctx.env, name="env")
_options.register(SESSION.options, name="repl")
