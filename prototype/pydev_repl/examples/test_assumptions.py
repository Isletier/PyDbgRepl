"""
Verify pydevd launch assumptions:
  1. --file script.py  works (baseline)
  2. --module --file pkg.mod  works
  3. missing --file  → pydevd errors out
  4. PYCHARM_DEBUG / PYDEV_DEBUG / PYDEVD_DEBUG bleed into stderr
  5. PYDEVD_IPYTHON_COMPATIBLE_DEBUGGING / PYDEVD_IPYTHON_CONTEXT are inert for plain scripts
"""
import os
import socket
import subprocess
import sys
import threading
import time

PYTHON = os.path.join(os.path.dirname(sys.executable), "python")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable

EXAMPLES = os.path.dirname(os.path.abspath(__file__))
ACCEPT_TIMEOUT = 10.0

# ── helpers ──────────────────────────────────────────────────────────────────

def spawn_pydevd(extra_args, env=None, cwd=None):
    """Bind an ephemeral port, spawn pydevd --client mode, return (proc, port)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    listener.settimeout(ACCEPT_TIMEOUT)

    cmd = [
        PYTHON, "-Xfrozen_modules=off", "-m", "pydevd",
        "--client", "127.0.0.1",
        "--port", str(port),
        "--json-dap-http",
    ] + extra_args

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )

    try:
        sock, _ = listener.accept()
    except socket.timeout:
        proc.kill()
        proc.wait()
        raise RuntimeError(f"pydevd did not connect (port {port})\n"
                           f"stderr: {proc.stderr.read().decode()}")
    finally:
        listener.close()

    return proc, sock


def drain(proc, timeout=3.0):
    """Wait for process to exit, return (stdout, stderr)."""
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return out.decode(), err.decode()


def ok(label):
    print(f"  PASS  {label}")

def fail(label, reason):
    print(f"  FAIL  {label}: {reason}")

# ── tests ─────────────────────────────────────────────────────────────────────

def test_file_mode():
    """Assumption: --file script.py launches and connects."""
    script = os.path.join(EXAMPLES, "counter.py")
    proc, sock = spawn_pydevd(["--file", script])
    sock.close()
    out, err = drain(proc)
    ok("--file mode connects")

def test_module_mode():
    """Assumption: --module --file pkg.main launches and connects."""
    proc, sock = spawn_pydevd(
        ["--module", "--file", "mypackage.main"],
        cwd=EXAMPLES,
    )
    sock.close()
    out, err = drain(proc)
    ok("--module --file mode connects")

def test_missing_file():
    """Assumption: missing --file causes pydevd to error without connecting."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    listener.settimeout(3.0)

    cmd = [
        PYTHON, "-Xfrozen_modules=off", "-m", "pydevd",
        "--client", "127.0.0.1",
        "--port", str(port),
        "--json-dap-http",
        # intentionally no --file
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    connected = False
    try:
        sock, _ = listener.accept()
        sock.close()
        connected = True
    except socket.timeout:
        pass
    finally:
        listener.close()

    out, err = drain(proc)

    if not connected:
        ok("missing --file: pydevd did not connect (expected)")
    else:
        # pydevd connected but may have run empty — check exit
        if proc.returncode == 0:
            fail("missing --file", "pydevd connected and exited 0 (ran empty target)")
        else:
            ok("missing --file: connected but exited non-zero")

def test_debug_env_bleed():
    """Assumption: PYCHARM_DEBUG / PYDEV_DEBUG / PYDEVD_DEBUG flood stderr."""
    script = os.path.join(EXAMPLES, "counter.py")
    base_env = os.environ.copy()

    results = {}
    for var in ("PYCHARM_DEBUG", "PYDEV_DEBUG", "PYDEVD_DEBUG"):
        env = base_env.copy()
        env[var] = "true"
        env.pop("PYDEVD_DEBUG_FILE", None)
        try:
            proc, sock = spawn_pydevd(["--file", script], env=env)
            sock.close()
            out, err = drain(proc)
            results[var] = len(err)
        except RuntimeError as e:
            results[var] = f"ERROR: {e}"

    # baseline: no debug env
    proc, sock = spawn_pydevd(["--file", script])
    sock.close()
    out, baseline_err = drain(proc)
    baseline_len = len(baseline_err)

    for var, length in results.items():
        if isinstance(length, str):
            fail(f"{var} bleed", length)
        elif length > baseline_len:
            ok(f"{var}=true produces more stderr ({length} vs baseline {baseline_len})")
        else:
            fail(f"{var} bleed", f"stderr not larger ({length} vs baseline {baseline_len})")

def test_ipython_env_inert():
    """Assumption: PYDEVD_IPYTHON_* vars don't break plain script execution."""
    script = os.path.join(EXAMPLES, "counter.py")
    env = os.environ.copy()
    env["PYDEVD_IPYTHON_COMPATIBLE_DEBUGGING"] = "true"
    env["PYDEVD_IPYTHON_CONTEXT"] = "interactiveshell.py,run_code,run_ast_nodes"

    try:
        proc, sock = spawn_pydevd(["--file", script], env=env)
        sock.close()
        drain(proc)
        ok("PYDEVD_IPYTHON_* vars: plain script unaffected")
    except RuntimeError as e:
        fail("PYDEVD_IPYTHON_* vars", str(e))

# ── main ──────────────────────────────────────────────────────────────────────

TESTS = [
    test_file_mode,
    test_module_mode,
    test_missing_file,
    test_debug_env_bleed,
    test_ipython_env_inert,
]

if __name__ == "__main__":
    print(f"Python:  {PYTHON}")
    print(f"Examples: {EXAMPLES}")
    print()
    for t in TESTS:
        label = t.__name__.replace("test_", "")
        print(f"[{label}]")
        try:
            t()
        except Exception as e:
            fail(label, f"exception: {e}")
        print()
