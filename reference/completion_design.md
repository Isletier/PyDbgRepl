# Tab-completion design

Design for ptpython-based tab completion (see [[project_ptpython_embed]]):
debugger-focused completions by default, with per-argument completion for
commands (pdb-style `b <TAB>`), basename-based file lookup, a live switch
back to "classical" Python completion, and bare-int shortcuts like
`breakpoint(10)`.


## 0. Prerequisite: stop using `embed()`

ptpython's `embed()` convenience function builds its own `PythonRepl` and
doesn't accept a custom `Completer`. To plug in our own completer we
construct `PythonRepl` directly (it's `PythonInput` + a couple of
conveniences; `PythonInput.__init__` accepts `_completer=`) and call
`.run()` ourselves. `_configure_ptpython` (prompt style, etc.) still applies
the same way, just called manually after construction instead of via
`configure=`.

Cost: small, mechanical change to `_embed_ptpython`.


## 1. Completion modes

New option `completion: str = "debugger"` on `ReplOptions`
(`set("completion", "classical")` / `set("completion", "debugger")`),
read **live** on every completion request — no restart needed.

- `"classical"`: delegate straight to ptpython's normal completer
  (jedi over `__main__`, builtins, modules, dict keys, etc. — what you get
  today).
- `"debugger"` (default): see below.

Cost: trivial — one option field plus a branch at the top of
`get_completions()`.


## 2. Debugger-mode completer

A single `Completer` (`DebuggerCompleter`) wraps ptpython's default
completer (kept around for `"classical"` mode and as a fallback — see below)
and adds two debugger-specific behaviors:

### 2.1 Top-level completions: command names only

At the start of an expression (no enclosing call, i.e. nothing useful to be
argument-aware about), only complete the names in `commands.__all__`
(`run`, `stop`, `cont`, `breakpoint`, `p`, ...). No builtins, no modules, no
`__main__` globals/locals. This directly satisfies "omit everything else
related to python/its modules".

`exit`/`quit`/`_` (ptpython's "last result" var) stay completable since
they're part of using the REPL itself, not "Python internals" in the sense
the request means.

### 2.2 Argument-position-aware completions

When the cursor is inside the parentheses of a call to one of our commands,
complete based on *which argument* we're in, using a small per-command
table:

| Command | Arg 0 | Arg 1 | Arg 2+ |
|---|---|---|---|
| `breakpoint(path, line, condition=...)` | file completer (§3) | — (no completion; pdb doesn't complete line numbers either) | — |
| `clear(path, line)` | file completer | — | — |
| `thread(thread_id)` | live thread ids from `SESSION.dap.threads()` (if connected), as `id` with `name` shown in the completion menu | | |
| `frame(index)` | `0..len(stack)-1` from the current thread's stack trace (if paused) | | |
| `catch(*filters)` | exception filter ids from the `initialize` response's `exceptionBreakpointFilters` (fallback: `raised`/`uncaught`/`userUnhandled`) | (same, repeatable) | |
| `set(name, value)` | option names from `_options.list_options()` | — (depends on option; no completion for v1) | |

**Detecting "inside call X, argument N"**: a regex/bracket-depth scan of
`document.text_before_cursor` from the end — find the nearest unmatched `(`,
check the identifier immediately before it is a known command, then count
top-level commas since that `(` to get the argument index. This handles the
common case (`breakpoint("src/co<TAB>`, `thread(<TAB>`) but **not** nested
calls/brackets inside an argument (e.g. `breakpoint(foo(1, 2)<TAB>` would
misidentify the argument index). Given how these commands are actually
called, that's an acceptable v1 limitation — "classical" mode is always one
`set()` away if it gets in the way.

If we're not at top level and not recognizably inside one of our commands'
calls (e.g. typing `x.attr<TAB>` for some arbitrary expression), **debugger
mode offers nothing** — this is the "omit everything else" behavior the
request asks for. `"classical"` mode restores jedi-based expression
completion for these cases.

Cost: moderate — the bracket/arg-index scanner plus the small per-command
table. All of the above is purely client-side/local state; no new DAP calls.


## 3. File completer: basename matching with disambiguation

Plain `PathCompleter` only completes from `./`, `~/`, `/`, etc. prefixes.
We add a second mode: if the in-progress text has **no `/`**, treat it as a
basename search across the whole project tree, not just the cwd-relative
prefix.

**Index source** (rebuilt on demand — cheap enough to redo on every Tab for
project-sized repos):
- If `cwd` is inside a git repo: `git ls-files` + `git ls-files --others
  --exclude-standard`, combined. This automatically respects `.gitignore`
  (`.venv/`, `__pycache__/`, `*.egg-info/`, etc.) and is sub-10ms even on
  larger repos.
- Otherwise: `os.walk` from `cwd`, skipping dot-directories and a small
  hardcoded denylist (`__pycache__`, `*.egg-info`, `node_modules`, `.venv`).

**Matching**:
- Fragment contains `/` → classical path-prefix completion (delegate to
  `PathCompleter` / direct `os.listdir` on the parent dir), unchanged from
  today.
- Fragment has no `/` → match against `os.path.basename(p)` for every
  indexed path `p`, prefix-matched against the fragment.
  - Exactly one match → complete directly to that file's path relative to
    `cwd` (e.g. typing `sleepy` while `tests/fixtures/sleepy.py` is the only
    match completes to `tests/fixtures/sleepy.py`).
  - Multiple matches → offer all of them as completions (full relative
    paths) so the next keystroke disambiguates by typing more of the path —
    same flow as a normal path completion menu, just seeded by basename.
  - Zero matches → fall back to classical prefix completion (so `./`-style
    paths still work even with this enabled).

Cost: moderate — index building (a couple of subprocess/`os.walk` lines,
cached per-keystroke is fine) + the basename-vs-prefix dispatch.


## 4. Classical shortcuts: `breakpoint(10)` / `clear(10)`

A `_current_file()` helper:
1. If paused and `SESSION.current_frame_id` is set: the current frame's
   `source.path` (from `stack_trace`).
2. Else: `SESSION.run_ctx.args_opt.file` (the script given to `run()`).
3. Else: error ("no current file — pass an explicit path").

`breakpoint()`/`clear()` gain a one-argument overload: if called with a
single `int` (and no `path=`/`line=` kwargs), it's treated as
`breakpoint(_current_file(), that_int)`. The two-argument
`breakpoint(path, line, ...)` form is unchanged.

Cost: small — a helper function plus a couple of `isinstance(..., int)`
checks at the top of `breakpoint()`/`clear()`. Worth documenting as an
overload in `reference/command_reference.md` once implemented.


## Out of scope for this pass (phase 2)

- **`p()`/`setvar()` expression completion** via the DAP `completions`
  request (item 10 in `command_reference.md`'s planned-work list). This is
  the "real" pdb-style `p <TAB>` experience (completing variable/attribute
  names in the current frame) but needs a live DAP round trip per
  keystroke and only makes sense while paused — bigger, separate task.
- Line-number completion for `breakpoint`/`clear` (e.g. only offering
  executable lines) — needs source analysis, low value vs. cost.


## Implementation order (cost, roughly ascending)

1. `embed()` → manual `PythonRepl` construction (prerequisite, no visible
   behavior change).
2. `completion` option + mode dispatch in a (for now empty/pass-through)
   `DebuggerCompleter`.
3. Classical shortcuts (`breakpoint(10)`/`clear(10)`) — independent of
   completion work, can land any time.
4. Top-level command-name-only completion.
5. File completer (basename matching + disambiguation).
6. Argument-position-aware completion table (uses the file completer from
   step 5 for `breakpoint`/`clear`'s first argument).
7. *(phase 2)* DAP-backed `p()` expression completion.
