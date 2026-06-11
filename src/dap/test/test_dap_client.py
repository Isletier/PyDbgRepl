"""End-to-end tests for the v1 DAP client against real pydevd instances.

No test framework dependency: each test_* function takes a port, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all
of them.

Run from the repo root with the venv active:

    python -m src.dap.test.test_dap_client
"""
import os

from ..client import DAPError
from .helpers import attach_and_configure, session

TARGETS = os.path.join(os.path.dirname(__file__), "targets")
CALC = os.path.join(TARGETS, "calc.py")
LOOP = os.path.join(TARGETS, "loop.py")


def test_session_lifecycle(port: int) -> None:
    with session(port, LOOP) as client:
        attach_and_configure(client)

        threads = client.threads()
        assert len(threads["threads"]) >= 1, threads

        client.disconnect(terminate_debuggee=True)


def test_line_breakpoint_and_inspection(port: int) -> None:
    with session(port, CALC) as client:
        attach_and_configure(client, breakpoints={CALC: [{"line": 2}]})  # `c = a + b` in inner()

        stopped = client.wait_for_event("stopped", timeout=10)
        assert stopped["reason"] == "breakpoint", stopped
        thread_id = stopped["threadId"]

        trace = client.stack_trace(thread_id)
        top = trace["stackFrames"][0]
        assert top["name"] == "inner", trace
        frame_id = top["id"]

        scopes = client.scopes(frame_id)["scopes"]
        locals_ref = scopes[0]["variablesReference"]

        variables = {v["name"]: v["value"] for v in client.variables(locals_ref)["variables"]}
        assert variables["a"] == "1", variables
        assert variables["b"] == "2", variables

        result = client.evaluate("a + b", frame_id=frame_id)
        assert result["result"] == "3", result

        client.set_variable(locals_ref, "a", "100")
        result = client.evaluate("a + b", frame_id=frame_id)
        assert result["result"] == "102", result

        client.set_expression("b", "5", frame_id=frame_id)
        result = client.evaluate("a + b", frame_id=frame_id)
        assert result["result"] == "105", result

        client.continue_(thread_id)


def test_step_commands(port: int) -> None:
    with session(port, CALC) as client:
        attach_and_configure(client, breakpoints={CALC: [{"line": 9}]})  # `z = inner(x, y)` in outer()

        stopped = client.wait_for_event("stopped", timeout=10)
        assert stopped["reason"] == "breakpoint", stopped
        thread_id = stopped["threadId"]

        client.step_in(thread_id)
        stopped = client.wait_for_event("stopped", timeout=10)
        assert stopped["reason"] == "step", stopped
        trace = client.stack_trace(thread_id)
        assert trace["stackFrames"][0]["name"] == "inner", trace

        client.step_out(thread_id)
        stopped = client.wait_for_event("stopped", timeout=10)
        assert stopped["reason"] == "step", stopped
        trace = client.stack_trace(thread_id)
        assert trace["stackFrames"][0]["name"] == "outer", trace

        client.next(thread_id)
        stopped = client.wait_for_event("stopped", timeout=10)
        assert stopped["reason"] == "step", stopped
        trace = client.stack_trace(thread_id)
        assert trace["stackFrames"][0]["name"] == "outer", trace

        client.continue_(thread_id)


def test_function_breakpoints(port: int) -> None:
    with session(port, CALC) as client:
        client.initialize()
        client.attach()
        client.wait_for_event("initialized", timeout=5)

        client.set_function_breakpoints([{"name": "inner"}])
        client.set_exception_breakpoints([])
        client.configuration_done()

        stopped = client.wait_for_event("stopped", timeout=10)
        assert stopped["reason"] == "function breakpoint", stopped
        thread_id = stopped["threadId"]

        trace = client.stack_trace(thread_id)
        assert trace["stackFrames"][0]["name"] == "inner", trace

        client.continue_(thread_id)


def test_exception_breakpoints_and_info(port: int) -> None:
    with session(port, CALC) as client:
        attach_and_configure(client, exception_filters=["raised"])

        stopped = client.wait_for_event("stopped", timeout=10)
        assert stopped["reason"] == "exception", stopped
        thread_id = stopped["threadId"]

        info = client.exception_info(thread_id)
        assert "ValueError" in info["exceptionId"], info
        assert "boom" in (info.get("description") or ""), info

        client.continue_(thread_id)


def test_pause(port: int) -> None:
    with session(port, LOOP) as client:
        attach_and_configure(client)

        threads = client.threads()["threads"]
        thread_id = threads[0]["id"]

        client.pause(thread_id)
        stopped = client.wait_for_event("stopped", timeout=10)
        assert stopped["reason"] == "pause", stopped

        client.continue_(thread_id)


def test_pydevd_system_info(port: int) -> None:
    with session(port, LOOP) as client:
        attach_and_configure(client)

        info = client.pydevd_system_info()
        assert info["process"]["pid"] > 0, info

        client.disconnect(terminate_debuggee=True)


def test_pydevd_authorize(port: int) -> None:
    with session(port, LOOP) as client:
        attach_and_configure(client)

        info = client.pydevd_authorize()
        assert info["clientAccessToken"] is None, info

        client.disconnect(terminate_debuggee=True)


def test_modules(port: int) -> None:
    with session(port, LOOP) as client:
        attach_and_configure(client)

        modules = client.modules()
        assert isinstance(modules["modules"], list), modules

        client.disconnect(terminate_debuggee=True)


def test_set_debugger_property(port: int) -> None:
    with session(port, LOOP) as client:
        attach_and_configure(client)

        result = client.set_debugger_property(multiThreadsSingleNotification=True)
        assert result == {}, result

        client.disconnect(terminate_debuggee=True)


def test_set_pydevd_source_map(port: int) -> None:
    with session(port, CALC) as client:
        attach_and_configure(client)

        result = client.set_pydevd_source_map({"path": CALC}, [])
        assert result == {}, result

        client.disconnect(terminate_debuggee=True)


def test_source_invalid_reference(port: int) -> None:
    with session(port, LOOP) as client:
        attach_and_configure(client)

        try:
            client.source(0)
        except DAPError as e:
            assert "Source unavailable" in str(e), e
        else:
            raise AssertionError("expected DAPError for sourceReference=0")

        client.disconnect(terminate_debuggee=True)


def test_completions(port: int) -> None:
    with session(port, CALC) as client:
        attach_and_configure(client, breakpoints={CALC: [{"line": 2}]})  # `c = a + b` in inner()

        stopped = client.wait_for_event("stopped", timeout=10)
        thread_id = stopped["threadId"]
        frame_id = client.stack_trace(thread_id)["stackFrames"][0]["id"]

        result = client.completions("a", 2, frame_id=frame_id)
        names = {t["text"] if "text" in t else t["label"] for t in result["targets"]}
        assert "a" in names, result

        client.continue_(thread_id)


def test_step_in_targets(port: int) -> None:
    with session(port, CALC) as client:
        attach_and_configure(client, breakpoints={CALC: [{"line": 9}]})  # `z = inner(x, y)` in outer()

        stopped = client.wait_for_event("stopped", timeout=10)
        thread_id = stopped["threadId"]
        frame_id = client.stack_trace(thread_id)["stackFrames"][0]["id"]

        result = client.step_in_targets(frame_id)
        assert len(result["targets"]) >= 1, result

        client.continue_(thread_id)


def test_goto(port: int) -> None:
    with session(port, CALC) as client:
        attach_and_configure(client, breakpoints={CALC: [{"line": 9}]})  # `z = inner(x, y)` in outer()

        stopped = client.wait_for_event("stopped", timeout=10)
        thread_id = stopped["threadId"]

        targets = client.goto_targets({"path": CALC}, 7)["targets"]  # `x = 1` in outer()
        target_id = targets[0]["id"]

        client.goto(thread_id, target_id)
        stopped = client.wait_for_event("stopped", timeout=10)
        assert stopped["reason"] == "goto", stopped

        client.continue_(thread_id)


TESTS = [
    test_session_lifecycle,
    test_line_breakpoint_and_inspection,
    test_step_commands,
    test_function_breakpoints,
    test_exception_breakpoints_and_info,
    test_pause,
    test_pydevd_system_info,
    test_pydevd_authorize,
    test_modules,
    test_set_debugger_property,
    test_set_pydevd_source_map,
    test_source_invalid_reference,
    test_completions,
    test_step_in_targets,
    test_goto,
]


def main() -> None:
    base_port = 17000
    failures = []
    for i, test in enumerate(TESTS):
        port = base_port + i
        print(f"{test.__name__} (port {port}) ... ", end="", flush=True)
        try:
            test(port)
        except Exception as e:
            print(f"FAIL: {e!r}")
            failures.append(test.__name__)
        else:
            print("ok")

    if failures:
        raise SystemExit(f"{len(failures)} test(s) failed: {', '.join(failures)}")
    print("all tests passed")


if __name__ == "__main__":
    main()
