"""Unit tests for `commands/misc.py`: modules, pydevd_info.

No pydevd, no sockets -- same `FakeClient`-plus-`sys.modules`-scan harness as
`test_execution.py`, duplicated locally with only the client surface this
module needs.

Run from the repo root with the venv active:

    python -m pdvp.test.test_misc
"""
import importlib
import sys

misc = importlib.import_module("pdvp.commands.misc")
from .. import dap as _dap
from ..model import Error, InfoSections, ModuleList
from ..session import SESSION as _REAL_SESSION
from ..session import Session

# ---------------------------------------------------------------- fakes

class _Body:
    """`to_dict()` mirrors the real `BaseSchema.to_dict()` closely enough for
    these tests: the real ones recurse into nested schema refs, this one has
    no nested schema fakes to recurse into, so a flat copy is equivalent."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class _Response:
    """Same shape the real Client returns: a response object with `.body`,
    not something itself dict-like or `.get()`-able. That distinction is the
    point of the two "currently raises" tests below."""

    def __init__(self, success: bool = True, **body_kw):
        self.success = success
        self.body = _Body(**body_kw)


class FakeClient:
    def __init__(self):
        self.calls: list[str] = []
        self.raise_on: dict[str, Exception] = {}
        self.modules_response = _Response(modules=[])
        self.system_info_response = _Response(process={"pid": 123})

    def _record(self, name: str) -> None:
        self.calls.append(name)
        failure = self.raise_on.pop(name, None)
        if failure is not None:
            raise failure

    def modules(self, start_module=None, module_count=None):
        self._record("modules")
        return self.modules_response

    def pydevd_system_info(self):
        self._record("pydevd_system_info")
        return self.system_info_response


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


# ================================================================= modules()

def test_modules_not_connected_is_an_error() -> None:
    session, client, restore = _new_session_env()
    try:
        assert isinstance(misc.modules(), Error)
    finally:
        restore()


def test_modules_dap_error_path_still_returns_a_clean_error() -> None:
    session, client, restore = _new_session_env()
    try:
        session.begin(client=client)
        client.raise_on["modules"] = _dap.DAPError("boom")
        result = misc.modules()
        assert isinstance(result, Error), result
        assert "boom" in result, result
    finally:
        restore()


def test_modules_happy_path_returns_the_wrapped_list() -> None:
    """Fixed during this test pass: `modules()` used to do
    `ModuleList(result.get("modules", []))` on the raw response object rather
    than `result.body.modules` -- the real Client's response has no `.get()`,
    so a successful request crashed with AttributeError instead of ever
    reaching `ModuleList(...)`."""
    session, client, restore = _new_session_env()
    try:
        session.begin(client=client)
        client.modules_response = _Response(modules=[{"id": 1, "name": "m"}])
        result = misc.modules()
        assert isinstance(result, ModuleList), result
        assert result == [{"id": 1, "name": "m"}], result
    finally:
        restore()


# ============================================================= pydevd_info()

def test_pydevd_info_not_connected_is_an_error() -> None:
    session, client, restore = _new_session_env()
    try:
        assert isinstance(misc.pydevd_info(), Error)
    finally:
        restore()


def test_pydevd_info_dap_error_path_still_returns_a_clean_error() -> None:
    session, client, restore = _new_session_env()
    try:
        session.begin(client=client)
        client.raise_on["pydevd_system_info"] = _dap.DAPError("boom")
        result = misc.pydevd_info()
        assert isinstance(result, Error), result
        assert "boom" in result, result
    finally:
        restore()


def test_pydevd_info_happy_path_returns_the_wrapped_sections() -> None:
    """Same bug class as modules() above, fixed during this test pass:
    `InfoSections(result)` -- `InfoSections` is a `dict` subclass -- was
    called on the raw response object rather than `result.body.to_dict()`.
    `dict(x)` requires `x` to be a mapping or an iterable of pairs; a plain
    response object is neither, so this raised TypeError on every successful
    request."""
    session, client, restore = _new_session_env()
    try:
        session.begin(client=client)
        client.system_info_response = _Response(process={"pid": 1}, python={"version": "3.x"})
        result = misc.pydevd_info()
        assert isinstance(result, InfoSections), result
        assert result["process"] == {"pid": 1}, result
        assert result["python"] == {"version": "3.x"}, result
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
