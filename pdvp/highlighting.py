"""ptpython syntax highlighting: give our debugger commands their own color.

Pygments' PythonLexer would otherwise render `run`, `cont`, `clear`, etc. as
plain `Name` (and `breakpoint` -- a real Python builtin -- as `Name.Builtin`,
the same color as `print`/`len`/etc.). We tag all of `commands.__all__` with a
dedicated token so they're visually distinct from both keywords and ordinary
builtins.
"""
from __future__ import annotations

from prompt_toolkit.lexers import Lexer, PygmentsLexer
from prompt_toolkit.styles import Style
from pygments.lexer import words
from pygments.lexers.python import PythonLexer
from pygments.token import Name

from . import commands as _commands

# Subtoken of Name.Builtin so it inherits sane fallback styling from any
# pygments theme, but gets its own color via STYLE_OVERRIDES below.
COMMAND_TOKEN = Name.Builtin.Debugger

STYLE_OVERRIDES = Style.from_dict({
    "pygments.name.builtin.debugger": "bold fg:ansibrightcyan",
})


def make_lexer() -> Lexer:
    tokens = dict(PythonLexer.tokens)
    tokens["builtins"] = [
        (words(tuple(_commands.__all__), prefix=r"(?<!\.)", suffix=r"\b"), COMMAND_TOKEN),
        *PythonLexer.tokens["builtins"],
    ]
    debugger_lexer = type("DebuggerCommandLexer", (PythonLexer,), {"tokens": tokens})
    return PygmentsLexer(debugger_lexer)
