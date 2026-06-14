# Inferior I/O model

How the debuggee's stdin/stdout/stderr relate to pydev-repl's own terminal,
across the two session kinds (`run()` vs `connect()`) and the three local
I/O modes (default PTY-pair passthrough, `--pty <device>`
external-terminal redirect, and per-stream `stdin=`/`stdout=`/`stderr=` file
redirection). Builds on [[project_sync_execution_model]] (the blocking
`cont()`/`step()`/... model) and supersedes the open questions in
[[project_pty_io_forwarding]].

The three local modes are mutually exclusive in pairs where it matters:
`--pty` and `stdin=`/`stdout=`/`stderr=` cannot be combined (see
"Mutual exclusion with `--pty`" below) — every session is either default
pty-pair, `--pty <device>`, or (default pty-pair + per-stream file
redirection for whichever streams were given).


## The two axes

|                          | **local (`run()`)** | **remote (`connect()`)** |
|---|---|---|
| We spawned the debuggee  | yes — `SESSION.process` | no |
| We hold an fd to its stdio | yes (`master_fd`, or the `--pty` device, or redirect files) | no, never |
| `--pty <device>` applies | yes | no (ignored, see below) |
| `stdin=`/`stdout=`/`stderr=` apply | yes | no (ignored, see below) |

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

Implemented: `_wait_for_resume_result()` starts a passthrough thread before
blocking on `wait_for_any_event(...)` and stops it when that returns:

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

`_StdinPassthrough.start()` guards `tcgetattr`/`setcbreak` with
`termios.error` (in addition to the `isatty()` check): some fds report
`isatty() == True` but don't support these ioctls. On that error it falls
back to no passthrough for this call, same as the non-tty case — better than
crashing the whole resume call over a cosmetic feature.

### Prompt switching and "is the inferior reading my input?" — rejected

`_PydevPromptStyle` (ptpython) has branches for `(running)` and `(paused)`;
`(disconnected)` covers the rest. There is **no `(inferior input)` state** —
an earlier draft of this design had one, gated on a `SESSION.awaiting_input`
flag toggled by pydevd's `pydevdInputRequested` event. Both the flag and the
prompt branch were **removed** after live testing (see "Why no stdin-state
indicator" below). Plain readline mode has no live prompt during a blocking
call anyway (the call hasn't returned), so there was never anything to redraw
there either.

The net result: while a passthrough-backed resume call is in flight, there is
**no UI indicator** for "the inferior is currently blocked in `input()`".
Typed bytes simply go to `master_fd` continuously, and the inferior consumes
them whenever it gets around to reading stdin — exactly as if you'd run the
script directly in a terminal and were watching its output to know when to
type. This is intentional, not a missing feature; see below.

### Why no stdin-state indicator: `pydevdInputRequested` doesn't work

pydevd's `pydevdInputRequested` event (`started: bool`) looked like the
obvious signal to drive a `(inferior input)` prompt / `"--- debuggee is
waiting for input ---"` marker. It was implemented and tested live, then
**removed entirely** — it is not just limited, it is actively misleading:

- Live testing showed the event arriving **after** the debuggee's output had
  already been printed and `readline()` had already returned — i.e. the
  marker appeared *after* the moment it claimed to describe, right before
  `*** program terminated`. Observed latency was 2+ seconds from when the
  debuggee actually entered `readline()`.
- **`input()` does not trigger this event at all when stdin/stdout are both
  ttys** (the common case in both default mode and `--pty <device>`).
  CPython's `input()` bypasses `sys.stdin` via `PyOS_Readline` (a direct fd
  read) whenever `isatty()` is true on both streams, so pydevd's wrapped
  `sys.stdin` — which is what emits the event — is never invoked.
- The installed pydevd's JSON `make_input_requested_message` sends `body: {}`
  for both "started" and "finished" — the documented `started: bool` field is
  never populated, so even the toggle-based workaround was guessing.

Given all three, an indicator built on this event would be wrong far more
often than right. The fallback — relying on the passthrough being
continuously active and the user watching live output, `expect`-style (see
`doc/references.md`) — is what gdb/`tmux`/`expect` all do anyway, and is what
pydev-repl now does too. `--skip-notify-stdin` is now in
`OBLIGATORY_RUN_ARGUMENTS` — moot either way since nothing listens for
`pydevdInputRequested`, but suppressing it at the source avoids paying for an
event we'll never use.

`_async_print` (used for async notifications like `"*** connection to pydevd
lost"`) needed no special-casing for this — it already has a
`SESSION.ptpython_active` branch (plain `print(message)`, ptpython's
`patch_stdout=True` handles redraw) and a plain-readline branch (clear line,
print, redraw `prompt + line_buffer`). Neither branch is exercised during the
passthrough window since there's nothing left that prints through it there.

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
`/dev/pts/7`), leave that terminal otherwise idle, then either:

```python
debug.set("pty", "/dev/pts/7")
debug.run("script.py")
```

or pass `--pty /dev/pts/7` on the pydev-repl command line (before `--file`,
like `--port`/`--log_level`/etc.) — it's parsed into `ArgsOptions.pty`
(`run_ctx.args_opt.pty`), the same field `set("pty", ...)` targets.

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

**TODO: verify with a real TUI/curses target.** The above (job control,
resize, curses) is reasoned from `setsid()`/controlling-terminal semantics,
not yet confirmed against an actual curses program run with `--pty
/dev/pts/N`. Only the default pty-pair + line-based case has a `samples/`
smoke test so far (`samples/io_passthrough_demo.py`).

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


## Per-stream file redirection: `stdin=`/`stdout=`/`stderr=`

A third local I/O mode, independent of (and **mutually exclusive with**)
`--pty`: redirect individual streams of the inferior to files, gdb/shell
`<`/`>`-style, without giving up default pty-pair behavior for the streams
left unredirected.

**Usage**:

```python
debug.run("script.py", stdin="input.txt", stdout="output.txt")
debug.run("script.py", stdout="output.txt", stderr="&1")
```

Backed by new `ArgsOptions.stdin`/`.stdout`/`.stderr` fields (`str | None`,
alongside the existing `pty` field) — the same fields `run()`'s kwargs and
`set("stdin"/"stdout"/"stderr", ...)` target. Unlike `pty`, these have no
`--stdin`/`--stdout`/`--stderr` CLI flags for v1 — `run()` kwargs or `set()`
only.

**Why not `<`/`>` operators**: `run("script.py") < "in.txt" > "out.txt"`
can't work the way it reads — `a < b > c` is a *chained comparison* in
Python (`(a < b) and (b > c)`), not two independent operator calls on the
result of `run(...)`, regardless of what `__lt__`/`__gt__` do. Keyword
arguments are the closest fit to the existing command style and need no
new syntax.

### Per-stream resolution

Each of `stdin`/`stdout`/`stderr` is resolved **independently** when
`spawn_pydevd` builds the `Popen` call:

- **Set to a path**: `os.open()` it directly — `os.O_RDONLY` for `stdin`,
  `os.O_WRONLY | os.O_CREAT | os.O_TRUNC` for `stdout`/`stderr`
  (truncate-on-open, shell `>` semantics; no `>>`-append in v1) — and pass
  that fd to `Popen` for this stream.
- **`stderr` only**, the literal string `"&1"`: "same destination as
  `stdout`" (shell `2>&1`) — `os.dup()` whatever fd `stdout` resolved to,
  whether that's a file or the pty slave. `stdin`/`stdout` have no
  equivalent sentinel (nothing else to alias to).
- **Unset** (the default): falls back to the pty-pair slave fd, exactly as
  today.

So `run("script.py", stdout="out.txt")` is a real mixed mode: stdin and
stderr still get the default pty-pair passthrough behavior (input
forwarding while blocked in `cont()`/etc., live streaming to our stdout for
stderr), while stdout goes straight to the file — not an all-or-nothing
switch.

### Effect on the background threads

- `_stream_output` (the `master_fd -> our stdout` thread) only starts if
  **stdout or stderr** still resolves to the pty slave. If both are
  redirected (to files, or `stderr="&1"` while `stdout` is a file), there's
  nothing on `master_fd` worth reading for display.
- `_StdinPassthrough` (per blocking resume call, as before) only starts if
  **stdin** still resolves to the pty slave. A file-redirected stdin needs
  no passthrough — the inferior reads the file directly.
- `pty.openpty()` itself is only called if at least one of the three streams
  still resolves to the pty slave. If `stdin`, `stdout`, and `stderr` are
  *all* redirected (to files and/or `"&1"`), no pty is created at all —
  `LaunchedProcess.master_fd` is `None`, same shape as `--pty` mode.

### Mutual exclusion with `--pty`

`--pty <device>` and any of `stdin=`/`stdout=`/`stderr=` are **mutually
exclusive** — `run()` errors ("error: --pty conflicts with
stdin=/stdout=/stderr= redirection — unset one") if both are set, rather
than defining a combined behavior.

Rationale: `--pty`'s entire point is handing the inferior a *real terminal*
(a controlling tty, for job control/curses/resize — see above); per-stream
file redirection's point is capturing one or more streams to a file
*instead of* a terminal. There's no coherent meaning for "send stdout to
this file, and also give the inferior `/dev/pts/7` as its controlling
terminal for the other streams" worth the implementation cost of
partial-`--pty` support, and no one has asked for it. Erroring loudly when
both are set is simpler than silently picking a precedence order, and keeps
`--pty`'s own code path (already a fully separate branch in
`spawn_pydevd`) simple and self-contained.

**`stdin=`/`stdout=`/`stderr=` + `connect()`**: same as `--pty` —
`connect()` never spawns a process, so these are simply unused/inert if set
without a prior local `run()`. No error, same "config now, used by `run()`
later" semantics.


## Remote sessions (`connect()`): hard limits

No `SESSION.process`, no fd of any kind to the debuggee's stdio — this is
fundamental, not a missing feature:

- **stdout/stderr**: pydevd can optionally be made to emit DAP `output`
  events (§3/§6 of `dap_scope.md`) carrying captured stdout/stderr text —
  technically the only way to see remote inferior output through pydev-repl
  itself. **Deliberately not pursued.** stdin is fundamentally not possible
  for `connect()` (see below), so wiring up `output` events would give a
  read-only half of the I/O model — and the remote debuggee's output is
  already visible wherever it was launched (its own terminal/log, reachable
  via a second `ssh` session to that machine). Adding a one-way output stream
  here wouldn't change what's actually needed to interact with the process.
- **stdin**: there is no DAP request that delivers stdin to the debuggee.
  `pydevdInputRequested` is purely a notification (`started=true/false`).
  **Not possible**, by design of the protocol — not a gap pydev-repl can
  close. If a remote `input()` call blocks, the only fix is on the machine
  actually running the debuggee.

`--pty` and `stdin=`/`stdout=`/`stderr=` are meaningless here for the same
reason they're no-ops above: there is no local process to attach a device or
file to.


## Summary

| Mode | stdout/stderr | stdin | Passthrough thread | Terminal mode changes | Job control / TUI |
|---|---|---|---|---|---|
| `run()`, default (no `--pty`, no redirection) | `master_fd -> our stdout`, continuous bg thread | `our stdin -> master_fd`, scoped to blocking resume calls | yes, started/stopped per resume call | `cbreak` (ISIG kept) during passthrough | none — no Ctrl+Z, no resize propagation, curses clashes with our screen |
| `run()`, `--pty <device>` | direct to external tty | direct to external tty | none | none — our terminal untouched | full — separate controlling terminal, Ctrl+Z/resize/curses all work natively |
| `run()`, `stdin=`/`stdout=`/`stderr=` (any subset) | redirected streams go straight to their file (or `&1`'s target); unredirected streams keep default-mode behavior | same | only for streams still on the pty slave | only while stdin is still on the pty slave | none for redirected streams; default-mode limits still apply to unredirected ones |
| `connect()` | not visible (not pursued — no point without stdin) | not possible | none | none | n/a |
