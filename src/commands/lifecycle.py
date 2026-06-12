"""Session lifecycle: run, stop, connect, disconnect, terminate, restart."""
import threading
import time

from .. import dap as _dap
from .. import launch as _launch
from ..session import SESSION
from ._internal import (
    _clear_dap_state,
    _end_session,
    _on_dap_disconnect,
    _stream_output,
    _wait_for_resume_result,
)

__all__ = ["run", "stop", "connect", "disconnect", "terminate", "restart"]


def run(script: str | None = None, *args: str) -> None:
    """Launch pydevd against `script` and connect to it, e.g. run("script.py", "--foo", "bar").

    If omitted, falls back to the --file (and trailing args) given on the
    command line at startup.
    """
    if SESSION.process is not None:
        print("error: a session is already running")
        return

    run_ctx = SESSION.run_ctx
    if script is not None:
        run_ctx.args_opt.file = script
        run_ctx.args = list(args)

    if run_ctx.args_opt.file is None:
        print("error: no script given (pass one to run(), or --file at startup)")
        return

    SESSION.process = _launch.spawn_pydevd(run_ctx)
    print(f"launched pid={SESSION.process.child.pid}")

    SESSION.reader_thread = threading.Thread(
        target=_stream_output, args=(SESSION.process.master_fd,), daemon=True
    )
    SESSION.reader_thread.start()

    # The pydevd server takes a moment to bind its socket after spawning.
    _connect(retries=25, delay=0.2)


def stop() -> None:
    """End the session: the pydevd connection and any spawned process share one lifetime."""
    if SESSION.dap is None and SESSION.process is None:
        print("error: no active session")
        return

    if SESSION.process is None and SESSION.dap is not None:
        # Remote session: ask pydevd to terminate the debuggee on its end.
        try:
            SESSION.dap.disconnect(terminate_debuggee=True)
        except _dap.DAPError:
            pass

    _end_session()
    print("session stopped")


def connect() -> None:
    """Connect to a remote pydevd DAP server (one we did not spawn ourselves).

    Assumes pydevd is already up and listening; for a session started with
    run(), the connect handshake already happened automatically.
    """
    _connect()


def _connect(retries: int = 1, delay: float = 0.2) -> None:
    if SESSION.dap is not None:
        print("error: already connected")
        return

    host = SESSION.options.dap_host
    port = SESSION.run_ctx.args_opt.port

    client = None
    for attempt in range(retries):
        try:
            client = _dap.DAPClient.connect(host, port)
            break
        except OSError as e:
            if attempt + 1 == retries:
                print(f"error: could not connect to {host}:{port}: {e}")
                return
            time.sleep(delay)

    client.initialize()
    client.attach()
    client.wait_for_event("initialized", timeout=5)

    for path, bps in SESSION.breakpoints.items():
        sent = [{k: v for k, v in b.items() if k != "enabled"} for b in bps if b.get("enabled", True)]
        client.set_breakpoints({"path": path}, sent)
    if SESSION.function_breakpoints:
        client.set_function_breakpoints(SESSION.function_breakpoints)
    client.set_exception_breakpoints(SESSION.exception_filters)

    client.on_disconnect = _on_dap_disconnect
    SESSION.dap = client
    SESSION.running = True

    client.configuration_done()
    print(f"connected to pydevd on {host}:{port}")

    # configurationDone() resumes the debuggee; block for its first stop
    # (initial breakpoint) or exit, same as cont()/step() etc.
    _wait_for_resume_result(client)


def disconnect() -> None:
    """Detach from the pydevd DAP server, leaving the debuggee running. Local or remote."""
    if SESSION.dap is None:
        print("error: not connected")
        return

    client = SESSION.dap
    client.on_disconnect = None
    try:
        client.disconnect(terminate_debuggee=False)
    except _dap.DAPError:
        pass
    client.close()
    _clear_dap_state()
    print("disconnected")


def terminate() -> None:
    """Ask pydevd to terminate the debuggee via the DAP terminate request. Local or remote."""
    if SESSION.dap is None:
        print("error: not connected")
        return
    try:
        SESSION.dap.terminate()
    except _dap.DAPError as e:
        print(f"error: {e}")
        return
    print("terminate requested")


def restart() -> None:
    """Restart the debuggee: stop() the current session (if any), then run() again."""
    if SESSION.dap is not None or SESSION.process is not None:
        stop()
    run()
