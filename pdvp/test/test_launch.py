"""Tests for pdvp/launch.py: the vm_type/python_executable split in
build_spawn_argv(), scrub_env(), and spawn_pydevd()'s fd/redirection wiring.

The fd tests never spawn a real pydevd: subprocess.Popen is monkeypatched to
a recorder that captures which fds it was handed (and what they point at, via
/proc/self/fd) without actually exec-ing anything. That is enough to check
spawn_pydevd()'s own bookkeeping -- pty-pair vs redirected-file selection,
the &1 dup, and that every fd we open on our side gets closed again -- without
depending on pydevd being importable or on a real DAP handshake completing.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all
of them (same convention as pdvp.test.test_config).

Run from the repo root with the venv active:

    python -m pdvp.test.test_launch
"""
import contextlib
import os
import pty
import sys
import tempfile

from .. import launch
from ..config import Config, VmType

# Stand-ins for a bound listener's address: build_spawn_argv() only ever
# writes these into argv, never dials them, so any values do.
_HOST, _PORT = "127.0.0.1", 45678


def _argv(config: Config) -> list[str]:
    return launch.build_spawn_argv(config, _HOST, _PORT)


def _open_fd_count() -> int:
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


def _fd_target(fd: int | None) -> str | None:
    """What `fd` points at, read while it is still open in our process."""
    if fd is None:
        return None
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


class _FakePopen:
    """Records the fds/argv it was given instead of spawning anything.

    spawn_pydevd() closes its own copies of the redirection fds (everything
    but master_fd) in a `finally` right after the Popen call returns, so
    whatever we want to know about them has to be captured here, during
    construction -- not after spawn_pydevd() hands control back.
    """

    last: "_FakePopen | None" = None

    def __init__(self, argv, stdin=None, stdout=None, stderr=None, start_new_session=None):
        self.argv = argv
        self.stdin_fd = stdin
        self.stdout_fd = stdout
        self.stderr_fd = stderr
        self.stdin_target = _fd_target(stdin)
        self.stdout_target = _fd_target(stdout)
        self.stderr_target = _fd_target(stderr)
        self.pid = 424242
        self.returncode = None
        _FakePopen.last = self

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@contextlib.contextmanager
def _fake_popen():
    original = launch.subprocess.Popen
    launch.subprocess.Popen = _FakePopen
    try:
        yield
    finally:
        launch.subprocess.Popen = original


def _real_path(p: str) -> str:
    return os.path.realpath(p)


# ---- vm_type / python_executable ----

def test_build_spawn_argv_python_default_uses_sys_executable() -> None:
    for vm_type in (None, VmType.PYTHON):
        c = Config()
        c.vm_type = vm_type
        argv = _argv(c)
        assert argv[0] == sys.executable, (vm_type, argv)
        assert argv[0] == c.python_executable, (vm_type, argv)


def test_build_spawn_argv_respects_python_executable_override() -> None:
    c = Config()
    c.python_executable = "/custom/interpreter"
    argv = _argv(c)
    assert argv[0] == "/custom/interpreter", argv


def test_build_spawn_argv_jython_ignores_python_executable() -> None:
    c = Config()
    c.vm_type = VmType.JYTHON
    c.python_executable = "/should/not/be/used"
    argv = _argv(c)
    assert argv[0] == "jython" == VmType.JYTHON.value, argv


def test_vm_type_never_reaches_the_spawn_argv() -> None:
    for vm_type in (None, VmType.PYTHON, VmType.JYTHON):
        c = Config()
        c.vm_type = vm_type
        argv = _argv(c)
        assert not any(a.startswith("--vm_type") or a.startswith("--vm-type") for a in argv), \
            (vm_type, argv)


def test_python_executable_default_factory_reads_env() -> None:
    saved = os.environ.get("PYTHON_EXECUTABLE")
    try:
        os.environ.pop("PYTHON_EXECUTABLE", None)
        assert Config().python_executable == sys.executable

        os.environ["PYTHON_EXECUTABLE"] = "/tmp/fake-python-for-test"
        assert Config().python_executable == "/tmp/fake-python-for-test"
    finally:
        if saved is None:
            os.environ.pop("PYTHON_EXECUTABLE", None)
        else:
            os.environ["PYTHON_EXECUTABLE"] = saved


def test_python_cli_flag_sets_python_executable() -> None:
    c = Config()
    launch.parse_argv(c, ["--python", "/some/interpreter"])
    assert c.python_executable == "/some/interpreter"


# ---- scrub_env ----

def test_scrub_env_drops_only_the_sanitize_list() -> None:
    saved = {name: os.environ.get(name) for name in launch.ENV_SANITIZE}
    sentinel = "PDVP_TEST_SCRUB_ENV_SENTINEL"
    saved_sentinel = os.environ.get(sentinel)
    try:
        for name in launch.ENV_SANITIZE:
            os.environ[name] = "1"
        os.environ[sentinel] = "keep-me"

        launch.scrub_env()

        for name in launch.ENV_SANITIZE:
            assert name not in os.environ, name
        assert os.environ.get(sentinel) == "keep-me"
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if saved_sentinel is None:
            os.environ.pop(sentinel, None)
        else:
            os.environ[sentinel] = saved_sentinel


# ---- spawn_pydevd: fd bookkeeping ----

def test_pty_conflicts_with_redirection_and_leaks_no_fds() -> None:
    for field in ("stdin", "stdout", "stderr"):
        c = Config()
        c.pty = "/dev/pts/does-not-need-to-exist-for-this-check"
        setattr(c, field, "/tmp/does-not-need-to-exist-either")

        before = _open_fd_count()
        try:
            launch.spawn_pydevd(c, _HOST, _PORT)
        except launch.LaunchError as e:
            assert "pty" in str(e), str(e)
        else:
            raise AssertionError(f"expected LaunchError for pty + {field}")
        assert _open_fd_count() == before, field


def test_default_pty_pair_used_when_nothing_set() -> None:
    c = Config()
    c.python_executable = sys.executable

    with _fake_popen():
        proc = launch.spawn_pydevd(c, _HOST, _PORT)
    try:
        assert proc.master_fd is not None
        assert proc.stdin_is_pty is True

        fake = _FakePopen.last
        assert fake.stdin_fd == fake.stdout_fd == fake.stderr_fd, fake

        try:
            os.fstat(fake.stdin_fd)
        except OSError:
            pass
        else:
            raise AssertionError("slave fd should be closed in our process after spawn")
    finally:
        os.close(proc.master_fd)


def test_full_redirection_no_pty() -> None:
    with tempfile.TemporaryDirectory() as d:
        stdin_path = os.path.join(d, "in.txt")
        stdout_path = os.path.join(d, "out.txt")
        stderr_path = os.path.join(d, "err.txt")
        open(stdin_path, "w").close()

        c = Config()
        c.python_executable = sys.executable
        c.stdin = stdin_path
        c.stdout = stdout_path
        c.stderr = stderr_path

        with _fake_popen():
            proc = launch.spawn_pydevd(c, _HOST, _PORT)

        assert proc.master_fd is None
        assert proc.stdin_is_pty is False

        fake = _FakePopen.last
        assert _real_path(fake.stdin_target) == _real_path(stdin_path)
        assert _real_path(fake.stdout_target) == _real_path(stdout_path)
        assert _real_path(fake.stderr_target) == _real_path(stderr_path)

        for fd in (fake.stdin_fd, fake.stdout_fd, fake.stderr_fd):
            try:
                os.fstat(fd)
            except OSError:
                continue
            raise AssertionError("redirection fd leaked past spawn_pydevd()")


def test_partial_redirection_still_uses_pty_for_missing_streams() -> None:
    with tempfile.TemporaryDirectory() as d:
        stdout_path = os.path.join(d, "out.txt")

        c = Config()
        c.python_executable = sys.executable
        c.stdout = stdout_path

        with _fake_popen():
            proc = launch.spawn_pydevd(c, _HOST, _PORT)
        try:
            assert proc.master_fd is not None

            fake = _FakePopen.last
            assert _real_path(fake.stdout_target) == _real_path(stdout_path)
            # stdin/stderr fall back to the pty slave, same as the all-unset case.
            assert fake.stdin_fd == fake.stderr_fd
            assert fake.stdin_fd != fake.stdout_fd
        finally:
            os.close(proc.master_fd)


def test_stderr_alias_dups_stdout() -> None:
    with tempfile.TemporaryDirectory() as d:
        stdin_path = os.path.join(d, "in.txt")
        stdout_path = os.path.join(d, "out.txt")
        open(stdin_path, "w").close()

        c = Config()
        c.python_executable = sys.executable
        c.stdin = stdin_path
        c.stdout = stdout_path
        c.stderr = "&1"

        with _fake_popen():
            proc = launch.spawn_pydevd(c, _HOST, _PORT)

        assert proc.master_fd is None  # all three streams accounted for, no pty needed

        fake = _FakePopen.last
        assert fake.stderr_fd != fake.stdout_fd, "stderr should be a dup, not the same fd number"
        assert _real_path(fake.stderr_target) == _real_path(fake.stdout_target) == _real_path(stdout_path)


def test_explicit_pty_device_used_directly() -> None:
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    os.close(slave_fd)  # only need the path; spawn_pydevd() reopens it itself
    try:
        c = Config()
        c.python_executable = sys.executable
        c.pty = slave_path

        with _fake_popen():
            proc = launch.spawn_pydevd(c, _HOST, _PORT)

        assert proc.master_fd is None

        fake = _FakePopen.last
        assert fake.stdin_fd == fake.stdout_fd == fake.stderr_fd
        assert _real_path(fake.stdin_target) == _real_path(slave_path)

        try:
            os.fstat(fake.stdin_fd)
        except OSError:
            pass
        else:
            raise AssertionError("pty device fd leaked past spawn_pydevd()")
    finally:
        os.close(master_fd)


TESTS = [
    test_build_spawn_argv_python_default_uses_sys_executable,
    test_build_spawn_argv_respects_python_executable_override,
    test_build_spawn_argv_jython_ignores_python_executable,
    test_vm_type_never_reaches_the_spawn_argv,
    test_python_executable_default_factory_reads_env,
    test_python_cli_flag_sets_python_executable,
    test_scrub_env_drops_only_the_sanitize_list,
    test_pty_conflicts_with_redirection_and_leaks_no_fds,
    test_default_pty_pair_used_when_nothing_set,
    test_full_redirection_no_pty,
    test_partial_redirection_still_uses_pty_for_missing_streams,
    test_stderr_alias_dups_stdout,
    test_explicit_pty_device_used_directly,
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
