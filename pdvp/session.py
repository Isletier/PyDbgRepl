"""Layer 2 -- Session: the reducer, and the state it reduces into.

Consumes `Client.on_event` on the reader thread and turns it into state, then
publishes the pdvp event onto the bus. Running there is deliberate: internal
liveness must never depend on user code, which only ever sees events through a
subscription queue it drains on its own thread.

Two rules constrain everything here:

  * **The reducer issues no requests.** It runs on the reader thread, so waiting
    for a response would be the reader waiting on itself. Anything needing a
    round trip is deferred to a caller's thread.
  * **The state lock is never held across a wait** (P3). Every critical section
    below is a handful of field reads or writes.

The other shared thing that lives here is the **thread control right**: run
state is the one resource concurrent callers genuinely compete for, so the
operations that change it are serialized per thread (`ControlRights`).

State is grouped by what kills it, and each group has exactly one reset.
Program-lifetime state survives run()/stop() cycles; connection-lifetime state
dies with the socket for any reason, including a `disconnect()` that leaves the
debuggee alive; process-lifetime state dies with the debuggee; stop-epoch state
is not held at all, it is the epoch counter that makes stale handles detectable.
"""
import contextlib
import dataclasses
import threading
from contextvars import ContextVar

from . import dap
from . import events
from . import launch
from . import model
from . import source


@dataclasses.dataclass
class ThreadState:
    """One inferior thread, as far as we can tell from the wire.

    A single `running` flag cannot describe non-stop mode, where some threads
    run while others stay parked, so the table is required rather than tidy. In
    all-stop it is trivially uniform and the same structure serves both.
    """

    id: int
    name: str | None = None
    stopped: bool = False
    reason: str | None = None
    # Incremented every time this thread resumes. Handles minted at one epoch
    # are refused at any other.
    epoch: int = 0
    # A resume we issued has been sent but not yet confirmed by a `continued`
    # event, so its epoch bump is already accounted for.
    pending_resume: bool = False


@dataclasses.dataclass(frozen=True)
class FrameHandle:
    """A frame, plus the two things that decide whether it may still be read."""

    thread_id: int
    epoch: int
    frame_id: int


# The cursor is context-local, not global: `frame()`, `up()`, `down()` and
# thread selection change an implicit argument to the *caller's* next command.
# A global cursor would let one consumer silently change what another's next
# locals() means, and two user threads calling frame() would simply race.
# ContextVars give each thread its own by construction.
_cursor_thread: ContextVar[int | None] = ContextVar("pdvp_cursor_thread", default=None)
_cursor_frame: ContextVar[FrameHandle | None] = ContextVar("pdvp_cursor_frame", default=None)

# Identifies the holder of a control right. Context-local for the same reason
# the cursor is: the holder of a sequence is whoever is running it, and each
# thread gets its own token without anyone passing one around.
_control_owner: ContextVar[object | None] = ContextVar("pdvp_control_owner", default=None)


def _owner() -> object:
    token = _control_owner.get()
    if token is None:
        token = object()
        _control_owner.set(token)
    return token


class ControlRights:
    """Serializes the operations that change an inferior thread's run state.

    Without it, two callers resuming the same thread produce one resumption:
    pydevd resumes on the first request, the second is a no-op, and both waits
    are satisfied by the same `stopped`. Two commands, one run, success
    reported to both. The other races here degrade into visible errors; that
    one is silent, which is the whole reason a right exists.

    A right is keyed by thread id, or None meaning every thread -- a resume in
    all-stop moves the entire program, so it conflicts with every per-thread
    holder and they with it. Nothing chooses between the two keys: `None` is
    what a resume that names no thread already passes.

    `pause` is deliberately not a client of this: measured, it is idempotent,
    so it cannot corrupt another caller's armed wait -- and it is the escape
    hatch when one caller's run is making another wait.

    Acquisition is reentrant per context, which is what lets `control()` extend
    one hold across a sequence whose commands each acquire it again. Held from
    the send until the stop, with no timeout (P4): the holder is woken by the
    reader thread, so nothing it waits for is behind the right.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        # key -> [owner, depth]
        self._held: dict[int | None, list] = {}

    @contextlib.contextmanager
    def hold(self, thread_id: int | None, announce=None):
        """Hold the right for `thread_id` (None = every thread) for the block.

        `announce()` is called once if the acquisition has to block, so a
        waiting caller says so rather than hanging mutely.
        """
        owner = _owner()
        self._acquire(thread_id, owner, announce)
        try:
            yield
        finally:
            self._release(thread_id, owner)

    def holder_of(self, thread_id: int | None) -> object | None:
        """The owner token holding `thread_id`'s right, if anyone does."""
        with self._cv:
            entry = self._held.get(thread_id)
            return entry[0] if entry is not None else None

    def _acquire(self, key: int | None, owner: object, announce) -> None:
        with self._cv:
            if not self._blocked(key, owner):
                self._take(key, owner)
                return

        # Announced outside the lock: it is I/O, and the release that would
        # unblock us needs the same lock.
        if announce is not None:
            announce()

        with self._cv:
            while self._blocked(key, owner):
                self._cv.wait()
            self._take(key, owner)

    def _blocked(self, key: int | None, owner: object) -> bool:
        """Whether someone else holds a right that overlaps `key`."""
        for held_key, (held_owner, _) in self._held.items():
            if held_owner is owner:
                continue
            if key is None or held_key is None or held_key == key:
                return True
        return False

    def _take(self, key: int | None, owner: object) -> None:
        entry = self._held.get(key)
        if entry is None:
            self._held[key] = [owner, 1]
        else:
            entry[1] += 1

    def _release(self, key: int | None, owner: object) -> None:
        with self._cv:
            entry = self._held.get(key)
            if entry is None or entry[0] is not owner:
                return
            entry[1] -= 1
            if entry[1] == 0:
                del self._held[key]
                self._cv.notify_all()


class Session:
    """The debugging session: one per program run, outliving every connection."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

        # ---- program lifetime: reset by nothing ----
        self.bus = events.EventBus()
        self.Breakpoints: dict[int, model.Breakpoint] = {}
        self.displays: list[dict] = []
        # Program lifetime because a hold is ended by its holder, never by a
        # lifetime reset: the caller blocked in a resume is woken by the
        # session ending and releases on its way out.
        self.control = ControlRights()

        # ---- connection lifetime: reset by end_connection() ----
        self.client: dap.Client | None = None
        # sourceReferences are only valid for one DAP session (per the spec),
        # so this map cannot outlive the connection.
        self.sourceMap = source.SourceMap()

        # ---- process lifetime: reset by end_process() ----
        self.process: launch.LaunchedProcess | None = None
        self.reader_thread: threading.Thread | None = None
        self._threads: dict[int, ThreadState] = {}
        # Epochs never restart at zero. pydevd hands out small thread ids and
        # reuses them across runs, so a per-thread counter starting from 0 would
        # let a frame handle from the last session validate against a brand new
        # thread that happens to share its id.
        self._epoch_seed = 0

        # ---- frontend lifetime ----
        self.ptpython_active = False

        # How many callers are blocked waiting for a resume to resolve. Not run
        # state -- it answers "will somebody notice this ending", which decides
        # whether the reducer has to announce a death itself.
        self._resume_waiters = 0

    # ---------------------------------------------------------- the reducer

    def reduce(self, message) -> None:
        """`Client.on_event`. Runs on the reader thread. Issues no requests.

        Everything the core says reaches the bus through here, including its
        death: a `ConnectionClosed` record is not a DAP event, so it is
        translated rather than published as one.
        """
        if isinstance(message, dap.ConnectionClosed):
            event = events.SessionEnded(
                events.EndReason.CLOSED if message.deliberate else events.EndReason.DISCONNECTED,
                detail=message.detail)
        else:
            event = events.from_dap(message.event, message.body)

        self.bus.publish(self._apply(event))

    def _apply(self, event: events.Event) -> events.Event:
        """Fold `event` into the state and stamp it with the epoch it belongs to."""
        with self._lock:
            if isinstance(event, events.Stopped):
                return self._on_stopped(event)
            if isinstance(event, events.Continued):
                return self._on_continued(event)
            if isinstance(event, events.ThreadStarted):
                self._track(event.thread_id)
            elif isinstance(event, events.ThreadExited):
                self._threads.pop(event.thread_id, None)
            elif isinstance(event, events.SessionEnded):
                self._threads.clear()
            return event

    def _on_stopped(self, event: events.Stopped) -> events.Stopped:
        reporter = self._track(event.thread_id) if event.thread_id is not None else None
        targets = list(self._threads.values()) if event.all_threads else [reporter]
        for state in targets:
            if state is None:
                continue
            state.stopped = True
            state.reason = event.reason
            state.pending_resume = False
        return dataclasses.replace(event, epoch=reporter.epoch if reporter else None)

    def _on_continued(self, event: events.Continued) -> events.Continued:
        reporter = self._track(event.thread_id) if event.thread_id is not None else None
        targets = list(self._threads.values()) if event.all_threads else [reporter]
        for state in targets:
            if state is None:
                continue
            state.stopped = False
            state.reason = None
            if not state.pending_resume:
                # A resume we did not issue, or one that widened past the
                # thread we named. note_resume() never bumped it, so do it now.
                state.epoch += 1
            state.pending_resume = False
        return dataclasses.replace(event, epoch=reporter.epoch if reporter else None)

    # ---------------------------------------------------------- thread table

    def _track(self, thread_id: int) -> ThreadState:
        """The record for `thread_id`, created if pydevd never announced it.

        Caller holds the lock. A `connect()` to a pydevd that started without
        us gets no `thread` events for threads that already exist, so the first
        mention of one is where it joins the table.
        """
        state = self._threads.get(thread_id)
        if state is None:
            state = self._threads[thread_id] = ThreadState(thread_id, epoch=self._epoch_seed)
        return state

    def adopt_threads(self, listing) -> None:
        """Reconcile the table against a `threads` response.

        The reducer cannot ask for this -- it may not issue requests -- so the
        caller that did the round trip feeds the answer back. Existing records
        keep their run state; only names and membership are refreshed.
        """
        with self._lock:
            seen = set()
            for entry in listing:
                thread_id = entry["id"] if isinstance(entry, dict) else entry.id
                name = entry.get("name") if isinstance(entry, dict) else getattr(entry, "name", None)
                self._track(thread_id).name = name
                seen.add(thread_id)
            for thread_id in list(self._threads):
                if thread_id not in seen:
                    del self._threads[thread_id]

    @property
    def threads(self) -> list[ThreadState]:
        with self._lock:
            return list(self._threads.values())

    def thread_state(self, thread_id: int | None) -> ThreadState | None:
        if thread_id is None:
            return None
        with self._lock:
            return self._threads.get(thread_id)

    def epoch_of(self, thread_id: int | None) -> int:
        state = self.thread_state(thread_id)
        return state.epoch if state is not None else 0

    def is_stopped(self, thread_id: int | None) -> bool:
        state = self.thread_state(thread_id)
        return state is not None and state.stopped

    @property
    def any_running(self) -> bool:
        with self._lock:
            return any(not state.stopped for state in self._threads.values())

    # ---------------------------------------------------------- resume bookkeeping

    def note_resume(self, thread_id: int | None) -> None:
        """Bump the epoch for the threads we expect to resume, at send time.

        Bumping at send rather than on the `continued` event is deliberate: an
        early bump costs a spurious "frame is stale" on a race, a late one hands
        back wrong data silently. Fail in the direction that is visible.

        `thread_id` None means every thread, which is what a resume looks like
        in all-stop -- pydevd discards the threadId there and resumes all of
        them. The `continued` event reconciles whatever this got wrong.
        """
        with self._lock:
            targets = ([self._track(thread_id)] if thread_id is not None
                       else list(self._threads.values()))
            for state in targets:
                state.epoch += 1
                state.stopped = False
                state.reason = None
                state.pending_resume = True

    def undo_resume(self, thread_id: int | None) -> None:
        """Take back the run state note_resume() set, after the request failed.

        The epoch bump is *not* taken back. A bump only ever costs a stale-handle
        error, and rolling it back could revalidate a handle the failed request
        already raced past.
        """
        with self._lock:
            targets = ([self._track(thread_id)] if thread_id is not None
                       else list(self._threads.values()))
            for state in targets:
                state.stopped = True
                state.pending_resume = False

    @contextlib.contextmanager
    def resume_wait(self):
        """Mark a caller as blocked on a resume outcome, for the reducer's benefit."""
        with self._lock:
            self._resume_waiters += 1
        try:
            yield
        finally:
            with self._lock:
                self._resume_waiters -= 1

    @property
    def awaiting_resume(self) -> bool:
        with self._lock:
            return self._resume_waiters > 0

    # ---------------------------------------------------------- the cursor

    @property
    def current_thread_id(self) -> int | None:
        return _cursor_thread.get()

    @current_thread_id.setter
    def current_thread_id(self, thread_id: int | None) -> None:
        # Unconditionally: a frame handle from thread A is meaningless once B
        # is selected, whether or not either is running.
        _cursor_frame.set(None)
        _cursor_thread.set(thread_id)

    @property
    def current_frame_id(self) -> int | None:
        handle = _cursor_frame.get()
        return handle.frame_id if handle is not None else None

    @current_frame_id.setter
    def current_frame_id(self, frame_id: int | None) -> None:
        if frame_id is None:
            _cursor_frame.set(None)
            return
        thread_id = _cursor_thread.get()
        _cursor_frame.set(FrameHandle(thread_id, self.epoch_of(thread_id), frame_id))

    # ---------------------------------------------------------- read guards

    def require_connected(self) -> model.Error | None:
        if self.client is None:
            return model.Error("not connected (use connect())")
        return None

    def require_stopped(self, thread_id: int | None) -> model.Error | None:
        """Gate a frame-scoped read on the thread actually being stopped.

        pydevd enforces nothing here: measured, it answers `stackTrace` on a
        running thread with a torn snapshot and `variables` with an empty list.
        Success-shaped garbage renders as "this frame has no locals" rather than
        "that question was meaningless", so the check lives on our side.
        """
        error = self.require_connected()
        if error is not None:
            return error
        if thread_id is None:
            return model.Error("no current thread (use threads())")
        state = self.thread_state(thread_id)
        if state is None:
            return model.Error(f"no thread {thread_id}")
        if not state.stopped:
            return model.Error(f"thread {thread_id} is running")
        return None

    def require_frame(self) -> FrameHandle | model.Error:
        """The current frame, if it is still readable.

        Two independent guards, and neither implies the other: the thread must
        be stopped, *and* the handle's epoch must still be current. Epochs only
        move on resume, so a running thread's stack churns at a constant epoch.
        """
        handle = _cursor_frame.get()
        if handle is None:
            # Report the more fundamental problem first: "use bt()" is useless
            # advice when bt() would fail for the same reason.
            error = self.require_stopped(self.current_thread_id)
            return error if error is not None else model.Error("no current frame (use bt())")
        error = self.require_stopped(handle.thread_id)
        if error is not None:
            return error
        if handle.epoch != self.epoch_of(handle.thread_id):
            return model.Error("frame is stale, the program has resumed since (use bt())")
        return handle

    # ---------------------------------------------------------- lifetimes

    def begin(self, client: dap.Client, pid: int | None = None) -> None:
        """Adopt a fresh connection and announce it.

        `SessionStarted` re-arms the bus's ending latch. Without it the previous
        session's `SessionEnded` would swallow this one's and every wait would
        go unbounded again.
        """
        with self._lock:
            self.client = client
            self._threads.clear()
            self._epoch_seed += 1
        self.bus.publish(events.SessionStarted(pid=pid))

    def end_connection(self) -> None:
        """Connection lifetime: everything a disconnect invalidates, for any reason.

        Process-lifetime state goes with it by implication -- we cannot track a
        debuggee we are no longer talking to.

        Cursors are not cleared here and cannot be: they are context-local, and
        this may run on any thread. They do not need to be -- with the table
        emptied, a cursor left over from the dead session fails its guard with a
        clean error rather than reading anything.
        """
        with self._lock:
            self.client = None
            self._threads.clear()
            self.sourceMap.clear()


SESSION = Session()
