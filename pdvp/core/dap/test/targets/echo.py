"""Blocks on stdin, echoes the line back -- pdvp's stdin-passthrough target."""
import sys

line = sys.stdin.readline()
print(f"echo: {line.strip()}", flush=True)
