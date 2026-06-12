# pydev-repl command reference

Complete list of pydev-repl's command surface: what's implemented today,
what's planned, and what's out of reach given pydevd's DAP support — mapped
against the two reference debuggers, **pdb** (Python's own) and **gdb**
(the CLI UX model this REPL follows, per
[[project_sync_execution_model]]/`reference/repl_execution_model.md`).

Every command is a plain Python function injected into `__main__` by
`start_eval()` (see `src/commands.py`). There is no separate command-line
parser/grammar — arguments are normal Python call arguments, e.g.
`breakpoint("foo.py", 12, condition="x > 0")`.

Columns: **pdb** / **gdb** give the closest equivalent command(s) for
orientation. **DAP** gives the backing request(s) (from
`reference/dap_scope.md`). **Completion** previews what tab-completion would
offer for each argument in `"debugger"` mode, per
[[project_completion_design]]/`reference/completion_design.md` — not yet
implemented, but the signatures below are written with it in mind.
**Status**: `done`, `planned`, or `n/a` (not feasible / not worth it, with
reason).


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

| Command | pdb | gdb | DAP | Completion | Status | Notes |
|---|---|---|---|---|---|---|
| `run(script=None, *args)` | `run`/`restart` (mid-session) | `run`/`start` | `attach`, `configurationDone` | `script`: file completer (basename search). `*args`: none. | done | Spawns pydevd, connects, applies stored breakpoints/filters, blocks for the first `stopped`/exit. |
| `stop()` | `q(uit)` (kills inferior) | `kill` | `disconnect` (remote only) | — (no args) | done | Tears down DAP connection + spawned process as one unit (`_end_session`, see [[project_sync_execution_model]]). |
| `connect()` | — | `target remote`/`attach <pid>` | `attach`, `configurationDone` | — | done | Attach to a pydevd already listening (not spawned by us). |
| `disconnect()` | — | `detach` | `disconnect` (terminateDebuggee=False) | — | done | Leave a remote debuggee running, drop our connection. |
| `terminate()` | — | (no exact equiv; closest is `kill`) | `terminate` | — | done | Ask pydevd to terminate the debuggee, local or remote. |
| `restart()` | `run`/`restart` | `run` (while running) | — | — | done | pydevd has `supportsRestartRequest=False`; implement as `stop()` + `run()` with the same `run_ctx`/breakpoints. |


## 2. Configuration

| Command | pdb | gdb | DAP | Completion | Status | Notes |
|---|---|---|---|---|---|---|
| `set(name, value)` | — (pdb has no generic config) | `set <param> <value>` | — | `name`: option names from `_options.list_options()`. `value`: none for v1 (could special-case bool/enum-valued options later). | done | Generic registry over `RunContext.args_opt`/`.env`/`ReplOptions` (`src/options.py`); includes the `completion` and `ui` options themselves. |
| `unset(name)` | — | (no direct equiv; `set` back to default) | — | `name`: same as `set()`. | done | Reset a field to its dataclass default. |


## 3. Execution control

All of these now **block** until the program stops/exits — see
[[project_sync_execution_model]]. Ctrl+C maps to `interrupt()`.

| Command | pdb | gdb | DAP | Completion | Status | Notes |
|---|---|---|---|---|---|---|
| `cont()` | `c(ont(inue))` | `continue`/`c` | `continue` + wait for `stopped`/`exited`/`terminated` | — (no args) | done | |
| `step()` | `s(tep)` | `step`/`s` | `stepIn` + wait | — | done | Descends into calls. |
| `next()` | `n(ext)` | `next`/`n` | `next` + wait | — | done | Steps over calls. |
| `finish()` | `r(eturn)` | `finish`/`fin` | `stepOut` + wait | — | done | Runs until the current frame returns. |
| `interrupt()` | (Ctrl+C → `Pdb.set_trace` re-entry) | Ctrl+C (`SIGINT`) | `pause` | — | done | Called directly by the Ctrl+C handler in `src/__init__.py`. |
| `until(line=None)` | `unt(il)` | `until`/`u`, `advance` | none directly | `line`: none for v1 (would need executable-line analysis of current file). | done | DAP has no "run until line" primitive. Emulated: set a temporary breakpoint at `line` (or "next line greater than current" if omitted), `cont()`, then clear it — same idea as `tbreak` + `cont`. |
| `jump(line)` | `j(ump)` | `jump`, `tbreak`+`jump` | `gotoTargets` + `goto` | `line`: none for v1 (same as `until`). | done | pydevd supports `supportsGotoTargetsRequest`. Resolves targets for `line` via `gotoTargets`, then `goto`. Skips/reruns code — same caveats as gdb's `jump` (no cleanup of skipped statements). |
| `stepi()` / `nexti()` | — | `stepi`/`si`, `nexti`/`ni` | — | n/a | n/a | Instruction-level stepping; pydevd traces Python bytecode/lines, not a meaningful "instruction" concept for users here. |
| reverse exec (`reverse-continue`, `stepBack`) | — | `reverse-continue`, `reverse-step` | n/a (`supportsStepBack=False`) | n/a | n/a | pydevd doesn't implement this. |


## 4. Breakpoints

| Command | pdb | gdb | DAP | Completion | Status | Notes |
|---|---|---|---|---|---|---|
| `breakpoint(path_or_line, line=None, condition=None, log_message=None)` | `b(reak) [file:]lineno[, cond]` | `break [file:]line if cond` | `setBreakpoints` (per-file list) | `path_or_line`: file completer (basename search) when typing a path, or a bare `int` for the current-file shortcut (see Argument conventions). `line`/`condition`/`log_message`: none. | done | Conditional breakpoints supported (`supportsConditionalBreakpoints`). `log_message` is the planned logpoint kwarg (see below) — listed here so the signature is settled up front. |
| `clear(path_or_line, line=None)` | `cl(ear) [file:]lineno` | `clear [file:]line`, `delete N` | `setBreakpoints` | `path_or_line`: same as `breakpoint()`. | done | |
| `catch(*filters)` | — (pdb catches all exceptions when `c`'d into) | `catch throw`/`catch catch` (C++-ish; closest broad analog) | `setExceptionBreakpoints` | `filters`: exception filter ids from the `initialize` response's `exceptionBreakpointFilters` (fallback `raised`/`uncaught`/`userUnhandled`). | done | Filters are pydevd's `raised`/`uncaught`/`userUnhandled` (from `initialize` response's `exceptionBreakpointFilters`). |
| `tbreak(path_or_line, line=None, condition=None)` | `tbreak` | `tbreak` | `setBreakpoints` (no native "temporary" flag) | `path_or_line`/`line`: same as `breakpoint()`. | done | Tracked as "temporary" ourselves: on the next `stopped` with `reason="breakpoint"` at that path:line, auto-`clear()`s it. |
| `enable(path_or_line, line=None)` / `disable(path_or_line, line=None)` | `enable`/`disable bpnum` | `enable`/`disable N` | `setBreakpoints` (omit from sent list) | `path_or_line`/`line`: ideally completes from *existing* `SESSION.breakpoints` entries (not a general file search) once `breakpoints()` exists, falling back to the `breakpoint()`-style file completer until then. | done | `SESSION.breakpoints` entries have a per-bp `enabled` flag; `disable` just omits it from the list sent to pydevd without forgetting it. |
| `ignore(path_or_line, line_or_count, count=None)` | `ignore bpnum count` | `ignore N count` | `setBreakpoints` (`hitCondition`) | `path_or_line`/`line_or_count`: same as `breakpoint()` for the path/line pair; `count`: none. | done | Maps to pydevd's `hitCondition` (`supportsHitConditionalBreakpoints`); thin wrapper that sets `hitCondition=f">= {count + 1}"`. If `count is None`, `(path_or_line, line_or_count)` is `(line, count)` against `_current_file()` — same shortcut convention as `breakpoint()`. |
| `breakpoint(..., log_message=...)` (logpoint) | — | gdb `dprintf` | `setBreakpoints` (`logMessage`) | (covered by `breakpoint()` row above) | done | pydevd supports `supportsLogPoints`: hits print a message but don't stop. Extra kwarg on existing `breakpoint()`. |
| `funcbreak(name, condition=None)` | `b function_name` | `break function_name` | `setFunctionBreakpoints` | `name`: none in debugger mode for v1 (would need a project-wide function/symbol index — out of scope; `"classical"` mode's jedi completion may incidentally help). | done | pydevd supports `setFunctionBreakpoints`; separate from line breakpoints in DAP, so a separate command. |
| `watch(expr)` / `rwatch` / `awatch` | — (no native pdb watch) | `watch`/`rwatch`/`awatch` | n/a (`supportsDataBreakpoints=False`) | n/a | n/a | pydevd doesn't implement data breakpoints. Only emulable via a polling loop + conditional breakpoints — not worth it for v1. |
| `breakpoints()` / `info_breakpoints()` | `b` (no args lists) | `info breakpoints` | — (local state only) | — (no args) | done | Pretty-prints `SESSION.breakpoints`, `SESSION.function_breakpoints` + `SESSION.exception_filters`; purely local, no DAP call needed. |


## 5. Stack & thread navigation

| Command | pdb | gdb | DAP | Completion | Status | Notes |
|---|---|---|---|---|---|---|
| `threads()` | — (pdb is single-threaded by design) | `info threads` | `threads` | — (no args) | done | Also picks a default current thread. |
| `thread(thread_id)` | — | `thread N` | — (local state only) | `thread_id`: live ids from `SESSION.dap.threads()` (if connected), shown with thread name. | done | Switches `SESSION.current_thread_id`/resets frame. |
| `bt(levels=None)` | `w(here)`/`bt` | `bt`/`where`/`backtrace` | `stackTrace` | `levels`: none. | done | |
| `frame(index)` | `u(p)`/`d(own)` (relative) move + implicit frame display | `frame N` | `stackTrace` (frame already fetched) | `index`: `0..len(stack)-1` from the current thread's stack trace (if paused). | done | Absolute frame index, unlike pdb/gdb's relative `up`/`down`. |
| `up(n=1)` / `down(n=1)` | `u(p) [n]` / `d(own) [n]` | `up [n]` / `down [n]` | `stackTrace` | `n`: none. | done | Thin wrappers around `frame()` using `SESSION.current_frame_id`'s index ± n, with bounds messages matching pdb's "Oldest/Newest frame". |


## 6. Inspection (variables & expressions)

| Command | pdb | gdb | DAP | Completion | Status | Notes |
|---|---|---|---|---|---|---|
| `p(expression)` | `p(rint) expr` | `print`/`p expr` | `evaluate` (context="repl") | `expression`: **phase 2** — names/attributes in the current frame via the DAP `completions` request (see `completions()` below); none for v1. | done | |
| `locals()` | `args` (params only) + reading `locals()` via `p` | `info locals` | `scopes` + `variables` | — (no args) | done | Filters to the "Locals" scope. |
| `globals_()` | `p globals()` (no dedicated cmd) | `info variables` (broad) | `scopes` + `variables` | — | done | Mirrors `locals()` but for the "Globals" scope — same code path, different scope name. (Named `globals_` to avoid shadowing the builtin in `__main__`.) |
| `args()` | `a(rgs)` | `info args` | `scopes` + `variables` (filter to params) | — | planned | pdb-specific convenience; DAP doesn't separate params from locals, so this is `locals()` filtered by the function's parameter names (via `evaluate`/`stackTrace` frame name + introspection, or just printed as a locals subset — low priority). |
| `setvar(name, value)` | `p x = 5` (works via exec) | `set var x = 5` | `setVariable` / `setExpression` | `name`: locals/globals names in the current frame — same phase-2 mechanism as `p()`. `value`: none. | done | `evaluate(f"{name} = {value}", context="repl")` works (pydevd evaluates via `exec` for assignments in repl context). |
| `whatis(expression)` / `pt(expression)` | `whatis expr` | `whatis expr`/`ptype expr` | `evaluate` (`context="hover"`, inspect `type`/`result` fields) | `expression`: same phase-2 mechanism as `p()`. | done | `evaluate` response's `type` field is printed instead of `result`. |
| `display(expression)` / `undisplay(id)` | `display`/`undisplay` | `display`/`undisplay` | `evaluate`, re-run after each stop | `display`'s `expression`: same phase-2 mechanism as `p()`. `undisplay`'s `id`: ids from `SESSION.displays`. | done | Purely client-side: a list of expressions in `SESSION.displays`, re-`evaluate()`d and printed after every `_report_stopped`. |
| `exception_info()` | (pdb auto-prints traceback on uncaught exception) | (gdb shows signal info on stop) | `exceptionInfo` | — (no args) | done | Calls `exceptionInfo` for the current thread and prints `exceptionId`/`description`/`stackTrace`. Natural follow-up to `catch()`. |
| `completions(text, column)` | (handled by external `readline`/`rlcompleter`) | (gdb has its own completer) | `completions` | n/a — this command *is* the phase-2 completion backend (for `p`/`setvar`/`whatis`/`display`), not itself completable in a meaningful way. | done | Thin wrapper printing `completions` targets for the current frame; not normally called directly — backing for future REPL tab-completion. |


## 7. Source listing

| Command | pdb | gdb | DAP | Completion | Status | Notes |
|---|---|---|---|---|---|---|
| `list(first=None, last=None)` / `l()` | `l(ist) [first[, last]]` | `list`/`l [linespec]` | `source` (only for `sourceReference != 0`, i.e. no local file) | `first`/`last`: none (bare line numbers in the current file). | done | No args: ~10 lines centered on the current line. `first` only: window centered on that line (like pdb `list 20`). Both: that range, inclusive. Reads the local file directly around `current_frame`'s line — no DAP round trip needed. `source` request (for pydevd-synthesized/remote sources) is not yet wired up. |


## 8. Misc / introspection

| Command | pdb | gdb | DAP | Completion | Status | Notes |
|---|---|---|---|---|---|---|
| `modules()` | — | `info sharedlibrary` (loose analog) | `modules` | — (no args) | done | Lists loaded modules: id, name, path. |
| `pydevd_info()` | — | — | `pydevdSystemInfo` | — | done | Process/Python/platform info dump; debugging-the-debugger convenience. |


## Out of scope (pydevd has no support at all)

From `reference/dap_scope.md` §8 — no point modeling these as commands:

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
    **`args()`** remains **planned**: pdb's parameter-only view has no direct
    DAP equivalent (no clean way to get a function's parameter names without
    source/AST introspection of the debuggee) — low value, still not
    implemented.

Tab-completion itself (`completion_design.md`) is a separate, parallel track
— the signatures and "Completion" column above are written so that whenever
each command above is implemented, adding its completer later is additive
and doesn't require another signature change.
