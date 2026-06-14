# Scenario mode: `repl.py` as a transcript

`repl.py` is meant to double as a **record of a debugging session**: setup,
then plain Python lines using the REPL commands (`run()`, `cont()`, `bt()`,
...), then -- by default -- an interactive prompt to keep poking around.

```python
import src as debug

debug.process_args_envs(sys.argv[1:])
debug.start_eval()

# scenario: plain Python, runs top to bottom like any script
bt(5)
p("x.frobnicate()")

# falls through to an interactive prompt here, unless --batch
```

## How it works

`start_eval()` injects the REPL commands into `__main__` and **returns**
immediately -- it no longer embeds a REPL or exits the process itself. Any
code after it in `repl.py` runs as a normal script body ("the scenario").

Once that script body finishes (normally or via an uncaught exception),
`atexit` fires `_enter_repl()`, which hands control to:

- **ptpython**, if enabled (`ui` option, see `doc/keybindings.md`), or
- **`code.InteractiveConsole`** over `__main__`'s namespace otherwise --
  `sys.__interactivehook__()` is called first to get the same
  readline history/tab-completion setup `python -i` would normally provide.

This is why the shebang is plain `#!/usr/bin/env python3` (no `-i`): `-i`'s
own fallback console would only kick in *after* the scenario body and after
`atexit`, fighting over the same slot. Dropping it removes that conflict, and
costs nothing -- `_enter_repl()` reproduces `-i`'s readline behavior for the
non-ptpython case.

## Known issue: `atexit` + ptpython's asyncio usage is broken

**Status: the `atexit`-based entry described above does not actually work
when ptpython is the active UI and is pending a fix.**

Symptom: typing anything (even before the prompt fully renders) produces
`Unhandled exception in event loop` / `RuntimeError: cannot schedule new
futures after interpreter shutdown` tracebacks through
`concurrent.futures.thread` and `prompt_toolkit`'s async completer.

Root cause: `_enter_repl()` runs as an `atexit` callback, i.e. during
interpreter finalization. ptpython's `repl.run()` is asyncio-based by
design -- it runs its own event loop and uses `ThreadPoolExecutor` (via
`run_in_executor`) for jedi completions/signature help off the UI thread.
CPython's asyncio/`concurrent.futures` machinery does not support spinning up
*new* event loops or thread pools once interpreter finalization has begun
(`concurrent.futures.thread._shutdown` and friends are already torn down by
that point). This isn't a ptpython bug per se -- any sufficiently
async-heavy code would hit the same wall if invoked from `atexit`; ptpython
just happens to need exactly that.

An eager `import concurrent.futures.thread` before `atexit.register()` (to
get `threading._register_atexit` to run early) was tried and did **not**
fix it -- the failure is the broader "no new event loops/executors during
finalization" constraint, not just that one registration call.

**Planned fix**: drop `atexit` entirely. `start_eval()` returns after setup
as today; the wrapper script ends with one explicit call (tentatively
`debug.enter_repl()`) that does today's ptpython-embed/readline-console
dispatch. Running as a normal statement (not during shutdown) means
ptpython's asyncio usage works exactly as it always has. Cost: one extra
line at the end of `repl.py`. The rest of this doc (scenario lines, no
auto-echo, `--batch`/no-implicit-cleanup) is unaffected by this change.

## Caveats for scenario lines

- **No auto-echo.** Inside the interactive prompt, non-`None` results are
  echoed via `repr()` ("value semantics", see `doc/value_semantics.md`).
  Scenario lines are plain statements in a script -- nothing auto-prints
  their value. Use `print(repr(...))` (or wrap in a small helper) if you want
  that.
- **Errors still get you to a prompt.** If a scenario line raises, the
  traceback prints and `atexit` still runs `_enter_repl()` afterwards --
  similar to how `python -i` used to leave you at a prompt after a script
  error.

## Batch mode (`--batch` / `interactive=False`)

For unattended automation, pass `--batch` on the command line (or
`set("interactive", "0")`/edit `ReplOptions.interactive` before
`start_eval()`). This skips the `atexit` registration entirely: once the
scenario body finishes, the process just exits -- no prompt.

```sh
./repl.py --batch --file target.py
```

```python
debug.start_eval()

run()
cont()
bt(5)
# script ends here -> process exits, no prompt
```

**No implicit cleanup.** If the scenario launched a debuggee (`run()`) and
doesn't call `stop()`/`terminate()`, the spawned child process is not killed
just because our process exits -- it's a separate OS process, not a daemon
thread. Add an explicit `stop()`/`terminate()` at the end of the scenario if
you want the debuggee torn down.
