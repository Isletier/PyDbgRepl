"""Extensible single-key shortcuts for the ptpython prompt.

See doc/keybindings.md for the design. `bind`/`unbind`/`reset` edit an
in-memory table; `install()` turns the current table into real
prompt_toolkit key bindings on a `PythonRepl` instance, and is called once
from `pdvp.extra._run_ptpython` during startup.
"""
from __future__ import annotations

from typing import Callable

def _f5_action() -> object:
    """cont() if a session is active, else run() with the saved context (e.g.
    --file at startup, or a previous run()'s script/args)."""
    from pdvp.core.session import SESSION
    from pdvp.core import commands

    if SESSION.client is None:
        return commands.run()
    return commands.cont()


# key (prompt_toolkit key spec, e.g. "f5", "c-x") -> action.
_DEFAULT_BINDINGS: dict[str, str | Callable[[], object]] = {
    "f5": _f5_action,
    "f10": "next()",
    "f11": "step()",
    "f12": "finish()",
}

_bindings: dict[str, str | Callable[[], object]] = dict(_DEFAULT_BINDINGS)


def bind(key: str, action: str | Callable[[], object]) -> None:
    """Bind `key` to `action`, replacing any existing binding for that key.

    `action` is either a string of Python source, `eval()`'d in `__main__`'s
    namespace each time the key is pressed, or a zero-argument callable.
    """
    _bindings[key.lower()] = action


def unbind(key: str) -> None:
    """Remove the binding for `key` entirely, including defaults."""
    _bindings.pop(key.lower(), None)


def reset(key: str | None = None) -> None:
    """Restore `key` to its default binding, or every key if `key` is omitted.

    Keys with no default are unbound.
    """
    if key is None:
        _bindings.clear()
        _bindings.update(_DEFAULT_BINDINGS)
        return
    key = key.lower()
    if key in _DEFAULT_BINDINGS:
        _bindings[key] = _DEFAULT_BINDINGS[key]
    else:
        _bindings.pop(key, None)


def active_bindings() -> dict[str, str | Callable[[], object]]:
    """Currently active {key: action}, for introspection."""
    return dict(_bindings)


def _run_action(action: str | Callable[[], object]) -> None:
    import __main__

    try:
        if isinstance(action, str):
            result = eval(action, vars(__main__))
        else:
            result = action()
    except Exception as e:
        print(f"error: {e}")
        return
    if result is not None:
        print(repr(result))


def install(repl) -> None:
    """Register every active binding as a real key binding on `repl`.

    Called once during ptpython setup, after any user customization
    (bind()/unbind()/reset()) has run.
    """
    from prompt_toolkit.application import run_in_terminal

    for key, action in active_bindings().items():
        @repl.add_key_binding(key)
        def _(event, action=action):
            run_in_terminal(lambda: _run_action(action))
