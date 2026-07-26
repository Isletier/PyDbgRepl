"""Source mapping control module"""

import pdvp.dap as dap
import os
import tempfile
import dataclasses
from pathlib import Path

type SourcePath = str

@dataclasses.dataclass
class SourceMap:
    type SourceRef  = int
    type SourceId   = tuple[str, SourceRef]

    _Source2Path:    dict[SourceId, SourcePath] = dataclasses.field(default_factory=dict)
    _Path2Source:    dict[SourcePath, dap.Source] = dataclasses.field(default_factory=dict)
    _count = 0

    def _next_temp_name() -> str:
        _count = _count + 1
        return str("pdvp_source_" + str(_count) / ".py")

    def _fetch(self, source: dap.Source) -> SourcePath:
        from pdvp.session import SESSION
        responce = SESSION.client.source(ref, source)

        name: str= str(source.sourceReference)
        if source.name is not None:
            name = _next_temp_name()

        full_path = Path(SESSION.options.source_catalog) / name
        with open(full_path, "w", encoding="utf-8") as file:
            file.write(responce.body.content)

        return str(full_path)


    def _register_pair(self, source: dap.Source, path: SourcePath):
        Id: SourceId = [source.path, source.sourceReference]
        self._Source2Path[Id] = path
        self._Path2Source[path] = source


    def register_source(self, source: dap.Source) -> SourcePath:
        if source.sourceReference is None and source.path is None:
            print("upidipup")
            return

        if source.sourceReference is None:
            if os.path.exists(source.path):
                #don't register anything, just pass directly
                self._Path2Source[source.path] = source
                return source.path
            else:
                #should not happend, but just in case
                #create temporary anyway, treat recived path as an id
                path = self._fetch(source)
                self._register_pair(source, path)
                return path

        # ref presented, check if already fetched
        Id: SourceId = [source.path, source.sourceReference]
        if (path := self._Source2Path.get(SourceId)) is not None:
            return path

        # ref presented, haven't been fetched
        path = self._fetch(source)
        self._register_pair(source, path)
        return path

    def get_source(self, path: SourcePath) -> dap.Source:
        if (source := self._Path2Source.get(path)) is not None:
            return source

        return dap.Source(path=path)


