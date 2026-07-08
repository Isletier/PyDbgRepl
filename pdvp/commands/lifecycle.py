"""Session lifecycle: run, stop, connect, disconnect, terminate, restart."""
import threading
import time

from .. import dap as _dap
from .. import launch as _launch
from ..session import SESSION
from ._display import Error, Status, StopResult
from ._internal import (
    _clear_dap_state,
    _end_session,
    _on_dap_disconnect,
    _stream_output,
    _wait_for_resume_result,
)

__all__ = ["run", "stop", "connect", "disconnect", "terminate", "restart"]


def run(
    script: str | None = None,
    *args: str,
    stdin: str | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
) -> StopResult | Error:
    """Launch pydevd against `script` and connect to it, e.g. run("script.py", "--foo", "bar").

    If omitted, `script`/`args` fall back to the --file (and trailing args)
    given on the command line at startup.

    `stdin`/`stdout`/`stderr` redirect the inferior's streams to files
    (`stderr="&1"` aliases stdout), gdb-style; any unset stream keeps the
    default owned-PTY-pair passthrough. Falls back to a previously
    set("stdin"/"stdout"/"stderr", ...) value if omitted here. Mutually
    exclusive with `set("pty", ...)`. See doc/io_model.md.

    If a session is already running, it's killed first (gdb-style restart).
    """
    return _run([], script, *args, stdin=stdin, stdout=stdout, stderr=stderr)


def _run(
    prefix_lines: list[str],
    script: str | None,
    *args: str,
    stdin: str | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
) -> StopResult | Error:
    if SESSION.client is not None or SESSION.process is not None:
        prefix_lines.append("killing previous instance")
        _stop_session()

    run_ctx = SESSION.run_ctx
    if script is not None:
        run_ctx.args_opt.file = script
        run_ctx.args = list(args)

    if run_ctx.args_opt.file is None:
        return Error("no script given (pass one to run(), or --file at startup)")

    if stdin is not None:
        run_ctx.args_opt.stdin = stdin
    if stdout is not None:
        run_ctx.args_opt.stdout = stdout
    if stderr is not None:
        run_ctx.args_opt.stderr = stderr

    if run_ctx.args_opt.pty is not None and (
        run_ctx.args_opt.stdin is not None
        or run_ctx.args_opt.stdout is not None
        or run_ctx.args_opt.stderr is not None
    ):
        return Error("--pty conflicts with stdin=/stdout=/stderr= redirection -- unset one")

    SESSION.process = _launch.spawn_pydevd(run_ctx, run_ctx.args_opt.pty)
    prefix_lines.append(f"launched pid={SESSION.process.child.pid}")

    if SESSION.process.master_fd is not None:
        SESSION.reader_thread = threading.Thread(
            target=_stream_output, args=(SESSION.process.master_fd,), daemon=True
        )
        SESSION.reader_thread.start()

    # The pydevd server takes a moment to bind its socket after spawning.
    return _connect(retries=25, delay=0.2, prefix_lines=prefix_lines)


def _stop_session() -> None:
    """Tear down the current session: end our process, or ask a remote pydevd to terminate the debuggee."""
    if SESSION.process is None and SESSION.client is not None:
        # Remote session: ask pydevd to terminate the debuggee on its end.
        try:
            SESSION.client.disconnect(terminate_debuggee=True)
        except _dap.DAPError:
            pass

    _end_session()


def stop() -> Status | Error:
    """End the session: the pydevd connection and any spawned process share one lifetime."""
    if SESSION.client is None and SESSION.process is None:
        return Error("no active session")

    _stop_session()
    return Status("session stopped")


def connect() -> StopResult | Error:
    """Connect to a remote pydevd DAP server (one we did not spawn ourselves).

    Assumes pydevd is already up and listening; for a session started with
    run(), the connect handshake already happened automatically.
    """
    return _connect()


def _connect(retries: int = 1, delay: float = 0.2, prefix_lines: list[str] | None = None) -> StopResult | Error:
    if prefix_lines is None:
        prefix_lines = []

    if SESSION.client is not None:
        return Error("already connected")

    host = SESSION.options.dap_host
    port = SESSION.run_ctx.args_opt.port

    client = None
    for attempt in range(retries):
        try:
            client = _dap.Client.connect(host, port)
            break
        except OSError as e:
            if attempt + 1 == retries:
                return Error(f"could not connect to {host}:{port}: {e}")
            time.sleep(delay)

    SESSION.capabilities = client.initialize()
    client.attach()
    client.wait_for_event("initialized", timeout=5)

    for path, bps in SESSION.breakpoints.items():
        sent = [{k: v for k, v in b.items() if k != "enabled"} for b in bps if b.get("enabled", True)]
        client.set_breakpoints({"path": path}, sent)
    if SESSION.function_breakpoints:
        client.set_function_breakpoints(SESSION.function_breakpoints)
    client.set_exception_breakpoints(SESSION.exception_filters, [], [])

    client.on_disconnect = _on_dap_disconnect
    SESSION.client = client
    SESSION.running = True

    client.configuration_done()
    prefix_lines.append(f"connected to pydevd on {host}:{port}")

    # configurationDone() resumes the debuggee; block for its first stop
    # (initial breakpoint) or exit, same as cont()/step() etc.
    return _wait_for_resume_result(client, prefix="\n".join(prefix_lines))


def disconnect() -> Status | Error:
    """Detach from the pydevd DAP server, leaving the debuggee running. Local or remote."""
    if SESSION.client is None:
        return Error("not connected")

    client = SESSION.client
    client.on_disconnect = None
    try:
        client.disconnect(terminate_debuggee=False)
    except _dap.DAPError:
        pass
    client.close()
    _clear_dap_state()
    return Status("disconnected")


def terminate() -> Status | Error:
    """Ask pydevd to terminate the debuggee via the DAP terminate request. Local or remote."""
    if SESSION.client is None:
        return Error("not connected")
    try:
        SESSION.client.terminate()
    except _dap.DAPError as e:
        return Error(str(e))
    return Status("terminate requested")


def restart() -> StopResult | Error:
    """Restart the debuggee: stop() the current session (if any), then run() again."""
    if SESSION.client is not None or SESSION.process is not None:
        stop()
    return run()
