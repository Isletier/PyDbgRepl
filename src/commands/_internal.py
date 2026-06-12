"""Shared internals used by the command submodules.

Not part of the public REPL surface (no __all__, never injected into
__main__).
"""
import os
import readline
import select
import sys
import termios
import threading
import tty

from .. import dap as _dap
from ..session import SESSION

# Hooks run from _report_stopped() after the built-in "*** stopped ..." line,
# e.g. to auto-clear a temporary breakpoint or re-evaluate display()
# expressions. Populated by the submodules that own that state.
post_stop_hooks: list = []


def _stream_output(master_fd: int) -> None:
    while True:
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            break
        if not data:
            break
        os.write(1, data)


# ---- async output ----

def _async_print(message: str) -> None:
    """Print `message` from a background thread without leaving a stale prompt.

    Clears the current input line, prints the message, then rewrites the
    prompt and whatever the user had typed so far.
    """
    if SESSION.ptpython_active:
        # ptpython runs with patch_stdout=True, which already handles
        # redrawing the prompt around out-of-band writes.
        print(message)
        return

    prompt = getattr(sys, "ps1", "")
    line = readline.get_line_buffer()
    sys.stdout.write("\r" + " " * (len(prompt) + len(line)) + "\r")
    print(message)
    sys.stdout.write(prompt + line)
    sys.stdout.flush()


# ---- session lifetime ----

def _clear_dap_state() -> None:
    SESSION.dap = None
    SESSION.running = False
    SESSION.current_thread_id = None
    SESSION.current_frame_id = None


def _end_session() -> None:
    """Tear down the pydevd connection and any spawned process together as one unit."""
    if SESSION.dap is not None:
        client = SESSION.dap
        client.on_disconnect = None
        client.close()

    if SESSION.process is not None:
        if SESSION.process.child.poll() is None:
            SESSION.process.child.kill()
        SESSION.process.child.wait()
        if SESSION.process.master_fd is not None:
            os.close(SESSION.process.master_fd)
        SESSION.process = None
        SESSION.reader_thread = None

    _clear_dap_state()


# ---- stdin passthrough (see doc/io_model.md) ----

class _StdinPassthrough:
    """Forward our stdin to the inferior's pty while a blocking resume call is in flight.

    Switches our terminal to cbreak (ICANON/ECHO off, ISIG kept -- Ctrl+C
    still raises SIGINT in our process and goes through _sigint_handler ->
    interrupt(), unchanged). Restores cooked mode on stop().
    """

    def __init__(self, master_fd: int):
        self._master_fd = master_fd
        self._stop_r, self._stop_w = os.pipe()
        self._old_settings: list | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not sys.stdin.isatty():
            return
        try:
            self._old_settings = termios.tcgetattr(0)
            tty.setcbreak(0)
        except termios.error:
            # isatty() can be true for devices tcgetattr/setcbreak don't
            # support; fall back to no passthrough rather than crash.
            self._old_settings = None
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                rlist, _, _ = select.select([0, self._stop_r], [], [])
            except (OSError, ValueError):
                return
            if self._stop_r in rlist:
                return
            try:
                data = os.read(0, 1024)
            except OSError:
                return
            if not data:
                return
            try:
                os.write(self._master_fd, data)
            except OSError:
                return

    def stop(self) -> None:
        if self._thread is not None:
            os.write(self._stop_w, b"x")
            self._thread.join()
        os.close(self._stop_r)
        os.close(self._stop_w)
        if self._old_settings is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, self._old_settings)


# ---- blocking resume + event handling ----

_RESUME_RESULT_EVENTS = {"stopped", "exited", "terminated", "_disconnected"}


def _wait_for_resume_result(client: _dap.DAPClient) -> None:
    """Block until the resumed program stops, exits, or the connection drops.

    While blocked, forwards our stdin to the inferior's pty (default mode
    only -- not under --pty, and not for connect()-only sessions where we
    hold no fd to the debuggee's stdio). See doc/io_model.md.
    """
    passthrough = None
    if SESSION.process is not None and SESSION.process.master_fd is not None:
        passthrough = _StdinPassthrough(SESSION.process.master_fd)
        passthrough.start()

    try:
        message = client.wait_for_any_event(_RESUME_RESULT_EVENTS)
    finally:
        if passthrough is not None:
            passthrough.stop()

    event = message["event"]
    body = message["body"]

    if event == "stopped":
        _report_stopped(body)
        SESSION.running = False
        return

    # "exited"/"terminated"/"_disconnected" all mean this session is over —
    # the pydevd connection and any spawned process share one lifetime.
    if event == "exited":
        print(f"*** program exited with code {body.get('exitCode')}")
    elif event == "terminated":
        print("*** program terminated")
    elif event == "_disconnected":
        print("*** connection to pydevd lost")

    _end_session()


def _report_stopped(body: dict) -> None:
    SESSION.current_thread_id = body.get("threadId")
    SESSION.current_frame_id = None
    reason = body.get("reason")

    top = None
    if SESSION.current_thread_id is not None:
        try:
            trace = SESSION.dap.stack_trace(SESSION.current_thread_id, levels=1)
            frames = trace["stackFrames"]
            if frames:
                top = frames[0]
                SESSION.current_frame_id = top["id"]
        except _dap.DAPError:
            pass

    if top is None:
        print(f"*** stopped ({reason})")
    else:
        path = (top.get("source") or {}).get("path", "?")
        print(f"*** stopped ({reason}) at {path}:{top['line']}, in {top['name']}")

    for hook in post_stop_hooks:
        hook(reason, top)


def _on_dap_disconnect() -> None:
    if SESSION.dap is None:
        return
    if SESSION.running:
        # Main thread is blocked in _wait_for_resume_result(); wake it up so
        # it can report the disconnect and tear down the session itself.
        SESSION.dap.events.put({"event": "_disconnected", "body": {}})
        return
    _end_session()
    _async_print("*** connection to pydevd lost")


# ---- guards ----

def _ensure_dap_paused() -> bool:
    if SESSION.dap is None:
        print("error: not connected (use connect())")
        return False
    if SESSION.running:
        print("error: program is running")
        return False
    return True


def _ensure_thread_paused() -> bool:
    if not _ensure_dap_paused():
        return False
    if SESSION.current_thread_id is None:
        print("error: no current thread (use threads())")
        return False
    return True


# ---- current location & path/line shortcuts ----

def _current_location() -> tuple[str | None, int | None]:
    """The current frame's (source path, line), or the run() script with no line."""
    if SESSION.dap is not None and SESSION.current_frame_id is not None:
        try:
            trace = SESSION.dap.stack_trace(SESSION.current_thread_id)
            for f in trace["stackFrames"]:
                if f["id"] == SESSION.current_frame_id:
                    path = (f.get("source") or {}).get("path")
                    if path:
                        return path, f.get("line")
        except _dap.DAPError:
            pass
    return SESSION.run_ctx.args_opt.file, None


def _current_file() -> str | None:
    return _current_location()[0]


def _resolve_path_line(path_or_line: str | int, line: int | None) -> tuple[str, int] | None:
    """Normalize the `(path_or_line, line)` shortcut shared by breakpoint/clear/etc.

    A bare `int` for `path_or_line` means "`path_or_line` is a line number in
    the current file". Prints an error and returns None if neither a path nor
    a current file is available.
    """
    if isinstance(path_or_line, int):
        path = _current_file()
        if path is None:
            print("error: no current file (pass an explicit path)")
            return None
        return path, path_or_line

    if line is None:
        print("error: line number required")
        return None
    return path_or_line, line
