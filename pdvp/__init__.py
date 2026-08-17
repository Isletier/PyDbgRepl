"""pydev-repl: a Python debugger REPL built on pydevd.

Typical wrapper script:

    import pdvp as debug

    debug.process_args_envs(sys.argv[1:])

    # optional: debug.CONFIG.log_level = "debug"

    debug.start_eval()

    # optional: plain Python "scenario" lines go here, e.g. cont(), bt(5), ...

start_eval() injects the REPL commands (and CONFIG, under the name `config`)
into __main__ and returns. Any
scenario lines after it run as a normal script body. Once the script body
finishes, an interactive prompt (ptpython, or readline via
code.InteractiveConsole) takes over with __main__'s namespace -- unless
`interactive` is False (--batch), in which case the process just exits. See
doc/scenario_mode.md.
"""

import atexit
import code
import os
import signal
import sys

from . import commands as _commands
from . import dap as _dap
from pdvp import launch
from .commands import *  # noqa: F401,F403
from .commands import __all__ as _commands_all
from .session import SESSION  # noqa: F401
#: The live configuration. Assign to it directly: `pdvp.CONFIG.port = 5678`.
#: It lives in the `pdvp.config` module, which is why it is not itself named
#: `config` -- `pdvp.config` is that module. At the prompt it *is* called
#: `config`, because start_eval() injects it into __main__ under that name.
from .config import CONFIG

__all__ = [*_commands_all, "process_args_envs", "start_eval", "CONFIG"]


def process_args_envs(argv: list[str] | None = None) -> None:
    """Populate CONFIG from the launch command line, and tidy the environment.

    Does not start anything, even if --file was given (it is just saved to
    CONFIG for start_eval()/run() to pick up later).

    Environment handling is deliberately near-zero: pydevd is configured
    through os.environ like any other program, so the only thing we do is drop
    inherited debug settings that would otherwise make us adopt another
    debugger's configuration (launch.ENV_SANITIZE). Assign to os.environ
    yourself, before or after this call -- later assignments win, and the
    inferior inherits our environment as-is.
    """
    argv = sys.argv[1:] if argv is None else argv

    launch.scrub_env()

    try:
        launch.parse_argv(CONFIG, argv)
    except launch.LaunchError as e:
        print(f"error: {e}")
        raise SystemExit(1)


def _sigint_handler(signum, frame) -> None:
    """gdb-style Ctrl+C: pause a running debuggee, otherwise cancel the current input."""
    thread_id = SESSION.current_thread_id
    if SESSION.client is not None and thread_id is not None and not SESSION.is_stopped(thread_id):
        try:
            _commands.interrupt()
        except _dap.DAPError:
            pass
        return
    signal.default_int_handler(signum, frame)


def _ptpython_enabled() -> bool:
    ui = CONFIG.ui
    if ui == "readline":
        return False
    try:
        import ptpython  # noqa: F401
    except ImportError:
        if ui == "ptpython":
            print("error: ui='ptpython' requested but ptpython is not installed")
        return False
    return True


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

    from .highlighting import STYLE_OVERRIDES

    repl.all_prompt_styles["pydev"] = _PydevPromptStyle()
    repl.prompt_style = "pydev"
    repl._current_style = merge_styles([repl._current_style, STYLE_OVERRIDES])


def _embed_ptpython() -> None:
    from prompt_toolkit.patch_stdout import patch_stdout as _patch_stdout
    from ptpython.repl import PythonRepl

    from . import keybindings
    from .completion import DebuggerCompleter
    from .highlighting import make_lexer

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


def _embed_readline() -> None:
    import __main__

    hook = getattr(sys, "__interactivehook__", None)
    if hook is not None:
        hook()
    code.InteractiveConsole(vars(__main__)).interact(banner="", exitmsg="")


def _enter_repl() -> None:
    """Drop into the interactive prompt. Registered with atexit by start_eval()."""
    if _ptpython_enabled():
        _embed_ptpython()
    else:
        _embed_readline()
    os._exit(0)


def start_eval() -> None:
    """Make REPL commands available and run the inferior first if --file was given.

    Injects the commands into __main__, then returns -- any further lines in
    the wrapper script run as a normal "scenario". Once the script body
    finishes, an interactive prompt (ptpython, or readline) takes over with
    __main__'s namespace, unless the "interactive" option is False (--batch),
    in which case the process just exits.
    """
    signal.signal(signal.SIGINT, _sigint_handler)

    if CONFIG.file is not None:
        # Mirror the REPL's own repr-echo of run()'s result, since this call
        # happens before the prompt (and its repl-echo machinery) exists.
        #print(repr(_commands.run()))
        pass

    import __main__
    for name in _commands_all:
        setattr(__main__, name, getattr(_commands, name))
    # so that `config.port = 5678` works at the prompt, not just `pdvp.CONFIG`
    setattr(__main__, "config", CONFIG)

    if CONFIG.interactive:
        _enter_repl()
