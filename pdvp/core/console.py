"""Console -- the single owner of asynchronous terminal output.

Command *results* do not come through here: commands return values and the
REPL's ordinary expression echo renders them (see doc/architecture.md, Layer 4).
What comes through here is everything the user did not ask for at the moment it
arrives -- the inferior's stdout/stderr, and stops nobody was blocked waiting
for.

One owner is what makes the prompt correct. A bare `print()` from a background
thread lands in the middle of a half-typed line and leaves a stale prompt
behind; `print_async()` clears the line, writes, and puts the prompt and the
partial input back.

`print_async` is part of the public surface, not an internal detail: a bus
subscriber that changes the program's state owns the reporting of what it did,
and its output routinely lands while somebody is at the prompt.
"""
import os
import readline
import select
import sys
import termios
import threading
import tty

from pdvp.core.session import SESSION


def print_async(message: str) -> None:
    """Print `message` from a background thread without leaving a stale prompt."""
    if SESSION.ptpython_active:
        # ptpython runs with patch_stdout=True, which already handles
        # redrawing the prompt around out-of-band writes.
        print(message)
        return

    prompt = getattr(sys, "ps1", "")
    line = readline.get_line_buffer()
    sys.stdout.write("\r" + " " * (len(prompt) + len(line)) + "\r")
    print(message)
    sys.stdout.write(prompt + line)
    sys.stdout.flush()


def pump_output(master_fd: int) -> None:
    """Copy the inferior's pty output to our stdout until the pty closes.

    The body of the pty pump thread (doc/architecture.md, §5).
    """
    while True:
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            break
        if not data:
            break
        os.write(1, data)


class StdinPassthrough:
    """Forward our stdin to the inferior's pty while a blocking resume is in flight.

    Switches our terminal to cbreak (ICANON/ECHO off, ISIG kept -- Ctrl+C still
    raises SIGINT in our process and reaches interrupt(), unchanged). Restores
    cooked mode on stop(). See doc/io_model.md.
    """

    def __init__(self, master_fd: int):
        self._master_fd = master_fd
        self._stop_r, self._stop_w = os.pipe()
        self._old_settings: list | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not sys.stdin.isatty():
            return
        try:
            self._old_settings = termios.tcgetattr(0)
            tty.setcbreak(0)
        except termios.error:
            # isatty() can be true for devices tcgetattr/setcbreak don't
            # support; fall back to no passthrough rather than crash.
            self._old_settings = None
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                rlist, _, _ = select.select([0, self._stop_r], [], [])
            except (OSError, ValueError):
                return
            if self._stop_r in rlist:
                return
            try:
                data = os.read(0, 1024)
            except OSError:
                return
            if not data:
                return
            try:
                os.write(self._master_fd, data)
            except OSError:
                return

    def stop(self) -> None:
        if self._thread is not None:
            os.write(self._stop_w, b"x")
            self._thread.join()
        os.close(self._stop_r)
        os.close(self._stop_w)
        if self._old_settings is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, self._old_settings)
