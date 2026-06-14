# Key-binding automation

An extensible layer of single-key shortcuts on top of the command surface
(`cont()`, `next()`, ...), for ptpython's prompt. Lives in `src/keybindings.py`.


## Defaults

| Key   | Action                                  | Same as       |
|-------|------------------------------------------|---------------|
| F5    | `cont()`, or `run()` if not yet launched | continue / launch |
| F10   | `next()`                                  | step over     |
| F11   | `step()`                                  | step into     |
| F12   | `finish()`                                | step out      |

These mirror the Visual Studio / VS Code execution-control layout. F5's
"launch or continue" behavior matches those tools too: with no active
session (`SESSION.dap is None`), F5 calls `run()` with whatever
script/args/redirections are currently saved on `SESSION.run_ctx` (the
`--file` given at startup, or a previous `run()`'s arguments) rather than
doing nothing.

Each binding is just an entry in a plain dict (`str`/zero-arg callable), so
there's nothing special about the defaults -- a user override looks identical
to a default, including F5's.


## Action model

A binding's action is either:

- a **string** of Python source, `eval()`'d in `__main__`'s namespace each
  time the key is pressed (e.g. `"cont()"`, `"p('x.frobnicate()')"`,
  `"bt(5)"`); or
- a **zero-argument callable**, called directly.

Either way, if the result is not `None`, its `repr()` is printed -- the same
"value semantics" (see `value_semantics.md`) as typing the equivalent
expression at the prompt and letting it auto-echo. Exceptions are caught and
printed as `error: ...` rather than producing a traceback in the middle of
the prompt.

Printing happens via prompt_toolkit's `run_in_terminal`, so it doesn't
corrupt the prompt's display even though it runs from inside a key-press
handler rather than the normal eval loop.


## API (`src/keybindings.py`)

```python
def bind(key: str, action: str | Callable[[], object]) -> None:
    """Bind `key` (a prompt_toolkit key spec, e.g. "f5", "c-x") to `action`,
    replacing any existing binding (default or user-defined) for that key."""

def unbind(key: str) -> None:
    """Remove the binding for `key` entirely, including defaults."""

def reset(key: str | None = None) -> None:
    """Restore `key` to its default binding, or every key if `key` is None.
    Keys with no default are unbound."""

def active_bindings() -> dict[str, str | Callable[[], object]]:
    """Currently active {key: action}, for introspection."""
```

`key` is whatever prompt_toolkit's `KeyBindings.add()` accepts as a string --
named keys (`"f5"`...`"f12"`, `"escape"`, ...) and control-chord shorthand
(`"c-x"`, `"c-up"`, ...). See prompt_toolkit's `Keys` enum for the full list.


## Customization in `repl.py`

`bind`/`unbind`/`reset` just edit an in-memory table; the table is only
turned into real prompt_toolkit key bindings once, when the ptpython REPL is
constructed (`install()`, called from `_embed_ptpython`). So overrides go in
the wrapper script's "user customization" section, before `start_eval()`:

```python
import src as debug
from src import keybindings

# Free F12 up for something else, drop the default.
keybindings.unbind("f12")

# Re-map F11 to print the current frame's locals instead of step().
keybindings.bind("f11", "locals()")

# Custom shortcut with a callable, for logic too involved for one expression.
def _show_context():
    bt(3)
    return locals()

keybindings.bind("f4", _show_context)

debug.start_eval()
```


## Scope and limitations

- **ptpython only.** This is a prompt_toolkit key-binding layer; the
  `"readline"` UI (see `session.py`'s `ui` option) has no equivalent, and
  `install()` is a no-op when ptpython isn't active.
- A binding fires regardless of what's currently typed at the prompt -- it
  does not touch or submit the input buffer, it just runs the action and
  prints its result above the prompt. Actions that depend on "what the user
  was about to type" aren't supported; write a command/expression instead.
- No support for chorded/sequence bindings beyond what prompt_toolkit's key
  strings already express (e.g. `"c-x c-s"`-style sequences aren't
  specifically tested, though prompt_toolkit itself allows multi-key specs).
