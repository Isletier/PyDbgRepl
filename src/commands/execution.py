"""Execution control: cont, step, next, finish, interrupt, until, jump.

All of these (other than interrupt) block until the program stops, exits, or
terminates — see [[project_sync_execution_model]].
"""
from .. import dap as _dap
from ..session import SESSION
from .breakpoints import breakpoint as _set_breakpoint
from .breakpoints import clear as _clear_breakpoint
from ._internal import (
    _current_location,
    _ensure_thread_paused,
    _wait_for_resume_result,
)

__all__ = ["cont", "step", "next", "finish", "interrupt", "until", "jump"]


def cont() -> None:
    """Resume execution and block until the program stops, exits, or terminates."""
    if not _ensure_thread_paused():
        return
    client = SESSION.dap
    client.continue_(SESSION.current_thread_id)
    SESSION.running = True
    print("continuing")
    _wait_for_resume_result(client)


def step() -> None:
    """Step into the next line, descending into calls. Blocks until the step completes."""
    if not _ensure_thread_paused():
        return
    client = SESSION.dap
    client.step_in(SESSION.current_thread_id)
    SESSION.running = True
    print("stepping")
    _wait_for_resume_result(client)


def next() -> None:
    """Step over the next line, without descending into calls. Blocks until the step completes."""
    if not _ensure_thread_paused():
        return
    client = SESSION.dap
    client.next(SESSION.current_thread_id)
    SESSION.running = True
    print("stepping over")
    _wait_for_resume_result(client)


def finish() -> None:
    """Run until the current function returns. Blocks until it does."""
    if not _ensure_thread_paused():
        return
    client = SESSION.dap
    client.step_out(SESSION.current_thread_id)
    SESSION.running = True
    print("finishing")
    _wait_for_resume_result(client)


def interrupt() -> None:
    """Pause a running program. Used internally by Ctrl+C; returns immediately."""
    if SESSION.dap is None:
        print("error: not connected (use connect())")
        return
    if SESSION.current_thread_id is None:
        print("error: no current thread (use threads())")
        return
    if not SESSION.running:
        print("error: program is not running")
        return
    SESSION.dap.pause(SESSION.current_thread_id)
    print("interrupting")


def until(line: int | None = None) -> None:
    """Run until `line` in the current file is reached (or the next line, if omitted).

    Emulated with a temporary breakpoint: set one at `line`, cont(), then
    clear it again -- pydevd has no native "run until" request.
    """
    if not _ensure_thread_paused():
        return

    path, current_line = _current_location()
    if path is None:
        print("error: no current file")
        return

    if line is None:
        if current_line is None:
            print("error: no current line")
            return
        line = current_line + 1

    already_set = any(b["line"] == line for b in SESSION.breakpoints.get(path, []))
    if not already_set:
        _set_breakpoint(path, line)
    try:
        cont()
    finally:
        if not already_set:
            _clear_breakpoint(path, line)


def jump(line: int) -> None:
    """Set the next line to execute in the current frame to `line`, without running it.

    Backed by the DAP gotoTargets/goto requests (supportsGotoTargetsRequest).
    Like gdb's jump, this skips/reruns code without any cleanup of
    skipped statements.
    """
    if not _ensure_thread_paused():
        return

    path, _ = _current_location()
    if path is None:
        print("error: no current file")
        return

    try:
        targets = SESSION.dap.goto_targets({"path": path}, line)["targets"]
    except _dap.DAPError as e:
        print(f"error: {e}")
        return
    if not targets:
        print(f"error: no jump target at {path}:{line}")
        return

    try:
        SESSION.dap.goto(SESSION.current_thread_id, targets[0]["id"])
        body = SESSION.dap.wait_for_event("stopped", timeout=5)
    except _dap.DAPError as e:
        print(f"error: {e}")
        return

    from ._internal import _report_stopped
    _report_stopped(body)
