"""Pdvp external types model."""

type Source = str
type SourceMap = dict[str, number]

class Breakpoint:
    ID:         number
    enabled:    bool
    verified:   bool        #consider this force disabled breakpoint

class SourceBreakpoint(Breakpoint):
    path:           Source,
    line:           number,
    condition:      str | None
    hitCondition:   str | None
    logMessage:     str | None

class FunctionBreakpoint(Breakpoint):
    name:           str,
    condition:      str | None
    hitCondition:   str | None


