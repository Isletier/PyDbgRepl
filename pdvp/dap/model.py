from enum import Enum

class ExceptionBreakMode(Enum):
    NEVER = 1
    ALWAYS = 2
    UNHANDLED = 3
    USERUNHANDLED = 4

class ExceptionPathSegment:
    path: str
    negate: bool

    def __init__(self, path: str, negate: bool):
        self.path = path
        self.negate = negate


class ExceptionOptions:
    paths: list[ExceptionPathSegment]
    break_mode: ExceptionBreakMode

    def __init__(self, paths: list[ExceptionPathSegment], mode: ExceptionBreakMode):
        self.paths = paths
        self.break_mode = mode

class ExceptionBreakpointFilters:
    RAISED = 1
    UNCAUGHT = 2
    USER_UNHANDLED = 3

class ExceptionFilterOptions:
    filter_id: ExceptionBreakpointFilters
    condition: str
    # mode: str -- unsopported by pydevd

class ExceptionBreakpointArguments:
    filters: set[ExceptionBreakpointFilters]
    filter_options: list[ExceptionFilterOptions]
    exception_options: list[ExceptionOptions]

class FunctionBreakpoint:
    name: str
    condition: str | None
    hitCondition?: str | None

type FunctionBreakpointsArguments = list[FunctionBreakpoint]

class SourceBreakpoint:
    line: int
#   column - unsupported by intentional design
    condition: str | None
    hitCondition: str | None
    logMessage: str | None
#   mode - unsoppoted by pydevd

class Source:
    path: str

class SourceBreakpointArguments:
    source: Source
    br:     SourceBreakpoint







