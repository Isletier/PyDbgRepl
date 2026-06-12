"""Inspection: p, locals, globals_, setvar, whatis, display/undisplay, exception_info, completions."""
from .. import dap as _dap
from ..session import SESSION
from . import _internal
from ._internal import _ensure_dap_paused

__all__ = [
    "p", "locals", "globals_", "setvar", "whatis",
    "display", "undisplay", "exception_info", "completions",
]


def p(expression: str) -> None:
    """Evaluate `expression` in the current frame and print the result."""
    if not _ensure_dap_paused():
        return

    try:
        result = SESSION.dap.evaluate(expression, frame_id=SESSION.current_frame_id, context="repl")
    except _dap.DAPError as e:
        print(f"error: {e}")
        return
    print(result["result"])


def _print_scope(scope_name: str) -> None:
    if not _ensure_dap_paused():
        return
    if SESSION.current_frame_id is None:
        print("error: no current frame (use bt())")
        return

    for scope in SESSION.dap.scopes(SESSION.current_frame_id)["scopes"]:
        if scope["name"] != scope_name:
            continue
        for v in SESSION.dap.variables(scope["variablesReference"])["variables"]:
            print(f"{v['name']} = {v['value']}")


def locals() -> None:
    """Print local variables of the current frame."""
    _print_scope("Locals")


def globals_() -> None:
    """Print global variables visible from the current frame."""
    _print_scope("Globals")


def setvar(name: str, value: str) -> None:
    """Assign `value` (a Python expression) to variable `name` in the current frame."""
    if not _ensure_dap_paused():
        return

    try:
        SESSION.dap.evaluate(f"{name} = {value}", frame_id=SESSION.current_frame_id, context="repl")
    except _dap.DAPError as e:
        print(f"error: {e}")
        return
    print(f"{name} = {value}")


def whatis(expression: str) -> None:
    """Print the type of `expression`, evaluated in the current frame."""
    if not _ensure_dap_paused():
        return

    try:
        result = SESSION.dap.evaluate(expression, frame_id=SESSION.current_frame_id, context="hover")
    except _dap.DAPError as e:
        print(f"error: {e}")
        return
    print(result.get("type", "?"))


def display(expression: str) -> None:
    """Add `expression` to the list re-evaluated and printed after every stop."""
    display_id = max((d["id"] for d in SESSION.displays), default=0) + 1
    SESSION.displays.append({"id": display_id, "expr": expression})
    print(f"{display_id}: {expression}")
    if SESSION.dap is not None and not SESSION.running:
        _show_display(SESSION.displays[-1])


def undisplay(display_id: int) -> None:
    """Remove a display expression added with display()."""
    before = len(SESSION.displays)
    SESSION.displays[:] = [d for d in SESSION.displays if d["id"] != display_id]
    if len(SESSION.displays) == before:
        print(f"error: no display {display_id}")
        return
    print(f"{display_id}: deleted")


def _show_display(d: dict) -> None:
    try:
        result = SESSION.dap.evaluate(d["expr"], frame_id=SESSION.current_frame_id, context="repl")
    except _dap.DAPError as e:
        print(f"{d['id']}: {d['expr']} = <error: {e}>")
        return
    print(f"{d['id']}: {d['expr']} = {result['result']}")


def exception_info() -> None:
    """Print details of the exception that stopped the current thread, if any."""
    if not _internal._ensure_thread_paused():
        return

    try:
        info = SESSION.dap.exception_info(SESSION.current_thread_id)
    except _dap.DAPError as e:
        print(f"error: {e}")
        return

    print(f"{info.get('exceptionId', '?')}: {info.get('description', '')}")
    details = info.get("details") or {}
    if details.get("stackTrace"):
        print(details["stackTrace"])


def completions(text: str, column: int) -> None:
    """Print completion suggestions for `text` (cursor at `column`) in the current frame.

    Backing for future REPL tab-completion (see completion_design.md); not
    normally called directly.
    """
    if not _ensure_dap_paused():
        return

    try:
        result = SESSION.dap.completions(text, column, frame_id=SESSION.current_frame_id)
    except _dap.DAPError as e:
        print(f"error: {e}")
        return
    for item in result.get("targets", []):
        print(item.get("label", item))


def _on_stopped(reason: str | None, top: dict | None) -> None:
    for d in SESSION.displays:
        _show_display(d)


_internal.post_stop_hooks.append(_on_stopped)
