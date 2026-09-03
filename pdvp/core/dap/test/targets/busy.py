"""A CPU-busy thread with no sleeps and no I/O, only deep-ish recursive calls
-- for observing whether interrupt()'s tracer-based pause can catch a thread
that never blocks, since pydevd's pause only fires at a traced call boundary.
"""
import threading


def descend(n: int) -> int:
    if n <= 0:
        return 0
    return 1 + descend(n - 1)


def busy() -> None:
    while True:
        descend(30)


if __name__ == "__main__":
    t = threading.Thread(target=busy, name="busy", daemon=True)
    t.start()
    t.join()
