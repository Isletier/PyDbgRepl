"""Content-Length framed JSON transport for DAP, over a TCP socket."""
import json
import socket


class DAPTransport:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""

    @classmethod
    def connect(cls, host: str, port: int) -> "DAPTransport":
        return cls(socket.create_connection((host, port)))

    def send(self, message: dict) -> None:
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._sock.sendall(header + body)

    def recv(self) -> dict:
        header = self._read_header()
        length = self._parse_content_length(header)
        body = self._read_exact(length)
        return json.loads(body.decode("utf-8"))

    def close(self) -> None:
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
