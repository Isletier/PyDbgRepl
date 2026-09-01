"""Execution control, and the one place a resume is issued.

`resume()` is the single decision point every state-changing command routes
through: `cont`, `step`, `next`, `finish`, `jump`, and the initial stop after
`configurationDone`. One function knows about the execution mode, holds the
thread control right, arms the wait, and turns the resulting stop into a
`StopResult`.

**The caller decides whether a resume blocks, not the mode.** `cont()` blocks
until the thread it named stops, in both modes; `cont(wait=False)` hands back a
`Resumption` to collect later. That is gdb's `continue &`, and it is why nothing
below this module knows whether anyone intends to wait.

The mode decides something else entirely -- what stops. In non-stop a resume
claims one thread; in all-stop it claims every thread, because pydevd discards
the threadId and moves the program. That claim is the `key`: the control right
is keyed on it, the wait matches on it, and the epoch bump covers it.
"""
import contextlib

from pdvp import dap as _dap
from pdvp import events
from pdvp.config import CONFIG
from pdvp.console import StdinPassthrough
from pdvp.session import SESSION
from pdvp.model import Error, ErrorKind, PDVPError, PydevdRefused, SourceLines, Status, StopResult
from pdvp.commands.breakpoints import commit_all
from pdvp.commands.location import current_location

__all__ = ["cont", "step", "next", "finish", "interrupt", "jump", "control", "non_stop"]


def describe_thread(thread_id: int | None) -> str:
    return "all threads" if thread_id is None else f"thread {thread_id}"


def resume_key(thread_id: int | None) -> int | None:
    """The thread set a resume actually claims.

    In non-stop that is the thread named. In all-stop it is every thread
    whatever we name, because pydevd rewrites the threadId to "*" and resumes
    the program -- so the honest key is `None`, which is already what a resume
    naming no thread passes. Nothing chooses between the two spellings.
    """
    return thread_id if SESSION.non_stop else None


class Resumption:
    """A resume in flight, from `cont(wait=False)` and friends -- gdb's `continue &`.

    The request is out and the thread is moving; the outcome is collected later
    with `.wait()`. The thread control right is *not* held for this object's
    lifetime, only across the send (see doc/architecture.md, §4), so another
    caller may take the thread the moment this is returned.

    A stop that lands while nobody is waiting is announced on the console like
    any other unattended stop; `.wait()` still returns it as this call's result.
    """

    def __init__(self, target: int | None, subscription, record, prefix: str):
        self._target = target
        self._subscription = subscription
        self._record = record
        self._prefix = prefix
        self._result: StopResult | None = None

    @property
    def done(self) -> bool:
        return self._result is not None

    def wait(self) -> StopResult:
        """Block until this resume ends. Idempotent -- the result is cached."""
        if self._result is not None:
            return self._result

        SESSION.begin_blocking(self._record)
        try:
            with self._subscription:
                event = _await_with_stdin(self._subscription)
        finally:
            SESSION.disarm_resume(self._record)

        if isinstance(event, events.Stopped):
            self._result = report_stop(event, prefix=self._prefix)
        else:
            # Exit, termination and disconnection are one ending -- the pydevd
            # connection and any spawned process share one lifetime.
            SESSION.end()
            self._result = StopResult(event, prefix=self._prefix)
        return self._result

    def close(self) -> None:
        """Give up on the outcome, releasing the subscription. Idempotent."""
        if self._result is None:
            SESSION.disarm_resume(self._record)
            self._subscription.close()

    def __repr__(self) -> str:
        if self._result is not None:
            return repr(self._result)
        return f"*** {describe_thread(self._target)} running (use .wait())"


def resume(thread_id: int | None, request, wait: bool = True,
           prefix: str = "") -> StopResult | Resumption | Error:
    """Resume `thread_id` (None meaning all) by sending `request(client)`.

    `wait=True` blocks until the program stops, exits, or the connection drops,
    and returns a resolved `StopResult`. `wait=False` returns a `Resumption`
    and the right is released as soon as the request is on the wire.

    Holding the right from before the send is what makes two callers resuming
    one thread become two serialized runs rather than one run reported to both
    as a success. The subscription is opened before the request goes out,
    because a `stopped` routinely beats the response to the request that caused
    it onto the wire; `SessionEnded` is on every subscription whether asked for
    or not, which is what bounds the wait without a timeout.

    `prefix` is shown before the outcome in the returned StopResult's repr,
    e.g. "continuing" or "launched pid=1234\\nconnected to 127.0.0.1:5678".
    """
    error = SESSION.require_connected()
    if error is not None:
        return error

    key = resume_key(thread_id)

    def announce() -> None:
        print(f"*** waiting for control of {describe_thread(key)}")

    with SESSION.control.hold(key, announce=announce):
        # Re-checked after acquiring, not before: whoever held the right may
        # have run this thread somewhere else entirely, or ended the session,
        # while we waited. A resume naming no thread has nothing to check --
        # the handshake's initial one happens before anything has stopped.
        error = SESSION.require_connected()
        if error is None and thread_id is not None:
            error = SESSION.require_stopped(thread_id)
        if error is not None:
            return error

        resumption = _issue(key, request, prefix, blocking=wait)
        if isinstance(resumption, Error):
            return resumption
        if not wait:
            return resumption
        return resumption.wait()


def _issue(key: int | None, request, prefix: str, blocking: bool) -> Resumption | Error:
    """Arm the wait, then send. Never the other way round (P5)."""
    subscription = SESSION.bus.subscribe(
        events.Stopped,
        match=lambda event: key is None or event.all_threads or event.thread_id == key)
    record = SESSION.arm_resume(key, blocking=blocking)

    # Bumped before the request goes out. An early bump costs a spurious "frame
    # is stale" if the send fails; a late one hands back wrong data silently.
    # From here on a Ctrl+C also has something to interrupt.
    SESSION.note_resume(key)
    try:
        request(SESSION.client)
    except _dap.DAPError as e:
        SESSION.undo_resume(key)
        SESSION.disarm_resume(record)
        subscription.close()
        return PydevdRefused(str(e), cause=e)
    except BaseException:
        SESSION.undo_resume(key)
        SESSION.disarm_resume(record)
        subscription.close()
        raise

    return Resumption(key, subscription, record, prefix)


def _await_with_stdin(subscription) -> events.Event:
    """Block for the outcome, forwarding our stdin to the inferior meanwhile.

    Only when the inferior's stdin is still the owned-PTY-pair slave: not under
    --pty, not redirected via stdin=, and not for connect()-only sessions where
    we hold no fd to the debuggee's stdio. See doc/io_model.md.
    """
    passthrough = None
    if (
        SESSION.process is not None
        and SESSION.process.master_fd is not None
        and SESSION.process.stdin_is_pty
    ):
        passthrough = StdinPassthrough(SESSION.process.master_fd)
        passthrough.start()

    try:
        return subscription.get()
    finally:
        if passthrough is not None:
            passthrough.stop()


def _source_line_at(top: dict | None) -> SourceLines | None:
    """The single line report_stop() stopped at, gdb-style -- one line, not a
    window (use ls() for that on demand). Degrades to None rather than
    failing the stop: a remote target, a deleted file, or a frame with no
    source info at all are all "no line to show", not an error.
    """
    if top is None:
        return None
    path = (top.get("source") or {}).get("path")
    line = top.get("line")
    if not path or line is None:
        return None
    try:
        with open(path) as f:
            for lineno, text in enumerate(f, start=1):
                if lineno == line:
                    return SourceLines([(line, text.rstrip())], current_line=line)
    except OSError:
        return None
    return None


def report_stop(event: events.Stopped, prefix: str = "") -> StopResult:
    """Turn the stop that ended a wait into the caller's result.

    Moves *this context's* cursor to the thread that stopped, and nothing else
    moves it. The wait matched on the resume's own claim, so this is the thread
    the caller was waiting for: in non-stop that is the thread they named, and
    in all-stop the resume claimed every thread, so a stop landing on a
    different one is a real thread switch and is announced.

    The frame cursor always resets to the new top frame -- frame handles are
    epoch-scoped, and the resume that just ended invalidated the old one.
    """
    previous = SESSION.current_thread_id
    SESSION.current_thread_id = event.thread_id

    lines = [prefix] if prefix else []
    if event.thread_id is not None and previous is not None and previous != event.thread_id:
        lines.append(f"[Switching to thread {event.thread_id}]")

    top = None
    if event.thread_id is not None and SESSION.client is not None:
        try:
            frames = SESSION.client.stack_trace(event.thread_id, levels=1).body.stackFrames
            if frames:
                top = frames[0]
                SESSION.current_frame_id = top["id"]
        except _dap.DAPError:
            pass

    return StopResult(event, top_frame=top, prefix="\n".join(lines), source=_source_line_at(top))


# ---- the four resumes ----

def _step(request: str, prefix: str, thread: int | None,
          wait: bool) -> StopResult | Resumption | Error:
    """The four resumes, which differ only in which request they send.

    The one thing checked here is that there is a thread at all -- that is about
    the *argument*, not about run state. The stopped-check lives in resume(),
    after the control right is acquired: doing it first would turn "wait your
    turn" into "thread 2 is running" for a caller queued behind somebody else's
    run, which is the serialization the right exists to give.
    """
    thread_id = SESSION.resolve_thread(thread)
    if thread_id is None:
        return Error("no current thread (use threads())", kind=ErrorKind.NO_CURRENT_THREAD)
    return resume(thread_id, lambda client: getattr(client, request)(thread_id),
                  wait=wait, prefix=prefix)


def cont(*, thread: int | None = None, wait: bool = True) -> StopResult | Resumption | Error:
    """Resume execution and block until the program stops, exits, or terminates.

    `thread` defaults to the caller's cursor. `wait=False` returns a
    `Resumption` instead of blocking -- gdb's `continue &`.
    """
    return _step("continue_", "continuing", thread, wait)


def step(*, thread: int | None = None, wait: bool = True) -> StopResult | Resumption | Error:
    """Step into the next line, descending into calls. Blocks until the step completes."""
    return _step("step_in", "stepping", thread, wait)


def next(*, thread: int | None = None, wait: bool = True) -> StopResult | Resumption | Error:
    """Step over the next line, without descending into calls. Blocks until the step completes."""
    return _step("next", "stepping over", thread, wait)


def finish(*, thread: int | None = None, wait: bool = True) -> StopResult | Resumption | Error:
    """Run until the current function returns. Blocks until it does."""
    return _step("step_out", "finishing", thread, wait)


def jump(line: int, *, thread: int | None = None,
         wait: bool = True) -> StopResult | Resumption | Error:
    """Set the next line to execute in the current frame to `line`, without running it.

    Backed by the DAP gotoTargets/goto requests (supportsGotoTargetsRequest).
    Like gdb's jump, this skips/reruns code without any cleanup of
    skipped statements.
    """
    thread_id = SESSION.resolve_thread(thread)
    err = SESSION.require_stopped(thread_id)
    if err is not None:
        return err

    path, _ = current_location()
    if path is None:
        return Error("no current file", kind=ErrorKind.NO_CURRENT_FILE)

    try:
        targets = SESSION.client.goto_targets({"path": path}, line).body.targets
    except _dap.DAPError as e:
        return PydevdRefused(str(e), cause=e)
    if not targets:
        return Error(f"no jump target at {path}:{line}", kind=ErrorKind.NO_JUMP_TARGET)

    # goto lands the thread on a new line and pydevd reports it with a
    # `stopped` event, so the outcome is the ordinary resume outcome. resume()
    # itself never lets a DAPError from the goto request escape -- _issue()
    # already turns it into PydevdRefused -- so there is nothing left to catch
    # here.
    target_id = targets[0]["id"]
    return resume(thread_id, lambda client: client.goto(thread_id, target_id), wait=wait)


# ---- interrupt ----

def interrupt() -> Status | Error:
    """Pause every thread we believe is running. Ctrl+C routes here.

    One rule in both modes. In all-stop pausing any single thread suspends
    everything, so pausing all of them is redundant in principle -- but it is
    what makes it robust: pydevd's pause is tracer-based, so a thread parked in
    a C-level call never suspends, and "pause the one thread you picked" can
    silently do nothing. Pausing all of them means any one of them landing
    stops the world. In non-stop there is no single current run to interrupt,
    so stop-the-world is the only sensible reading.

    Exempt from the control right, deliberately: ending somebody else's run is
    precisely its job, and pause is idempotent, so it cannot corrupt the armed
    wait of whoever holds the right. That exemption is what keeps Ctrl+C working
    while a run is in flight.

    Returns as soon as the requests are on the wire. What ends a blocked
    resume is the `stopped` event its subscription is already armed for, not
    these responses -- and this runs in a signal handler, on the very thread it
    would otherwise be blocking.
    """
    if SESSION.client is None:
        return Error("not connected (use connect())", kind=ErrorKind.NOT_CONNECTED)

    running = SESSION.running_threads
    if not running:
        return Error("program is not running", kind=ErrorKind.PROGRAM_NOT_RUNNING)

    for thread_id in running:
        try:
            SESSION.client.pause_async(thread_id)
        except _dap.DAPError:
            # One unreachable thread must not stop us pausing the rest; any of
            # them landing is enough in all-stop.
            pass

    return Status(f"interrupting {len(running)} thread(s)")


# ---- execution mode ----

def _mode_name(non_stop: bool) -> str:
    return "non-stop" if non_stop else "all-stop"


def non_stop(enable: bool | None = None) -> Status | Error:
    """Read or set the execution mode.

    all-stop (the default) suspends every thread when one stops; non-stop
    leaves the others running and reports their stops on the console.

    Before a session exists this just records the mode for the next attach. Once
    connected it is a command rather than a plain assignment, because it can
    refuse: the precondition is that **no resume we initiated is in flight**.
    Deliberately not "every thread is suspended" -- a thread blocked in a
    C-level call never suspends, so that spelling would make any program calling
    join() unable to switch modes for its whole life. Use interrupt() to reach
    the precondition.

    A state-changing command like any other (§4), keyed `None` -- the same
    "every thread" key an all-stop resume already uses -- so the precondition
    check and the flip are atomic against a concurrent `cont()`/`step()`/etc.
    Without this, another caller's resume could land between the check and
    `set_debugger_property()`, producing exactly the hybrid
    (breakpoints stop one thread, steps resume everything) the two-step
    set-then-recommit procedure below exists to prevent.
    """
    if enable is None:
        return Status(_mode_name(SESSION.non_stop))

    enable = bool(enable)

    if SESSION.client is None:
        CONFIG.non_stop = enable
        return Status(f"{_mode_name(enable)} (applied at the next run())")

    def announce() -> None:
        print(f"*** waiting for control of {describe_thread(None)}")

    with SESSION.control.hold(None, announce=announce):
        # Re-checked after acquiring, not before (same rule as resume()):
        # whoever held the right may have ended the session while this call
        # was blocked waiting for it.
        error = SESSION.require_connected()
        if error is not None:
            return error
        if SESSION.resume_in_flight:
            return Error("a resume is in flight (use interrupt(), then try again)",
                         kind=ErrorKind.RESUME_IN_FLIGHT)

        try:
            SESSION.client.set_debugger_property(multi_threads_single_notification=not enable)
        except _dap.DAPError as e:
            return PydevdRefused(str(e), cause=e)

        SESSION.non_stop = enable
        CONFIG.non_stop = enable

        # pydevd stamps each breakpoint's suspend policy from the mode when the
        # breakpoint is installed, so breakpoints set before the flip keep the old
        # behaviour until they are sent again -- new stops reported one way, old
        # breakpoints suspending the other.
        err = commit_all()
        if err is not None:
            return err

    return Status(f"{_mode_name(enable)}")


# ---- explicit sequences ----

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
    prompt does not -- they are sequential anyway.
    """
    if all or not SESSION.non_stop:
        # In all-stop every resume claims the whole program, so the per-thread
        # key would be a right nothing else ever contends for.
        key = None
    else:
        key = SESSION.resolve_thread(thread)
        if key is None:
            raise PDVPError("no current thread (use threads(), or control(all=True))")

    def announce() -> None:
        print(f"*** waiting for control of {describe_thread(key)}")

    with SESSION.control.hold(key, announce=announce):
        yield
