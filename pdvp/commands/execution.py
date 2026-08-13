"""Execution control: cont, step, next, finish, interrupt, until, jump.

All of these (other than interrupt) block until the program stops, exits, or
terminates — see [[project_sync_execution_model]].
"""
from .. import dap as _dap
from ..session import SESSION
from .breakpoints import breakpoint as _set_breakpoint
from .breakpoints import clear as _clear_breakpoint
from pdvp.model import Error, Status, StopResult
from ._internal import (
    _current_location,
    _ensure_thread_paused,
    _report_stopped,
    _wait_for_resume_result,
)

__all__ = ["cont", "step", "next", "finish", "interrupt", "until", "jump"]


def cont() -> StopResult | Error:
    """Resume execution and block until the program stops, exits, or terminates."""
    err = _ensure_thread_paused()
    if err is not None:
        return err
    client = SESSION.client
    client.continue_(SESSION.current_thread_id)
    SESSION.running = True
    return _wait_for_resume_result(client, prefix="continuing")


def step() -> StopResult | Error:
    """Step into the next line, descending into calls. Blocks until the step completes."""
    err = _ensure_thread_paused()
    if err is not None:
        return err
    client = SESSION.client
    client.step_in(SESSION.current_thread_id)
    SESSION.running = True
    return _wait_for_resume_result(client, prefix="stepping")


def next() -> StopResult | Error:
    """Step over the next line, without descending into calls. Blocks until the step completes."""
    err = _ensure_thread_paused()
    if err is not None:
        return err
    client = SESSION.client
    client.next(SESSION.current_thread_id)
    SESSION.running = True
    return _wait_for_resume_result(client, prefix="stepping over")


def finish() -> StopResult | Error:
    """Run until the current function returns. Blocks until it does."""
    err = _ensure_thread_paused()
    if err is not None:
        return err
    client = SESSION.client
    client.step_out(SESSION.current_thread_id)
    SESSION.running = True
    return _wait_for_resume_result(client, prefix="finishing")


def interrupt() -> Status | Error:
    """Pause a running program. Used internally by Ctrl+C; returns immediately."""
    if SESSION.client is None:
        return Error("not connected (use connect())")
    if SESSION.current_thread_id is None:
        return Error("no current thread (use threads())")
    if not SESSION.running:
        return Error("program is not running")
    SESSION.client.pause(SESSION.current_thread_id)
    return Status("interrupting")


def until(line: int | None = None) -> StopResult | Error:
    """Run until `line` in the current file is reached (or the next line, if omitted).

    Emulated with a temporary breakpoint: set one at `line`, cont(), then
    clear it again -- pydevd has no native "run until" request.
    """
    err = _ensure_thread_paused()
    if err is not None:
        return err

    path, current_line = _current_location()
    if path is None:
        return Error("no current file")

    if line is None:
        if current_line is None:
            return Error("no current line")
        line = current_line + 1

    already_set = any(b["line"] == line for b in SESSION.breakpoints.get(path, []))
    if not already_set:
        _set_breakpoint(path, line)
    try:
        return cont()
    finally:
        if not already_set:
            _clear_breakpoint(path, line)


def jump(line: int) -> StopResult | Error:
    """Set the next line to execute in the current frame to `line`, without running it.

    Backed by the DAP gotoTargets/goto requests (supportsGotoTargetsRequest).
    Like gdb's jump, this skips/reruns code without any cleanup of
    skipped statements.
    """
    err = _ensure_thread_paused()
    if err is not None:
        return err

    path, _ = _current_location()
    if path is None:
        return Error("no current file")

    try:
        targets = SESSION.client.goto_targets({"path": path}, line)["targets"]
    except _dap.DAPError as e:
        return Error(str(e))
    if not targets:
        return Error(f"no jump target at {path}:{line}")

    try:
        SESSION.client.goto(SESSION.current_thread_id, targets[0]["id"])
        body = SESSION.client.wait_for_event("stopped", timeout=5)
    except _dap.DAPError as e:
        return Error(str(e))

    return _report_stopped(body)
