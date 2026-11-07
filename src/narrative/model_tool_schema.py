"""Model-only contracts omit host provenance, never business concurrency inputs."""
from __future__ import annotations

from copy import deepcopy

PROVENANCE_TOOLS = frozenset({'submit_scene_table', 'submit_script_unit', 'mark_writing_basis_current'})
PROVENANCE_FIELDS = frozenset({'task_id', 'manifest_ref'})


def model_tool_schema(tool) -> dict:
    schema = deepcopy(tool.schema())
    if tool.name in PROVENANCE_TOOLS:
        for name in PROVENANCE_FIELDS:
            schema.get('properties', {}).pop(name, None)
        if 'required' in schema:
            schema['required'] = [name for name in schema['required'] if name not in PROVENANCE_FIELDS]
    return schema
