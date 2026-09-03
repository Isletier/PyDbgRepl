"""Unit tests for `commands/location.py`: current_location, current_file,
resolve_path_line.

No pydevd, no sockets -- same `FakeClient`-plus-`sys.modules`-scan harness as
`test_execution.py`, duplicated locally with only the client surface this
module needs.

Run from the repo root with the venv active:

    python -m pdvp.core.test.test_location
"""
import importlib
import sys

location = importlib.import_module("pdvp.core.commands.location")
from pdvp.core import dap as _dap
from pdvp.core.model import Error
from pdvp.core.session import SESSION as _REAL_SESSION
from pdvp.core.session import Session

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
    """Stands in for `dap.Client`'s `stack_trace()`."""

    def __init__(self):
        self.calls: list[int] = []
        self.frames: list[dict] = []
        self.raise_on_stack_trace: Exception | None = None

    def stack_trace(self, thread_id, start_frame=None, levels=None):
        self.calls.append(thread_id)
        if self.raise_on_stack_trace is not None:
            raise self.raise_on_stack_trace
        return _Response(stackFrames=self.frames)


def _session_bound_modules() -> list:
    return [m for name, m in sys.modules.items()
            if name.startswith("pdvp.") and name != "pdvp.core.session"
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


def _with_config_file(path):
    """Temporarily set CONFIG.file, restoring it afterward -- CONFIG is a
    module-level singleton shared with the rest of the process."""
    from pdvp.core.config import CONFIG
    before = CONFIG.file
    CONFIG.file = path

    class _Restorer:
        def __enter__(self_inner):
            return None

        def __exit__(self_inner, *exc):
            CONFIG.file = before
            return False

    return _Restorer()


# ============================================================ current_location

def test_current_location_reads_the_frame_matching_the_current_selection() -> None:
    session, client, restore = _new_session_env()
    try:
        session.begin(client=client)
        session.reduce(_Message("thread", {"threadId": 1, "reason": "started"}))
        session.reduce(_Message("stopped", {"threadId": 1, "reason": "breakpoint"}))
        session.current_thread_id = 1
        session.current_frame_id = 200
        client.frames = [
            {"id": 200, "name": "inner", "line": 7, "source": {"path": "/tmp/t.py"}},
            {"id": 201, "name": "outer", "line": 12, "source": {"path": "/tmp/t.py"}},
        ]

        path, line = location.current_location()
        assert path == "/tmp/t.py", path
        assert line == 7, line
        assert client.calls == [1]
    finally:
        restore()


def test_current_location_falls_back_when_not_connected() -> None:
    session, client, restore = _new_session_env()
    try:
        with _with_config_file("/tmp/fallback.py"):
            path, line = location.current_location()
            assert path == "/tmp/fallback.py", path
            assert line is None, line
    finally:
        restore()


def test_current_location_falls_back_when_no_frame_is_selected() -> None:
    session, client, restore = _new_session_env()
    try:
        session.begin(client=client)
        with _with_config_file("/tmp/fallback.py"):
            path, line = location.current_location()
            assert path == "/tmp/fallback.py", path
            assert line is None, line
            assert client.calls == [], "must not round-trip with no frame selected"
    finally:
        restore()


def test_current_location_falls_back_when_the_frame_id_is_not_in_the_trace() -> None:
    session, client, restore = _new_session_env()
    try:
        session.begin(client=client)
        session.reduce(_Message("thread", {"threadId": 1, "reason": "started"}))
        session.reduce(_Message("stopped", {"threadId": 1, "reason": "breakpoint"}))
        session.current_thread_id = 1
        session.current_frame_id = 999  # not in client.frames
        client.frames = [{"id": 1, "name": "f", "line": 1, "source": {"path": "/tmp/t.py"}}]

        with _with_config_file("/tmp/fallback.py"):
            path, line = location.current_location()
            assert path == "/tmp/fallback.py", path
            assert line is None, line
    finally:
        restore()


def test_current_location_swallows_a_dap_error_from_the_round_trip() -> None:
    """The source has a bare `except _dap.DAPError: pass` around the
    stack_trace() round trip -- pinned as current behavior. Whether silently
    falling back (rather than surfacing the failure) is the right call is not
    this test's business; it's the one place in the codebase that eats a
    DAPError outright, which seemed worth a direct test rather than only
    ever being exercised by accident."""
    session, client, restore = _new_session_env()
    try:
        session.begin(client=client)
        session.reduce(_Message("thread", {"threadId": 1, "reason": "started"}))
        session.reduce(_Message("stopped", {"threadId": 1, "reason": "breakpoint"}))
        session.current_thread_id = 1
        session.current_frame_id = 200
        client.raise_on_stack_trace = _dap.DAPError("thread is running")

        with _with_config_file("/tmp/fallback.py"):
            path, line = location.current_location()
            assert path == "/tmp/fallback.py", path
            assert line is None, line
    finally:
        restore()


def test_current_file_is_the_first_element_of_current_location() -> None:
    session, client, restore = _new_session_env()
    try:
        with _with_config_file("/tmp/fallback.py"):
            assert location.current_file() == "/tmp/fallback.py"
    finally:
        restore()


# =============================================================== resolve_path_line

def test_resolve_path_line_bare_int_uses_the_current_file() -> None:
    session, client, restore = _new_session_env()
    try:
        with _with_config_file("/tmp/fallback.py"):
            result = location.resolve_path_line(10, None)
            assert result == ("/tmp/fallback.py", 10), result
    finally:
        restore()


def test_resolve_path_line_bare_int_with_no_current_file_is_an_error() -> None:
    session, client, restore = _new_session_env()
    try:
        with _with_config_file(None):
            result = location.resolve_path_line(10, None)
            assert isinstance(result, Error), result
            assert "no current file" in result, result
    finally:
        restore()


def test_resolve_path_line_passes_through_an_explicit_path_and_line() -> None:
    session, client, restore = _new_session_env()
    try:
        result = location.resolve_path_line("a.py", 5)
        assert result == ("a.py", 5), result
    finally:
        restore()


def test_resolve_path_line_a_path_with_no_line_is_an_error() -> None:
    session, client, restore = _new_session_env()
    try:
        result = location.resolve_path_line("a.py", None)
        assert isinstance(result, Error), result
        assert "line number required" in result, result
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
