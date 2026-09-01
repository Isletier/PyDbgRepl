"""Two worker threads looping independently, for observing whether pydevd
suspends one thread or all of them when a breakpoint is hit.

Thread "alpha" hits the breakpoint on line 16; "beta" keeps counting. If only
alpha is suspended, beta's counter keeps advancing while we sit at the stop.
"""
import threading
import time

beta_counter = 0


def alpha() -> None:
    for i in range(1000):
        time.sleep(0.05)
        print(f"alpha {i} (beta at {beta_counter})", flush=True)  # breakpoint here


def beta() -> None:
    global beta_counter
    for i in range(1000):
        time.sleep(0.05)
        beta_counter = i


if __name__ == "__main__":
    threads = [
        threading.Thread(target=alpha, name="alpha"),
        threading.Thread(target=beta, name="beta"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
