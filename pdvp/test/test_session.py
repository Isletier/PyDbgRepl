"""Unit tests for Session: the reducer, the thread table, epochs and the cursor.

No pydevd, no sockets -- messages are fed in the shape Client.on_event
delivers them.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all
of them.

Run from the repo root with the venv active:

    python -m pdvp.test.test_session
"""
import threading

from .. import events as E
from ..dap import ConnectionClosed
from ..model import Error
from ..session import ControlRights, Session


class _Message:
    """What Client hands the reducer for a DAP event."""

    def __init__(self, event: str, body):
        self.event = event
        self.body = body


def _connected() -> Session:
    """A session with a live-looking connection and one stopped thread, id 1."""
    session = Session()
    session.begin(client=object())
    session.reduce(_Message("thread", {"threadId": 1, "reason": "started"}))
    session.reduce(_Message("stopped", {"threadId": 1, "reason": "breakpoint"}))
    session.current_thread_id = 1
    return session


# ---- thread table

def test_thread_events_maintain_the_table() -> None:
    session = Session()
    session.begin(client=object())

    session.reduce(_Message("thread", {"threadId": 7, "reason": "started"}))
    assert [t.id for t in session.threads] == [7], session.threads

    session.reduce(_Message("thread", {"threadId": 7, "reason": "exited"}))
    assert session.threads == [], session.threads


def test_a_stop_registers_a_thread_we_never_saw_start() -> None:
    """connect() to a pydevd that started without us replays no `thread` events."""
    session = Session()
    session.begin(client=object())
    session.reduce(_Message("stopped", {"threadId": 3, "reason": "pause"}))

    assert session.is_stopped(3), session.threads
    assert session.thread_state(3).reason == "pause"


def test_all_threads_stopped_marks_every_thread() -> None:
    session = Session()
    session.begin(client=object())
    for tid in (1, 2, 3):
        session.reduce(_Message("thread", {"threadId": tid, "reason": "started"}))

    session.reduce(_Message("stopped", {"threadId": 1, "reason": "breakpoint",
                                        "allThreadsStopped": True}))
    assert all(t.stopped for t in session.threads), session.threads


def test_one_thread_stopping_leaves_the_others_running() -> None:
    """Non-stop is the default; a single `running` bool could not say this."""
    session = Session()
    session.begin(client=object())
    for tid in (1, 2):
        session.reduce(_Message("thread", {"threadId": tid, "reason": "started"}))

    session.reduce(_Message("stopped", {"threadId": 1, "reason": "breakpoint"}))
    assert session.is_stopped(1)
    assert not session.is_stopped(2)
    assert session.any_running


def test_adopt_threads_reconciles_against_a_threads_response() -> None:
    session = _connected()
    session.adopt_threads([{"id": 1, "name": "MainThread"}, {"id": 2, "name": "worker"}])

    assert sorted(t.id for t in session.threads) == [1, 2]
    assert session.thread_state(1).name == "MainThread"
    # Adopting names must not forget that thread 1 is stopped.
    assert session.is_stopped(1)

    session.adopt_threads([{"id": 2, "name": "worker"}])
    assert [t.id for t in session.threads] == [2]


# ---- epochs

def test_the_epoch_is_bumped_at_send_not_on_the_event() -> None:
    session = _connected()
    before = session.epoch_of(1)

    session.note_resume(1)
    assert session.epoch_of(1) == before + 1, session.epoch_of(1)
    assert not session.is_stopped(1)

    # The event that confirms it must not bump a second time.
    session.reduce(_Message("continued", {"threadId": 1, "allThreadsContinued": False}))
    assert session.epoch_of(1) == before + 1, session.epoch_of(1)


def test_a_resume_we_did_not_issue_still_bumps() -> None:
    session = _connected()
    before = session.epoch_of(1)

    session.reduce(_Message("continued", {"threadId": 1, "allThreadsContinued": False}))
    assert session.epoch_of(1) == before + 1, session.epoch_of(1)
    assert not session.is_stopped(1)


def test_all_threads_continued_reconciles_the_threads_we_did_not_name() -> None:
    session = _connected()
    session.reduce(_Message("thread", {"threadId": 2, "reason": "started"}))
    session.reduce(_Message("stopped", {"threadId": 2, "reason": "breakpoint"}))
    before = session.epoch_of(2)

    session.note_resume(1)                      # we only asked for thread 1
    session.reduce(_Message("continued", {"threadId": 1, "allThreadsContinued": True}))

    assert session.epoch_of(2) == before + 1, session.epoch_of(2)
    assert not session.is_stopped(2)


def test_undo_resume_restores_the_run_state_but_keeps_the_bump() -> None:
    session = _connected()
    before = session.epoch_of(1)

    session.note_resume(1)
    session.undo_resume(1)

    assert session.is_stopped(1)
    assert session.epoch_of(1) == before + 1, "a bump must never be taken back"


def test_the_stop_event_carries_its_epoch() -> None:
    session = Session()
    session.begin(client=object())
    with session.bus.subscribe(E.Stopped) as stops:
        session.reduce(_Message("stopped", {"threadId": 1, "reason": "breakpoint"}))
        event = stops.get(timeout=1)

    assert event.epoch == session.epoch_of(1), (event.epoch, session.epoch_of(1))
    assert event.epoch is not None


# ---- guards

def test_require_stopped_says_which_thing_is_wrong() -> None:
    session = Session()
    assert isinstance(session.require_stopped(1), Error)        # not connected

    session.begin(client=object())
    assert isinstance(session.require_stopped(None), Error)     # no cursor
    assert isinstance(session.require_stopped(9), Error)        # unknown thread

    session.reduce(_Message("thread", {"threadId": 1, "reason": "started"}))
    assert isinstance(session.require_stopped(1), Error)        # known but running

    session.reduce(_Message("stopped", {"threadId": 1, "reason": "breakpoint"}))
    assert session.require_stopped(1) is None


def test_a_frame_from_before_a_resume_is_stale() -> None:
    session = _connected()
    session.current_frame_id = 100
    assert session.require_frame().frame_id == 100

    session.note_resume(1)
    session.reduce(_Message("stopped", {"threadId": 1, "reason": "step"}))

    error = session.require_frame()
    assert isinstance(error, Error), error
    assert "stale" in str(error), error


def test_a_frame_on_a_running_thread_is_refused_at_the_same_epoch() -> None:
    """The two guards are independent: a running thread's stack churns without
    the epoch moving at all."""
    session = _connected()
    session.current_frame_id = 100

    session.thread_state(1).stopped = False     # running, epoch untouched
    error = session.require_frame()
    assert isinstance(error, Error), error
    assert "running" in str(error), error


def test_no_frame_selected_is_its_own_error() -> None:
    session = _connected()
    error = session.require_frame()
    assert isinstance(error, Error) and "no current frame" in str(error), error


# ---- the cursor

def test_selecting_a_thread_clears_the_frame() -> None:
    session = _connected()
    session.current_frame_id = 100
    session.current_thread_id = 1
    assert session.current_frame_id is None


def test_the_cursor_is_context_local() -> None:
    session = _connected()
    session.current_thread_id = 1
    session.current_frame_id = 100

    seen = {}

    def other_thread():
        seen["thread"] = session.current_thread_id
        seen["frame"] = session.current_frame_id
        session.current_thread_id = 1           # this thread's cursor only

    worker = threading.Thread(target=other_thread)
    worker.start()
    worker.join()

    assert seen == {"thread": None, "frame": None}, seen
    # And the other thread's assignment did not disturb ours.
    assert session.current_frame_id == 100, session.current_frame_id


# ---- lifetimes

def test_the_ending_clears_the_thread_table() -> None:
    session = _connected()
    session.reduce(_Message("terminated", {}))
    assert session.threads == [], session.threads


def test_connection_death_becomes_one_ending() -> None:
    session = _connected()
    with session.bus.subscribe(E.SessionEnded) as ended:
        session.reduce(ConnectionClosed(deliberate=False, detail="peer gone"))
        event = ended.get(timeout=1)

    assert event.reason == E.EndReason.DISCONNECTED, event
    assert event.detail == "peer gone", event


def test_a_deliberate_close_is_not_a_disconnect() -> None:
    session = _connected()
    with session.bus.subscribe(E.SessionEnded) as ended:
        session.reduce(ConnectionClosed(deliberate=True, detail="closed locally"))
        assert ended.get(timeout=1).reason == E.EndReason.CLOSED


def test_end_connection_drops_connection_lifetime_state() -> None:
    session = _connected()
    session.end_connection()

    assert session.client is None
    assert session.threads == []
    assert isinstance(session.require_stopped(1), Error)


def test_a_new_session_cannot_revalidate_an_old_frame() -> None:
    """pydevd reuses small thread ids across runs, so epochs never restart."""
    session = _connected()
    session.current_frame_id = 100
    stale = session.require_frame()
    assert not isinstance(stale, Error), stale

    session.end_connection()
    session.begin(client=object())
    session.reduce(_Message("thread", {"threadId": 1, "reason": "started"}))
    session.reduce(_Message("stopped", {"threadId": 1, "reason": "breakpoint"}))

    error = session.require_frame()
    assert isinstance(error, Error) and "stale" in str(error), error


def test_begin_rearms_the_ending_latch() -> None:
    session = _connected()
    session.reduce(_Message("terminated", {}))
    assert session.bus.ended is not None

    session.begin(client=object())
    assert session.bus.ended is None, session.bus.ended


def test_awaiting_resume_tracks_blocked_callers() -> None:
    session = Session()
    assert not session.awaiting_resume
    with session.resume_wait():
        assert session.awaiting_resume
        with session.resume_wait():
            assert session.awaiting_resume
        assert session.awaiting_resume
    assert not session.awaiting_resume


# ---- the thread control right

def _in_a_thread(function) -> threading.Thread:
    thread = threading.Thread(target=function, daemon=True)
    thread.start()
    return thread


def _holds(rights, key, entered: threading.Event, release: threading.Event):
    """Take `key`, signal `entered`, hold until `release`. For a helper thread."""
    def run():
        with rights.hold(key):
            entered.set()
            release.wait()
    return run


def _acquires(rights, key, acquired: threading.Event, announce=None):
    """Take `key`, signal `acquired`, give it straight back."""
    def run():
        with rights.hold(key, announce=announce):
            acquired.set()
    return run


def test_two_callers_on_one_thread_serialize() -> None:
    """The point of the right: pydevd would swallow the second resume."""
    rights = ControlRights()
    entered, release, acquired = threading.Event(), threading.Event(), threading.Event()

    holder = _in_a_thread(_holds(rights, 2, entered, release))
    entered.wait()

    waiter = _in_a_thread(_acquires(rights, 2, acquired))
    assert not acquired.wait(0.2), "acquired a right somebody else was holding"

    release.set()
    assert acquired.wait(2), "the right was not handed over when it was released"
    holder.join()
    waiter.join()


def test_a_right_on_another_thread_does_not_block() -> None:
    rights = ControlRights()
    entered, release = threading.Event(), threading.Event()
    holder = _in_a_thread(_holds(rights, 2, entered, release))
    entered.wait()

    took_it = threading.Event()
    waiter = _in_a_thread(_acquires(rights, 3, took_it))
    assert took_it.wait(2), "thread 3's right waited on thread 2's"

    release.set()
    holder.join()
    waiter.join()


def test_the_global_right_conflicts_with_every_thread() -> None:
    """A resume naming no thread moves the whole program, in either direction."""
    rights = ControlRights()

    entered, release, acquired = threading.Event(), threading.Event(), threading.Event()
    holder = _in_a_thread(_holds(rights, 2, entered, release))
    entered.wait()
    _in_a_thread(_acquires(rights, None, acquired))
    assert not acquired.wait(0.2), "took every thread while one was held"
    release.set()
    assert acquired.wait(2)
    holder.join()

    rights = ControlRights()
    entered, release, acquired = threading.Event(), threading.Event(), threading.Event()
    holder = _in_a_thread(_holds(rights, None, entered, release))
    entered.wait()
    _in_a_thread(_acquires(rights, 2, acquired))
    assert not acquired.wait(0.2), "took one thread while every thread was held"
    release.set()
    assert acquired.wait(2)
    holder.join()


def test_the_right_is_reentrant_in_one_context() -> None:
    """control() holds it; the commands inside acquire it again and must not deadlock."""
    rights = ControlRights()
    with rights.hold(2):
        with rights.hold(2):
            assert rights.holder_of(2) is not None
        # Still held after the inner release -- depth, not a flag.
        assert rights.holder_of(2) is not None
    assert rights.holder_of(2) is None


def test_a_context_holding_every_thread_may_take_one() -> None:
    """`with control(all=True): cont()` -- the inner acquire names a thread."""
    rights = ControlRights()
    with rights.hold(None):
        with rights.hold(2):
            pass
    assert rights.holder_of(None) is None and rights.holder_of(2) is None


def test_a_blocked_acquisition_announces_itself_once() -> None:
    rights = ControlRights()
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    holder = _in_a_thread(_holds(rights, 2, entered, release))
    entered.wait()

    announced = []
    waiter = _in_a_thread(_acquires(rights, 2, done, announce=lambda: announced.append(1)))
    release.set()
    assert done.wait(2)
    assert announced == [1], announced
    holder.join()
    waiter.join()


def test_an_uncontended_acquisition_says_nothing() -> None:
    rights = ControlRights()
    announced = []
    with rights.hold(2, announce=lambda: announced.append(1)):
        pass
    assert announced == [], announced


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
    raise SystemExit(main())
