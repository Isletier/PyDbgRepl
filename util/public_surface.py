#!/usr/bin/env python3
"""List what each pdvp module exports -- plain data, no verdicts.

For every module under pdvp/, prints its `__all__` if declared, otherwise
falls back to every top-level name that doesn't start with `_` (and flags
that fallback, since it means `import *` on that module isn't curated).

Usage:
    util/public_surface.py [dotted.module ...]

With no arguments, walks every non-test module under pdvp/.
"""
import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = "pdvp"

sys.path.insert(0, str(ROOT))


def module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def discover_modules() -> list[str]:
    names = []
    for path in sorted((ROOT / PKG).rglob("*.py")):
        rel = path.relative_to(ROOT)
        if "test" in rel.parts or path.stem.startswith("test_"):
            continue
        names.append(module_name(path))
    return names


def report(mod_name: str) -> None:
    mod = importlib.import_module(mod_name)
    all_ = getattr(mod, "__all__", None)
    if all_ is not None:
        names, source = list(all_), "__all__"
    else:
        names, source = [n for n in dir(mod) if not n.startswith("_")], "dir() -- no __all__"

    print(f"{mod_name} ({source}, {len(names)}):")
    for n in names:
        print(f"  {n}")


def main(argv: list[str]) -> None:
    for name in argv or discover_modules():
        report(name)


if __name__ == "__main__":
    main(sys.argv[1:])
