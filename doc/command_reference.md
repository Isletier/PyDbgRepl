# pydev-repl command reference

Complete list of pydev-repl's command surface: what's implemented today,
what's planned, and what's out of reach given pydevd's DAP support.

Every command is a plain Python function injected into `__main__` by
`start_eval()` (see `src/commands/`). There is no separate command-line
parser/grammar — arguments are normal Python call arguments, e.g.
`breakpoint("foo.py", 12, condition="x > 0")`.

**Status**: `done`, `planned`, or `n/a` (not feasible / not worth it, with
reason).

**Return values**: every command returns a value — never `None` — whose
`__repr__` is the human-readable output shown below; the REPL echoes this
repr for any non-`None` expression result, so typed at the prompt it looks
exactly like a `print()`-based command always did, while scripts get real
data (lists, dicts, or `str` subclasses) and a uniform `if not result:`
failure check via the `Error` type. See [[project_value_semantics]] /
`doc/value_semantics.md` for the full convention and the wrapper types in
`src/commands/_display.py`.

For each command's closest **pdb**/**gdb** equivalent, the backing **DAP**
request(s), and what tab-completion offers for its arguments, see
[Reference: pdb/gdb/DAP equivalents and completion](#reference-pdbgdbdap-equivalents-and-completion)
below.


### Argument conventions

- **`path`**: a file path (string). Tab-completion (debugger mode) searches
  by basename across the whole project, not just `./`-relative prefixes —
  see `completion_design.md` §3.
- **`path, line` shortcut**: any command taking a leading `(path, line)`
  pair — `breakpoint`, `clear`, `tbreak`, `enable`, `disable`, `ignore` —
  also accepts a single bare `int` in place of `path`, meaning "`line` in
  the *current file*". E.g. `breakpoint(10)` == `breakpoint(_current_file(),
  10)`. `_current_file()` is the current frame's source path if paused,
  else the script given to `run()`. This is purely an argument-normalization
  convenience in each command, not a separate code path.
- **`line` (no path)**: `until`, `jump`, `list` take a bare line number in
  the current file directly — there's no `path` to omit, so no shortcut
  needed.


## 1. Session lifecycle

| Command | Returns | Description | Status |
|---|---|---|---|
| `run(script=None, *args, stdin=None, stdout=None, stderr=None)` | `StopResult \| Error` | Spawns pydevd, connects, applies stored breakpoints/filters, and blocks for the first stop or exit. If a session is already running, it's killed first (gdb-style restart). `script`/`args` default to the `--file` (and trailing args) given on the command line at startup, and are remembered for subsequent no-argument calls. `stdin`/`stdout`/`stderr` redirect the inferior's streams to files (`stderr="&1"` aliases stdout); any unset stream keeps the default owned-PTY-pair passthrough, and falls back to a previously `set("stdin"/"stdout"/"stderr", ...)` value. Mutually exclusive with `set("pty", ...)` — see [[project_io_model]]. | done |
| `stop()` | `Status \| Error` | Ends the session: tears down the DAP connection and any spawned process as one unit. | done |
| `connect()` | `StopResult \| Error` | Attach to a pydevd already listening (not spawned by us). | done |
| `disconnect()` | `Status \| Error` | Leave a remote debuggee running, drop our connection. | done |
| `terminate()` | `Status \| Error` | Ask pydevd to terminate the debuggee, local or remote. | done |
| `restart()` | `StopResult \| Error` | Restart the debuggee: `stop()` the current session (if any), then `run()` again with the same `run_ctx`/breakpoints. | done |


## 2. Configuration

| Command | Returns | Description | Status |
|---|---|---|---|
| `set(name, value)` | `Status \| Error` | Generic config registry over `RunContext.args_opt`/`.env`/`ReplOptions` (`src/options.py`); includes the `completion` and `ui` options themselves. | done |
| `get(name)` | `Status \| Error` | Get the current value of a single option, or every option in a group at once: `"args_opt"`, `"env"`, `"repl"` (the corresponding `RunContext`/`ReplOptions` dataclasses), or `"args"` (the inferior's argv). Same `name` arguments as `reset()`. | done |
| `reset(name)` | `Status \| Error` | Reset a single option to its dataclass default, or a whole group at once: `"args_opt"`, `"env"`, `"repl"` (the corresponding `RunContext`/`ReplOptions` dataclasses), or `"args"` (the inferior's argv, reset to `[]`). | done |


## 3. Execution control

All of these now **block** until the program stops/exits — see
[[project_sync_execution_model]]. Ctrl+C maps to `interrupt()`.

| Command | Returns | Description | Status |
|---|---|---|---|
| `cont()` | `StopResult \| Error` | Resume execution until the next stop or exit. | done |
| `step()` | `StopResult \| Error` | Step one line, descending into calls. | done |
| `next()` | `StopResult \| Error` | Step one line, stepping over calls. | done |
| `finish()` | `StopResult \| Error` | Run until the current frame returns. | done |
| `interrupt()` | `Status \| Error` | Pause a running debuggee. Called directly by the Ctrl+C handler in `src/__init__.py`. | done |
| `until(line=None)` | `StopResult \| Error` | Run until `line` (or the next line greater than the current one, if omitted) in the current file. Emulated since DAP has no "run until line" primitive: set a temporary breakpoint, `cont()`, then clear it — same idea as `tbreak` + `cont`. | done |
| `jump(line)` | `StopResult \| Error` | Jump execution to `line` without running the lines in between. Resolves targets via `gotoTargets`, then `goto`. Skips/reruns code — same caveats as gdb's `jump` (no cleanup of skipped statements). | done |
| `stepi()` / `nexti()` | — | Instruction-level stepping. Not meaningful here — pydevd traces Python bytecode/lines, not a CPU-instruction concept. | n/a |
| reverse execution (`reverse-continue`, `stepBack`) | — | pydevd doesn't implement this (`supportsStepBack=False`). | n/a |


## 4. Breakpoints

| Command | Returns | Description | Status |
|---|---|---|---|
| `breakpoint(path_or_line, line=None, condition=None, log_message=None)` | `Status \| Error` | Set a breakpoint at `path_or_line:line` (or just `path_or_line` as a bare line number in the current file — see Argument conventions), optionally conditional. `log_message` makes it a logpoint (pydevd `supportsLogPoints`): hits print a message but don't stop the program. | done |
| `clear(path_or_line, line=None)` | `Status \| Error` | Remove a breakpoint. | done |
| `catch(*filters)` | `Status` | Set exception breakpoints. `filters` are pydevd's `raised`/`uncaught`/`userUnhandled` (from the `initialize` response's `exceptionBreakpointFilters`). | done |
| `tbreak(path_or_line, line=None, condition=None)` | `Status \| Error` | Temporary breakpoint: tracked ourselves — on the next stop with `reason="breakpoint"` at that path:line, it's auto-`clear()`ed (DAP has no native "temporary" flag), reported as a `StopResult` suffix line. | done |
| `enable(path_or_line, line=None)` / `disable(path_or_line, line=None)` | `Status \| Error` | Enable/disable a breakpoint without forgetting it. `SESSION.breakpoints` entries carry a per-bp `enabled` flag; `disable` just omits it from the list sent to pydevd. | done |
| `ignore(path_or_line, line_or_count, count=None)` | `Status \| Error` | Ignore a breakpoint until it's been hit `count` times. Maps to pydevd's `hitCondition` (`supportsHitConditionalBreakpoints`) as `hitCondition=f">= {count + 1}"`. If `count is None`, `(path_or_line, line_or_count)` is read as `(line, count)` against `_current_file()` — same shortcut convention as `breakpoint()`. | done |
| `funcbreak(name, condition=None)` | `Status` | Break on entry to function `name`, optionally conditional. Separate from line breakpoints in DAP (`setFunctionBreakpoints`), hence a separate command. | done |
| `watch(expr)` / `rwatch` / `awatch` | — | Data breakpoints. pydevd doesn't implement them (`supportsDataBreakpoints=False`); only emulable via a polling loop + conditional breakpoints, not worth it for v1. | n/a |
| `breakpoints()` / `info_breakpoints()` | `Breakpoints` | All breakpoints: `SESSION.breakpoints`, `SESSION.function_breakpoints`, and `SESSION.exception_filters`. Purely local state, no DAP call needed. | done |


## 5. Stack & thread navigation

| Command | Returns | Description | Status |
|---|---|---|---|
| `threads()` | `ThreadList \| Error` | List threads and pick a default current thread. | done |
| `thread(thread_id)` | `Status \| Error` | Switch `SESSION.current_thread_id` (and reset the current frame). | done |
| `bt(levels=None)` | `FrameList \| Error` | A backtrace (stack trace) of the current thread. | done |
| `frame(index)` | `FrameRef \| Error` | Switch to stack frame `index` (absolute, unlike pdb/gdb's relative `up`/`down`). | done |
| `up(n=1)` / `down(n=1)` | `FrameRef \| Error` | Move `n` frames toward the caller/callee. Thin wrappers around `frame()` using `SESSION.current_frame_id`'s index ± n; a move clamped at the stack's edge sets the returned `FrameRef`'s `prefix` to pdb's "Oldest/Newest frame" message. | done |


## 6. Inspection (variables & expressions)

| Command | Returns | Description | Status |
|---|---|---|---|
| `p(expression)` | `Status \| Error` | Evaluate `expression` in the current frame. | done |
| `locals()` | `Scope \| Error` | Local variables of the current frame (the "Locals" scope). | done |
| `globals_()` | `Scope \| Error` | Global variables (the "Globals" scope). Named `globals_` to avoid shadowing the builtin in `__main__`. | done |
| `setvar(name, value)` | `Status \| Error` | Assign `value` to variable `name` in the current frame. Works because pydevd's `evaluate` runs assignments via `exec` in repl context. | done |
| `whatis(expression)` / `pt(expression)` | `Status \| Error` | The type of `expression` (the `evaluate` response's `type` field, instead of `result`). | done |
| `display(expression)` / `undisplay(id)` | `Status \| Error` | Re-evaluate `expression` after every stop, until `undisplay(id)`. Purely client-side: a list of expressions in `SESSION.displays`, re-evaluated after every stop and folded into the resulting `StopResult`'s `suffix`. | done |
| `exception_info()` | `ExceptionInfo \| Error` | Details of the current exception (`exceptionId`/`description`/`stackTrace`) for the current thread. Natural follow-up to `catch()`. | done |
| `completions(text, column)` | `CompletionList \| Error` | Completion candidates for `text` at `column` in the current frame. Backs the planned `p`/`setvar`/`whatis`/`display` expression-completion (not normally called directly). | done |


## 7. Source listing

| Command | Returns | Description | Status |
|---|---|---|---|
| `list(first=None, last=None)` / `l()` | `SourceLines \| Error` | Source lines around the current line. No args: ~10 lines centered on the current line. `first` only: window centered on that line (like pdb `list 20`). Both: that range, inclusive. Reads the local file directly around the current frame's line — no DAP round trip needed (the `source` request, for pydevd-synthesized/remote sources, is not yet wired up). | done |


## 8. Misc / introspection

| Command | Returns | Description | Status |
|---|---|---|---|
| `modules()` | `ModuleList \| Error` | Loaded modules: id, name, path. | done |
| `pydevd_info()` | `InfoSections \| Error` | Process/Python/platform info — debugging-the-debugger convenience. | done |


## Key-binding shortcuts

ptpython F-key shortcuts (F5/F10/F11/F12 → `cont`/`next`/`step`/`finish` by
default) are layered on top of these commands — see `doc/keybindings.md` for
the extensible binding system and how to customize/disable them.


## Scenario / batch mode

`repl.py` can carry plain Python "scenario" lines after `start_eval()`, which
run before an interactive prompt takes over. `--batch` (or
`set("interactive", "0")`) skips the prompt entirely for unattended
automation. See `doc/scenario_mode.md`.


## Out of scope (pydevd has no support at all)

From `doc/dap_scope.md` §8 — no point modeling these as commands:

- Memory/disassembly: `x` (gdb examine), `disassemble`, `readMemory`/`writeMemory` — `supportsDisassembleRequest=False`, `supportsReadMemoryRequest=False`.
- `restart`/`restartFrame` as a *protocol* feature — `supportsRestartRequest=False` (but see `restart()` above — emulable via `stop()`+`run()`).
- `reverseContinue`/`stepBack` — `supportsStepBack=False`.
- `terminateThreads` — `supportsTerminateThreadsRequest=False`.
- Data breakpoints (`watch`/`rwatch`/`awatch`) — `supportsDataBreakpoints=False`.


## Summary of planned work, roughly in priority order

Items 0-9 and most of 10 are **done**. `src/commands.py` was split into a
`src/commands/` package by topic (`lifecycle`, `config`, `execution`,
`stack`, `breakpoints`, `inspect_`, `source`, `misc`), with shared internals
(`_current_file()`/`_current_location()`, `_resolve_path_line()`,
`_wait_for_resume_result()`, `_report_stopped()`, ...) in `_internal.py`.
`_report_stopped()` now runs a list of `post_stop_hooks` so `tbreak()`'s
auto-clear and `display()`'s re-evaluation can hook in without circular
imports.

0. ~~**`_current_file()`**~~ — done, as `_current_location()`/`_current_file()`
   in `_internal.py`.
1. ~~**`list()`**~~ — done.
2. ~~**`globals_()`**~~ — done.
3. ~~**`up()`/`down()`**~~ — done.
4. ~~**`exception_info()`**~~ — done.
5. ~~**`display()`/`undisplay()`**~~ — done.
6. ~~**`breakpoints()`**~~ — done.
7. ~~**`tbreak()`, `enable()`/`disable()`, `ignore()`, logpoints, `funcbreak()`**~~ — done.
8. ~~**`until()`, `jump()`**~~ — done.
9. ~~**`restart()`**~~ — done.
10. ~~**`whatis()`, `setvar()`, `modules()`, `pydevd_info()`, `completions()`**~~ — done.

All command-reference items are now done.

Tab-completion (`completion_design.md`) is now implemented for debugger mode:
top-level command-name completion, a basename-based file completer, and an
argument-position-aware completion table covering `breakpoint`/`clear`/
`tbreak`/`enable`/`disable`/`ignore` (file), `thread`/`frame` (live ids),
`catch` (exception filters), and `set`/`reset` (option names). Remaining:
phase-2 DAP-backed `p()`/`setvar()` expression completion (see
`completion_design.md`'s "Out of scope for this pass" section).


## Reference: pdb/gdb/DAP equivalents and completion

For orientation against the two reference debuggers, **pdb** (Python's own)
and **gdb** (the CLI UX model this REPL follows, per
[[project_sync_execution_model]]/`doc/repl_execution_model.md`), and the
backing DAP request(s) (from `doc/dap_scope.md`). **Completion** describes
what tab-completion offers for each argument in `"debugger"` mode, per
[[project_completion_design]]/`doc/completion_design.md` — implemented for
the commands listed in `_ARG_TABLE` (`src/completion.py`); others fall back
to no completion in debugger mode (`"classical"` mode is always available
via `set("completion", "classical")`).

### Session lifecycle

| Command | pdb | gdb | DAP | Completion |
|---|---|---|---|---|
| `run(...)` | `run`/`restart` (mid-session) | `run`/`start` | `attach`, `configurationDone` | `script`: file completer (basename search). `*args`: none. `stdin`/`stdout`/`stderr`: none for v1 (could gain a path-aware file completer later). |
| `stop()` | `q(uit)` (kills inferior) | `kill` | `disconnect` (remote only) | — |
| `connect()` | — | `target remote`/`attach <pid>` | `attach`, `configurationDone` | — |
| `disconnect()` | — | `detach` | `disconnect` (terminateDebuggee=False) | — |
| `terminate()` | — | (no exact equiv; closest is `kill`) | `terminate` | — |
| `restart()` | `run`/`restart` | `run` (while running) | — | — |

### Configuration

| Command | pdb | gdb | DAP | Completion |
|---|---|---|---|---|
| `set(name, value)` | — (pdb has no generic config) | `set <param> <value>` | — | `name`: option names from `_options.list_options()`. `value`: none for v1. |
| `reset(name)` | — | (no direct equiv; `set` back to default) | — | `name`: option names, plus group names `"args_opt"`/`"env"`/`"repl"`/`"args"`. |

### Execution control

| Command | pdb | gdb | DAP | Completion |
|---|---|---|---|---|
| `cont()` | `c(ont(inue))` | `continue`/`c` | `continue` + wait for `stopped`/`exited`/`terminated` | — |
| `step()` | `s(tep)` | `step`/`s` | `stepIn` + wait | — |
| `next()` | `n(ext)` | `next`/`n` | `next` + wait | — |
| `finish()` | `r(eturn)` | `finish`/`fin` | `stepOut` + wait | — |
| `interrupt()` | (Ctrl+C → `Pdb.set_trace` re-entry) | Ctrl+C (`SIGINT`) | `pause` | — |
| `until(line=None)` | `unt(il)` | `until`/`u`, `advance` | none directly | `line`: none for v1 (would need executable-line analysis of current file). |
| `jump(line)` | `j(ump)` | `jump`, `tbreak`+`jump` | `gotoTargets` + `goto` | `line`: none for v1 (same as `until`). |
| `stepi()` / `nexti()` | — | `stepi`/`si`, `nexti`/`ni` | — | n/a |
| reverse exec | — | `reverse-continue`, `reverse-step` | n/a (`supportsStepBack=False`) | n/a |

### Breakpoints

| Command | pdb | gdb | DAP | Completion |
|---|---|---|---|---|
| `breakpoint(path_or_line, line=None, condition=None, log_message=None)` | `b(reak) [file:]lineno[, cond]` | `break [file:]line if cond` | `setBreakpoints` (per-file list) | `path_or_line`: file completer (basename search) when typing a path, or a bare `int` for the current-file shortcut. `line`/`condition`/`log_message`: none. |
| `clear(path_or_line, line=None)` | `cl(ear) [file:]lineno` | `clear [file:]line`, `delete N` | `setBreakpoints` | `path_or_line`: same as `breakpoint()`. |
| `catch(*filters)` | — (pdb catches all exceptions when `c`'d into) | `catch throw`/`catch catch` (C++-ish; closest broad analog) | `setExceptionBreakpoints` | `filters`: exception filter ids from the `initialize` response's `exceptionBreakpointFilters` (fallback `raised`/`uncaught`/`userUnhandled`). |
| `tbreak(path_or_line, line=None, condition=None)` | `tbreak` | `tbreak` | `setBreakpoints` (no native "temporary" flag) | `path_or_line`/`line`: same as `breakpoint()`. |
| `enable(...)` / `disable(...)` | `enable`/`disable bpnum` | `enable`/`disable N` | `setBreakpoints` (omit from sent list) | `path_or_line`/`line`: ideally completes from *existing* `SESSION.breakpoints` entries (not a general file search) once `breakpoints()` exists, falling back to the `breakpoint()`-style file completer until then. |
| `ignore(path_or_line, line_or_count, count=None)` | `ignore bpnum count` | `ignore N count` | `setBreakpoints` (`hitCondition`) | `path_or_line`/`line_or_count`: same as `breakpoint()` for the path/line pair; `count`: none. |
| `breakpoint(..., log_message=...)` (logpoint) | — | gdb `dprintf` | `setBreakpoints` (`logMessage`) | (covered by `breakpoint()` row above) |
| `funcbreak(name, condition=None)` | `b function_name` | `break function_name` | `setFunctionBreakpoints` | `name`: none in debugger mode for v1 (would need a project-wide function/symbol index — out of scope; `"classical"` mode's jedi completion may incidentally help). |
| `watch(expr)` / `rwatch` / `awatch` | — (no native pdb watch) | `watch`/`rwatch`/`awatch` | n/a (`supportsDataBreakpoints=False`) | n/a |
| `breakpoints()` / `info_breakpoints()` | `b` (no args lists) | `info breakpoints` | — (local state only) | — |

### Stack & thread navigation

| Command | pdb | gdb | DAP | Completion |
|---|---|---|---|---|
| `threads()` | — (pdb is single-threaded by design) | `info threads` | `threads` | — |
| `thread(thread_id)` | — | `thread N` | — (local state only) | `thread_id`: live ids from `SESSION.dap.threads()` (if connected), shown with thread name. |
| `bt(levels=None)` | `w(here)`/`bt` | `bt`/`where`/`backtrace` | `stackTrace` | `levels`: none. |
| `frame(index)` | `u(p)`/`d(own)` (relative) move + implicit frame display | `frame N` | `stackTrace` (frame already fetched) | `index`: `0..len(stack)-1` from the current thread's stack trace (if paused). |
| `up(n=1)` / `down(n=1)` | `u(p) [n]` / `d(own) [n]` | `up [n]` / `down [n]` | `stackTrace` | `n`: none. |

### Inspection (variables & expressions)

| Command | pdb | gdb | DAP | Completion |
|---|---|---|---|---|
| `p(expression)` | `p(rint) expr` | `print`/`p expr` | `evaluate` (context="repl") | `expression`: **phase 2** — names/attributes in the current frame via the DAP `completions` request; none for v1. |
| `locals()` | `args` (params only) + reading `locals()` via `p` | `info locals` | `scopes` + `variables` | — |
| `globals_()` | `p globals()` (no dedicated cmd) | `info variables` (broad) | `scopes` + `variables` | — |
| `setvar(name, value)` | `p x = 5` (works via exec) | `set var x = 5` | `setVariable` / `setExpression` | `name`: locals/globals names in the current frame — same phase-2 mechanism as `p()`. `value`: none. |
| `whatis(expression)` / `pt(expression)` | `whatis expr` | `whatis expr`/`ptype expr` | `evaluate` (`context="hover"`, inspect `type`/`result` fields) | `expression`: same phase-2 mechanism as `p()`. |
| `display(expression)` / `undisplay(id)` | `display`/`undisplay` | `display`/`undisplay` | `evaluate`, re-run after each stop | `display`'s `expression`: same phase-2 mechanism as `p()`. `undisplay`'s `id`: ids from `SESSION.displays`. |
| `exception_info()` | (pdb auto-prints traceback on uncaught exception) | (gdb shows signal info on stop) | `exceptionInfo` | — |
| `completions(text, column)` | (handled by external `readline`/`rlcompleter`) | (gdb has its own completer) | `completions` | n/a — this command *is* the phase-2 completion backend, not itself completable in a meaningful way. |

### Source listing

| Command | pdb | gdb | DAP | Completion |
|---|---|---|---|---|
| `list(first=None, last=None)` / `l()` | `l(ist) [first[, last]]` | `list`/`l [linespec]` | `source` (only for `sourceReference != 0`, i.e. no local file) | `first`/`last`: none (bare line numbers in the current file). |

### Misc / introspection

| Command | pdb | gdb | DAP | Completion |
|---|---|---|---|---|
| `modules()` | — | `info sharedlibrary` (loose analog) | `modules` | — |
| `pydevd_info()` | — | — | `pydevdSystemInfo` | — |
