"""DAP client: one connection, response correlation, and a single event sink.

Knows nothing about pdvp's model -- no threads-as-state, no breakpoints, no
session. It is reusable against any DAP peer, which is the test for whether
something belongs here.

Two surfaces, and no third:

    send(request) -> Pending     allocate seq, register a slot, transmit
    request(...)  -> Response    send(...).wait(), plus a check on success
    on_event                     one sink, invoked on the reader thread

There is deliberately **no event-waiting API**. Client correlates responses and
forwards events; it never blocks anyone on an event. A caller that needs to wait
for one subscribes to the event bus before sending (arm, then trigger). That is
what keeps this layer a plain DAP client -- it needs nothing above it.

`on_event` is a constructor argument rather than an attribute assigned
afterwards, because the reader thread must not be running before its sink
exists. It receives `schema.Event` for anything the peer sent, and exactly one
`ConnectionClosed` when the connection is over -- the death signal travels the
ordinary path, so nothing above needs a second channel to learn about it.

Waits here have no timeouts. A wait ends when its response arrives or the
connection dies, and the second is guaranteed: the death sweep walks the whole
response table under the registry lock, and registering is atomic with it, so a
caller cannot slip in behind the sweep and block forever.
"""
from __future__ import annotations

import json
import threading
import traceback
from dataclasses import dataclass
from typing import Any, Callable

import pdvp.schema.pydevd_base_schema as base_schema
import pdvp.schema.pydevd_schema as schema

from .transport import ProtocolError, Transport

# How long close() waits for the reader thread after shutting the socket down.
# The only timeout in this file, and it bounds teardown rather than a protocol
# wait: the reader is already unblocked by the shutdown, this just declines to
# hang forever if it is wedged in a sink callback.
READER_JOIN_TIMEOUT = 1.0

# Set to print every message crossing the wire. Off by default because it would
# scribble over the REPL prompt.
TRACE = False


class DAPError(Exception):
    """Base for everything this layer raises."""


class RequestFailed(DAPError):
    """The peer answered with `success: false`.

    Carries the response, so a caller that wants the failure as data can read
    `err.response` instead of dropping to `send(...).wait()`.
    """

    def __init__(self, response: Any):
        self.response = response
        message = getattr(response, "message", None) or "request failed"
        super().__init__(f"{getattr(response, 'command', '?')}: {message}")


class ConnectionLost(DAPError):
    """The connection ended while a request was outstanding, or before it went out."""

    def __init__(self, closed: "ConnectionClosed"):
        self.closed = closed
        super().__init__(str(closed))


@dataclass(frozen=True)
class ConnectionClosed:
    """The one death report, delivered through `on_event` exactly once.

    Not a `schema.Event`: DAP has no such event and inventing one would mean
    every consumer has to know which of its "DAP" events are actually ours.
    `deliberate` distinguishes our own close() from the peer vanishing, which
    is the difference between a clean teardown and a failure.
    """

    deliberate: bool
    detail: str = ""

    def __str__(self) -> str:
        kind = "connection closed" if self.deliberate else "connection lost"
        return f"{kind}: {self.detail}" if self.detail else kind


class Pending:
    """One outstanding request, and the slot its response will land in.

    A context manager, so a caller that gives up unregisters instead of leaving
    a slot the death sweep has to walk:

        with client.send(req) as pending:
            ...
            response = pending.wait()
    """

    __slots__ = ("seq", "_client", "_arrived", "_response", "_closed")

    def __init__(self, client: "Client", seq: int):
        self.seq = seq
        self._client = client
        self._arrived = threading.Event()
        self._response: Any = None
        self._closed: ConnectionClosed | None = None

    def wait(self) -> Any:
        """Block until the response arrives or the connection dies.

        No timeout: a protocol-sequenced wait ends because the protocol moved,
        and a dead peer is covered by the transport's keepalive. Returns the
        response whether or not it succeeded; `Client.request()` is the surface
        that raises on failure.
        """
        if threading.current_thread() is self._client._reader_thread:
            # The reader would be waiting on itself. This is the same rule as
            # "the reducer may not issue requests", caught rather than hung.
            raise DAPError("cannot wait for a response on the reader thread")

        self._arrived.wait()
        if self._response is not None:
            return self._response
        raise ConnectionLost(self._closed or ConnectionClosed(False, "no response"))

    @property
    def done(self) -> bool:
        return self._arrived.is_set()

    def close(self) -> None:
        """Give up on this request. Idempotent."""
        self._client._unregister(self.seq)

    def __enter__(self) -> "Pending":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<Pending seq={self.seq} {'done' if self.done else 'waiting'}>"

    # ---- reader side

    def _resolve(self, response: Any) -> None:
        self._response = response
        self._arrived.set()

    def _kill(self, closed: ConnectionClosed) -> None:
        self._closed = closed
        self._arrived.set()


class Client:
    def __init__(self, transport: Transport,
                 on_event: Callable[[Any], None] | None = None):
        """Take over an already-connected `transport`.

        Construct the transport yourself -- Transport.connect() for a remote
        pydevd, Transport.accept() for one we spawned.

        `on_event` is invoked on the reader thread with each `schema.Event`, and
        once with a `ConnectionClosed` when the connection ends. None drops
        events, which is only useful for a caller that does request/response and
        nothing else. It cannot be assigned after construction: the reader
        starts here, and a sink wired afterwards would miss whatever arrived in
        between.

        Ownership transfers: Client.close() closes the transport, so don't close
        it yourself. The one exception is if this raises, since then the caller
        is the only one still holding it.
        """
        self._transport = transport
        self._on_event = on_event

        # Covers the seq counter, the response table, the death record and the
        # deliberate-close flag. Seq allocation and registration have to be
        # atomic with each other or the reader can deliver a response before its
        # slot exists; registration has to be atomic with death or a caller who
        # registers just behind the sweep waits forever.
        self._registry = threading.Lock()
        self._seq = 0
        self._pending: dict[int, Pending] = {}
        self._closed: ConnectionClosed | None = None
        self._deliberate = False

        # Socket writes only. A DAP message is a header plus a body across more
        # than one write; interleaving corrupts the stream. Deliberately not the
        # registry lock -- holding that across a send would couple every pending
        # caller to socket backpressure.
        self._send_lock = threading.Lock()

        self.trace = TRACE

        self._reader_thread = threading.Thread(
            target=self._read_loop, name="pdvp-dap-reader", daemon=True)
        self._reader_thread.start()

    # ---- lifetime

    @property
    def closed(self) -> ConnectionClosed | None:
        """The death record, or None while the connection is up."""
        with self._registry:
            return self._closed

    def close(self) -> None:
        """Tear the connection down. Idempotent, and safe from any thread.

        Safe from the reader thread itself, which must not join on itself.
        """
        with self._registry:
            if self._deliberate:
                return
            # Set before the shutdown, so the reader racing to declare death
            # first still reports a local close rather than misreporting our own
            # disconnect as the peer vanishing.
            self._deliberate = True

        self._transport.shutdown()
        if threading.current_thread() is not self._reader_thread:
            self._reader_thread.join(READER_JOIN_TIMEOUT)
        self._transport.close()

        self._die("closed locally")

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- the two primitives

    def send(self, request: schema.Request) -> Pending:
        """Allocate a seq, register a slot, and transmit. Does not block."""
        with self._registry:
            if self._closed is not None:
                raise ConnectionLost(self._closed)
            self._seq += 1
            seq = self._seq
            pending = Pending(self, seq)
            self._pending[seq] = pending

        request.seq = seq
        try:
            payload = request.to_json().encode("utf-8")
        except Exception:
            self._unregister(seq)
            raise

        if self.trace:
            print(f">>> {payload!r}")

        try:
            with self._send_lock:
                self._transport.send(payload)
        except OSError as error:
            # The socket is gone; every outstanding wait, including this one,
            # has to end.
            raise ConnectionLost(self._die(f"send failed: {error}")) from error

        return pending

    def request(self, request: schema.Request) -> Any:
        """Send and block for the response, raising if the peer refused it.

        Sugar over `send(...).wait()`, plus the success check: under concurrent
        callers a failure attributed to the wrong command is worse than an
        exception, so this surface raises `RequestFailed` and the primitive
        returns the failed response for anyone who wants to inspect it.
        """
        with self.send(request) as pending:
            response = pending.wait()

        if not getattr(response, "success", True):
            raise RequestFailed(response)
        return response

    # ---- reader thread

    def _read_loop(self) -> None:
        detail = "peer closed the connection"
        try:
            while True:
                self._handle(self._transport.recv())
        except ConnectionError as error:
            detail = str(error) or "peer closed the connection"
        except ProtocolError as error:
            detail = f"protocol error: {error}"
        except OSError as error:
            detail = f"socket error: {error}"
        except Exception as error:      # a bug here must not lose the death path
            traceback.print_exc()
            detail = f"reader failed: {error!r}"
        finally:
            self._transport.shutdown()
            self._transport.close()
            self._die(detail)

    def _handle(self, raw: bytes) -> None:
        if self.trace:
            print(f"<<< {raw!r}")

        message = _decode(raw)
        kind = getattr(message, "type", None)

        if kind == "response":
            self._deliver(message)
        elif kind == "event":
            self._emit(message)
        elif kind == "request":
            # A reverse request (adapter -> client). We implement none of them,
            # and a peer waiting on an answer it never gets is worse than a
            # refusal, so refuse.
            self._refuse(message)
        else:
            raise ProtocolError(f"unknown message type: {kind!r}")

    def _deliver(self, response: Any) -> None:
        request_seq = getattr(response, "request_seq", None)
        with self._registry:
            pending = self._pending.pop(request_seq, None)

        if pending is None:
            # A response to a request whose caller gave up, or one we never
            # sent. Nothing to correlate it with; dropping it is the only
            # option, but silence would make it undiagnosable.
            print(f"pdvp: unmatched response for seq {request_seq!r}")
            return
        pending._resolve(response)

    def _refuse(self, request: Any) -> None:
        command = getattr(request, "command", "?")
        with self._registry:
            if self._closed is not None:
                return
            self._seq += 1
            seq = self._seq

        refusal = schema.Response(
            request_seq=getattr(request, "seq", -1),
            success=False,
            command=command,
            seq=seq,
            message=f"pdvp does not implement the {command!r} request",
        )
        try:
            with self._send_lock:
                self._transport.send(refusal.to_json().encode("utf-8"))
        except OSError:
            pass    # the read loop is about to notice the same thing

    def _emit(self, message: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(message)
        except Exception:
            # The sink is somebody else's code. A fault in it must not take the
            # reader down, or one bad reducer costs us the death path too.
            traceback.print_exc()

    # ---- death

    def _die(self, detail: str) -> ConnectionClosed:
        """Declare the connection over, exactly once, and end every wait.

        Returns the death record, whether this call made it or found it already
        made, so a caller can report the real cause rather than the one it
        happened to notice.
        """
        with self._registry:
            if self._closed is not None:
                return self._closed
            closed = self._closed = ConnectionClosed(self._deliberate, detail)
            pending, self._pending = self._pending, {}

        for slot in pending.values():
            slot._kill(closed)
        self._emit(closed)
        return closed

    def _unregister(self, seq: int) -> None:
        with self._registry:
            self._pending.pop(seq, None)

    # ---- session lifecycle

    def initialize(self) -> schema.InitializeResponse:
        return self.request(schema.InitializeRequest(schema.InitializeRequestArguments(
            adapterID="pdvp",
            clientID="pdvp",
            clientName="pdvp",
            pathFormat="path",
            linesStartAt1=True,
            columnsStartAt1=True,
            supportsVariableType=True,
            supportsRunInTerminalRequest=True,
        )))

    def attach(self, **arguments) -> schema.AttachResponse:
        # AttachRequestArguments declares only __restart; everything else is
        # implementation-specific and rides in **kwargs, which to_dict() emits
        # verbatim. Passing the dict positionally would ship it as __restart.
        attach_req = schema.AttachRequest(
            arguments=schema.AttachRequestArguments(**arguments))
        return self.request(attach_req)

    def configuration_done(self) -> schema.ConfigurationDoneResponse:
        conf_done_req = schema.ConfigurationDoneRequest(arguments=schema.ConfigurationDoneArguments())
        return self.request(conf_done_req)

    def disconnect(self, terminate_debuggee: bool | None = None) -> schema.DisconnectResponse:
        disconnect_req = schema.DisconnectRequest(arguments=schema.DisconnectArguments(terminateDebuggee=terminate_debuggee))
        return self.request(disconnect_req)

    def terminate(self, restart: bool | None = None) -> schema.TerminateResponse:
        terminate_req = schema.TerminateRequest(arguments=schema.TerminateArguments(restart))
        return self.request(terminate_req)

    # ---- execution control

    def continue_(self, thread_id: int, single_thread: bool = False) -> schema.ContinueResponse:
        cont_req = schema.ContinueRequest(arguments=schema.ContinueArguments(
            thread_id,
            single_thread
        ))

        return self.request(cont_req)

    def next(self, thread_id: int, single_thread: bool = False, granularity: str | None = None) -> schema.NextResponse:
        next_req = schema.NextRequest(arguments=schema.NextArguments(
            thread_id,
            single_thread,
            granularity
        ))

        return self.request(next_req)

    def step_in(self, thread_id: int, single_thread: bool = False, target_id: int | None = None, granularity: str | None = None) -> schema.StepInResponse:
        stepIn_req = schema.StepInRequest(arguments=schema.StepInArguments(
            thread_id,
            single_thread,
            target_id,
            granularity
        ))

        return self.request(stepIn_req)

    def step_out(self, thread_id: int, single_thread: bool = False, granularity: str | None = None) -> schema.StepOutResponse:
        stepOut_req = schema.StepOutRequest(arguments=schema.StepOutArguments(
            thread_id,
            single_thread,
            granularity
        ))
        return self.request(stepOut_req)

    def pause(self, thread_id: int) -> schema.PauseResponse:
        pause_req = schema.PauseRequest(arguments=schema.PauseArguments(
            thread_id
        ))
        return self.request(pause_req)

    def pause_async(self, thread_id: int) -> Pending:
        """`pause` without waiting for the reply.

        What ends a blocked resume is the `stopped` event, not this response,
        so interrupt() has nothing to gain by waiting -- and must not wait, since
        it runs in a signal handler on the very thread it would be blocking.
        """
        return self.send(schema.PauseRequest(arguments=schema.PauseArguments(
            thread_id
        )))

    # ---- inspection

    def threads(self) -> schema.ThreadsResponse:
        return self.request(schema.ThreadsRequest())

    def stack_trace(self, thread_id: int, start_frame: int | None = None, levels: int | None = None) -> schema.StackTraceResponse:
        stack_trace_req = schema.StackTraceRequest(arguments=schema.StackTraceArguments(
            thread_id,
            start_frame,
            levels,
            format=None
        ))

        return self.request(stack_trace_req)

    def scopes(self, frame_id: int) -> schema.ScopesResponse:
        scopes_req = schema.ScopesRequest(arguments=schema.ScopesArguments(
            frame_id
        ))

        return self.request(scopes_req)

    def variables(self, variables_reference: int, filter: str | None = None,
                  start: int | None = None, count: int | None = None) -> schema.VariablesResponse:
        variables_req = schema.VariablesRequest(arguments=schema.VariablesArguments(
            variables_reference,
            filter,
            start,
            count,
            format=None
        ))

        return self.request(variables_req)

    def set_variable(self, variables_reference: int, name: str, value: str) -> schema.SetVariableResponse:
        set_variable_req = schema.SetVariableRequest(arguments=schema.SetVariableArguments(
            variables_reference,
            name,
            value,
            format=None
        ))

        return self.request(set_variable_req)

    def set_expression(self, expression: str, value: str, frame_id: int | None = None) -> schema.SetExpressionResponse:
        set_expr_req = schema.SetExpressionRequest(arguments=schema.SetExpressionArguments(
            expression,
            value,
            frame_id,
            format=None
        ))

        return self.request(set_expr_req)

    def evaluate(self, expression: str, frame_id: int | None = None, context: str | None = None) -> schema.EvaluateResponse:
        evaluate_req = schema.EvaluateRequest(arguments=schema.EvaluateArguments(
            expression,
            frame_id,
            context,
            format=None
        ))

        return self.request(evaluate_req)

    def exception_info(self, thread_id: int) -> schema.ExceptionInfoResponse:
        exception_info_req = schema.ExceptionInfoRequest(arguments=schema.ExceptionInfoArguments(
            thread_id
        ))

        return self.request(exception_info_req)

    # ---- breakpoints

    def set_breakpoints(self, source: schema.Source, breakpoints: list[schema.SourceBreakpoint]) -> schema.SetBreakpointsResponse:
        set_breakpoints_req = schema.SetBreakpointsRequest(arguments=schema.SetBreakpointsArguments(
            source,
            breakpoints,
            lines=None,
            sourceModified=False
        ))

        return self.request(set_breakpoints_req)

    def set_function_breakpoints(self, fbreakpoints: list[schema.FunctionBreakpoint]) -> schema.SetFunctionBreakpointsResponse:
        set_function_breakpoints_req = schema.SetFunctionBreakpointsRequest(arguments=schema.SetFunctionBreakpointsArguments(
            fbreakpoints
        ))

        return self.request(set_function_breakpoints_req)

    def set_exception_breakpoints(self,
            filters: list[str],
            filter_options: list[schema.ExceptionFilterOptions],
            exception_options: list[schema.ExceptionOptions]
        ) -> schema.SetExceptionBreakpointsResponse:

        set_exception_breakpoints_req = schema.SetExceptionBreakpointsRequest(arguments=schema.SetExceptionBreakpointsArguments(
            filters,
            filter_options,
            exception_options
        ))

        return self.request(set_exception_breakpoints_req)

    # ---- execution control extras

    def step_in_targets(self, frame_id: int) -> schema.StepInTargetsResponse:
        step_in_targets_req = schema.StepInTargetsRequest(arguments=schema.StepInTargetsArguments(
            frame_id
        ))

        return self.request(step_in_targets_req)

    def goto_targets(self, source: schema.Source, line: int, column: int | None = None) -> schema.GotoTargetsResponse:
        goto_targets_req = schema.GotoTargetsRequest(arguments=schema.GotoTargetsArguments(
            source,
            line,
            column
        ))

        return self.request(goto_targets_req)

    def goto(self, thread_id: int, target_id: int) -> schema.GotoResponse:
        goto_req = schema.GotoRequest(arguments=schema.GotoArguments(
            thread_id,
            target_id
        ))

        return self.request(goto_req)

    # ---- inspection extras

    def completions(self, text: str, column: int, frame_id: int | None = None, line: int | None = None) -> schema.CompletionsResponse:
        completeions_req = schema.CompletionsRequest(arguments=schema.CompletionsArguments(
            text,
            column,
            frame_id,
            line
        ))

        return self.request(completeions_req)

    def source(self, source_reference: int, source: schema.Source | None = None) -> schema.SourceResponse:
        source_req = schema.SourceRequest(arguments=schema.SourceArguments(
            source_reference,
            source
        ))

        return self.request(source_req)

    def modules(self, start_module: int | None = None, module_count: int | None = None) -> schema.ModulesResponse:
        modules_req = schema.ModulesRequest(arguments=schema.ModulesArguments(
            start_module,
            module_count
        ))

        return self.request(modules_req)

    # ---- pydevd-specific extensions

    def pydevd_authorize(self, debug_server_access_token: str | None = None) -> schema.PydevdAuthorizeResponse:
        pydevd_authorize_req = schema.PydevdAuthorizeRequest(arguments=schema.PydevdAuthorizeArguments(
            debug_server_access_token
        ))

        return self.request(pydevd_authorize_req)

    def pydevd_system_info(self) -> schema.PydevdSystemInfoResponse:
        pydevd_system_info_req = schema.PydevdSystemInfoRequest(arguments=schema.PydevdSystemInfoArguments())
        return self.request(pydevd_system_info_req)

    def set_debugger_property(self,
            ide_os: str | None = None,
            dont_trace_start_patterns: list[str] | None = None,
            dont_trace_end_patterns: list[str] | None = None,
            skip_suspend_on_breakpoint_exception: list[str] | None = None,
            skip_print_breakpoint_exception: list[str] | None = None,
            multi_threads_single_notification: bool | None = None,
        ) -> schema.SetDebuggerPropertyResponse:
        """The mid-session mode switch, among other pydevd knobs.

        `multi_threads_single_notification` is all-stop when true, non-stop when
        false -- the one property pydevd lets us change after attach.
        """
        set_debugger_property_req = schema.SetDebuggerPropertyRequest(arguments=schema.SetDebuggerPropertyArguments(
            ideOS=ide_os,
            dontTraceStartPatterns=dont_trace_start_patterns,
            dontTraceEndPatterns=dont_trace_end_patterns,
            skipSuspendOnBreakpointException=skip_suspend_on_breakpoint_exception,
            skipPrintBreakpointException=skip_print_breakpoint_exception,
            multiThreadsSingleNotification=multi_threads_single_notification
        ))

        return self.request(set_debugger_property_req)

    def set_pydevd_source_map(self, source: schema.Source, pydevd_source_maps: list[schema.PydevdSourceMap]) -> schema.SetPydevdSourceMapResponse:
        set_pydevd_source_map_req = schema.SetPydevdSourceMapRequest(arguments=schema.SetPydevdSourceMapArguments(
            source,
            pydevd_source_maps
        ))

        return self.request(set_pydevd_source_map_req)


def _decode(raw: bytes) -> Any:
    """Parse one framed message into a schema object.

    Falls back to the generic Request/Response/Event class when the peer sends a
    command or event this schema has no type for. A core that grew a message we
    do not know about is not a reason to drop the connection; only framing we
    cannot parse at all is.
    """
    try:
        as_dict = json.loads(raw)
    except ValueError as error:
        raise ProtocolError(f"malformed JSON: {error}") from None

    if not isinstance(as_dict, dict) or "type" not in as_dict:
        raise ProtocolError(f"not a DAP message: {raw[:200]!r}")

    try:
        return base_schema.from_dict(as_dict)
    except Exception:
        pass

    kind = as_dict.get("type")
    try:
        if kind == "event":
            return schema.Event(**as_dict)
        if kind == "response":
            return schema.Response(**as_dict)
        if kind == "request":
            return schema.Request(**as_dict)
    except TypeError as error:
        raise ProtocolError(f"malformed {kind}: {error}") from None

    raise ProtocolError(f"unknown message type: {kind!r}")
