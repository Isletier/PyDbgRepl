"""End-to-end tests for Client's method surface against a real pydevd --
the DAP requests test_client_events.py doesn't touch (it exercises the event
bus itself: death, matching, family subscriptions). Together the two files
cover the whole of Layer 1.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all
of them. pytest also collects these directly by name.

Run from the repo root with the venv active:

    python -m pdvp.dap.test.test_dap_client
"""
import os

from ... import events
from ...schema import pydevd_schema as schema
from ..client import DAPError
from .helpers import attach_and_configure, session

TARGETS = os.path.join(os.path.dirname(__file__), "targets")
CALC = os.path.join(TARGETS, "calc.py")
LOOP = os.path.join(TARGETS, "loop.py")

# Every wait here is armed before the thing that triggers it, so these bound a
# failure rather than a race. Nothing inside pdvp passes a timeout.
WAIT = 10


def test_line_breakpoint_and_inspection() -> None:
    with session(CALC) as (client, bus):
        with bus.subscribe(events.Stopped) as stops:
            attach_and_configure(client, bus, breakpoints={CALC: [{"line": 2}]})  # `c = a + b` in inner()
            stopped = stops.get(timeout=WAIT)

        assert stopped.reason == "breakpoint", stopped
        thread_id = stopped.thread_id

        trace = client.stack_trace(thread_id).body
        top = trace.stackFrames[0]
        assert top["name"] == "inner", trace
        frame_id = top["id"]

        scopes = client.scopes(frame_id).body.scopes
        locals_ref = scopes[0]["variablesReference"]

        variables = {v["name"]: v["value"] for v in client.variables(locals_ref).body.variables}
        assert variables["a"] == "1", variables
        assert variables["b"] == "2", variables

        result = client.evaluate("a + b", frame_id=frame_id)
        assert result.body.result == "3", result

        client.set_variable(locals_ref, "a", "100")
        result = client.evaluate("a + b", frame_id=frame_id)
        assert result.body.result == "102", result

        client.set_expression("b", "5", frame_id=frame_id)
        result = client.evaluate("a + b", frame_id=frame_id)
        assert result.body.result == "105", result

        client.continue_(thread_id)


def test_step_commands() -> None:
    with session(CALC) as (client, bus):
        with bus.subscribe(events.Stopped) as stops:
            attach_and_configure(client, bus, breakpoints={CALC: [{"line": 9}]})  # `z = inner(x, y)` in outer()
            stopped = stops.get(timeout=WAIT)
            assert stopped.reason == "breakpoint", stopped
            thread_id = stopped.thread_id

            client.step_in(thread_id)
            stopped = stops.get(timeout=WAIT)
            assert stopped.reason == "step", stopped
            trace = client.stack_trace(thread_id).body
            assert trace.stackFrames[0]["name"] == "inner", trace

            client.step_out(thread_id)
            stopped = stops.get(timeout=WAIT)
            assert stopped.reason == "step", stopped
            trace = client.stack_trace(thread_id).body
            assert trace.stackFrames[0]["name"] == "outer", trace

            client.next(thread_id)
            stopped = stops.get(timeout=WAIT)
            assert stopped.reason == "step", stopped
            trace = client.stack_trace(thread_id).body
            assert trace.stackFrames[0]["name"] == "outer", trace

        client.continue_(thread_id)


def test_function_breakpoints() -> None:
    with session(CALC) as (client, bus):
        with bus.subscribe(events.Initialized) as initialized, \
             bus.subscribe(events.Stopped) as stops:
            client.initialize()
            client.attach()
            assert isinstance(initialized.get(timeout=WAIT), events.Initialized)

            client.set_function_breakpoints([schema.FunctionBreakpoint("inner", None, None)])
            client.set_exception_breakpoints([], [], [])
            client.configuration_done()

            stopped = stops.get(timeout=WAIT)

        assert stopped.reason == "function breakpoint", stopped
        thread_id = stopped.thread_id

        trace = client.stack_trace(thread_id).body
        assert trace.stackFrames[0]["name"] == "inner", trace

        client.continue_(thread_id)


def test_exception_breakpoints_and_info() -> None:
    with session(CALC) as (client, bus):
        with bus.subscribe(events.Stopped) as stops:
            attach_and_configure(client, bus, exception_filters=["raised"])
            stopped = stops.get(timeout=WAIT)

        assert stopped.reason == "exception", stopped
        thread_id = stopped.thread_id

        info = client.exception_info(thread_id).body
        assert "ValueError" in info.exceptionId, info
        assert "boom" in (info.description or ""), info

        client.continue_(thread_id)


def test_pause() -> None:
    with session(LOOP) as (client, bus):
        attach_and_configure(client, bus)

        threads = client.threads().body.threads
        thread_id = threads[0]["id"]

        with bus.subscribe(events.Stopped) as stops:
            client.pause(thread_id)
            stopped = stops.get(timeout=WAIT)
        assert stopped.reason == "pause", stopped

        client.continue_(thread_id)


def test_pydevd_system_info() -> None:
    with session(LOOP) as (client, bus):
        attach_and_configure(client, bus)

        info = client.pydevd_system_info().body
        assert info.process.pid > 0, info

        client.disconnect(terminate_debuggee=True)


def test_pydevd_authorize() -> None:
    with session(LOOP) as (client, bus):
        attach_and_configure(client, bus)

        info = client.pydevd_authorize().body
        assert info.clientAccessToken is None, info

        client.disconnect(terminate_debuggee=True)


def test_modules() -> None:
    with session(LOOP) as (client, bus):
        attach_and_configure(client, bus)

        modules = client.modules().body.modules
        assert isinstance(modules, list), modules

        client.disconnect(terminate_debuggee=True)


def test_set_debugger_property() -> None:
    with session(LOOP) as (client, bus):
        attach_and_configure(client, bus)

        result = client.set_debugger_property(multi_threads_single_notification=True)
        assert result.success, result

        client.disconnect(terminate_debuggee=True)


def test_set_pydevd_source_map() -> None:
    with session(CALC) as (client, bus):
        attach_and_configure(client, bus)

        result = client.set_pydevd_source_map(schema.Source(path=CALC), [])
        assert result.success, result

        client.disconnect(terminate_debuggee=True)


def test_source_invalid_reference() -> None:
    with session(LOOP) as (client, bus):
        attach_and_configure(client, bus)

        try:
            client.source(0)
        except DAPError as e:
            assert "Source unavailable" in str(e), e
        else:
            raise AssertionError("expected DAPError for sourceReference=0")

        client.disconnect(terminate_debuggee=True)


def test_completions() -> None:
    with session(CALC) as (client, bus):
        with bus.subscribe(events.Stopped) as stops:
            attach_and_configure(client, bus, breakpoints={CALC: [{"line": 2}]})  # `c = a + b` in inner()
            stopped = stops.get(timeout=WAIT)

        thread_id = stopped.thread_id
        frame_id = client.stack_trace(thread_id).body.stackFrames[0]["id"]

        result = client.completions("a", 2, frame_id=frame_id).body
        names = {t["text"] if "text" in t else t["label"] for t in result.targets}
        assert "a" in names, result.targets

        client.continue_(thread_id)


def test_step_in_targets() -> None:
    with session(CALC) as (client, bus):
        with bus.subscribe(events.Stopped) as stops:
            attach_and_configure(client, bus, breakpoints={CALC: [{"line": 9}]})  # `z = inner(x, y)` in outer()
            stopped = stops.get(timeout=WAIT)

        thread_id = stopped.thread_id
        frame_id = client.stack_trace(thread_id).body.stackFrames[0]["id"]

        result = client.step_in_targets(frame_id).body
        assert len(result.targets) >= 1, result.targets

        client.continue_(thread_id)


def test_goto() -> None:
    with session(CALC) as (client, bus):
        with bus.subscribe(events.Stopped) as stops:
            attach_and_configure(client, bus, breakpoints={CALC: [{"line": 9}]})  # `z = inner(x, y)` in outer()
            stopped = stops.get(timeout=WAIT)
            thread_id = stopped.thread_id

            targets = client.goto_targets(schema.Source(path=CALC), 7).body.targets  # `x = 1` in outer()
            target_id = targets[0]["id"]

            client.goto(thread_id, target_id)
            stopped = stops.get(timeout=WAIT)
            assert stopped.reason == "goto", stopped

        client.continue_(thread_id)


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
