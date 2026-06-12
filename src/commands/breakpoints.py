"""Breakpoints: breakpoint, clear, catch, tbreak, enable/disable, ignore, funcbreak."""
from .. import dap as _dap
from ..session import SESSION
from . import _internal
from ._internal import _resolve_path_line

__all__ = [
    "breakpoint", "clear", "catch", "tbreak",
    "enable", "disable", "ignore", "breakpoints", "funcbreak",
]


def _send_breakpoints(path: str) -> None:
    if SESSION.dap is None:
        return
    sent = [
        {k: v for k, v in b.items() if k != "enabled"}
        for b in SESSION.breakpoints.get(path, [])
        if b.get("enabled", True)
    ]
    SESSION.dap.set_breakpoints({"path": path}, sent)


def breakpoint(path_or_line: str | int, line: int | None = None,
               condition: str | None = None, log_message: str | None = None) -> None:
    """Set a line breakpoint at `path_or_line`:`line`.

    `path_or_line` may be a bare line number in the current file instead of a
    path -- see "Argument conventions" in command_reference.md. `condition`
    makes it conditional; `log_message` makes it a logpoint (prints the
    message and continues, without stopping).
    """
    resolved = _resolve_path_line(path_or_line, line)
    if resolved is None:
        return
    path, line = resolved

    bp = {"line": line, "enabled": True}
    if condition is not None:
        bp["condition"] = condition
    if log_message is not None:
        bp["logMessage"] = log_message

    bps = SESSION.breakpoints.setdefault(path, [])
    bps[:] = [b for b in bps if b["line"] != line] + [bp]

    _send_breakpoints(path)
    print(f"breakpoint set at {path}:{line}")


def clear(path_or_line: str | int, line: int | None = None) -> None:
    """Remove the breakpoint at `path_or_line`:`line`, if any."""
    resolved = _resolve_path_line(path_or_line, line)
    if resolved is None:
        return
    path, line = resolved

    bps = SESSION.breakpoints.get(path, [])
    bps[:] = [b for b in bps if b["line"] != line]
    SESSION.temporary_breakpoints.discard((path, line))

    _send_breakpoints(path)
    print(f"breakpoint cleared at {path}:{line}")


def catch(*filters: str) -> None:
    """Set exception breakpoint filters, e.g. catch("raised", "uncaught")."""
    SESSION.exception_filters = list(filters)
    if SESSION.dap is not None:
        SESSION.dap.set_exception_breakpoints(SESSION.exception_filters)
    print(f"exception filters = {SESSION.exception_filters}")


def tbreak(path_or_line: str | int, line: int | None = None, condition: str | None = None) -> None:
    """Set a temporary breakpoint: cleared automatically the first time it's hit."""
    resolved = _resolve_path_line(path_or_line, line)
    if resolved is None:
        return
    path, line = resolved

    breakpoint(path, line, condition=condition)
    SESSION.temporary_breakpoints.add((path, line))


def _set_enabled(path_or_line: str | int, line: int | None, enabled: bool) -> None:
    resolved = _resolve_path_line(path_or_line, line)
    if resolved is None:
        return
    path, line = resolved

    for b in SESSION.breakpoints.get(path, []):
        if b["line"] == line:
            b["enabled"] = enabled
            _send_breakpoints(path)
            print(f"breakpoint at {path}:{line} {'enabled' if enabled else 'disabled'}")
            return
    print(f"error: no breakpoint at {path}:{line}")


def enable(path_or_line: str | int, line: int | None = None) -> None:
    """Re-enable a breakpoint without forgetting its condition/etc."""
    _set_enabled(path_or_line, line, True)


def disable(path_or_line: str | int, line: int | None = None) -> None:
    """Disable a breakpoint without forgetting it -- omitted from setBreakpoints until re-enabled."""
    _set_enabled(path_or_line, line, False)


def ignore(path_or_line: str | int, line_or_count: int, count: int | None = None) -> None:
    """Ignore the next `count` hits of a breakpoint, via pydevd's hitCondition.

    Normally `ignore(path_or_line, line, count)`. If `count` is omitted,
    `(path_or_line, line_or_count)` is instead `(line, count)` against
    `_current_file()` -- the same shortcut convention as `breakpoint()`.
    """
    if count is None:
        path = _internal._current_file()
        if path is None:
            print("error: no current file (pass an explicit path)")
            return
        line, count = path_or_line, line_or_count
    else:
        resolved = _resolve_path_line(path_or_line, line_or_count)
        if resolved is None:
            return
        path, line = resolved

    for b in SESSION.breakpoints.get(path, []):
        if b["line"] == line:
            if count > 0:
                b["hitCondition"] = f">= {count + 1}"
            else:
                b.pop("hitCondition", None)
            _send_breakpoints(path)
            print(f"breakpoint at {path}:{line} will ignore the next {count} hits")
            return
    print(f"error: no breakpoint at {path}:{line}")


def funcbreak(name: str, condition: str | None = None) -> None:
    """Set a breakpoint on entry to function `name` (setFunctionBreakpoints)."""
    fb = {"name": name}
    if condition is not None:
        fb["condition"] = condition

    bps = SESSION.function_breakpoints
    bps[:] = [b for b in bps if b["name"] != name] + [fb]

    if SESSION.dap is not None:
        SESSION.dap.set_function_breakpoints(bps)
    print(f"function breakpoint set at {name}")


def breakpoints() -> None:
    """List all breakpoints, function breakpoints, and exception filters."""
    printed = False

    for path, bps in SESSION.breakpoints.items():
        for b in sorted(bps, key=lambda b: b["line"]):
            status = "enabled" if b.get("enabled", True) else "disabled"
            extra = ", ".join(
                f"{k}={v!r}" for k, v in b.items() if k not in ("line", "enabled")
            )
            suffix = f" ({extra})" if extra else ""
            print(f"{path}:{b['line']} [{status}]{suffix}")
            printed = True

    for fb in SESSION.function_breakpoints:
        extra = ", ".join(f"{k}={v!r}" for k, v in fb.items() if k != "name")
        suffix = f" ({extra})" if extra else ""
        print(f"function {fb['name']}{suffix}")
        printed = True

    if SESSION.exception_filters:
        print(f"exception filters: {SESSION.exception_filters}")
        printed = True

    if not printed:
        print("no breakpoints set")


def _on_stopped(reason: str | None, top: dict | None) -> None:
    if reason != "breakpoint" or top is None:
        return
    path = (top.get("source") or {}).get("path")
    line = top.get("line")
    if (path, line) in SESSION.temporary_breakpoints:
        clear(path, line)


_internal.post_stop_hooks.append(_on_stopped)
