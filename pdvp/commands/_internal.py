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
from .. import events
from ..config import CONFIG
from ..session import SESSION
from pdvp.model import Error, StopResult

# Hooks run from _report_stopped() to compute the returned StopResult's
# `suffix` (extra lines shown after "*** stopped ..."), e.g. to auto-clear a
# temporary breakpoint or re-evaluate display() expressions. Each hook is
# called as hook(reason, top) and returns a str line (or None to contribute
# nothing). Populated by the submodules that own that state.
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


# ---- the reducer: DAP in, pdvp events out ----

def _dispatch(message) -> None:
    """The Client's `on_event` sink. Runs on the reader thread.

    Session does the reduction and the publishing; the only thing that belongs
    to Layer 4 is what to do about a death nobody is waiting for.
    """
    SESSION.reduce(message)

    if (isinstance(message, _dap.ConnectionClosed)
            and not message.deliberate
            and not SESSION.awaiting_resume):
        # Nobody is blocked in _resume() to notice and tear the session down,
        # so do it here and say so.
        _end_session()
        _async_print("*** connection to pydevd lost")


# ---- session lifetime ----

def _clear_dap_state() -> None:
    """Reset everything whose lifetime is the pydevd connection.

    Called by _end_session(), and directly by disconnect() -- which leaves the
    debuggee running but still ends our DAP session, so its sourceReferences
    and breakpoint verifications are just as dead either way.
    """
    # Imported here rather than at module level: commands.breakpoints imports
    # _internal (for _current_file), so the module-level edge only goes one way.
    from .breakpoints import invalidate_all

    SESSION.end_connection()
    invalidate_all()


def _end_session() -> None:
    """Tear down the pydevd connection and any spawned process together as one unit."""
    if SESSION.client is not None:
        SESSION.client.close()

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

def describe_thread(thread_id: int | None) -> str:
    return "all threads" if thread_id is None else f"thread {thread_id}"


def _resume(thread_id: int | None, request, prefix: str = "") -> StopResult | Error:
    """Resume `thread_id` (None meaning all) and block until the program stops,
    exits, or the connection drops.

    `request(client)` issues the actual DAP request. Every state-changing
    operation routes through here, which is what makes the thread control
    right a single acquisition site: it is held from the send until the stop,
    so two callers resuming one thread become two serialized runs rather than
    one run reported as two.

    The subscription is opened before the request goes out, because a `stopped`
    routinely beats the response to the request that caused it onto the wire.
    `SessionEnded` is on every subscription whether asked for or not, which is
    what makes this wait bounded without a timeout.

    While blocked, forwards our stdin to the inferior's pty -- only if its
    stdin is still the owned-PTY-pair slave (not under --pty, not redirected
    via stdin=, and not for connect()-only sessions where we hold no fd to
    the debuggee's stdio). See doc/io_model.md.

    `prefix` is shown before the outcome in the returned StopResult's repr,
    e.g. "continuing" or "launched pid=1234\nconnected to 127.0.0.1:5678".
    """
    error = SESSION.require_connected()
    if error is not None:
        return error

    def announce() -> None:
        print(f"*** waiting for control of {describe_thread(thread_id)}")

    with SESSION.control.hold(thread_id, announce=announce):
        # Re-checked after acquiring, not before: whoever held the right may
        # have run this thread somewhere else entirely, or ended the session,
        # while we waited. A resume naming no thread has nothing to check --
        # the handshake's initial one happens before anything has stopped.
        error = SESSION.require_connected()
        if error is None and thread_id is not None:
            error = SESSION.require_stopped(thread_id)
        if error is not None:
            return error

        return _await_stop(thread_id, request, prefix)


def _await_stop(thread_id: int | None, request, prefix: str) -> StopResult:
    """The resume itself, with the control right already held."""
    with SESSION.bus.subscribe(events.Stopped) as outcome, SESSION.resume_wait():
        # Bumped before the request goes out. An early bump costs a spurious
        # "frame is stale" if the send fails; a late one hands back wrong data
        # silently. From here on a Ctrl+C also has something to interrupt.
        SESSION.note_resume(thread_id)
        try:
            request(SESSION.client)
        except BaseException:
            SESSION.undo_resume(thread_id)
            raise

        passthrough = None
        if (
            SESSION.process is not None
            and SESSION.process.master_fd is not None
            and SESSION.process.stdin_is_pty
        ):
            passthrough = _StdinPassthrough(SESSION.process.master_fd)
            passthrough.start()

        try:
            event = outcome.get()
        finally:
            if passthrough is not None:
                passthrough.stop()

    if isinstance(event, events.Stopped):
        return _report_stopped(event, prefix=prefix)

    # Exit, termination and disconnection are one ending — the pydevd
    # connection and any spawned process share one lifetime.
    _end_session()
    return StopResult(event, prefix=prefix)


def _report_stopped(event: events.Stopped, prefix: str = "") -> StopResult:
    SESSION.current_thread_id = event.thread_id
    reason = event.reason

    top = None
    if event.thread_id is not None:
        try:
            trace = SESSION.client.stack_trace(event.thread_id, levels=1)
            frames = trace.body.stackFrames
            if frames:
                top = frames[0]
                SESSION.current_frame_id = top["id"]
        except _dap.DAPError:
            pass

    suffix_lines = [line for line in (hook(reason, top) for hook in post_stop_hooks) if line]

    return StopResult(event, top_frame=top, prefix=prefix, suffix="\n".join(suffix_lines))


# ---- guards ----

def _ensure_connected() -> Error | None:
    return SESSION.require_connected()


def _ensure_thread_paused() -> Error | None:
    """The gate on everything frame-scoped: connected, a thread selected, and stopped."""
    return SESSION.require_stopped(SESSION.current_thread_id)


# ---- current location & path/line shortcuts ----

def _current_location() -> tuple[str | None, int | None]:
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


def _current_file() -> str | None:
    return _current_location()[0]


def _resolve_path_line(path_or_line: str | int, line: int | None) -> tuple[str, int] | Error:
    """Normalize the `(path_or_line, line)` shortcut shared by breakpoint/clear/etc.

    A bare `int` for `path_or_line` means "`path_or_line` is a line number in
    the current file". Returns an `Error` if neither a path nor a current
    file is available.
    """
    if isinstance(path_or_line, int):
        path = _current_file()
        if path is None:
            return Error("no current file (pass an explicit path)")
        return path, path_or_line

    if line is None:
        return Error("line number required")
    return path_or_line, line
