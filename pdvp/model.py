#TODO: change this
"""Result types for "info" commands: real data structures with a human-
readable __repr__, so e.g. `locals()` both prints nicely at the prompt (via
the REPL's normal repr-echo of expression statements) and returns something
usable from scripts (iterate, index, pass to other code).
"""
from __future__ import annotations
from pdvp.source import SourcePath

class Breakpoint:
    _id_gen = 0

    def __init__(self, enabled: bool = True, verified: bool = False):
        Breakpoint._id_gen += 1
        self.ID = Breakpoint._id_gen 
        self.enabled = enabled
        self.verified = verified

    ID:         int
    enabled:    bool
    verified:   bool        #consider this force disabled breakpoint

class SourceBreakpoint(Breakpoint):

    def __init__(self, path: SourcePath, line: int, condition: str | None, hitCondition: str | None, logMessage: str | None, enabled: bool = True, verified: bool = False):
        super().__init__(enabled, verified)
        self.path = path
        self.line = line
        self.condition = condition
        self.hitCondition = hitCondition
        self.logMessage = logMessage

    path:           SourcePath
    line:           int
    condition:      str | None
    hitCondition:   str | None
    logMessage:     str | None

class FunctionBreakpoint(Breakpoint):

    def __init__(self, name: str, condition: str | None, hitCondition: str | None, enabled: bool = True, verified: bool = False):
        super().__init__(enabled, verified)
        self.name = name
        self.condition = condition
        self.hitCondition = hitCondition

    name:           str
    condition:      str | None
    hitCondition:   str | None

class PDVPError(Exception):
    """Generic type for pdvp exceptions"""
    pass



class Scope(list):
    """Variables from one DAP scope (locals()/globals_()): a list of the raw
    `variables` response dicts (name/value/type/...), reprs as "name = value"."""

    def __repr__(self) -> str:
        if not self:
            return "(empty)"
        return "\n".join(f"{v['name']} = {v['value']}" for v in self)


class ThreadList(list):
    """Threads from threads(): a list of the raw `threads` response dicts,
    reprs with a "*" marker on the current thread."""

    def __init__(self, items, current_id: int | None = None):
        super().__init__(items)
        self.current_id = current_id

    def __repr__(self) -> str:
        if not self:
            return "(no threads)"
        lines = []
        for t in self:
            marker = "*" if t["id"] == self.current_id else " "
            lines.append(f"{marker} {t['id']}: {t['name']}")
        return "\n".join(lines)


class FrameList(list):
    """Stack frames from bt(): a list of the raw `stackTrace` frame dicts,
    reprs with a "*" marker on the current frame."""

    def __init__(self, items, current_id: int | None = None):
        super().__init__(items)
        self.current_id = current_id

    def __repr__(self) -> str:
        if not self:
            return "(no frames)"
        lines = []
        for i, f in enumerate(self):
            marker = "*" if f["id"] == self.current_id else " "
            path = (f.get("source") or {}).get("path", "?")
            lines.append(f"{marker} #{i} {f['name']} at {path}:{f['line']}")
        return "\n".join(lines)


class ModuleList(list):
    """Modules from modules(): a list of the raw `modules` response dicts."""

    def __repr__(self) -> str:
        if not self:
            return "(no modules)"
        lines = []
        for m in self:
            path = m.get("path", "")
            lines.append(f"{m.get('id')}: {m.get('name')}{' (' + path + ')' if path else ''}")
        return "\n".join(lines)


class InfoSections(dict):
    """Nested section dict from pydevd_info() (the raw `pydevdSystemInfo`
    response), reprs as "section:" headers with indented key = value lines."""

    def __repr__(self) -> str:
        lines = []
        for section, values in self.items():
            lines.append(f"{section}:")
            if isinstance(values, dict):
                for k, v in values.items():
                    lines.append(f"  {k} = {v}")
            else:
                lines.append(f"  {values}")
        return "\n".join(lines)


class ExceptionInfo(dict):
    """The raw `exceptionInfo` response from exception_info(), reprs as the
    exception id/description plus stack trace (if any)."""

    def __repr__(self) -> str:
        lines = [f"{self.get('exceptionId', '?')}: {self.get('description', '')}"]
        details = self.get("details") or {}
        if details.get("stackTrace"):
            lines.append(details["stackTrace"])
        return "\n".join(lines)


class CompletionList(list):
    """Completion targets from completions(): a list of the raw `completions`
    response items, reprs one label per line."""

    def __repr__(self) -> str:
        if not self:
            return "(no completions)"
        return "\n".join(item.get("label", str(item)) for item in self)


class SourceLines(list):
    """Lines from list()/l(): a list of (lineno, text) pairs, reprs with
    line numbers and a "->" marker on the current line."""

    def __init__(self, items, current_line: int | None = None):
        super().__init__(items)
        self.current_line = current_line

    def __repr__(self) -> str:
        if not self:
            return "(no lines)"
        lines = []
        for lineno, text in self:
            marker = "->" if lineno == self.current_line else "  "
            lines.append(f"{marker}{lineno:5d}\t{text}")
        return "\n".join(lines)


class Status(str):
    """A short status/result message returned by action commands (set(),
    breakpoint(), stop(), p(), ...).

    Behaves like the message itself for scripting (it *is* a `str`), but
    reprs without the surrounding quotes a plain `str` would get -- so at
    the prompt it looks exactly like the old `print()` output.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return str(self)


class Error(Status):
    """An error result returned by a failed command, e.g. `Error("not connected")`.

    A `Status` (so it still reprs without quotes, as `"error: <message>"`,
    matching the old `print(f"error: ...")` output) that is additionally
    falsy: `bool(Error(...))` is `False`, so `if not result:` is the one
    idiom for "did this command fail" across the whole command surface --
    while empty-but-successful results (`Scope([])`, `ThreadList([])`, ...)
    stay truthy.
    """

    __slots__ = ()

    def __new__(cls, message: str) -> "Error":
        return super().__new__(cls, f"error: {message}")

    def __bool__(self) -> bool:
        return False


class StopResult:
    """Result of a blocking resume (cont()/step()/next()/finish()/until()/
    jump()/connect()/run()/restart()): what happened, and -- if stopped --
    where. `event` is "stopped"/"exited"/"terminated"/"_disconnected"; for
    "stopped", `reason`/`top_frame` describe the new location (`top_frame`
    is the raw top `stackTrace` frame dict, or None if it couldn't be
    fetched); for "exited", `exit_code` is the process's exit code.

    `prefix`, if set, is one or more status lines (e.g. "continuing",
    "launched pid=...") shown before the outcome. `suffix`, if set, is one or
    more lines shown after it (e.g. re-evaluated display() expressions, or a
    tbreak's auto-clear confirmation) -- gathered from `post_stop_hooks`.
    """

    def __init__(
        self,
        event: str,
        body: dict,
        top_frame: dict | None = None,
        prefix: str = "",
        suffix: str = "",
    ):
        self.event = event
        self.body = body
        self.top_frame = top_frame
        self.prefix = prefix
        self.suffix = suffix

    @property
    def reason(self) -> str | None:
        return self.body.get("reason") if self.event == "stopped" else None

    @property
    def exit_code(self) -> int | None:
        return self.body.get("exitCode") if self.event == "exited" else None

    def __repr__(self) -> str:
        if self.event == "stopped":
            if self.top_frame is None:
                line = f"*** stopped ({self.reason})"
            else:
                path = (self.top_frame.get("source") or {}).get("path", "?")
                line = f"*** stopped ({self.reason}) at {path}:{self.top_frame['line']}, in {self.top_frame['name']}"
        elif self.event == "exited":
            line = f"*** program exited with code {self.exit_code}"
        elif self.event == "terminated":
            line = "*** program terminated"
        else:  # "_disconnected"
            line = "*** connection to pydevd lost"
        if self.prefix:
            line = f"{self.prefix}\n{line}"
        if self.suffix:
            line = f"{line}\n{self.suffix}"
        return line


class FrameRef(dict):
    """A single stack frame selected by frame()/up()/down() (the raw
    `stackTrace` frame dict), reprs as "#index name at path:line".

    `prefix`, if set, is a status line (e.g. "*** Oldest frame") shown before
    it -- used by up()/down() when a move is clamped to the stack's edge.
    """

    def __init__(self, frame: dict, index: int, prefix: str = ""):
        super().__init__(frame)
        self.index = index
        self.prefix = prefix

    def __repr__(self) -> str:
        path = (self.get("source") or {}).get("path", "?")
        line = f"#{self.index} {self['name']} at {path}:{self['line']}"
        return f"{self.prefix}\n{line}" if self.prefix else line


class Breakpoints:
    """All breakpoints from breakpoints(): line breakpoints by file
    (`by_file`, same shape as `SESSION.breakpoints`), function breakpoints,
    and exception filters."""

    def __init__(self, by_file: dict[str, list[dict]], function_breakpoints: list[dict], exception_filters: list[str]):
        self.by_file = by_file
        self.function_breakpoints = function_breakpoints
        self.exception_filters = exception_filters

    def __repr__(self) -> str:
        lines = []
        for path, bps in self.by_file.items():
            for b in sorted(bps, key=lambda b: b["line"]):
                status = "enabled" if b.get("enabled", True) else "disabled"
                extra = ", ".join(f"{k}={v!r}" for k, v in b.items() if k not in ("line", "enabled"))
                suffix = f" ({extra})" if extra else ""
                lines.append(f"{path}:{b['line']} [{status}]{suffix}")

        for fb in self.function_breakpoints:
            extra = ", ".join(f"{k}={v!r}" for k, v in fb.items() if k != "name")
            suffix = f" ({extra})" if extra else ""
            lines.append(f"function {fb['name']}{suffix}")

        if self.exception_filters:
            lines.append(f"exception filters: {self.exception_filters}")

        return "\n".join(lines) if lines else "no breakpoints set"
