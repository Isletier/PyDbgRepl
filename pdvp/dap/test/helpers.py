"""Shared helpers for DAP client tests that spawn a real pydevd instance."""
import contextlib
import os

from ... import launch
from ..client import Client
from ..transport import Transport, listen

# Generous next to the ~90ms a real connect takes, but this is a test harness
# under load, and the failure it guards against (pydevd never dials) is
# already reported by the child's exit status.
ACCEPT_TIMEOUT = 10.0


@contextlib.contextmanager
def session(target_path: str, *args: str):
    """Spawn pydevd against `target_path` and yield a connected Client.

    No port argument and no retry loop: we bind first and let the kernel pick
    the port, then pydevd dials back into it, so concurrent sessions cannot
    collide and there is no startup race to wait out.

    The caller is responsible for the initialize/attach/configurationDone
    handshake (order and breakpoint setup varies per test).
    """
    config = launch.Config()
    config.file = target_path
    config.args = list(args)

    with contextlib.closing(listen()) as listener:
        host, port = listener.getsockname()
        proc = launch.spawn_pydevd(config, host, port)
        try:
            client = Client(Transport.accept(listener, ACCEPT_TIMEOUT))
        except TimeoutError:
            proc.child.kill()
            proc.child.wait()
            raise RuntimeError(
                f"pydevd never dialled {host}:{port} "
                f"(exit status {proc.child.returncode})")

    try:
        try:
            yield client
        finally:
            client.close()
    finally:
        proc.child.kill()
        proc.child.wait()
        os.close(proc.master_fd)


def attach_and_configure(client: Client, breakpoints: dict | None = None, exception_filters: list[str] | None = None) -> dict:
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

    client.set_exception_breakpoints(exception_filters or [], [], [])
    client.configuration_done()
    return caps
