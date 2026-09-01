"""Unit tests for `Client`/`Pending` against a fake `Transport` -- no socket,
no real pydevd.

Three files in this directory have similar names and cover different ground:
`test_client_events.py` and `test_dap_client.py` are both end-to-end against a
real pydevd (event/lifecycle behaviour and the request method surface,
respectively); this file is the one level below that -- the races and wire
edge cases that a real process makes slow or outright unreliable to trigger on
purpose (two sends racing seq allocation, a death sweep racing concurrent
callers, a peer that frames garbage). `FakeTransport` stands in for
`Transport`: `deliver()`/`deliver_raw()` queue what `recv()` hands the reader
thread next, `kill()` queues an exception instead, and `send()` records what
`Client` wrote instead of doing I/O, which is also how the refusal
`Client._refuse()` sends for a reverse request gets inspected below.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all
of them. pytest also collects these directly by name.

Run from the repo root with the venv active:

    python -m pdvp.dap.test.test_client
"""
import json
import queue
import threading
import time

from pdvp.schema import pydevd_schema as schema
from pdvp.dap.client import Client, ConnectionClosed, ConnectionLost, DAPError, RequestFailed

# Every wait here bounds a failure rather than a race -- nothing inside pdvp
# itself passes a timeout, but a hung test should fail loudly, not hang the
# suite.
WAIT = 10


class FakeTransport:
    """An in-process stand-in for `Transport`: a controllable queue instead
    of a socket, so `Client`'s reader thread can be driven one message at a
    time from the test's own thread."""

    def __init__(self) -> None:
        self._inbox: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self.sent: list[dict] = []
        self.shutdown_count = 0
        self.close_count = 0

    def deliver(self, message: dict) -> None:
        self.deliver_raw(json.dumps(message).encode("utf-8"))

    def deliver_raw(self, raw: bytes) -> None:
        self._inbox.put(("msg", raw))

    def kill(self, exc: BaseException) -> None:
        """Make the next recv() raise `exc`, simulating a peer that died."""
        self._inbox.put(("exc", exc))

    # ---- Transport's surface, exactly what Client calls

    def send(self, message: bytes) -> None:
        with self._lock:
            self.sent.append(json.loads(message))

    def recv(self) -> bytes:
        kind, payload = self._inbox.get()
        if kind == "exc":
            raise payload
        return payload

    def shutdown(self) -> None:
        self.shutdown_count += 1
        # The real Transport.shutdown() unblocks a recv() already in flight by
        # tearing the socket down under it; queuing a ConnectionError is the
        # same effect without a real fd.
        self._inbox.put(("exc", ConnectionError("shutdown")))

    def close(self) -> None:
        self.close_count += 1


# ---- message builders: just enough to exercise Client, not full coverage of
# every schema field.

def _response(request_seq: int, *, success: bool = True, command: str = "threads",
              seq: int = 1, message: str | None = None, body: dict | None = None) -> dict:
    d = {"type": "response", "request_seq": request_seq, "success": success,
         "command": command, "seq": seq}
    if message is not None:
        d["message"] = message
    if body is not None:
        d["body"] = body
    return d


def _threads_body(*ids: int) -> dict:
    return {"threads": [{"id": i, "name": f"t{i}"} for i in ids]}


def _event(name: str, *, seq: int = 1, body: dict | None = None) -> dict:
    d = {"type": "event", "event": name, "seq": seq}
    if body is not None:
        d["body"] = body
    return d


def _output_event(text: str, *, seq: int = 1) -> dict:
    return _event("output", seq=seq, body={"output": text})


def _client(on_event=None) -> tuple[Client, FakeTransport]:
    transport = FakeTransport()
    return Client(transport, on_event=on_event), transport


def _wait_for(predicate, timeout: float = WAIT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")


# ---- seq allocation / registration

def test_send_allocates_and_registers_distinct_seqs_under_concurrent_callers() -> None:
    client, transport = _client()
    try:
        barrier = threading.Barrier(2)
        pendings: list = [None, None]

        def worker(i: int) -> None:
            barrier.wait(timeout=WAIT)
            pendings[i] = client.send(schema.ThreadsRequest())

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT)

        seqs = [p.seq for p in pendings]
        assert len(set(seqs)) == 2, seqs

        for i, p in enumerate(pendings):
            transport.deliver(_response(p.seq, body=_threads_body(100 + i)))

        for i, p in enumerate(pendings):
            response = p.wait()
            assert response.body.threads[0]["id"] == 100 + i, response.body.threads
    finally:
        client.close()


# ---- death sweep

def test_close_racing_concurrent_requests_never_hangs() -> None:
    """The documented guarantee: nobody can slip in behind the death sweep
    and block forever. Run repeatedly since it is a race, not a repro."""
    for _ in range(20):
        client, transport = _client()
        try:
            results: list[str] = []
            lock = threading.Lock()

            def worker() -> None:
                try:
                    client.request(schema.ThreadsRequest())
                    outcome = "response"
                except ConnectionLost:
                    outcome = "lost"
                with lock:
                    results.append(outcome)

            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            client.close()
            for t in threads:
                t.join(WAIT)

            assert all(not t.is_alive() for t in threads), "a caller hung past close()"
            assert len(results) == 5, results
            # Nothing was ever delivered, so every caller must have lost.
            assert results == ["lost"] * 5, results
        finally:
            client.close()


# ---- reader-thread self-wait guard

def test_wait_from_inside_on_event_is_refused_not_hung() -> None:
    caught: list[Exception] = []
    pending_box: list = []

    def on_event(message) -> None:
        if getattr(message, "event", None) == "output":
            try:
                pending_box[0].wait()
            except DAPError as e:
                caught.append(e)

    client, transport = _client(on_event=on_event)
    try:
        pending_box.append(client.send(schema.ThreadsRequest()))
        transport.deliver(_output_event("trigger"))
        _wait_for(lambda: len(caught) == 1)
        assert "reader thread" in str(caught[0]), caught[0]

        transport.deliver(_response(pending_box[0].seq, body=_threads_body(1)))
        pending_box[0].wait()
    finally:
        client.close()


# ---- unmatched response

def test_unmatched_response_is_dropped_not_fatal() -> None:
    client, transport = _client()
    try:
        transport.deliver(_response(99999, body=_threads_body(1)))  # nobody sent seq 99999

        pending = client.send(schema.ThreadsRequest())
        transport.deliver(_response(pending.seq, body=_threads_body(2)))
        response = pending.wait()
        assert response.body.threads[0]["id"] == 2, response.body.threads
        assert client.closed is None
    finally:
        client.close()


# ---- reverse request refusal

def test_reverse_request_is_refused_and_the_reader_survives() -> None:
    client, transport = _client()
    try:
        transport.deliver({"type": "request", "seq": 1, "command": "runInTerminal", "arguments": {}})

        def _refused() -> bool:
            return any(m.get("type") == "response" and m.get("request_seq") == 1 for m in transport.sent)
        _wait_for(_refused)

        refusal = next(m for m in transport.sent if m.get("request_seq") == 1)
        assert refusal["success"] is False, refusal
        assert "runInTerminal" in refusal["message"], refusal

        pending = client.send(schema.ThreadsRequest())
        transport.deliver(_response(pending.seq, body=_threads_body(7)))
        response = pending.wait()
        assert response.body.threads[0]["id"] == 7, response.body.threads
    finally:
        client.close()


# ---- decode fallback chain

def test_a_known_event_reaches_on_event() -> None:
    received: list = []
    client, transport = _client(on_event=lambda m: received.append(m))
    try:
        transport.deliver(_output_event("hello"))
        _wait_for(lambda: len(received) == 1)
        assert received[0].event == "output", received[0]
        assert received[0].body.output == "hello", received[0]
    finally:
        client.close()


def test_an_unmodelled_event_falls_back_to_the_generic_type_not_a_crash() -> None:
    received: list = []
    client, transport = _client(on_event=lambda m: received.append(m))
    try:
        transport.deliver(_event("somethingPdvpDoesNotModel", body={"anything": 1}))
        _wait_for(lambda: len(received) == 1)
        assert getattr(received[0], "event", None) == "somethingPdvpDoesNotModel", received[0]

        # The reader survived decoding it: a legit request afterwards still resolves.
        pending = client.send(schema.ThreadsRequest())
        transport.deliver(_response(pending.seq, body=_threads_body(3)))
        assert pending.wait().body.threads[0]["id"] == 3
    finally:
        client.close()


def test_malformed_json_is_a_protocol_error_and_ends_the_connection() -> None:
    client, transport = _client()
    try:
        transport.deliver_raw(b"not json at all")
        _wait_for(lambda: client.closed is not None)
        assert "protocol error" in client.closed.detail, client.closed

        try:
            client.request(schema.ThreadsRequest())
        except ConnectionLost:
            pass
        else:
            raise AssertionError("request() succeeded on a dead connection")
    finally:
        client.close()


def test_a_message_with_no_type_field_is_also_a_protocol_error() -> None:
    client, transport = _client()
    try:
        transport.deliver({"command": "threads"})  # valid JSON, no "type"
        _wait_for(lambda: client.closed is not None)
        assert "protocol error" in client.closed.detail, client.closed
    finally:
        client.close()


# ---- close()

def test_close_is_idempotent() -> None:
    seen: list = []
    client, transport = _client(on_event=lambda m: seen.append(m))
    client.close()
    client.close()
    closes = [m for m in seen if isinstance(m, ConnectionClosed)]
    assert len(closes) == 1, closes


def test_close_is_safe_to_call_from_inside_on_event() -> None:
    """The documented guarantee: close() must not join the reader thread on
    itself when called from inside a sink callback running on that thread."""
    seen: list = []
    triggered = threading.Event()
    client_box: list = []

    def on_event(message) -> None:
        seen.append(message)
        if getattr(message, "event", None) == "output":
            client_box[0].close()
            triggered.set()

    client, transport = _client(on_event=on_event)
    client_box.append(client)
    try:
        transport.deliver(_output_event("trigger"))
        _wait_for(triggered.is_set)

        client._reader_thread.join(WAIT)
        assert not client._reader_thread.is_alive(), "reader thread did not terminate"

        closes = [m for m in seen if isinstance(m, ConnectionClosed)]
        assert len(closes) == 1, seen
    finally:
        client.close()


# ---- RequestFailed / request() vs send()

def test_request_raises_request_failed_with_the_peers_message() -> None:
    client, transport = _client()
    result: dict = {}

    def worker() -> None:
        try:
            client.request(schema.ThreadsRequest())
        except Exception as e:
            result["error"] = e

    try:
        t = threading.Thread(target=worker)
        t.start()
        _wait_for(lambda: len(transport.sent) >= 1)
        seq = transport.sent[-1]["seq"]
        transport.deliver(_response(seq, success=False, message="nope, try later"))
        t.join(WAIT)

        assert isinstance(result.get("error"), RequestFailed), result
        assert str(result["error"]) == "threads: nope, try later", str(result["error"])
    finally:
        client.close()


def test_request_failed_falls_back_to_a_generic_message_when_the_peer_gives_none() -> None:
    client, transport = _client()
    result: dict = {}

    def worker() -> None:
        try:
            client.request(schema.ThreadsRequest())
        except Exception as e:
            result["error"] = e

    try:
        t = threading.Thread(target=worker)
        t.start()
        _wait_for(lambda: len(transport.sent) >= 1)
        seq = transport.sent[-1]["seq"]
        transport.deliver(_response(seq, success=False))  # no message key at all
        t.join(WAIT)

        assert isinstance(result.get("error"), RequestFailed), result
        assert str(result["error"]) == "threads: request failed", str(result["error"])
    finally:
        client.close()


def test_send_wait_does_not_raise_on_failure_only_request_does() -> None:
    client, transport = _client()
    try:
        pending = client.send(schema.ThreadsRequest())
        transport.deliver(_response(pending.seq, success=False, message="nope"))
        response = pending.wait()  # the primitive: no raise
        assert response.success is False, response
    finally:
        client.close()


TESTS = [value for name, value in sorted(globals().items()) if name.startswith("test_")]


def main() -> int:
    failures = 0
    for test in TESTS:
        try:
            test()
        except Exception as error:
            failures += 1
            print(f"FAIL {test.__name__}: {type(error).__name__}: {error}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
