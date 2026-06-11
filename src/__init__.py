"""pydev-repl: a Python debugger REPL built on pydevd.

Typical wrapper script:

    import src as debug

    debug.process_args_envs(sys.argv[1:])

    # optional: debug.set("log_level", "debug")

    debug.start_eval()

start_eval() injects the REPL commands into __main__ and returns; the wrapper
script's shebang runs python with -i so the standard interactive prompt (with
readline/tab-completion bound to __main__) takes over afterwards.
"""
import os
import signal
import sys

from . import commands as _commands
from . import dap as _dap
from . import launch as _launch
from .commands import *  # noqa: F401,F403
from .commands import __all__ as _commands_all
from .session import SESSION  # noqa: F401

__all__ = [*_commands_all, "process_args_envs", "start_eval"]


def process_args_envs(argv: list[str] | None = None, env: dict[str, str] | None = None) -> None:
    """Populate RUN_CTX from the launch command line and environment.

    Does not start anything, even if --file was given (it is just saved to
    RUN_CTX for start_eval()/run() to pick up later).
    """
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env

    try:
        _launch.process_args(SESSION.run_ctx, argv)
        _launch.process_envs(SESSION.run_ctx, env)
    except _launch.LaunchError as e:
        print(f"error: {e}")
        raise SystemExit(1)


def _sigint_handler(signum, frame) -> None:
    """gdb-style Ctrl+C: pause a running debuggee, otherwise cancel the current input."""
    if SESSION.dap is not None and SESSION.running and SESSION.current_thread_id is not None:
        try:
            SESSION.dap.pause(SESSION.current_thread_id)
        except _dap.DAPError:
            pass
        return
    signal.default_int_handler(signum, frame)


def start_eval() -> None:
    """Make REPL commands available and run the inferior first if --file was given.

    Injects the commands into __main__ so that `python -i repl.py` drops into
    a normal interactive prompt (full readline/tab-completion) with them in scope.
    """
    signal.signal(signal.SIGINT, _sigint_handler)

    if SESSION.run_ctx.args_opt.file is not None:
        _commands.run()

    import __main__
    for name in _commands_all:
        setattr(__main__, name, getattr(_commands, name))
