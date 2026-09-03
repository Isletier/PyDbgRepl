"""Real-pydevd integration tests for multithreading, execution-mode, interrupt
and stdin-passthrough behavior that a FakeClient unit test can't reach --
actual inferior thread scheduling and real pydevd notification timing.

Two access patterns, matched to what each test actually needs to prove:

  * `session()`/`attach_and_configure()` (this package's `helpers.py`), the
    same raw Client+bus level `test_client_events.py`/`test_dap_client.py`
    use, for anything that only needs to observe pydevd's own wire behavior.
  * `real_run()` (below), which drives the real command layer against the
    real `pdvp.core.session.SESSION` singleton via `pdvp.core.commands.lifecycle.run()`
    -- the actual production entry point -- for anything that specifically
    claims to test *pdvp's* behavior (interrupt(), non_stop(), the cursor)
    rather than pydevd's.

`SESSION`/`CONFIG` are process-wide singletons, unlike `pdvp/core/test/test_execution.py`'s
FakeClient tests, which patch a private one in for the duration of each test.
Real pydevd can't be faked, so `real_run()` resets both around its block
instead, including a `SESSION.Breakpoints` clear -- program-lifetime state
`end_connection()` deliberately leaves alone (breakpoints outlive one
session, gdb-style), which would otherwise leak a test's breakpoints into the
next one that runs in this process.

No test framework dependency, matching every other file in this package:
bare `assert`, `test_*` functions, a `TESTS` list, a `main()` runner. pytest
collects these directly too.

Slower and more timing-sensitive than the rest of the suite by nature -- real
process spawns, real thread scheduling, real pydevd notification timers.

Run from the repo root with the venv active:

    python -m pdvp.core.dap.test.test_multithreading
"""
import contextlib
import importlib
import os
import pty
import select
import sys
import termios
import threading
import time

from pdvp.core import events
from pdvp.core.config import CONFIG
from pdvp.core.schema import pydevd_schema as schema
from pdvp.core.session import SESSION
from pdvp.core.dap.test.helpers import attach_and_configure, session

# importlib, not `from ...commands import execution`: `commands/__init__.py`
# does `from .breakpoints import *`, which shadows the *package's*
# `breakpoints` attribute with the `breakpoints()` command function of the
# same name -- see pdvp/test/test_execution.py's docstring for the full story.
execution = importlib.import_module("pdvp.core.commands.execution")
lifecycle = importlib.import_module("pdvp.core.commands.lifecycle")
bp_cmds = importlib.import_module("pdvp.core.commands.breakpoints")
stack = importlib.import_module("pdvp.core.commands.stack")
inspection = importlib.import_module("pdvp.core.commands.inspection")

TARGETS = os.path.join(os.path.dirname(__file__), "targets")
LOOP = os.path.join(TARGETS, "loop.py")
TWO_THREADS = os.path.join(TARGETS, "two_threads.py")
BUSY = os.path.join(TARGETS, "busy.py")
ECHO = os.path.join(TARGETS, "echo.py")

ALPHA_LINE = 20  # `marker = i` in two_threads.py's alpha()
BETA_LINE = 28   # `beta_counter = i` in two_threads.py's beta()

# Real process spawns and real pydevd notification timers are slower than the
# in-process FakeClient suite; generous but still bounded, matching
# helpers.ACCEPT_TIMEOUT and test_dap_client.py's WAIT.
WAIT = 10


def _wait_until(predicate, timeout: float = WAIT, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _has_data(fd: int) -> bool:
    ready, _, _ = select.select([fd], [], [], 0)
    return bool(ready)


def _fd0_left_canonical_mode() -> bool:
    """Whether fd 0 is out of canonical mode -- the actual signal that
    `StdinPassthrough.start()` has run its `tty.setcbreak(0)`, not a guess."""
    try:
        return not (termios.tcgetattr(0)[3] & termios.ICANON)
    except termios.error:
        return False


@contextlib.contextmanager
def real_run(target_path: str, *args: str, non_stop: bool = False):
    """Drive the real command layer against the real SESSION singleton.

    Sets `CONFIG.file`/`args`/`non_stop` and yields; the caller sets any
    breakpoints (via `bp_cmds.sbreak()`, before calling `lifecycle.run()` --
    they queue in `SESSION.Breakpoints` and get committed at attach) and
    drives the session itself. Tears the session down and resets both
    process-wide singletons on the way out, on any exit path.
    """
    CONFIG.reset()
    CONFIG.file = target_path
    CONFIG.args = list(args)
    CONFIG.non_stop = non_stop
    try:
        yield
    finally:
        if SESSION.client is not None or SESSION.process is not None:
            lifecycle.stop()
        SESSION.Breakpoints.clear()
        CONFIG.reset()


# ---- all-stop: does pydevd (and pdvp's interrupt()) really suspend both? ----

def test_all_stop_suspends_the_other_thread_too() -> None:
    """A breakpoint in one thread, in all-stop, genuinely halts the other --
    not just an `allThreadsStopped` flag pydevd reports without meaning it.

    Attaches by hand rather than through `attach_and_configure()`:
    `stopAllThreadsOnSuspend` isn't one of that helper's defaults (pydevd's
    own default is per-thread stop), so this pins it explicitly, matching
    what `lifecycle._handshake()` actually sends in all-stop mode.
    """
    with session(TWO_THREADS) as (client, bus):
        with bus.subscribe(events.Initialized) as initialized, \
             bus.subscribe(events.Stopped) as stops:
            client.initialize()
            client.attach(stopAllThreadsOnSuspend=True, steppingResumesAllThreads=False)
            assert isinstance(initialized.get(timeout=WAIT), events.Initialized)

            client.set_breakpoints(schema.Source(path=TWO_THREADS),
                                   [schema.SourceBreakpoint(line=ALPHA_LINE)])
            client.set_exception_breakpoints([], [], [])
            client.configuration_done()

            stopped = stops.get(timeout=WAIT)

        assert stopped.reason == "breakpoint", stopped
        assert stopped.all_threads, stopped
        thread_id = stopped.thread_id

        frame_id = client.stack_trace(thread_id).body.stackFrames[0]["id"]
        first = client.evaluate("beta_counter", frame_id=frame_id, context="repl").body.result
        time.sleep(0.3)
        second = client.evaluate("beta_counter", frame_id=frame_id, context="repl").body.result
        assert first == second, ("beta kept advancing while all-stop was supposedly in effect", first, second)

        client.continue_(thread_id)


def test_interrupt_pauses_every_thread_in_all_stop() -> None:
    """execution.interrupt() names no thread but stops the whole program,
    matching its docstring: robust against a single pause landing on only one
    of several running threads."""
    with real_run(TWO_THREADS):
        result_holder: dict = {}

        def go() -> None:
            result_holder["result"] = lifecycle.run()

        runner = threading.Thread(target=go, daemon=True)
        runner.start()
        try:
            assert _wait_until(lambda: SESSION.client is not None and len(SESSION.running_threads) >= 2), \
                "both worker threads never registered as running"
            status = execution.interrupt()
            assert status, status
            runner.join(timeout=WAIT)
            assert not runner.is_alive(), "run() never returned after interrupt()"
        finally:
            runner.join(timeout=1)

        result = result_holder.get("result")
        assert result is not None and result.stopped, result
        assert result.reason == "pause", result

        for state in SESSION.threads:
            assert state.stopped, ("not every thread stopped", state, SESSION.threads)

        execution.cont(wait=False).close()


# ---- non-stop: does the other thread actually keep running? ----

def test_non_stop_leaves_the_other_thread_running() -> None:
    with real_run(TWO_THREADS, non_stop=True):
        bp_cmds.sbreak(TWO_THREADS, ALPHA_LINE)
        result = lifecycle.run()
        assert result.stopped, result
        assert result.reason == "breakpoint", result
        assert result.event.thread_id is not None, result

        first = str(inspection.p("beta_counter"))
        assert not first.startswith("error:"), first
        time.sleep(0.3)
        second = str(inspection.p("beta_counter"))
        assert first != second, ("beta did not advance while stopped in non-stop mode", first, second)

        execution.cont(wait=False).close()


def test_pydevds_pause_timer_can_report_all_threads_stopped_in_non_stop() -> None:
    """pydevd's pause-timer notification (AbstractSingleNotificationBehavior.
    _notify_after_timeout) can report allThreadsStopped=True for a lone
    pause() even in non-stop mode -- the raw quirk `session.py`'s
    `_on_stopped` corrects at the pdvp layer, tested there with a synthetic
    event. This only confirms the raw wire behavior it guards against is
    real, on a best-effort basis: pydevd's exact timing isn't ours to
    control, so it observes and reports rather than asserting either way.
    """
    with session(LOOP) as (client, bus):
        attach_and_configure(client, bus)
        client.set_debugger_property(multi_threads_single_notification=False)

        threads = client.threads().body.threads
        thread_id = threads[0]["id"]

        with bus.subscribe(events.Stopped) as stops:
            client.pause(thread_id)
            stopped = stops.get(timeout=WAIT)

        assert stopped.reason == "pause", stopped
        if stopped.all_threads:
            print("observed: raw pydevd pause notification set allThreadsStopped=True in non-stop mode")
        else:
            print("not observed this run: pydevd reported allThreadsStopped=False for this pause")

        client.continue_(thread_id)


def test_two_client_threads_have_independent_cursors_against_one_live_session() -> None:
    """Two real OS threads on the *test* side, both issuing commands against
    one live session concurrently -- confirms cursor.scope()'s default
    (threading.current_thread()) isolates real client threads under real
    socket-IO/GIL interleaving, not just the simulated version in
    pdvp/test/test_execution.py."""
    with real_run(TWO_THREADS, non_stop=True):
        bp_cmds.sbreak(TWO_THREADS, ALPHA_LINE)
        bp_cmds.sbreak(TWO_THREADS, BETA_LINE)

        # Armed before configurationDone triggers either thread (P5): both
        # breakpoints fire independently and close together, so a
        # subscription opened only after run() returns could already have
        # missed the second one.
        with SESSION.bus.subscribe(events.Stopped) as stops:
            result = lifecycle.run()
            assert result.stopped and result.reason == "breakpoint", result
            first_tid = result.event.thread_id

            second_tid = None
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline:
                event = stops.get(timeout=max(0.1, deadline - time.monotonic()))
                if isinstance(event, events.Stopped) and event.thread_id not in (None, first_tid):
                    second_tid = event.thread_id
                    break
            assert second_tid is not None, "the second thread never hit its own breakpoint"

        results: dict = {}
        barrier = threading.Barrier(2)

        def pick(name: str, tid: int) -> None:
            barrier.wait(timeout=WAIT)
            stack.thread(tid)
            stack.bt()
            results[name] = SESSION.current_thread_id

        pickers = [
            threading.Thread(target=pick, args=("first", first_tid)),
            threading.Thread(target=pick, args=("second", second_tid)),
        ]
        for p in pickers:
            p.start()
        for p in pickers:
            p.join(timeout=WAIT)

        assert results.get("first") == first_tid, results
        assert results.get("second") == second_tid, results

        execution.cont(thread=first_tid, wait=False).close()
        execution.cont(thread=second_tid, wait=False).close()


# ---- interrupt() against a thread with no I/O to block on ----

def test_interrupt_catches_a_busy_thread_at_a_traced_call_boundary() -> None:
    """interrupt()'s own docstring flags the limit: a thread parked in a
    C-level call never suspends, since pause is tracer-based. `busy.py` has
    no C-level call to hide in -- only Python function calls, which are
    traced boundaries -- so this confirms the ordinary case actually works,
    not the documented exception to it."""
    with real_run(BUSY):
        result_holder: dict = {}

        def go() -> None:
            result_holder["result"] = lifecycle.run()

        runner = threading.Thread(target=go, daemon=True)
        runner.start()
        try:
            assert _wait_until(lambda: SESSION.client is not None and len(SESSION.running_threads) >= 1), \
                "the busy thread never registered as running"
            status = execution.interrupt()
            assert status, status
            runner.join(timeout=WAIT)
            assert not runner.is_alive(), "interrupt() never caught the busy thread within the wait"
        finally:
            runner.join(timeout=1)

        result = result_holder.get("result")
        assert result is not None and result.stopped, result
        assert result.reason == "pause", result

        execution.cont(wait=False).close()


# ---- stdin passthrough through a real pty ----

def test_stdin_passthrough_reaches_the_inferior_through_a_real_pty() -> None:
    """StdinPassthrough only activates when sys.stdin.isatty() is true (fd 0,
    hardcoded -- console.py), so this redirects this process's fd 0 to a
    fresh pty slave and fd 1 to a capture pipe for the duration of the test,
    and restores both unconditionally, even on failure.

    Under pytest's default capture, `sys.stdin` is pytest's own non-tty
    stand-in, not a view onto the real fd 0 -- redirecting the raw fd alone
    leaves `sys.stdin.isatty()` false, `StdinPassthrough` never starts, and
    the target hangs forever on `readline()` (found by running this under
    `pytest -q` -- it hung the whole process, since the still-blocked runner
    thread below is not a daemon). `sys.stdin` is reassigned explicitly here
    for the same reason the raw fd is, and restored the same way. The
    `runner.join()`+explicit-kill fallback is defense in depth against
    exactly that failure mode recurring some other way.
    """
    my_master, my_slave = pty.openpty()
    saved_fd0 = os.dup(0)
    saved_fd1 = os.dup(1)
    saved_stdin = sys.stdin
    out_r, out_w = os.pipe()
    result_holder: dict = {}

    try:
        os.dup2(my_slave, 0)
        os.dup2(out_w, 1)
        sys.stdin = os.fdopen(os.dup(0), "r", closefd=True)

        def go() -> None:
            with real_run(ECHO):
                result_holder["result"] = lifecycle.run()

        runner = threading.Thread(target=go, daemon=True)
        runner.start()
        try:
            assert _wait_until(lambda: SESSION.process is not None and SESSION.process.master_fd is not None), \
                "the target never spawned"
            # Wait for the actual signal that StdinPassthrough.start() has run,
            # not a guessed sleep: tty.setcbreak()'s default TCSAFLUSH discards
            # whatever is already queued on fd 0 the moment it switches modes,
            # so writing before that switch (a real race under load -- the
            # handshake before this point is several DAP round trips) would
            # silently vanish instead of being delivered late, and the target
            # would then hang on readline() until the join below times out.
            assert _wait_until(_fd0_left_canonical_mode), \
                "StdinPassthrough never switched fd 0 out of canonical mode"
            os.write(my_master, b"hello there\n")
            runner.join(timeout=WAIT)
            if runner.is_alive():
                # The wait never resolved on its own -- force the death path
                # (P4) rather than leave a non-daemon thread, or a
                # daemon-but-orphaned pydevd child, hanging around.
                if SESSION.process is not None:
                    SESSION.process.child.kill()
                runner.join(timeout=WAIT)
            assert not runner.is_alive(), "run() never returned, even after killing the child"
            # Give pump_output's daemon thread a moment to drain the
            # inferior's last output before fd 1 is redirected back.
            time.sleep(0.3)
        finally:
            runner.join(timeout=1)
    finally:
        sys.stdin.close()
        sys.stdin = saved_stdin
        os.dup2(saved_fd0, 0)
        os.dup2(saved_fd1, 1)
        os.close(saved_fd0)
        os.close(saved_fd1)
        os.close(my_master)
        os.close(my_slave)
        os.close(out_w)

    captured = b""
    while _has_data(out_r):
        chunk = os.read(out_r, 4096)
        if not chunk:
            break
        captured += chunk
    os.close(out_r)

    assert result_holder.get("result") is not None, "target never finished"
    assert b"echo: hello there" in captured, captured


TESTS = [value for name, value in sorted(globals().items()) if name.startswith("test_")]


def main() -> int:
    failures = 0
    for test in TESTS:
        try:
            test()
        except Exception as error:
            failures += 1
            print(f"FAIL {test.__name__}: {type(error).__name__}: {error}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
