#TODO: change this
"""Result types for "info" commands: real data structures with a human-
readable __repr__, so e.g. `locals()` both prints nicely at the prompt (via
the REPL's normal repr-echo of expression statements) and returns something
usable from scripts (iterate, index, pass to other code).
"""
from __future__ import annotations
import enum
from pdvp.core import events
from pdvp.core.source import SourcePath

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


class CursorList(list):
    """Rows from cursors(): one dict per caller that has selected something,
    reprs with a "*" on the calling caller's own row and a final line for what
    everyone who has not selected reads."""

    def __init__(self, items, default_thread: int | None = None):
        super().__init__(items)
        self.default_thread = default_thread

    def __repr__(self) -> str:
        lines = []
        for row in self:
            marker = "*" if row["current"] else " "
            frame = f", frame {row['frame']}" if row["frame"] is not None else ""
            note = "  (from a previous session, ignored)" if row["stale"] else ""
            lines.append(f"{marker} {row['owner']}: thread {row['thread']}{frame}{note}")

        if self.default_thread is None:
            lines.append("  (unselected): no thread has stopped yet")
        else:
            lines.append(f"  (unselected): thread {self.default_thread}")
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


class ErrorKind(enum.Enum):
    """The uniform tag every `Error` carries, so a caller can branch on
    *why* a command failed without parsing the message -- an exhaustive
    `match self.kind:` works the same whether or not anything is ever
    raised (nothing here is tied to the raise-vs-return choice, see
    doc/architecture.md's output/return-type redesign section).

    Most kinds need nothing beyond the tag. The two that carry genuine
    extra structured data get their own `Error` subclass instead of a
    generic payload field here: `STALE_FRAME` (`StaleFrameError`, below) and
    `PYDEVD_REFUSED` (`PydevdRefused`, below).
    """

    NOT_CONNECTED = enum.auto()
    ALREADY_CONNECTED = enum.auto()
    NO_CURRENT_THREAD = enum.auto()
    NO_SUCH_THREAD = enum.auto()
    THREAD_RUNNING = enum.auto()
    NO_CURRENT_FRAME = enum.auto()
    STALE_FRAME = enum.auto()
    NO_CURRENT_FILE = enum.auto()
    LINE_NUMBER_REQUIRED = enum.auto()
    NO_SUCH_FRAME = enum.auto()
    NO_FRAMES = enum.auto()
    NO_JUMP_TARGET = enum.auto()
    PROGRAM_NOT_RUNNING = enum.auto()
    RESUME_IN_FLIGHT = enum.auto()
    NO_SCRIPT = enum.auto()
    NO_ACTIVE_SESSION = enum.auto()
    LAUNCH_FAILED = enum.auto()
    HANDSHAKE_FAILED = enum.auto()
    SOURCE_UNAVAILABLE = enum.auto()
    PYDEVD_REFUSED = enum.auto()


class Error(Status):
    """An error result returned by a failed command, e.g.
    `Error("not connected", kind=ErrorKind.NOT_CONNECTED)`.

    A `Status` (so it still reprs without quotes, as `"error: <message>"`,
    matching the old `print(f"error: ...")` output) that is additionally
    falsy: `bool(Error(...))` is `False`, so `if not result:` is the one
    idiom for "did this command fail" across the whole command surface --
    while empty-but-successful results (`Scope([])`, `ThreadList([])`, ...)
    stay truthy.

    `kind` is always set (never `None`) -- every call site in this codebase
    names one explicitly; there is no silent "uncategorized" default to
    encourage skipping it.
    """

    __slots__ = ("kind",)

    def __new__(cls, message: str, *, kind: ErrorKind) -> "Error":
        self = super().__new__(cls, f"error: {message}")
        self.kind = kind
        return self

    def __bool__(self) -> bool:
        return False


class StaleFrameError(Error):
    """`require_frame()` refused a read because the epoch moved since the
    handle was minted (session.py): the thread resumed and re-stopped, or
    resumed and is still running, between the caller minting the handle and
    using it. `thread_id`/`stale_epoch`/`current_epoch` let a caller decide
    programmatically (e.g. "was this *my* resume or someone else's") instead
    of parsing the message.
    """

    __slots__ = ("thread_id", "stale_epoch", "current_epoch")

    def __new__(cls, thread_id: int, stale_epoch: int, current_epoch: int) -> "StaleFrameError":
        self = super().__new__(
            cls,
            "frame is stale, the program has resumed since (use bt())",
            kind=ErrorKind.STALE_FRAME,
        )
        self.thread_id = thread_id
        self.stale_epoch = stale_epoch
        self.current_epoch = current_epoch
        return self


class PydevdRefused(Error):
    """pydevd refused or failed a request: a `dap.DAPError` propagated from
    the wire, or a DAP response that came back `success=False`. `cause` is
    the underlying `DAPError` when there was one (`None` for a failed
    response with no exception), for a caller that wants more than the
    message string -- its own `.message`/args, not just this wrapper's text.
    """

    __slots__ = ("cause",)

    def __new__(cls, message: str, cause: Exception | None = None) -> "PydevdRefused":
        self = super().__new__(cls, message, kind=ErrorKind.PYDEVD_REFUSED)
        self.cause = cause
        return self


class StopResult:
    """Result of a blocking resume (cont()/step()/next()/finish()/jump()):
    what happened, and -- if stopped -- where.

    connect()/run()/restart() return the richer `ConnectResult`/`RunResult`
    below, which extend this with what the connection/launch itself involved
    -- they are not this same flat type, since cont()/step()/etc. would then
    carry three fields that are always empty.

    `event` is the pdvp event that ended the wait: an `events.Stopped`, whose
    `reason`/`top_frame` describe the new location (`top_frame` is the raw top
    `stackTrace` frame dict, or None if it couldn't be fetched), or an
    `events.SessionEnded`, whose `reason` says which of exit, termination and
    disconnection got there first.

    `prefix`, if set, is one or more status lines (e.g. "continuing",
    "launched pid=...", "[Switching to thread 3]") shown before the outcome.

    `source`, if set, is the single line stopped at (a one-entry
    `SourceLines`, gdb's own convention -- use `ls()` for a window of
    context on demand) -- `None` whenever it couldn't be fetched (no frame,
    no source path, or the file isn't reachable), which is never treated as
    a failure of the stop itself.
    """

    def __init__(
        self,
        event: events.Event,
        top_frame: dict | None = None,
        prefix: str = "",
        source: SourceLines | None = None,
    ):
        self.event = event
        self.top_frame = top_frame
        self.prefix = prefix
        self.source = source

    @property
    def stopped(self) -> bool:
        return isinstance(self.event, events.Stopped)

    @property
    def reason(self) -> str | None:
        return getattr(self.event, "reason", None)

    @property
    def exit_code(self) -> int | None:
        return getattr(self.event, "exit_code", None)

    def __repr__(self) -> str:
        if self.stopped:
            if self.top_frame is None:
                line = f"*** stopped ({self.reason})"
            else:
                path = (self.top_frame.get("source") or {}).get("path", "?")
                line = f"*** stopped ({self.reason}) at {path}:{self.top_frame['line']}, in {self.top_frame['name']}"
            if self.source:
                line = f"{line}\n{self.source!r}"
        elif self.exit_code is not None:
            line = f"*** program exited with code {self.exit_code}"
        elif self.reason is events.EndReason.DISCONNECTED:
            line = "*** connection to pydevd lost"
        elif self.reason is events.EndReason.CLOSED:
            line = "*** session closed"
        else:
            line = "*** program terminated"
        if self.prefix:
            line = f"{self.prefix}\n{line}"
        return line


class ConnectResult(StopResult):
    """Result of connect(): a `StopResult` plus the address dialled.

    `connected_to` is `(host, port)` -- structured, unlike the prefix text
    every `StopResult` already shows ("connected to pydevd on host:port");
    a caller that wants the address programmatically no longer has to parse
    the repr for it.
    """

    def __init__(
        self,
        event: events.Event,
        top_frame: dict | None = None,
        connected_to: tuple[str, int] | None = None,
        source: SourceLines | None = None,
    ):
        prefix = f"connected to pydevd on {connected_to[0]}:{connected_to[1]}" if connected_to else ""
        super().__init__(event, top_frame=top_frame, prefix=prefix, source=source)
        self.connected_to = connected_to


class RunResult(ConnectResult):
    """Result of run()/restart(): a `ConnectResult` plus what launching involved.

    `killed_previous` is whether a prior session was torn down first (gdb-style
    restart); `spawned_pid` is the pid of the process run() started -- `None`
    for `connect()`, which never spawns anything.
    """

    def __init__(
        self,
        event: events.Event,
        top_frame: dict | None = None,
        connected_to: tuple[str, int] | None = None,
        killed_previous: bool = False,
        spawned_pid: int | None = None,
        source: SourceLines | None = None,
    ):
        super().__init__(event, top_frame=top_frame, connected_to=connected_to, source=source)
        self.killed_previous = killed_previous
        self.spawned_pid = spawned_pid

        prefix_lines = []
        if killed_previous:
            prefix_lines.append("killing previous instance")
        if spawned_pid is not None:
            prefix_lines.append(f"launched pid={spawned_pid}")
        if self.prefix:
            prefix_lines.append(self.prefix)
        self.prefix = "\n".join(prefix_lines)


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


class Breakpoints(dict):
    """All breakpoints from breakpoints(): `SESSION.Breakpoints`'s own
    `{id: Breakpoint}` mapping (still a dict -- iterate, index, len() it like
    one), reprs grouped by file (source breakpoints, files sorted by path,
    each file's breakpoints sorted by line) then function breakpoints,
    computed on demand from each `Breakpoint`'s own type/fields rather than
    tracked separately -- one source of truth, nothing to fall out of sync
    with it.
    """

    def __repr__(self) -> str:
        by_file: dict[SourcePath, list[SourceBreakpoint]] = {}
        function_breakpoints: list[FunctionBreakpoint] = []
        for bp in self.values():
            if isinstance(bp, SourceBreakpoint):
                by_file.setdefault(bp.path, []).append(bp)
            elif isinstance(bp, FunctionBreakpoint):
                function_breakpoints.append(bp)

        lines = []
        for path, bps in sorted(by_file.items()):
            for b in sorted(bps, key=lambda b: b.line):
                status = "enabled" if b.enabled else "disabled"
                extra = ", ".join(
                    f"{name}={value!r}"
                    for name, value in (
                        ("condition", b.condition),
                        ("hitCondition", b.hitCondition),
                        ("logMessage", b.logMessage),
                    )
                    if value is not None
                )
                suffix = f" ({extra})" if extra else ""
                lines.append(f"{path}:{b.line} [{status}]{suffix}")

        for fb in function_breakpoints:
            status = "enabled" if fb.enabled else "disabled"
            extra = ", ".join(
                f"{name}={value!r}"
                for name, value in (("condition", fb.condition), ("hitCondition", fb.hitCondition))
                if value is not None
            )
            suffix = f" ({extra})" if extra else ""
            lines.append(f"function {fb.name} [{status}]{suffix}")

        return "\n".join(lines) if lines else "no breakpoints set"
