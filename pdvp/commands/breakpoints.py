"""Breakpoints: breakpoint, clear, catch, tbreak, enable/disable, ignore, funcbreak."""

import pdvp.model as model
from pdvp.session import SESSION
from pathlib import Path
from pdvp.dap.client import Client
import pdvp.schema.pydevd_schema as schema


__all__ = [
    "breakpoint", "clear", "catch", "tbreak", "sbreak",
    "enable", "disable", "ignore", "breakpoints", "funcbreak",
]


def _send_breakpoints(path: str) -> None:
    commit_source_breakpoints(path)

def commit_all() -> None:
    sources: set[model.SourcePath] = set()

    for key, breakp in SESSION.Breakpoints.items():
        if not isinstance(breakp, model.SourceBreakpoint) or not breakp.enabled:
            continue

        sources.add(breakp.path)


    print("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    print(sources)
    for source in sources:
        commit_source_breakpoints(source)

    return


def commit_source_breakpoints(path: model.SourcePath):
    if SESSION.client is None:
        return

    source_br_list: list[model.SourceBreakpoint] = list()
    for i, breakp in SESSION.Breakpoints.items():
        if not isinstance(breakp, model.SourceBreakpoint) or not breakp.enabled:
            continue

        if breakp.path == path:
            source_br_list.append(breakp)


    serialized_br: list[schema.SourceBreakpoint] = list()
    for breakp in source_br_list:
        serialized_br.append(schema.SourceBreakpoint(
            breakp.line,
            None,
            breakp.condition,
            breakp.hitCondition,
            breakp.logMessage
        ))


    serialized_source: schema.Source = SESSION.sourceMap.get_source(path)

    responce = SESSION.client.set_breakpoints(serialized_source, serialized_br)
    if not responce.success:
        raise model.PDVPError()

    breakpoints: list[schema.Breakpoint] = responce.body.breakpoints
    source_path = SESSION.sourceMap.register_source(breakpoints[0].source)
    SESSION.Breakpoints[source_br.ID] = source_br

    for index, b in enumerate(responce.body.breakpoints):
        source_breakpoint: schema.Breakpoint = b;
        destination_breakpoint: model.SourceBreakpoint = session.breakpoints[source_br_list[index].ID]

        destination_breakpoint.verified = source_breakpoint.verified
        destination_breakpoint.line = source_breakpoint.line
        destination_breakpoint.path = source_path

    return


def sbreak(*args, condition: str | None = None, hit_condition: str | None = None, log_message: str | None = None) -> model.SourceBreakpoint:
    path = "CURRENT_PATH_CURRENTLY_NOT_IMPLEMENTED"
    line = None

    match args:
        case [int(l), *rest] if len(rest) <= 3 and all(isinstance(x, str) for x in rest):
            line = l
            pass
        case [str(p), int(l), *rest] if len(rest) <= 3 and all(isinstance(x, str) for x in rest):
            path = p
            line = l
        case _:
            raise TypeError("Invalid argument types for sbreak call")


    #add path/file validation here, i guess?

    source_br: model.SourceBreakpoint = model.SourceBreakpoint(
            path,
            line,
            condition,
            hit_condition,
            log_message
    )

    SESSION.Breakpoints[source_br.ID] = source_br

    #pass possible exception, keep internal model as source of truth, 
    #until state is resynced some way or another
    commit_source_breakpoints(path)

    return source_br


def breakpoint(*args) -> model.Breakpoint | None:

    match args:
        case [int() as line]:
            return sbreak(line)
        case [str() as path, int() as line]:
            return sbreak(path, line);
        case [str() as path, int() as line, *rest] if len(rest) <= 3 and all(isinstance(x, str) for x in rest):
            cond, hit, log = rest + [None] * (3 - len(rest))
            return sbreak(line, path, cond, hit, log)










































































from .. import dap as _dap
from pdvp.session import SESSION
from . import _internal
from ._display import Breakpoints, Error, Status
from ._internal import _resolve_path_line


#def breakpoint(path_or_line: str | int, line: int | None = None,
#               condition: str | None = None, log_message: str | None = None) -> Status | Error:
#    """Set a line breakpoint at `path_or_line`:`line`.
#
#    `path_or_line` may be a bare line number in the current file instead of a
#    path -- see "Argument conventions" in command_reference.md. `condition`
#    makes it conditional; `log_message` makes it a logpoint (prints the
#    message and continues, without stopping).
#    """
#    resolved = _resolve_path_line(path_or_line, line)
#    if isinstance(resolved, Error):
#        return resolved
#    path, line = resolved
#
#    bp = {"line": line, "enabled": True}
#    if condition is not None:
#        bp["condition"] = condition
#    if log_message is not None:
#        bp["logMessage"] = log_message
#
#    bps = SESSION.breakpoints.setdefault(path, [])
#    bps[:] = [b for b in bps if b["line"] != line] + [bp]
#
#    _esend_breakpoints(path)
#    return Status(f"breakpoint set at {path}:{line}")


def clear(path_or_line: str | int, line: int | None = None) -> Status | Error:
    """Remove the breakpoint at `path_or_line`:`line`, if any."""
    resolved = _resolve_path_line(path_or_line, line)
    if isinstance(resolved, Error):
        return resolved
    path, line = resolved

    bps = SESSION.breakpoints.get(path, [])
    bps[:] = [b for b in bps if b["line"] != line]
    SESSION.temporary_breakpoints.discard((path, line))

    _send_breakpoints(path)
    return Status(f"breakpoint cleared at {path}:{line}")


def catch(*filters: str) -> Status:
    """Set exception breakpoint filters, e.g. catch("raised", "uncaught")."""
    SESSION.exception_filters = list(filters)
    if SESSION.client is not None:
        SESSION.client.set_exception_breakpoints(SESSION.exception_filters, [], [])
    return Status(f"exception filters = {SESSION.exception_filters}")


def tbreak(path_or_line: str | int, line: int | None = None, condition: str | None = None) -> Status | Error:
    """Set a temporary breakpoint: cleared automatically the first time it's hit."""
    resolved = _resolve_path_line(path_or_line, line)
    if isinstance(resolved, Error):
        return resolved
    path, line = resolved

    result = breakpoint(path, line, condition=condition)
    if isinstance(result, Error):
        return result
    SESSION.temporary_breakpoints.add((path, line))
    return result


def _set_enabled(path_or_line: str | int, line: int | None, enabled: bool) -> Status | Error:
    resolved = _resolve_path_line(path_or_line, line)
    if isinstance(resolved, Error):
        return resolved
    path, line = resolved

    for b in SESSION.breakpoints.get(path, []):
        if b["line"] == line:
            b["enabled"] = enabled
            _send_breakpoints(path)
            return Status(f"breakpoint at {path}:{line} {'enabled' if enabled else 'disabled'}")
    return Error(f"no breakpoint at {path}:{line}")


def enable(path_or_line: str | int, line: int | None = None) -> Status | Error:
    """Re-enable a breakpoint without forgetting its condition/etc."""
    return _set_enabled(path_or_line, line, True)


def disable(path_or_line: str | int, line: int | None = None) -> Status | Error:
    """Disable a breakpoint without forgetting it -- omitted from setBreakpoints until re-enabled."""
    return _set_enabled(path_or_line, line, False)


def ignore(path_or_line: str | int, line_or_count: int, count: int | None = None) -> Status | Error:
    """Ignore the next `count` hits of a breakpoint, via pydevd's hitCondition.

    Normally `ignore(path_or_line, line, count)`. If `count` is omitted,
    `(path_or_line, line_or_count)` is instead `(line, count)` against
    `_current_file()` -- the same shortcut convention as `breakpoint()`.
    """
    if count is None:
        path = _internal._current_file()
        if path is None:
            return Error("no current file (pass an explicit path)")
        line, count = path_or_line, line_or_count
    else:
        resolved = _resolve_path_line(path_or_line, line_or_count)
        if isinstance(resolved, Error):
            return resolved
        path, line = resolved

    for b in SESSION.breakpoints.get(path, []):
        if b["line"] == line:
            if count > 0:
                b["hitCondition"] = f">= {count + 1}"
            else:
                b.pop("hitCondition", None)
            _send_breakpoints(path)
            return Status(f"breakpoint at {path}:{line} will ignore the next {count} hits")
    return Error(f"no breakpoint at {path}:{line}")


def funcbreak(name: str, condition: str | None = None) -> Status:
    """Set a breakpoint on entry to function `name` (setFunctionBreakpoints)."""
    fb = {"name": name}
    if condition is not None:
        fb["condition"] = condition

    bps = SESSION.function_breakpoints
    bps[:] = [b for b in bps if b["name"] != name] + [fb]

    if SESSION.client is not None:
        SESSION.client.set_function_breakpoints(bps)
    return Status(f"function breakpoint set at {name}")


def breakpoints() -> Breakpoints:
    """All breakpoints, function breakpoints, and exception filters."""
    return Breakpoints(SESSION.breakpoints, SESSION.function_breakpoints, SESSION.exception_filters)


def _on_stopped(reason: str | None, top: dict | None) -> str | None:
    if reason != "breakpoint" or top is None:
        return None
    path = (top.get("source") or {}).get("path")
    line = top.get("line")
    if (path, line) in SESSION.temporary_breakpoints:
        return str(clear(path, line))
    return None


_internal.post_stop_hooks.append(_on_stopped)

