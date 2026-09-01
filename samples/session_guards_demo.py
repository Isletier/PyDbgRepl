#!/usr/bin/env python3
"""Layer 2's two read guards, against a real pydevd.

pydevd enforces neither, and both failures are silent: a `stackTrace` on a
running thread returns a torn snapshot, and `variables` returns an empty list
that renders as "this frame has no locals". Session refuses the question
instead.

Uses samples/targets/two_threads.py, where "alpha" hits the breakpoint and
"beta" keeps running -- pydevd's default is non-stop, so one thread stops.

    .venv/bin/python samples/session_guards_demo.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdvp
from pdvp.model import Error
from pdvp.session import SESSION

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets", "two_threads.py")

pdvp.process_args_envs(["--batch"])
pdvp.start_eval()

from pdvp.commands import breakpoint, bt, locals, next, run, stop, thread, threads  # noqa: E402

breakpoint(TARGET, 16)
print(run(TARGET))

stopped_id = SESSION.current_thread_id
print("\nthread table:", [(t.id, t.name, t.stopped, t.epoch) for t in SESSION.threads])
print(threads())
print("thread table:", [(t.id, t.name, t.stopped, t.epoch) for t in SESSION.threads])

print("\n--- on the stopped thread ---")
print(bt(2))
scope = locals()
assert not isinstance(scope, Error), scope
print("locals ok:", len(scope), "names")

running = [t for t in SESSION.threads if not t.stopped]
if not running:
    print("\n(no thread was left running -- pydevd stopped everything)")
else:
    print("\n--- on a thread that is still running ---")
    print(thread(running[0].id))
    for name, result in (("bt", bt()), ("locals", locals())):
        print(f"{name}: {result}")
        assert isinstance(result, Error), f"{name} answered on a running thread: {result!r}"

print("\n--- a frame handle that outlived its stop ---")
print(thread(stopped_id))
print(bt(1))
held = SESSION.require_frame()
print("held:", held)

print(next())
now = SESSION.epoch_of(stopped_id)
print(f"after next(): thread {stopped_id} is at epoch {now}, the held frame at {held.epoch}")
assert held.epoch != now, (held.epoch, now)
# require_frame() compares exactly these two, so the held handle can no longer
# be read -- while the cursor the stop refreshed can.
assert not isinstance(locals(), Error)

stop()
print("\nok")
