"""Breakpoints: breakpoint, clear, catch, tbreak, enable/disable, ignore, funcbreak."""

import pdvp.model as model
from pdvp.session import SESSION
from pathlib import Path
from pdvp.dap.client import Client
import pdvp.schema.pydevd_schema as schema


__all__ = [
    "breakpoint", "clear", "sbreak", "fbreak",
    "enable", "disable","breakpoints"
]


def _send_breakpoints(path: str) -> None:
    commit_source_breakpoints(path)

def commit_all() -> None:
    sources: set[model.SourcePath] = set()

    for key, breakp in SESSION.Breakpoints.items():
        if not isinstance(breakp, model.SourceBreakpoint) or not breakp.enabled:
            continue

        sources.add(breakp.path)


    for source in sources:
        commit_source_breakpoints(source)

    commit_function_breakpoints()

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

    for index, b in enumerate(responce.body.breakpoints):
        source_breakpoint = schema.Breakpoint(**b)
        SESSION.sourceMap.register_source(source_breakpoint.source)

        destination_breakpoint: model.SourceBreakpoint = SESSION.Breakpoints[source_br_list[index].ID]

        destination_breakpoint.verified = source_breakpoint.verified
        destination_breakpoint.line = source_breakpoint.line
        destination_breakpoint.path = path

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

    commit_source_breakpoints(path)

    return source_br

def commit_function_breakpoints():
    if SESSION.client is None:
        return

    func_br_list: list[model.FunctionBreakpoint] = list()
    for i, breakp in SESSION.Breakpoints.items():
        if not isinstance(breakp, model.FunctionBreakpoint) or not breakp.enabled:
            continue

        func_br_list.append(breakp)

    serialized_br: list[schema.FunctionBreakpoint] = list()
    for breakp in func_br_list:
        serialized_br.append(schema.FunctionBreakpoint(
            breakp.name,
            breakp.condition,
            breakp.hitCondition
        ))

    responce = SESSION.client.set_function_breakpoints(serialized_br)
    if not responce.success:
        raise model.PDVPError

    for index, b in enumerate(responce.body.breakpoints):
        source_breakpoint = schema.Breakpoint(**b)
        SESSION.sourceMap.register_source(source_breakpoint.source)

        destination_breakpoint: model.FunctionBreakpoint = SESSION.Breakpoints[func_br_list[index].ID]

        destination_breakpoint.verified = source_breakpoint.verified

    return


def fbreak(func_name: str, condition: str | None = None, hitCondition: str | None = None) -> model.FunctionBreakpoint:
    function_break: model.FunctionBreakpoint = model.FunctionBreakpoint(
        func_name,
        condition,
        hitCondition
    )


    SESSION.Breakpoints[function_break.ID] = function_break
    commit_function_breakpoints()

    return SESSION.Breakpoints[function_break.ID]


def breakpoint(*args) -> model.Breakpoint | None:

    match args:
        case [int() as line]:
            return sbreak(line)
        case [str() as path, int() as line]:
            return sbreak(path, line);
        case [str() as path, int() as line, *rest] if len(rest) <= 3 and all(isinstance(x, str) for x in rest):
            cond, hit, log = rest + [None] * (3 - len(rest))
            return sbreak(line, path, cond, hit, log)
        case [str() as func_name, *rest] if len(rest) <= 2 and all(isinstance(x, str) for x in rest):
            cond, hit = rest + [None] * (2 - len(rest))
            return fbreak(func_name, cond, hit)

    raise model.PDVPError()


def clear(Id: int):
    match bp := SESSION.Breakpoints.get(Id):
        case model.SourceBreakpoint:
            source = bp.path
            SESSION.Breakpoints[Id] = None
            commit_source_breakpoints(source)
            pass
        case model.FunctionBreakpoint:
            SESSION.Breakpoints[Id] = None
            commit_function_breakpoints()
            pass
        case None:
            pass

    return

def _set_enable(Id: int, flag: bool):
    """Re-enable a breakpoint without forgetting its condition/etc."""
    match bp := SESSION.Breakpoints.get(Id):
        case model.SourceBreakpoint:
            source = bp.path
            SESSION.Breakpoints[Id].enabled = True
            commit_source_breakpoints(source)
            pass
        case model.FunctionBreakpoint:
            SESSION.Breakpoints[Id].enabled = True
            commit_function_breakpoints()
            pass
        case None:
            pass

    return


def enable(Id: int):
    _set_enable(Id, true)

def disable(Id: int):
    _set_enable(Id, false)


def breakpoints():
    """All breakpoints, function breakpoints, and exception filters."""
    return SESSION.Breakpoints


#def _on_stopped(reason: str | None, top: dict | None) -> str | None:
#    if reason != "breakpoint" or top is None:
#        return None
#    path = (top.get("source") or {}).get("path")
#    line = top.get("line")
#    if (path, line) in SESSION.temporary_breakpoints:
#        return str(clear(path, line))
#    return None
#
#
#_internal.post_stop_hooks.append(_on_stopped)

