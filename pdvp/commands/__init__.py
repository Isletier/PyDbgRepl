"""Functions exposed to the REPL global namespace.

Split by topic into submodules; this package re-exports all of them as a
single flat namespace, matching the old single-file `commands.py` layout.
"""
from .breakpoints import *  # noqa: F401,F403
from .breakpoints import __all__ as _breakpoints_all
from .execution import *  # noqa: F401,F403
from .execution import __all__ as _execution_all
from .inspect_ import *  # noqa: F401,F403
from .inspect_ import __all__ as _inspect_all
from .lifecycle import *  # noqa: F401,F403
from .lifecycle import __all__ as _lifecycle_all
from .misc import *  # noqa: F401,F403
from .misc import __all__ as _misc_all
from .source import *  # noqa: F401,F403
from .source import __all__ as _source_all
from .stack import *  # noqa: F401,F403
from .stack import __all__ as _stack_all

# Not a topic module's command, but part of the same surface: a subscriber that
# changes the program's state owns the reporting of what it did, and its output
# lands while somebody is at a prompt.
from ..console import print_async  # noqa: F401

__all__ = [
    *_lifecycle_all,
    *_execution_all,
    *_stack_all,
    *_breakpoints_all,
    *_inspect_all,
    *_source_all,
    *_misc_all,
    "print_async",
]
