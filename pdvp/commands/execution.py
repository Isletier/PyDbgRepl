"""Execution control: cont, step, next, finish, interrupt, jump, control.

All of these (other than interrupt) block until the program stops, exits, or
terminates — see [[project_sync_execution_model]].

Each of them is state-changing, so each acquires the thread control right for
the duration of its run. Nobody writes that down: the acquisition lives in
_resume(), and `control()` is the same primitive held longer.
"""
import contextlib

from .. import dap as _dap
from ..session import SESSION
from pdvp.model import Error, PDVPError, Status, StopResult
from ._internal import (
    _current_location,
    _ensure_thread_paused,
    _resume,
    describe_thread,
)

__all__ = ["cont", "step", "next", "finish", "interrupt", "jump", "control"]


def _step(request: str, prefix: str) -> StopResult | Error:
    """The four resumes, which differ only in which request they send.

    The one thing checked here is that the caller has a thread at all -- that
    is about *their* cursor, not about run state. The stopped-check lives in
    _resume(), after the control right is acquired: doing it first would turn
    "wait your turn" into "thread 2 is running" for a caller queued behind
    somebody else's run, which is the serialization the right exists to give.
    """
    thread_id = SESSION.current_thread_id
    if thread_id is None:
        return Error("no current thread (use threads())")
    return _resume(thread_id, lambda client: getattr(client, request)(thread_id), prefix=prefix)


def cont() -> StopResult | Error:
    """Resume execution and block until the program stops, exits, or terminates."""
    return _step("continue_", prefix="continuing")


def step() -> StopResult | Error:
    """Step into the next line, descending into calls. Blocks until the step completes."""
    return _step("step_in", prefix="stepping")


def next() -> StopResult | Error:
    """Step over the next line, without descending into calls. Blocks until the step completes."""
    return _step("next", prefix="stepping over")


def finish() -> StopResult | Error:
    """Run until the current function returns. Blocks until it does."""
    return _step("step_out", prefix="finishing")


def interrupt() -> Status | Error:
    """Pause a running program. Used internally by Ctrl+C; returns immediately."""
    if SESSION.client is None:
        return Error("not connected (use connect())")
    if SESSION.current_thread_id is None:
        return Error("no current thread (use threads())")
    if SESSION.is_stopped(SESSION.current_thread_id):
        return Error("program is not running")
    # Exempt from the control right, deliberately. Measured, pause is
    # idempotent -- a second one on an already-suspended thread emits nothing
    # -- so it cannot corrupt the armed wait of whoever holds the right. That
    # exemption is what keeps Ctrl+C working while a run is in flight, and is
    # the escape hatch when one caller's run is making another wait.
    #
    # What ends the blocked cont() is the `stopped` event its subscription is
    # already armed for, not this response -- so this returns as soon as pydevd
    # acknowledges, without waiting for the thread to actually suspend.
    SESSION.client.pause(SESSION.current_thread_id)
    return Status("interrupting")


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
        targets = SESSION.client.goto_targets({"path": path}, line).body.targets
    except _dap.DAPError as e:
        return Error(str(e))
    if not targets:
        return Error(f"no jump target at {path}:{line}")

    # goto lands the thread on a new line and pydevd reports it with a
    # `stopped` event, so the outcome is the ordinary resume outcome.
    thread_id = SESSION.current_thread_id
    target_id = targets[0]["id"]
    try:
        return _resume(thread_id, lambda client: client.goto(thread_id, target_id))
    except _dap.DAPError as e:
        return Error(str(e))


@contextlib.contextmanager
def control(thread: int | None = None, *, all: bool = False):
    """Hold the thread control right across a sequence of commands.

        with control():         # the current thread
            cont()
            bt()
            step()

    Per-command acquisition keeps concurrent callers from corrupting each
    other, but not from interleaving: between `cont()` and `bt()` somebody
    else's `cont()` can land, and the `bt()` then reads a thread they resumed.
    When that matters, say so.

    `thread` defaults to the current one; `all=True` takes the right for every
    thread, which is what a resume that names none needs. Reentrant, so the
    commands inside the block acquire it again for free.

    Scripts and subscription-draining threads want this. The human at the
    prompt does not — they are sequential anyway.
    """
    if all:
        key = None
    else:
        key = SESSION.current_thread_id if thread is None else thread
        if key is None:
            raise PDVPError("no current thread (use threads(), or control(all=True))")

    def announce() -> None:
        print(f"*** waiting for control of {describe_thread(key)}")

    with SESSION.control.hold(key, announce=announce):
        yield
