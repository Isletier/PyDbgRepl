"""Minimal DAP client for talking to pydevd's --json-dap-http server."""
from .client import (
    Client,
    ConnectionClosed,
    ConnectionLost,
    DAPError,
    Pending,
    RequestFailed,
)
from .transport import LISTEN_HOST, ProtocolError, Transport, listen

from pdvp.schema.pydevd_schema import (
    Breakpoint,
    BreakpointLocation,
#   BreakpointMode
#   BreakpointModeApplicability
    Capabilities,
    Checksum,
    ChecksumAlgorithm,
    ColumnDescriptor,
    CompletionItem,
#   DataBreakpoint
#   DataBrekpointAccessType
#   DisassembledInstruction,
    ExceptionBreakMode,
    ExceptionBreakpointsFilter,
    ExceptionDetails,
    ExceptionFilterOptions,
    ExceptionOptions,
    ExceptionPathSegment,
    FunctionBreakpoint,
    GotoTarget,
#   InstructionBreakpoint,
#   InvalidatedAreas
    Message,
    Module,
    Scope,
    Source,
    SourceBreakpoint,
    StackFrame,
    StackFrameFormat,
    StepInTarget,
    SteppingGranularity,
    Thread,
    ValueFormat,
    Variable,
    VariablePresentationHint,
)

# pydevd's `exceptionBreakpointFilters`, as reported in its initialize
# response. We target one debugger core, so this is a constant here rather
# than per-session state; connect() warns if the pydevd we reach disagrees.
EXCEPTION_BREAKPOINT_FILTERS = ["raised", "uncaught", "userUnhandled"]

__all__ = [
    "Client",
    "Pending",
    "Transport",
    "listen",
    "LISTEN_HOST",
    "ConnectionClosed",
    "ConnectionLost",
    "DAPError",
    "ProtocolError",
    "RequestFailed",
    "EXCEPTION_BREAKPOINT_FILTERS",
    "Breakpoint",
    "BreakpointLocation",
#   "BreakpointMode"
#   "BreakpointModeApplicability"
    "Capabilities",
    "Checksum",
    "ChecksumAlgorithm",
    "ColumnDescriptor",
    "CompletionItem",
#   "DataBreakpoint"
#   "DataBrekpointAccessType"
#   "DisassembledInstruction",
    "ExceptionBreakMode",
    "ExceptionBreakpointsFilter",
    "ExceptionDetails",
    "ExceptionFilterOptions",
    "ExceptionOptions",
    "ExceptionPathSegment",
    "FunctionBreakpoint",
    "GotoTarget",
#   "InstructionBreakpoint",
#   "InvalidatedAreas"
    "Message",
    "Module",
    "Scope",
    "Source",
    "SourceBreakpoint",
    "StackFrame",
    "StackFrameFormat",
    "StepInTarget",
    "SteppingGranularity",
    "Thread",
    "ValueFormat",
    "Variable",
    "VariablePresentationHint",
]

