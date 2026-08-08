"""ptpython completer for pydev-repl: debugger-focused completions by default.

See doc/completion_design.md for the design this implements.
"""
from __future__ import annotations

import os
import subprocess

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from . import commands as _commands
from . import dap as _dap
from .session import SESSION

# Per-command argument completion kinds, by positional argument index.
# A bare string (rather than a list) applies to every argument position --
# for *args-style commands like catch(*filters).
_ARG_TABLE: dict[str, list[str | None] | str] = {
    "run": "run_arg",
    "breakpoint": ["file", None, None, None],
    "clear": ["file", None],
    "tbreak": ["file", None, None],
    "enable": ["file", None],
    "disable": ["file", None],
    "ignore": ["file", None, None],
    "thread": ["thread_id"],
    "frame": ["frame_index"],
    "catch": "exception_filter",
}

# run()'s keyword-only stdio-redirection arguments -- discoverable from any
# argument position, since they can follow any number of *args. See
# doc/io_model.md.
_RUN_KWARGS = ["stdin", "stdout", "stderr"]

# Directories os.walk-based project indexing never descends into, on top of
# dot-directories (matches what `git ls-files` would exclude via .gitignore
# for projects without git).
_WALK_DENYLIST_SUFFIXES = (".egg-info",)
_WALK_DENYLIST = {"__pycache__", "node_modules", ".venv"}

# Completable at top level alongside our commands even though they're not
# part of "Python internals" in the sense the debugger-mode restriction means.
_EXTRA_TOP_LEVEL = {"exit", "quit", "_"}

_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
_CLOSE_TO_OPEN = {")": "(", "]": "[", "}": "{"}


def _bracket_depth(text: str) -> int:
    """Net count of unmatched opening brackets in `text` (ignores strings)."""
    stack: list[str] = []
    for ch in text:
        if ch in _OPEN_TO_CLOSE:
            stack.append(ch)
        elif ch in _CLOSE_TO_OPEN:
            if stack and stack[-1] == _CLOSE_TO_OPEN[ch]:
                stack.pop()
    return len(stack)


class DebuggerCompleter(Completer):
    """Wraps ptpython's default completer; adds debugger-mode behaviors.

    `wrapped` is ptpython's normal completer (jedi over `__main__`, builtins,
    etc.) -- used as-is for `"classical"` mode.
    """

    def __init__(self, wrapped: Completer):
        self.wrapped = wrapped

    def get_completions(self, document: Document, complete_event):
        if SESSION.config.completion == "classical":
            yield from self.wrapped.get_completions(document, complete_event)
            return

        yield from self._debugger_completions(document, complete_event)

    def _debugger_completions(self, document: Document, complete_event):
        text = document.text_before_cursor

        if _bracket_depth(text) == 0:
            word = document.get_word_before_cursor()
            text_before_word = text[: len(text) - len(word)]
            if text_before_word.endswith("."):
                # Attribute access on an arbitrary expression -- "omit
                # everything else related to python/its modules".
                return
            yield from self._command_name_completions(word)
            return

        ctx = _find_call_context(text)
        if ctx is None:
            # Not recognizably inside one of our commands' calls -- debugger
            # mode offers nothing.
            return
        command, arg_index, paren_pos = ctx
        yield from self._argument_completions(command, arg_index, text[paren_pos + 1:])

    def _command_name_completions(self, word: str):
        names = sorted(set(_commands.__all__) | _EXTRA_TOP_LEVEL)
        for name in names:
            if name.startswith(word):
                yield Completion(name, start_position=-len(word))

    def _argument_completions(self, command: str, arg_index: int, after_paren_text: str):
        kinds = _ARG_TABLE.get(command)
        if kinds is None:
            return
        if isinstance(kinds, str):
            kind = kinds
        elif 0 <= arg_index < len(kinds):
            kind = kinds[arg_index]
        else:
            kind = None
        if kind is None:
            return

        arg_text = _current_arg_text(after_paren_text)

        if kind == "file":
            yield from _quoted_completions(arg_text, file_completions)
        elif kind == "run_arg":
            yield from self._run_arg_completions(arg_index, arg_text)
        elif kind == "thread_id":
            yield from self._thread_id_completions(arg_text)
        elif kind == "frame_index":
            yield from self._frame_index_completions(arg_text)
        elif kind == "exception_filter":
            yield from _quoted_completions(arg_text, _exception_filter_completions)

    def _run_arg_completions(self, arg_index: int, arg_text: str):
        """Completions for run()'s arguments: `script`, `*args`, and the
        `stdin=`/`stdout=`/`stderr=` redirection kwargs.

        The kwargs can appear after any number of `*args`, so kwarg-name
        discovery applies at every argument position, not just 0.
        """
        stripped = arg_text.lstrip()
        leading_ws = len(arg_text) - len(stripped)

        for kw in _RUN_KWARGS:
            prefix = kw + "="
            if stripped.startswith(prefix):
                value_text = arg_text[leading_ws + len(prefix):]
                if kw == "stderr":
                    yield from _quoted_completions(value_text, _stderr_value_completions)
                else:
                    yield from _quoted_completions(value_text, lambda f: file_completions(f, ext=None))
                return

        if not stripped.startswith(("'", '"')):
            for kw in _RUN_KWARGS:
                if kw.startswith(stripped):
                    yield Completion(kw + "=", start_position=-len(stripped))

        if arg_index == 0:
            yield from _quoted_completions(arg_text, file_completions)

    def _thread_id_completions(self, arg_text: str):
        fragment = arg_text.strip()
        if SESSION.client is None:
            return
        try:
            threads = SESSION.client.threads()["threads"]
        except _dap.DAPError:
            return
        for t in threads:
            tid = str(t["id"])
            if tid.startswith(fragment):
                yield Completion(tid, start_position=-len(fragment), display_meta=t.get("name", ""))

    def _frame_index_completions(self, arg_text: str):
        fragment = arg_text.strip()
        if SESSION.client is None or SESSION.current_thread_id is None:
            return
        try:
            frames = SESSION.client.stack_trace(SESSION.current_thread_id)["stackFrames"]
        except _dap.DAPError:
            return
        for i, f in enumerate(frames):
            idx = str(i)
            if idx.startswith(fragment):
                yield Completion(idx, start_position=-len(fragment), display_meta=f.get("name", ""))


def _find_call_context(text: str) -> tuple[str, int, int] | None:
    """If `text` ends inside an unmatched `(` of a known command, return
    `(command_name, arg_index, paren_pos)`; else None.

    `arg_index` is the count of top-level commas since `paren_pos`.
    Doesn't handle nested calls as the enclosing call (e.g.
    `breakpoint(foo(1, 2)` would misidentify the argument index) -- see
    completion_design.md §2.2.
    """
    stack: list[tuple[str, int]] = []
    for i, ch in enumerate(text):
        if ch in _OPEN_TO_CLOSE:
            stack.append((ch, i))
        elif ch in _CLOSE_TO_OPEN:
            if stack and stack[-1][0] == _CLOSE_TO_OPEN[ch]:
                stack.pop()

    if not stack or stack[-1][0] != "(":
        return None
    pos = stack[-1][1]

    end = pos
    while end > 0 and text[end - 1] in " \t":
        end -= 1
    start = end
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    name = text[start:end]
    if name not in _ARG_TABLE:
        return None

    arg_index = 0
    depth = 0
    for ch in text[pos + 1:]:
        if ch in _OPEN_TO_CLOSE:
            depth += 1
        elif ch in _CLOSE_TO_OPEN:
            depth -= 1
        elif ch == "," and depth == 0:
            arg_index += 1
    return name, arg_index, pos


def _current_arg_text(after_paren_text: str) -> str:
    """The portion of `after_paren_text` for the argument the cursor is in (after its last top-level comma)."""
    depth = 0
    last_comma = -1
    for i, ch in enumerate(after_paren_text):
        if ch in _OPEN_TO_CLOSE:
            depth += 1
        elif ch in _CLOSE_TO_OPEN:
            depth -= 1
        elif ch == "," and depth == 0:
            last_comma = i
    return after_paren_text[last_comma + 1:]


def _quoted_completions(arg_text: str, candidates_fn):
    """Yield Completions for candidates_fn(fragment), for a string-valued argument.

    If `arg_text` is already an open string literal (starts with `'`/`"`),
    complete its contents in place. Otherwise -- nothing typed yet, or a bare
    word typed without an opening quote -- treat the typed text (if any) as
    the fragment and insert fully-quoted completions in its place, so
    tab-completion works before the user types the opening quote.
    """
    text = arg_text.lstrip()
    if text and text[0] in ("'", '"'):
        fragment = text[1:]
        for candidate in candidates_fn(fragment):
            yield Completion(candidate, start_position=-len(fragment))
        return

    for candidate in candidates_fn(text):
        yield Completion(f'"{candidate}"', start_position=-len(arg_text))


def _exception_filter_completions(fragment: str) -> list[str]:
    return [f for f in _dap.EXCEPTION_BREAKPOINT_FILTERS if f.startswith(fragment)]


def _project_files(cwd: str = ".") -> list[str]:
    """All project files, cwd-relative. See completion_design.md §3."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=cwd, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        return tracked + untracked
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass

    files = []
    for root, dirs, filenames in os.walk(cwd):
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".")
            and d not in _WALK_DENYLIST
            and not d.endswith(_WALK_DENYLIST_SUFFIXES)
        ]
        for name in filenames:
            files.append(os.path.relpath(os.path.join(root, name), cwd))
    return files


_PY_EXTENSION = ".py"


def _classical_path_completions(fragment: str, ext: str | None = _PY_EXTENSION) -> list[str]:
    """Plain cwd-relative prefix completion: list `dirname(fragment)`, filter by basename prefix.

    Keeps directories (for navigating into them) and files matching `ext`
    (or every file, if `ext` is None) -- see "what's a Python script file"
    discussion.
    """
    dirname, base = os.path.split(fragment)
    parent = dirname or "."
    try:
        entries = os.listdir(parent)
    except OSError:
        return []
    result = []
    for e in entries:
        if not e.startswith(base):
            continue
        if ext is not None and not e.endswith(ext) and not os.path.isdir(os.path.join(parent, e)):
            continue
        result.append(os.path.join(dirname, e) if dirname else e)
    return result


_STDERR_TO_STDOUT = "&1"


def _stderr_value_completions(fragment: str) -> list[str]:
    """File paths plus the `"&1"` sentinel (shell `2>&1`-style), for `stderr=`."""
    candidates = file_completions(fragment, ext=None)
    if _STDERR_TO_STDOUT.startswith(fragment):
        candidates = [_STDERR_TO_STDOUT] + candidates
    return candidates


def file_completions(fragment: str, ext: str | None = _PY_EXTENSION) -> list[str]:
    """Candidate file paths for `fragment`, per completion_design.md §3.

    - Fragment containing `/`: classical cwd-relative prefix completion.
    - Fragment with no `/`: basename search across the whole project; falls
      back to classical prefix completion if nothing matches.

    `ext` restricts matches to that extension (default `.py`); pass `ext=None`
    for unrestricted file paths (e.g. stdin=/stdout=/stderr= redirection
    targets, which aren't Python scripts).
    """
    if "/" in fragment:
        return _classical_path_completions(fragment, ext)

    matches = [
        p for p in _project_files()
        if (ext is None or p.endswith(ext)) and os.path.basename(p).startswith(fragment)
    ]
    if matches:
        return matches
    return _classical_path_completions(fragment, ext)
