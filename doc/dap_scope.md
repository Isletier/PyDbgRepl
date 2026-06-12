# DAP scope for pydev-repl

DAP.json is the full Microsoft Debug Adapter Protocol schema (192 definitions,
44 requests, 17 events). pydevd implements a subset of it (plus a few of its
own extensions). This doc maps that subset against what pydev-repl actually
needs, so we don't build client support for things pydevd will just reject or
that don't fit a single local CLI session.

Source for "what pydevd implements": `_pydevd_bundle/pydevd_process_net_command_json.py`
(`on_<x>_request` handlers) and `_pydevd_bundle/pydevd_net_command_factory_json.py`
(events actually emitted), in the installed `pydevd` package / `../PyDev.Debugger`.


## 1. Transport (always needed)

Standard DAP framing: `Content-Length: N\r\n\r\n<json body>` over a TCP socket,
both directions. `--json-dap-http` (already forced in `OBLIGATORY_RUN_ARGUMENTS`)
makes pydevd speak this. This is the only framing/protocol layer to implement —
no need to support `--json-dap` (raw JSON, no framing) or the legacy XML
protocols.


## 2. Session lifecycle — needed first

These form the mandatory handshake for any session, in order:

- **initialize** (request/response) — client capabilities in, adapter
  capabilities out. pydevd's response capabilities (see §5) tell us what else
  is safe to call.
- **attach** (request) — pydev-repl always *attaches* (we already spawned the
  process via `--file ... --server`; pydevd is waiting for a DAP connection,
  it didn't ask DAP to launch anything). `launch` (DAP request, distinct from
  our `run()` command) is NOT relevant — pydevd's `launch` handler exists but
  our flow never uses it.
- **initialized** (event) — sent by pydevd *after* the `attach` response
  (`_handle_launch_or_attach_request` in `pydevd_process_net_command_json.py`),
  not after `initialize` as the DAP spec's general framing suggests. Signals
  the client may now send `setBreakpoints`/`setExceptionBreakpoints`/
  `configurationDone`. Verified against a live pydevd instance via
  `src/dap/test/smoke_test.py`.
- **configurationDone** (request) — tells pydevd to let the program actually
  start running after breakpoints are configured. Required.
- **disconnect** / **terminate** (requests) — ending the session. Both are
  implemented by pydevd; `terminate` is closer to gdb's "kill inferior",
  `disconnect` detaches without necessarily killing (depends on
  `terminateDebuggee` arg). Both relevant for `stop()`/`connect()` teardown.


## 3. Execution control — needed soon (continue/step commands)

All implemented by pydevd, all directly map to REPL commands:

- **continue** → `continue_()`/`cont()`
- **next** → `next`/`step over`
- **stepIn** → `step`/`step into`
- **stepOut** → `finish`/`step out`
- **pause** → `interrupt`
- **stepInTargets** — supported by pydevd (`supportsStepInTargetsRequest`),
  used when `stepIn` is ambiguous (multiple call targets on one line). Nice
  to have, not blocking for v1.
- **goto** / **gotoTargets** — supported (`supportsGotoTargetsRequest`), lets
  you jump execution to another line. Edge-case gdb-like feature, low
  priority.

Events to react to:

- **stopped** (event) — the big one: program hit a breakpoint/step/exception/
  pause. Carries `reason`, `threadId`, etc. Drives the REPL's "we're now
  paused, here's where" state.
- **continued** (event) — counterpart, program resumed.


## 4. Inspection — needed for variable/stack access ("p expr", "bt", "locals")

All implemented by pydevd:

- **threads** — list threads (needed even for single-threaded scripts; pydevd
  always reports at least the main thread).
- **stackTrace** — frames for a thread. Supports
  `supportsDelayedStackTraceLoading` (paginate via `startFrame`/`levels`).
- **scopes** — variable scopes (locals/globals) for a frame.
- **variables** — expand a scope/variable reference into its children.
- **setVariable** — assign to a variable (supported).
- **setExpression** — assign via arbitrary expression (supported).
- **evaluate** — the core "eval an expression in frame N" — this is most of
  what a REPL *is*. Supports `supportsEvaluateForHovers` and
  `supportsClipboardContext`.
- **exceptionInfo** — details of the exception that caused a stop (supported).
- **completions** — tab-completion for expressions in the debuggee's
  namespace (supported) — could back REPL autocompletion later.
- **source** — fetch source for frames where the file isn't locally
  available (supported, mostly relevant for remote/embedded sources).
- **modules** — list loaded modules (supported,
  `supportsModulesRequest`) — informational, low priority.


## 5. Breakpoints — needed for any non-trivial debugging

All implemented by pydevd:

- **setBreakpoints** — line breakpoints, with conditions/hit-conditions/log
  points all supported (`supportsConditionalBreakpoints`,
  `supportsHitConditionalBreakpoints`, `supportsLogPoints`).
- **setFunctionBreakpoints** — supported.
- **setExceptionBreakpoints** — supported, with `exceptionBreakpointFilters`
  reported in `initialize` response (raised/uncaught exceptions etc).
- **breakpoint** (event) — pydevd can push breakpoint-verification updates;
  handle but not critical for v1.


## 6. pydevd-specific extensions (not in DAP.json at all)

pydevd adds a few of its own request/event types on top of standard DAP.
Worth knowing about, not needed for v1:

- **pydevdAuthorize** (request) — token-based auth handshake, only relevant
  if `--access-token`/`--client-access-token` are used. We don't set those.
- **pydevdSystemInfo** (request) — process/python/platform info dump.
  Informational.
- **setDebuggerProperty** (request) — pydevd-specific runtime config (e.g.
  `dontTraceStartEndPatterns`, `multiThreadsSingleNotification`). Optional
  tuning knob for later.
- **setPydevdSourceMap** (request) — source-map support for non-Python
  generated code (e.g. Robot Framework). Not relevant to us.
- **pydevdInputRequested** (event) — fired when the debuggee calls `input()`.
  This is the hook for [[project_pty_io_forwarding]] — even though stdin
  itself goes through the PTY, this event tells the REPL *when* the inferior
  is blocked on input, useful for prompt-switching. Worth handling once PTY
  passthrough work starts; not needed for the DAP client's first cut.


## 7. Out of scope — pydevd implements but doesn't fit our use case

Implemented by pydevd, but not useful for a single local CLI debug session
(skip for now, revisit only if a concrete need shows up):

- **launch** (DAP request) — we never ask pydevd to launch via DAP; we spawn
  the process ourselves and `attach`.


## 8. Out of scope — pydevd does NOT implement (don't bother modeling)

Standard DAP requests with no `on_..._request` handler in pydevd at all —
sending these would just get a generic "not implemented" error response:

- **breakpointLocations**, **cancel**, **dataBreakpointInfo**,
  **setDataBreakpoints** (`supportsDataBreakpoints=False`)
- **disassemble**, **readMemory**, **writeMemory**
  (`supportsDisassembleRequest=False`, `supportsReadMemoryRequest=False`)
- **loadedSources** (`supportsLoadedSourcesRequest=False`)
- **restart**, **restartFrame** (`supportsRestartRequest=False`,
  `supportsRestartFrame=False`)
- **reverseContinue**, **stepBack** (`supportsStepBack=False`)
- **runInTerminal**, **startDebugging** — these are *adapter→client* reverse
  requests for spawning terminals/sub-sessions (used by VS Code-style
  multi-process debugging); pydevd doesn't initiate them in our setup.
- **setInstructionBreakpoints** — disassembly-related, n/a.
- **terminateThreads** (`supportsTerminateThreadsRequest=False`)

Standard DAP events pydevd never emits:

- **breakpointLocations**-related, **capabilities** (capabilities only ever
  sent once, in the `initialize` response body, not as a separate event),
  **invalidated**, **loadedSource**, **memory**, **progressStart/Update/End**.


## Summary: what the v1 client needs to support

Requests: `initialize`, `attach`, `configurationDone`, `setBreakpoints`,
`setFunctionBreakpoints`, `setExceptionBreakpoints`, `continue`, `next`,
`stepIn`, `stepOut`, `pause`, `threads`, `stackTrace`, `scopes`, `variables`,
`setVariable`, `setExpression`, `evaluate`, `exceptionInfo`, `disconnect`,
`terminate`.

Events to dispatch: `initialized`, `stopped`, `continued`, `output`,
`thread`, `module`, `exited`, `terminated`, `breakpoint`,
`pydevdInputRequested` (custom).

Everything else in DAP.json (~20 more request types, several event types) can
be left as "unsupported, falls through to a generic dict" — the transport/
client layer should be generic enough that adding one later is just a new
convenience method, not a structural change.
