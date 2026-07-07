"""Minimal DAP client for talking to pydevd's --json-dap-http server."""
from .client import Client, DAPError, dap_event_name

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

__all__ = ["DAPClient", "DAPError", "dap_event_name"]

