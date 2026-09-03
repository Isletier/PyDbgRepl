#!/usr/bin/env python3
"""The three ways a session ends, now that they are all one bus event.

  1. disconnect() while stopped -- deliberate, no "connection lost" noise
  2. pydevd killed while stopped   -- the reducer tears down and says so
  3. pydevd killed while running   -- the blocked cont() reports it instead

    .venv/bin/python samples/event_death_paths.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdvp import breakpoint, cont, disconnect, run, stop
from pdvp.core.session import SESSION

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets", "sleep_sum.py")

breakpoint(TARGET, 4)


print("===== 1. deliberate disconnect while stopped =====")
print(run(TARGET))
print(disconnect())
print("client:", SESSION.client, " ended:", SESSION.bus.ended.reason)
stop()


print("\n===== 2. pydevd killed while stopped =====")
print(run(TARGET))
SESSION.process.child.kill()
time.sleep(1)                       # let the reader thread notice
print("client:", SESSION.client, " ended:", SESSION.bus.ended.reason)
stop()


print("\n===== 3. pydevd killed while running =====")
print(run(TARGET))
proc = SESSION.process


def kill_soon():
    time.sleep(0.5)
    proc.child.kill()


import threading  # noqa: E402
threading.Thread(target=kill_soon, daemon=True).start()
print(cont())                       # blocks; must be woken by the death
print("client:", SESSION.client, " ended:", SESSION.bus.ended.reason)
stop()

print("\nok")
