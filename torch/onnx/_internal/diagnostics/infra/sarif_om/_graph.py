# This file was generated by jschema_to_python version 1.2.3.

import attr


@attr.s
class Graph(object):
    """A network of nodes and directed edges that describes some aspect of the structure of the code (for example, a call graph)."""

    description = attr.ib(
        default=None, metadata={"schema_property_name": "description"}
    )
    edges = attr.ib(default=None, metadata={"schema_property_name": "edges"})
    nodes = attr.ib(default=None, metadata={"schema_property_name": "nodes"})
    properties = attr.ib(default=None, metadata={"schema_property_name": "properties"})


# flake8: noqa
