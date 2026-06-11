"""Shared helpers for DAP client tests that spawn a real pydevd instance."""
import contextlib
import os
import time

from ... import launch
from ..client import DAPClient

CONNECT_TIMEOUT = 10.0
CONNECT_RETRY_INTERVAL = 0.1
STARTUP_DELAY = 1.5


@contextlib.contextmanager
def session(port: int, target_path: str, *args: str):
    """Spawn pydevd against `target_path` and yield a connected DAPClient.

    The caller is responsible for the initialize/attach/configurationDone
    handshake (order and breakpoint setup varies per test).
    """
    run_ctx = launch.RunContext()
    run_ctx.args_opt.port = port
    run_ctx.args_opt.file = target_path
    run_ctx.args = list(args)

    proc = launch.spawn_pydevd(run_ctx)
    try:
        time.sleep(STARTUP_DELAY)

        deadline = time.monotonic() + CONNECT_TIMEOUT
        client = None
        last_error = None
        while time.monotonic() < deadline:
            try:
                client = DAPClient.connect("127.0.0.1", port)
                break
            except OSError as e:
                last_error = e
                time.sleep(CONNECT_RETRY_INTERVAL)
        if client is None:
            raise RuntimeError(f"could not connect to pydevd on port {port}: {last_error}")

        try:
            yield client
        finally:
            client.close()
    finally:
        proc.child.kill()
        proc.child.wait()
        os.close(proc.master_fd)


def attach_and_configure(client: DAPClient, breakpoints: dict | None = None, exception_filters: list[str] | None = None) -> dict:
    """Run the initialize/attach/setBreakpoints/configurationDone handshake.

    `breakpoints` is {source_path: [{"line": N}, ...]} per setBreakpoints.
    Returns the `initialize` response capabilities.
    """
    caps = client.initialize()
    client.attach()
    client.wait_for_event("initialized", timeout=5)

    if breakpoints:
        for source_path, bps in breakpoints.items():
            client.set_breakpoints({"path": source_path}, bps)

    client.set_exception_breakpoints(exception_filters or [])
    client.configuration_done()
    return caps
