# Inferior I/O model

How the debuggee's stdin/stdout/stderr relate to pydev-repl's own terminal,
across the two session kinds (`run()` vs `connect()`) and the two I/O modes
(default PTY-pair passthrough vs `--pty <device>` external-terminal
redirect). Builds on [[project_sync_execution_model]] (the blocking
`cont()`/`step()`/... model) and supersedes the open questions in
[[project_pty_io_forwarding]].


## The two axes

|                          | **local (`run()`)** | **remote (`connect()`)** |
|---|---|---|
| We spawned the debuggee  | yes — `SESSION.process` | no |
| We hold an fd to its stdio | yes (`master_fd`, or the `--pty` device) | no, never |
| `--pty <device>` applies | yes | no (ignored, see below) |

`connect()` attaches to a pydevd instance we did not start. Its stdin/stdout/
stderr are wired to *whatever launched it* — a different terminal, a log
file, `/dev/null`, possibly on a different host entirely. pydev-repl has no
fd-level access to any of that, full stop. Everything below except the last
section ("Remote sessions") is about `run()`.


## Default mode: owned PTY pair, passthrough during blocking calls

This is the current/default behavior (`--pty` not given).

`spawn_pydevd` (`src/launch.py`) does `pty.openpty()` and gives the slave end
to the child as stdin/stdout/stderr; we keep `master_fd`. Two things happen
with `master_fd`:

1. **Output — continuous, already implemented.** A background thread
   (`_stream_output`, started once in `run()`) copies `master_fd -> our
   stdout` for the whole lifetime of the process. This runs *regardless* of
   whether the inferior is paused or running — a background thread in the
   debuggee printing while we're sitting at the `(paused) >>>` prompt shows
   up immediately, same as it would in gdb. No change needed here.

2. **Input — not yet implemented.** Nothing currently writes to `master_fd`,
   so `input()` in the debuggee blocks forever with no way to satisfy it.
   pydevd's `pydevdInputRequested` event is purely informational (no DAP
   request can deliver stdin) — the only way in is writing raw bytes to
   `master_fd`, i.e. PTY passthrough.

### Scoping the passthrough to blocking resume calls

Per [[project_sync_execution_model]], `cont()`/`step()`/`next()`/`finish()`/
the initial resume in `run()`/`connect()` already **block** the main thread in
`_wait_for_resume_result()`. That's exactly the window where the inferior
might call `input()` and where forwarding our stdin makes sense — and exactly
when our own `input()`-based prompt loop is *not* running, so there's no
contention for stdin.

Plan: `_wait_for_resume_result()` starts a passthrough thread before blocking
on `wait_for_any_event(...)` and stops it when that returns:

- **stdin -> master_fd**: read raw bytes from `sys.stdin.fileno()`, write to
  `master_fd`.
- The existing `master_fd -> stdout` thread keeps running unchanged (it's not
  scoped — see point 1 above).
- On return (stopped/exited/terminated/disconnected), signal the passthrough
  thread to stop and join it before printing `*** stopped (...)`/etc., so
  REPL output doesn't interleave with a half-read passthrough iteration.
  (`select()` with a short timeout, or a self-pipe added to the `select()`
  set, gives a clean way to interrupt the blocking read.)

### Terminal mode: cbreak, not raw — this is the key decision

While the passthrough thread is active, our stdin needs `tty.setcbreak()`
(disables `ICANON`/`ECHO`, **keeps `ISIG`**), not full raw mode:

- **`ECHO` off**: the inferior's own pty slave has `ECHO` on by default, so
  characters we forward get echoed back to us via the `master_fd -> stdout`
  thread. If our side also echoed, every keystroke would appear twice.
- **`ICANON` off**: characters reach `master_fd` as the user types them
  (unbuffered), which is what an interactive `input()` in the debuggee
  expects — no waiting for our terminal's line buffer to flush on Enter.
- **`ISIG` stays on**: Ctrl+C continues to raise `SIGINT` in *our* process via
  the normal terminal line discipline, hitting the existing
  `_sigint_handler` -> `interrupt()` (DAP `pause`) path unchanged. This is
  the escape hatch back to the prompt while the passthrough thread is
  running and the main thread is otherwise blocked — without it there would
  be no way to get pydev-repl's attention back while `cont()` is in flight
  and the debuggee is in `input()`.

  (Ctrl+C is *not* forwarded as a raw byte to the inferior in this mode —
  it always means "ask pydev-repl to pause", same as today. An inferior that
  wants to catch its own `SIGINT`/`KeyboardInterrupt` will get it indirectly
  once `pause()` stops it — same as gdb's `interrupt()`, not a passthrough of
  the raw byte.)

Restore the previous `termios` settings (cooked mode, with our own readline
prompt) when the passthrough thread stops, before returning control to the
REPL loop.

### Prompt switching

`_PydevPromptStyle` (ptpython) already renders `(running)` vs `(paused)`.
Plain readline mode has no live prompt during a blocking call anyway (the
call hasn't returned), so there's nothing to redraw — the `(running)`/
`(paused)` distinction in ptpython is sufficient and no separate "inferior
has the terminal" indicator is needed beyond that.

### Indicating "the inferior is reading your input" via `pydevdInputRequested`

Even with the passthrough thread running, the user has no way to tell *when*
their keystrokes are actually going to the debuggee vs. just being buffered
for nothing (the inferior may be mid-computation, not at an `input()` call
yet). pydevd's `pydevdInputRequested` event (`started: bool`) fires exactly
when the debuggee enters/leaves `sys.stdin.read()`/`readline()` —
[[project_pty_io_forwarding]] previously dismissed this event as useless
*for delivering input* (correct — it carries no data, passthrough is still
required), but it's exactly the right signal for a **UI indicator**, which is
a separate concern:

- On `pydevdInputRequested(started=true)`: print a one-line marker via
  `_async_print`, e.g. `"--- debuggee is waiting for input ---"`, so the user
  knows the next keystrokes go to the inferior's `input()`, not to a buffered
  pydev-repl command.
- On `pydevdInputRequested(started=false)`: print a matching close, e.g.
  `"--- input received, resuming ---"`.
- In ptpython mode, additionally flip `_PydevPromptStyle` to a third state
  (e.g. `"(inferior input)"`) for the duration, reverting on `started=false`
  or whenever the passthrough thread stops (whichever comes first — the
  debuggee could also be killed/interrupted mid-`input()`).

This requires `client.on_event` to be wired up for the duration of the
passthrough thread (it's otherwise unused, per the dap_scope.md review) —
scope the handler narrowly to `pydevdInputRequested` so it doesn't interact
with the `wait_for_any_event`/deferred-queue mechanism used for
`stopped`/`exited`/`terminated`.

Not applicable to `--pty <device>` mode (no passthrough thread, no ambiguity
about where keystrokes go) or to `connect()` (event not observable without
`output`/general event wiring, and there's no stdin path regardless).


## `--pty <device>`: external-terminal redirect (gdb `inferior-tty` style)

For cases where mixing REPL output and inferior output on one terminal is
undesirable even momentarily — e.g. the debuggee is a TUI, or prints enough
that interleaving with `*** stopped ...` notifications is confusing.

**Usage**: in another terminal, run `tty` to get its device path (e.g.
`/dev/pts/7`), leave that terminal otherwise idle, then:

```python
debug.set("pty", "/dev/pts/7")
debug.run("script.py")
```

or `--pty /dev/pts/7` on the command line.

**Implementation** (`spawn_pydevd`): if `pty` is set, `os.open(path,
os.O_RDWR)` and pass that single fd as the child's stdin **and** stdout
**and** stderr (`subprocess.Popen(..., stdin=fd, stdout=fd, stderr=fd)`).
This covers *both directions* — the answer to "ideally rewire output too" is
yes, opening the target tty device for all three standard fds redirects the
inferior's entire stdio to that terminal, same as gdb's `tty` command (which
also redirects all three).

**Consequences**:

- `pty.openpty()` is **not** called; there is no `master_fd`.
- The `_stream_output` background thread is **not** started — there is
  nothing on our side to read.
- The stdin-passthrough thread described above is **not** started —
  `_wait_for_resume_result()` just blocks on `wait_for_any_event(...)` as it
  did before any PTY work, with no terminal-mode juggling at all.
- The inferior's terminal (the other pty) is completely independent: its own
  line discipline, its own Ctrl+C (delivered to the inferior's foreground
  process group by the kernel via that pty's `ISIG`, nothing to do with
  pydev-repl), its own echo. pydev-repl's terminal is never touched.

**Caveats** (same as gdb's `tty`):

- The target device must not have another foreground process reading from it
  (e.g. a shell prompt actively blocked on `read()`) — that process and the
  debuggee would race for input.
- Permissions: the target pty must be owned by / accessible to the user
  running pydev-repl (true for ptys you opened yourself via another terminal
  in the same session).
- If the target terminal is closed while the debuggee is running, the
  debuggee gets `SIGHUP`/`EIO` on its next I/O — same as any process whose
  controlling terminal disappears. Not specially handled.

**`--pty` + `connect()`**: `connect()` never spawns a process, so there is
nothing for `--pty` to apply to. If `pty` is set and the user calls
`connect()` without a prior local `run()`, it's simply unused — no error,
just inert (mirrors `set()`'s general "config now, used by `run()` later"
semantics). Document it as a no-op rather than special-casing an error.


## Remote sessions (`connect()`): hard limits

No `SESSION.process`, no fd of any kind to the debuggee's stdio — this is
fundamental, not a missing feature:

- **stdout/stderr**: pydevd can optionally be made to emit DAP `output`
  events (§3/§6 of `dap_scope.md`) carrying captured stdout/stderr text. This
  would be the *only* way to see remote inferior output through pydev-repl
  itself. Not currently wired up (`client.on_event` unused — see the
  follow-up note in the dap_scope.md review). Low priority: usually the
  remote debuggee's output is already visible wherever it was launched
  (its own terminal/log).
- **stdin**: there is no DAP request that delivers stdin to the debuggee.
  `pydevdInputRequested` is purely a notification (`started=true/false`).
  **Not possible**, by design of the protocol — not a gap pydev-repl can
  close. If a remote `input()` call blocks, the only fix is on the machine
  actually running the debuggee.

`--pty` is meaningless here for the same reason it's a no-op above: there is
no local process to attach a device to.


## Summary

| Mode | stdout/stderr | stdin | Passthrough thread | Terminal mode changes |
|---|---|---|---|---|
| `run()`, default (no `--pty`) | `master_fd -> our stdout`, continuous bg thread | `our stdin -> master_fd`, scoped to blocking resume calls | yes, started/stopped per resume call | `cbreak` (ISIG kept) during passthrough |
| `run()`, `--pty <device>` | direct to external tty | direct to external tty | none | none — our terminal untouched |
| `connect()` | not visible (or via `output` events, future work) | not possible | none | none |
