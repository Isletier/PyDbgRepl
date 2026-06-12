"""Source listing: list()/l()."""
from ._internal import _current_location

__all__ = ["list", "l"]


def list(first: int | None = None, last: int | None = None) -> None:
    """Print lines from the current file.

    No args: ~10 lines centered on the current line. `first` only: a window
    centered on that line (like pdb's `list 20`). Both: that range,
    inclusive.
    """
    path, current_line = _current_location()
    if path is None:
        print("error: no current file")
        return

    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError as e:
        print(f"error: {e}")
        return
    total = len(lines)

    if first is None and last is None:
        center = current_line or 1
        start = max(1, center - 5)
        end = min(total, start + 9)
    elif last is None:
        start = max(1, first - 5)
        end = min(total, start + 9)
    else:
        start = max(1, first)
        end = min(total, last)

    for i in range(start, end + 1):
        marker = "->" if i == current_line else "  "
        print(f"{marker}{i:5d}\t{lines[i - 1].rstrip()}")


l = list
