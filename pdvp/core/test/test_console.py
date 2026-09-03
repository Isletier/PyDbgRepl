"""Unit tests for console.py: StdinPassthrough, pump_output, print_async.

No pydevd, no sockets -- but unlike every other test file here, this one
touches real OS-level terminal state (fd 0, fd 1, termios), because that is
what the module under test does. Every test that redirects a real fd or
flips terminal mode restores it in a `finally`, unconditionally: a failed
assertion must never leave the test process's actual terminal in cbreak mode
or its stdout pointed at a dead pipe, or every test that runs after it in the
same process is compromised too.

`start()`/`_loop()`/`stop()` hardcode fd 0 rather than `sys.stdin.fileno()`,
so exercising the forwarding loop for real means redirecting the process's
real fd 0 -- there is no way to inject a fake stdin object underneath it.
The isatty() gate lives only in `start()`, so the forwarding tests below call
`_loop()` directly (constructing the thread by hand, the way `start()` would)
to exercise the real mechanism without needing fd 0 to be a tty, and without
going anywhere near `termios` -- `_old_settings` stays `None`, so `stop()`'s
restore path is simply not in play for those.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all
of them.

Run from the repo root with the venv active:

    python -m pdvp.core.test.test_console
"""
import contextlib
import io
import os
import threading
import time

from pdvp.core import console
from pdvp.core.session import SESSION

WAIT = 5.0


# ---- helpers

class _NotATty:
    def isatty(self) -> bool:
        return False


@contextlib.contextmanager
def _fd_saved(fd: int):
    """Save fd `fd`'s target and restore it no matter what happens inside."""
    saved = os.dup(fd)
    try:
        yield saved
    finally:
        os.dup2(saved, fd)
        os.close(saved)


def _read_available(fd: int, n: int = 4096, timeout: float = WAIT) -> bytes:
    """Poll-read `fd` until at least one byte shows up or `timeout` elapses."""
    import select
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            return os.read(fd, n)
    raise TimeoutError(f"no data on fd {fd} within {timeout}s")


# ---- StdinPassthrough: start()'s tty gate

def test_non_tty_stdin_is_a_clean_noop() -> None:
    import sys
    real_stdin = sys.stdin
    sys.stdin = _NotATty()
    try:
        read_fd, write_fd = os.pipe()
        try:
            passthrough = console.StdinPassthrough(write_fd)
            passthrough.start()
            assert passthrough._thread is None
            assert passthrough._old_settings is None
            passthrough.stop()  # must not raise even though start() no-opped
        finally:
            os.close(read_fd)
            os.close(write_fd)
    finally:
        sys.stdin = real_stdin


def test_stop_on_a_never_started_instance_does_not_raise() -> None:
    read_fd, write_fd = os.pipe()
    try:
        passthrough = console.StdinPassthrough(write_fd)
        passthrough.stop()  # never start()ed: _thread is None, _old_settings is None
    finally:
        os.close(read_fd)
        os.close(write_fd)


# ---- StdinPassthrough: the real forwarding loop, driven directly

def test_loop_forwards_bytes_from_fd0_to_master_fd() -> None:
    """Drive `_loop()` directly against a real fd 0, bypassing `start()`'s
    isatty() gate entirely -- see module docstring for why."""
    stdin_read, stdin_write = os.pipe()
    inferior_read, inferior_write = os.pipe()
    try:
        with _fd_saved(0):
            os.dup2(stdin_read, 0)

            passthrough = console.StdinPassthrough(inferior_write)
            thread = threading.Thread(target=passthrough._loop, daemon=True)
            passthrough._thread = thread
            thread.start()
            try:
                os.write(stdin_write, b"hello from the user\n")
                got = _read_available(inferior_read, timeout=WAIT)
                assert got == b"hello from the user\n", got
            finally:
                passthrough.stop()
                thread.join(timeout=WAIT)
                assert not thread.is_alive()
    finally:
        for fd in (stdin_read, stdin_write, inferior_read, inferior_write):
            os.close(fd)


def test_stop_signal_unblocks_the_loop_with_nothing_pending() -> None:
    """The stop pipe wakes select() even when no stdin data ever arrives."""
    stdin_read, stdin_write = os.pipe()
    inferior_read, inferior_write = os.pipe()
    try:
        with _fd_saved(0):
            os.dup2(stdin_read, 0)

            passthrough = console.StdinPassthrough(inferior_write)
            thread = threading.Thread(target=passthrough._loop, daemon=True)
            passthrough._thread = thread
            thread.start()
            time.sleep(0.05)  # let the loop actually reach select()
            passthrough.stop()
            thread.join(timeout=WAIT)
            assert not thread.is_alive()
    finally:
        for fd in (stdin_read, stdin_write, inferior_read, inferior_write):
            os.close(fd)


# ---- pump_output

def test_pump_output_ends_cleanly_on_eof() -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)  # EOF immediately visible to the reader

    thread = threading.Thread(target=console.pump_output, args=(read_fd,), daemon=True)
    thread.start()
    thread.join(timeout=WAIT)
    assert not thread.is_alive()
    os.close(read_fd)


def test_pump_output_ends_cleanly_on_a_read_error() -> None:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)  # the fd pump_output is handed is already dead

    thread = threading.Thread(target=console.pump_output, args=(read_fd,), daemon=True)
    thread.start()
    thread.join(timeout=WAIT)
    assert not thread.is_alive()
    os.close(write_fd)


def test_pump_output_copies_data_to_fd1() -> None:
    read_fd, write_fd = os.pipe()
    capture_read, capture_write = os.pipe()
    try:
        with _fd_saved(1):
            os.dup2(capture_write, 1)
            os.close(capture_write)

            thread = threading.Thread(target=console.pump_output, args=(read_fd,), daemon=True)
            thread.start()
            try:
                os.write(write_fd, b"child output\n")
                got = _read_available(capture_read, timeout=WAIT)
                assert got == b"child output\n", got
            finally:
                os.close(write_fd)  # EOF: lets the pump thread exit
                thread.join(timeout=WAIT)
                assert not thread.is_alive()
    finally:
        os.close(read_fd)
        os.close(capture_read)


# ---- print_async

def _saved_ptpython_active():
    return SESSION.ptpython_active


def test_print_async_under_ptpython_just_prints() -> None:
    original = _saved_ptpython_active()
    SESSION.ptpython_active = True
    original_get_line_buffer = console.readline.get_line_buffer
    console.readline.get_line_buffer = lambda: (_ for _ in ()).throw(
        AssertionError("get_line_buffer() must not be called on the ptpython path"))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            console.print_async("hello")
    finally:
        console.readline.get_line_buffer = original_get_line_buffer
        SESSION.ptpython_active = original

    assert "hello" in buf.getvalue()
    assert "\r" not in buf.getvalue()


def test_print_async_without_ptpython_redraws_the_prompt() -> None:
    original = _saved_ptpython_active()
    SESSION.ptpython_active = False
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            console.print_async("hello")
    finally:
        SESSION.ptpython_active = original

    output = buf.getvalue()
    assert "hello" in output
    assert "\r" in output  # the clear-line sequence: absent from the ptpython path


TESTS = [value for name, value in sorted(globals().items()) if name.startswith("test_")]


def main() -> int:
    failures = 0
    for test in TESTS:
        try:
            test()
        except Exception as error:
            failures += 1
            print(f"FAIL {test.__name__}: {type(error).__name__}: {error}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
