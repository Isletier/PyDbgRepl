"""DAP client: request/response correlation and event dispatch over DAPTransport.

Covers the v1 request/event subset documented in reference/dap_scope.md.
"""
import json
import queue
import threading

from .transport import DAPTransport


class DAPError(Exception):
    pass


class DAPClient:
    def __init__(self, transport: DAPTransport):
        self._transport = transport
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self._pending_lock = threading.Lock()
        self.events: queue.Queue[dict] = queue.Queue()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    @classmethod
    def connect(cls, host: str, port: int) -> "DAPClient":
        return cls(DAPTransport.connect(host, port))

    def close(self) -> None:
        self._transport.close()

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _read_loop(self) -> None:
        while True:
            try:
                message = self._transport.recv()
            except (ConnectionError, OSError):
                break
            except json.JSONDecodeError:
                # pydevd occasionally sends a stray legacy (non-JSON) command
                # through the same Content-Length framing, e.g. an internal
                # "Console" pseudo-thread suspend notification. Ignore it.
                continue
            if message.get("type") == "response":
                self._handle_response(message)
            elif message.get("type") == "event":
                self.events.put(message)
            # adapter->client reverse requests are out of scope for v1

    def _handle_response(self, message: dict) -> None:
        with self._pending_lock:
            pending = self._pending.pop(message["request_seq"], None)
        if pending is None:
            return
        event, holder = pending
        holder["response"] = message
        event.set()

    def request(self, command: str, arguments: dict | None = None, timeout: float | None = None) -> dict:
        """Send a request and block for its response body. Raises DAPError on failure/timeout."""
        seq = self._next_seq()
        message: dict = {"seq": seq, "type": "request", "command": command}
        if arguments is not None:
            message["arguments"] = arguments

        event = threading.Event()
        holder: dict = {}
        with self._pending_lock:
            self._pending[seq] = (event, holder)

        self._transport.send(message)

        if not event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(seq, None)
            raise DAPError(f"timed out waiting for response to '{command}'")

        response = holder["response"]
        if not response.get("success", False):
            raise DAPError(response.get("message") or f"'{command}' request failed")
        return response.get("body") or {}

    def wait_for_event(self, event_name: str, timeout: float | None = None) -> dict:
        """Block until an event named `event_name` arrives. Other events are kept in order.

        Raises DAPError on timeout.
        """
        return self.wait_for_any_event({event_name}, timeout)["body"]

    def wait_for_any_event(self, event_names: set[str], timeout: float | None = None) -> dict:
        """Block until an event whose name is in `event_names` arrives.

        Returns the full message (with "event" and "body" keys). Other events
        are kept in order. Raises DAPError on timeout.
        """
        deferred = []
        try:
            while True:
                try:
                    message = self.events.get(timeout=timeout)
                except queue.Empty:
                    raise DAPError(f"timed out waiting for one of {sorted(event_names)!r}")
                if message.get("event") in event_names:
                    message["body"] = message.get("body") or {}
                    return message
                deferred.append(message)
        finally:
            for message in deferred:
                self.events.put(message)

    # ---- session lifecycle ----

    def initialize(self, **kwargs) -> dict:
        arguments = {
            "clientID": "pydev-repl",
            "clientName": "pydev-repl",
            "adapterID": "pydevd",
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "supportsVariableType": True,
            "supportsRunInTerminalRequest": False,
            **kwargs,
        }
        return self.request("initialize", arguments)

    def attach(self, **arguments) -> dict:
        return self.request("attach", arguments)

    def configuration_done(self) -> dict:
        return self.request("configurationDone")

    def disconnect(self, terminate_debuggee: bool | None = None) -> dict:
        arguments = {}
        if terminate_debuggee is not None:
            arguments["terminateDebuggee"] = terminate_debuggee
        return self.request("disconnect", arguments)

    def terminate(self, restart: bool | None = None) -> dict:
        arguments = {}
        if restart is not None:
            arguments["restart"] = restart
        return self.request("terminate", arguments)

    # ---- execution control ----

    def continue_(self, thread_id: int, single_thread: bool = False) -> dict:
        return self.request("continue", {"threadId": thread_id, "singleThread": single_thread})

    def next(self, thread_id: int, single_thread: bool = False, granularity: str | None = None) -> dict:
        arguments = {"threadId": thread_id, "singleThread": single_thread}
        if granularity is not None:
            arguments["granularity"] = granularity
        return self.request("next", arguments)

    def step_in(
        self,
        thread_id: int,
        single_thread: bool = False,
        target_id: int | None = None,
        granularity: str | None = None,
    ) -> dict:
        arguments = {"threadId": thread_id, "singleThread": single_thread}
        if target_id is not None:
            arguments["targetId"] = target_id
        if granularity is not None:
            arguments["granularity"] = granularity
        return self.request("stepIn", arguments)

    def step_out(self, thread_id: int, single_thread: bool = False, granularity: str | None = None) -> dict:
        arguments = {"threadId": thread_id, "singleThread": single_thread}
        if granularity is not None:
            arguments["granularity"] = granularity
        return self.request("stepOut", arguments)

    def pause(self, thread_id: int) -> dict:
        return self.request("pause", {"threadId": thread_id})

    # ---- inspection ----

    def threads(self) -> dict:
        return self.request("threads")

    def stack_trace(self, thread_id: int, start_frame: int | None = None, levels: int | None = None) -> dict:
        arguments = {"threadId": thread_id}
        if start_frame is not None:
            arguments["startFrame"] = start_frame
        if levels is not None:
            arguments["levels"] = levels
        return self.request("stackTrace", arguments)

    def scopes(self, frame_id: int) -> dict:
        return self.request("scopes", {"frameId": frame_id})

    def variables(self, variables_reference: int, **kwargs) -> dict:
        return self.request("variables", {"variablesReference": variables_reference, **kwargs})

    def set_variable(self, variables_reference: int, name: str, value: str) -> dict:
        return self.request(
            "setVariable",
            {"variablesReference": variables_reference, "name": name, "value": value},
        )

    def set_expression(self, expression: str, value: str, frame_id: int | None = None) -> dict:
        arguments = {"expression": expression, "value": value}
        if frame_id is not None:
            arguments["frameId"] = frame_id
        return self.request("setExpression", arguments)

    def evaluate(self, expression: str, frame_id: int | None = None, context: str | None = None) -> dict:
        arguments = {"expression": expression}
        if frame_id is not None:
            arguments["frameId"] = frame_id
        if context is not None:
            arguments["context"] = context
        return self.request("evaluate", arguments)

    def exception_info(self, thread_id: int) -> dict:
        return self.request("exceptionInfo", {"threadId": thread_id})

    # ---- breakpoints ----

    def set_breakpoints(self, source: dict, breakpoints: list[dict] | None = None) -> dict:
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
