"""pydev-repl: a Python debugger REPL built on pydevd.

`pdvp` is the umbrella package: `pdvp.core` is the thin one-request-one-
response command layer (no ptpython dependency), and `pdvp.extra` is the
interactive-session convenience layer built on top of it (ptpython
integration, keybindings, the gdb-style Ctrl+C policy). `pdvp` itself just
re-exports `pdvp.core`'s flat surface, so `from pdvp import *` already gives
every command plus `CONFIG`:

    import pdvp

    pdvp.process_args_envs(sys.argv[1:])

    # optional: pdvp.CONFIG.log_level = "debug"

    print(pdvp.run())
    # optional: further scenario lines, e.g. cont(), bt(5), ...

    # optional, needs pdvp.extra (ptpython installed):
    #   pdvp.extra.embed()          -- block right here
    #   pdvp.extra.install_hook()   -- make ptpython the `-i` interpreter

`pdvp.extra` is not imported here: by design (doc/architecture.md, P0), no
caller gets privileged automatic setup, and the umbrella importing it would
make ptpython a hard dependency of plain `import pdvp`. Reach for it
explicitly: `import pdvp.extra` or `from pdvp.extra import embed`.
"""

from pdvp.core import *  # noqa: F401,F403
from pdvp.core import __all__

__all__ = list(__all__)
