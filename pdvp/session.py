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
import os
import threading

from . import cursor
from . import dap
from . import events
from . import launch
from . import model
from . import source
# Cursor state lives with the cursor; re-exported because a FrameHandle is what
# Session's read guards hand back.
from .cursor import FrameHandle


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


@dataclasses.dataclass
class ResumeWait:
    """One resume we issued and have not yet seen the end of.

    `target` is the thread set the resume claimed -- a thread id in non-stop,
    `None` (every thread) in all-stop, which is the same key the control right
    uses. `blocking` says whether a caller is actually parked on the outcome,
    as opposed to holding a `Resumption` they have not waited on yet. The two
    answer different questions and neither implies the other: a stop is
    announced on the console unless a *blocking* wait will report it, while
    the mode switch is refused if *any* resume is still in flight.
    """

    target: int | None
    blocking: bool

    def matches(self, event) -> bool:
        """Whether `event` is the stop this resume was waiting for."""
        return self.target is None or event.all_threads or event.thread_id == self.target


# Which caller is asking. Both the cursor table and the control right key on it,
# so a sequence and the cursor it moves belong to the same caller by
# construction. Replaceable in one place -- see pdvp/cursor.py.
_owner = cursor.owner


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
        # Program lifetime because a hold is ended by its holder, never by a
        # lifetime reset: the caller blocked in a resume is woken by the
        # session ending and releases on its way out.
        self.control = ControlRights()
        # The table is program lifetime; the selections in it are connection
        # lifetime, and end_connection() empties it.
        self.cursors = cursor.Cursors()

        # ---- connection lifetime: reset by end_connection() ----
        self.client: dap.Client | None = None
        # sourceReferences are only valid for one DAP session (per the spec),
        # so this map cannot outlive the connection.
        self.sourceMap = source.SourceMap()
        # The live execution mode, negotiated at attach from config.non_stop.
        # Connection lifetime because that is when it is agreed; the config
        # field is what the next attach will ask for.
        self.non_stop = False
        # The session-wide cursor default: what a context that never selected a
        # thread reads through to. Connection lifetime -- a thread id from a
        # dead session names nothing.
        self._last_stopped: int | None = None

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

        # Resumes we issued that have not resolved. Not run state -- these
        # answer "will somebody report this outcome", which is what decides
        # whether the console has to announce a stop or a death itself.
        self._resumes: list[ResumeWait] = []

    # ---------------------------------------------------------- the reducer

    def reduce(self, message) -> events.Event:
        """`Client.on_event`. Runs on the reader thread. Issues no requests.

        Everything the core says reaches the bus through here, including its
        death: a `ConnectionClosed` record is not a DAP event, so it is
        translated rather than published as one.

        Returns the event it published, so the caller on the reader thread can
        decide what to do about one nobody is waiting for without translating
        the message a second time.
        """
        if isinstance(message, dap.ConnectionClosed):
            event = events.SessionEnded(
                events.EndReason.CLOSED if message.deliberate else events.EndReason.DISCONNECTED,
                detail=message.detail)
        else:
            event = events.from_dap(message.event, message.body)

        event = self._apply(event)
        self.bus.publish(event)
        return event

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
        if event.all_threads and self.non_stop:
            # pydevd leaks an all-stop notification into non-stop: every `pause`
            # request arms a 0.5s timer, and the timer reports whichever thread
            # happens to be suspended as a single notification without checking
            # the mode (AbstractSingleNotificationBehavior._notify_after_timeout,
            # which notify_thread_suspended does check). Taking it at face value
            # marks every thread stopped, and -- worse -- satisfies a wait on a
            # thread that never stopped, since a wait matches on all_threads.
            event = dataclasses.replace(event, all_threads=False)

        reporter = self._track(event.thread_id) if event.thread_id is not None else None
        if event.thread_id is not None:
            self._last_stopped = event.thread_id
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

    @property
    def running_threads(self) -> list[int]:
        """The threads we believe are not suspended -- what interrupt() pauses."""
        with self._lock:
            return [state.id for state in self._threads.values() if not state.stopped]

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

    def arm_resume(self, target: int | None, blocking: bool) -> ResumeWait:
        """Register a resume as in flight. Armed before the request goes out."""
        record = ResumeWait(target, blocking)
        with self._lock:
            self._resumes.append(record)
        return record

    def begin_blocking(self, record: ResumeWait) -> None:
        """A caller has parked on a resume that was issued in the background."""
        with self._lock:
            record.blocking = True

    def disarm_resume(self, record: ResumeWait) -> None:
        """The resume has resolved (or was never sent). Idempotent."""
        with self._lock:
            if record in self._resumes:
                self._resumes.remove(record)

    @contextlib.contextmanager
    def resume_wait(self, target: int | None):
        """Arm a blocking resume wait for the length of the block."""
        record = self.arm_resume(target, blocking=True)
        try:
            yield record
        finally:
            self.disarm_resume(record)

    @property
    def awaiting_resume(self) -> bool:
        """Whether any caller is blocked on a resume outcome.

        What decides whether a connection death is announced by whoever is
        waiting on it, or has to be announced by the console.
        """
        with self._lock:
            return any(record.blocking for record in self._resumes)

    @property
    def resume_in_flight(self) -> bool:
        """Whether any resume we issued is still unresolved, waited on or not.

        The precondition for switching execution modes.
        """
        with self._lock:
            return bool(self._resumes)

    def stop_is_awaited(self, event: events.Stopped) -> bool:
        """Whether a blocked caller will report `event` as its own outcome."""
        with self._lock:
            return any(record.blocking and record.matches(event) for record in self._resumes)

    def stop_was_news(self, event: events.Stopped) -> bool:
        """Whether `event` told us something the one before it had not.

        pydevd re-reports suspensions it has already reported: each `pause`
        request arms its own notification timer, so one interrupt() across N
        threads produces N copies of the same fact, all naming whichever thread
        the timer happened to find suspended. Epochs move only on resume, so
        "same thread, same epoch" is exactly "we already knew this".

        Collapses consecutive repeats, which is the shape they arrive in. Read
        by the console to decide whether a stop is worth announcing; nothing
        about the state depends on it.
        """
        key = (event.thread_id, event.epoch)
        with self._lock:
            if key == self._last_stop_seen:
                return False
            self._last_stop_seen = key
            return True

    # ---------------------------------------------------------- the cursor

    @property
    def generation(self) -> int:
        """Which connection we are on. Bumped by begin().

        The same counter as the epoch seed, because both answer one question:
        was this handle minted against the session we are in now.
        """
        return self._epoch_seed

    def _selection(self) -> cursor.Cursor | None:
        """This caller's selection, if it made one *in this session*.

        A selection from a previous connection is not merely stale, it is
        actively misleading: pydevd reuses small thread ids, so the id would
        very likely resolve against a completely different thread. Treated as
        never having chosen, so it reads the default instead.
        """
        chosen = self.cursors.get()
        if chosen is None or chosen.generation != self.generation:
            return None
        return chosen

    @property
    def current_thread_id(self) -> int | None:
        """The calling caller's thread, or the session-wide default.

        A caller that never selected one -- a worker thread, a subscription
        drainer, and the REPL itself before its first `thread()` -- reads
        through to the last thread to stop. Selecting one detaches that caller
        from the default. Nobody is privileged: everyone who has not chosen sees
        the same thread.
        """
        chosen = self._selection()
        if chosen is not None and chosen.thread_id is not None:
            return chosen.thread_id
        return self._last_stopped

    @current_thread_id.setter
    def current_thread_id(self, thread_id: int | None) -> None:
        selection = self.cursors.own()
        # Unconditionally: a frame handle from thread A is meaningless once B
        # is selected, whether or not either is running.
        selection.frame = None
        selection.thread_id = thread_id
        selection.generation = self.generation

    def resolve_thread(self, thread_id: int | None = None) -> int | None:
        """The thread a command means: the one it was given, or the cursor.

        The single place an implicit cursor becomes an explicit argument. Every
        command below takes its thread as a parameter and calls this to fill it
        in, so nothing in the command layer is bound to there being an ambient
        cursor at all -- which is what makes `cursor.scope()` swappable in one
        place rather than auditable in five.
        """
        return thread_id if thread_id is not None else self.current_thread_id

    @property
    def current_frame_id(self) -> int | None:
        handle = self.frame_handle
        return handle.frame_id if handle is not None else None

    @current_frame_id.setter
    def current_frame_id(self, frame_id: int | None) -> None:
        selection = self.cursors.own()
        selection.generation = self.generation
        if frame_id is None:
            selection.frame = None
            return
        # Through the property, so a caller reading the session-wide default
        # still stamps a real thread onto the handle -- otherwise every later
        # guard on this frame asks about None.
        thread_id = self.current_thread_id
        selection.frame = FrameHandle(thread_id, self.epoch_of(thread_id), frame_id)

    @property
    def frame_handle(self) -> FrameHandle | None:
        """This caller's frame selection, unvalidated. See require_frame()."""
        chosen = self._selection()
        return chosen.frame if chosen is not None else None

    # ---------------------------------------------------------- read guards

    def require_connected(self) -> model.Error | None:
        if self.client is None:
            return model.Error("not connected (use connect())", kind=model.ErrorKind.NOT_CONNECTED)
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
            return model.Error("no current thread (use threads())", kind=model.ErrorKind.NO_CURRENT_THREAD)
        state = self.thread_state(thread_id)
        if state is None:
            return model.Error(f"no thread {thread_id}", kind=model.ErrorKind.NO_SUCH_THREAD)
        if not state.stopped:
            return model.Error(f"thread {thread_id} is running", kind=model.ErrorKind.THREAD_RUNNING)
        return None

    def require_frame(self) -> FrameHandle | model.Error:
        """The current frame, if it is still readable.

        Two independent guards, and neither implies the other: the thread must
        be stopped, *and* the handle's epoch must still be current. Epochs only
        move on resume, so a running thread's stack churns at a constant epoch.
        """
        handle = self.frame_handle
        if handle is None:
            # Report the more fundamental problem first: "use bt()" is useless
            # advice when bt() would fail for the same reason.
            error = self.require_stopped(self.current_thread_id)
            if error is not None:
                return error
            return model.Error("no current frame (use bt())", kind=model.ErrorKind.NO_CURRENT_FRAME)
        error = self.require_stopped(handle.thread_id)
        if error is not None:
            return error
        current_epoch = self.epoch_of(handle.thread_id)
        if handle.epoch != current_epoch:
            return model.StaleFrameError(handle.thread_id, handle.epoch, current_epoch)
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

        Breakpoints themselves survive, gdb-style, but `verified` is a fact
        about one debuggee process rather than about the breakpoint, so it goes
        back to False. A `disconnect()` that leaves the debuggee running still
        ends our DAP session, so its sourceReferences and verifications are
        just as dead either way.

        Every caller's cursor goes too. That is possible only because the table
        is ours: a ContextVar or a threading.local is writable solely from the
        caller that owns it, so this reset could never have reached them, and
        "a leftover selection fails its guard" would have had to be a hope about
        the emptied thread table rather than something we do.
        """
        with self._lock:
            self.client = None
            self._threads.clear()
            self.sourceMap.clear()
            self.cursors.clear()
            self._last_stopped = None
            for breakp in self.Breakpoints.values():
                breakp.verified = False

    def end_process(self) -> None:
        """Process lifetime: kill the debuggee we spawned and reap it.

        A no-op for a session we only connected to -- there is no child of ours
        to kill, and terminating somebody else's debuggee is a DAP request
        rather than a signal.
        """
        if self.process is None:
            return
        if self.process.child.poll() is None:
            self.process.child.kill()
        self.process.child.wait()
        if self.process.master_fd is not None:
            os.close(self.process.master_fd)
        self.process = None
        self.reader_thread = None

    def end(self) -> None:
        """Tear the connection and any process we spawned down as one unit.

        They share one lifetime by design, so there is no ordering to get right
        at the call sites: whoever ends either ends both.
        """
        if self.client is not None:
            self.client.close()
        self.end_process()
        self.end_connection()


SESSION = Session()
