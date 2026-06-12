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

**Verified caveats (real limitations, not just theoretical)**:

- `pydevdInputRequested` is gated behind pydevd's `--skip-notify-stdin` flag
  (`patch_stdin()` is skipped if set). It was previously in
  `OBLIGATORY_RUN_ARGUMENTS` (always passed), which silenced the event
  entirely — **removed** so `patch_stdin()` runs (its default, `skip-notify-
  stdin=False`, is what we want).
- Even with `patch_stdin()` active, **`input()` does not trigger this event
  when stdin/stdout are both ttys** — true for both our default mode and
  `--pty <device>`. CPython's `input()` builtin bypasses `sys.stdin` entirely
  via `PyOS_Readline` (reading the fd directly) whenever `isatty()` is true
  on both streams, so pydevd's `sys.stdin` wrapper (which is what emits the
  event) is never invoked. **In practice this means the indicator will not
  fire for the common case of a script calling `input()`.**
- It *does* fire for an inferior calling `sys.stdin.read()`/`readline()` or
  `getpass.getpass()` directly (these go through pydevd's wrapped
  `sys.stdin`, regardless of tty-ness).
- The installed pydevd's JSON `make_input_requested_message` sends `body:
  {}` for both the "started" and "finished" notifications — the documented
  `started: bool` field is never populated. `_on_input_requested` falls back
  to toggling `SESSION.awaiting_input` on each occurrence (using `started`
  from the body if a future pydevd version actually sends it).

Net effect: this is implemented as a low-cost best-effort indicator for the
narrower `sys.stdin.readline()`/`getpass` case, but is **not** a general
solution to "tell me when `input()` is waiting" — for plain `input()` over a
pty, the only feedback the user has is the passthrough itself being active
(no special prompt/marker).

### Job control and TUI/curses limitations of default mode

The default mode gives the inferior its *own* pty, separate from ours, with
only raw bytes shuttled between the two. This has two real, user-visible
consequences — not edge cases, but everyday limitations worth knowing about:

- **No job control for the inferior.** Ctrl+Z does not suspend it (there is
  no `SIGTSTP` path to the inferior's pty — Ctrl+Z, like every other key,
  only has special meaning on *our* tty, where `ISIG` maps it to whatever our
  terminal driver does with *us*, not the inferior). Likewise, resizing your
  terminal window does **not** propagate: the inferior's pty keeps whatever
  size it was created with (`pty.openpty()`'s default, generally inherited
  from ours at spawn time but never updated afterwards), so `SIGWINCH` /
  `TIOCGWINSZ` are stale for the lifetime of the session.
- **Curses/raw-mode TUI applications will visually clash with pydev-repl's
  own output.** `_stream_output` copies the inferior's pty bytes verbatim to
  our stdout — including ANSI escape sequences for cursor positioning,
  alternate-screen-buffer switches, etc. Our terminal interprets those
  sequences as applying to *our* screen (where the REPL prompt lives), not
  some isolated sub-region. A curses app and pydev-repl's prompt are fighting
  over the same physical screen with no coordination. **There is no fix for
  this within default mode** — it's an inherent consequence of multiplexing
  two ttys' output onto one physical terminal via raw byte copying.

**`--pty <device>` is the answer to both**, and is the recommended (only
sane) way to debug a TUI/curses application or anything that needs real job
control — see below. This isn't a workaround so much as the correct tool:
gdb users reach for `tty`/`inferior-tty` for exactly the same reason.

### pydevd's own startup chatter on the debuggee's stdio

Separate from the REPL-vs-inferior multiplexing above: pydevd itself writes
a couple of lines directly to the debuggee process's real stdout/stderr fd
(not via DAP `output` events) during its own startup, *before* the user's
script runs:

- `"<N>s - Debugger warning: It seems that frozen modules are being
  used..."` — via `pydev_log.critical` (`pydevd_file_utils.py`). Respects
  `--log-file`/`PYDEVD_DEBUG_FILE` (redirects pydev_log's stream to a file)
  and `PYDEVD_DISABLE_FILE_VALIDATION` (downgrades it to debug level,
  suppressed at the default `log_level`).
- `"pydevd: waiting for connection at: HOST:PORT"` — `pydevd_comm.
  start_server`, an **unconditional `print(msg, file=sys.stderr)`**. Cannot
  be suppressed or redirected by any pydevd option.

Once execution actually starts, pydevd's own diagnostics (e.g. "evaluation
did not finish after Ns") go over the DAP `output` channel to *us*, not to
the debuggee's stdio — so a running TUI is not at ongoing risk from pydevd
itself, only from these one-time startup lines.

In default mode these just appear in the interleaved output like anything
else. In `--pty <device>` mode, they land on the target terminal as the
first line(s), before the script (and any `curses.initscr()`) runs — once
curses switches to the alternate screen buffer they're hidden underneath,
and reappear (scrolled past) when the TUI exits. Cosmetic and transient, not
an ongoing interference.


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
- **Real job control.** `spawn_pydevd` uses `start_new_session=True`
  (`setsid()`) for both modes. With no `O_NOCTTY` on the `os.open(path,
  os.O_RDWR)`, opening the target tty as the new session's first terminal
  makes it that session's *controlling terminal* (standard Linux/POSIX
  behavior). So Ctrl+Z typed in the target terminal genuinely suspends the
  inferior (`SIGTSTP` via that tty's line discipline to the inferior's
  process group), terminal resize of the target terminal is seen normally by
  the inferior (`SIGWINCH`/`TIOCGWINSZ` against *that* tty), and curses/raw
  mode there is exactly as if the inferior had been run directly in that
  terminal — none of the default-mode limitations above apply.

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

| Mode | stdout/stderr | stdin | Passthrough thread | Terminal mode changes | Job control / TUI |
|---|---|---|---|---|---|
| `run()`, default (no `--pty`) | `master_fd -> our stdout`, continuous bg thread | `our stdin -> master_fd`, scoped to blocking resume calls | yes, started/stopped per resume call | `cbreak` (ISIG kept) during passthrough | none — no Ctrl+Z, no resize propagation, curses clashes with our screen |
| `run()`, `--pty <device>` | direct to external tty | direct to external tty | none | none — our terminal untouched | full — separate controlling terminal, Ctrl+Z/resize/curses all work natively |
| `connect()` | not visible (or via `output` events, future work) | not possible | none | none | n/a |
