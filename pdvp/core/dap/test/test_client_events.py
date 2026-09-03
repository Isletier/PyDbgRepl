"""End-to-end tests for Client + EventBus against a real pydevd.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all
of them.

Run from the repo root with the venv active:

    python -m pdvp.core.dap.test.test_client_events
"""
import os

from pdvp.core import events
from pdvp.core.schema import pydevd_schema as schema
from pdvp.core.dap.client import ConnectionLost, RequestFailed
from pdvp.core.dap.test.helpers import attach_and_configure, session

TARGETS = os.path.join(os.path.dirname(__file__), "targets")
CALC = os.path.join(TARGETS, "calc.py")
LOOP = os.path.join(TARGETS, "loop.py")

# Every wait here is armed before the thing that triggers it, so these bound a
# failure rather than a race. Nothing inside pdvp passes a timeout.
WAIT = 10


def test_handshake_and_threads() -> None:
    with session(LOOP) as (client, bus):
        attach_and_configure(client, bus)

        threads = client.threads().body.threads
        assert len(threads) >= 1, threads

        client.disconnect(terminate_debuggee=True)


def test_breakpoint_stop_carries_thread() -> None:
    with session(CALC) as (client, bus):
        with bus.subscribe(events.Stopped) as stops:
            attach_and_configure(client, bus, breakpoints={CALC: [{"line": 2}]})
            stopped = stops.get(timeout=WAIT)

        assert isinstance(stopped, events.Stopped), stopped
        assert stopped.reason == "breakpoint", stopped
        assert stopped.thread_id is not None, stopped

        frames = client.stack_trace(stopped.thread_id).body.stackFrames
        assert frames[0]["name"] == "inner", frames[0]
        assert frames[0]["line"] == 2, frames[0]


def test_match_selects_one_thread() -> None:
    """A `match` predicate replaces the discard loop a name-only wait needs."""
    with session(CALC) as (client, bus):
        with bus.subscribe(events.Stopped) as stops:
            attach_and_configure(client, bus, breakpoints={CALC: [{"line": 2}]})
            first = stops.get(timeout=WAIT)

        tid = first.thread_id
        with bus.subscribe(events.Continued, match=lambda e: e.thread_id == tid) as resumed:
            client.continue_(tid)
            event = resumed.get(timeout=WAIT)

        assert isinstance(event, events.Continued), event
        assert event.thread_id == tid, event


def test_debuggee_exit_is_one_session_ended() -> None:
    """The debuggee finishing and the socket dying are one ending, not two.

    Measured: pydevd emits `terminated` and never `exited`, so the reason is
    TERMINATED and there is no exit code to be had from DAP -- it can only come
    from the process we spawned. The connection death that follows a second
    later must not publish a second ending.
    """
    with session(CALC) as (client, bus):
        with bus.subscribe(events.SessionEnded) as ended:
            attach_and_configure(client, bus)
            end = ended.get(timeout=WAIT)

            assert isinstance(end, events.SessionEnded), end
            assert end.reason == events.EndReason.TERMINATED, end

            try:
                extra = ended.get(timeout=2)
            except TimeoutError:
                extra = None
            assert extra is None, f"second ending published: {extra}"

        assert bus.ended is end, bus.ended


def test_session_ended_reaches_a_subscription_that_never_asked() -> None:
    """The one guarantee that keeps a wait from hanging past the session."""
    with session(LOOP) as (client, bus):
        attach_and_configure(client, bus)

        # Waiting for a stop that will never come, on a subscription whose only
        # declared interest is Stopped.
        with bus.subscribe(events.Stopped) as stops:
            assert events.SessionEnded in stops.types, stops.types
            client.close()
            end = stops.get(timeout=WAIT)

        assert isinstance(end, events.SessionEnded), end
        assert end.reason == events.EndReason.CLOSED, end


def test_pending_requests_are_woken_by_death() -> None:
    with session(LOOP) as (client, bus):
        attach_and_configure(client, bus)

        pending = client.send(schema.ThreadsRequest())
        client.close()

        try:
            pending.wait()
        except ConnectionLost as error:
            assert error.closed.deliberate, error.closed
        else:
            raise AssertionError("wait() returned after the connection closed")

        # And a send afterwards fails immediately rather than registering a slot
        # nobody will ever fill.
        try:
            client.request(schema.ThreadsRequest())
        except ConnectionLost:
            pass
        else:
            raise AssertionError("send() succeeded on a closed client")


def test_failed_request_raises_but_the_primitive_returns_it() -> None:
    with session(LOOP) as (client, bus):
        attach_and_configure(client, bus)

        bad = schema.SourceRequest(arguments=schema.SourceArguments(sourceReference=0))
        try:
            client.request(bad)
        except RequestFailed as error:
            assert error.response.success is False, error.response
        else:
            raise AssertionError("expected RequestFailed for sourceReference=0")

        with client.send(schema.SourceRequest(
                arguments=schema.SourceArguments(sourceReference=0))) as pending:
            response = pending.wait()
        assert response.success is False, response

        client.disconnect(terminate_debuggee=True)


def test_family_subscription_takes_every_thread_event() -> None:
    """A base class subscribes to a family -- what a name-keyed bus cannot do."""
    with session(CALC) as (client, bus):
        with bus.subscribe(events.ThreadEvent) as threads:
            attach_and_configure(client, bus)

            seen = []
            for event in threads:
                if isinstance(event, events.SessionEnded):
                    break
                seen.append(type(event))

        assert events.ThreadStarted in seen, seen
        assert events.ThreadExited in seen, seen


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
