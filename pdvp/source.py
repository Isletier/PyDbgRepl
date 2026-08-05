"""Source mapping control module"""

import pdvp.dap as dap
import dataclasses
from pathlib import Path

type SourcePath = str

@dataclasses.dataclass
class SourceMap:
    type SourceRef  = int
    type SourceId   = tuple[str | None, SourceRef]

    _Source2Path:    dict[SourceId, SourcePath] = dataclasses.field(default_factory=dict)
    _Path2Source:    dict[SourcePath, dap.Source] = dataclasses.field(default_factory=dict)
    _count:          int = 0

    def _next_temp_name(self, hint: str | None = None) -> str:
        """A unique file name for a fetched source, under `source_catalog`.

        `hint` (the DAP source's `name`) only keeps the file recognizable --
        the counter is what makes it unique, and Path().stem drops any
        directory part so a hostile name can't escape the catalog.
        """
        self._count += 1
        stem = Path(hint).stem if hint else "source"
        return f"pdvp_{self._count}_{stem}.py"

    def _fetch(self, source: dap.Source) -> SourcePath:
        from pdvp.session import SESSION
        responce = SESSION.client.source(source.sourceReference, source)

        name: str = self._next_temp_name(source.name)

        full_path = Path(SESSION.config.source_catalog) / name
        with open(full_path, "w", encoding="utf-8") as file:
            file.write(responce.body.content)

        return str(full_path)


    def _register_pair(self, source: dap.Source, path: SourcePath):
        Id: SourceId = (source.path, source.sourceReference)
        self._Source2Path[Id] = path
        self._Path2Source[path] = source


    def clear(self) -> None:
        """Drop everything: a sourceReference is only valid for one DAP
        session (see the DAP spec), so nothing here outlives the connection.
        Fetched files under `source_catalog` are left on disk."""
        self._Source2Path.clear()
        self._Path2Source.clear()


    def register_source(self, source: dap.Source) -> SourcePath | None:
        # A sourceReference of 0 means "not available" per the DAP spec, same
        # as it being absent. With no path either, there is nothing to map.
        if not source.sourceReference and source.path is None:
            return None

        if not source.sourceReference:
            #don't register anything, just pass directly
            self._Path2Source[source.path] = source
            return source.path

        # ref presented, check if already fetched
        Id: SourceId = (source.path, source.sourceReference)
        if (path := self._Source2Path.get(Id)) is not None:
            return path

        # ref presented, haven't been fetched
        path = self._fetch(source)
        self._register_pair(source, path)
        return path

    def get_source(self, path: SourcePath) -> dap.Source:
        if (source := self._Path2Source.get(path)) is not None:
            return source

        return dap.Source(path=path)
