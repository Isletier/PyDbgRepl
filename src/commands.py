"""Functions exposed to the REPL global namespace."""
import os
import readline
import sys
import threading
import time

from . import dap as _dap
from . import launch as _launch
from . import options as _options
from .session import SESSION

__all__ = [
    "run", "stop", "set", "unset",
    "connect", "disconnect", "terminate",
    "cont", "step", "next", "finish", "interrupt",
    "threads", "thread", "bt", "frame", "p", "locals",
    "breakpoint", "clear", "catch",
]


def _stream_output(master_fd: int) -> None:
    while True:
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            break
        if not data:
            break
        os.write(1, data)


def set(name: str, value) -> None:
    """Set an option, e.g. set("port", 5678) or set("vm_type", "jython")."""
    try:
        result = _options.set_option(name, value)
    except KeyError:
        print(f"error: unknown option '{name}'")
        return
    except (_launch.LaunchError, ValueError) as e:
        print(f"error: {e}")
        return
    print(f"{name} = {result!r}")


def unset(name: str) -> None:
    """Reset an option to its default value."""
    try:
        default = _options.unset_option(name)
    except KeyError:
        print(f"error: unknown option '{name}'")
        return
    print(f"{name} = {default!r}")


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
    """Kill the pydevd session we spawned (run()), if any.

    There is no local process to kill for a remote (connect()-only) session;
    use terminate() or disconnect() instead.
    """
    if SESSION.process is None:
        print("error: no local session to stop (use terminate() or disconnect() for a remote session)")
        return

    if SESSION.dap is not None:
        client = SESSION.dap
        client.on_disconnect = None
        try:
            client.disconnect(terminate_debuggee=True)
        except _dap.DAPError:
            pass
        client.close()
        _clear_dap_state()

    SESSION.process.child.kill()
    SESSION.process.child.wait()
    os.close(SESSION.process.master_fd)
    SESSION.process = None
    SESSION.reader_thread = None
    print("session stopped")


# ---- DAP connection ----

def _clear_dap_state() -> None:
    SESSION.dap = None
    SESSION.running = False
    SESSION.current_thread_id = None
    SESSION.current_frame_id = None


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
        client.set_breakpoints({"path": path}, bps)
    client.set_exception_breakpoints(SESSION.exception_filters)

    # Register dispatch hooks before configurationDone(), since that request
    # may let the debuggee run immediately and hit a breakpoint before this
    # function returns.
    client.on_event = _on_dap_event
    client.on_disconnect = _on_dap_disconnect
    SESSION.dap = client
    SESSION.running = True

    client.configuration_done()

    print(f"connected to pydevd on {host}:{port}")


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


# ---- async event dispatch ----

def _async_print(message: str) -> None:
    """Print `message` from a background thread without leaving a stale prompt.

    Clears the current input line, prints the message, then rewrites the
    prompt and whatever the user had typed so far.
    """
    prompt = getattr(sys, "ps1", "")
    line = readline.get_line_buffer()
    sys.stdout.write("\r" + " " * (len(prompt) + len(line)) + "\r")
    print(message)
    sys.stdout.write(prompt + line)
    sys.stdout.flush()


def _on_dap_event(message: dict) -> None:
    event = message.get("event")
    body = message.get("body") or {}

    if event == "stopped":
        # stackTrace etc. need a request/response round trip, which would
        # deadlock if done synchronously from the reader thread (it's the
        # same thread that reads the response). Handle it on its own thread.
        threading.Thread(target=_handle_stopped, args=(body,), daemon=True).start()
    elif event == "exited":
        SESSION.running = False
        SESSION.current_thread_id = None
        SESSION.current_frame_id = None
        _async_print(f"*** program exited with code {body.get('exitCode')}")
    elif event == "terminated":
        SESSION.running = False
        SESSION.current_thread_id = None
        SESSION.current_frame_id = None
        _async_print("*** program terminated")


def _handle_stopped(body: dict) -> None:
    # Don't flip SESSION.running to False until we're fully done handling this
    # stop (including the stackTrace fetch below). Otherwise a resume command
    # issued by the user can race in and send pydevd an overlapping
    # request for the same thread while this stop is still being processed,
    # which can desync pydevd's stepping state machine and silently drop the
    # next "stopped" event, leaving SESSION.running stuck at True forever.
    SESSION.current_thread_id = body.get("threadId")
    SESSION.current_frame_id = None
    reason = body.get("reason")

    if SESSION.current_thread_id is None:
        SESSION.running = False
        _async_print(f"*** stopped ({reason})")
        return

    try:
        trace = SESSION.dap.stack_trace(SESSION.current_thread_id, levels=1)
    except _dap.DAPError:
        SESSION.running = False
        _async_print(f"*** stopped ({reason})")
        return

    frames = trace["stackFrames"]
    if not frames:
        SESSION.running = False
        _async_print(f"*** stopped ({reason})")
        return

    top = frames[0]
    SESSION.current_frame_id = top["id"]
    path = (top.get("source") or {}).get("path", "?")
    SESSION.running = False
    _async_print(f"*** stopped ({reason}) at {path}:{top['line']}, in {top['name']}")


def _on_dap_disconnect() -> None:
    if SESSION.dap is None:
        return
    _clear_dap_state()
    _async_print("*** connection to pydevd lost")


# ---- execution control ----

def _ensure_dap_paused() -> bool:
    if SESSION.dap is None:
        print("error: not connected (use connect())")
        return False
    if SESSION.running:
        print("error: program is running")
        return False
    return True


def _ensure_thread_paused() -> bool:
    if not _ensure_dap_paused():
        return False
    if SESSION.current_thread_id is None:
        print("error: no current thread (use threads())")
        return False
    return True


def cont() -> None:
    """Resume execution. Returns immediately; the stop is reported asynchronously."""
    if not _ensure_thread_paused():
        return
    SESSION.dap.continue_(SESSION.current_thread_id)
    SESSION.running = True
    print("continuing")


def step() -> None:
    """Step into the next line, descending into calls. Returns immediately."""
    if not _ensure_thread_paused():
        return
    SESSION.dap.step_in(SESSION.current_thread_id)
    SESSION.running = True
    print("stepping")


def next() -> None:
    """Step over the next line, without descending into calls. Returns immediately."""
    if not _ensure_thread_paused():
        return
    SESSION.dap.next(SESSION.current_thread_id)
    SESSION.running = True
    print("stepping over")


def finish() -> None:
    """Run until the current function returns. Returns immediately."""
    if not _ensure_thread_paused():
        return
    SESSION.dap.step_out(SESSION.current_thread_id)
    SESSION.running = True
    print("finishing")


def interrupt() -> None:
    """Pause a running program. Returns immediately."""
    if SESSION.dap is None:
        print("error: not connected (use connect())")
        return
    if SESSION.current_thread_id is None:
        print("error: no current thread (use threads())")
        return
    if not SESSION.running:
        print("error: program is not running")
        return
    SESSION.dap.pause(SESSION.current_thread_id)
    print("interrupting")


# ---- inspection ----

def threads() -> None:
    """List threads. Picks a current thread if none is selected yet."""
    if SESSION.dap is None:
        print("error: not connected")
        return

    thread_list = SESSION.dap.threads()["threads"]
    for t in thread_list:
        marker = "*" if t["id"] == SESSION.current_thread_id else " "
        print(f"{marker} {t['id']}: {t['name']}")

    if SESSION.current_thread_id is None and thread_list:
        SESSION.current_thread_id = thread_list[0]["id"]


def thread(thread_id: int) -> None:
    """Switch the current thread."""
    if SESSION.dap is None:
        print("error: not connected")
        return
    SESSION.current_thread_id = thread_id
    SESSION.current_frame_id = None
    print(f"current thread is now {thread_id}")


def bt(levels: int | None = None) -> None:
    """Print the stack trace for the current thread."""
    if not _ensure_thread_paused():
        return

    trace = SESSION.dap.stack_trace(SESSION.current_thread_id, levels=levels)
    frames = trace["stackFrames"]
    for i, f in enumerate(frames):
        marker = "*" if f["id"] == SESSION.current_frame_id else " "
        path = (f.get("source") or {}).get("path", "?")
        print(f"{marker} #{i} {f['name']} at {path}:{f['line']}")

    if SESSION.current_frame_id is None and frames:
        SESSION.current_frame_id = frames[0]["id"]


def frame(index: int) -> None:
    """Select frame `index` (0 = innermost) from the current thread's stack."""
    if not _ensure_thread_paused():
        return

    frames = SESSION.dap.stack_trace(SESSION.current_thread_id)["stackFrames"]
    if not (0 <= index < len(frames)):
        print(f"error: no frame {index}")
        return

    f = frames[index]
    SESSION.current_frame_id = f["id"]
    path = (f.get("source") or {}).get("path", "?")
    print(f"#{index} {f['name']} at {path}:{f['line']}")


def p(expression: str) -> None:
    """Evaluate `expression` in the current frame and print the result."""
    if not _ensure_dap_paused():
        return

    try:
        result = SESSION.dap.evaluate(expression, frame_id=SESSION.current_frame_id, context="repl")
    except _dap.DAPError as e:
        print(f"error: {e}")
        return
    print(result["result"])


def locals() -> None:
    """Print local variables of the current frame."""
    if not _ensure_dap_paused():
        return
    if SESSION.current_frame_id is None:
        print("error: no current frame (use bt())")
        return

    for scope in SESSION.dap.scopes(SESSION.current_frame_id)["scopes"]:
        if scope["name"] != "Locals":
            continue
        for v in SESSION.dap.variables(scope["variablesReference"])["variables"]:
            print(f"{v['name']} = {v['value']}")


# ---- breakpoints ----

def breakpoint(path: str, line: int, condition: str | None = None) -> None:
    """Set a line breakpoint at `path`:`line`, optionally conditional."""
    bp = {"line": line}
    if condition is not None:
        bp["condition"] = condition

    bps = SESSION.breakpoints.setdefault(path, [])
    bps[:] = [b for b in bps if b["line"] != line] + [bp]

    if SESSION.dap is not None:
        SESSION.dap.set_breakpoints({"path": path}, bps)
    print(f"breakpoint set at {path}:{line}")


def clear(path: str, line: int) -> None:
    """Remove the breakpoint at `path`:`line`, if any."""
    bps = SESSION.breakpoints.get(path, [])
    bps[:] = [b for b in bps if b["line"] != line]

    if SESSION.dap is not None:
        SESSION.dap.set_breakpoints({"path": path}, bps)
    print(f"breakpoint cleared at {path}:{line}")


def catch(*filters: str) -> None:
    """Set exception breakpoint filters, e.g. catch("raised", "uncaught")."""
    SESSION.exception_filters = list(filters)
    if SESSION.dap is not None:
        SESSION.dap.set_exception_breakpoints(SESSION.exception_filters)
    print(f"exception filters = {SESSION.exception_filters}")
