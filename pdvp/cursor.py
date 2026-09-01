"""The cursor: which inferior thread and frame a caller's next command means.

Ambient per-caller state is unavoidable here. A human types `bt()`, not
`bt(thread=3, frame=0)`, and threading a thread id through every command is the
one thing a REPL cannot ask of the person using it. So the cursor is implicit --
and it is the only thing in pdvp a concurrency model can disagree with us about.

Python has several, and they disagree about what "the same caller" even *is*:
two pool jobs on one worker, two asyncio tasks on one thread, two greenlets.
Nothing can decide that for the caller, so we do not try. The cursor lives in a
table we own, keyed by whatever `scope()` returns, and `scope()` is one function
a caller replaces:

    import greenlet, pdvp
    pdvp.cursor.scope = greenlet.getcurrent

Owning the table rather than borrowing a ContextVar or a threading.local buys
three things none of those can give: the cursors can be **enumerated** (they are
otherwise invisible, which was the real complaint), **cleared from any thread**
when the session they belong to dies, and isolated per `Session` in tests.
"""
import dataclasses
import threading
import weakref


class _Shared:
    """The identity for a `scope()` that names nobody -- see `owner()`.

    A class rather than a bare `object()` because the table holds weak keys and
    `object()` instances cannot be weak-referenced. Anything defining
    `__weakref__` can, which every normal class does by default.
    """


_SHARED = _Shared()


def scope() -> object | None:
    """Who is asking: the identity two commands must share to mean one cursor.

    Replace this to match your concurrency model. The token must be hashable,
    **weak-referenceable**, and stable for the lifetime of one logical caller;
    returning None means "one caller, no per-caller state", which is what a
    plain script wants. Thread, Task and greenlet objects all qualify; a bare
    `object()` does not, which is the one surprise here.

    The default is the thread object rather than `threading.get_ident()`,
    which is what the same pattern usually keys on elsewhere: an ident is an
    int the OS reuses once a thread dies, so a new thread can inherit a dead
    one's cursor. A Thread cannot be confused with its successor, and being
    weak-referenceable is what lets its entry disappear on its own.

    Set it before the session starts. Changing it later orphans every existing
    entry rather than migrating it.
    """
    return threading.current_thread()


def owner() -> object:
    """`scope()`, guaranteed to name something. The key the tables use.

    Also who holds a thread control right, so a sequence and the cursor it
    moves belong to the same caller by construction. A `scope()` returning None
    collapses every caller onto one identity, which makes the right reentrant
    everywhere -- correct, because in that model there is only one caller.
    """
    key = scope()
    return _SHARED if key is None else key


@dataclasses.dataclass(frozen=True)
class FrameHandle:
    """A frame, plus the two things that decide whether it may still be read."""

    thread_id: int
    epoch: int
    frame_id: int


@dataclasses.dataclass
class Cursor:
    """One caller's selection.

    `generation` is the connection the selection was made in. pydevd hands out
    small thread ids and reuses them across runs, so without it a `thread(3)`
    from the previous session silently names a different thread 3 in this one --
    and it would win over the session-wide default, turning "no thread selected"
    into "the wrong thread selected".
    """

    generation: int = 0
    thread_id: int | None = None
    frame: FrameHandle | None = None


class Cursors:
    """The cursor table, keyed by `owner()`.

    Weak keys: a caller that has gone away takes its cursor with it, without
    anything having to notice it left.
    """

    def __init__(self) -> None:
        # Never held across anything that can block: every critical section
        # here is one dict operation.
        self._lock = threading.Lock()
        self._table: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def get(self) -> Cursor | None:
        """This caller's cursor, or None if it never selected one."""
        with self._lock:
            return self._table.get(owner())

    def own(self) -> Cursor:
        """This caller's cursor, created on first selection."""
        key = owner()
        with self._lock:
            cursor = self._table.get(key)
            if cursor is None:
                cursor = self._table[key] = Cursor()
            return cursor

    def clear(self) -> None:
        """Drop every cursor, from whatever thread this runs on.

        The operation neither a ContextVar nor a threading.local can offer, and
        the reason the table is ours: a lifetime reset can actually reset it.
        """
        with self._lock:
            self._table.clear()

    def all(self) -> list[tuple[object, Cursor]]:
        """Every caller that has selected something. What `cursors()` renders."""
        with self._lock:
            return list(self._table.items())
