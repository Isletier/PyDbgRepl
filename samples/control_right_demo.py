#!/usr/bin/env python3
"""The thread control right (doc/architecture.md §4), against a real pydevd.

Three things, in order:

  1. Two callers resuming one thread produce two serialized runs. Without the
     right they produce one: pydevd resumes on the first `continue`, the second
     is a no-op, and both waits are satisfied by the same `stopped` -- two
     commands, one run, success reported to both.
  2. `control()` extends one hold across a sequence, so nobody else's resume
     lands between our `cont()` and the `bt()` that reads the result.
  3. `pause` is exempt from the right, which is what keeps Ctrl+C working while
     somebody else's run is in flight.

Note that each caller selects its own thread: the cursor is context-local, so
a worker thread starts with none.

    .venv/bin/python samples/control_right_demo.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdvp
from pdvp import events
from pdvp.model import StopResult
from pdvp.session import SESSION

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets", "two_threads.py")

pdvp.process_args_envs(["--batch"])
pdvp.start_eval()

from pdvp.commands import breakpoint, bt, cont, control, interrupt, run, stop, thread, threads  # noqa: E402


def drain(subscription) -> list:
    """Everything queued right now, without blocking on more."""
    seen = []
    while True:
        try:
            seen.append(subscription.get(timeout=0.3))
        except TimeoutError:
            return seen


def wait_until(predicate, what: str, limit: float = 5.0) -> None:
    deadline = time.monotonic() + limit
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


breakpoint(TARGET, 16)
print(run(TARGET))

alpha = SESSION.current_thread_id
threads()                            # names, so part 3 can pick by one
# Part 3 needs a thread that is actually executing Python: pydevd's pause takes
# effect through the tracer, so a thread parked in a C-level join() stays
# unsuspended however long you wait for it. "beta" is looping.
beta = [t.id for t in SESSION.threads if t.name == "beta"][0]
print(f"alpha is thread {alpha}, the other looping thread is {beta}")


print("\n--- 1. two callers, one thread ---")
results: dict[str, object] = {}


def resume_alpha(name: str) -> None:
    thread(alpha)
    results[name] = cont()


with SESSION.bus.subscribe(events.Continued, events.Stopped,
                           match=lambda e: e.thread_id == alpha) as seen:
    callers = [threading.Thread(target=resume_alpha, args=(name,), daemon=True) for name in ("A", "B")]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join()
    traffic = [type(e).__name__ for e in drain(seen)]

for name, result in sorted(results.items()):
    print(f"{name}: {result!r}")
print("alpha's traffic:", traffic)

assert all(isinstance(r, StopResult) and r.stopped for r in results.values()), results
assert traffic.count("Continued") == 2, traffic
assert traffic.count("Stopped") == 2, traffic
assert results["A"].event is not results["B"].event, "one stop reported to both callers"


print("\n--- 2. control() holds it across a sequence ---")
thread(alpha)
rival_result = []


def rival() -> None:
    thread(alpha)
    rival_result.append(cont())


with control():                      # the current thread, alpha
    contender = threading.Thread(target=rival, daemon=True)
    contender.start()
    time.sleep(0.5)                  # long enough for it to block on the right
    assert not rival_result, "the rival resumed a thread we hold"
    print(cont())
    print(bt(1))                     # reads a stop nobody else could have moved
contender.join()
print("rival, once we let go:", rival_result[0])


print("\n--- 3. pause bypasses the right ---")
thread(beta)
interrupt()
wait_until(lambda: SESSION.is_stopped(beta), f"thread {beta} to suspend")

held = []


def run_beta() -> None:
    thread(beta)
    held.append(cont())              # holds beta's right until beta stops again


runner = threading.Thread(target=run_beta, daemon=True)

# Waiting for the `continued` event, not for the right to be taken: the right
# is acquired before the request is sent, and a pause that arrives while the
# thread is still suspended emits nothing at all (doc/architecture.md §8) --
# it would be swallowed, and the run would never end.
with SESSION.bus.subscribe(events.Continued, match=lambda e: e.thread_id == beta) as resumed:
    runner.start()
    resumed.get()

# We hold nothing, and beta's right is somebody else's -- yet this still lands,
# which is the whole reason Ctrl+C keeps working during a run.
print(interrupt())
runner.join(timeout=10)
assert held, "pause did not end the run somebody else was holding"
print("their cont() ended with:", held[0])

stop()
print("\nok")
