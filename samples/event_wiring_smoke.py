#!/usr/bin/env python3
"""Batch smoke test for the event-bus wiring: run/stop/step/cont, twice.

The second cycle is the point -- it only works if SessionStarted re-arms the
bus's ending latch, so run() must be exercised more than once.

    .venv/bin/python samples/event_wiring_smoke.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdvp
from pdvp import events
from pdvp.session import SESSION

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets", "sleep_sum.py")

pdvp.process_args_envs(["--batch"])
pdvp.start_eval()

from pdvp.commands import breakpoint, cont, next, run, stop  # noqa: E402


def cycle(label: str) -> None:
    print(f"===== {label} =====")
    breakpoint(TARGET, 4)
    print(run(TARGET))
    print(next())
    print(cont())
    print("bus.ended:", SESSION.bus.ended)
    print(stop())


# A subscription taken before any connection must still be live after two of
# them: the bus outlives the client.
watcher = SESSION.bus.subscribe(events.ThreadEvent)

cycle("first run")
cycle("second run")

seen = []
while True:
    try:
        seen.append(watcher.get(timeout=0))
    except TimeoutError:
        break
print("\nprogram-lifetime subscription saw:", [type(e).__name__ for e in seen])
assert sum(isinstance(e, events.SessionEnded) for e in seen) == 2, seen
print("ok")
