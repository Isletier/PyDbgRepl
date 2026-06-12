"""Sample debuggee: read a line from stdin and echo it back.

Used by samples/io_passthrough_demo.py to exercise pydev-repl's stdin
passthrough (see doc/io_model.md).
"""
import sys

line = sys.stdin.readline()
print(f"hello {line.strip()}")
