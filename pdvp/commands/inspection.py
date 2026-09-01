"""Inspection: p, locals, globals_, setvar, whatis, exception_info, completions."""
from pdvp import dap as _dap
from pdvp.session import SESSION
from pdvp.model import CompletionList, Error, ExceptionInfo, PydevdRefused, Scope, Status

__all__ = [
    "p", "locals", "globals_", "setvar", "whatis",
    "exception_info", "completions",
]

# Everything here reads through a frame, so everything here goes through
# SESSION.require_frame(): the thread must be stopped *and* the handle must
# still be at the epoch it was minted in. pydevd checks neither -- against a
# running thread it answers with a torn stack and an empty variables list.


def p(expression: str) -> Status | Error:
    """Evaluate `expression` in the current frame and return the result."""
    frame = SESSION.require_frame()
    if isinstance(frame, Error):
        return frame

    try:
        result = SESSION.client.evaluate(expression, frame_id=frame.frame_id, context="repl")
    except _dap.DAPError as e:
        return PydevdRefused(str(e), cause=e)
    return Status(result.body.result)


def _scope(scope_name: str) -> Scope | Error:
    frame = SESSION.require_frame()
    if isinstance(frame, Error):
        return frame

    for scope in SESSION.client.scopes(frame.frame_id).body.scopes:
        if scope["name"] != scope_name:
            continue
        return Scope(SESSION.client.variables(scope["variablesReference"]).body.variables)
    return Scope()


def locals() -> Scope | Error:
    """Local variables of the current frame."""
    return _scope("Locals")


def globals_() -> Scope | Error:
    """Global variables visible from the current frame."""
    return _scope("Globals")


def setvar(name: str, value: str) -> Status | Error:
    """Assign `value` (a Python expression) to variable `name` in the current frame."""
    frame = SESSION.require_frame()
    if isinstance(frame, Error):
        return frame

    try:
        SESSION.client.evaluate(f"{name} = {value}", frame_id=frame.frame_id, context="repl")
    except _dap.DAPError as e:
        return PydevdRefused(str(e), cause=e)
    return Status(f"{name} = {value}")


def whatis(expression: str) -> Status | Error:
    """The type of `expression`, evaluated in the current frame."""
    frame = SESSION.require_frame()
    if isinstance(frame, Error):
        return frame

    try:
        result = SESSION.client.evaluate(expression, frame_id=frame.frame_id, context="hover")
    except _dap.DAPError as e:
        return PydevdRefused(str(e), cause=e)
    return Status(result.body.type or "?")


def exception_info(*, thread: int | None = None) -> ExceptionInfo | Error:
    """Details of the exception that stopped `thread`, defaulting to the current one."""
    thread_id = SESSION.resolve_thread(thread)
    err = SESSION.require_stopped(thread_id)
    if err is not None:
        return err

    try:
        info = SESSION.client.exception_info(thread_id)
    except _dap.DAPError as e:
        return PydevdRefused(str(e), cause=e)

    return ExceptionInfo(info.body.to_dict())


def completions(text: str, column: int) -> CompletionList | Error:
    """Completion suggestions for `text` (cursor at `column`) in the current frame.

    Backing for future REPL tab-completion (see completion_design.md); not
    normally called directly.
    """
    frame = SESSION.require_frame()
    if isinstance(frame, Error):
        return frame

    try:
        result = SESSION.client.completions(text, column, frame_id=frame.frame_id)
    except _dap.DAPError as e:
        return PydevdRefused(str(e), cause=e)
    return CompletionList(result.body.targets)
