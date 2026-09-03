"""Two worker threads, for observing whether pydevd suspends both or just one
when a breakpoint in one of them is hit.

`beta_counter` is read back via evaluate() on alpha's (stopped) frame while
alpha sits at its breakpoint -- a module-level global is visible from any
frame in the same module, so this works without beta itself being stopped.
Two independent breakpoints (one per thread) let both threads be suspended at
once in non-stop mode, for testing that two callers hold two independent
cursors against one live session.
"""
import threading
import time

beta_counter = 0


def alpha() -> None:
    for i in range(6):
        time.sleep(0.05)
        marker = i  # breakpoint here (alpha)
    print("alpha done", flush=True)


def beta() -> None:
    global beta_counter
    for i in range(60):
        time.sleep(0.02)
        beta_counter = i  # breakpoint here (beta)
    print("beta done", flush=True)


if __name__ == "__main__":
    threads = [
        threading.Thread(target=alpha, name="alpha"),
        threading.Thread(target=beta, name="beta"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
