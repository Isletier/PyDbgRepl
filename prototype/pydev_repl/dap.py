"""Minimum Debug Adapter Protocol client over a TCP socket.

Wire format (per DAP spec): each message is a length-framed JSON envelope.
    Content-Length: <N>\r\n\r\n<N bytes of UTF-8 JSON>

A background reader thread demuxes the inbound stream into:
  - responses (matched to outstanding requests by request_seq)
  - events    (delivered on a queue for the caller to consume)
  - reverse-requests (rare; pydevd extensions — we ack them politely)
"""
import json
import queue
import socket
import threading
import time


class DapError(Exception):
    pass


class DapClient:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._rfile = sock.makefile("rb")
        self._wfile = sock.makefile("wb")
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending = {}      # request seq -> threading.Event
        self._responses = {}    # request seq -> response message
        self._events = queue.Queue()
        self._closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ---- public API -----------------------------------------------------

    def request(self, command: str, arguments=None, timeout: float = 10.0):
        seq = self._next_seq()
        msg = {"seq": seq, "type": "request", "command": command}
        if arguments is not None:
            msg["arguments"] = arguments
        evt = threading.Event()
        self._pending[seq] = evt
        self._write_message(msg)
        if not evt.wait(timeout):
            self._pending.pop(seq, None)
            raise DapError(f"{command!r} request timed out after {timeout}s")
        resp = self._responses.pop(seq, None)
        if resp is None:
            raise DapError(f"{command!r}: connection closed before response")
        if not resp.get("success", False):
            raise DapError(f"{command!r} failed: {resp.get('message')!r}")
        return resp

    def wait_event(self, names, timeout: float = 10.0):
        """Block until an event with .event in `names` arrives. Drop others."""
        if isinstance(names, str):
            names = {names}
        else:
            names = set(names)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DapError(f"timed out waiting for events {names}")
            try:
                msg = self._events.get(timeout=remaining)
            except queue.Empty:
                raise DapError(f"timed out waiting for events {names}")
            if msg is None:
                raise DapError(f"connection closed while waiting for {names}")
            if msg.get("event") in names:
                return msg
            # else: discard (output/thread/module/etc.) — extend on demand

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    # ---- internals ------------------------------------------------------

    def _next_seq(self):
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _read_loop(self):
        try:
            while not self._closed.is_set():
                msg = self._read_message()
                if msg is None:
                    break
                mtype = msg.get("type")
                if mtype == "response":
                    rs = msg.get("request_seq")
                    self._responses[rs] = msg
                    evt = self._pending.pop(rs, None)
                    if evt is not None:
                        evt.set()
                elif mtype == "event":
                    self._events.put(msg)
                elif mtype == "request":
                    # Reverse-request from the adapter (e.g. pydevdSystemInfo).
                    # Ack so the adapter doesn't hang waiting on us.
                    self._reverse_ack(msg)
        except Exception:
            pass
        finally:
            self._closed.set()
            for evt in list(self._pending.values()):
                evt.set()
            self._events.put(None)  # wake any wait_event() caller

    def _reverse_ack(self, req):
        resp = {
            "seq": self._next_seq(),
            "type": "response",
            "request_seq": req.get("seq"),
            "success": True,
            "command": req.get("command"),
            "body": {},
        }
        try:
            self._write_message(resp)
        except Exception:
            pass

    def _read_message(self):
        header = bytearray()
        while not bytes(header).endswith(b"\r\n\r\n"):
            b = self._rfile.read(1)
            if not b:
                return None
            header.extend(b)
        length = None
        for line in bytes(header).split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
                break
        if length is None:
            raise DapError(f"missing Content-Length header: {bytes(header)!r}")
        body = bytearray()
        while len(body) < length:
            chunk = self._rfile.read(length - len(body))
            if not chunk:
                return None
            body.extend(chunk)
        return json.loads(bytes(body).decode("utf-8"))

    def _write_message(self, msg):
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self._write_lock:
            self._wfile.write(header)
            self._wfile.write(body)
            self._wfile.flush()
