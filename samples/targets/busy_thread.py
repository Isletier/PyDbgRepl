"""One CPU-busy thread whose stack depth oscillates, one idle sleeper.

Used to observe what pydevd reports for a thread it has *not* suspended:
whether stackTrace answers at all, whether the frames are coherent, and whether
a frameId stays usable long enough for a follow-up variables request.
"""
import threading
import time


def descend(depth: int) -> int:
    if depth <= 0:
        return 0
    local_marker = depth * 2
    return local_marker + descend(depth - 1)


def busy() -> None:
    while True:
        for depth in range(1, 40):
            descend(depth)


def sleeper() -> None:
    while True:
        time.sleep(0.05)


if __name__ == "__main__":
    for target, name in ((busy, "busy"), (sleeper, "sleeper")):
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
    time.sleep(60)
