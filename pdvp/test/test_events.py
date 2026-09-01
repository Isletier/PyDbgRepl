"""Unit tests for the event vocabulary and the bus. No pydevd, no sockets.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all
of them.

Run from the repo root with the venv active:

    python -m pdvp.test.test_events
"""
import threading
import time

from pdvp import events as E


def _stopped(thread_id: int = 1, reason: str = "breakpoint") -> E.Stopped:
    return E.Stopped(thread_id=thread_id, reason=reason)


# ---- filtering

def test_subscription_filters_by_type() -> None:
    bus = E.EventBus()
    with bus.subscribe(E.Stopped) as stops:
        bus.publish(E.Continued(thread_id=1))
        bus.publish(_stopped())
        event = stops.get(timeout=1)

    assert isinstance(event, E.Stopped), event


def test_no_types_means_everything() -> None:
    bus = E.EventBus()
    with bus.subscribe() as everything:
        bus.publish(E.Initialized())
        bus.publish(E.Continued(thread_id=1))
        assert isinstance(everything.get(timeout=1), E.Initialized)
        assert isinstance(everything.get(timeout=1), E.Continued)


def test_a_base_class_takes_the_family() -> None:
    bus = E.EventBus()
    with bus.subscribe(E.ThreadEvent) as family:
        bus.publish(E.Initialized())            # not a ThreadEvent
        bus.publish(E.ThreadStarted(thread_id=7))
        bus.publish(_stopped(thread_id=7))
        assert isinstance(family.get(timeout=1), E.ThreadStarted)
        assert isinstance(family.get(timeout=1), E.Stopped)


def test_names_resolve_to_types() -> None:
    bus = E.EventBus()
    with bus.subscribe("stopped") as stops:
        assert E.Stopped in stops.types, stops.types

    try:
        bus.subscribe("no_such_event")
    except LookupError:
        pass
    else:
        raise AssertionError("subscribing to an unknown name should raise")


def test_match_is_applied_at_fanout() -> None:
    bus = E.EventBus()
    with bus.subscribe(E.Stopped, match=lambda e: e.thread_id == 2) as mine:
        bus.publish(_stopped(thread_id=1))
        bus.publish(_stopped(thread_id=2))
        event = mine.get(timeout=1)

    assert event.thread_id == 2, event


def test_a_broken_match_loses_events_not_the_reader() -> None:
    def explode(event):
        raise RuntimeError("bad predicate")

    bus = E.EventBus()
    with bus.subscribe(E.Stopped, match=explode) as broken, bus.subscribe(E.Stopped) as fine:
        bus.publish(_stopped())

        assert broken.errors == 1, broken.errors
        assert isinstance(fine.get(timeout=1), E.Stopped)

        # And it still cannot swallow the ending, which is what would hang.
        bus.publish(E.SessionEnded(E.EndReason.CLOSED))
        assert isinstance(broken.get(timeout=1), E.SessionEnded)


# ---- the one ending

def test_session_ended_is_added_to_every_subscription() -> None:
    bus = E.EventBus()
    with bus.subscribe(E.Stopped) as stops:
        assert E.SessionEnded in stops.types, stops.types
        bus.publish(E.SessionEnded(E.EndReason.DISCONNECTED))
        assert isinstance(stops.get(timeout=1), E.SessionEnded)


def test_three_endings_publish_once() -> None:
    bus = E.EventBus()
    with bus.subscribe() as everything:
        bus.publish(E.SessionEnded(E.EndReason.EXITED, exit_code=0))
        bus.publish(E.SessionEnded(E.EndReason.TERMINATED))
        bus.publish(E.SessionEnded(E.EndReason.DISCONNECTED))

        first = everything.get(timeout=1)
        assert first.reason == E.EndReason.EXITED, first
        assert bus.ended is first, bus.ended
        try:
            extra = everything.get(timeout=0.1)
        except TimeoutError:
            extra = None
        assert extra is None, extra


def test_session_started_rearms_the_latch() -> None:
    bus = E.EventBus()
    with bus.subscribe() as everything:
        bus.publish(E.SessionEnded(E.EndReason.CLOSED))
        bus.publish(E.SessionStarted(pid=1))
        bus.publish(E.SessionEnded(E.EndReason.DISCONNECTED))

        assert everything.get(timeout=1).reason == E.EndReason.CLOSED
        assert isinstance(everything.get(timeout=1), E.SessionStarted)
        assert everything.get(timeout=1).reason == E.EndReason.DISCONNECTED
        assert bus.ended is not None


def test_a_blocked_getter_is_woken_by_the_ending() -> None:
    bus = E.EventBus()
    got = []
    stops = bus.subscribe(E.Stopped, match=lambda e: e.thread_id == 99)

    waiter = threading.Thread(target=lambda: got.append(stops.get()), daemon=True)
    waiter.start()
    time.sleep(0.05)                    # let it block
    bus.publish(E.SessionEnded(E.EndReason.DISCONNECTED, detail="peer gone"))
    waiter.join(2)

    assert not waiter.is_alive(), "getter still blocked after the session ended"
    assert isinstance(got[0], E.SessionEnded), got


# ---- queues

def test_ordering_holds_within_a_subscription() -> None:
    bus = E.EventBus()
    with bus.subscribe(E.Stopped) as stops:
        for tid in range(20):
            bus.publish(_stopped(thread_id=tid))
        assert [stops.get(timeout=1).thread_id for _ in range(20)] == list(range(20))


def test_bounded_queue_drops_the_oldest() -> None:
    bus = E.EventBus()
    with bus.subscribe(E.Output, maxsize=2) as lagging:
        for i in range(5):
            bus.publish(E.Output(text=str(i)))

        assert lagging.dropped == 3, lagging.dropped
        assert [lagging.get(timeout=1).text for _ in range(2)] == ["3", "4"]


def test_unbounded_is_the_default() -> None:
    bus = E.EventBus()
    with bus.subscribe(E.Output) as stream:
        for i in range(1000):
            bus.publish(E.Output(text=str(i)))
        assert stream.dropped == 0, stream.dropped


# ---- teardown

def test_close_wakes_every_getter() -> None:
    bus = E.EventBus()
    stops = bus.subscribe(E.Stopped)
    outcomes = []

    def wait():
        try:
            stops.get()
        except E.SubscriptionClosed:
            outcomes.append("closed")

    waiters = [threading.Thread(target=wait, daemon=True) for _ in range(3)]
    for waiter in waiters:
        waiter.start()
    time.sleep(0.05)
    stops.close()
    for waiter in waiters:
        waiter.join(2)

    assert outcomes == ["closed"] * 3, outcomes


def test_close_is_idempotent_and_detaches() -> None:
    bus = E.EventBus()
    stops = bus.subscribe(E.Stopped)
    stops.close()
    stops.close()

    bus.publish(_stopped())             # must not reach a closed subscription
    try:
        stops.get(timeout=0.1)
    except E.SubscriptionClosed:
        pass
    else:
        raise AssertionError("a closed subscription still delivered an event")


def test_iteration_ends_at_close_not_at_the_ending() -> None:
    bus = E.EventBus()
    stops = bus.subscribe(E.Stopped)
    seen = []

    reader = threading.Thread(target=lambda: seen.extend(stops), daemon=True)
    reader.start()
    time.sleep(0.05)

    bus.publish(_stopped())
    bus.publish(E.SessionEnded(E.EndReason.CLOSED))
    bus.publish(E.SessionStarted())
    bus.publish(_stopped(thread_id=2))
    time.sleep(0.05)
    stops.close()
    reader.join(2)

    assert not reader.is_alive(), "iteration did not end at close()"
    assert [type(e) for e in seen] == [E.Stopped, E.SessionEnded, E.Stopped], seen


def test_bus_close_wakes_everyone_and_refuses_new_subscribers() -> None:
    bus = E.EventBus()
    stops = bus.subscribe(E.Stopped)
    bus.close()
    bus.close()

    try:
        stops.get(timeout=0.1)
    except E.SubscriptionClosed:
        pass
    else:
        raise AssertionError("bus.close() did not wake the subscriber")

    try:
        bus.subscribe(E.Stopped)
    except E.BusClosed:
        pass
    else:
        raise AssertionError("subscribed to a closed bus")


# ---- translation

def test_from_dap_translates_the_events_we_model() -> None:
    stopped = E.from_dap("stopped", {"threadId": 3, "reason": "step",
                                     "hitBreakpointIds": [7], "allThreadsStopped": True})
    assert stopped == E.Stopped(thread_id=3, reason="step",
                                all_threads=True, hit_breakpoint_ids=(7,)), stopped

    assert E.from_dap("thread", {"threadId": 3, "reason": "started"}) == E.ThreadStarted(3)
    assert E.from_dap("thread", {"threadId": 3, "reason": "exited"}) == E.ThreadExited(3)
    assert E.from_dap("initialized", None) == E.Initialized()


def test_exited_and_terminated_both_mean_the_end() -> None:
    exited = E.from_dap("exited", {"exitCode": 3})
    assert exited == E.SessionEnded(E.EndReason.EXITED, exit_code=3), exited
    assert E.from_dap("terminated", {}) == E.SessionEnded(E.EndReason.TERMINATED)


def test_missing_all_threads_continued_means_all() -> None:
    """The spec asymmetry a generic 'absent is false' helper gets backwards."""
    assert E.from_dap("continued", {"threadId": 1}).all_threads is True
    assert E.from_dap("continued", {"threadId": 1, "allThreadsContinued": False}).all_threads is False
    assert E.from_dap("stopped", {"threadId": 1, "reason": "x"}).all_threads is False


def test_an_unmodelled_dap_event_is_published_not_dropped() -> None:
    event = E.from_dap("progressStart", {"progressId": "1"})
    assert isinstance(event, E.UnhandledDapEvent), event
    assert event.dap_name == "progressStart", event


def test_a_malformed_body_does_not_raise_at_translation() -> None:
    event = E.from_dap("breakpoint", "not a body")
    assert isinstance(event, (E.BreakpointChanged, E.UnhandledDapEvent)), event


def test_events_are_immutable() -> None:
    event = _stopped()
    try:
        event.reason = "other"
    except Exception:
        return
    raise AssertionError("events must be immutable")


def test_duplicate_event_names_are_rejected() -> None:
    try:
        class Clash(E.Event):
            name = "stopped"
    except TypeError:
        return
    raise AssertionError("two event types took the same name")


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
