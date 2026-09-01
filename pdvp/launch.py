"""Build and spawn the pydevd subprocess (translated from prototype/zig/src/main.zig)."""
import dataclasses
import os
import pty
import subprocess

# Aliased: spawn_pydevd()/build_spawn_argv() take a parameter called `config`,
# which would otherwise shadow the module.
from pdvp import config as _config
from pdvp.config import (  # re-exported: callers say launch.LaunchError, launch.VmType, ...
    Config,
    LaunchError,
    LogLevel,
    QtSupport,
    VmType,
    normalize,
)

OBLIGATORY_RUN_ARGUMENTS = [
    "--json-dap-http",
    "--skip-notify-stdin",
]

SANITIZE_RUN_ARGUMENTS = [
    # We spawn pydevd in --client mode so that it dials back into a socket we
    # bound before it existed: that removes the startup race, and the port
    # comes from the kernel instead of a guess. --server is the mode we
    # replaced, and --client is ours to set, not the user's.
    "--server",
    "--client",
    "--cmd-line",
    "--access-token",
    "--debug-mode",
    "--multiproc",
    "--multiprocess",
    "--save-signatures",
    "--save-threading",
    "--save-asyncio",
    "--json-dap",
    "--protocol-quoted-line",
    "--protocol-http",
    "--DEBUG",
]

# Inherited pydevd/IDE debug settings we drop at startup so that running
# pydev-repl from inside PyCharm (or under another pydevd) doesn't silently
# adopt that debugger's configuration. Scrubbed once, in-place, by
# process_args_envs(); anything the user assigns to os.environ afterwards
# wins, and the inferior simply inherits our environment. See doc/options.txt
# for what each one does.
ENV_SANITIZE = [
    "PYDEVD_DEBUG",
    "PYDEV_DEBUG",
    "PYCHARM_DEBUG",
    "PYDEVD_DEBUG_FILE",
    "PYDEVD_IPYTHON_COMPATIBLE_DEBUGGING",
    "PYDEVD_IPYTHON_CONTEXT",
]


def scrub_env() -> None:
    """Drop the inherited debug settings listed in ENV_SANITIZE."""
    for key in ENV_SANITIZE:
        os.environ.pop(key, None)


# ---- command line in ----

def _cli_table() -> dict[str, dataclasses.Field]:
    """{command-line spelling: field}, for every field that opted in."""
    table = {}
    for f in dataclasses.fields(Config):
        spec = _config.spec_of(f)
        if spec is not None and spec.cli is not None:
            table[spec.cli] = f
    return table


def parse_argv(config: Config, argv: list[str]) -> None:
    """Populate `config` from our launch command line."""
    args = list(argv)

    # --file terminates our command line: pydevd's own rule is that the
    # script path is last and everything after it belongs to the inferior.
    if "--file" in args:
        i = args.index("--file")
        if i + 1 >= len(args):
            raise LaunchError("expected parameter value for --file")
        config.file = args[i + 1]
        config.args = args[i + 2:]
        args = args[:i]

    table = _cli_table()
    resolved = _config.hints()

    while args:
        flag, _, inline = args.pop(0).partition("=")

        f = table.get(flag)
        if f is None:
            if flag in SANITIZE_RUN_ARGUMENTS:
                raise LaunchError(f"pydevd original flag {flag} is not supported")
            if flag in OBLIGATORY_RUN_ARGUMENTS:
                raise LaunchError(f"pydevd original flag {flag} is enabled by default")
            raise LaunchError(f"unknown flag: {flag}")

        spec = _config.spec_of(f)

        if spec.style is _config.Style.FLAG:
            if inline:
                raise LaunchError(f"{flag} takes no value")
            setattr(config, f.name, not spec.invert)
            continue

        if not inline:
            if not args:
                raise LaunchError(f"expected parameter value for {flag}")
            inline = args.pop(0)

        setattr(config, f.name, _config.coerce(resolved[f.name], inline, f.name))


# ---- command line out ----

def _emit(spec: _config.OptSpec, value, default) -> list[str]:
    if spec.style is _config.Style.FLAG:
        return [spec.spawn] if value else []

    if spec.emit is _config.Emit.IF_SET and value is None:
        return []
    if spec.emit is _config.Emit.IF_CHANGED and value == default:
        return []

    wire = spec.wire(value) if spec.wire is not None else str(getattr(value, "value", value))

    if spec.style is _config.Style.JOINED:
        return [f"{spec.spawn}={wire}"]
    return [spec.spawn, wire]


def build_spawn_argv(config: Config, host: str, port: int) -> list[str]:
    """pydevd's argv, told to dial back into `host:port`.

    The address is a parameter rather than a config field on purpose: it is
    the address of a socket the caller has already bound, so it cannot exist
    before the listener does. Taking it here makes that ordering a matter of
    the signature instead of a comment -- there is no port to pass until
    dap.listen() has produced one.
    """
    # vm_type is pydevd's own --vm_type wire concept, never emitted to argv
    # (config.py); the executable we actually exec is python_executable,
    # which only coincides with vm_type's "python" spelling for the common
    # case and is unrelated to it otherwise -- reusing vm_type.value here for
    # both was the bug: a bare "python" isn't on PATH on every system this
    # runs on (this one included), where python_executable's default,
    # sys.executable, always resolves.
    vm_type = config.vm_type or VmType.PYTHON
    executable = config.python_executable if vm_type is VmType.PYTHON else vm_type.value
    argv = [executable, "-m", "pydevd"]
    argv.extend(OBLIGATORY_RUN_ARGUMENTS)
    argv += ["--client", host, "--port", str(port)]

    for f in dataclasses.fields(config):
        spec = _config.spec_of(f)
        if spec is None or spec.spawn is None:
            continue
        argv += _emit(spec, getattr(config, f.name), _config.default_of(f))

    # Not in the spec table: pydevd requires --file to be the final flag, with
    # the inferior's own argv after it.
    if config.file is not None:
        argv += ["--file", config.file]
    argv.extend(config.args)

    return argv


# ---- spawning ----

@dataclasses.dataclass
class LaunchedProcess:
    child: subprocess.Popen
    # None when `pty` was given, or when stdin/stdout/stderr were all
    # redirected to files: nothing on our side to stream from/to (see
    # doc/io_model.md).
    master_fd: int | None
    # True if the inferior's stdin is the owned-PTY-pair slave (i.e. not
    # redirected to a file and not `pty`). Gates _StdinPassthrough.
    stdin_is_pty: bool = False


def spawn_pydevd(config: Config, host: str, port: int) -> LaunchedProcess:
    """Start pydevd against `config`; it dials back into `host:port`.

    Returns as soon as the child exists -- there is nothing to wait for here.
    The caller already holds the listening socket, so accept() is the wait,
    and pydevd blocks before running the target until that connection is
    established (pydevd.py: `debugger.connect(...)` precedes `debugger.run`).

    The child inherits our os.environ.
    """
    spawn_argv = build_spawn_argv(config, host, port)

    redirected = config.stdin is not None or config.stdout is not None or config.stderr is not None

    if config.pty is not None:
        if redirected:
            raise LaunchError("pty conflicts with stdin/stdout/stderr redirection -- unset one")
        fd = os.open(config.pty, os.O_RDWR)
        try:
            child = subprocess.Popen(
                spawn_argv,
                stdin=fd,
                stdout=fd,
                stderr=fd,
                start_new_session=True,
            )
        finally:
            os.close(fd)
        return LaunchedProcess(child=child, master_fd=None)

    master_fd: int | None = None
    slave_fd: int | None = None
    if config.stdin is None or config.stdout is None or config.stderr is None:
        master_fd, slave_fd = pty.openpty()

    opened_fds = []

    if config.stdin is not None:
        stdin_fd = os.open(config.stdin, os.O_RDONLY)
        opened_fds.append(stdin_fd)
    else:
        stdin_fd = slave_fd

    if config.stdout is not None:
        stdout_fd = os.open(config.stdout, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        opened_fds.append(stdout_fd)
    else:
        stdout_fd = slave_fd

    if config.stderr == "&1":
        stderr_fd = os.dup(stdout_fd)
        opened_fds.append(stderr_fd)
    elif config.stderr is not None:
        stderr_fd = os.open(config.stderr, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        opened_fds.append(stderr_fd)
    else:
        stderr_fd = slave_fd

    try:
        child = subprocess.Popen(
            spawn_argv,
            stdin=stdin_fd,
            stdout=stdout_fd,
            stderr=stderr_fd,
            start_new_session=True,
        )
    finally:
        if slave_fd is not None:
            os.close(slave_fd)
        for fd in opened_fds:
            os.close(fd)

    process = LaunchedProcess(child=child, master_fd=master_fd, stdin_is_pty=config.stdin is None)

    return process
