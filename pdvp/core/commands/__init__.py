"""Functions exposed to the REPL global namespace.

Split by topic into submodules; this package re-exports all of them as a
single flat namespace, matching the old single-file `commands.py` layout.
"""
from pdvp.core.commands.breakpoints import *  # noqa: F401,F403
from pdvp.core.commands.breakpoints import __all__ as _breakpoints_all
from pdvp.core.commands.execution import *  # noqa: F401,F403
from pdvp.core.commands.execution import __all__ as _execution_all
from pdvp.core.commands.inspection import *  # noqa: F401,F403
from pdvp.core.commands.inspection import __all__ as _inspection_all
from pdvp.core.commands.lifecycle import *  # noqa: F401,F403
from pdvp.core.commands.lifecycle import __all__ as _lifecycle_all
from pdvp.core.commands.misc import *  # noqa: F401,F403
from pdvp.core.commands.misc import __all__ as _misc_all
from pdvp.core.commands.source import *  # noqa: F401,F403
from pdvp.core.commands.source import __all__ as _source_all
from pdvp.core.commands.stack import *  # noqa: F401,F403
from pdvp.core.commands.stack import __all__ as _stack_all

# Not a topic module's command, but part of the same surface: a subscriber that
# changes the program's state owns the reporting of what it did, and its output
# lands while somebody is at a prompt.
from pdvp.core.console import print_async  # noqa: F401

__all__ = [
    *_lifecycle_all,
    *_execution_all,
    *_stack_all,
    *_breakpoints_all,
    *_inspection_all,
    *_source_all,
    *_misc_all,
    "print_async",
]
