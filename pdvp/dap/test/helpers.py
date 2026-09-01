"""Shared helpers for DAP client tests that spawn a real pydevd instance."""
import contextlib
import os

from pdvp import events, launch
from pdvp.schema import pydevd_schema as schema
from pdvp.dap.client import Client, ConnectionClosed
from pdvp.dap.transport import Transport, listen

# Generous next to the ~90ms a real connect takes, but this is a test harness
# under load, and the failure it guards against (pydevd never dials) is
# already reported by the child's exit status.
ACCEPT_TIMEOUT = 10.0


def reduce_to(bus: events.EventBus):
    """A stand-in for Session's reducer: DAP in, pdvp events out.

    Session will own this and add state to it. Here it is only the translation,
    which is all a client-level test needs.
    """
    def on_event(message) -> None:
        if isinstance(message, ConnectionClosed):
            bus.publish(events.SessionEnded(
                events.EndReason.CLOSED if message.deliberate else events.EndReason.DISCONNECTED,
                detail=message.detail))
        else:
            bus.publish(events.from_dap(message.event, message.body))

    return on_event


@contextlib.contextmanager
def session(target_path: str, *args: str):
    """Spawn pydevd against `target_path` and yield a connected (client, bus).

    No port argument and no retry loop: we bind first and let the kernel pick
    the port, then pydevd dials back into it, so concurrent sessions cannot
    collide and there is no startup race to wait out.

    The caller is responsible for the initialize/attach/configurationDone
    handshake (order and breakpoint setup varies per test).
    """
    config = launch.Config()
    config.file = target_path
    config.args = list(args)

    bus = events.EventBus()

    with contextlib.closing(listen()) as listener:
        host, port = listener.getsockname()
        proc = launch.spawn_pydevd(config, host, port)
        try:
            client = Client(Transport.accept(listener, ACCEPT_TIMEOUT), on_event=reduce_to(bus))
        except TimeoutError:
            proc.child.kill()
            proc.child.wait()
            raise RuntimeError(
                f"pydevd never dialled {host}:{port} "
                f"(exit status {proc.child.returncode})")

    try:
        try:
            yield client, bus
        finally:
            client.close()
            bus.close()
    finally:
        proc.child.kill()
        proc.child.wait()
        os.close(proc.master_fd)


def attach_and_configure(client: Client, bus: events.EventBus,
                         breakpoints: dict | None = None,
                         exception_filters: list[str] | None = None):
    """Run the initialize/attach/setBreakpoints/configurationDone handshake.

    `breakpoints` is {source_path: [{"line": N}, ...]} per setBreakpoints.
    Returns the `initialize` response's capabilities body.

    The subscription is opened before anything is sent, because `initialized`
    routinely beats the response that caused it onto the wire: arm, then
    trigger. The timeout is the harness's -- a hung handshake should fail the
    run, not wedge it.
    """
    with bus.subscribe(events.Initialized) as initialized:
        capabilities = client.initialize().body
        client.attach()
        assert isinstance(initialized.get(timeout=10), events.Initialized)

    for source_path, source_breakpoints in (breakpoints or {}).items():
        client.set_breakpoints(schema.Source(path=source_path),
                               [schema.SourceBreakpoint(**bp) for bp in source_breakpoints])

    client.set_exception_breakpoints(exception_filters or [], [], [])
    client.configuration_done()
    return capabilities
