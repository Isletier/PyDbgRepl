"""The option table: one flat dataclass the user assigns to directly.

    pdvp.CONFIG.port = 5678
    pdvp.CONFIG.log_level = "debug"     # normalized to LogLevel at run()
    del pdvp.CONFIG.log_level           # back to its default
    pdvp.CONFIG.reset()                 # all of them back

CONFIG (bottom of this module) is that one live instance; `pdvp.CONFIG`
re-exports it, and start_eval() injects it into __main__ as `config`, which is
what it is called at the prompt. It is *not* named `config` here, because that
is this module.

Every option is described here and nowhere else. A field optionally carries:

  cli=    the launch command-line spelling that sets it. This is a deliberately
          curated subset -- the command line covers what matters at startup,
          and everything else is set in repl.py, which is the configuration
          file. A new field is *not* on the command line unless it says so.
  spawn=  the pydevd argv spelling it is emitted as. Fields that are ours
          rather than pydevd's (pty, ui, ...) carry no spawn spelling at all.

launch.parse_argv() and launch.build_spawn_argv() are generic walks over these
specs, so the two can never disagree about a flag's name, arity or spelling.
"""

import dataclasses
import enum
import functools
import os
import tempfile
import types
import typing
from typing import Any, Callable


class LaunchError(Exception):
    """A configuration value we can't use: wrong type, unknown value, or two
    settings that contradict each other."""


# ---- value domains ----

class VmType(enum.Enum):
    PYTHON = "python"
    JYTHON = "jython"


class LogLevel(enum.Enum):
    CRITICAL = "critical"
    INFO = "info"
    DEBUG = "debug"
    VERBOSE = "verbose"

    @property
    def level(self) -> int:
        """pydevd's --log-level is numeric (0=critical .. 3=verbose); the
        names are ours. Emitting the name gets `int()` in pydevd's arg
        handler and kills the inferior before it starts."""
        return _LOG_LEVELS[self]

    @classmethod
    def _missing_(cls, value):
        """Accept pydevd's own numeric spelling as well, so `--log-level 2`
        and `--log-level debug` both work -- doc/options.txt documents the
        numeric one, and it is what a pydevd user already knows."""
        try:
            wanted = int(value)
        except (TypeError, ValueError):
            return None
        for member, level in _LOG_LEVELS.items():
            if level == wanted:
                return member
        return None


_LOG_LEVELS = {
    LogLevel.CRITICAL: 0,
    LogLevel.INFO: 1,
    LogLevel.DEBUG: 2,
    LogLevel.VERBOSE: 3,
}


class QtSupport(enum.Enum):
    AUTO = "auto"
    PYQT5 = "pyqt5"
    PYQT4 = "pyqt4"
    PYSIDE = "pyside"
    PYSIDE2 = "pyside2"
    NONE = "none"


# ---- per-field launch behaviour ----

class Emit(enum.Enum):
    """When a field makes it into pydevd's argv."""

    ALWAYS = "always"
    IF_SET = "if_set"          # value is not None
    IF_CHANGED = "if_changed"  # value differs from the field's default


class Style(enum.Enum):
    """How a field is spelled on a command line."""

    SEPARATE = "separate"  # --flag value
    JOINED = "joined"      # --flag=value
    FLAG = "flag"          # --flag, no value


@dataclasses.dataclass(frozen=True)
class OptSpec:
    cli: str | None = None
    spawn: str | None = None
    emit: Emit = Emit.IF_SET
    style: Style = Style.SEPARATE
    # FLAG only: the flag's presence sets the field False rather than True
    # (--batch, which turns `interactive` off).
    invert: bool = False
    # value -> the string pydevd wants, when it differs from str(value).
    wire: Callable[[Any], str] | None = None


def opt(**kwargs) -> dict:
    """Field metadata: see the module docstring for `cli=` vs `spawn=`."""
    return {"opt": OptSpec(**kwargs)}


def spec_of(field: dataclasses.Field) -> OptSpec | None:
    return field.metadata.get("opt")


# ---- the options ----

@dataclasses.dataclass(slots=True)
class Config:
    """Everything the user can tune. `slots=True` is deliberate: it turns a
    typo (`config.prot = 5678`) into an AttributeError instead of a silently
    ignored assignment, which is the one thing plain attribute access would
    otherwise lose against the old set()/get() pair."""

    # -- pydevd launch --
    # The port connect() dials on a remote pydevd. Not used by run(): that
    # binds its own socket and lets the kernel pick, so there is no port to
    # guess and no collision to handle. Emitted by neither -- run() passes the
    # resolved port to build_spawn_argv() explicitly.
    port: int = dataclasses.field(
        default=0,
        metadata=opt(cli="--port"))
    ppid: int = dataclasses.field(
        default=0,
        metadata=opt(cli="--ppid", spawn="--ppid", emit=Emit.ALWAYS))
    # pydevd really does spell this one with an underscore.
    vm_type: VmType | None = dataclasses.field(
        default=None,
        metadata=opt(cli="--vm_type"))
    preimport: str | None = dataclasses.field(
        default=None,
        metadata=opt(cli="--preimport", spawn="--preimport"))
    log_file: str | None = dataclasses.field(
        default=None,
        metadata=opt(cli="--log-file", spawn="--log-file"))
    log_level: LogLevel = dataclasses.field(
        default=LogLevel.CRITICAL,
        metadata=opt(cli="--log-level", spawn="--log-level",
                     emit=Emit.IF_CHANGED, wire=lambda v: str(v.level)))
    # pydevd only accepts the joined `--qt-support=<mode>` spelling; passing
    # it as two tokens makes pydevd reject the mode as an unknown option.
    qt_support: QtSupport = dataclasses.field(
        default=QtSupport.AUTO,
        metadata=opt(cli="--qt-support", spawn="--qt-support",
                     emit=Emit.IF_CHANGED, style=Style.JOINED))
    startup_msg: bool = dataclasses.field(
        default=False,
        metadata=opt(cli="--print-in-debugger-startup",
                     spawn="--print-in-debugger-startup", style=Style.FLAG))
    module: bool = dataclasses.field(
        default=False,
        metadata=opt(cli="--module", spawn="--module", style=Style.FLAG))

    # -- ours, not pydevd's: on the command line, never emitted --
    # External tty device (e.g. "/dev/pts/7") to redirect the inferior's
    # stdin/stdout/stderr to, gdb `tty`-style. None: default owned-PTY-pair
    # passthrough. Mutually exclusive with stdin/stdout/stderr below.
    # Consulted by run(); a no-op for connect(). See doc/io_model.md.
    pty: str | None = dataclasses.field(
        default=None,
        metadata=opt(cli="--pty"))
    # If True (default), start_eval() drops into an interactive prompt once
    # the script body finishes. If False (--batch), the process just exits --
    # for unattended automation scenarios. See doc/scenario_mode.md.
    interactive: bool = dataclasses.field(
        default=True,
        metadata=opt(cli="--batch", style=Style.FLAG, invert=True))

    # -- repl.py-only knobs: neither a flag nor emitted --
    # Per-stream file redirection for the inferior, gdb-style. Each defaults
    # to the owned-PTY-pair slave when unset. `stderr` additionally accepts
    # the sentinel "&1" (shell `2>&1`-style: alias stdout's resolved fd).
    # Mutually exclusive with `pty` above. See doc/io_model.md.
    stdin: str | None = None
    stdout: str | None = None
    stderr: str | None = None

    # The host connect() dials. Like `port`, remote-only: run() always listens
    # on loopback (dap.LISTEN_HOST), which is not negotiable -- anything that
    # can reach that socket can drive the debugger.
    dap_host: str = "127.0.0.1"
    # REPL frontend: "auto" (ptpython if installed, else plain readline),
    # "ptpython", or "readline".
    ui: str = "auto"
    # Tab-completion mode (ptpython only): "debugger" (command-aware) or
    # "classical" (ptpython's normal jedi-based completion). Read live on
    # every completion request -- no restart needed.
    completion: str = "debugger"
    source_catalog: str = os.path.realpath(tempfile.gettempdir())

    # The script and its argv. `file` is not in the spec table above because
    # --file is positional-ish on both sides: it terminates our command line,
    # and it must be the last thing in pydevd's. Both ends handle it
    # explicitly -- see launch.parse_argv()/build_spawn_argv().
    file: str | None = None
    args: list[str] = dataclasses.field(default_factory=list)

    # -- establishing the DAP connection --
    # The local case is not configurable: run() binds its own socket, lets the
    # kernel pick the port, and pydevd dials back into it (--client), so there
    # is no address, no port and no retry to tune. Only the wait has a number,
    # because a spawn that fails must not hang forever -- pydevd exits within
    # milliseconds when it cannot reach us, and the connect itself takes ~90ms.
    accept_timeout:             float = 1.0
    # These two are the remote case only: connect() dials a pydevd somebody
    # else started, where the address is unknown to us and a dropped SYN is a
    # real possibility.
    connection_timeout:         float | None = None
    connection_retry:           int = dataclasses.field(default=1)

    def __delattr__(self, name: str) -> None:
        """`del config.port` puts one field back to its default -- the
        counterpart to plain assignment, and the same gesture as
        `del os.environ["X"]`.

        Nothing is really deleted: with slots=True a real delete leaves the
        field genuinely missing, and every later read of it would raise. We
        re-apply the declared default instead, so "every field always has a
        value" holds for every reader.

        The default is whatever the field declares, and a default_factory is
        re-run rather than shared -- `del config.args` hands back a fresh
        list, not the one some other Config is holding.
        """
        f = field_table().get(name)
        if f is None:
            # Not a field: object's own __delattr__ raises the normal
            # AttributeError, so a typo fails the same either side of the `=`.
            # Explicit rather than super(): dataclass(slots=True) can't add
            # slots to an existing class, so it builds a replacement one, and
            # the __class__ cell zero-arg super() reads still points at the
            # original -- it would raise TypeError here.
            object.__delattr__(self, name)
            return

        default = default_of(f)
        if default is dataclasses.MISSING:
            raise LaunchError(f"{name} declares no default to restore")
        setattr(self, name, default)

    def reset(self) -> None:
        """Restore every field at once, including `file` and `args` -- the
        script named by --file at startup goes too.

        Delegates to __delattr__ so "default" has exactly one definition, and
        mutates in place, so `pdvp.CONFIG` and every module that imported
        CONFIG keep pointing at this same object.
        """
        for name in field_table():
            delattr(self, name)


@functools.cache
def field_table() -> dict[str, dataclasses.Field]:
    """Config's fields by name. Cached: the set is fixed at class creation."""
    return {f.name: f for f in dataclasses.fields(Config)}


@functools.cache
def hints() -> dict[str, Any]:
    """Config's resolved annotations. Via get_type_hints() rather than
    `Field.type`, which is a bare string under `from __future__ import
    annotations` and under PEP 649's lazy annotations."""
    return typing.get_type_hints(Config)


def unwrap_optional(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


# ---- strings in, real values out ----

def parse_bool(value: str) -> bool:
    if value.lower() in ("1", "true", "yes"):
        return True
    if value.lower() in ("0", "false", "no"):
        return False
    raise LaunchError(f"invalid bool value '{value}'")


def coerce(kind: Any, raw: str, name: str) -> Any:
    """Turn a command-line string into `kind`.

    The only string-coercion site left in the codebase: everything else
    assigns real Python values, so this runs on the command-line boundary
    and on whatever the user typed as a convenience string at the prompt.
    """
    kind = unwrap_optional(kind)

    if isinstance(kind, type) and issubclass(kind, enum.Enum):
        try:
            return kind(raw)
        except ValueError:
            valid = ", ".join(m.value for m in kind)
            raise LaunchError(f"invalid value '{raw}' for {name} (expected one of: {valid})")
    if kind is bool:
        return parse_bool(raw)
    if kind is int:
        try:
            return int(raw)
        except ValueError:
            raise LaunchError(f"invalid int value '{raw}' for {name}")
    if kind is float:
        try:
            return float(raw)
        except ValueError:
            raise LaunchError(f"invalid float value '{raw}' for {name}")
    return raw


def normalize(config: Config) -> None:
    """Bring every field to its declared type, in place.

    Assignment is plain Python, so `config.log_level = "debug"` is both legal
    and the natural thing to type at a prompt -- it becomes a LogLevel here.
    There is no separate validate() step: this runs from run(), the moment
    the configuration stops being a half-edited draft and becomes a claim
    about a process we are about to start. Every bad field is reported
    together, rather than one per attempt.
    """
    problems: list[str] = []
    resolved = hints()

    for f in dataclasses.fields(config):
        value = getattr(config, f.name)
        if value is None:
            continue

        kind = unwrap_optional(resolved[f.name])
        if typing.get_origin(kind) is not None or not isinstance(kind, type):
            continue  # list[str] and friends: nothing worth checking

        # bool is a subclass of int, so `port = True` must not count as an int.
        if isinstance(value, kind) and not (kind is not bool and isinstance(value, bool)):
            continue

        # A convenience value the user typed rather than the declared type:
        # "debug" or 2 for a LogLevel, "5678" for a port. Enums also accept
        # their own alternate spellings, so they always go through coerce().
        if issubclass(kind, enum.Enum) or isinstance(value, str):
            try:
                setattr(config, f.name, coerce(kind, str(value), f.name))
            except LaunchError as e:
                problems.append(str(e))
            continue

        problems.append(f"invalid value {value!r} for {f.name} (expected {kind.__name__})")

    if problems:
        raise LaunchError("; ".join(problems))


def default_of(f: dataclasses.Field) -> Any:
    if f.default_factory is not dataclasses.MISSING:
        return f.default_factory()
    return f.default

CONFIG = Config()
