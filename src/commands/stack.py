"""Thread and stack-frame navigation: threads, thread, bt, frame, up, down."""
from ..session import SESSION
from ._internal import _ensure_thread_paused

__all__ = ["threads", "thread", "bt", "frame", "up", "down"]


def threads() -> None:
    """List threads. Picks a current thread if none is selected yet."""
    if SESSION.dap is None:
        print("error: not connected")
        return

    thread_list = SESSION.dap.threads()["threads"]
    for t in thread_list:
        marker = "*" if t["id"] == SESSION.current_thread_id else " "
        print(f"{marker} {t['id']}: {t['name']}")

    if SESSION.current_thread_id is None and thread_list:
        SESSION.current_thread_id = thread_list[0]["id"]


def thread(thread_id: int) -> None:
    """Switch the current thread."""
    if SESSION.dap is None:
        print("error: not connected")
        return
    SESSION.current_thread_id = thread_id
    SESSION.current_frame_id = None
    print(f"current thread is now {thread_id}")


def bt(levels: int | None = None) -> None:
    """Print the stack trace for the current thread."""
    if not _ensure_thread_paused():
        return

    trace = SESSION.dap.stack_trace(SESSION.current_thread_id, levels=levels)
    frames = trace["stackFrames"]
    for i, f in enumerate(frames):
        marker = "*" if f["id"] == SESSION.current_frame_id else " "
        path = (f.get("source") or {}).get("path", "?")
        print(f"{marker} #{i} {f['name']} at {path}:{f['line']}")

    if SESSION.current_frame_id is None and frames:
        SESSION.current_frame_id = frames[0]["id"]


def frame(index: int) -> None:
    """Select frame `index` (0 = innermost) from the current thread's stack."""
    if not _ensure_thread_paused():
        return

    frames = SESSION.dap.stack_trace(SESSION.current_thread_id)["stackFrames"]
    if not (0 <= index < len(frames)):
        print(f"error: no frame {index}")
        return

    f = frames[index]
    SESSION.current_frame_id = f["id"]
    path = (f.get("source") or {}).get("path", "?")
    print(f"#{index} {f['name']} at {path}:{f['line']}")


def _move_frame(delta: int) -> None:
    if not _ensure_thread_paused():
        return

    frames = SESSION.dap.stack_trace(SESSION.current_thread_id)["stackFrames"]
    if not frames:
        print("error: no frames")
        return

    if SESSION.current_frame_id is None:
        index = 0
    else:
        index = next((i for i, f in enumerate(frames) if f["id"] == SESSION.current_frame_id), 0)

    new_index = index + delta
    if new_index < 0:
        print("*** Oldest frame")
        new_index = 0
    elif new_index >= len(frames):
        print("*** Newest frame")
        new_index = len(frames) - 1

    frame(new_index)


def up(n: int = 1) -> None:
    """Move `n` frames toward the caller (older frames)."""
    _move_frame(n)


def down(n: int = 1) -> None:
    """Move `n` frames toward the callee (newer frames)."""
    _move_frame(-n)
