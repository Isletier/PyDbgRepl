"""pdvp's event vocabulary and the bus that delivers it.

This is the only place anything waits for an event. Commands are not a special
case: a blocking `cont()` opens a private, short-lived subscription exactly the
way user code opens a long-lived one.

Events are pdvp types, not DAP messages. Some are translated from a DAP event
by `from_dap()`; others -- `SessionStarted`, `SessionEnded` -- are ours, with no
DAP counterpart. A subscriber cannot tell which is which, and that is the point:
the frontend can add events without the debugger core having anything to say.

Two rules the rest of the system leans on:

  * `SessionEnded` is the one event for the end of things. The debuggee exiting,
    the debuggee being terminated, and the connection dying are three signals for
    one fact, because we tie the inferior's lifetime to the connection's. The
    first signal wins and latches, so one ending is one event.

  * Every subscription carries `SessionEnded` whether or not it asked for it, so
    every wait is woken by exactly one of: its event arriving, or the session
    ending. A waiter must expect it; `get()` returns it like any other event.

There is no dispatch thread and no callback registry. A subscription is a
blocking queue the subscriber owns and drains on its own thread.
"""
from __future__ import annotations

import queue
import threading
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping

__all__ = [
    "Event", "ThreadEvent",
    "Initialized", "Stopped", "Continued", "ThreadStarted", "ThreadExited",
    "Output", "BreakpointChanged", "ModuleChanged", "LoadedSource",
    "ProcessStarted", "CapabilitiesChanged", "UnhandledDapEvent",
    "SessionStarted", "SessionEnded", "EndReason",
    "EventBus", "Subscription", "SubscriptionClosed", "BusClosed",
    "from_dap",
]


# ---------------------------------------------------------------- vocabulary

_BY_NAME: dict[str, type["Event"]] = {}


@dataclass(frozen=True)
class Event:
    """Base of every event on the bus.

    Immutable, so fan-out costs N references rather than N copies and no
    subscriber can corrupt another's view. Subclasses are declared statically --
    subscribing to a name that does not exist is an error, not an empty stream.

    Frozen but not slotted, so a subclass may cache a lazily fetched attribute
    (`functools.cached_property` writes straight into `__dict__`).
    """

    name: ClassVar[str] = "event"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registered = _BY_NAME.setdefault(cls.name, cls)
        if registered is not cls:
            raise TypeError(f"event name {cls.name!r} is already taken by {registered.__name__}")


@dataclass(frozen=True)
class ThreadEvent(Event):
    """Family base for everything that is about one inferior thread.

    Subscribable on its own: `bus.subscribe(ThreadEvent)` takes the whole family.
    """

    name: ClassVar[str] = "thread_event"

    thread_id: int | None


# ---- core: translated from DAP -------------------------------------------

@dataclass(frozen=True)
class Initialized(Event):
    name: ClassVar[str] = "initialized"


@dataclass(frozen=True)
class Stopped(ThreadEvent):
    name: ClassVar[str] = "stopped"

    reason: str
    description: str | None = None
    text: str | None = None
    all_threads: bool = False
    hit_breakpoint_ids: tuple[int, ...] = ()
    preserve_focus: bool = False
    # The stop epoch this event belongs to; filled in by the reducer, which is
    # the only thing that knows it. Handles minted here are valid only at it.
    epoch: int | None = None


@dataclass(frozen=True)
class Continued(ThreadEvent):
    name: ClassVar[str] = "continued"

    all_threads: bool = False
    epoch: int | None = None


@dataclass(frozen=True)
class ThreadStarted(ThreadEvent):
    name: ClassVar[str] = "thread_started"


@dataclass(frozen=True)
class ThreadExited(ThreadEvent):
    name: ClassVar[str] = "thread_exited"


@dataclass(frozen=True)
class Output(Event):
    name: ClassVar[str] = "output"

    text: str
    category: str = "console"
    source_path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class BreakpointChanged(Event):
    name: ClassVar[str] = "breakpoint_changed"

    reason: str                 # new | changed | removed
    breakpoint_id: int | None
    verified: bool = False
    line: int | None = None
    path: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ModuleChanged(Event):
    name: ClassVar[str] = "module_changed"

    reason: str                 # new | changed | removed
    module_id: int | str | None
    module_name: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class LoadedSource(Event):
    name: ClassVar[str] = "loaded_source"

    reason: str                 # new | changed | removed
    path: str | None = None
    source_reference: int | None = None
    source_name: str | None = None


@dataclass(frozen=True)
class ProcessStarted(Event):
    name: ClassVar[str] = "process_started"

    process_name: str
    pid: int | None = None
    is_local: bool = True
    start_method: str | None = None


@dataclass(frozen=True)
class CapabilitiesChanged(Event):
    name: ClassVar[str] = "capabilities_changed"

    capabilities: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class UnhandledDapEvent(Event):
    """A DAP event with no pdvp counterpart.

    Published rather than dropped: a core that grows a new event should show up
    in a subscriber's stream, not vanish silently.
    """

    name: ClassVar[str] = "unhandled_dap_event"

    dap_name: str
    body: Any = None


# ---- ours: no DAP counterpart --------------------------------------------

@dataclass(frozen=True)
class SessionStarted(Event):
    """A connection is up and the handshake is done. Re-arms the end latch."""

    name: ClassVar[str] = "session_started"

    pid: int | None = None


class EndReason(StrEnum):
    EXITED = "exited"                # the debuggee ran to completion
    TERMINATED = "terminated"        # the debuggee was terminated
    DISCONNECTED = "disconnected"    # the connection died under us
    CLOSED = "closed"                # we closed it


@dataclass(frozen=True)
class SessionEnded(Event):
    """The end of things: debuggee exit, termination, or connection death.

    One event for all three, because in pdvp they are one fact -- the DAP
    connection and any process we spawned share a lifetime. Whichever signal
    arrives first wins, so `exit_code` is set when we learned it from `exited`
    and None when the connection died before we could.
    """

    name: ClassVar[str] = "session_ended"

    reason: EndReason
    exit_code: int | None = None
    detail: str = ""


# ---------------------------------------------------------- DAP translation

def _field(body: Any, name: str, default: Any = None) -> Any:
    """Read `name` off a schema body object or the plain dict we fell back to."""
    if isinstance(body, dict):
        return body.get(name, default)
    value = getattr(body, name, default)
    return default if value is None else value


def _thread(body: Any) -> Event:
    reason = _field(body, "reason")
    thread_id = _field(body, "threadId")
    return ThreadExited(thread_id) if reason == "exited" else ThreadStarted(thread_id)


def _stopped(body: Any) -> Event:
    return Stopped(
        thread_id=_field(body, "threadId"),
        reason=_field(body, "reason", ""),
        description=_field(body, "description"),
        text=_field(body, "text"),
        all_threads=bool(_field(body, "allThreadsStopped", False)),
        hit_breakpoint_ids=tuple(_field(body, "hitBreakpointIds", ()) or ()),
        preserve_focus=bool(_field(body, "preserveFocusHint", False)),
    )


def _continued(body: Any) -> Event:
    # Spec asymmetry worth not getting backwards: a missing allThreadsStopped
    # means only that thread stopped, but a missing allThreadsContinued means
    # *all* threads resumed.
    all_threads = _field(body, "allThreadsContinued")
    return Continued(
        thread_id=_field(body, "threadId"),
        all_threads=True if all_threads is None else bool(all_threads),
    )


def _breakpoint(body: Any) -> Event:
    bp = _field(body, "breakpoint")
    source = _field(bp, "source") or {}
    return BreakpointChanged(
        reason=_field(body, "reason", ""),
        breakpoint_id=_field(bp, "id"),
        verified=bool(_field(bp, "verified", False)),
        line=_field(bp, "line"),
        path=_field(source, "path"),
        message=_field(bp, "message"),
    )


def _module(body: Any) -> Event:
    module = _field(body, "module")
    return ModuleChanged(
        reason=_field(body, "reason", ""),
        module_id=_field(module, "id"),
        module_name=_field(module, "name"),
        path=_field(module, "path"),
    )


def _loaded_source(body: Any) -> Event:
    source = _field(body, "source")
    return LoadedSource(
        reason=_field(body, "reason", ""),
        path=_field(source, "path"),
        source_reference=_field(source, "sourceReference"),
        source_name=_field(source, "name"),
    )


def _output(body: Any) -> Event:
    source = _field(body, "source") or {}
    return Output(
        text=_field(body, "output", ""),
        category=_field(body, "category", "console") or "console",
        source_path=_field(source, "path"),
        line=_field(body, "line"),
    )


def _process(body: Any) -> Event:
    return ProcessStarted(
        process_name=_field(body, "name", ""),
        pid=_field(body, "systemProcessId"),
        is_local=bool(_field(body, "isLocalProcess", True)),
        start_method=_field(body, "startMethod"),
    )


def _capabilities(body: Any) -> Event:
    caps = _field(body, "capabilities") or {}
    if not isinstance(caps, dict):
        caps = getattr(caps, "kwargs", {})
    return CapabilitiesChanged(MappingProxyType(dict(caps)))


# DAP event name -> the pdvp event it means. `exited` and `terminated` both
# land on SessionEnded; the bus latch turns the pair into one ending.
_FROM_DAP: dict[str, Callable[[Any], Event]] = {
    "initialized": lambda body: Initialized(),
    "stopped": _stopped,
    "continued": _continued,
    "thread": _thread,
    "output": _output,
    "breakpoint": _breakpoint,
    "module": _module,
    "loadedSource": _loaded_source,
    "process": _process,
    "capabilities": _capabilities,
    "exited": lambda body: SessionEnded(EndReason.EXITED, exit_code=_field(body, "exitCode")),
    "terminated": lambda body: SessionEnded(EndReason.TERMINATED),
}


def from_dap(dap_name: str, body: Any) -> Event:
    """Translate one DAP event into pdvp's vocabulary.

    Total: an event we have no type for becomes `UnhandledDapEvent` rather than
    an exception on the reader thread.
    """
    build = _FROM_DAP.get(dap_name)
    if build is None:
        return UnhandledDapEvent(dap_name, body)
    try:
        return build(body)
    except Exception:
        traceback.print_exc()
        return UnhandledDapEvent(dap_name, body)


# ----------------------------------------------------------------- the bus

class SubscriptionClosed(Exception):
    """close() was called on the subscription a caller is blocked in."""


class BusClosed(Exception):
    """subscribe() on a bus that has been shut down."""


_CLOSED = object()      # queued sentinel that wakes every blocked get()


class Subscription:
    """A blocking queue of events, owned and drained by one subscriber.

    Created by `EventBus.subscribe()`. Never created directly, because the bus
    has to know about it before the first event can reach it (P5: arm, then
    trigger).
    """

    def __init__(self, bus: "EventBus", types: tuple[type[Event], ...],
                 match: Callable[[Event], bool] | None, maxsize: int):
        self._bus = bus
        self._queue: queue.Queue = queue.Queue(maxsize)
        self._match = match
        self._closed = False

        #: Effective type filter, always including SessionEnded.
        self.types = types
        #: Events discarded because a bounded queue was full and nobody drained it.
        self.dropped = 0
        #: Times `match` raised. A broken predicate loses events, not the reader.
        self.errors = 0

    # ---- subscriber side

    def get(self, timeout: float | None = None) -> Event:
        """Block until the next matching event.

        May return `SessionEnded` on any subscription, whether or not it was
        asked for -- that is how a wait ends when the thing it waited for can
        no longer happen. Raises `SubscriptionClosed` after close(), and
        `TimeoutError` if `timeout` elapses. `timeout` is the subscriber's
        option; nothing inside pdvp passes one.
        """
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no event within timeout") from None

        if item is _CLOSED:
            # Put it back: close() must wake every getter, not just the first.
            self._offer_raw(_CLOSED)
            raise SubscriptionClosed("subscription closed")
        return item

    def __iter__(self) -> Iterator[Event]:
        """Yield events until the subscription is closed.

        Does not stop at `SessionEnded` -- it yields it. The bus outlives any
        one connection, so a long-lived consumer keeps running into the next
        session.
        """
        while True:
            try:
                yield self.get()
            except SubscriptionClosed:
                return

    def close(self) -> None:
        """Detach from the bus and wake anyone blocked in get(). Idempotent."""
        self._bus._detach(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        names = ", ".join(t.name for t in self.types)
        state = "closed" if self._closed else f"{self._queue.qsize()} queued"
        return f"<Subscription {names} ({state})>"

    # ---- bus side; called with the bus lock held, and must never block

    def _offer(self, event: Event) -> None:
        if self._closed or not isinstance(event, self.types):
            return

        # A predicate can neither reject nor break the one event that ends
        # every wait, or a bad filter becomes a hang.
        if self._match is not None and not isinstance(event, SessionEnded):
            try:
                if not self._match(event):
                    return
            except Exception:
                self.errors += 1
                traceback.print_exc()
                return

        self._offer_raw(event)

    def _offer_raw(self, item: Any) -> None:
        try:
            self._queue.put_nowait(item)
            return
        except queue.Full:
            pass

        # Bounded queue, nobody draining: drop the oldest rather than stall the
        # reader. Dropping the newest would discard the ending itself.
        try:
            self._queue.get_nowait()
            self.dropped += 1
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1


class EventBus:
    """Fan-out from the reducer to every subscriber.

    Program-lifetime: constructed once, alongside Session, and outliving any one
    connection. A subscription handle taken before a `run()` still works after
    the next one, which is what "a user's subscription is long-lived" has to mean.
    """

    def __init__(self):
        # Reentrant because fan-out runs a subscriber's `match` predicate under
        # this lock, and one that touches the bus must fail loudly rather than
        # deadlock. Never held across a wait: every put is non-blocking.
        self._lock = threading.RLock()
        self._subscriptions: list[Subscription] = []
        self._ended: SessionEnded | None = None
        self._closed = False

    def subscribe(self, *types: type[Event] | str,
                  match: Callable[[Event], bool] | None = None,
                  maxsize: int = 0) -> Subscription:
        """Open a subscription. No types means every event.

        `types` are event classes, or their names; a family base such as
        `ThreadEvent` takes everything under it. `SessionEnded` is added
        whether or not it was asked for.

        `match` is applied at fan-out, so a command waiting on one thread's stop
        does not hand-roll a discard loop. It runs on the reader thread: keep it
        cheap and total. It is never applied to `SessionEnded`.

        `maxsize` is a memory policy, not a correctness one. Zero is unbounded,
        so fan-out cannot stall the reader; a positive size drops the oldest
        event for a consumer that knows it will lag.
        """
        subscription = Subscription(self, _resolve(types), match, maxsize)
        with self._lock:
            if self._closed:
                raise BusClosed("event bus is closed")
            self._subscriptions.append(subscription)
        return subscription

    def publish(self, event: Event) -> None:
        """Fan `event` out to every matching subscription. Never blocks.

        Called from the reader thread for anything the core caused, and from a
        command's thread for anything the frontend originates.
        """
        if not isinstance(event, Event):
            raise TypeError(f"not an event: {event!r}")

        with self._lock:
            if self._closed:
                return
            if isinstance(event, SessionEnded):
                # The debuggee exiting, being terminated and the socket dying
                # arrive as up to three signals for one ending. First wins.
                if self._ended is not None:
                    return
                self._ended = event
            elif isinstance(event, SessionStarted):
                self._ended = None

            for subscription in self._subscriptions:
                subscription._offer(event)

    @property
    def ended(self) -> SessionEnded | None:
        """The ending this bus has already published, if the session is over."""
        with self._lock:
            return self._ended

    def close(self) -> None:
        """Shut the bus down and wake every subscriber. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscriptions, self._subscriptions = self._subscriptions, []
            for subscription in subscriptions:
                subscription._closed = True
                subscription._offer_raw(_CLOSED)

    def _detach(self, subscription: Subscription) -> None:
        with self._lock:
            if subscription._closed:
                return
            subscription._closed = True
            try:
                self._subscriptions.remove(subscription)
            except ValueError:
                pass
            subscription._offer_raw(_CLOSED)


def _resolve(types: tuple[type[Event] | str, ...]) -> tuple[type[Event], ...]:
    """Turn subscribe()'s arguments into a filter that always ends waits."""
    resolved: list[type[Event]] = []
    for entry in types:
        if isinstance(entry, str):
            cls = _BY_NAME.get(entry)
            if cls is None:
                raise LookupError(f"no such event: {entry!r}")
            resolved.append(cls)
        elif isinstance(entry, type) and issubclass(entry, Event):
            resolved.append(entry)
        else:
            raise TypeError(f"not an event type: {entry!r}")

    if not resolved:
        return (Event,)
    if not any(issubclass(SessionEnded, cls) for cls in resolved):
        resolved.append(SessionEnded)
    return tuple(resolved)
