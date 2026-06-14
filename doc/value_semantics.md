# Command return values: "value semantics"

## Principle

Every command function in `src/commands/` returns an object whose `__repr__`
reproduces what the command used to (or would otherwise) `print()`. Nothing
calls `print()` directly anymore (with the rare `n/a` exceptions noted
below). At the interactive prompt, the REPL auto-echoes the `repr()` of any
non-`None` expression-statement result, so e.g.

```
>>> locals()
x = 1
y = "hello"
```

looks exactly like a `print()`-based command always did. But the value is
also *real data*: assign it to a variable, slice it, iterate it, check its
fields — `locals()["x"]`, `[t["id"] for t in threads()]`, `if not p("x.y"):
...`, etc. Scripts get structured data; humans get readable text; one
implementation serves both.

This is the same idea as Python's own `repr()`/`str()` split, applied
systematically: a command's return value is the "real" result, and its
`__repr__` is just how that result happens to look when nothing consumes it.

All of these wrapper types live in `src/commands/_display.py`.


## Success values

Most commands return a `list`/`dict` subclass that *is* the underlying DAP
data (raw `variables`/`threads`/`stackFrames`/... dicts), with a `__repr__`
that formats it for humans:

- **`Scope(list)`** — `locals()`/`globals_()`: `"name = value"` per line.
- **`ThreadList(list)`** — `threads()`: `"* id: name"`, `*` on the current thread.
- **`FrameList(list)`** — `bt()`: `"* #i name at path:line"`, `*` on the current frame.
- **`ModuleList(list)`** — `modules()`: `"id: name (path)"` per line.
- **`InfoSections(dict)`** — `pydevd_info()`: `"section:"` headers with indented `key = value`.
- **`ExceptionInfo(dict)`** — `exception_info()`: exception id/description + stack trace.
- **`CompletionList(list)`** — `completions()`: one completion label per line.
- **`SourceLines(list)`** — `list()`/`l()`: `(lineno, text)` pairs with a `->` marker on the current line.
- **`Breakpoints`** — `breakpoints()`: line/function breakpoints + exception filters, one per line.
- **`FrameRef(dict)`** — `frame()`/`up()`/`down()`: a single raw stack-frame
  dict, reprs as `"#index name at path:line"`.

For commands whose "result" isn't a data structure but a one-line
confirmation (`set()`, `breakpoint()`, `stop()`, `interrupt()`, ...), the
return value is a **`Status(str)`** — a `str` subclass whose `__repr__` omits
the quotes a plain `str` would get, so it echoes exactly like the old
`print()` line did, while `str(result)` or using it as a normal string works
for scripts.

**`StopResult`** is the result of any blocking resume (`cont()`, `step()`,
`next()`, `finish()`, `until()`, `jump()`, `connect()`, `run()`,
`restart()`). It carries `event` (`"stopped"`/`"exited"`/`"terminated"`/
`"_disconnected"`), the raw event `body`, and — for `"stopped"` — the new
`top_frame`. Its `__repr__` reproduces the old `"*** stopped (...)"` /
`"*** program exited with code N"` etc. lines. A `prefix` field carries any
status lines that used to be printed *before* the outcome (`"continuing"`,
`"launched pid=..."`, `"connected to host:port"`, `"killing previous
instance"`) — these are folded into one returned object instead of being
printed immediately, so the whole operation produces exactly one echoed
value.


## Error values

Symmetrically, error paths return an **`Error`** value instead of `print(f"error:
...")` followed by `return None`. `Error` is a `Status` subclass (so it's
still a `str`, and still reprs without quotes — `error: not connected` looks
the same as it always did) that additionally has `__bool__` returning
`False`. This gives scripts a natural failure check:

```python
result = p("x.frobnicate()")
if not result:
    ...  # result is an Error; str(result) is the message
```

while a success value (`Status`, `Scope`, `StopResult`, ...) is truthy —
empty collections like `Scope([])` or `ThreadList([])` still repr as
`"(empty)"`/`"(no threads)"` but are *not* `Error`s, so `not locals()` is
`False` even when there are no locals. Truthiness signals "did the command
itself fail", not "is the result empty".

Because `Error` is a `Status`, every command's return type is uniformly "some
object with a `__repr__`" — there is no `None` return path left to special-case,
and `if not result:` is the one idiom for "did this fail" across the whole
command surface.


## What doesn't follow this convention

A few commands are genuinely fire-and-forget or have no meaningful result to
report, and keep returning `None`:

- Commands that are pure `n/a`/no-ops by design (see
  `command_reference.md`'s "Out of scope" section) don't exist as functions
  at all, so this doesn't apply to them.
- Internal helpers in `_internal.py` (not part of the public command surface)
  are unaffected — this convention is about the functions injected into
  `__main__`.

If a new command is added and it's unclear which category it falls into: if
it has *any* user-visible side effect or outcome (including failure), it
returns a value (`Status`/`Error`/a dedicated wrapper); only truly
side-effect-free, can't-fail operations would justify `None`, and in practice
none of the current commands qualify.


## Known limitation: `Status` is a stub for scripting

`Status` was designed to make the *human-readable* line a real return value
(see "Success values" above), but for many commands it's currently just that
one formatted string with no structured data behind it — so while
`if not result:` works, scripts can't get at the actual value without
re-parsing the `repr()`. This was an oversight: scripting use was the whole
point of this convention, and `Status` as a bare string doesn't deliver it
for these commands. Notable offenders:

- **`set(name, value)`/`get(name)`/`reset(name)`** — return `Status(f"{name}
  = {value!r}")`. A script calling `get("port")` gets back the string
  `"port = 25516"`, not `25516` or `{"port": 25516}`. For a single option
  these should probably return the value itself (or `(name, value)`); for a
  group, a dict/mapping type a la `InfoSections`.
- **`breakpoint()`/`tbreak()`/`enable()`/`disable()`/`clear()`/`ignore()`/
  `catch()`** — return a one-line `Status` confirmation instead of the
  breakpoint dict(s) affected, so scripts can't chain off the new
  breakpoint's id/line/etc.
- **`stop()`/`disconnect()`/`terminate()`/`interrupt()`** — `Status("...")`
  is probably fine as-is (genuinely no structured result), but worth
  revisiting once the above are reworked, for consistency.

This needs a proper pass per-command (not a blanket type swap — each
command's "useful structured value" differs), but should happen before
relying on these commands from scripts beyond truthiness checks.
