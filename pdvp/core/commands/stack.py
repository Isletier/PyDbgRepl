"""Thread and stack-frame navigation: threads, thread, cursors, bt, frame, up, down.

Every command that means "a thread" takes one, defaulting to the caller's
cursor through `SESSION.resolve_thread()`. That default is the only thing tying
this module to there being an ambient cursor at all -- `bt(thread=5)` is a
complete question on its own, which is what a script or a different frontend
needs.

`frame`, `up` and `down` are the exception, and deliberately: they *are* cursor
navigation, so there is nothing for them to take.
"""
from pdvp.core import cursor as _cursor
from pdvp.core.session import SESSION
from pdvp.core.model import CursorList, Error, ErrorKind, FrameRef, FrameList, Status, ThreadList

__all__ = ["threads", "thread", "cursors", "bt", "frame", "up", "down"]


def threads() -> ThreadList | Error:
    """List threads. Picks a current thread if none is selected yet."""
    err = SESSION.require_connected()
    if err is not None:
        return err

    thread_list = SESSION.client.threads().body.threads
    # The reducer may not issue requests, so the round trip that could refresh
    # the thread table has to feed the answer back from here.
    SESSION.adopt_threads(thread_list)

    if SESSION.current_thread_id is None and thread_list:
        SESSION.current_thread_id = thread_list[0]["id"]

    return ThreadList(thread_list, current_id=SESSION.current_thread_id)


def thread(thread_id: int) -> Status | Error:
    """Switch this caller's current thread. Clears the frame cursor -- a frame
    from the old thread means nothing against the new one."""
    err = SESSION.require_connected()
    if err is not None:
        return err
    SESSION.current_thread_id = thread_id
    return Status(f"current thread is now {thread_id}")


def cursors() -> CursorList:
    """Who has selected what.

    The cursor is per caller and otherwise invisible, which is the one real cost
    of not passing a thread id to every command. This is where you look: one row
    per caller that has chosen, plus what everyone who has not chosen reads.
    """
    mine = _cursor.owner()
    generation = SESSION.generation

    rows = []
    for key, selection in SESSION.cursors.all():
        rows.append({
            "owner": getattr(key, "name", None) or repr(key),
            "thread": selection.thread_id,
            "frame": selection.frame.frame_id if selection.frame is not None else None,
            "current": key is mine,
            # A selection from a previous connection: ignored on read, shown
            # here so it is not a mystery why it is being ignored.
            "stale": selection.generation != generation,
        })

    rows.sort(key=lambda row: (not row["current"], row["owner"]))
    return CursorList(rows, default_thread=SESSION._last_stopped)


def bt(levels: int | None = None, *, thread: int | None = None) -> FrameList | Error:
    """The stack trace for `thread`, defaulting to the caller's current one."""
    thread_id = SESSION.resolve_thread(thread)
    err = SESSION.require_stopped(thread_id)
    if err is not None:
        return err

    trace = SESSION.client.stack_trace(thread_id, levels=levels)
    frames = trace.body.stackFrames

    # Only when this is the caller's own thread: asking about somebody else's
    # stack is a read, not a move.
    if thread is None and SESSION.current_frame_id is None and frames:
        SESSION.current_frame_id = frames[0]["id"]

    return FrameList(frames, current_id=SESSION.current_frame_id)


def frame(index: int) -> FrameRef | Error:
    """Select frame `index` (0 = innermost) from the current thread's stack."""
    thread_id = SESSION.current_thread_id
    err = SESSION.require_stopped(thread_id)
    if err is not None:
        return err

    frames = SESSION.client.stack_trace(thread_id).body.stackFrames
    if not (0 <= index < len(frames)):
        return Error(f"no frame {index}", kind=ErrorKind.NO_SUCH_FRAME)

    f = frames[index]
    SESSION.current_frame_id = f["id"]
    return FrameRef(f, index)


def _move_frame(delta: int) -> FrameRef | Error:
    thread_id = SESSION.current_thread_id
    err = SESSION.require_stopped(thread_id)
    if err is not None:
        return err

    frames = SESSION.client.stack_trace(thread_id).body.stackFrames
    if not frames:
        return Error("no frames", kind=ErrorKind.NO_FRAMES)

    if SESSION.current_frame_id is None:
        index = 0
    else:
        index = next((i for i, f in enumerate(frames) if f["id"] == SESSION.current_frame_id), 0)

    new_index = index + delta
    prefix = ""
    if new_index < 0:
        prefix = "*** Oldest frame"
        new_index = 0
    elif new_index >= len(frames):
        prefix = "*** Newest frame"
        new_index = len(frames) - 1

    result = frame(new_index)
    if isinstance(result, FrameRef):
        result.prefix = prefix
    return result


def up(n: int = 1) -> FrameRef | Error:
    """Move `n` frames toward the caller (older frames)."""
    return _move_frame(n)


def down(n: int = 1) -> FrameRef | Error:
    """Move `n` frames toward the callee (newer frames)."""
    return _move_frame(-n)
