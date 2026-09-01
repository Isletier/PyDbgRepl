"""Unit tests for the command layer: resume(), Resumption, interrupt(),
non_stop(), and the control-right races between them.

No pydevd, no sockets: a `FakeClient` stands in for `dap.Client` (recording
every call it receives instead of talking to a socket), and DAP events are fed
straight into `Session.reduce()` the same way `test_session.py` does, from a
background thread when a test wants to simulate the reader thread reporting a
stop asynchronously.

Every `commands/*.py` submodule binds `SESSION` at import time via
`from ..session import SESSION`. Rather than hand-maintain a list of which
ones do (easy to under-count -- `location.py` binds it too, for `jump()`'s
`current_location()`, and isn't itself re-exported by `commands/__init__.py`),
`_new_session_env()` scans `sys.modules` for every module currently bound to
the real singleton and patches all of them for the duration of a test. Same
trick `unittest.mock.patch` uses, done by hand to avoid a new dependency, and
self-updating if another command module starts importing `SESSION` later.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all of
them. pytest also collects these directly by name.

Run from the repo root with the venv active:

    python -m pdvp.test.test_execution
"""
import importlib
import os
import sys
import tempfile
import threading
import time

# `pdvp.commands`' __init__.py does `from .breakpoints import *`, which shadows
# the *package's* `breakpoints` attribute with the `breakpoints()` command
# function (same name, in its __all__) -- a plain `import ... as` would grab
# that function instead of the module. importlib.import_module() resolves via
# sys.modules and isn't affected by the parent package's own attributes.
# Importing `execution` transitively imports every module these tests touch
# (breakpoints, location, lifecycle, stack, ...), which is what makes the
# sys.modules scan below find them all.
execution = importlib.import_module("pdvp.commands.execution")
lifecycle = importlib.import_module("pdvp.commands.lifecycle")
stack = importlib.import_module("pdvp.commands.stack")
from pdvp import dap as _dap
from pdvp.model import Error, ErrorKind, SourceLines, StaleFrameError, Status, StopResult
from pdvp.session import SESSION as _REAL_SESSION
from pdvp.session import Session

# ---------------------------------------------------------------- fakes

class _Message:
    """What Client hands the reducer for a DAP event -- same shape test_session.py uses."""

    def __init__(self, event: str, body):
        self.event = event
        self.body = body


class _Body:
    """A response body: attribute access over a dict, like the real schema objects."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Response:
    def __init__(self, success: bool = True, message: str | None = None, **body_kw):
        self.success = success
        self.message = message
        self.body = _Body(**body_kw)


class FakeClient:
    """Stands in for `dap.Client`. Records every call; a test drives outcomes
    by feeding events into `Session.reduce()` directly, not through this
    object -- the fake models the wire, not the reader thread.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.calls: list[tuple[str, tuple, dict]] = []
        self.raise_on: dict[str, Exception] = {}

    def _record(self, name, *args, **kwargs):
        with self._lock:
            self.calls.append((name, args, kwargs))
        failure = self.raise_on.pop(name, None)
        if failure is not None:
            raise failure

    def count(self, name: str) -> int:
        with self._lock:
            return sum(1 for call in self.calls if call[0] == name)

    def calls_for(self, name: str) -> list[tuple]:
        with self._lock:
            return [call for call in self.calls if call[0] == name]

    # ---- execution control

    def continue_(self, thread_id, single_thread=False):
        self._record("continue_", thread_id, single_thread=single_thread)
        return _Response(allThreadsContinued=False)

    def next(self, thread_id, single_thread=False, granularity=None):
        self._record("next", thread_id)
        return _Response()

    def step_in(self, thread_id, single_thread=False, target_id=None, granularity=None):
        self._record("step_in", thread_id)
        return _Response()

    def step_out(self, thread_id, single_thread=False, granularity=None):
        self._record("step_out", thread_id)
        return _Response()

    def pause_async(self, thread_id):
        self._record("pause_async", thread_id)
        return None

    def set_debugger_property(self, **kwargs):
        self._record("set_debugger_property", **kwargs)
        return _Response()

    def close(self) -> None:
        self._record("close")

    def goto_targets(self, source, line, column=None):
        self._record("goto_targets", line)
        return _Response(targets=[{"id": 1}])

    def goto(self, thread_id, target_id):
        self._record("goto", thread_id, target_id)
        return _Response()

    # ---- reads

    def stack_trace(self, thread_id, start_frame=None, levels=None):
        self._record("stack_trace", thread_id)
        return _Response(stackFrames=[{
            "id": thread_id * 1000, "name": "frame0", "line": 1, "source": {"path": "t.py"},
        }])

    # ---- breakpoints (commit_all() always calls set_function_breakpoints,
    # even with none configured)

    def set_function_breakpoints(self, fbreakpoints):
        self._record("set_function_breakpoints", len(fbreakpoints))
        return _Response(breakpoints=[])

    def set_breakpoints(self, source, sbreakpoints):
        self._record("set_breakpoints", len(sbreakpoints))
        return _Response(breakpoints=[{"verified": True, "line": b.line, "source": {}}
                                       for b in sbreakpoints])


def _session_bound_modules() -> list:
    """Every currently-loaded module whose `SESSION` name is the real
    singleton -- found by scanning, not by a maintained list, so a new
    command module that starts importing SESSION is covered automatically."""
    return [m for name, m in sys.modules.items()
            if name.startswith("pdvp.") and name != "pdvp.session"
            and getattr(m, "SESSION", None) is _REAL_SESSION]


def _new_session_env():
    """A fresh Session, wired into every command module that binds SESSION at
    import time. Returns (session, client, restore) -- call restore() when done."""
    session = Session()
    client = FakeClient()
    modules = _session_bound_modules()
    originals = [(m, m.SESSION) for m in modules]

    def restore() -> None:
        for m, original in originals:
            m.SESSION = original

    for m in modules:
        m.SESSION = session
    return session, client, restore


def _in_a_thread(function) -> threading.Thread:
    thread = threading.Thread(target=function, daemon=True)
    thread.start()
    return thread


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _connected(session: Session, client: FakeClient, *thread_ids: int, non_stop: bool = False) -> None:
    """A live-looking connection with `thread_ids` announced and stopped."""
    session.begin(client=client)
    session.non_stop = non_stop
    for tid in thread_ids:
        session.reduce(_Message("thread", {"threadId": tid, "reason": "started"}))
    for tid in thread_ids:
        session.reduce(_Message("stopped", {
            "threadId": tid, "reason": "breakpoint", "allThreadsStopped": not non_stop,
        }))


def _stop(session: Session, thread_id: int, reason: str = "breakpoint", all_threads: bool = False) -> None:
    session.reduce(_Message("stopped", {
        "threadId": thread_id, "reason": reason, "allThreadsStopped": all_threads,
    }))


# ================================================================== resume()

def test_cont_blocks_until_the_stop_and_returns_a_stop_result() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)

        _in_a_thread(lambda: (time.sleep(0.05), _stop(session, 1)))
        result = execution.cont(thread=1)

        assert isinstance(result, StopResult), result
        assert result.reason == "breakpoint"
        assert client.count("continue_") == 1
        assert client.calls_for("continue_")[0][1] == (1,)
        assert session.current_thread_id == 1
    finally:
        restore()


def test_stop_result_source_is_none_when_the_file_is_not_reachable() -> None:
    """FakeClient's stack_trace() always reports source path "t.py", which
    does not exist relative to the test process's cwd -- the default case
    every other test in this file already exercises without noticing.
    Pinned explicitly: a missing file must degrade to `source=None`, not
    raise and fail the stop."""
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)
        _in_a_thread(lambda: (time.sleep(0.05), _stop(session, 1)))
        result = execution.cont(thread=1)

        assert isinstance(result, StopResult), result
        assert result.source is None
    finally:
        restore()


def test_stop_result_carries_the_single_source_line_when_reachable() -> None:
    session, client, restore = _new_session_env()
    before = os.getcwd()
    tmpdir = tempfile.TemporaryDirectory()
    try:
        with open(os.path.join(tmpdir.name, "t.py"), "w") as f:
            f.write("first\nsecond\nthird\n")
        os.chdir(tmpdir.name)

        _connected(session, client, 1, non_stop=True)
        _in_a_thread(lambda: (time.sleep(0.05), _stop(session, 1)))
        result = execution.cont(thread=1)

        assert isinstance(result, StopResult), result
        # FakeClient's stack_trace() always reports line 1.
        assert isinstance(result.source, SourceLines), result.source
        assert list(result.source) == [(1, "first")], result.source
        assert result.source.current_line == 1
        assert "first" in repr(result)
    finally:
        os.chdir(before)
        tmpdir.cleanup()
        restore()


def test_cont_wait_false_releases_the_right_immediately_and_wait_is_idempotent() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)

        resumption = execution.cont(thread=1, wait=False)
        assert isinstance(resumption, execution.Resumption)
        assert not resumption.done
        # wait=False: the right is released the instant the send is on the
        # wire, before the caller ever sees the Resumption (§4).
        assert session.control.holder_of(1) is None

        _stop(session, 1)
        first = resumption.wait()
        second = resumption.wait()
        assert isinstance(first, StopResult)
        assert first is second, "a second .wait() must return the cached result, not re-resolve"
    finally:
        restore()


def test_step_next_finish_route_through_resume_with_the_right_client_call() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)

        for command, wire_method in (
            (execution.step, "step_in"),
            (execution.next, "next"),
            (execution.finish, "step_out"),
        ):
            before = client.count(wire_method)
            _in_a_thread(lambda: (time.sleep(0.03), _stop(session, 1)))
            result = command(thread=1)
            assert isinstance(result, StopResult), (command, result)
            assert client.count(wire_method) == before + 1, wire_method
    finally:
        restore()


def test_jump_resolves_a_goto_target_and_reports_a_stop() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)
        stack.frame(0)  # jump() resolves the target file from the current frame

        _in_a_thread(lambda: (time.sleep(0.03), _stop(session, 1, reason="goto")))
        result = execution.jump(5, thread=1)

        assert isinstance(result, StopResult), result
        assert client.count("goto_targets") == 1
        assert client.count("goto") == 1
    finally:
        restore()


def test_cont_against_a_running_thread_is_a_clean_error_not_a_hang() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)
        _stop(session, 1)  # arrives while nobody claimed the resume: thread 1 running
        # thread 1 is running now (no pending_resume, the _stop() above just
        # re-reports the same stop -- flip it running via a continued event):
        session.reduce(_Message("continued", {"threadId": 1, "allThreadsContinued": False}))

        result = execution.cont(thread=1)
        assert isinstance(result, Error), result
        assert "running" in result
        assert client.count("continue_") == 0, "must not have sent a request for a running thread"
    finally:
        restore()


# ============================================================ control right

def test_two_callers_on_one_thread_serialize_through_resume() -> None:
    """The point of the right, exercised through the real command surface, not
    just ControlRights directly."""
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)

        results: dict[str, StopResult] = {}
        _in_a_thread(lambda: results.__setitem__("a", execution.cont(thread=1)))
        assert _wait_until(lambda: client.count("continue_") >= 1), "caller A never sent its resume"

        _in_a_thread(lambda: results.__setitem__("b", execution.cont(thread=1)))
        time.sleep(0.15)
        assert client.count("continue_") == 1, "caller B raced ahead of A's still-live resume"
        assert "a" not in results and "b" not in results

        _stop(session, 1)  # resolves A, hands the right to B
        assert _wait_until(lambda: "a" in results), "A's cont() never returned"
        assert _wait_until(lambda: client.count("continue_") >= 2), "B never got its turn"

        _stop(session, 1)  # resolves B
        assert _wait_until(lambda: "b" in results), "B's cont() never returned"

        assert isinstance(results["a"], StopResult) and isinstance(results["b"], StopResult)
    finally:
        restore()


def test_all_stop_serializes_different_named_threads_through_one_key() -> None:
    """pydevd discards the threadId in all-stop and moves everything (§8), so
    two callers naming *different* threads still contend for the same right --
    unlike non-stop, where they would not."""
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, 2, non_stop=False)

        results: dict[str, StopResult] = {}
        _in_a_thread(lambda: results.__setitem__("a", execution.cont(thread=1)))
        assert _wait_until(lambda: client.count("continue_") >= 1)

        _in_a_thread(lambda: results.__setitem__("b", execution.cont(thread=2)))
        time.sleep(0.15)
        assert client.count("continue_") == 1, "a different named thread should not bypass the global key"

        _stop(session, 1, all_threads=True)
        assert _wait_until(lambda: "a" in results)
        assert _wait_until(lambda: client.count("continue_") >= 2)

        _stop(session, 2, all_threads=True)
        assert _wait_until(lambda: "b" in results)
    finally:
        restore()


def test_interrupt_bypasses_a_held_right_and_does_not_block() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)

        results: dict[str, StopResult] = {}
        _in_a_thread(lambda: results.__setitem__("a", execution.cont(thread=1)))
        assert _wait_until(lambda: client.count("continue_") >= 1)

        # thread 1 is running and its right is held by caller A right now;
        # interrupt() must reach it anyway, without waiting.
        result = execution.interrupt()
        assert isinstance(result, Status) and not isinstance(result, Error), result
        assert client.count("pause_async") == 1
        assert client.calls_for("pause_async")[0][1] == (1,)

        _stop(session, 1, reason="pause")
        assert _wait_until(lambda: "a" in results)
    finally:
        restore()


def test_interrupt_with_nothing_running_is_an_error() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)
        result = execution.interrupt()
        assert isinstance(result, Error), result
        assert client.count("pause_async") == 0
    finally:
        restore()


# ================================================================ non_stop()

def test_non_stop_blocks_behind_a_live_resume_then_succeeds() -> None:
    """Regression test for the control-right fix.

    Before the fix, non_stop() read SESSION.resume_in_flight as an unguarded
    snapshot and could flip the mode while another caller's resume was
    genuinely live, landing the exact "breakpoints stop one thread, steps
    resume everything" hybrid the two-step switch procedure exists to
    prevent. After the fix it acquires the same control right (key=None)
    every other state-changing command does, so it blocks -- and, per
    doc/architecture.md §4 "Usability", *succeeds* once the live resume
    resolves, rather than failing fast the way it used to when contended.
    That's a deliberate behavior change: see the note below for the case
    where it still refuses outright.
    """
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=False)

        resumed: list[StopResult] = []
        _in_a_thread(lambda: resumed.append(execution.cont(thread=1)))
        assert _wait_until(lambda: client.count("continue_") >= 1), "A never sent its resume"

        switched: list[Status] = []
        _in_a_thread(lambda: switched.append(execution.non_stop(True)))
        time.sleep(0.15)
        assert not switched, "non_stop() must not flip the mode while A's resume is live"
        assert client.count("set_debugger_property") == 0

        _stop(session, 1, all_threads=True)  # resolves A, hands the right to non_stop()
        assert _wait_until(lambda: resumed), "A's cont() never returned"
        assert _wait_until(lambda: switched), "non_stop() never got its turn after A released"

        assert isinstance(switched[0], Status) and not isinstance(switched[0], Error), switched[0]
        assert session.non_stop is True
        assert client.count("set_debugger_property") == 1
        assert client.count("set_function_breakpoints") == 1, "commit_all() must run after the flip"
    finally:
        restore()


def test_non_stop_still_refuses_a_detached_wait_false_resume() -> None:
    """The one case that still produces an immediate Error: a wait=False
    resume that already sent (and released its right) but has not been
    .wait()-ed yet -- SESSION.resume_in_flight is true, but nothing is held,
    so non_stop() acquires freely and then finds it via the bookkeeping
    check, exactly as before the fix."""
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=False)

        resumption = execution.cont(thread=1, wait=False)
        assert not resumption.done
        assert session.control.holder_of(None) is None, "wait=False releases the right after the send"

        result = execution.non_stop(True)
        assert isinstance(result, Error), result
        assert "resume is in flight" in result
        assert client.count("set_debugger_property") == 0

        resumption.close()
    finally:
        restore()


def test_non_stop_re_checks_the_connection_after_the_right_is_acquired() -> None:
    """Regression test for a TOCTOU gap: non_stop() checked `SESSION.client is
    None` *before* acquiring the control right, never after -- unlike
    resume(), which re-checks require_connected() once it actually holds the
    right, precisely because "whoever held the right may have... ended the
    session, while we waited." A disconnect landing while non_stop() was
    blocked behind another holder used to reach
    SESSION.client.set_debugger_property() on a None client (AttributeError)
    instead of returning a clean Error.
    """
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=False)

        holder_ready = threading.Event()
        release_holder = threading.Event()

        def hold_the_right() -> None:
            with session.control.hold(None):
                holder_ready.set()
                release_holder.wait(timeout=2.0)

        _in_a_thread(hold_the_right)
        assert holder_ready.wait(timeout=2.0), "holder never acquired the right"

        switched: list = []
        _in_a_thread(lambda: switched.append(execution.non_stop(True)))
        time.sleep(0.1)
        assert not switched, "non_stop() must be blocked behind the held right"

        # The connection dies while non_stop() is still queued for the right --
        # its pre-acquire "SESSION.client is None" check already passed.
        session.end_connection()

        release_holder.set()
        assert _wait_until(lambda: switched), "non_stop() never returned after the right was released"

        result = switched[0]
        assert isinstance(result, Error), result
        assert "not connected" in result, result
        assert client.count("set_debugger_property") == 0
    finally:
        restore()


def test_non_stop_before_connecting_just_records_the_default() -> None:
    session, client, restore = _new_session_env()
    try:
        result = execution.non_stop(True)
        assert isinstance(result, Status) and not isinstance(result, Error), result
        from pdvp.config import CONFIG
        before = CONFIG.non_stop
        try:
            assert CONFIG.non_stop is True
        finally:
            CONFIG.non_stop = before
    finally:
        restore()


def test_non_stop_read_only_reports_the_current_mode() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=False)
        assert execution.non_stop() == "all-stop"
        session.non_stop = True
        assert execution.non_stop() == "non-stop"
    finally:
        restore()


# ============================================================== death mid-resume

def test_connection_death_resolves_a_blocked_resume_exactly_once() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)

        results: list[StopResult] = []
        thread = _in_a_thread(lambda: results.append(execution.cont(thread=1)))
        assert _wait_until(lambda: client.count("continue_") >= 1)

        session.reduce(_dap.ConnectionClosed(deliberate=False, detail="peer gone"))

        assert _wait_until(lambda: results), "the blocked cont() was never woken by the death"
        result = results[0]
        assert isinstance(result, StopResult)
        assert not result.stopped
        assert session.client is None, "SESSION.end() should have run"
        thread.join(2)
    finally:
        restore()


def test_death_with_no_blocked_caller_is_reported_by_the_console_path() -> None:
    """No caller waiting: lifecycle._dispatch, not a Resumption, has to notice
    and tear the session down. Exercised directly since it needs a Client's
    `on_event` sink, not a bare `Session.reduce()` call."""
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)
        assert not session.awaiting_resume

        lifecycle._dispatch(_dap.ConnectionClosed(deliberate=False, detail="peer gone"))

        assert session.client is None
    finally:
        restore()


# =================================================================== cursor

def test_a_stale_frame_is_refused_for_a_caller_who_did_not_resume_it() -> None:
    """The cursor is per-caller (§2): caller A's selection is untouched by
    caller B's resume. That's exactly why it goes stale instead of silently
    tracking B's new stop -- A never asked to move, and the epoch moved out
    from under the handle they're still holding."""
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)

        selected, proceed, outcome = threading.Event(), threading.Event(), {}

        def caller_a() -> None:
            ref = stack.frame(0)
            assert not isinstance(ref, Error), ref
            selected.set()
            proceed.wait(2)
            outcome["guard"] = session.require_frame()

        a = _in_a_thread(caller_a)
        assert selected.wait(2), "caller A never selected a frame"

        # A *different* caller (this thread) resumes and re-stops thread 1,
        # bumping its epoch -- A's held selection did not move with it.
        _in_a_thread(lambda: (time.sleep(0.03), _stop(session, 1)))
        b_result = execution.cont(thread=1)
        assert isinstance(b_result, StopResult), b_result

        proceed.set()
        a.join(2)
        guard = outcome.get("guard")
        assert isinstance(guard, Error), guard
        assert "stale" in guard
        assert isinstance(guard, StaleFrameError), guard
        assert guard.kind is ErrorKind.STALE_FRAME
        assert guard.thread_id == 1
        assert guard.current_epoch > guard.stale_epoch
    finally:
        restore()


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
    import sys
    sys.exit(main())
