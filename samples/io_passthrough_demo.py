"""End-to-end demo/smoke test for the stdin-passthrough I/O model.

See doc/io_model.md. Spawns samples/targets/echo_input.py under pydevd
via pydev-repl's run(), fakes a tty for our own stdin (so
_StdinPassthrough.start() activates -- it no-ops when isatty() is false), then
while the debuggee is blocked in sys.stdin.readline() writes "world\\n" to
that fake tty and expects the debuggee to print "hello world" back.

Run directly:
    .venv/bin/python samples/io_passthrough_demo.py
"""
import os
import pty
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import src as debug

# Fake a tty for our own stdin so the passthrough thread activates.
our_master, our_slave = pty.openpty()
os.dup2(our_slave, 0)
os.close(our_slave)

target = os.path.join(os.path.dirname(__file__), "targets", "echo_input.py")
debug.process_args_envs(["--file", target])
print(f"port: {debug.SESSION.run_ctx.args_opt.port}", flush=True)


def feeder() -> None:
    # Give the debuggee time to reach sys.stdin.readline() before writing.
    time.sleep(2)
    os.write(our_master, b"world\n")


threading.Thread(target=feeder, daemon=True).start()

# run() blocks until the debuggee exits (_wait_for_resume_result); the
# passthrough thread forwards our_master -> the debuggee's pty during that
# block, and _stream_output copies the debuggee's "hello world" back to our
# real stdout.
debug.run()

if debug.SESSION.dap is not None or debug.SESSION.process is not None:
    debug.stop()
