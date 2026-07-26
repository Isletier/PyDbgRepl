"""Pdvp external types model."""

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
