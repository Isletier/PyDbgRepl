"""Functions exposed to the REPL global namespace."""
import dataclasses
import os
import threading

from . import launch as _launch
from .session import SESSION

__all__ = ["run", "stop", "connect", "set", "unset"]

_REFLECTIONS = {
    "vm_type": _launch.vm_type_reflection,
    "log_level": _launch.log_level_reflection,
    "qt_support": _launch.qt_support_reflection,
}


def _stream_output(master_fd: int) -> None:
    while True:
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            break
        if not data:
            break
        os.write(1, data)


def _resolve_target(name: str):
    run_ctx = SESSION.run_ctx
    if hasattr(run_ctx.args_opt, name):
        return run_ctx.args_opt
    if hasattr(run_ctx.env, name):
        return run_ctx.env
    return None


def _field_for(target, name: str) -> dataclasses.Field:
    for f in dataclasses.fields(target):
        if f.name == name:
            return f
    raise KeyError(name)


def set(name: str, value) -> None:
    """Set a RUN_CTX option, e.g. set("port", 5678) or set("vm_type", "jython")."""
    target = _resolve_target(name)
    if target is None:
        print(f"error: unknown option '{name}'")
        return

    if isinstance(value, str):
        if name in _REFLECTIONS:
            try:
                value = _REFLECTIONS[name](value)
            except _launch.LaunchError as e:
                print(f"error: {e}")
                return
        else:
            kind = _launch._unwrap_optional(_field_for(target, name).type)
            try:
                if kind is bool:
                    value = _launch.parse_bool(value)
                elif kind is int:
                    value = int(value)
                elif kind is float:
                    value = float(value)
            except (_launch.LaunchError, ValueError) as e:
                print(f"error: {e}")
                return

    setattr(target, name, value)
    print(f"{name} = {getattr(target, name)!r}")


def unset(name: str) -> None:
    """Reset a RUN_CTX option to its default value."""
    target = _resolve_target(name)
    if target is None:
        print(f"error: unknown option '{name}'")
        return

    default = _field_for(target, name).default
    setattr(target, name, default)
    print(f"{name} = {default!r}")


def run(script: str | None = None, *args: str) -> None:
    """Launch pydevd against `script`, e.g. run("script.py", "--foo", "bar").

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


def stop() -> None:
    """Kill the running pydevd session, if any."""
    if SESSION.process is None:
        print("error: no active session")
        return

    SESSION.process.child.kill()
    SESSION.process.child.wait()
    os.close(SESSION.process.master_fd)
    SESSION.process = None
    SESSION.reader_thread = None
    print("session stopped")


def connect() -> None:
    """Connect to the pydevd DAP server (not yet implemented)."""
    print("error: connect() is not implemented yet")
