"""DAP client: request/response correlation and event dispatch over DAPTransport.

Covers the v1 request/event subset documented in doc/dap_scope.md.
"""
import json
import queue
import threading
from enum import StrEnum
from typing import Callable

from .transport import DAPTransport
import pdvp.schema.pydevd_schema as dap
import pdvp.schema.pydevd_base_schema as dap_base

class DAPError(Exception):
    pass

class dap_event_name(StrEnum):
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

class DAPClient:
    def __init__(self, transport: DAPTransport):
        self._transport = transport

        self._seq = 0
        self._seq_lock = threading.Lock()

        self._pending: dict[int, tuple[threading.Event, dap.Response | None]] = {}
        self._pending_lock = threading.Lock()

        self.events: queue.Queue[dict] = queue.Queue()
        self.on_event: Callable[[dict], None] | None = None

        self.on_disconnect: Callable[[], None] | None = None

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    @classmethod
    def connect(cls, host: str, port: int) -> "DAPClient":
        return cls(DAPTransport.connect(host, port))

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
                message : dap.ProtocolMessage = dap_base.from_json(responce_str)
            except Exception as e:
                self.close()
                raise e

            if message.type == "response":
                self._handle_response(message)
            elif message.type == "event":
                self.events.put(message)
                if self.on_event is not None:
                    self.on_event(message)
            else:
                raise DAPError()

    def _handle_response(self, responce: dap.Response) -> None:
        # a server side seq handling should be processed
        with self._pending_lock:
            event, _ = self._pending.get(responce.request_seq, None)
            self._pending[responce.request_seq] = responce
            event.set()


    def request(self, request: dap.Request, timeout: float | None = None) -> dap.Response:
        """Send a request and block for its response body. Raises DAPError on failure/timeout."""
        request.seq = self._next_seq()

        event = threading.Event()
        with self._pending_lock:
            self._pending[request.seq] = (event, None)

        self._transport.send(request.to_json().encode("utf-8"))

        if not event.wait(timeout):
            raise DAPError(f"timed out waiting for response to '{command}'")

        with self._pending_lock:
            resp = self._pending.pop(request.seq, None)

        return resp

    def wait_for_event(self, event_name: dap_event_name, timeout: float | None = None) -> dap.Event:
        """Block until an event named `event_name` arrives. Other events are kept in order.

        Raises DAPError on timeout.
        """

        return self.wait_for_events({event_name}, timeout)

    def wait_for_events(self, event_names: set[dap_event_name], timeout: float | None = None) -> dap.Event:
        """Block until an event whose name is in `event_names` arrives.

        Returns the full message (with "event" and "body" keys). Other events
        are kept in order. Raises DAPError on timeout.
        """

        deferred = []
        try:
            while True:
                try:
                    event: dap.Event = self.events.get(timeout=timeout)
                    print(event)
                except queue.Empty:
                    raise DAPError(f"timed out waiting for one of {sorted(event_names)!r}")
                if event.event in event_names:
                    return event
                deferred.append(event)
        finally:
            for event in deferred:
                self.events.put(event)

    # ---- session lifecycle ----

    def initialize(self, **kwargs) -> dap.InitializeResponse:
        initRequest = dap.InitializeRequest(dap.InitializeRequestArguments(
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

    def attach(self, **arguments) -> dap.AttachResponse:
        attach_req = dap.AttachRequest(arguments=dap.AttachRequestArguments(arguments))
        return self.request(attach_req)

    def configuration_done(self) -> dap.ConfigurationDoneResponse:
        conf_done_req = dap.ConfigurationDoneRequest(arguments=dap.ConfigurationDoneArguments())
        return self.request(conf_done_req)

    def disconnect(self, terminate_debuggee: bool | None = None) -> dap.DisconnectResponse:
        disconnect_req = dap.DisconnectRequest(arguments=dap.DisconnectArguments(terminateDebuggee=terminate_debuggee))
        return self.request(disconnect_req)

    def terminate(self, restart: bool | None = None) -> dap.TerminateResponse:
        terminate_req = dap.TerminateRequest(arguments=dap.TerminateArguments(restart))
        return self.request(terminate_req)

    # ---- execution control ----

    def continue_(self, thread_id: int, single_thread: bool = False) -> dap.ContinueResponse:
        cont_req = dap.ContinueRequest(arguments=dap.ContinueArguments(
            thread_id,
            single_thread
        ))

        return self.request(cont_req)

    def next(self, thread_id: int, single_thread: bool = False, granularity: str | None = None) -> dap.ContinueResponse:
        next_req = dap.NextRequest(arguments=dap.NextArguments(
            thread_id,
            single_thread,
            granularity
        ))

        return self.request(next_req)

    def step_in(self, thread_id: int, single_thread: bool = False, target_id: int | None = None, granularity: str | None = None) -> dap.StepInResponse:
        stepIn_req = dap.StepInRequest(arguments=dap.StepInArguments(
            thread_id,
            single_thread,
            target_id,
            granularity
        ))

        return self.request(stepIn_req)

    def step_out(self, thread_id: int, single_thread: bool = False, granularity: str | None = None) -> dap.StepOutResponse:
        stepOut_req = dap.StepOutRequest(arguments=dap.StepOutArguments(
            thread_id,
            single_thread,
            granularity
        ))
        return self.request(stepOut_req)

    def pause(self, thread_id: int) -> dap.PauseResponse:
        pause_req = dap.PauseRequest(arguments=dap.PauseArguments(
            thread_id
        ))
        return self.request(pause_req)

    # ---- inspection ----

    def threads(self) -> dap.ThreadsResponse:
        return self.request(dap.ThreadsRequest())

    def stack_trace(self, thread_id: int, start_frame: int | None = None, levels: int | None = None) -> dap.StackTraceResponse:
        stack_trace_req = dap.StackTraceRequest(arguments=dap.StackTraceArguments(
            thread_id,
            start_frame,
            levels,
            format=None
        ))

        return self.request(stack_trace_req)

    def scopes(self, frame_id: int) -> dap.ScopesResponse:
        scopes_req = dap.ScopesRequest(arguments=dap.ScopesArguments(
            frame_id
        ))

        return self.request(scopes_req)

    def variables(self, variables_reference: int, **kwargs) -> dap.VariablesResponse:
        variables_req = dap.VariablesRequest(arguments=dap.VariablesArguments(
            variables_reference,
            filter=None,
            start=None,
            count=None,
            format=None
        ))

        return self.request(variables_req)

    def set_variable(self, variables_reference: int, name: str, value: str) -> dap.SetVariableResponse:
        set_variable_req = dap.SetVariableRequest(arguments=dap.SetVariableArguments(
            variables_reference,
            name,
            value,
            format=None
        ))

        return self.request(set_variable_req)

    def set_expression(self, expression: str, value: str, frame_id: int | None = None) -> dap.SetExpressionResponse:
        set_expr_req = dap.SetExpressionRequest(arguments=dap.SetExpressionArguments(
            expression,
            value,
            frame_id,
            format=None
        ))

        return self.request(set_expr_req)

    def evaluate(self, expression: str, frame_id: int | None = None, context: str | None = None) -> dap.EvaluateResponse:
        evaluate_req = dap.EvaluateRequest(arguments=dap.EvaluateArguments(
            expression,
            frame_id,
            context,
            format=None
        ))

        return self.request(evaluate_req)

    def exception_info(self, thread_id: int) -> dap.ExceptionInfoResponse:
        exception_info_req = dap.ExceptionInfoRequest(arguments=dap.ExceptionInfoArguments(
            thread_id
        ))

        return self.request(exception_info_req)

    # ---- breakpoints ----

    def set_breakpoints(self, source: dict, breakpoints: list[dict] | None = None) -> dict:
        set_breakpoints_req = dap.SetBreakpointsRequest(arguments=dap.SetBreakpointsArguments(source, breakpoints,
        arguments = {"source": source}
        if breakpoints is not None:
            arguments["breakpoints"] = breakpoints
        return self.request("setBreakpoints", arguments)

    def set_function_breakpoints(self, breakpoints: list[dict]) -> dict:
        return self.request("setFunctionBreakpoints", {"breakpoints": breakpoints})

    def set_exception_breakpoints(self, filters: list[str], filter_options: list[dict] | None = None) -> dict:
        arguments = {"filters": filters}
        if filter_options is not None:
            arguments["filterOptions"] = filter_options
        return self.request("setExceptionBreakpoints", arguments)

    # ---- execution control extras ----

    def step_in_targets(self, frame_id: int) -> dict:
        return self.request("stepInTargets", {"frameId": frame_id})

    def goto_targets(self, source: dict, line: int, column: int | None = None) -> dict:
        arguments = {"source": source, "line": line}
        if column is not None:
            arguments["column"] = column
        return self.request("gotoTargets", arguments)

    def goto(self, thread_id: int, target_id: int) -> dict:
        return self.request("goto", {"threadId": thread_id, "targetId": target_id})

    # ---- inspection extras ----

    def completions(self, text: str, column: int, frame_id: int | None = None, line: int | None = None) -> dict:
        arguments = {"text": text, "column": column}
        if frame_id is not None:
            arguments["frameId"] = frame_id
        if line is not None:
            arguments["line"] = line
        return self.request("completions", arguments)

    def source(self, source_reference: int, source: dict | None = None) -> dict:
        arguments = {"sourceReference": source_reference}
        if source is not None:
            arguments["source"] = source
        return self.request("source", arguments)

    def modules(self, start_module: int | None = None, module_count: int | None = None) -> dict:
        arguments = {}
        if start_module is not None:
            arguments["startModule"] = start_module
        if module_count is not None:
            arguments["moduleCount"] = module_count
        return self.request("modules", arguments)

    # ---- pydevd-specific extensions ----

    def pydevd_authorize(self, debug_server_access_token: str | None = None) -> dict:
        arguments = {}
        if debug_server_access_token is not None:
            arguments["debugServerAccessToken"] = debug_server_access_token
        return self.request("pydevdAuthorize", arguments)

    def pydevd_system_info(self) -> dict:
        return self.request("pydevdSystemInfo")

    def set_debugger_property(self, **kwargs) -> dict:
        return self.request("setDebuggerProperty", kwargs)

    def set_pydevd_source_map(self, source: dict, pydevd_source_maps: list[dict]) -> dict:
        return self.request("setPydevdSourceMap", {"source": source, "pydevdSourceMaps": pydevd_source_maps})
