"""Session lifecycle: run, stop, connect, disconnect, terminate, restart."""
import contextlib
import threading
import time

from pdvp.commands.breakpoints import commit_all

from .. import dap as _dap
from .. import events
from pdvp import launch
from ..config import CONFIG
from ..session import SESSION
from pdvp.model import Error, Status, StopResult
from ._internal import (
    _clear_dap_state,
    _dispatch,
    _end_session,
    _resume,
    _stream_output,
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
    default owned-PTY-pair passthrough. Falls back to a previously assigned
    config.stdin/stdout/stderr if omitted here. Mutually exclusive with
    `config.pty`. See doc/io_model.md.

    If a session is already running, it's killed first (gdb-style restart).
    """
    return _run(script, *args, stdin=stdin, stdout=stdout, stderr=stderr)


def _run(
    script: str | None,
    *args: str,
    stdin: str | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
) -> StopResult | Error:
    prefix_lines = []
    if SESSION.client is not None or SESSION.process is not None:
        prefix_lines.append("killing previous instance")
        _stop_session()

    config = CONFIG
    if script is not None:
        config.file = script
        config.args = list(args)

    if config.file is None:
        return Error("no script given (pass one to run(), or --file at startup)")

    if stdin is not None:
        config.stdin = stdin
    if stdout is not None:
        config.stdout = stdout
    if stderr is not None:
        config.stderr = stderr

    if SESSION.client is not None:
        return Error("already connected")

    # The configuration stops being a half-edited draft here: normalize() turns
    # convenience strings into real values and rejects the rest, and
    # spawn_pydevd() owns the pty-vs-redirection conflict.
    try:
        launch.normalize(config)
    except launch.LaunchError as e:
        return Error(str(e))

    # Bind before spawning: pydevd takes the address on its command line and
    # dials back into it, so there is no race to wait out and no port to
    # guess. closing() covers the paths where we never get a connection --
    # otherwise each failed run() would leak a bound port and an fd.
    with contextlib.closing(_dap.listen()) as listener:
        host, port = listener.getsockname()

        try:
            SESSION.process = launch.spawn_pydevd(config, host, port)
        except launch.LaunchError as e:
            return Error(str(e))

        prefix_lines.append(f"launched pid={SESSION.process.child.pid}")

        if SESSION.process.master_fd is not None:
            SESSION.reader_thread = threading.Thread(
                target=_stream_output, args=(SESSION.process.master_fd,), daemon=True
            )
            SESSION.reader_thread.start()

        try:
            transport = _dap.Transport.accept(listener, CONFIG.accept_timeout)
        except TimeoutError:
            # pydevd exits 1 within milliseconds when it cannot reach us, so
            # by now the real cause is usually already on the child's status.
            status = SESSION.process.child.poll()
            if status is not None:
                return Error(f"pydevd exited with status {status} without connecting")
            return Error(f"pydevd did not connect within {CONFIG.accept_timeout}s")

    prefix_lines.append(f"connected to pydevd on {host}:{port}")
    return _handshake(_dap.Client(transport, on_event=_dispatch), prefix_lines,
                      pid=SESSION.process.child.pid)


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


def _check_capabilities(response) -> None:
    """Warn if the pydevd we reached disagrees with what we assume about it.

    We target one debugger core, so its capabilities live as constants in our
    code rather than as session state -- this is the one-time check that keeps
    those constants honest.
    """
    reported = getattr(response.body, "exceptionBreakpointFilters", None) or []
    names = [f.get("filter") if isinstance(f, dict) else getattr(f, "filter", None) for f in reported]
    names = [n for n in names if n]

    if names and names != _dap.EXCEPTION_BREAKPOINT_FILTERS:
        print(f"warning: pydevd reports exception filters {names}, expected {_dap.EXCEPTION_BREAKPOINT_FILTERS}")


def _connect(prefix_lines: list[str] | None = None) -> StopResult | Error:
    """Dial a pydevd somebody else started. The local case does not come
    through here -- run() accepts a connection instead of making one."""
    if prefix_lines is None:
        prefix_lines = []

    if SESSION.client is not None:
        return Error("already connected")

    host = CONFIG.dap_host
    port = CONFIG.port

    transport = _dap.Transport.connect(host, port, CONFIG.connection_timeout, CONFIG.connection_retry)

    prefix_lines.append(f"connected to pydevd on {host}:{port}")
    return _handshake(_dap.Client(transport, on_event=_dispatch), prefix_lines)


def _handshake(client, prefix_lines: list[str], pid: int | None = None) -> StopResult | Error:
    """Everything after the socket exists -- identical whether we dialled or
    accepted, which is the point of confining the asymmetry to construction."""
    # Adopting the client also re-arms the bus's ending latch: without that,
    # the SessionEnded left over from the previous run() would swallow this
    # session's, and every wait would be unbounded again.
    SESSION.begin(client, pid=pid)

    # `initialized` routinely arrives before the response to the request that
    # caused it, so arm first and trigger second.
    with SESSION.bus.subscribe(events.Initialized) as initialized:
        _check_capabilities(client.initialize())
        client.attach()
        if not isinstance(initialized.get(), events.Initialized):
            _end_session()
            return Error("pydevd went away during the handshake")

    commit_all()

    # configurationDone() resumes the debuggee; block for its first stop
    # (initial breakpoint) or exit, same as cont()/step() etc. No thread id --
    # nothing has stopped yet, so there is nothing to bump.
    return _resume(None, lambda c: c.configuration_done(), prefix="\n".join(prefix_lines))


def disconnect() -> Status | Error:
    """Detach from the pydevd DAP server, leaving the debuggee running. Local or remote."""
    if SESSION.client is None:
        return Error("not connected")

    client = SESSION.client
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
