"""Source listing: list()/l()."""
from ._display import Error, SourceLines
from ._internal import _current_location

__all__ = ["ls", "l"]


def ls(first: int | None = None, last: int | None = None) -> SourceLines | Error:
    """Lines from the current file.

    No args: ~10 lines centered on the current line. `first` only: a window
    centered on that line (like pdb's `list 20`). Both: that range,
    inclusive.
    """
    path, current_line = _current_location()
    if path is None:
        return Error("no current file")

    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError as e:
        return Error(str(e))
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

    return SourceLines(
        ((i, lines[i - 1].rstrip()) for i in range(start, end + 1)),
        current_line=current_line,
    )


l = ls
