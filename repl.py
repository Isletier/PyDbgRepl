#!/usr/bin/env python3
"""Entry point: ./repl.py [pydevd options] [--batch] [--file script.py [script args...]]"""
import sys
import pdvp

pdvp.process_args_envs(sys.argv[1:])

# user customization goes here, e.g.:
# pdvp.config.log_level = "debug"
#
# from pdvp import keybindings
# keybindings.unbind("f12")
# keybindings.bind("f11", "locals()")


pdvp.breakpoint("prototype/pydev_repl/examples/counter.py", 3)
pdvp.fbreak("count")
pdvp.run("prototype/pydev_repl/examples/counter.py")


pdvp.start_eval()

# optional "scenario" lines go here, e.g.:
# cont()
# bt(5)
#
# Once this script ends, an interactive prompt takes over (unless --batch
# was given) -- see doc/scenario_mode.md.
