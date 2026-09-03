"""pdvp.core: the thin one-request-one-response debugger command layer.

No caller gets privileged automatic setup (doc/architecture.md, P0) -- this
package does nothing on import beyond making names available. The REPL,
ptpython integration, and every other interactive-session concern live in
pdvp.extra, which this package does not know about.

    import pdvp.core as pdvp

    pdvp.process_args_envs(sys.argv[1:])

    # optional: pdvp.CONFIG.log_level = "debug"

    print(pdvp.run())
    # optional: further scenario lines, e.g. cont(), bt(5), ...

`from pdvp.core import *` (equivalently `from pdvp import *`, via the
umbrella package) gives the full command surface plus CONFIG directly, with
no injection step: pdvp.core does not touch __main__.

`SESSION` is deliberately not re-exported here: it is a mutation-heavy
internal object (control rights, resume-wait bookkeeping, `reduce()`,
`begin()`/`end()`), not a read-only status view, so it stays reachable only
as `pdvp.core.session.SESSION` -- an explicit "you're reaching into
internals" import rather than a blessed top-level name. See
doc/architecture.md's Open section for a curated public read-surface
(connection status, current thread, event-bus subscribe) as a future,
separately-designed alternative.
"""

import sys as _sys

from pdvp.core import commands as _commands
from pdvp.core import launch
from pdvp.core.commands import *  # noqa: F401,F403
from pdvp.core.commands import __all__ as _commands_all
#: The live configuration. Assign to it directly: `pdvp.CONFIG.port = 5678`.
#: It lives in the `pdvp.core.config` module, which is why it is not itself
#: named `config` here -- `pdvp.core.config` is that module. At the prompt,
#: `from pdvp import CONFIG as config` gets the lowercase spelling.
from pdvp.core.config import CONFIG

__all__ = [*_commands_all, "process_args_envs", "CONFIG"]


def process_args_envs(argv: list[str] | None = None) -> None:
    """Populate CONFIG from the launch command line, and tidy the environment.

    Does not start anything, even if --file was given (it is just saved to
    CONFIG for run() to pick up later).

    Environment handling is deliberately near-zero: pydevd is configured
    through os.environ like any other program, so the only thing we do is drop
    inherited debug settings that would otherwise make us adopt another
    debugger's configuration (launch.ENV_SANITIZE). Assign to os.environ
    yourself, before or after this call -- later assignments win, and the
    inferior inherits our environment as-is.
    """
    argv = _sys.argv[1:] if argv is None else argv

    launch.scrub_env()

    try:
        launch.parse_argv(CONFIG, argv)
    except launch.LaunchError as e:
        print(f"error: {e}")
        raise SystemExit(1)
