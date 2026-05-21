"""High-level debug session: spawn pydevd-as-launcher and drive DAP.

Lifecycle (launch):
  1. bind an ephemeral TCP listener on 127.0.0.1
  2. spawn `python -m pydevd --client 127.0.0.1 --port P --json-dap --file SCRIPT`
     pydevd dials back to us as a TCP client speaking DAP
  3. accept the inbound socket → wrap in DapClient
  4. initialize → attach → (wait initialized event) →
     setBreakpoints@line1 → configurationDone → wait `stopped` event
  5. expose step_over() and exit() on top
"""
import os
import socket
import subprocess
import sys

from pydev_repl.dap import DapClient, DapError


ACCEPT_TIMEOUT = 15.0
DAP_TIMEOUT = 15.0


class DebugSession:
    def __init__(self):
        self._proc = None
        self._dap = None
        self._thread_id = None
        self._finished = False

    # ---- lifecycle ------------------------------------------------------

    def launch(self, script_path: str):
        if self._dap is not None:
            return {"event": "error", "message": "already launched"}
        script_path = os.path.abspath(script_path)
        if not os.path.isfile(script_path):
            return {"event": "error", "message": f"no such file: {script_path}"}

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        listener.settimeout(ACCEPT_TIMEOUT)

        try:
            self._proc = self._spawn_pydevd(port, script_path)
            sock, _ = listener.accept()
        finally:
            listener.close()

        self._dap = DapClient(sock)
        self._dap_handshake(script_path)
        return self._wait_for_pause()

    def step_over(self):
        if self._dap is None:
            return {"event": "error", "message": "not running"}
        if self._finished:
            return {"event": "finished"}
        if self._thread_id is None:
            return {"event": "error", "message": "no current thread"}
        self._dap.request(
            "next",
            {"threadId": self._thread_id, "granularity": "line"},
            timeout=DAP_TIMEOUT,
        )
        return self._wait_for_pause()

    def exit(self):
        if self._dap is not None and not self._finished:
            try:
                self._dap.request(
                    "disconnect", {"terminateDebuggee": True}, timeout=3.0
                )
            except DapError:
                pass
        if self._dap is not None:
            self._dap.close()
            self._dap = None
        if self._proc is not None:
            try:
                self._proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
            self._proc = None
        self._finished = True
        return {"event": "shutdown"}

    # ---- internals ------------------------------------------------------

    def _spawn_pydevd(self, port: int, script_path: str) -> subprocess.Popen:
        cmd = [
            sys.executable,
            "-m", "pydevd",
            "--client", "127.0.0.1",
            "--port", str(port),
            "--json-dap-http",
            "--file", script_path,
        ]
        return subprocess.Popen(cmd)

    def _dap_handshake(self, script_path: str):
        self._dap.request("initialize", {
            "clientID": "pydev-repl",
            "clientName": "pydev-repl",
            "adapterID": "pydevd",
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "supportsVariableType": True,
            "supportsRunInTerminalRequest": False,
        }, timeout=DAP_TIMEOUT)
        self._dap.request("attach", {}, timeout=DAP_TIMEOUT)
        self._dap.wait_event("initialized", timeout=DAP_TIMEOUT)
        self._dap.request("setBreakpoints", {
            "source": {
                "path": script_path,
                "name": os.path.basename(script_path),
            },
            "breakpoints": [{"line": 1}],
            "lines": [1],
        }, timeout=DAP_TIMEOUT)
        self._dap.request("configurationDone", {}, timeout=DAP_TIMEOUT)

    def _wait_for_pause(self):
        ev = self._dap.wait_event(
            {"stopped", "terminated", "exited"}, timeout=DAP_TIMEOUT
        )
        name = ev.get("event")
        body = ev.get("body") or {}
        if name == "stopped":
            tid = body.get("threadId")
            if tid is not None:
                self._thread_id = tid
            loc = self._current_location()
            return {"event": "paused", "reason": body.get("reason"), **loc}
        if name in ("terminated", "exited"):
            self._finished = True
            result = {"event": "finished"}
            if "exitCode" in body:
                result["exitCode"] = body["exitCode"]
            return result
        return {"event": "unknown", "raw": ev}

    def _current_location(self):
        if self._thread_id is None:
            return {}
        try:
            resp = self._dap.request(
                "stackTrace",
                {"threadId": self._thread_id, "startFrame": 0, "levels": 1},
                timeout=DAP_TIMEOUT,
            )
        except DapError:
            return {}
        frames = (resp.get("body") or {}).get("stackFrames") or []
        if not frames:
            return {}
        top = frames[0]
        source = top.get("source") or {}
        return {"file": source.get("path"), "line": top.get("line")}
