#!/usr/bin/env -S python3 -i
"""Entry point: ./repl.py [pydevd options] [--file script.py [script args...]]"""
import sys

import src as debug

debug.process_args_envs(sys.argv[1:])

# user customization goes here, e.g.:
# debug.set("log_level", "debug")
#
# from src import keybindings
# keybindings.unbind("f12")
# keybindings.bind("f11", "locals()")

debug.start_eval()
