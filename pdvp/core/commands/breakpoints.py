"""Breakpoint submodule. This module provides basic commands for managing breakpoints within debug session. Avaliable commands are:
    breakpoint(*args) -> model.Breakpoint | None
     - a general wrapper for various brekpoint commands type creation, a concrete command depends on provided args tuple types, see below
    sbreak(*args, condition: str | None = None, hit_condition: str | None = None, log_message: str | None = None) -> model.SourceBreakpoint
     - a command for creating "source" breakpoint - a breakpoint associated with exact path:line, the *args is either just a line number and therefore a source is assciated with current frame or combination of str, int corresponding to path, line.
    TODO: finish
    """

import pdvp.core.model as model
from pdvp.core import dap as _dap
from pdvp.core.model import Error, PydevdRefused
from pdvp.core.session import SESSION
from pathlib import Path
from pdvp.core.dap.client import Client
import pdvp.core.schema.pydevd_schema as schema

from pdvp.core.commands.location import current_file


__all__ = [
    "breakpoint", "clear", "sbreak", "fbreak",
    "enable", "disable","breakpoints"
]


def commit_all() -> Error | None:
    sources: set[model.SourcePath] = set()

    for key, breakp in SESSION.Breakpoints.items():
        if not isinstance(breakp, model.SourceBreakpoint) or not breakp.enabled:
            continue

        sources.add(breakp.path)


    for source in sources:
        err = commit_source_breakpoints(source)
        if err is not None:
            return err

    return commit_function_breakpoints()


def commit_source_breakpoints(path: model.SourcePath) -> Error | None:
    if SESSION.client is None:
        return None

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

    try:
        responce = SESSION.client.set_breakpoints(serialized_source, serialized_br)
        if not responce.success:
            return PydevdRefused(responce.message or f"failed to set breakpoints in {path}")

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
    except _dap.DAPError as e:
        return PydevdRefused(str(e), cause=e)

    return None


def sbreak(*args, condition: str | None = None, hit_condition: str | None = None, log_message: str | None = None) -> model.SourceBreakpoint | Error:
    match args:
        case [int(line)]:
            path = current_file()
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

    err = commit_source_breakpoints(path)
    if err is not None:
        return err

    return source_br

def commit_function_breakpoints() -> Error | None:
    if SESSION.client is None:
        return None

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

    try:
        responce = SESSION.client.set_function_breakpoints(serialized_br)
        if not responce.success:
            return PydevdRefused(responce.message or "failed to set function breakpoints")

        for b, destination_breakpoint in zip(responce.body.breakpoints, func_br_list):
            source_breakpoint = schema.Breakpoint(**b)
            SESSION.sourceMap.register_source(source_breakpoint.source)

            destination_breakpoint.verified = source_breakpoint.verified
    except _dap.DAPError as e:
        return PydevdRefused(str(e), cause=e)

    return None


def fbreak(func_name: str, condition: str | None = None, hitCondition: str | None = None) -> model.FunctionBreakpoint | Error:
    function_break: model.FunctionBreakpoint = model.FunctionBreakpoint(
        func_name,
        condition,
        hitCondition
    )


    SESSION.Breakpoints[function_break.ID] = function_break
    err = commit_function_breakpoints()
    if err is not None:
        return err

    return SESSION.Breakpoints[function_break.ID]


def breakpoint(*args) -> model.Breakpoint | Error:
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


def clear(Id: int) -> Error | None:
    """Forget breakpoint `Id` entirely."""
    match bp := SESSION.Breakpoints.get(Id):
        case model.SourceBreakpoint():
            source = bp.path
            del SESSION.Breakpoints[Id]
            return commit_source_breakpoints(source)
        case model.FunctionBreakpoint():
            del SESSION.Breakpoints[Id]
            return commit_function_breakpoints()
        case None:
            return None


def _set_enable(Id: int, flag: bool) -> Error | None:
    """Re-enable a breakpoint without forgetting its condition/etc."""
    match bp := SESSION.Breakpoints.get(Id):
        case model.SourceBreakpoint():
            source = bp.path
            SESSION.Breakpoints[Id].enabled = flag
            return commit_source_breakpoints(source)
        case model.FunctionBreakpoint():
            SESSION.Breakpoints[Id].enabled = flag
            return commit_function_breakpoints()
        case None:
            return None


def enable(Id: int) -> Error | None:
    return _set_enable(Id, True)

def disable(Id: int) -> Error | None:
    return _set_enable(Id, False)


def breakpoints() -> model.Breakpoints:
    """All breakpoints and function breakpoints, grouped by file for display."""
    return model.Breakpoints(SESSION.Breakpoints)

