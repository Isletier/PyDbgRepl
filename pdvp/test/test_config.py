"""Tests for the option table: coercion, command-line parsing, argv building.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all
of them (same convention as pdvp.dap.test.test_dap_client).

Run from the repo root with the venv active:

    python -m pdvp.test.test_config
"""
import dataclasses

from .. import launch
# Aliased: several tests use `config` as a local name for a Config instance.
from .. import config as _config
from ..config import CONFIG, Config, LaunchError, LogLevel, QtSupport, VmType


# The address pydevd is told to dial back into. Not a config field: run()
# binds its own socket and passes the resolved address down, so these are
# stand-ins for whatever the kernel handed us.
_HOST, _PORT = "127.0.0.1", 45678


def _argv(config: Config) -> list[str]:
    return launch.build_spawn_argv(config, _HOST, _PORT)


def _expect_error(fn, needle: str) -> None:
    try:
        fn()
    except LaunchError as e:
        assert needle in str(e), f"expected {needle!r} in {str(e)!r}"
        return
    raise AssertionError(f"expected LaunchError containing {needle!r}")


# ---- coercion ----

def test_coerce_scalars() -> None:
    assert _config.coerce(int, "42", "port") == 42
    assert _config.coerce(float, "0.5", "x") == 0.5
    assert _config.coerce(str, "hi", "x") == "hi"
    assert _config.coerce(bool, "true", "x") is True
    assert _config.coerce(bool, "0", "x") is False
    # Optional[str] unwraps to str rather than falling through as a Union.
    assert _config.coerce(str | None, "hi", "x") == "hi"


def test_coerce_enum() -> None:
    assert _config.coerce(LogLevel, "debug", "log_level") is LogLevel.DEBUG
    assert _config.coerce(VmType | None, "jython", "vm_type") is VmType.JYTHON


def test_coerce_rejects_bad_values() -> None:
    # The enum message must list the valid values -- that is the whole point
    # of handling enums generically instead of per-field reflection functions.
    _expect_error(lambda: _config.coerce(LogLevel, "verbos", "log_level"), "verbose")
    _expect_error(lambda: _config.coerce(int, "abc", "port"), "port")
    _expect_error(lambda: _config.coerce(bool, "maybe", "module"), "bool")


# ---- normalize ----

def test_normalize_accepts_convenience_strings() -> None:
    c = Config()
    c.log_level = "debug"
    c.qt_support = "none"
    _config.normalize(c)
    assert c.log_level is LogLevel.DEBUG
    assert c.qt_support is QtSupport.NONE


def test_normalize_reports_every_problem_at_once() -> None:
    c = Config()
    c.log_level = "nope"
    c.port = "abc"
    try:
        _config.normalize(c)
    except LaunchError as e:
        assert "log_level" in str(e) and "port" in str(e), str(e)
        return
    raise AssertionError("expected LaunchError")


def test_normalize_rejects_wrong_types() -> None:
    c = Config()
    c.interactive = 2
    _expect_error(lambda: _config.normalize(c), "interactive")

    c = Config()
    c.port = True  # bool is a subclass of int; must not slip through
    _expect_error(lambda: _config.normalize(c), "port")


def test_typo_raises_attribute_error() -> None:
    c = Config()
    try:
        c.prot = 5678
    except AttributeError:
        return
    raise AssertionError("slots=True should reject unknown attribute names")


# ---- parsing ----

def test_parse_basic_flags() -> None:
    c = Config()
    launch.parse_argv(c, ["--port", "5678", "--log-level", "debug", "--module"])
    assert c.port == 5678
    assert c.log_level is LogLevel.DEBUG
    assert c.module is True


def test_parse_joined_and_separate_spellings() -> None:
    for argv in (["--qt-support=pyqt5"], ["--qt-support", "pyqt5"]):
        c = Config()
        launch.parse_argv(c, argv)
        assert c.qt_support is QtSupport.PYQT5, argv


def test_parse_file_is_terminal() -> None:
    c = Config()
    launch.parse_argv(c, ["--port", "5678", "--file", "s.py", "--port", "9", "x"])
    # Everything after the script path belongs to the inferior, including
    # things that look like our own flags.
    assert c.port == 5678
    assert c.file == "s.py"
    assert c.args == ["--port", "9", "x"]


def test_parse_batch_inverts_interactive() -> None:
    c = Config()
    assert c.interactive is True
    launch.parse_argv(c, ["--batch"])
    assert c.interactive is False


def test_parse_rejects_bad_input() -> None:
    _expect_error(lambda: launch.parse_argv(Config(), ["--nope"]), "unknown flag")
    # Both modes are ours to pick, not the user's: we spawn pydevd in --client
    # mode so it dials back into a socket we bound first.
    _expect_error(lambda: launch.parse_argv(Config(), ["--client", "h"]), "not supported")
    _expect_error(lambda: launch.parse_argv(Config(), ["--server"]), "not supported")
    _expect_error(lambda: launch.parse_argv(Config(), ["--port"]), "expected parameter value")
    _expect_error(lambda: launch.parse_argv(Config(), ["--module=1"]), "takes no value")
    _expect_error(lambda: launch.parse_argv(Config(), ["--file"]), "expected parameter value")


# ---- emitting ----

def test_log_level_emits_pydevds_integer() -> None:
    # pydevd parses --log-level with int(); emitting the name kills the
    # inferior before it starts.
    c = Config()
    c.log_level = LogLevel.DEBUG
    argv = _argv(c)
    assert "--log-level" in argv
    assert argv[argv.index("--log-level") + 1] == "2", argv


def test_qt_support_emits_one_joined_token() -> None:
    # pydevd only accepts --qt-support=<mode>; two tokens make it reject the
    # mode as an unknown option.
    c = Config()
    c.qt_support = QtSupport.PYQT5
    argv = _argv(c)
    assert "--qt-support=pyqt5" in argv, argv
    assert "--qt-support" not in argv, argv


def test_defaults_are_not_emitted() -> None:
    argv = _argv(Config())
    for flag in ("--log-level", "--qt-support", "--module", "--preimport", "--file"):
        assert not any(a.startswith(flag) for a in argv), (flag, argv)


def test_file_is_last_flag_and_args_follow() -> None:
    c = Config()
    c.file = "s.py"
    c.args = ["--foo", "1"]
    argv = _argv(c)
    assert argv[-4:] == ["--file", "s.py", "--foo", "1"], argv


def test_every_spawn_field_reaches_the_argv() -> None:
    """Set each spawn-specced field to something non-default and check it
    actually shows up. This is the test that the old hand-written emitter
    could not have: it is what catches a field added to the table but
    forgotten in the emitter, which is how the --log-level and --qt-support
    bugs survived."""
    samples = {
        "ppid": 99,
        "preimport": "mymod",
        "log_file": "/tmp/x.log",
        "log_level": LogLevel.VERBOSE,
        "qt_support": QtSupport.PYSIDE2,
        "startup_msg": True,
        "module": True,
    }

    for f in dataclasses.fields(Config):
        spec = _config.spec_of(f)
        if spec is None or spec.spawn is None:
            continue
        assert f.name in samples, f"no sample value for new spawn field {f.name!r}"

        c = Config()
        setattr(c, f.name, samples[f.name])
        argv = _argv(c)
        assert any(a.split("=")[0] == spec.spawn for a in argv), (f.name, argv)


def test_parse_emit_round_trip() -> None:
    argv_in = [
        "--port", "5678", "--ppid", "99", "--preimport", "mymod",
        "--log-file", "/tmp/x.log", "--log-level", "verbose",
        "--qt-support=pyside2", "--module", "--print-in-debugger-startup",
        "--file", "s.py", "--foo",
    ]
    first = Config()
    launch.parse_argv(first, argv_in)

    # Re-parsing what we emit must land on the same configuration, so the two
    # halves of the table cannot drift apart.
    emitted = _argv(first)
    fixed = len(["python", "-m", "pydevd"]) + len(launch.OBLIGATORY_RUN_ARGUMENTS) + len(["--client", _HOST, "--port", str(_PORT)])
    second = Config()
    launch.parse_argv(second, emitted[fixed:])

    for f in dataclasses.fields(Config):
        # vm_type selects the executable rather than being emitted; port is
        # the address connect() dials, and the one in the spawn argv belongs
        # to the listener we bound, so neither round-trips through the table.
        if f.name in ("vm_type", "port"):
            continue
        assert getattr(first, f.name) == getattr(second, f.name), f.name


def test_pydevd_is_always_told_to_dial_back() -> None:
    """--client is what removes the startup race: pydevd connects to a socket
    we bound before spawning it, rather than binding one of its own."""
    argv = _argv(Config())
    assert argv[argv.index("--client") + 1] == _HOST, argv
    assert argv[argv.index("--port") + 1] == str(_PORT), argv
    assert "--server" not in argv, argv


def test_config_port_never_reaches_the_spawn_argv() -> None:
    """`config.port` is the remote dial target for connect(). A local run
    binds its own socket, so a stale value must not leak into the child."""
    c = Config()
    c.port = 9999
    argv = _argv(c)
    assert "9999" not in argv, argv


# ---- restoring defaults ----

def test_del_restores_one_field() -> None:
    config = Config()
    config.log_level = LogLevel.DEBUG
    config.ui = "readline"

    del config.log_level
    assert config.log_level is LogLevel.CRITICAL
    assert config.ui == "readline"  # neighbours untouched


def test_del_never_leaves_a_field_missing() -> None:
    """The point of __delattr__: with slots=True a real delete would make
    every later read of the field raise."""
    config = Config()
    for name in _config.field_table():
        delattr(config, name)
        getattr(config, name)  # raises AttributeError if the slot went empty


def test_del_reruns_the_default_factory() -> None:
    """A factory default is re-evaluated rather than shared, so `del` hands
    back a fresh object and not the one some other Config is holding."""
    config = Config()
    config.args = ["--foo"]

    del config.args
    assert config.args == []
    assert config.args is not Config().args


def test_del_unknown_name_raises() -> None:
    config = Config()
    for name in ("prot", "__doc__"):
        try:
            delattr(config, name)
        except AttributeError:
            continue
        raise AssertionError(f"expected AttributeError for del config.{name}")


def test_reset_restores_everything_in_place() -> None:
    config = Config()
    config.log_level = LogLevel.DEBUG
    config.ui = "readline"
    config.file = "script.py"
    config.args = ["--foo"]

    config.reset()

    assert config.log_level is LogLevel.CRITICAL
    assert config.ui == "auto"
    assert config.file is None  # --file goes too
    assert config.args == []


def test_reset_keeps_the_object_identity() -> None:
    """Everything that reads the configuration holds the one CONFIG object --
    `pdvp.CONFIG`, `pdvp.config.CONFIG`, and `config` at the prompt. reset()
    must therefore mutate in place rather than rebind."""
    import pdvp

    assert pdvp.CONFIG is CONFIG is _config.CONFIG

    CONFIG.ui = "readline"
    CONFIG.reset()

    assert pdvp.CONFIG is CONFIG
    assert CONFIG.ui == "auto"


TESTS = [
    test_coerce_scalars,
    test_coerce_enum,
    test_coerce_rejects_bad_values,
    test_normalize_accepts_convenience_strings,
    test_normalize_reports_every_problem_at_once,
    test_normalize_rejects_wrong_types,
    test_typo_raises_attribute_error,
    test_parse_basic_flags,
    test_parse_joined_and_separate_spellings,
    test_parse_file_is_terminal,
    test_parse_batch_inverts_interactive,
    test_parse_rejects_bad_input,
    test_log_level_emits_pydevds_integer,
    test_qt_support_emits_one_joined_token,
    test_defaults_are_not_emitted,
    test_file_is_last_flag_and_args_follow,
    test_every_spawn_field_reaches_the_argv,
    test_parse_emit_round_trip,
    test_pydevd_is_always_told_to_dial_back,
    test_config_port_never_reaches_the_spawn_argv,
    test_del_restores_one_field,
    test_del_never_leaves_a_field_missing,
    test_del_reruns_the_default_factory,
    test_del_unknown_name_raises,
    test_reset_restores_everything_in_place,
    test_reset_keeps_the_object_identity,
]


def main() -> None:
    failures = []
    for test in TESTS:
        print(f"{test.__name__} ... ", end="", flush=True)
        try:
            test()
        except Exception as e:
            print(f"FAIL: {e!r}")
            failures.append(test.__name__)
        else:
            print("ok")

    if failures:
        raise SystemExit(f"{len(failures)} test(s) failed: {', '.join(failures)}")
    print("all tests passed")


if __name__ == "__main__":
    main()
