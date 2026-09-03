"""Content-Length framed message transport for DAP, over a TCP socket.

A Transport owns one connected socket and its read buffer. Framing is DAP's:
an HTTP-style header block in which only Content-Length is meaningful, then
that many bytes of body. Bodies are bytes; encoding is the client's business.

Two constructors, and only they retry or take timeouts:

  Transport.connect()  we dial a pydevd that is already listening (remote).
  Transport.accept()   a pydevd we spawned with --client dials us (local).

Past construction the two are indistinguishable.

Every exception from send() and recv() is terminal; the caller's response is
always close(). This layer never retries, reconnects, or reports:

  ConnectionError  the peer is gone -- EOF, reset, or a socket closed under us.
  ProtocolError    the peer framed something unparseable.

shutdown() unblocks a recv() already in flight; close() does not. Keepalive is
enabled on every connection.
"""
import socket

# What listen() binds to; never a routable address.
LISTEN_HOST = "127.0.0.1"

# Caps on what we buffer for one message.
MAX_HEADER_SIZE = 8 * 1024
MAX_BODY_SIZE = 64 * 1024 * 1024

# Budget for noticing a peer that died without closing: 60s idle, 60s with a
# send outstanding. The kernel defaults are 2h11m and ~15min.
KEEPALIVE_IDLE = 30         # seconds of silence before the first probe
KEEPALIVE_INTERVAL = 10     # seconds between probes
KEEPALIVE_COUNT = 3         # unanswered probes before the connection is dead
USER_TIMEOUT_MS = 60_000    # ms an unacknowledged send may stay outstanding


class ProtocolError(Exception):
    """The peer framed something we cannot parse. Unrecoverable."""


def listen(port: int = 0) -> socket.socket:
    """Bind and listen on loopback for a pydevd spawned with --client.

    Port 0 lets the kernel choose; the bound address is sock.getsockname().
    The caller owns the socket and must close it, including when the spawn or
    the later accept() fails.
    """
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_HOST, port))
    sock.listen(1)
    return sock


def _enable_keepalive(sock: socket.socket) -> None:
    """Bound how long a vanished peer can keep recv() blocked.

    A no-op on anything but TCP. Options the platform does not define are
    skipped, leaving the system defaults.
    """
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (
        ("TCP_KEEPIDLE", KEEPALIVE_IDLE),
        ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL),
        ("TCP_KEEPCNT", KEEPALIVE_COUNT),
        ("TCP_USER_TIMEOUT", USER_TIMEOUT_MS),
    ):
        option = getattr(socket, name, None)
        if option is not None:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)


class Transport:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""
        _enable_keepalive(sock)

    @classmethod
    def accept(cls, listener: socket.socket, timeout: float | None = None) -> "Transport":
        """Take the connection from a pydevd we spawned with --client.

        `timeout` bounds the wait; None blocks indefinitely. Raises
        TimeoutError when nobody dials. Does not close `listener`, on either
        path -- the caller owns it.
        """
        listener.settimeout(timeout)
        sock, _peer = listener.accept()

        # The accepted socket inherits the process-wide default timeout
        # (CPython issue #7995); reads here must block.
        sock.settimeout(None)
        return cls(sock)

    @classmethod
    def connect(cls, host: str, port: int, timeout: float | None = None, retry: int = 1) -> "Transport":
        """Dial a pydevd that is already listening, retrying only a timeout.

        `retry` is the number of *attempts*, not extra ones: 1 means try once.
        """
        failure: TimeoutError | None = None

        for _ in range(max(retry, 1)):
            try:
                sock = socket.create_connection((host, port), timeout)
            except TimeoutError as e:
                failure = e
                continue
            sock.settimeout(None)
            return cls(sock)

        raise failure

    def send(self, message: bytes) -> None:
        header = f"Content-Length: {len(message)}\r\n\r\n".encode("ascii")
        self._sock.sendall(header + message)

    def recv(self) -> bytes:
        header = self._read_header()

        length = self._parse_content_length(header)
        return self._read_exact(length)

    def shutdown(self) -> None:
        """Unblock a pending recv() and send FIN. Does not release the fd."""
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass    # ENOTCONN: peer already gone

    def close(self) -> None:
        """Release the fd. Call shutdown() first if a reader may be blocked."""
        self._sock.close()

    def _read_header(self) -> bytes:
        while b"\r\n\r\n" not in self._buf:
            if len(self._buf) > MAX_HEADER_SIZE:
                raise ProtocolError(f"header exceeds {MAX_HEADER_SIZE} bytes with no end marker")
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed while reading header")
            self._buf += chunk
        header, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest
        return header

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed while reading body")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    @staticmethod
    def _parse_content_length(header: bytes) -> int:
        for line in header.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() != b"content-length":
                continue
            try:
                length = int(value.strip())
            except ValueError:
                raise ProtocolError(f"malformed Content-Length: {value!r}") from None
            if not 0 < length <= MAX_BODY_SIZE:
                raise ProtocolError(f"out-of-range Content-Length: {length}")
            return length
        raise ProtocolError(f"missing Content-Length header: {header!r}")

