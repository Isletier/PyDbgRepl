#!/usr/bin/env python3
"""End-to-end check of the per-caller cursor, the mode switch, and wait=False.

Drives samples/targets/two_threads.py, which runs two independent workers, so
all-stop and non-stop are actually distinguishable.

    .venv/bin/python samples/cursor_and_modes_demo.py

Exercises, in order: the session-wide cursor default; an explicit selection
showing up in cursors(); a worker thread reading the default rather than the
REPL's selection; the all-stop -> non-stop switch and its breakpoint re-commit;
cont(wait=False) returning a Resumption; interrupt() as stop-the-world; and the
selection being dropped when the connection ends.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdvp
from pdvp import CONFIG
from pdvp.core.session import SESSION

TARGET = "samples/targets/two_threads.py"


def show(label, value):
    print(f"\n--- {label}\n{value!r}")


def _run_state() -> str:
    """Which threads pydevd actually parked -- the difference between the modes."""
    return ", ".join(
        f"{t.name or t.id}={'stopped' if t.stopped else 'running'}"
        for t in sorted(SESSION.threads, key=lambda t: t.id))


def main() -> int:
    pdvp.process_args_envs([])
    CONFIG.non_stop = False             # start in all-stop

    show("breakpoint", pdvp.breakpoint(TARGET, 16))

    started = pdvp.run(TARGET)
    show("run (all-stop)", started)
    if not started:
        return 1

    show("threads", pdvp.threads())
    show("cursors: threads() selected one", pdvp.cursors())

    show("bt", pdvp.bt())

    # A second caller has its own cursor and never sees ours.
    seen = {}
    worker = threading.Thread(
        target=lambda: seen.update(thread=SESSION.current_thread_id))
    worker.start()
    worker.join()
    print(f"\n--- worker read thread {seen['thread']} (the session default)")

    print(f"\n--- all-stop suspended: {_run_state()}")

    show("non_stop(True)", pdvp.non_stop(True))
    show("cont() in non-stop", pdvp.cont())
    print(f"\n--- non-stop suspended: {_run_state()}")

    resumption = pdvp.cont(wait=False)
    show("cont(wait=False)", resumption)
    show("a second resume, refused while it moves", pdvp.cont(wait=False))
    show("interrupt", pdvp.interrupt())
    show("resumption.wait()", resumption.wait())

    # A stop nobody is blocked on: in non-stop this is how a second thread
    # reports a breakpoint, and the console has to say so or it is silent.
    show("breakpoint in beta", pdvp.breakpoint(TARGET, 23))
    show("resume alpha in the background", pdvp.cont(wait=False))
    print("\n--- waiting for beta to hit its own breakpoint (announced below)")
    time.sleep(2)

    show("cursors before teardown", pdvp.cursors())
    show("stop", pdvp.stop())
    show("cursors after teardown", pdvp.cursors())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
