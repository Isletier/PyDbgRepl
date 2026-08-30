"""Unit tests for `commands/inspect_.py`: p, locals, globals_, setvar, whatis,
exception_info, completions.

No pydevd, no sockets -- same `FakeClient`-plus-`sys.modules`-scan harness as
`test_execution.py`; see that file's module docstring for why the scan exists.
Duplicated locally (as every test file in this repo already does for its own
small helpers) rather than imported, since these tests need a different
`FakeClient` shape than `test_execution.py`'s.

Run from the repo root with the venv active:

    python -m pdvp.test.test_inspect
"""
import importlib
import sys

inspect_ = importlib.import_module("pdvp.commands.inspect_")
from ..model import CompletionList, Error, ExceptionInfo, Scope, Status
from ..session import SESSION as _REAL_SESSION
from ..session import Session

# ---------------------------------------------------------------- fakes

class _Message:
    """What Client hands the reducer for a DAP event -- same shape test_session.py uses."""

    def __init__(self, event: str, body):
        self.event = event
        self.body = body


class _Body:
    """A response body: attribute access over a dict, like the real schema objects.

    `to_dict()` mirrors the real `BaseSchema.to_dict()` closely enough for
    these tests: the real ones recurse into nested schema refs, this one has
    no nested schema fakes to recurse into, so a flat copy is equivalent.
    """

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class _Response:
    def __init__(self, success: bool = True, **body_kw):
        self.success = success
        self.body = _Body(**body_kw)


class FakeClient:
    """Stands in for `dap.Client`'s inspection surface. Canned responses are
    mutable attributes so a test can shape one before calling the command."""

    def __init__(self):
        self.calls: list[str] = []
        self.raise_on: dict[str, Exception] = {}
        self.evaluate_response = _Response(result="3", type="int")
        self.scopes_response = _Response(scopes=[
            {"name": "Locals", "variablesReference": 10},
            {"name": "Globals", "variablesReference": 20},
        ])
        self.variables_by_ref: dict[int, list[dict]] = {
            10: [{"name": "a", "value": "1"}], 20: [],
        }
        # The real Client.exception_info() returns a schema response object
        # (ExceptionInfoResponse) with the fields under `.body`, not at the
        # top level -- this fake matches that shape on purpose (see the
        # exception_info tests below).
        self.exception_info_response = _Response(exceptionId="ValueError", description="boom")
        self.completions_response = _Response(targets=[{"label": "a"}, {"label": "abs"}])

    def _record(self, name: str) -> None:
        self.calls.append(name)
        failure = self.raise_on.pop(name, None)
        if failure is not None:
            raise failure

    def evaluate(self, expression, frame_id=None, context=None):
        self._record("evaluate")
        return self.evaluate_response

    def scopes(self, frame_id):
        self._record("scopes")
        return self.scopes_response

    def variables(self, variables_reference, filter=None, start=None, count=None):
        self._record("variables")
        return _Response(variables=self.variables_by_ref.get(variables_reference, []))

    def exception_info(self, thread_id):
        self._record("exception_info")
        return self.exception_info_response

    def completions(self, text, column, frame_id=None, line=None):
        self._record("completions")
        return self.completions_response


def _session_bound_modules() -> list:
    """Every currently-loaded module whose `SESSION` name is the real
    singleton -- found by scanning, not a maintained list (see test_execution.py)."""
    return [m for name, m in sys.modules.items()
            if name.startswith("pdvp.") and name != "pdvp.session"
            and getattr(m, "SESSION", None) is _REAL_SESSION]


def _new_session_env():
    """A fresh Session, wired into every command module that binds SESSION at
    import time. Returns (session, client, restore)."""
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


def _stopped_at_a_frame(session, client, thread_id: int = 1, frame_id: int = 100) -> None:
    """A connected session, `thread_id` stopped, with `frame_id` selected as
    the caller's current frame -- the state every happy-path test starts from."""
    session.begin(client=client)
    session.reduce(_Message("thread", {"threadId": thread_id, "reason": "started"}))
    session.reduce(_Message("stopped", {"threadId": thread_id, "reason": "breakpoint"}))
    session.current_thread_id = thread_id
    session.current_frame_id = frame_id


# ============================================================= read guards
#
# p, locals, globals_, setvar, whatis and completions all go through
# SESSION.require_frame(), which chains require_stopped() first. The four
# ways that can fail are the same regardless of which command asks, so this
# table drives all of them rather than repeating the setup six times.

def _not_connected(session, client) -> None:
    pass  # fresh Session: never begin()'d


def _no_current_thread(session, client) -> None:
    session.begin(client=client)


def _thread_running(session, client) -> None:
    session.begin(client=client)
    session.reduce(_Message("thread", {"threadId": 1, "reason": "started"}))
    session.current_thread_id = 1


def _stale_frame(session, client) -> None:
    _stopped_at_a_frame(session, client)
    session.note_resume(1)
    session.reduce(_Message("stopped", {"threadId": 1, "reason": "step"}))


_GUARD_SCENARIOS = [
    ("not connected", _not_connected, "not connected"),
    ("no current thread", _no_current_thread, "no current thread"),
    ("thread running", _thread_running, "running"),
    ("stale frame", _stale_frame, "stale"),
]

_FRAME_COMMANDS = [
    ("p", lambda: inspect_.p("1 + 1")),
    ("locals", lambda: inspect_.locals()),
    ("globals_", lambda: inspect_.globals_()),
    ("setvar", lambda: inspect_.setvar("x", "1")),
    ("whatis", lambda: inspect_.whatis("1")),
    ("completions", lambda: inspect_.completions("a", 1)),
]


def test_every_frame_bound_command_reports_the_specific_guard_failure() -> None:
    for scenario_name, setup, expected_substring in _GUARD_SCENARIOS:
        for command_name, call in _FRAME_COMMANDS:
            session, client, restore = _new_session_env()
            try:
                setup(session, client)
                result = call()
                assert isinstance(result, Error), (scenario_name, command_name, result)
                assert expected_substring in result, (scenario_name, command_name, result)
            finally:
                restore()


def test_exception_info_reports_the_specific_guard_failure_via_require_stopped() -> None:
    """exception_info() takes an explicit/resolved thread and goes through
    require_stopped() directly rather than require_frame() -- no frame/epoch
    guard applies to it, so only three of the four scenarios are relevant."""
    for scenario_name, setup, expected_substring in _GUARD_SCENARIOS[:3]:
        session, client, restore = _new_session_env()
        try:
            setup(session, client)
            result = inspect_.exception_info()
            assert isinstance(result, Error), (scenario_name, result)
            assert expected_substring in result, (scenario_name, result)
        finally:
            restore()


# ================================================================ happy paths

def test_p_evaluates_in_the_current_frame() -> None:
    session, client, restore = _new_session_env()
    try:
        _stopped_at_a_frame(session, client)
        result = inspect_.p("1 + 2")
        assert isinstance(result, Status) and not isinstance(result, Error), result
        assert result == "3", result
        assert client.calls == ["evaluate"]
    finally:
        restore()


def test_setvar_assigns_and_reports_the_assignment_text() -> None:
    session, client, restore = _new_session_env()
    try:
        _stopped_at_a_frame(session, client)
        result = inspect_.setvar("x", "5")
        assert isinstance(result, Status) and not isinstance(result, Error), result
        assert result == "x = 5", result
    finally:
        restore()


def test_whatis_reports_the_evaluated_type() -> None:
    session, client, restore = _new_session_env()
    try:
        _stopped_at_a_frame(session, client)
        client.evaluate_response = _Response(result="3", type="int")
        result = inspect_.whatis("1 + 2")
        assert result == "int", result
    finally:
        restore()


def test_locals_and_globals_read_the_matching_scope() -> None:
    session, client, restore = _new_session_env()
    try:
        _stopped_at_a_frame(session, client)
        loc = inspect_.locals()
        assert isinstance(loc, Scope) and not isinstance(loc, Error), loc
        assert loc == [{"name": "a", "value": "1"}], loc

        glb = inspect_.globals_()
        assert isinstance(glb, Scope) and not isinstance(glb, Error), glb
        assert glb == [], glb
    finally:
        restore()


def test_completions_happy_path_returns_the_targets() -> None:
    session, client, restore = _new_session_env()
    try:
        _stopped_at_a_frame(session, client)
        result = inspect_.completions("a", 1)
        assert isinstance(result, CompletionList) and not isinstance(result, Error), result
        assert len(result) == 2, result
    finally:
        restore()



# ======================================================== exception_info()
#
# Fixed during this test pass: exception_info() used to do
# `return ExceptionInfo(info)` on the *whole* response object rather than
# `info.body.to_dict()` -- the real ExceptionInfoResponse is a
# `__slots__`-based BaseSchema, not dict-constructible, so a successful
# request crashed with TypeError instead of ever returning an ExceptionInfo.
# The DAPError-path test below predates the fix and still holds; the
# happy-path test was added once the fix landed.

def test_exception_info_dap_error_path_still_returns_a_clean_error() -> None:
    from .. import dap as _dap
    from ..model import ErrorKind, PydevdRefused
    session, client, restore = _new_session_env()
    try:
        _stopped_at_a_frame(session, client)
        cause = _dap.DAPError("no exception on this thread")
        client.raise_on["exception_info"] = cause
        result = inspect_.exception_info()
        assert isinstance(result, Error), result
        assert "no exception on this thread" in result, result
        assert isinstance(result, PydevdRefused), result
        assert result.kind is ErrorKind.PYDEVD_REFUSED
        assert result.cause is cause
    finally:
        restore()


def test_exception_info_happy_path_returns_the_wrapped_result() -> None:
    session, client, restore = _new_session_env()
    try:
        _stopped_at_a_frame(session, client)
        result = inspect_.exception_info()
        assert isinstance(result, ExceptionInfo), result
        assert result["exceptionId"] == "ValueError", result
        assert result["description"] == "boom", result
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
