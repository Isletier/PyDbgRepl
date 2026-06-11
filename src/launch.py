"""Build and spawn the pydevd subprocess (translated from prototype/zig/src/main.zig)."""
import dataclasses
import enum
import os
import pty
import subprocess
import types
import typing


class LaunchError(Exception):
    pass


class VmType(enum.Enum):
    PYTHON = "python"
    JYTHON = "jython"


class LogLevel(enum.Enum):
    CRITICAL = "critical"
    INFO = "info"
    DEBUG = "debug"
    VERBOSE = "verbose"


class QtSupport(enum.Enum):
    AUTO = "auto"
    PYQT5 = "pyqt5"
    PYQT4 = "pyqt4"
    PYSIDE = "pyside"
    PYSIDE2 = "pyside2"
    NONE = "none"


OBLIGATORY_RUN_ARGUMENTS = [
    "--server",
    "--json-dap-http",
    "--cmd-line",
    "--skip-notify-stdin",
]

SANITIZE_RUN_ARGUMENTS = [
    "--client",
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

ENV_SANITIZE = [
    "PYDEVD_DEBUG",
    "PYDEV_DEBUG",
    "PYCHARM_DEBUG",
    "PYDEVD_DEBUG_FILE",
    "PYDEVD_IPYTHON_COMPATIBLE_DEBUGGING",
    "PYDEVD_IPYTHON_CONTEXT",
]


@dataclasses.dataclass
class ArgsOptions:
    port: int = 0
    ppid: int = 0
    vm_type: VmType | None = None
    preimport: str | None = None
    log_file: str | None = None
    log_level: LogLevel = LogLevel.CRITICAL
    qt_support: QtSupport = QtSupport.AUTO
    startup_msg: bool = False
    module: bool = False
    file: str | None = None


@dataclasses.dataclass
class EnvOptions:
    PYDEVD_USE_SYS_MONITORING: bool | None = None
    PYDEVD_USE_CYTHON: bool | None = None
    PYDEVD_USE_FRAME_EVAL: bool | None = None
    PYDEVD_DEBUG_FILE: str | None = None
    PYDEVD_LOG_TIME: bool = True
    GEVENT_SUPPORT: bool = False
    GEVENT_SHOW_PAUSED_GREENLETS: bool = False
    GEVENT_SUPPORT_NOT_SET_MSG: str | None = None
    PYDEVD_LOAD_NATIVE_LIB: str | None = None
    PYDEVD_DISABLE_FILE_VALIDATION: bool = False
    PYDEVD_LOAD_VALUES_ASYNC: bool = False
    PYDEVD_APPLY_PATCHING_TO_HIDE_PYDEVD_THREADS: float = 0.5
    PYDEVD_SHOW_COMPILE_CYTHON_COMMAND_LINE: bool = False
    PYDEVD_WARN_EVALUATION_TIMEOUT: float = 3.0
    PYDEVD_THREAD_DUMP_ON_WARN_EVALUATION_TIMEOUT: bool = False
    PYDEVD_UNBLOCK_THREADS_TIMEOUT: float = -1.0
    PYDEVD_INTERRUPT_THREAD_TIMEOUT: float = -1.0
    PYDEVD_CONTAINER_INITIAL_EXPANDED_ITEMS: int = 100
    PYDEVD_CONTAINER_BUCKET_SIZE: int = 1000
    PYDEVD_CONTAINER_RANDOM_ACCESS_MAX_ITEMS: int = 500
    PYDEVD_CONTAINER_NUMPY_MAX_ITEMS: int = 500
    PYDEVD_WARN_SLOW_RESOLVE_TIMEOUT: float = 0.5
    PYDEVD_PANDAS_MAX_ROWS: int = 60
    PYDEVD_PANDAS_MAX_COLS: int = 10
    PYDEVD_PANDAS_MAX_COLWIDTH: int = 50


@dataclasses.dataclass
class RunContext:
    env: EnvOptions = dataclasses.field(default_factory=EnvOptions)
    args_opt: ArgsOptions = dataclasses.field(default_factory=ArgsOptions)
    args: list[str] = dataclasses.field(default_factory=list)


def parse_bool(value: str) -> bool:
    if value in ("1", "true"):
        return True
    if value in ("0", "false"):
        return False
    raise LaunchError(f"invalid bool value '{value}'")


def vm_type_reflection(value: str) -> VmType:
    try:
        return VmType(value)
    except ValueError:
        raise LaunchError(f"invalid vm_type value '{value}'")


def log_level_reflection(value: str) -> LogLevel:
    try:
        return LogLevel(value)
    except ValueError:
        raise LaunchError(f"invalid log level value '{value}'")


def qt_support_reflection(value: str) -> QtSupport:
    try:
        return QtSupport(value)
    except ValueError:
        raise LaunchError(f"invalid qt_support value '{value}'")


def serialize_launch_args(run_ctx: RunContext, args: list[str]) -> list[str]:
    flag = args[0]

    def value() -> str:
        if len(args) < 2:
            raise LaunchError(f"expected parameter value for {flag}")
        return args[1]

    if flag == "--file":
        run_ctx.args_opt.file = value()
        return args[2:]
    if flag == "--port":
        run_ctx.args_opt.port = int(value())
        return args[2:]
    if flag == "--ppid":
        run_ctx.args_opt.ppid = int(value())
        return args[2:]
    if flag == "--vm_type":
        run_ctx.args_opt.vm_type = vm_type_reflection(value())
        return args[2:]
    if flag == "--preimport":
        run_ctx.args_opt.preimport = value()
        return args[2:]
    if flag == "--log_file":
        run_ctx.args_opt.log_file = value()
        return args[2:]
    if flag == "--log_level":
        run_ctx.args_opt.log_level = log_level_reflection(value())
        return args[2:]
    if flag == "--qt_support":
        run_ctx.args_opt.qt_support = qt_support_reflection(value())
        return args[2:]
    if flag == "--print-in-debugger-startup":
        run_ctx.args_opt.startup_msg = True
        return args[1:]
    if flag == "--module":
        run_ctx.args_opt.module = True
        return args[1:]
    return args[1:]


def process_args(run_ctx: RunContext, argv: list[str]) -> None:
    args = argv
    while args:
        arg = args[0]
        if arg in SANITIZE_RUN_ARGUMENTS:
            raise LaunchError(f"pydevd original flag {arg} is not supported")
        if arg in OBLIGATORY_RUN_ARGUMENTS:
            raise LaunchError(f"pydevd original flag {arg} is enabled by default")

        args = serialize_launch_args(run_ctx, args)

        if arg == "--file":
            run_ctx.args = list(args)
            return


def _unwrap_optional(annotation: type) -> type:
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def process_envs(run_ctx: RunContext, env: dict[str, str]) -> None:
    for f in dataclasses.fields(EnvOptions):
        if f.name not in env:
            continue
        raw = env[f.name]
        kind = _unwrap_optional(f.type)
        try:
            if kind is bool:
                parsed: object = parse_bool(raw)
            elif kind is float:
                parsed = float(raw)
            elif kind is int:
                parsed = int(raw)
            else:
                parsed = raw
        except (LaunchError, ValueError):
            print(f"error: invalid value '{raw}' for {f.name}")
            continue
        setattr(run_ctx.env, f.name, parsed)


def build_spawn_argv(run_ctx: RunContext) -> list[str]:
    vm_type = run_ctx.args_opt.vm_type or VmType.PYTHON
    argv = [vm_type.value, "-m", "pydevd"]
    argv.extend(OBLIGATORY_RUN_ARGUMENTS)

    argv += ["--port", str(run_ctx.args_opt.port)]
    argv += ["--ppid", str(run_ctx.args_opt.ppid)]

    if run_ctx.args_opt.preimport is not None:
        argv += ["--preimport", run_ctx.args_opt.preimport]
    if run_ctx.args_opt.log_file is not None:
        argv += ["--log-file", run_ctx.args_opt.log_file]
    if run_ctx.args_opt.log_level != LogLevel.CRITICAL:
        argv += ["--log-level", run_ctx.args_opt.log_level.value]
    if run_ctx.args_opt.qt_support != QtSupport.AUTO:
        argv += ["--qt-support", run_ctx.args_opt.qt_support.value]
    if run_ctx.args_opt.startup_msg:
        argv.append("--print-in-debugger-startup")
    if run_ctx.args_opt.module:
        argv.append("--module")
    if run_ctx.args_opt.file is not None:
        argv += ["--file", run_ctx.args_opt.file]

    argv.extend(run_ctx.args)
    return argv


def build_spawn_env(run_ctx: RunContext, source_env: dict[str, str]) -> dict[str, str]:
    env = dict(source_env)
    for key in ENV_SANITIZE:
        env.pop(key, None)

    def set_bool(key: str, val: bool) -> None:
        env[key] = "1" if val else "0"

    if run_ctx.env.PYDEVD_USE_SYS_MONITORING is not None:
        set_bool("PYDEVD_USE_SYS_MONITORING", run_ctx.env.PYDEVD_USE_SYS_MONITORING)
    if run_ctx.env.PYDEVD_USE_CYTHON is not None:
        set_bool("PYDEVD_USE_CYTHON", run_ctx.env.PYDEVD_USE_CYTHON)
    if run_ctx.env.PYDEVD_USE_FRAME_EVAL is not None:
        set_bool("PYDEVD_USE_FRAME_EVAL", run_ctx.env.PYDEVD_USE_FRAME_EVAL)
    if run_ctx.env.PYDEVD_DEBUG_FILE is not None:
        env["PYDEVD_DEBUG_FILE"] = run_ctx.env.PYDEVD_DEBUG_FILE
    if run_ctx.env.GEVENT_SUPPORT_NOT_SET_MSG is not None:
        env["GEVENT_SUPPORT_NOT_SET_MSG"] = run_ctx.env.GEVENT_SUPPORT_NOT_SET_MSG
    if run_ctx.env.PYDEVD_LOAD_NATIVE_LIB is not None:
        env["PYDEVD_LOAD_NATIVE_LIB"] = run_ctx.env.PYDEVD_LOAD_NATIVE_LIB

    set_bool("PYDEVD_LOG_TIME", run_ctx.env.PYDEVD_LOG_TIME)
    set_bool("GEVENT_SUPPORT", run_ctx.env.GEVENT_SUPPORT)
    set_bool("GEVENT_SHOW_PAUSED_GREENLETS", run_ctx.env.GEVENT_SHOW_PAUSED_GREENLETS)
    set_bool("PYDEVD_DISABLE_FILE_VALIDATION", run_ctx.env.PYDEVD_DISABLE_FILE_VALIDATION)
    set_bool("PYDEVD_LOAD_VALUES_ASYNC", run_ctx.env.PYDEVD_LOAD_VALUES_ASYNC)
    set_bool("PYDEVD_SHOW_COMPILE_CYTHON_COMMAND_LINE", run_ctx.env.PYDEVD_SHOW_COMPILE_CYTHON_COMMAND_LINE)
    set_bool("PYDEVD_THREAD_DUMP_ON_WARN_EVALUATION_TIMEOUT", run_ctx.env.PYDEVD_THREAD_DUMP_ON_WARN_EVALUATION_TIMEOUT)

    env["PYDEVD_APPLY_PATCHING_TO_HIDE_PYDEVD_THREADS"] = str(run_ctx.env.PYDEVD_APPLY_PATCHING_TO_HIDE_PYDEVD_THREADS)
    env["PYDEVD_WARN_EVALUATION_TIMEOUT"] = str(run_ctx.env.PYDEVD_WARN_EVALUATION_TIMEOUT)
    env["PYDEVD_UNBLOCK_THREADS_TIMEOUT"] = str(run_ctx.env.PYDEVD_UNBLOCK_THREADS_TIMEOUT)
    env["PYDEVD_INTERRUPT_THREAD_TIMEOUT"] = str(run_ctx.env.PYDEVD_INTERRUPT_THREAD_TIMEOUT)
    env["PYDEVD_WARN_SLOW_RESOLVE_TIMEOUT"] = str(run_ctx.env.PYDEVD_WARN_SLOW_RESOLVE_TIMEOUT)
    env["PYDEVD_CONTAINER_INITIAL_EXPANDED_ITEMS"] = str(run_ctx.env.PYDEVD_CONTAINER_INITIAL_EXPANDED_ITEMS)
    env["PYDEVD_CONTAINER_BUCKET_SIZE"] = str(run_ctx.env.PYDEVD_CONTAINER_BUCKET_SIZE)
    env["PYDEVD_CONTAINER_RANDOM_ACCESS_MAX_ITEMS"] = str(run_ctx.env.PYDEVD_CONTAINER_RANDOM_ACCESS_MAX_ITEMS)
    env["PYDEVD_CONTAINER_NUMPY_MAX_ITEMS"] = str(run_ctx.env.PYDEVD_CONTAINER_NUMPY_MAX_ITEMS)
    env["PYDEVD_PANDAS_MAX_ROWS"] = str(run_ctx.env.PYDEVD_PANDAS_MAX_ROWS)
    env["PYDEVD_PANDAS_MAX_COLS"] = str(run_ctx.env.PYDEVD_PANDAS_MAX_COLS)
    env["PYDEVD_PANDAS_MAX_COLWIDTH"] = str(run_ctx.env.PYDEVD_PANDAS_MAX_COLWIDTH)

    return env


@dataclasses.dataclass
class LaunchedProcess:
    child: subprocess.Popen
    master_fd: int


def spawn_pydevd(run_ctx: RunContext) -> LaunchedProcess:
    spawn_argv = build_spawn_argv(run_ctx)
    spawn_env = build_spawn_env(run_ctx, dict(os.environ))

    master_fd, slave_fd = pty.openpty()
    try:
        child = subprocess.Popen(
            spawn_argv,
            env=spawn_env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
    finally:
        os.close(slave_fd)
    return LaunchedProcess(child=child, master_fd=master_fd)
