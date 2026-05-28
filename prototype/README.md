# pydev-repl

A minimal interactive REPL for stepping through Python scripts line-by-line, built on top of [PyDev.Debugger](https://github.com/fabioz/PyDev.Debugger) (`pydevd`) via the [Debug Adapter Protocol](https://microsoft.github.io/debug-adapter-protocol/) (DAP).

## What it does

`pydev-repl` gives you a text prompt where you can load a Python script and step through it one source line at a time — using the same debugger engine that powers PyCharm and the PyDev Eclipse plugin.

```
$ pydev-repl
pydev-repl prototype. Type 'help' for commands.
(pydev-repl) launch examples/sample.py
paused at /path/to/sample.py:1 (breakpoint)
(pydev-repl) next
paused at /path/to/sample.py:2 (step)
(pydev-repl) next
paused at /path/to/sample.py:3 (step)
(pydev-repl) exit
session closed
```

## Setup

Requires Python 3.8+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Running

```bash
# Interactive REPL
pydev-repl

# Or directly
python -m pydev_repl.client
```

### REPL commands

| Command | Description |
|---|---|
| `launch <path>` | Start debugging the Python script at `<path>` |
| `next` | Step over to the next source line |
| `exit` / `quit` | Terminate the debuggee and close the session |
| `help` | Show command reference |

### End-to-end smoke test

```bash
python pydev_repl/test_e2e.py
```

Launches `examples/sample.py` under the debugger and steps through all lines programmatically, asserting that each step returns a `paused` event and that the script reaches `finished`.

## Architecture

```
pydev_repl/
├── client.py   — interactive REPL (entry point)
├── session.py  — high-level debug session lifecycle
├── dap.py      — low-level DAP client over TCP
└── examples/
    └── sample.py   — simple demo script
```

### `dap.py` — DAP wire client

`DapClient` wraps a TCP socket and implements the DAP wire format: `Content-Length`-framed JSON messages (`\r\n\r\n`-delimited header, UTF-8 JSON body).

A background reader thread demuxes the inbound stream into three buckets:
- **Responses** — matched to outstanding requests by `request_seq` via a `threading.Event`-based map; `request()` blocks until the matching response arrives.
- **Events** — placed on a `queue.Queue`; consumed by `wait_event()`.
- **Reverse-requests** — pydevd occasionally sends requests *to* the client (e.g. `pydevdSystemInfo`); these are silently ack'd so the adapter doesn't hang.

### `session.py` — `DebugSession`

Manages the full lifecycle of one debug run:

1. Bind an ephemeral `127.0.0.1` TCP listener on a random port.
2. Spawn `python -m pydevd --client 127.0.0.1 --port <P> --json-dap-http --file <script>`. pydevd dials back as a TCP client speaking DAP.
3. Accept the inbound connection → wrap in `DapClient`.
4. Drive the DAP handshake: `initialize` → `attach` → wait `initialized` event → `setBreakpoints` at line 1 → `configurationDone`.
5. Wait for a `stopped` event (script is now paused at line 1).

Exposed methods:
- `launch(script_path)` — performs steps 1–5, returns a `paused` dict with file/line.
- `step_over()` — sends a `next` DAP request and waits for the next `stopped`, `terminated`, or `exited` event.
- `exit()` — sends `disconnect`, closes the socket, and waits for the subprocess to terminate (kills after 3 s if needed).

### `client.py` — interactive REPL

A `readline`-driven loop (`input()`) that parses plain-text commands and delegates to `DebugSession`. Response dicts are formatted by `_fmt()` into human-readable strings before printing.
