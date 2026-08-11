"""DAP client: request/response correlation and event dispatch over a Transport.
"""

import json
import queue
import threading
from enum import StrEnum
from typing import Callable

from .transport import Transport
import pdvp.schema.pydevd_schema as schema
import pdvp.schema.pydevd_base_schema as base_schema

class DAPError(Exception):
    pass

class event_name(StrEnum):
    INITIALIZED     = "initialized",
    STOPPED         = "stopped",
    CONTINUED       = "continued",
    EXITED          = "exited",
    TERMINATED      = "terminated",
    THREAD          = "thread",
    OUTPUT          = "output",
    BREAKPOINT      = "breakpoint",
    MODULE          = "module",
    LOADEDSOURCE    = "loadedsource",
    PROCESS         = "process",
    CAPABILITIES    = "capabilities",
    PROGRESSSTART   = "progressstart",
    PROGRESSUPDATE  = "progressupdate",
    PROGRESSEND     = "progressend",
    INVALIDATED     = "invalidated",
    MEMORY          = "memory"

class Client:
    def __init__(self, transport: Transport):
        """Take over an already-connected `transport`.

        Construct the transport yourself -- Transport.connect() for a remote
        pydevd, Transport.accept() for one we spawned.

        Ownership transfers: Client.close() closes the transport, so don't
        close it yourself. The one exception is if this raises, since then
        the caller is the only one still holding it.

        """
        self._transport = transport

        self._seq = 0
        self._seq_lock = threading.Lock()

        self._pending: dict[int, tuple[threading.Event, schema.Response | None]] = {}
        self._pending_lock = threading.Lock()

        self.events: queue.Queue[dict] = queue.Queue()
        self.on_event: Callable[[dict], None] | None = None

        self.on_disconnect: Callable[[], None] | None = None

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def close(self) -> None:
        if self.on_disconnect is not None:
            self.on_disconnect()
        self._transport.close()

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _read_loop(self) -> None:
        while True:
            try:
                responce_str = self._transport.recv()
            except (ConnectionError):
                return

            message : schema.ProtocolMessage = base_schema.from_json(responce_str)

            if message.type == "response":
                self._handle_response(message)
            elif message.type == "event":
                self.events.put(message)
                if self.on_event is not None:
                    self.on_event(message)
            else:
                raise DAPError()

    def _handle_response(self, responce: schema.Response) -> None:
        # a server side seq handling should be processed
        with self._pending_lock:
            event, _ = self._pending.get(responce.request_seq, None)
            self._pending[responce.request_seq] = responce
            event.set()


    def request(self, request: schema.Request, timeout: float | None = None) -> schema.Response:
        """Send a request and block for its response body. Raises DAPError on failure/timeout."""
        request.seq = self._next_seq()

        event = threading.Event()
        with self._pending_lock:
            self._pending[request.seq] = (event, None)

        self._transport.send(request.to_json().encode("utf-8"))

        if not event.wait(timeout):
            raise DAPError(f"timed out waiting for response to '{command}'")

        with self._pending_lock:
            resp: schema.Response = self._pending.pop(request.seq, None)

        assert(resp != None)
        return resp

    def wait_for_event(self, event_name: event_name, timeout: float | None = None) -> schema.Event:
        """Block until an event named `event_name` arrives. Other events are kept in order.

        Raises DAPError on timeout.
        """

        return self.wait_for_events({event_name}, timeout)

    def wait_for_events(self, event_names: set[event_name], timeout: float | None = None) -> schema.Event:
        """Block until an event whose name is in `event_names` arrives.

        Returns the full message (with "event" and "body" keys). Other events
        are kept in order. Raises DAPError on timeout.
        """

        deferred = []
        try:
            while True:
                try:
                    event: schema.Event = self.events.get(timeout=timeout)
                except queue.Empty:
                    raise DAPError(f"timed out waiting for one of {sorted(event_names)!r}")
                if event.event in event_names:
                    return event
                deferred.append(event)
        finally:
            for event in deferred:
                self.events.put(event)

    # ---- session lifecycle ----

    def initialize(self, **kwargs) -> schema.InitializeResponse:
        initRequest = schema.InitializeRequest(schema.InitializeRequestArguments(
            adapterID = "pdvp",
            ClientID = "pdvp",
            ClientName = "pdvp",
            pathFormat = "path",
            linesStartAt1 = True,
            columnsStartAt1 = True,
            supportVariableType = True,
            supportRunInTerminalRequest = True
        ))

        return self.request(initRequest)

    def attach(self, **arguments) -> schema.AttachResponse:
        attach_req = schema.AttachRequest(arguments=schema.AttachRequestArguments(arguments))
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

    # ---- execution control ----

    def continue_(self, thread_id: int, single_thread: bool = False) -> schema.ContinueResponse:
        cont_req = schema.ContinueRequest(arguments=schema.ContinueArguments(
            thread_id,
            single_thread
        ))

        return self.request(cont_req)

    def next(self, thread_id: int, single_thread: bool = False, granularity: str | None = None) -> schema.ContinueResponse:
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

    # ---- inspection ----

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

    def variables(self, variables_reference: int, **kwargs) -> schema.VariablesResponse:
        variables_req = schema.VariablesRequest(arguments=schema.VariablesArguments(
            variables_reference,
            filter=None,
            start=None,
            count=None,
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

    # ---- breakpoints ----

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

    # ---- execution control extras ----

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

    # ---- inspection extras ----

    def completions(self, text: str, column: int, frame_id: int | None = None, line: int | None = None) -> schema.CompletionsResponse:
        completeions_req = schema.CompletionsRequest(arguments=schema.CompletionsArguments(
            text,
            column,
            frame_id,
            line
        ))

        return self.request(completeions_req)

    def source(self, source_reference: int, source: schema.Source | None) -> schema.SourceResponse:
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

    # ---- pydevd-specific extensions ----

    def pydevd_authorize(self, debug_server_access_token: str | None = None) -> schema.PydevdAuthorizeResponse:
        pydevd_authorize_req = schema.PydevdAuthorizeRequest(arguments=schema.PydevdAuthorizeArguments(
            debug_server_access_token
        ))

        return self.request(pydevd_authorize_req)

    def pydevd_system_info(self) -> schema.PydevdSystemInfoResponse:
        pydevd_system_info_req = schema.PydevdSystemInfoRequest(arguments=schema.PydevdSystemInfoArguments())
        return self.request(pydevd_system_info_req)

    def set_debugger_property(self) -> schema.SetDebuggerPropertyResponse:
        set_debugger_property_req = schema.SetDebuggerPropertyRequest(arguments=schema.SetDebuggerPropertyArguments(
            ideOS=None,
            dontTraceStartPatterns=None,
            dontTraceEndPatterns=None,
            skipSuspendOnBreakpointException=None,
            skipPrintBreakpointException=None,
            multiThreadsSingleNotification=None
        ))

        return self.request(set_debugger_property)

    def set_pydevd_source_map(self, source: dict, pydevd_source_maps: list[schema.PydevdSourceMap]) -> schema.SetPydevdSourceMapResponse:
        set_pydevd_source_map_req = schema.SetPydevdSourceMapRequest(arguments=schema.SetPydevdSourceMapArguments(
            source,
            pydevdSourceMaps
        ))

        return self.request(set_pydevd_source_map_req)

