"""Thin graph-project entry points.

Use GraphOperations for project business operations and project_catalog for
listing, creating and opening projects.
"""

from __future__ import annotations

from pathlib import Path

from .graph_operations import GraphOperations
from .project_catalog import (
    create_graph_project,
    duplicate_graph_project,
    inspect_graph_project,
    list_projects,
    next_project_copy_path,
    open_graph_project,
)


class Operations:
    create_graph_project = staticmethod(create_graph_project)
    duplicate_graph_project = staticmethod(duplicate_graph_project)
    inspect_graph_project = staticmethod(inspect_graph_project)
    next_project_copy_path = staticmethod(next_project_copy_path)
    open_graph_project = staticmethod(open_graph_project)
    list_projects = staticmethod(list_projects)

    @staticmethod
    def open(path: str | Path) -> GraphOperations:
        return open_graph_project(path)
