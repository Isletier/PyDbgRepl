"""Debugging session state: pydevd launch config plus our own REPL options."""
import dataclasses
import threading

from . import launch
from . import options as _options
from .dap import DAPClient


@dataclasses.dataclass
class ReplOptions:
    """Options for pydev-repl itself."""

    dap_host: str = "127.0.0.1"


@dataclasses.dataclass
class SessionState:
    run_ctx: launch.RunContext = dataclasses.field(default_factory=launch.RunContext)
    options: ReplOptions = dataclasses.field(default_factory=ReplOptions)
    process: launch.LaunchedProcess | None = None
    reader_thread: threading.Thread | None = None
    dap: DAPClient | None = None
    running: bool = False
    current_thread_id: int | None = None
    current_frame_id: int | None = None
    breakpoints: dict[str, list[dict]] = dataclasses.field(default_factory=dict)
    exception_filters: list[str] = dataclasses.field(default_factory=list)


SESSION = SessionState()

_options.register(SESSION.run_ctx.args_opt, {
    "vm_type": launch.vm_type_reflection,
    "log_level": launch.log_level_reflection,
    "qt_support": launch.qt_support_reflection,
})
_options.register(SESSION.run_ctx.env)
_options.register(SESSION.options)
