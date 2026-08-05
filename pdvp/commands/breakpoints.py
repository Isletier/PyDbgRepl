"""Breakpoint submodule. This module provides basic commands for managing breakpoints within debug session. Avaliable commands are:
    breakpoint(*args) -> model.Breakpoint | None
     - a general wrapper for various brekpoint commands type creation, a concrete command depends on provided args tuple types, see below
    sbreak(*args, condition: str | None = None, hit_condition: str | None = None, log_message: str | None = None) -> model.SourceBreakpoint
     - a command for creating "source" breakpoint - a breakpoint associated with exact path:line, the *args is either just a line number and therefore a source is assciated with current frame or combination of str, int corresponding to path, line.
    TODO: finish
    """

import pdvp.model as model
from pdvp.session import SESSION
from pathlib import Path
from pdvp.dap.client import Client
import pdvp.schema.pydevd_schema as schema

from ._internal import _current_file


__all__ = [
    "breakpoint", "clear", "sbreak", "fbreak",
    "enable", "disable","breakpoints"
]


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


def invalidate_all() -> None:
    """Forget what the (now dead) pydevd session told us about our breakpoints.

    The breakpoints themselves survive teardown, gdb-style, but `verified` is
    a fact about one debuggee process rather than about the breakpoint itself,
    so it goes back to False. Called from _clear_dap_state().
    """
    for breakp in SESSION.Breakpoints.values():
        breakp.verified = False

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

    for b, destination_breakpoint in zip(responce.body.breakpoints, source_br_list):
        source_breakpoint = schema.Breakpoint(**b)
        SESSION.sourceMap.register_source(source_breakpoint.source)

        destination_breakpoint.verified = source_breakpoint.verified
        # pydevd may slide a breakpoint onto the next executable line, and the
        # breakpoint's location follows that resolution. A breakpoint that
        # failed to verify comes back with no line at all -- keep ours then.
        if source_breakpoint.line is not None:
            destination_breakpoint.line = source_breakpoint.line
        destination_breakpoint.path = path

    return


def sbreak(*args, condition: str | None = None, hit_condition: str | None = None, log_message: str | None = None) -> model.SourceBreakpoint:
    match args:
        case [int(line)]:
            path = _current_file()
            if path is None:
                raise model.PDVPError("no current file (pass an explicit path)")
        case [str(path), int(line)]:
            pass
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
        raise model.PDVPError()

    for b, destination_breakpoint in zip(responce.body.breakpoints, func_br_list):
        source_breakpoint = schema.Breakpoint(**b)
        SESSION.sourceMap.register_source(source_breakpoint.source)

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
            return sbreak(path, line, condition=cond, hit_condition=hit, log_message=log)
        case [str() as func_name, *rest] if len(rest) <= 2 and all(isinstance(x, str) for x in rest):
            cond, hit = rest + [None] * (2 - len(rest))
            return fbreak(func_name, cond, hit)

    raise model.PDVPError("invalid arguments for breakpoint()")


def clear(Id: int):
    """Forget breakpoint `Id` entirely."""
    match bp := SESSION.Breakpoints.get(Id):
        case model.SourceBreakpoint():
            source = bp.path
            del SESSION.Breakpoints[Id]
            commit_source_breakpoints(source)
            pass
        case model.FunctionBreakpoint():
            del SESSION.Breakpoints[Id]
            commit_function_breakpoints()
            pass
        case None:
            pass

    return

def _set_enable(Id: int, flag: bool):
    """Re-enable a breakpoint without forgetting its condition/etc."""
    match bp := SESSION.Breakpoints.get(Id):
        case model.SourceBreakpoint():
            source = bp.path
            SESSION.Breakpoints[Id].enabled = flag
            commit_source_breakpoints(source)
            pass
        case model.FunctionBreakpoint():
            SESSION.Breakpoints[Id].enabled = flag
            commit_function_breakpoints()
            pass
        case None:
            pass

    return


def enable(Id: int):
    _set_enable(Id, True)

def disable(Id: int):
    _set_enable(Id, False)


def breakpoints():
    """All breakpoints, function breakpoints, and exception filters."""
    return SESSION.Breakpoints

