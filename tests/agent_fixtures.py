"""Synthetic graph drafts and isolated projects for the unified conversation.

Per-test database isolation does not need a full schema build: one empty project
is created per session and each test copies it. New fixtures should use
``fresh_project`` (or plain dicts for pure functions) instead of rebuilding.
"""

import shutil

import pytest

from narrative.operations import Operations

TEXT = "甲约好接乙。乙收起便笺。甲接到乙后离开。"

LOCATION_DEFINITION = {"kind": "property_definition", "id": "地点", "value_type": "either", "cardinality": "one"}


def location_record(event_id, source_id, start, quote, *, value="未知"):
    """Explicit event-owned world location state for graph-level fixtures."""
    return {"kind": "property_change", "id": f"loc-{event_id}", "node_id": event_id,
            "property_id": "地点", "event_id": event_id, "op": "set", "scope": "world",
            "value": value, "sequence": start,
            "disclosure": {"source_id": source_id, "start": start},
            "evidence": {"source_id": source_id, "quote": quote}}



def draft_result(*, note=False, ended=True):
    result = {
        "nodes": [
            {"type": "person", "name": "甲", "quotes": ["甲约好接乙。"]},
            {"type": "person", "name": "乙", "quotes": ["甲约好接乙。"]},
            {"type": "big_event", "name": "接人", "quotes": [TEXT]},
            {"type": "scene", "name": "甲约定并完成接乙", "big_event": "接人", "quotes": [TEXT]},
            {"type": "event", "name": "甲约好接乙", "scene": "甲约定并完成接乙",
             "location": "未知", "participants": ["甲", "乙"], "quotes": ["甲约好接乙。"]},
            {"type": "event", "name": "甲接到乙后离开", "scene": "甲约定并完成接乙",
             "location": "未知", "participants": ["甲", "乙"], "quotes": ["甲接到乙后离开。"]},
        ],
        "properties": [
            {"node": "甲", "property": "计划", "value": "接乙", "value_type": "string",
             "scope": "claim", "holder": "甲", "category": "心理与意图", "source": "speech",
             "quotes": ["甲约好接乙。"], "at_event": "甲约好接乙"},
            {"node": "甲", "property": "朋友", "value": "乙", "value_type": "node",
             "category": "关系", "quotes": ["甲约好接乙。"], "at_event": "甲约好接乙"},
        ],
        "uncertainties": [], "exclusions": [],
    }
    for row in result["properties"]:
        row["op"] = "set"
    if ended:
        result["properties"].append({"node": "甲", "property": "计划", "category": "未分类", "at_event": "甲接到乙后离开",
            "op": "clear", "scope": "claim", "holder": "甲", "quotes": ["甲接到乙后离开。"]})
    if note:
        result["nodes"].append({"type": "item", "name": "便笺", "quotes": ["乙收起便笺。"]})
        result["properties"].append({"node": "便笺", "property": "持有者", "value": "乙", "value_type": "either",
                                     "op": "set", "scope": "world", "category": "归属与持有", "quotes": ["乙收起便笺。"],
                                     "at_event": "甲接到乙后离开"})
    return result


def fresh_project(tmp_path, monkeypatch, *, template, name="unified"):
    """Copy the session template into this test's temp dir and open it.

    Copying preserves an empty, known state (so atomic/staging assertions stay
    meaningful) while skipping schema creation and directory scaffolding.
    """
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    # Keep the historical tmp_path/unified.nproj location: tests and CLI helpers
    # derive the entry path from tmp_path.
    shutil.copytree(template, tmp_path, dirs_exist_ok=True)
    return Operations.open(tmp_path / "unified.nproj")


@pytest.fixture
def project(tmp_path, monkeypatch, project_template):
    return fresh_project(tmp_path, monkeypatch, template=project_template)
