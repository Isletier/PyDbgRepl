# UI pluggability: ptpython dependencies and how to reduce them

`ptpython` is an **optional** dependency (`pip install pydev-repl[ptpython]`);
`prompt_toolkit` is a **hard** dependency (used directly by our highlighting,
completion, and keybindings modules). Today, though, those modules are only
*reachable* through ptpython -- if it's not installed, `ui="auto"` silently
falls back to a bare `code.InteractiveConsole` and the user loses
highlighting, debugger-aware completion, and the F5/F10/F11/F12 bindings all
at once, even though most of that machinery doesn't actually need ptpython.

This doc inventories every ptpython touchpoint, separates "ptpython-specific"
from "prompt_toolkit-generic" within each, and lays out a path to a middle
tier -- a `prompt_toolkit`-only frontend -- that keeps most of the rich UX
without the ptpython dependency.


## Inventory

### `src/__init__.py` -- the embed/dispatch layer

- `_ptpython_enabled()`: gates on `ui` option + whether `import ptpython`
  succeeds.
- `_embed_ptpython()`: constructs `ptpython.repl.PythonRepl` directly (not
  `embed()`, see `doc/completion_design.md` §0), wires in `make_lexer()`,
  `DebuggerCompleter`, `_configure_ptpython()`, `keybindings.install()`, then
  `repl.run()` under `patch_stdout`.
- `_configure_ptpython()`: sets `repl.all_prompt_styles["pydev"]`,
  `repl.prompt_style`, and merges into `repl._current_style` -- all
  `PythonRepl`-specific attributes/APIs (`all_prompt_styles` and the
  `PromptStyle` protocol are ptpython concepts; `_current_style` is a private
  ptpython attribute, not prompt_toolkit's).
- `_embed_readline()`: pure stdlib (`code.InteractiveConsole` +
  `sys.__interactivehook__`) -- no ptpython, no prompt_toolkit.
- `SESSION.ptpython_active`: a bool flag, read in one place
  (`commands/_internal.py`).

### `src/highlighting.py`

- Builds a `PygmentsLexer` subclassing `PythonLexer` (tags our command names
  with `COMMAND_TOKEN`) and a `Style` (`STYLE_OVERRIDES`).
- **Generic**: `Lexer`, `PygmentsLexer`, `Style`, pygments -- all
  prompt_toolkit/pygments, nothing ptpython-specific in the module itself.
- **ptpython-specific consumption**: `_embed_ptpython` passes
  `make_lexer()` to `PythonRepl(_lexer=...)` (a ptpython constructor kwarg)
  and merges `STYLE_OVERRIDES` into `repl._current_style` (a ptpython
  attribute).

### `src/completion.py`

- `DebuggerCompleter(Completer)` wraps a `wrapped: Completer` and adds
  debugger-mode behaviors (command-name-only top level, per-argument
  completions, file completer, etc).
- **Generic**: `Completer`/`Completion`/`Document` (prompt_toolkit), and
  100% of `DebuggerCompleter`'s own logic (`_ARG_TABLE`,
  `_find_call_context`, `file_completions`, ...) -- none of it touches
  ptpython.
- **ptpython-specific**: only what gets wrapped. `"classical"` mode
  delegates to `self.wrapped`, which `_embed_ptpython` sets to *ptpython's
  own default completer* (`repl.completer`, jedi-based, built by
  `PythonRepl.__init__`). Without ptpython there's no equivalent "classical"
  completer sitting around for free.

### `src/keybindings.py`

- `bind`/`unbind`/`reset`/`active_bindings`/`_run_action`: pure data + `eval`
  -- **generic**, no UI dependency at all.
- `install(repl)`: the only function that touches a UI object.
  - `repl.add_key_binding(key)` is a **ptpython** `PythonRepl` convenience
    method (registers into ptpython's own `KeyBindings` plus does some
    ptpython bookkeeping), not raw prompt_toolkit.
  - `run_in_terminal` (used inside `_run_action`'s caller indirectly via
    `install`) is **generic** prompt_toolkit.
- Per `doc/keybindings.md` §Scope: "ptpython only ... `install()` is a no-op
  when ptpython isn't active." That's the cliff this doc is about.

### `src/commands/_internal.py`

- `_async_print()`: branches on `SESSION.ptpython_active` to decide whether
  `patch_stdout` is already handling redraw (ptpython) or it needs to
  manually clear/redraw the readline prompt+buffer itself.
- The *real* condition isn't "is ptpython active" -- it's "is something
  running under `prompt_toolkit.patch_stdout`". Currently those happen to be
  the same thing, but only because ptpython is the only `patch_stdout` user.


## Summary table: ptpython-specific vs prompt_toolkit-generic

| Area | Generic (reusable as-is) | ptpython-specific (needs an adapter) |
|---|---|---|
| Highlighting | `make_lexer()`, `STYLE_OVERRIDES`, all of `highlighting.py` | The two call sites: `PythonRepl(_lexer=...)`, `repl._current_style` merge |
| Completion | `DebuggerCompleter` and everything it does in `"debugger"` mode | The `"classical"` fallback's `wrapped` completer (today: ptpython's jedi completer) |
| Keybindings | `bind`/`unbind`/`reset`/`active_bindings`/`_run_action`, `run_in_terminal` | `install()`'s use of `repl.add_key_binding` |
| Prompt style | `Style.from_dict` usage pattern | `_configure_ptpython`'s `all_prompt_styles`/`prompt_style`/`_current_style` (ptpython's `PromptStyle` protocol) |
| Async redraw | `prompt_toolkit.patch_stdout` itself | `SESSION.ptpython_active` as the *name* of the condition (should be "is patch_stdout active", not "is ptpython active") |

The pattern is consistent: **every piece of "rich UI" logic we've written is
already prompt_toolkit-generic**. The only ptpython-specific code is the thin
glue that hands these things to a `PythonRepl` instance and a couple of
ptpython-only conveniences (`add_key_binding`, `PromptStyle`,
`repl.completer` as the classical-mode fallback).


## Proposal: a third tier -- `prompt_toolkit`-only frontend

Since `prompt_toolkit` is already a hard dependency, a middle tier between
ptpython and bare readline is mostly free:

`prompt_toolkit.PromptSession(lexer=..., completer=DebuggerCompleter(...),
key_bindings=..., style=..., message=<dynamic prompt>)` run in a loop with
`patch_stdout()`, `eval`/`exec`-ing each line against `__main__` (same
namespace contract `_embed_readline` already follows).

What this tier gets, with **zero new third-party deps**:

- Syntax highlighting (`make_lexer`/`STYLE_OVERRIDES` -- unchanged).
- Full `DebuggerCompleter` "debugger" mode (unchanged). "classical" mode
  needs *some* completer to wrap -- `prompt_toolkit` ships
  `WordCompleter`/`PathCompleter` but nothing jedi-based; either accept a
  weaker "classical" fallback here, or make jedi-based completion an
  optional extra independent of ptpython (jedi is ptpython's dependency, not
  ours -- would need adding).
- Real key bindings: `KeyBindings()` object passed to
  `PromptSession(key_bindings=...)`, built from the same `active_bindings()`
  table `install()` already produces -- `add_key_binding` is sugar over
  exactly this.
- A dynamic prompt via `message=` (a callable), replacing
  `_PydevPromptStyle`/`_configure_ptpython`'s ptpython-specific
  `PromptStyle` protocol with a plain prompt_toolkit `message` callable +
  `Style`.
- `_async_print`'s `patch_stdout`-handles-it branch applies here too --
  confirms the flag should be keyed on "running under `patch_stdout`", not
  "is ptpython".

What's lost vs. ptpython: multi-line editing UX niceties, history-based
auto-suggest, the sidebar/status bar, vi/emacs mode toggle, and other
ptpython "IDE" extras -- the things that are genuinely ptpython's value-add
beyond raw prompt_toolkit.


## Refactor sketch

1. **Rename the condition, not (yet) the behavior.** `SESSION.ptpython_active`
   -> something like `SESSION.rich_ui_active` (or reuse for a `ui_kind: str`
   enum: `"ptpython" | "prompt_toolkit" | "readline"`). `_async_print` keys
   off "redraw handled by `patch_stdout`" (true for the first two). Pure
   rename + trivial logic change, no new frontend yet -- safe first step.

2. **Extract adapters** for the two ptpython-specific glue points so both
   `_embed_ptpython` and a future `_embed_prompt_toolkit` can share the
   *generic* halves:
   - lexer/style attachment (`PythonRepl(_lexer=...)` + `_current_style`
     merge vs. `PromptSession(lexer=..., style=...)`).
   - keybindings attachment (`repl.add_key_binding` vs.
     `KeyBindings().add(...)` passed to `PromptSession`).
   - prompt-style (`_PydevPromptStyle`/`PromptStyle` vs. a `message=`
     callable).

   None of this touches `highlighting.py`, `completion.py`'s `"debugger"`
   mode, or `keybindings.py`'s data model -- they're already
   frontend-agnostic.

3. **Implement `_embed_prompt_toolkit()`** using `PromptSession` per the
   proposal above. Reuses everything from step 2.

4. **Extend the `ui` option**: `"auto"` becomes
   ptpython (if installed) -> prompt_toolkit -> readline (last resort, e.g.
   `TERM=dumb` or no tty). `"ptpython"`/`"prompt_toolkit"`/`"readline"`
   force a specific tier (erroring like today's `_ptpython_enabled()` does
   if the forced tier can't actually run).

5. **Docs**: `doc/keybindings.md`'s "ptpython only" caveat shrinks to
   "readline only has no key bindings"; `doc/completion_design.md`'s
   `"classical"` mode description gains a note about the prompt_toolkit
   tier's weaker fallback (or the jedi-as-optional-extra decision).


## Relation to scenario/batch mode

The interactive/batch axis (`doc/scenario_mode.md`) and the UI-tier axis here
are orthogonal: `--batch` skips frontend selection entirely (no prompt of any
kind). Once `scenario_mode.md`'s planned `enter_repl()` exists as the single
explicit dispatch point (replacing the current `atexit` hook), it's also the
natural place to host the `ui="auto"` tier-selection logic from step 4 above
-- one function, one decision tree, called once, outside of interpreter
shutdown.


## Cost summary

| Step | Cost | New deps |
|---|---|---|
| 1. Rename `ptpython_active` condition | trivial | none |
| 2. Extract lexer/style/keybinding adapters | small-moderate, mechanical | none |
| 3. `_embed_prompt_toolkit()` | moderate | none |
| 4. `ui="auto"` 3-tier ordering + forced-tier errors | small | none |
| (optional) jedi-based "classical" completion w/o ptpython | small | `jedi` (currently transitive via ptpython only) |

Nothing here requires dropping ptpython support -- it stays as the richest
tier for users who opt in. The deliverable is that **not having ptpython
installed** no longer means falling off a cliff to plain readline.
