"""pdvp.extra: interactive-session convenience built on top of pdvp.core.

ptpython/prompt_toolkit integration, keybindings, and the gdb-style Ctrl+C
policy -- everything that only matters to a human at a prompt (see
doc/architecture.md's core/extra split, and P0: no caller gets privileged
automatic setup). Nothing here runs on import; every entry point below is an
explicit, opt-in call.

Two separately-named ways to get ptpython:

    pdvp.extra.embed()          # block right here, regardless of how the
                                 # process was launched
    pdvp.extra.install_hook()   # make ptpython the interpreter that `-i` /
                                 # bare `python3` drops into, e.g. from a
                                 # PYTHONSTARTUP file
"""
import os as _os
import signal as _signal
import sys as _sys

from pdvp.core import commands as _commands
from pdvp.core import dap as _dap
from pdvp.core.session import SESSION

__all__ = ["embed", "install_hook"]


def _sigint_handler(signum, frame) -> None:
    """gdb-style Ctrl+C: pause a running debuggee, otherwise cancel the current input.

    The condition is "anything is running", not "the thread I am sitting on is
    running": in non-stop a thread this context never selected can be the only
    one moving, and Ctrl+C is stop-the-world in both modes. At an idle prompt
    this stays the REPL's line-clear.
    """
    if SESSION.client is not None and SESSION.any_running:
        try:
            _commands.interrupt()
        except _dap.DAPError:
            pass
        return
    _signal.default_int_handler(signum, frame)


def _install_sigint_policy() -> None:
    _signal.signal(_signal.SIGINT, _sigint_handler)


class _PydevPromptStyle:
    """ptpython PromptStyle showing the session state, e.g. "(paused) >>> "."""

    def in_prompt(self):
        if SESSION.client is None:
            status = "disconnected"
        elif SESSION.any_running:
            status = "running"
        else:
            status = "paused"
        return [("class:pygments.comment", f"({status}) "), ("class:prompt", ">>> ")]

    def in2_prompt(self, width: int):
        return [("class:prompt.dots", "...".rjust(width))]

    def out_prompt(self):
        return []


def _configure_ptpython(repl) -> None:
    from prompt_toolkit.styles import merge_styles

    from pdvp.extra.highlighting import STYLE_OVERRIDES

    repl.all_prompt_styles["pydev"] = _PydevPromptStyle()
    repl.prompt_style = "pydev"
    repl._current_style = merge_styles([repl._current_style, STYLE_OVERRIDES])


def _run_ptpython() -> None:
    from prompt_toolkit.patch_stdout import patch_stdout as _patch_stdout
    from ptpython.repl import PythonRepl

    from pdvp.extra import keybindings
    from pdvp.extra.completion import DebuggerCompleter
    from pdvp.extra.highlighting import make_lexer

    import __main__
    SESSION.ptpython_active = True

    def get_globals():
        return vars(__main__)

    repl = PythonRepl(get_globals=get_globals, get_locals=get_globals, _lexer=make_lexer())
    repl.completer = DebuggerCompleter(repl.completer)
    _configure_ptpython(repl)
    keybindings.install(repl)

    with _patch_stdout():
        repl.run()


def embed() -> None:
    """Block and drop the caller into a ptpython REPL, in __main__'s namespace.

    Works regardless of how the process was launched -- call it directly from
    any script that wants an interactive prompt right there. Installs the
    gdb-style Ctrl+C policy for the duration. Returns once the user exits the
    REPL (e.g. Ctrl+D): the caller's own code after this call still runs --
    unlike install_hook(), nothing here is "instead of" anything else.
    """
    _install_sigint_policy()
    _run_ptpython()


def install_hook() -> None:
    """Make ptpython the interpreter that `-i` / bare `python3` drops into.

    Sets sys.__interactivehook__ -- the hook CPython calls right before its
    own interactive loop -- to embed ptpython instead and hard-exit rather
    than fall through to the stock loop once it returns. The mechanism for
    "make ptpython the default interpreter" for a whole session, e.g. from a
    PYTHONSTARTUP file: no pdvp-aware wrapper script required.
    """
    def _hook() -> None:
        _install_sigint_policy()
        try:
            _run_ptpython()
        finally:
            _os._exit(0)

    _sys.__interactivehook__ = _hook
