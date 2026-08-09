"""Content-Length framed JSON transport for DAP, over a TCP socket."""
import socket
import time

class DAPTransport:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""

    @classmethod
    def connect(cls, host: str, port: int, timeout: float | None = None, retry: int | None = None) -> "DAPTransport":
        sock = socket.create_connection((host, port), 1000000000.0)
        sock.settimeout(None)
        return cls(sock)

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
