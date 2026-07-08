"""Thread and stack-frame navigation: threads, thread, bt, frame, up, down."""
from ..session import SESSION
from ._display import Error, FrameRef, FrameList, Status, ThreadList
from ._internal import _ensure_thread_paused

__all__ = ["threads", "thread", "bt", "frame", "up", "down"]


def threads() -> ThreadList | Error:
    """List threads. Picks a current thread if none is selected yet."""
    if SESSION.client is None:
        return Error("not connected")

    thread_list = SESSION.client.threads()["threads"]

    if SESSION.current_thread_id is None and thread_list:
        SESSION.current_thread_id = thread_list[0]["id"]

    return ThreadList(thread_list, current_id=SESSION.current_thread_id)


def thread(thread_id: int) -> Status | Error:
    """Switch the current thread."""
    if SESSION.client is None:
        return Error("not connected")
    SESSION.current_thread_id = thread_id
    SESSION.current_frame_id = None
    return Status(f"current thread is now {thread_id}")


def bt(levels: int | None = None) -> FrameList | Error:
    """The stack trace for the current thread."""
    err = _ensure_thread_paused()
    if err is not None:
        return err

    trace = SESSION.client.stack_trace(SESSION.current_thread_id, levels=levels)
    frames = trace["stackFrames"]

    if SESSION.current_frame_id is None and frames:
        SESSION.current_frame_id = frames[0]["id"]

    return FrameList(frames, current_id=SESSION.current_frame_id)


def frame(index: int) -> FrameRef | Error:
    """Select frame `index` (0 = innermost) from the current thread's stack."""
    err = _ensure_thread_paused()
    if err is not None:
        return err

    frames = SESSION.client.stack_trace(SESSION.current_thread_id)["stackFrames"]
    if not (0 <= index < len(frames)):
        return Error(f"no frame {index}")

    f = frames[index]
    SESSION.current_frame_id = f["id"]
    return FrameRef(f, index)


def _move_frame(delta: int) -> FrameRef | Error:
    err = _ensure_thread_paused()
    if err is not None:
        return err

    frames = SESSION.client.stack_trace(SESSION.current_thread_id)["stackFrames"]
    if not frames:
        return Error("no frames")

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
