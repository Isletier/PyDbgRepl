"""Unit tests for the command layer: sbreak/fbreak/breakpoint() dispatch,
clear/enable/disable, and commit_all()/commit_source_breakpoints()/
commit_function_breakpoints().

Same harness as test_execution.py, imported rather than duplicated: a
`FakeClient` standing in for `dap.Client`, and `_new_session_env()`'s
`sys.modules` scan that rebinds SESSION in every command module that bound it
at import time (breakpoints.py, location.py, ...). See that module's docstring
for why the scan exists rather than a maintained list.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all of
them. pytest also collects these directly by name.

Run from the repo root with the venv active:

    python -m pdvp.test.test_breakpoints
"""
import importlib

from pdvp import dap as _dap
from pdvp import model
from pdvp.config import CONFIG
from pdvp.test.test_execution import (
    FakeClient,
    _Response,
    _connected,
    _in_a_thread,
    _new_session_env,
)

# Same reason test_execution.py imports this way: `pdvp.commands`'s __init__.py
# shadows the package's `breakpoints` attribute with the `breakpoints()`
# command function of the same name, so a plain `import ... as` would grab the
# function instead of the module.
breakpoints = importlib.import_module("pdvp.commands.breakpoints")
stack = importlib.import_module("pdvp.commands.stack")


def _connect(session, client) -> None:
    """A live-looking connection; no threads or stops needed for these tests."""
    session.begin(client=client)


def _raises(exc_type, fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except exc_type:
        return True
    return False


# ============================================================ sbreak / fbreak

def test_sbreak_line_only_resolves_via_the_current_frame() -> None:
    session, client, restore = _new_session_env()
    try:
        _connected(session, client, 1, non_stop=True)
        stack.frame(0)  # selects the FakeClient's frame on "t.py"

        bp = breakpoints.sbreak(5)
        assert isinstance(bp, model.SourceBreakpoint)
        assert bp.path == "t.py" and bp.line == 5
        assert bp.ID in session.Breakpoints
        assert client.count("set_breakpoints") == 1
    finally:
        restore()


def test_sbreak_explicit_path_bypasses_the_current_file() -> None:
    session, client, restore = _new_session_env()
    try:
        # Never connected -- current_file() is never consulted when a path is given.
        bp = breakpoints.sbreak("explicit.py", 7)
        assert bp.path == "explicit.py" and bp.line == 7
        assert client.count("set_breakpoints") == 0, "no client yet, commit must no-op"
    finally:
        restore()


def test_sbreak_line_only_without_a_current_file_is_an_error() -> None:
    session, client, restore = _new_session_env()
    before = CONFIG.file
    CONFIG.file = None
    try:
        assert _raises(model.PDVPError, breakpoints.sbreak, 3)
    finally:
        CONFIG.file = before
        restore()


# ============================================================ breakpoint()

def test_breakpoint_dispatch_source_shapes() -> None:
    session, client, restore = _new_session_env()
    try:
        bp = breakpoints.breakpoint("f.py", 10)
        assert isinstance(bp, model.SourceBreakpoint)
        assert (bp.path, bp.line) == ("f.py", 10)
        assert bp.condition is None and bp.hitCondition is None and bp.logMessage is None

        bp = breakpoints.breakpoint("f.py", 11, "x>0")
        assert bp.condition == "x>0" and bp.hitCondition is None and bp.logMessage is None

        bp = breakpoints.breakpoint("f.py", 12, "x>0", "5")
        assert bp.condition == "x>0" and bp.hitCondition == "5" and bp.logMessage is None

        bp = breakpoints.breakpoint("f.py", 13, "x>0", "5", "hit!")
        assert bp.condition == "x>0" and bp.hitCondition == "5" and bp.logMessage == "hit!"
    finally:
        restore()


def test_breakpoint_dispatch_function_shapes() -> None:
    session, client, restore = _new_session_env()
    try:
        bp = breakpoints.breakpoint("myfunc")
        assert isinstance(bp, model.FunctionBreakpoint)
        assert bp.name == "myfunc" and bp.condition is None and bp.hitCondition is None

        bp = breakpoints.breakpoint("myfunc", "x>0")
        assert bp.condition == "x>0" and bp.hitCondition is None

        bp = breakpoints.breakpoint("myfunc", "x>0", "5")
        assert bp.condition == "x>0" and bp.hitCondition == "5"
    finally:
        restore()


def test_breakpoint_dispatch_line_only() -> None:
    session, client, restore = _new_session_env()
    before = CONFIG.file
    CONFIG.file = "current.py"
    try:
        bp = breakpoints.breakpoint(20)
        assert isinstance(bp, model.SourceBreakpoint)
        assert (bp.path, bp.line) == ("current.py", 20)
    finally:
        CONFIG.file = before
        restore()


def test_breakpoint_dispatch_rejects_invalid_shapes() -> None:
    session, client, restore = _new_session_env()
    try:
        invalid = [
            (),
            (1, 2),                                  # two ints: no case matches
            ("f.py", 10, "a", "b", "c", "d"),          # more than 3 trailing strings
            ("f.py", 10, 5),                           # trailing non-string
            ("myfunc", "a", "b", "c"),                  # more than 2 trailing strings
        ]
        for args in invalid:
            assert _raises(model.PDVPError, breakpoints.breakpoint, *args), args
    finally:
        restore()


# ============================================================ commit reconciliation

def test_commit_source_breakpoints_reconciles_a_slid_line() -> None:
    """pydevd may move a breakpoint onto the next executable line; the
    breakpoint's own `line` follows that resolution."""
    session, client, restore = _new_session_env()
    try:
        _connect(session, client)

        def sliding(source, sbreakpoints):
            client._record("set_breakpoints", len(sbreakpoints))
            return _Response(breakpoints=[
                {"verified": True, "line": b.line + 1, "source": {}} for b in sbreakpoints
            ])
        client.set_breakpoints = sliding

        bp = breakpoints.sbreak("slide.py", 10)
        assert bp.verified is True
        assert bp.line == 11, "pydevd's slid line must win over the requested one"
    finally:
        restore()


def test_commit_source_breakpoints_keeps_our_line_when_unverified() -> None:
    """A breakpoint that failed to verify comes back with no line at all --
    keep ours then (breakpoints.py's own comment)."""
    session, client, restore = _new_session_env()
    try:
        _connect(session, client)

        def unverified(source, sbreakpoints):
            client._record("set_breakpoints", len(sbreakpoints))
            return _Response(breakpoints=[
                {"verified": False, "line": None, "source": {}} for _ in sbreakpoints
            ])
        client.set_breakpoints = unverified

        bp = breakpoints.sbreak("noverify.py", 8)
        assert bp.verified is False
        assert bp.line == 8
    finally:
        restore()


def test_commit_no_ops_without_a_client() -> None:
    session, client, restore = _new_session_env()
    try:
        # session.client stays None -- never connected.
        bp = breakpoints.sbreak("nope.py", 1)
        assert bp.path == "nope.py"
        assert client.count("set_breakpoints") == 0
    finally:
        restore()


def test_commit_source_breakpoints_returns_error_on_a_failed_response() -> None:
    session, client, restore = _new_session_env()
    try:
        _connect(session, client)

        def failed(source, sbreakpoints):
            client._record("set_breakpoints", len(sbreakpoints))
            return _Response(success=False, message="nope")
        client.set_breakpoints = failed

        result = breakpoints.sbreak("fail.py", 1)
        assert isinstance(result, model.Error), result
    finally:
        restore()


def test_commit_source_breakpoints_returns_error_on_a_dap_error() -> None:
    session, client, restore = _new_session_env()
    try:
        _connect(session, client)

        def blows_up(source, sbreakpoints):
            client._record("set_breakpoints", len(sbreakpoints))
            raise _dap.DAPError("pydevd is gone")
        client.set_breakpoints = blows_up

        result = breakpoints.sbreak("boom.py", 1)
        assert isinstance(result, model.Error), result
        assert "pydevd is gone" in result
    finally:
        restore()


# ============================================================ clear / enable / disable

def test_clear_enable_disable_on_an_unknown_id_is_a_silent_noop() -> None:
    session, client, restore = _new_session_env()
    try:
        breakpoints.clear(999999)
        breakpoints.enable(999999)
        breakpoints.disable(999999)
        # Getting here without an exception is the test.
    finally:
        restore()


def test_clear_removes_the_breakpoint_and_recommits() -> None:
    session, client, restore = _new_session_env()
    try:
        _connect(session, client)
        bp = breakpoints.sbreak("clearme.py", 4)
        assert client.count("set_breakpoints") == 1

        breakpoints.clear(bp.ID)
        assert bp.ID not in session.Breakpoints
        assert client.count("set_breakpoints") == 2, "clear() must recommit"
        assert client.calls_for("set_breakpoints")[-1][1] == (0,), "the recommit sends the now-empty set"
    finally:
        restore()


def test_enable_disable_toggle_excludes_from_commit() -> None:
    session, client, restore = _new_session_env()
    try:
        _connect(session, client)
        bp = breakpoints.sbreak("toggle.py", 6)
        assert client.calls_for("set_breakpoints")[-1][1] == (1,)

        breakpoints.disable(bp.ID)
        assert session.Breakpoints[bp.ID].enabled is False
        assert client.calls_for("set_breakpoints")[-1][1] == (0,), "disabled bp must drop out of the commit"

        breakpoints.enable(bp.ID)
        assert session.Breakpoints[bp.ID].enabled is True
        assert client.calls_for("set_breakpoints")[-1][1] == (1,)
    finally:
        restore()


def test_clear_of_a_function_breakpoint_only_recommits_function_breakpoints() -> None:
    session, client, restore = _new_session_env()
    try:
        _connect(session, client)
        fb = breakpoints.fbreak("myfunc")
        source_calls_before = client.count("set_breakpoints")

        breakpoints.clear(fb.ID)
        assert fb.ID not in session.Breakpoints
        assert client.count("set_breakpoints") == source_calls_before, "must not touch source breakpoints"
        assert client.count("set_function_breakpoints") == 2, "one from fbreak(), one from clear()"
    finally:
        restore()


# ============================================================ commit_all()

def test_commit_all_groups_by_path_and_skips_disabled() -> None:
    session, client, restore = _new_session_env()
    try:
        _connect(session, client)
        breakpoints.sbreak("a.py", 1)
        breakpoints.sbreak("a.py", 2)
        b_bp = breakpoints.sbreak("b.py", 3)
        breakpoints.fbreak("myfunc")
        breakpoints.disable(b_bp.ID)

        client.calls.clear()
        breakpoints.commit_all()

        source_calls = client.calls_for("set_breakpoints")
        assert len(source_calls) == 1, "only a.py has an enabled source breakpoint left"
        assert source_calls[0][1] == (2,)
        assert client.count("set_function_breakpoints") == 1
    finally:
        restore()


def test_a_breakpoint_set_on_one_caller_thread_is_visible_to_commit_all_elsewhere() -> None:
    """SESSION.Breakpoints is shared program-lifetime state, not per-caller
    like the cursor table (doc/architecture.md §2)."""
    session, client, restore = _new_session_env()
    try:
        _connect(session, client)
        setter = _in_a_thread(lambda: breakpoints.sbreak("shared.py", 9))
        setter.join(2)
        assert not setter.is_alive()

        client.calls.clear()
        breakpoints.commit_all()

        source_calls = client.calls_for("set_breakpoints")
        assert len(source_calls) == 1 and source_calls[0][1] == (1,)
    finally:
        restore()


# ============================================================ breakpoints()

def test_breakpoints_is_the_session_dict_wrapped_not_copied() -> None:
    """model.Breakpoints is a dict subclass over SESSION.Breakpoints's own
    mapping -- one source of truth, per doc/architecture.md's redesign
    section. breakpoints() constructs a fresh wrapper each call, but its
    contents are the very same Breakpoint objects, not copies."""
    session, client, restore = _new_session_env()
    try:
        bp = breakpoints.sbreak("a.py", 1)

        result = breakpoints.breakpoints()
        assert isinstance(result, model.Breakpoints)
        assert isinstance(result, dict)
        assert len(result) == 1
        assert result[bp.ID] is bp
    finally:
        restore()


def test_breakpoints_repr_is_empty_with_none_set() -> None:
    session, client, restore = _new_session_env()
    try:
        assert repr(breakpoints.breakpoints()) == "no breakpoints set"
    finally:
        restore()


def test_breakpoints_repr_groups_by_file_sorted_by_line_then_lists_functions() -> None:
    session, client, restore = _new_session_env()
    try:
        breakpoints.sbreak("b.py", 9)
        breakpoints.sbreak("a.py", 2, condition="x > 1")
        breakpoints.sbreak("a.py", 1)
        breakpoints.fbreak("myfunc", condition="y")
        breakpoints.disable(list(session.Breakpoints)[0])  # first sbreak: b.py:9

        text = repr(breakpoints.breakpoints())
        lines = text.splitlines()

        assert lines == [
            "a.py:1 [enabled]",
            "a.py:2 [enabled] (condition='x > 1')",
            "b.py:9 [disabled]",
            "function myfunc [enabled] (condition='y')",
        ], text
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
