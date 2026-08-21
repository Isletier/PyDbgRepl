"""Where the cursor is, in source terms.

The bridge between the cursor (a thread and a frame id) and the path/line pair
that breakpoint, list and jump commands actually take. Nothing here changes
state; the round trip it makes is a `stackTrace` on a thread the caller's
cursor already names.
"""
from .. import dap as _dap
from ..config import CONFIG
from ..session import SESSION
from pdvp.model import Error


def current_location() -> tuple[str | None, int | None]:
    """The current frame's (source path, line), or the run() script with no line."""
    if SESSION.client is not None and SESSION.current_frame_id is not None:
        try:
            trace = SESSION.client.stack_trace(SESSION.current_thread_id)
            for f in trace.body.stackFrames:
                if f["id"] == SESSION.current_frame_id:
                    path = (f.get("source") or {}).get("path")
                    if path:
                        return path, f.get("line")
        except _dap.DAPError:
            pass
    return CONFIG.file, None


def current_file() -> str | None:
    return current_location()[0]


def resolve_path_line(path_or_line: str | int, line: int | None) -> tuple[str, int] | Error:
    """Normalize the `(path_or_line, line)` shortcut shared by breakpoint/clear/etc.

    A bare `int` for `path_or_line` means "`path_or_line` is a line number in
    the current file". Returns an `Error` if neither a path nor a current file
    is available.
    """
    if isinstance(path_or_line, int):
        path = current_file()
        if path is None:
            return Error("no current file (pass an explicit path)")
        return path, path_or_line

    if line is None:
        return Error("line number required")
    return path_or_line, line
