# Project notes for Claude

## Test/demo samples

Ad-hoc scripts used as debuggee targets during manual or live testing
(e.g. `python -i repl.py` sessions, end-to-end demos) belong in
`samples/targets/`. Larger standalone demo scripts (like
`samples/io_passthrough_demo.py`) go directly in `samples/`.

Don't leave throwaway target scripts in `/tmp` or the repo root — move
them into `samples/targets/` with a descriptive name so they can be
reused in future sessions.
