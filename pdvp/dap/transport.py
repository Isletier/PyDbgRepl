"""Content-Length framed JSON transport for DAP, over a TCP socket.

There are two ways to end up with one, and they differ only in how the socket
is obtained:

  Transport.connect()  we dial a pydevd that is already listening (remote).
  Transport.accept()   a pydevd we spawned with --client dials us (local).

Everything past construction -- send, recv, close, and the reader thread above
it -- is identical, and nothing downstream can tell which happened.
"""
import socket

# We only ever listen on loopback: anything that can reach this socket can
# drive the debugger, and the process dialling in is one we just spawned.
LISTEN_HOST = "127.0.0.1"


def listen(port: int = 0) -> socket.socket:
    """Bind and listen for a pydevd we are about to spawn with --client.

    Separate from Transport.accept() because of an ordering constraint:
    pydevd takes the port on its command line, so the socket has to be bound
    *before* the spawn and can only be accepted *after* it.

    Port 0 asks the kernel for a free one, which is why nothing here has to
    guess a port or handle a collision; the resolved address is
    sock.getsockname().

    The caller owns the returned socket and must close it -- including when
    the spawn fails or accept() raises, which is the path that would
    otherwise leak a bound port and an fd per attempt.
    """
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_HOST, port))
    sock.listen(1)
    return sock


class Transport:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""

    @classmethod
    def accept(cls, listener: socket.socket, timeout: float | None = None) -> "Transport":
        """Take the connection from a pydevd we spawned with --client.

        `timeout` bounds the wait: pydevd exits 1 within milliseconds if it
        cannot reach us, so without one a failed spawn would hang here
        forever with no reader thread yet running to notice.

        Does not close `listener` -- the caller owns it, and still has to
        close it when this raises.
        """
        listener.settimeout(timeout)
        sock, _peer = listener.accept()

        # accept() only forces the new socket blocking when the process-wide
        # default timeout is None (CPython issue #7995), and anything the
        # user imports at the REPL can call socket.setdefaulttimeout(). Reads
        # here block until the debuggee next stops, which is unbounded.
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
        print(message.decode("utf-8"))
        header = f"Content-Length: {len(message)}\r\n\r\n".encode("ascii")
        self._sock.sendall(header + message)

    def recv(self) -> str:
        header = self._read_header()

        length = self._parse_content_length(header)
        body = self._read_exact(length).decode("utf-8")
        print(body)
        return body

    def close(self) -> None:
        try:
            self._sock.setblocking(False)
            while self._sock.recv(65536):     # empty the receive queue first
                pass
        except (OSError):
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)   # now wakes the reader, sends FIN
        except OSError:
            pass                                     # ENOTCONN: peer already gone
        self._sock.close()

    def _read_header(self) -> bytes:
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed while reading header")
            self._buf += chunk
        header, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest
        return header

    def _read_exact(self, n: int) -> bytes:
        if n < 0:
            raise ConnectionError("Negative exact read byte count")

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
            if name.strip().lower() == b"content-length":
                return int(value.strip())
        raise ValueError(f"missing Content-Length header: {header!r}")
