# REPL execution model: avoiding a blocked interpreter loop

## Problem

The current `cont()`/`step()`/`next()`/`finish()`/`interrupt()` implementations
send a DAP request and then **block** the whole Python interpreter on
`wait_for_any_event({"stopped", "exited", "terminated"})`. This is wrong for
two reasons:

1. **It can hang forever.** pydevd doesn't always emit `exited`/`terminated`
   for a debuggee that finishes on its own (only on explicit
   `disconnect`/`terminate`). A 30s timeout band-aids this but is arbitrary,
   and the `wait()` escape hatch is not how any real debugger UI works.
2. **It blocks the whole REPL**, including things that don't need the
   debuggee to be stopped at all (e.g. checking `threads()`, sending
   `interrupt()`, or just typing the next command while a long-running
   program executes). The user can't do anything else until pydevd happens
   to report back.

Neither gdb nor VS Code's debug UI work this way:

- **gdb CLI**: `continue` *does* block the CLI thread, but Ctrl+C delivers
  SIGINT, which gdb's signal handler turns into "stop the inferior and return
  to the prompt" — i.e. blocking is escapable on demand.
- **gdb/MI** (the protocol IDEs actually use) and **VS Code's DAP client**:
  resume commands (`continue`, `next`, ...) return **immediately** with an
  acknowledgement. The actual stop is reported later via an async
  out-of-band record/event (`*stopped` in MI, the `stopped` DAP event in VS
  Code) that the frontend renders whenever it arrives — the UI is never
  blocked waiting for it.


## Approach 1 — Blocking + Ctrl+C interrupt (gdb CLI style)

`cont()` etc. keep blocking on `wait_for_any_event(...)`, but install a
`SIGINT` handler that, on Ctrl+C, sends `pause()` to pydevd (or just gives up
waiting) and returns control to the prompt.

- **Pros**: minimal change to current code/control flow; "stopped" reporting
  stays synchronous and easy to reason about.
- **Cons**: directly violates "the interpreter loop should never be blocked"
  — you still can't run *any* other command (not even `threads()` or
  `p(...)`) while waiting. Mixing `signal` handlers with a background reader
  thread and `python -i`'s own input loop is also fiddly (signals are
  delivered to the main thread only; `event.wait()` needs to be interruptible,
  which `threading.Event.wait()` is, but `queue.Queue.get()` with a SIGINT
  needs care to not eat the KeyboardInterrupt silently).
- **Verdict**: rejected — doesn't meet the stated requirement.


## Approach 2 — Non-blocking resume + async event dispatcher (gdb/MI, VS Code style) — RECOMMENDED

`cont()`/`step()`/`next()`/`finish()` send the DAP request and return
**immediately** (e.g. printing `"continuing"`/`"stepping"`). A background
dispatcher (piggybacking on the existing `DAPClient` reader thread, or a
second thread draining `client.events`) handles `stopped` / `continued` /
`exited` / `terminated` / `output` events as they arrive and:

- prints an async notification, e.g.
  `\n*** stopped (breakpoint) at calc.py:2, in inner\n`
- updates `SESSION.current_thread_id` / `current_frame_id` / a new
  `SESSION.running: bool` flag

Inspection commands (`bt`, `frame`, `p`, `locals`) check `SESSION.running`
first and print `"error: program is running"` instead of issuing a request
pydevd can't answer yet (stackTrace/evaluate/scopes/variables all require the
target to be paused). `threads()`, `interrupt()`, breakpoint commands work
regardless of run state (pydevd's command thread answers these even while
running).

- **Pros**: interpreter loop is never blocked, by construction — matches the
  "VS Code button" model exactly. `interrupt()` becomes a real, always-available
  "pause" rather than a Ctrl+C hack. No timeouts/`wait()` needed at all;
  removes the whole hung-`wait()` problem class.
- **Cons**:
  - Async print output can interleave with whatever the user is typing —
    same as a shell job-control `[1]+ Done ...` message. Cosmetic, not
    fixable without a real TUI (acceptable for now, gdb has the same issue
    with background-thread stop notifications).
  - Requires a small `DAPClient` addition: a way to react to events as they
    arrive rather than only via `wait_for_event`/`wait_for_any_event`
    (currently pull-based via `events` queue). Cleanest option: an optional
    `on_event` callback invoked synchronously from `_read_loop` for every
    event message, in addition to (not instead of) queueing — existing
    `wait_for_event`-based tests keep working unchanged.
  - Connection-drop handling needs to be explicit: when `_read_loop` exits on
    `ConnectionError`/`OSError`, it must notify the dispatcher (e.g. call
    `on_event` with a synthetic `{"event": "_disconnected"}`, or a separate
    `on_disconnect` callback) so `SESSION.dap`/`SESSION.running` get cleared
    and the user sees `"connection to pydevd lost"` instead of silence.


## No `wait()`

Approach 2 already makes a blocking primitive unnecessary: `cont()` etc.
return immediately, `SESSION.running` reflects current state, and
`interrupt()` is itself non-blocking (just sends `pause()`). A user scripting
a one-off scenario who wants "block until stopped, but give up after N
seconds" can write that themselves in a couple of lines using public state —
e.g. poll `SESSION.running` in a loop with `time.sleep`, and call
`interrupt()` if a deadline passes. That's their timer, their policy, no
framework primitive needed. So: no `wait()`, no `_RESUME_TIMEOUT`, no
`_wait_for_stop_or_exit` timeout-by-default — resume commands never wait at
all.


## Keyboard interrupts (Ctrl+C) — gdb-style

gdb's Ctrl+C behavior is the right model:

- **Inferior running** (gdb's `continue` is blocking the CLI): Ctrl+C sends
  SIGINT to gdb, which stops the inferior and returns to the prompt with a
  "Program received signal SIGINT" notification.
- **At the prompt** (inferior stopped or not running): Ctrl+C just cancels
  whatever's being typed and redraws the prompt — a no-op for the debuggee.

Under Approach 2 the REPL prompt is *always* available (never blocked), so
"inferior running" isn't a separate blocking state — it's just
`SESSION.running == True` at the moment Ctrl+C arrives. Mapping:

- `start_eval()` installs a `signal.signal(signal.SIGINT, handler)`.
- **handler**: if `SESSION.dap is not None and SESSION.running`, send
  `pause()` (fire-and-forget, like `interrupt()`) and **return normally**
  (don't raise). The interrupted `input()` call gets `EINTR` and keeps
  waiting for a line — the prompt doesn't visibly change. The async
  dispatcher prints the `stopped (pause)` notification once pydevd responds,
  same as any other async stop.
- **else** (not running / not connected): call
  `signal.default_int_handler(...)` to raise `KeyboardInterrupt` as usual —
  `python -i`'s loop catches it, prints `KeyboardInterrupt`, and redraws the
  prompt. This cancels whatever the user was typing, matching gdb's
  "Ctrl+C at prompt" no-op.

This gives exactly the gdb mental model: Ctrl+C either "stops the program" or
"cancels my typing", depending on whether the program is currently running —
without ever blocking the loop either way.


## Stopping/killing the session: local vs. remote

`stop()` currently assumes we spawned the debuggee ourselves
(`SESSION.process`) and just `kill()`s it. Once `connect()` supports a
**remote** pydevd (one we never spawned — no `SESSION.process`), "stop
everything" means different things:

- **Local session** (`run()` spawned `SESSION.process`, which *is* the
  debuggee — pydevd traces in-process, not via a child): the most reliable
  "kill everything" is still `child.kill()` + `child.wait()` (the
  `subprocess.Popen.wait()` reap call already used in `stop()` today — an
  internal implementation detail, *not* the rejected REPL `wait()` command).
  If a DAP session is attached, send `disconnect(terminateDebuggee=True)`
  first (best-effort — ignore errors) so pydevd shuts down cleanly, then
  `kill()`/`wait()` as a fallback/guarantee regardless.
- **Remote session** (`connect()` only, no `SESSION.process`): there is **no
  local process to kill** — confirmed, not possible in general (could be a
  different host entirely). The only lever is the DAP protocol itself:
  - `disconnect()` → `disconnect(terminateDebuggee=False)`: detach, leave the
    remote debuggee running. Always available.
  - `terminate()` → DAP `terminate` request: pydevd implements
    `on_terminate_request` and will terminate the debuggee on its end. This
    is the closest thing to "stop everything" for a remote session, and works
    for local sessions too.

So the three commands end up as:

- `disconnect()` — detach only, debuggee keeps running. Local or remote.
- `terminate()` — ask pydevd to terminate the debuggee via DAP. Local or
  remote. (Already exists as a thin `DAPClient.terminate()` wrapper; just
  needs exposing as a REPL command.)
- `stop()` — "kill everything *we* started": if `SESSION.process` is set,
  terminate (DAP, best-effort) + `kill()` the child + clear `SESSION.process`.
  If `SESSION.process` is `None` (remote-only), `stop()` prints an error
  pointing at `terminate()`/`disconnect()` instead — there is nothing local
  for it to do.


## Recommendation

**Approach 2**, with the Ctrl+C handler and `disconnect`/`terminate`/`stop`
split described above. Concretely:

1. `DAPClient`: add `on_event: Callable[[dict], None] | None` (settable
   after construction or via `connect()`), called synchronously from
   `_read_loop` for every event message (queueing for `wait_for_event` stays
   as-is, used by tests). Add `on_disconnect: Callable[[], None] | None`
   called when `_read_loop` exits.
2. `SESSION` gains `running: bool = False`.
3. `commands.py`:
   - `connect()` registers `on_event`/`on_disconnect` handlers that update
     `SESSION.running`/`current_thread_id`/`current_frame_id` and print
     stop/exit/disconnect notifications.
   - `cont()`/`step()`/`next()`/`finish()` send the request and return
     immediately (`SESSION.running = True`, print `"continuing"`/etc.).
   - `interrupt()` sends `pause()` and returns immediately.
   - `bt()`/`frame()`/`p()`/`locals()`/`set_variable` etc. check
     `SESSION.running` and bail with an error if the program isn't paused.
   - `disconnect()`/`terminate()`/`stop()` per the local-vs-remote split
     above.
4. `start_eval()` installs the Ctrl+C handler described above.
