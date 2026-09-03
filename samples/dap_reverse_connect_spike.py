#!/usr/bin/env python3
"""Spike: does pydevd's `--client` (reverse-connect) mode speak the same DAP?

The question this answers, before any refactoring depends on it: pydevd is
normally started with `--server`, so it listens and we dial. In `--client
<host>` mode it dials *us*. That removes the startup race entirely -- our
listening socket exists before the child does, so there is nothing to poll,
retry or sleep for, and binding port 0 lets the kernel hand us a free port
instead of guessing one.

What has to hold for that to be usable:

  1. pydevd connects out to our listener.
  2. We are still the DAP *client* over that socket -- i.e. we send
     `initialize`, it answers. TCP direction and DAP role are independent in
     principle; this checks that pydevd agrees.
  3. The rest of the handshake (attach / initialized / setBreakpoints /
     configurationDone) behaves as it does in server mode.
  4. A breakpoint actually hits, so the connection is real and not just a
     handshake that happens to complete.

Deliberately written against a raw socket rather than pdvp.core.dap, so it
tests pydevd rather than our own transport.

    python samples/dap_reverse_connect_spike.py
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "targets", "sleep_sum.py")
ACCEPT_TIMEOUT = 10.0
READ_TIMEOUT = 15.0


# ---- minimal Content-Length framing ----

class Wire:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.seq = 0
        # Events seen while waiting for something else. pydevd sends
        # `initialized` before the `attach` response, so anything that waits
        # for an event by name has to look here first or it waits forever for
        # a second copy. (Same ordering trap the real client has to handle.)
        self.seen = []

    def send(self, command, **arguments):
        self.seq += 1
        body = json.dumps({
            "seq": self.seq,
            "type": "request",
            "command": command,
            "arguments": arguments,
        }).encode("utf-8")
        self.sock.sendall(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        return self.seq

    def recv(self):
        while b"\r\n\r\n" not in self.buf:
            self.buf += self._chunk()
        header, _, self.buf = self.buf.partition(b"\r\n\r\n")

        length = 0
        for line in header.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())

        while len(self.buf) < length:
            self.buf += self._chunk()
        payload, self.buf = self.buf[:length], self.buf[length:]
        return json.loads(payload.decode("utf-8"))

    def _chunk(self):
        chunk = self.sock.recv(4096)
        if not chunk:
            raise ConnectionError("pydevd closed the connection")
        return chunk

    def await_response(self, seq, what):
        """Read until the response to `seq`, reporting events seen on the way."""
        while True:
            msg = self.recv()
            if msg.get("type") == "event":
                print(f"      event: {msg['event']}")
                self.seen.append(msg)
            elif msg.get("type") == "response" and msg.get("request_seq") == seq:
                ok = msg.get("success")
                print(f"   -> {what}: success={ok}" + ("" if ok else f"  {msg.get('message')!r}"))
                return msg

    def await_event(self, name):
        for i, msg in enumerate(self.seen):
            if msg["event"] == name:
                print(f"   -> {name}: already arrived")
                return self.seen.pop(i)

        while True:
            msg = self.recv()
            if msg.get("type") == "event":
                print(f"      event: {msg['event']}")
                if msg["event"] == name:
                    return msg
                self.seen.append(msg)


def main() -> int:
    # 1. Bind BEFORE spawning: pydevd needs the port on its command line, and
    #    port 0 means the kernel picks a free one for us.
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    print(f"[1] listening on {host}:{port} (kernel-assigned, no guessing)")

    argv = [
        sys.executable, "-m", "pydevd",
        "--client", host,            # <-- the whole point: pydevd dials us
        "--port", str(port),
        "--json-dap-http",
        "--skip-notify-stdin",
        "--file", TARGET,
    ]
    print(f"[2] spawning: {' '.join(argv[1:])}")
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)

    def drain(stream, label):
        for line in stream:
            print(f"      [{label}] {line.rstrip()}")

    threading.Thread(target=drain, args=(child.stdout, "child out"), daemon=True).start()
    threading.Thread(target=drain, args=(child.stderr, "child err"), daemon=True).start()

    try:
        # 2. accept() IS the wait. No polling, no retry, no sleep.
        listener.settimeout(ACCEPT_TIMEOUT)
        started = time.monotonic()
        try:
            sock, peer = listener.accept()
        except TimeoutError:
            print(f"[3] FAIL: nothing connected within {ACCEPT_TIMEOUT}s "
                  f"(child exit status: {child.poll()})")
            return 1
        waited = time.monotonic() - started
        print(f"[3] accepted from {peer} after {waited*1000:.0f} ms")
    finally:
        listener.close()   # one connection is all we ever want

    sock.settimeout(READ_TIMEOUT)
    wire = Wire(sock)

    try:
        # 3. Are we still the DAP client over a socket we accepted?
        print("[4] handshake")
        seq = wire.send("initialize", adapterID="pdvp-spike", clientID="pdvp",
                        linesStartAt1=True, columnsStartAt1=True,
                        pathFormat="path", supportsVariableType=True)
        init = wire.await_response(seq, "initialize")
        if not init.get("success"):
            print("    FAIL: pydevd did not answer initialize")
            return 1
        caps = sorted(k for k, v in (init.get("body") or {}).items() if v is True)
        print(f"      capabilities: {len(caps)} advertised, e.g. {caps[:4]}")

        seq = wire.send("attach", justMyCode=False)
        wire.await_response(seq, "attach")
        wire.await_event("initialized")

        # 4. A breakpoint, to prove the connection is real and not just a
        #    handshake that completed by accident.
        seq = wire.send("setBreakpoints",
                        source={"path": TARGET},
                        breakpoints=[{"line": 6}],
                        lines=[6])
        bps = wire.await_response(seq, "setBreakpoints")
        print(f"      verified: {[b.get('verified') for b in (bps.get('body') or {}).get('breakpoints', [])]}")

        seq = wire.send("configurationDone")
        wire.await_response(seq, "configurationDone")

        print("[5] waiting for the breakpoint to hit")
        stopped = wire.await_event("stopped")
        reason = (stopped.get("body") or {}).get("reason")
        print(f"    -> stopped, reason={reason!r}")

        seq = wire.send("disconnect", terminateDebuggee=True)
        try:
            wire.await_response(seq, "disconnect")
        except (ConnectionError, OSError):
            print("   -> disconnect: peer closed without answering (fine)")

        print("\nRESULT: --client mode speaks DAP exactly as --server does.")
        print("        We accept the socket; we are still the DAP client.")
        return 0

    except (ConnectionError, OSError, TimeoutError) as e:
        print(f"\nRESULT: FAILED -- {type(e).__name__}: {e}")
        return 1
    finally:
        try:
            sock.close()
        except OSError:
            pass
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
        print(f"        child exit status: {child.returncode}")


if __name__ == "__main__":
    raise SystemExit(main())
