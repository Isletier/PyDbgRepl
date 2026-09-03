"""Tests for Transport construction: connect retry policy, timeouts, accept.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all
of them (same convention as pdvp.core.dap.test.test_dap_client).

Run from the repo root with the venv active:

    python -m pdvp.core.dap.test.test_transport

Two layers, because they answer different questions:

  * a stubbed socket.create_connection pins down the *policy* -- how many
    attempts, which exceptions are retried -- instantly and deterministically.
  * one real connection against a saturated listener proves the timeout
    actually reaches the socket, which a stub can never show.
"""
import contextlib
import socket
import threading
import time

from pdvp.core.dap.transport import (
    LISTEN_HOST,
    MAX_BODY_SIZE,
    MAX_HEADER_SIZE,
    ProtocolError,
    Transport,
    listen,
)

# How many connections we are willing to open while filling a listener's
# accept queue before giving up on saturating it.
_MAX_FILL = 16


# ---- stubbing socket.create_connection ----

class _FakeConnect:
    """Stands in for socket.create_connection, playing a scripted list of
    outcomes and recording what it was called with.

    An outcome is either an exception instance (raised) or the string "ok"
    (returns a real, connected socket -- a socketpair end, so settimeout()
    and gettimeout() behave for real). The last outcome repeats forever.
    """

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self._opened: list[socket.socket] = []
        self.calls = 0
        self.timeouts_seen: list[float | None] = []

    def __call__(self, address, timeout=None, *args, **kwargs):
        self.calls += 1
        self.timeouts_seen.append(timeout)

        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome

        left, right = socket.socketpair()
        self._opened += [left, right]
        return left

    def cleanup(self) -> None:
        for sock in self._opened:
            sock.close()


@contextlib.contextmanager
def _stub_connect(*outcomes):
    """Swap socket.create_connection for a scripted stub.

    transport.py does `import socket` and calls socket.create_connection, so
    patching the attribute on the shared module object is what it sees.
    """
    fake = _FakeConnect(outcomes)
    real = socket.create_connection
    socket.create_connection = fake
    try:
        yield fake
    finally:
        socket.create_connection = real
        fake.cleanup()


def test_succeeds_without_retrying_when_the_first_attempt_works() -> None:
    with _stub_connect("ok") as fake:
        Transport.connect("127.0.0.1", 1234, timeout=0.1, retry=5)
    assert fake.calls == 1, fake.calls


def test_retries_a_timeout_until_one_attempt_succeeds() -> None:
    with _stub_connect(TimeoutError(), TimeoutError(), "ok") as fake:
        Transport.connect("127.0.0.1", 1234, timeout=0.1, retry=5)
    # Stops as soon as it works: 3 attempts, not the full 5.
    assert fake.calls == 3, fake.calls


def test_retry_counts_attempts_not_extra_attempts() -> None:
    """`retry=3` means three connections are attempted in total."""
    with _stub_connect(TimeoutError()) as fake:
        try:
            Transport.connect("127.0.0.1", 1234, timeout=0.1, retry=3)
        except TimeoutError:
            pass
        else:
            raise AssertionError("expected the last timeout to propagate")
    assert fake.calls == 3, fake.calls


def test_retry_of_zero_or_one_still_attempts_once() -> None:
    for retry in (0, 1):
        with _stub_connect("ok") as fake:
            Transport.connect("127.0.0.1", 1234, retry=retry)
        assert fake.calls == 1, (retry, fake.calls)


def test_refused_is_not_retried() -> None:
    """The policy under test: only a timeout is ambiguous about whether
    anyone is listening. A refusal is a definite answer, so retrying it just
    collects the same rejection `retry` times."""
    with _stub_connect(ConnectionRefusedError(111, "Connection refused")) as fake:
        try:
            Transport.connect("127.0.0.1", 1234, timeout=0.1, retry=5)
        except ConnectionRefusedError:
            pass
        else:
            raise AssertionError("expected ConnectionRefusedError to propagate")
    assert fake.calls == 1, fake.calls


def test_other_oserrors_are_not_retried_either() -> None:
    for error in (OSError(101, "Network is unreachable"),
                  OSError(13, "Permission denied"),
                  socket.gaierror(-2, "Name or service not known")):
        with _stub_connect(error) as fake:
            try:
                Transport.connect("nowhere", 1234, timeout=0.1, retry=5)
            except OSError as e:
                assert e is error, e
            else:
                raise AssertionError(f"expected {error!r} to propagate")
        assert fake.calls == 1, (error, fake.calls)


def test_the_timeout_is_passed_to_every_attempt() -> None:
    with _stub_connect(TimeoutError()) as fake:
        try:
            Transport.connect("127.0.0.1", 1234, timeout=0.25, retry=3)
        except TimeoutError:
            pass
    assert fake.timeouts_seen == [0.25, 0.25, 0.25], fake.timeouts_seen


def test_connect_timeout_does_not_become_a_read_timeout() -> None:
    """create_connection leaves its timeout on the socket it returns. Reads
    here block until the debuggee next stops, which is unbounded, so connect()
    must clear it."""
    with _stub_connect("ok"):
        transport = Transport.connect("127.0.0.1", 1234, timeout=0.25)
    assert transport._sock.gettimeout() is None, transport._sock.gettimeout()


# ---- a real connect timeout ----

@contextlib.contextmanager
def _closed_port():
    """A port on loopback with nothing bound to it."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    host, port = probe.getsockname()
    probe.close()
    yield host, port


@contextlib.contextmanager
def _blackhole_port():
    """A loopback port where connect() hangs instead of being refused.

    A *closed* loopback port is useless for timeout testing: the kernel
    answers the SYN with an immediate RST, so connect() raises
    ConnectionRefusedError in microseconds and never gets near the timeout.

    A *listening* socket whose accept queue has overflowed behaves the way a
    dropped packet does. With net.ipv4.tcp_abort_on_overflow=0 (the default),
    Linux silently discards further SYNs rather than resetting them, so the
    client retransmits and connect() blocks until its own timeout fires. No
    root and no real network needed.

    We saturate the queue by connecting until one of our own connects hangs,
    rather than assuming a fixed number -- the exact threshold depends on the
    kernel's backlog accounting.
    """
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)  # tiny accept queue, and nothing ever calls accept()
    host, port = listener.getsockname()

    holders: list[socket.socket] = []
    try:
        for _ in range(_MAX_FILL):
            filler = socket.socket()
            filler.settimeout(0.5)
            try:
                filler.connect((host, port))
            except OSError:
                # This one was dropped: the queue is full and the port is now
                # a blackhole for anybody else.
                filler.close()
                break
            holders.append(filler)
        else:
            raise RuntimeError(
                f"could not saturate the accept queue after {_MAX_FILL} connections")

        yield host, port
    finally:
        for sock in holders:
            sock.close()
        listener.close()


def test_closed_port_is_refused_rather_than_timing_out() -> None:
    """Why _blackhole_port() has to exist at all."""
    with _closed_port() as (host, port):
        started = time.monotonic()
        try:
            Transport.connect(host, port, timeout=5.0, retry=1)
        except ConnectionRefusedError:
            pass
        else:
            raise AssertionError("expected ConnectionRefusedError on a closed port")
        elapsed = time.monotonic() - started

    # Nowhere near the 5s timeout: the refusal is immediate, which is exactly
    # why it must not be retried.
    assert elapsed < 1.0, elapsed


def test_real_connect_times_out_and_retries_each_attempt() -> None:
    attempts, timeout = 3, 0.25

    with _blackhole_port() as (host, port):
        started = time.monotonic()
        try:
            Transport.connect(host, port, timeout=timeout, retry=attempts)
        except TimeoutError:
            pass
        else:
            raise AssertionError("expected TimeoutError against a blackhole port")
        elapsed = time.monotonic() - started

    # Each attempt waits out the full timeout, so the total is attempts x
    # timeout. This is the assertion that would catch the timeout not being
    # passed through, or the retry loop running the wrong number of times.
    assert elapsed >= attempts * timeout * 0.9, elapsed
    assert elapsed < attempts * timeout + 1.0, elapsed


# ---- listen() / Transport.accept() ----

def test_listen_binds_a_kernel_chosen_loopback_port() -> None:
    with contextlib.closing(listen()) as listener:
        host, port = listener.getsockname()
        assert host == LISTEN_HOST, host
        assert port != 0, port


def test_listen_honours_an_explicit_port() -> None:
    with contextlib.closing(listen()) as first:
        wanted = first.getsockname()[1]
    with contextlib.closing(listen(wanted)) as second:
        assert second.getsockname()[1] == wanted


def test_accept_times_out_when_nobody_dials() -> None:
    """The case that guards a failed spawn: pydevd exits within milliseconds
    when it cannot reach us, and without a timeout this would hang forever
    with no reader thread yet running to notice."""
    with contextlib.closing(listen()) as listener:
        started = time.monotonic()
        try:
            Transport.accept(listener, timeout=0.25)
        except TimeoutError:
            pass
        else:
            raise AssertionError("expected TimeoutError with nobody connecting")
        elapsed = time.monotonic() - started

    assert 0.2 <= elapsed < 1.5, elapsed


def test_accept_leaves_the_listener_open_on_both_paths() -> None:
    """accept() does not own the listener -- the caller closes it, including
    when accept() raises, which is the path that would otherwise leak a bound
    port and an fd per failed run()."""
    with contextlib.closing(listen()) as listener:
        try:
            Transport.accept(listener, timeout=0.1)
        except TimeoutError:
            pass
        assert listener.fileno() != -1, "listener closed on the raise path"

        with contextlib.closing(_dial(listener.getsockname())):
            transport = Transport.accept(listener, timeout=2.0)
        assert listener.fileno() != -1, "listener closed on the success path"
        transport.close()


def test_accepted_socket_has_no_read_timeout() -> None:
    """CPython's accept() only forces blocking when the process-wide default
    timeout is None, and we always set one on the listener to bound the wait.
    Reads block until the debuggee next stops, which is unbounded."""
    socket.setdefaulttimeout(0.5)   # what any REPL-imported library could do
    try:
        with contextlib.closing(listen()) as listener:
            with contextlib.closing(_dial(listener.getsockname())):
                transport = Transport.accept(listener, timeout=2.0)
        assert transport._sock.gettimeout() is None, transport._sock.gettimeout()
        transport.close()
    finally:
        socket.setdefaulttimeout(None)


def test_accepted_and_dialled_transports_behave_identically() -> None:
    """The executable form of "the asymmetry stops at construction": both
    constructors yield a transport that frames and reads the same way."""
    with contextlib.closing(listen()) as listener:
        address = listener.getsockname()

        far_end: list = []
        dialler = threading.Thread(
            target=lambda: far_end.append(Transport.connect(*address, timeout=2.0)),
            daemon=True)
        dialler.start()

        accepted = Transport.accept(listener, timeout=2.0)
        dialler.join(timeout=2.0)

    dialled = far_end[0]
    try:
        request = b'{"seq":1,"type":"request","command":"initialize"}'
        accepted.send(request)
        assert dialled.recv() == request

        response = b'{"seq":1,"type":"response","success":true}'
        dialled.send(response)
        assert accepted.recv() == response
    finally:
        accepted.close()
        dialled.close()


def _dial(address):
    """A bare socket connected to `address`, for tests that need something on
    the far end without caring what it is."""
    return socket.create_connection(address, 2.0)


# ---- framing errors ----

@contextlib.contextmanager
def _pair():
    """A Transport with a bare socket on the far end, to feed it raw bytes."""
    with contextlib.closing(listen()) as listener:
        far = _dial(listener.getsockname())
        transport = Transport.accept(listener, timeout=2.0)
    try:
        yield transport, far
    finally:
        far.close()
        transport.close()


def _expect_protocol_error(transport, framed: bytes) -> None:
    try:
        transport.recv()
    except ProtocolError:
        return
    except Exception as e:
        raise AssertionError(f"{framed!r} raised {e!r}, expected ProtocolError") from None
    raise AssertionError(f"{framed!r} was accepted")


def test_framing_errors_are_protocol_errors() -> None:
    """A framing error means a bug on one side of the wire, not a dead
    connection, and must not reach the reader thread as a bare ValueError."""
    bad = [
        b"Content-Type: application/json\r\n\r\n{}",   # no Content-Length
        b"Content-Length: banana\r\n\r\n{}",           # not a number
        b"Content-Length: -5\r\n\r\n{}",               # negative
        b"Content-Length: 0\r\n\r\n",                  # empty body
    ]
    for framed in bad:
        with _pair() as (transport, far):
            far.sendall(framed)
            _expect_protocol_error(transport, framed)


def test_an_oversized_length_is_rejected_before_buffering() -> None:
    """Rejected while parsing the header, so it fails now rather than blocking
    forever on a body the peer is never going to send."""
    framed = f"Content-Length: {MAX_BODY_SIZE + 1}\r\n\r\n".encode("ascii")
    with _pair() as (transport, far):
        far.sendall(framed)
        started = time.monotonic()
        _expect_protocol_error(transport, framed)
        assert time.monotonic() - started < 1.0


def test_a_headerless_flood_is_rejected_rather_than_buffered() -> None:
    with _pair() as (transport, far):
        junk = b"x" * (MAX_HEADER_SIZE + 4096)
        far.sendall(junk)
        _expect_protocol_error(transport, junk)


# ---- shutdown / close ----

def test_shutdown_unblocks_a_pending_recv() -> None:
    """close() alone does not wake a thread already inside recv(); shutdown()
    is what lets the reader thread be joined instead of abandoned."""
    with contextlib.closing(listen()) as listener:
        far = _dial(listener.getsockname())
        transport = Transport.accept(listener, timeout=2.0)

    outcome: list = []
    reader = threading.Thread(target=lambda: outcome.append(_recv_outcome(transport)), daemon=True)
    reader.start()
    try:
        time.sleep(0.1)          # let it park inside recv()
        assert reader.is_alive(), "reader returned before anything was sent"

        transport.shutdown()
        reader.join(timeout=2.0)
        assert not reader.is_alive(), "shutdown() did not wake recv()"
        assert isinstance(outcome[0], ConnectionError), outcome[0]

        transport.close()        # safe now: the reader is gone
    finally:
        far.close()


def _recv_outcome(transport):
    try:
        return transport.recv()
    except Exception as e:
        return e


# ---- keepalive ----

def test_keepalive_is_configured_on_both_constructors() -> None:
    """Without it, a peer that dies without closing leaves recv() blocked
    forever: an idle TCP connection carries no traffic for anything to fail."""
    with contextlib.closing(listen()) as listener:
        address = listener.getsockname()

        far_end: list = []
        dialler = threading.Thread(
            target=lambda: far_end.append(Transport.connect(*address, timeout=2.0)),
            daemon=True)
        dialler.start()

        accepted = Transport.accept(listener, timeout=2.0)
        dialler.join(timeout=2.0)

    # Spelled out rather than imported: the point is to pin the detection
    # budget at 30 + 10*3 = 60s, so retuning it has to be deliberate.
    expected = {
        "SO_KEEPALIVE": 1,
        "TCP_KEEPIDLE": 30,
        "TCP_KEEPINTVL": 10,
        "TCP_KEEPCNT": 3,
        "TCP_USER_TIMEOUT": 60_000,
    }

    dialled = far_end[0]
    try:
        for name, transport in (("accepted", accepted), ("dialled", dialled)):
            sock = transport._sock
            actual = {
                option: sock.getsockopt(
                    socket.SOL_SOCKET if option.startswith("SO_") else socket.IPPROTO_TCP,
                    getattr(socket, option),
                )
                for option in expected
            }
            assert actual == expected, (name, actual, expected)
    finally:
        accepted.close()
        dialled.close()


def test_keepalive_is_skipped_on_a_non_tcp_socket() -> None:
    """The TCP-level options do not exist on an AF_UNIX socket, so setting
    them there raises rather than being ignored."""
    left, right = socket.socketpair()
    try:
        Transport(left)     # must not raise
    finally:
        left.close()
        right.close()


TESTS = [
    test_succeeds_without_retrying_when_the_first_attempt_works,
    test_retries_a_timeout_until_one_attempt_succeeds,
    test_retry_counts_attempts_not_extra_attempts,
    test_retry_of_zero_or_one_still_attempts_once,
    test_refused_is_not_retried,
    test_other_oserrors_are_not_retried_either,
    test_the_timeout_is_passed_to_every_attempt,
    test_connect_timeout_does_not_become_a_read_timeout,
    test_closed_port_is_refused_rather_than_timing_out,
    test_real_connect_times_out_and_retries_each_attempt,
    test_listen_binds_a_kernel_chosen_loopback_port,
    test_listen_honours_an_explicit_port,
    test_accept_times_out_when_nobody_dials,
    test_accept_leaves_the_listener_open_on_both_paths,
    test_accepted_socket_has_no_read_timeout,
    test_accepted_and_dialled_transports_behave_identically,
    test_framing_errors_are_protocol_errors,
    test_an_oversized_length_is_rejected_before_buffering,
    test_a_headerless_flood_is_rejected_rather_than_buffered,
    test_shutdown_unblocks_a_pending_recv,
    test_keepalive_is_configured_on_both_constructors,
    test_keepalive_is_skipped_on_a_non_tcp_socket,
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
