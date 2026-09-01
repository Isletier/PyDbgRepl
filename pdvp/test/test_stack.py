"""Unit tests for `commands/stack.py`: threads, thread, cursors, bt, frame, up, down.

No pydevd, no sockets -- same `FakeClient`-plus-`sys.modules`-scan harness as
`test_execution.py`, duplicated locally with only the client surface this
module needs. See `test_execution.py`'s module docstring for why the scan
exists.

Run from the repo root with the venv active:

    python -m pdvp.test.test_stack
"""
import importlib
import sys
import threading

stack = importlib.import_module("pdvp.commands.stack")
from pdvp.model import CursorList, Error, FrameList, FrameRef, Status, ThreadList
from pdvp.session import SESSION as _REAL_SESSION
from pdvp.session import Session

# ---------------------------------------------------------------- fakes

class _Message:
    def __init__(self, event: str, body):
        self.event = event
        self.body = body


class _Body:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Response:
    def __init__(self, success: bool = True, **body_kw):
        self.success = success
        self.body = _Body(**body_kw)


class FakeClient:
    """Stands in for `dap.Client`'s `threads()`/`stack_trace()`."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.threads_body: list[dict] = []
        # thread_id -> ordered list of stackTrace frame dicts, index 0 = the
        # currently executing (innermost) frame -- the same ordering
        # report_stop()/bt() assume when they read frames[0] as "the top".
        self.frames_by_thread: dict[int, list[dict]] = {}

    def _record(self, name, *args) -> None:
        self.calls.append((name, args))

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)

    def threads(self):
        self._record("threads")
        return _Response(threads=self.threads_body)

    def stack_trace(self, thread_id, start_frame=None, levels=None):
        self._record("stack_trace", thread_id)
        return _Response(stackFrames=self.frames_by_thread.get(thread_id, []))


def _session_bound_modules() -> list:
    return [m for name, m in sys.modules.items()
            if name.startswith("pdvp.") and name != "pdvp.session"
            and getattr(m, "SESSION", None) is _REAL_SESSION]


def _new_session_env():
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


def _connected(session, client, *thread_ids: int) -> None:
    """A live-looking connection with `thread_ids` announced and stopped."""
    session.begin(client=client)
    for tid in thread_ids:
        session.reduce(_Message("thread", {"threadId": tid, "reason": "started"}))
    for tid in thread_ids:
        session.reduce(_Message("stopped", {"threadId": tid, "reason": "breakpoint"}))


def _in_a_thread(function) -> threading.Thread:
    thread = threading.Thread(target=function, daemon=True)
    thread.start()
    return thread


def _frames(*ids: int) -> list[dict]:
    """Frame dicts for the given ids, id[0] = innermost (frame 0)."""
    return [{"id": fid, "name": f"f{i}", "line": i + 1, "source": {"path": "t.py"}}
            for i, fid in enumerate(ids)]


# =================================================================== threads()

def test_threads_not_connected_is_an_error() -> None:
    session, client, restore = _new_session_env()
    try:
        assert isinstance(stack.threads(), Error)
    finally:
        restore()


def test_threads_auto_selects_the_first_thread_when_nothing_is_selected_yet() -> None:
    """Pinned as current behavior -- see doc/architecture.md's Open section,
    "threads()'s auto-select cursor side effect": this is flagged there as
    dead in practice and a candidate for removal, not tested here as
    something to keep. It's tested so whichever way that's decided, the
    change shows up as a failing test rather than a silent behavior change."""
    session, client, restore = _new_session_env()
    try:
        session.begin(client=client)
        client.threads_body = [{"id": 5, "name": "MainThread"}, {"id": 6, "name": "worker"}]

        result = stack.threads()
        assert isinstance(result, ThreadList) and not isinstance(result, Error), result
        assert session.current_thread_id == 5, session.current_thread_id
        assert result.current_id == 5
    finally:
        restore()


def test_threads_does_not_clobber_an_existing_selection() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, 2)
        session.current_thread_id = 2
        client.threads_body = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]

        stack.threads()
        assert session.current_thread_id == 2, session.current_thread_id
    finally:
        restore()


# ==================================================================== thread()

def test_thread_switches_the_cursor_and_clears_the_frame() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, 2)
        session.current_thread_id = 1
        session.current_frame_id = 100

        result = stack.thread(2)
        assert isinstance(result, Status) and not isinstance(result, Error), result
        assert session.current_thread_id == 2
        assert session.current_frame_id is None
    finally:
        restore()


def test_thread_not_connected_is_an_error() -> None:
    session, client, restore = _new_session_env()
    try:
        assert isinstance(stack.thread(1), Error)
    finally:
        restore()


# =================================================================== cursors()

def test_cursors_lists_every_caller_and_flags_the_calling_one() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, 2)
        stack.thread(1)  # this test's own thread is one caller

        other_selected = threading.Event()

        def other() -> None:
            stack.thread(2)
            other_selected.set()

        worker = _in_a_thread(other)
        assert other_selected.wait(2)
        worker.join(2)

        rows = stack.cursors()
        assert isinstance(rows, CursorList)
        assert len(rows) == 2, rows

        mine = next(r for r in rows if r["current"])
        theirs = next(r for r in rows if not r["current"])
        assert mine["thread"] == 1, mine
        assert theirs["thread"] == 2, theirs
        assert not mine["stale"] and not theirs["stale"]
        assert rows.default_thread == session._last_stopped
    finally:
        restore()


def test_cursors_flags_a_selection_from_a_previous_generation_as_stale() -> None:
    """Direct Session-API manipulation, matching test_session.py's
    `test_a_new_session_cannot_revalidate_an_old_frame`: begin() a second time
    without an intervening end_connection() bumps `generation` but does not
    clear the cursor table (only end_connection() does that), which is the
    one way to produce a stale row through the *normal* command surface
    reaching cursors() -- every real disconnect path clears the table first,
    so this exercises the field rather than a reachable user scenario."""
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1)
        stack.thread(1)

        session.begin(client=client)  # same client, no end_connection() first
        rows = stack.cursors()
        assert len(rows) == 1, rows
        assert rows[0]["stale"] is True, rows[0]
    finally:
        restore()


# ========================================================================= bt()

def test_bt_returns_the_full_trace_and_marks_the_current_frame() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1)
        client.frames_by_thread[1] = _frames(100, 101, 102)

        result = stack.bt()
        assert isinstance(result, FrameList) and not isinstance(result, Error), result
        assert [f["id"] for f in result] == [100, 101, 102]
        assert result.current_id == 100, "must auto-select the top frame for the caller's own thread"
        assert session.current_frame_id == 100
    finally:
        restore()


def test_bt_on_an_explicit_other_thread_does_not_move_the_callers_cursor() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, 2)
        client.frames_by_thread[2] = _frames(200, 201)

        result = stack.bt(thread=2)
        assert isinstance(result, FrameList) and not isinstance(result, Error), result
        assert session.current_frame_id is None, "reading another thread's stack must not select a frame"
    finally:
        restore()


def test_bt_not_stopped_is_an_error() -> None:
    session, client, restore = _new_session_env()
    try:
        session.begin(client=client)
        session.reduce(_Message("thread", {"threadId": 1, "reason": "started"}))
        session.current_thread_id = 1
        assert isinstance(stack.bt(), Error)
    finally:
        restore()


# =================================================================== frame()

def test_frame_selects_a_valid_index() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1)
        client.frames_by_thread[1] = _frames(100, 101, 102)

        result = stack.frame(1)
        assert isinstance(result, FrameRef) and not isinstance(result, Error), result
        assert result["id"] == 101
        assert session.current_frame_id == 101
    finally:
        restore()


def test_frame_out_of_range_is_an_error_and_does_not_move_the_cursor() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1)
        client.frames_by_thread[1] = _frames(100, 101, 102)
        stack.frame(1)  # select something first

        assert isinstance(stack.frame(3), Error)
        assert isinstance(stack.frame(-1), Error)
        assert session.current_frame_id == 101, "an out-of-range request must not disturb the selection"
    finally:
        restore()


# ================================================================= up() / down()

def test_up_and_down_default_to_index_zero_when_nothing_is_selected() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1)
        client.frames_by_thread[1] = _frames(100, 101, 102)

        result = stack.up(1)  # 0 + 1 = index 1
        assert isinstance(result, FrameRef) and not isinstance(result, Error), result
        assert result["id"] == 101, result
    finally:
        restore()


def test_up_clamps_at_the_outermost_frame() -> None:
    """`_move_frame`'s current clamp labels: running off the *high* end of the
    index (past the outermost/caller frame) is reported as "*** Newest frame".
    Pinned as observed -- see the report back to the parent conversation:
    given frames[0] is documented as the innermost/currently-executing frame
    (bt()'s own comment, and report_stop()'s `top = frames[0]`), the highest
    index is the *outermost* frame, which reads like it should be labeled
    "Oldest", not "Newest". Possible label swap in `_move_frame`, not fixed
    here."""
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1)
        client.frames_by_thread[1] = _frames(100, 101, 102)
        stack.frame(0)

        stack.up(1)  # -> index 1
        stack.up(1)  # -> index 2 (last valid index, no clamp yet)
        result = stack.up(1)  # -> index 3, clamps to 2

        assert isinstance(result, FrameRef), result
        assert result["id"] == 102
        assert result.prefix == "*** Newest frame", repr(result.prefix)
    finally:
        restore()


def test_down_clamps_at_the_innermost_frame() -> None:
    """See the label note on test_up_clamps_at_the_outermost_frame -- the
    same current (possibly swapped) labeling applies here in reverse."""
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1)
        client.frames_by_thread[1] = _frames(100, 101, 102)
        stack.frame(0)

        result = stack.down(1)  # -> index -1, clamps to 0
        assert isinstance(result, FrameRef), result
        assert result["id"] == 100
        assert result.prefix == "*** Oldest frame", repr(result.prefix)
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
    sys.exit(main())
