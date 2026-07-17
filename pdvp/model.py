"""Pdvp external types model."""

from pdvp.source import Source
from pdvp.source import SourceMap

class Breakpoint:
    def __init__(self, enabled: bool = True, verified: bool = False):
        _id_gen += 1
        self.ID = _id_gen
        self.enabled = enabled
        self.verified = verified

    _id_gen = 0

    ID:         int
    enabled:    bool
    verified:   bool        #consider this force disabled breakpoint

class SourceBreakpoint(Breakpoint):
    def __init__(self, path: Source, line: int, condition: str | None, hitCondition: str | None, logMessage: str | None, enabled: bool = True, verified: bool = False):
        super().__init__(self, enabled, verified)
        self.path = path
        self.line = line
        self.condition = condition
        self.hitCondition = hitCondition
        self.logMessage = logMessage

    path:           Source
    line:           int
    condition:      str | None
    hitCondition:   str | None
    logMessage:     str | None

class FunctionBreakpoint(Breakpoint):
    name:           str
    condition:      str | None
    hitCondition:   str | None

class PDVPError(Exception):
    """Generic type for pdvp exceptions"""
    pass
