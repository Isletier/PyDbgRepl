"""Source mapping control module"""

import pdvp.dap


type Source = str
type SourceMap = dict[Source, int]



class SourceMapT:
    mapping:    dict[Source, int]

    def __init__(self):
        self.mapping = dict()
