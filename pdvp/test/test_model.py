"""Unit tests for `pdvp/model.py`'s `Error` taxonomy: `ErrorKind`, the base
`Error`, and the two typed subclasses (`StaleFrameError`, `PydevdRefused`).

No pydevd, no sockets, no SESSION -- these are plain construction/contract
tests against the types themselves.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all
of them. pytest also collects these directly by name.

Run from the repo root with the venv active:

    python -m pdvp.test.test_model
"""
from pdvp.model import Error, ErrorKind, PydevdRefused, StaleFrameError, Status


def test_error_is_falsy_and_reprs_without_quotes() -> None:
    err = Error("not connected", kind=ErrorKind.NOT_CONNECTED)
    assert bool(err) is False
    assert not err
    assert str(err) == "error: not connected"
    assert repr(err) == "error: not connected"


def test_error_is_a_status_and_a_str() -> None:
    err = Error("boom", kind=ErrorKind.NOT_CONNECTED)
    assert isinstance(err, Status)
    assert isinstance(err, str)


def test_error_requires_a_kind() -> None:
    """Locks in the design decision: no silent 'uncategorized' default --
    every call site in the codebase names a kind explicitly."""
    try:
        Error("no kind given")
    except TypeError:
        pass
    else:
        raise AssertionError("Error() without kind= should raise TypeError")


def test_error_kind_is_set() -> None:
    err = Error("not connected", kind=ErrorKind.NOT_CONNECTED)
    assert err.kind is ErrorKind.NOT_CONNECTED


def test_stale_frame_error_carries_thread_and_epochs() -> None:
    err = StaleFrameError(thread_id=3, stale_epoch=1, current_epoch=2)
    assert isinstance(err, Error)
    assert err.kind is ErrorKind.STALE_FRAME
    assert err.thread_id == 3
    assert err.stale_epoch == 1
    assert err.current_epoch == 2
    assert not err
    assert "stale" in str(err)


def test_pydevd_refused_carries_the_cause() -> None:
    cause = ValueError("pydevd said no")
    err = PydevdRefused("pydevd said no", cause=cause)
    assert isinstance(err, Error)
    assert err.kind is ErrorKind.PYDEVD_REFUSED
    assert err.cause is cause
    assert not err


def test_pydevd_refused_cause_defaults_to_none() -> None:
    """The success=False (no exception) case -- breakpoints.py's commit
    functions hit this when pydevd answers a failed response rather than
    raising DAPError."""
    err = PydevdRefused("failed to set breakpoints")
    assert err.cause is None
    assert err.kind is ErrorKind.PYDEVD_REFUSED


def test_error_kind_is_exhaustively_matchable() -> None:
    """Every member is a distinct, real enum value -- catches an accidental
    duplicate enum.auto() alias slipping in."""
    values = [member.value for member in ErrorKind]
    assert len(values) == len(set(values)), "ErrorKind has duplicate-valued members"
    assert len(list(ErrorKind)) >= 15


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
