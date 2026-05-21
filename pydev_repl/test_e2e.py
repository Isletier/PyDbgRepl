"""End-to-end smoke test: launch sample.py under pydevd, step until done."""
import os

from pydev_repl.session import DebugSession


HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, "examples", "sample.py")


def main():
    session = DebugSession()
    try:
        resp = session.launch(SAMPLE)
        print("launch:", resp)
        assert resp.get("event") == "paused", f"expected paused, got {resp}"
        lines = [resp["line"]]

        steps = 0
        while True:
            resp = session.step_over()
            print(f"next #{steps + 1}:", resp)
            steps += 1
            if resp.get("event") != "paused":
                break
            lines.append(resp["line"])
            if steps > 30:
                raise AssertionError("too many steps; aborting")

        assert resp.get("event") == "finished", f"expected finished, got {resp}"
        print(f"\nOK: paused at lines {lines} over {steps} step(s).")
    finally:
        print("exit:", session.exit())


if __name__ == "__main__":
    main()
