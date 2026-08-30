"""Unit tests for `pdvp/source.py`'s `SourceMap`: the id-table cache between a
DAP `Source` (path and/or sourceReference) and the on-disk file a fetched one
is written to.

No pydevd, no sockets: `SESSION.client` is swapped for a `_FakeClient` whose
`source()` records how many times it was called and hands back canned
content, the same style `test_execution.py`'s `FakeClient` uses. `SourceMap`
only ever reaches `SESSION` through `SESSION.client`, so this needs none of
that file's module-wide `sys.modules` patching -- one attribute, restored
after each test.

No test framework dependency: each test_* function takes no arguments, raises
AssertionError on failure, and the __main__ runner reports pass/fail for all
of them. pytest also collects these directly by name.

Run from the repo root with the venv active:

    python -m pdvp.test.test_source
"""
import os
import tempfile
from pathlib import Path

from .. import session as _session
from ..config import CONFIG
from ..dap import Source
from ..source import SourceMap


class _Body:
    def __init__(self, content: str):
        self.content = content


class _Response:
    def __init__(self, content: str):
        self.body = _Body(content)


class _FakeClient:
    """Stands in for `dap.Client`'s one method `SourceMap` calls."""

    def __init__(self):
        self.calls: list[tuple[int, Source]] = []
        self.content = "print('fetched')\n"

    def source(self, source_reference: int, source: Source) -> _Response:
        self.calls.append((source_reference, source))
        return _Response(self.content)


def _with_catalog():
    """A temp dir installed as CONFIG.source_catalog, plus a restore()."""
    tmp = tempfile.TemporaryDirectory()
    before = CONFIG.source_catalog
    CONFIG.source_catalog = tmp.name

    def restore() -> None:
        CONFIG.source_catalog = before
        tmp.cleanup()

    return tmp.name, restore


def _with_client(client) -> callable:
    """Install `client` as SESSION.client; returns a restore()."""
    before = _session.SESSION.client
    _session.SESSION.client = client

    def restore() -> None:
        _session.SESSION.client = before

    return restore


# --------------------------------------------------------- register_source()

def test_a_path_only_source_registers_without_fetching() -> None:
    client = _FakeClient()
    restore_client = _with_client(client)
    try:
        source_map = SourceMap()
        source = Source(path="/debuggee/mod.py")

        result = source_map.register_source(source)

        assert result == "/debuggee/mod.py"
        assert source_map._Path2Source["/debuggee/mod.py"] is source
        assert client.calls == []
    finally:
        restore_client()


def test_a_source_with_neither_reference_nor_path_registers_nothing() -> None:
    client = _FakeClient()
    restore_client = _with_client(client)
    try:
        source_map = SourceMap()
        result = source_map.register_source(Source())

        assert result is None
        assert source_map._Path2Source == {}
        assert source_map._Source2Path == {}
        assert client.calls == []
    finally:
        restore_client()


def test_a_referenced_source_is_fetched_and_written_under_the_catalog() -> None:
    client = _FakeClient()
    restore_client = _with_client(client)
    catalog, restore_catalog = _with_catalog()
    try:
        source_map = SourceMap()
        source = Source(path=None, sourceReference=7, name="mod.py")

        result = source_map.register_source(source)

        assert Path(result).parent == Path(catalog)
        assert Path(result).exists()
        assert Path(result).read_text() == client.content
        assert len(client.calls) == 1
        assert client.calls[0] == (7, source)

        Id = (source.path, source.sourceReference)
        assert source_map._Source2Path[Id] == result
        assert source_map._Path2Source[result] is source
    finally:
        restore_catalog()
        restore_client()


def test_the_same_reference_is_fetched_only_once() -> None:
    client = _FakeClient()
    restore_client = _with_client(client)
    catalog, restore_catalog = _with_catalog()
    try:
        source_map = SourceMap()
        source = Source(path="/debuggee/mod.py", sourceReference=7, name="mod.py")

        first = source_map.register_source(source)
        second = source_map.register_source(source)

        assert first == second
        assert len(client.calls) == 1, "the second call must hit the id-table cache, not fetch again"
    finally:
        restore_catalog()
        restore_client()


def test_the_same_hint_twice_still_gets_distinct_files() -> None:
    client = _FakeClient()
    restore_client = _with_client(client)
    catalog, restore_catalog = _with_catalog()
    try:
        source_map = SourceMap()
        first = source_map.register_source(Source(sourceReference=1, name="foo.py"))
        second = source_map.register_source(Source(sourceReference=2, name="foo.py"))

        assert first != second, "the counter in _next_temp_name must disambiguate same-named sources"
        assert Path(first).exists() and Path(second).exists()
    finally:
        restore_catalog()
        restore_client()


def test_a_hostile_hint_cannot_escape_the_catalog() -> None:
    client = _FakeClient()
    restore_client = _with_client(client)
    catalog, restore_catalog = _with_catalog()
    try:
        source_map = SourceMap()
        for hint in ("../../etc/passwd", "/etc/passwd", "../../../x"):
            result = source_map.register_source(
                Source(sourceReference=source_map._count + 1, name=hint))
            assert Path(result).parent == Path(catalog), (hint, result)
    finally:
        restore_catalog()
        restore_client()


# -------------------------------------------------------------- get_source()

def test_get_source_returns_the_registered_object() -> None:
    client = _FakeClient()
    restore_client = _with_client(client)
    try:
        source_map = SourceMap()
        source = Source(path="/debuggee/mod.py")
        source_map.register_source(source)

        assert source_map.get_source("/debuggee/mod.py") is source
    finally:
        restore_client()


def test_get_source_for_an_unseen_path_is_a_bare_source() -> None:
    source_map = SourceMap()
    result = source_map.get_source("/never/registered.py")

    assert isinstance(result, Source)
    assert result.path == "/never/registered.py"
    assert result.sourceReference is None


# ------------------------------------------------------------------ clear()

def test_clear_drops_the_tables_but_leaves_fetched_files_on_disk() -> None:
    client = _FakeClient()
    restore_client = _with_client(client)
    catalog, restore_catalog = _with_catalog()
    try:
        source_map = SourceMap()
        source = Source(path="/debuggee/mod.py", sourceReference=7, name="mod.py")
        fetched_path = source_map.register_source(source)

        source_map.clear()

        assert source_map._Source2Path == {}
        assert source_map._Path2Source == {}
        # get_source() after clear() falls back to the bare-Source case again.
        again = source_map.get_source(fetched_path)
        assert again.path == fetched_path and again.sourceReference is None

        assert os.path.exists(fetched_path), "clear() must not touch files already on disk"
    finally:
        restore_catalog()
        restore_client()


TESTS = [value for name, value in sorted(globals().items()) if name.startswith("test_")]


def main() -> int:
    failures = 0
    for test in TESTS:
        try:
            test()
        except Exception as error:
            failures += 1
            print(f"FAIL {test.__name__}: {type(error).__name__}: {error}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
