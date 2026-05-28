"""Interactive REPL on top of DebugSession."""
from pydev_repl.session import DebugSession


USAGE = (
    "Commands:\n"
    "  launch <path>   start debugging the script at <path>\n"
    "  next            step over to the next source line\n"
    "  exit            quit (alias: quit)\n"
    "  help            show this help\n"
)


def _fmt(resp):
    if not isinstance(resp, dict):
        return repr(resp)
    ev = resp.get("event")
    if ev == "paused":
        loc = f"{resp.get('file')}:{resp.get('line')}"
        reason = resp.get("reason")
        return f"paused at {loc}" + (f" ({reason})" if reason else "")
    if ev == "finished":
        ec = resp.get("exitCode")
        return f"debuggee finished (exit={ec})" if ec is not None else "debuggee finished"
    if ev == "shutdown":
        return "session closed"
    if ev == "error":
        return f"error: {resp.get('message')}"
    return repr(resp)


def run():
    session = DebugSession()
    print("pydev-repl prototype. Type 'help' for commands.")
    try:
        while True:
            try:
                line = input("(pydev-repl) ").strip()
            except EOFError:
                print()
                break
            if not line:
                continue
            parts = line.split(None, 1)
            cmd = parts[0]
            if cmd == "help":
                print(USAGE)
            elif cmd == "launch":
                if len(parts) != 2:
                    print("usage: launch <path>")
                    continue
                try:
                    print(_fmt(session.launch(parts[1])))
                except Exception as e:
                    print(f"error: {e!r}")
            elif cmd == "next":
                try:
                    print(_fmt(session.step_over()))
                except Exception as e:
                    print(f"error: {e!r}")
            elif cmd in ("exit", "quit"):
                break
            else:
                print(f"unknown command: {cmd!r} (try 'help')")
    finally:
        print(_fmt(session.exit()))


if __name__ == "__main__":
    run()
