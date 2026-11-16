"""实体必须在事件链上有落点：没有落点就报 `entity_unanchored`（warning，不阻塞）。

「事件是时间点，事件链就是时间轴」的直接后果：一个既不出现在任何事件里、也不是
事件发生处的地点、没有在事件处易手、也没有被任何事件处属性引用的实体，在时间轴上
是查不到的——它可能是录入冗余，也可能是漏挂事件，两种都只能人工核。所以这里只报
warning，不改数据、不阻塞入库。
"""
from __future__ import annotations

import pytest

from narrative.operations import Operations

TEXT = "林岚把铜钥匙交给周宁。周宁收好钥匙离开档案室。"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    ops = Operations.create_graph_project(tmp_path / "unanchored.nproj")
    return ops, ops.import_source("落点", TEXT)


def codes(ops) -> list[tuple[str, str]]:
    return [(row["code"], row.get("object_name") or row.get("node_name") or "")
            for row in ops.check_project()["issues"]]


def anchored_draft() -> dict:
    return {"nodes": [
        {"type": "person", "name": "林岚"},
        {"type": "person", "name": "周宁"},
        {"type": "location", "name": "档案室"},
        {"type": "item", "name": "铜钥匙"},
        {"type": "scene", "name": "交接"},
        {"type": "event", "name": "交出钥匙", "scene": "交接", "participants": ["林岚", "周宁"],
         "location": "档案室", "anchor": {"start": 0, "end": len(TEXT)}},
    ], "properties": [
        {"node": "铜钥匙", "property": "持有者", "category": "未分类", "value": "周宁",
         "value_type": "either", "scope": "world", "at_event": "交出钥匙", "op": "set"},
    ]}


def test_entity_without_any_event_link_is_reported(project):
    ops, source = project
    result = ops.submit_extraction(source, {"nodes": [
        {"type": "person", "name": "无名路人"},
    ], "properties": []})
    assert result["status"] == "accepted", result
    row = next(item for item in ops.check_project()["issues"]
               if item["code"] == "entity_unanchored")
    assert row["severity"] == "warning"
    assert "无名路人" in row["message"]
    # 不阻塞：实体照样入库。
    assert any(node["name"] == "无名路人" for node in ops.graph_snapshot()["nodes"])


def test_entities_reachable_through_an_event_are_not_reported(project):
    ops, source = project
    assert ops.submit_extraction(source, anchored_draft())["status"] == "accepted"
    assert not [item for item in codes(ops) if item[0] == "entity_unanchored"]


def test_property_reference_alone_counts_as_an_anchor(project):
    ops, source = project
    result = ops.submit_extraction(source, {
        "nodes": [
            {"type": "person", "name": "旁听者"},
            {"type": "scene", "name": "宣布现场"},
            {"type": "event", "name": "宣布", "scene": "宣布现场", "participants": ["旁听者"], "location": "未知",
             "anchor": {"start": 0, "end": len(TEXT)}},
        ],
        "properties": [
            {"node": "宣布", "property": "在场者", "category": "未分类", "value": "旁听者",
             "value_type": "node", "scope": "world", "at_event": "宣布", "op": "set"},
        ]})
    assert result["status"] == "accepted", result
    assert not [item for item in codes(ops) if item[0] == "entity_unanchored"]
