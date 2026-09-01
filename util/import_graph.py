#!/usr/bin/env python3
"""Print the intra-project (pdvp -> pdvp) import graph and flag cycles.

Parses imports with `ast` rather than importing the package, so it reflects
what's on disk even if the package doesn't currently import cleanly. Test
modules are excluded by default since they legitimately import broadly and
would just add noise to the production dependency tree.

Usage:
    util/import_graph.py [--cycles-only] [--include-tests]
"""
import ast
import pathlib
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = "pdvp"


def module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == PKG:
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and node.module.split(".")[0] == PKG:
                found.add(node.module)
    return found


def is_test_path(path: pathlib.Path) -> bool:
    return "test" in path.relative_to(ROOT).parts or path.stem.startswith("test_")


def build_graph(include_tests: bool) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for path in sorted((ROOT / PKG).rglob("*.py")):
        if not include_tests and is_test_path(path):
            continue
        mod = module_name(path)
        graph[mod] |= imported_modules(path)
    return graph


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    cycles = []
    visited = set()

    def dfs(node: str, stack: list[str]) -> None:
        if node in stack:
            i = stack.index(node)
            cycles.append(stack[i:] + [node])
            return
        if node in visited:
            return
        visited.add(node)
        for nxt in sorted(graph.get(node, ())):
            dfs(nxt, stack + [node])

    for n in sorted(graph):
        dfs(n, [])
    return cycles


def main(argv: list[str]) -> None:
    include_tests = "--include-tests" in argv
    cycles_only = "--cycles-only" in argv
    graph = build_graph(include_tests)

    if not cycles_only:
        for mod in sorted(graph):
            deps = sorted(graph[mod] - {mod})
            if deps:
                print(mod)
                for dep in deps:
                    print(f"  -> {dep}")

    cycles = find_cycles(graph)
    if cycles:
        print("\nCYCLES:")
        for c in cycles:
            print("  " + " -> ".join(c))
    else:
        print("\nno import cycles")


if __name__ == "__main__":
    main(sys.argv[1:])
