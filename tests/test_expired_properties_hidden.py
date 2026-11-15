"""过期（失效）的属性不显示——这是项目从第一天起的基本要求。

两层都要守：
* 数据层：同一属性后一次提及让前一次失效，读取只给当前值，旧值只在 `records` 里留痕（`is_current=false`）；
* 展示层：浏览区的属性行不得把 `records`（含已失效的旧值）铺成第二份取值。

历史用例见过一次真实的破口：属性行展开后把全部披露记录逐条列出来，于是「矮小黝黑」这种
早已被取代的旧外貌和当前值并排显示，读起来像两个都成立。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from narrative.operations import Operations

ROOT = Path(__file__).resolve().parents[1]
TEXT = "李强今年二十九岁，个头矮小，皮肤黝黑。三年后他改造过，如今身高一百八十公分。"
FIRST, SECOND = "李强出场", "改造之后"


@pytest.fixture
def ops(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    project = Operations.create_graph_project(tmp_path / "expired.nproj")
    source = project.import_source("外貌", TEXT)
    accepted = project.submit_extraction(source, {
        "nodes": [
            {"type": "person", "name": "李强", "quotes": ["李强"]},
            {"type": "scene", "name": "出场", "quotes": ["李强今年二十九岁"]},
            {"type": "event", "name": FIRST, "scene": "出场", "participants": ["李强"],
             "location": "未知", "quotes": ["个头矮小，皮肤黝黑"]},
            {"type": "event", "name": SECOND, "scene": "出场", "participants": ["李强"],
             "location": "未知", "quotes": ["如今身高一百八十公分"]},
        ],
        "properties": [
            {"node": "李强", "property": "外貌", "value": "矮小黑瘦", "value_type": "string",
             "scope": "world", "at_event": FIRST, "op": "set", "quotes": ["个头矮小，皮肤黝黑"],
             "category": "外貌"},
            {"node": "李强", "property": "外貌", "value": "身高一百八十公分", "value_type": "string",
             "scope": "world", "at_event": SECOND, "op": "set", "quotes": ["如今身高一百八十公分"],
             "category": "外貌"},
        ],
    })
    assert accepted["status"] == "accepted", accepted
    return project


def test_only_the_current_value_is_a_current_value(ops):
    person = next(node for node in ops.graph_snapshot()["nodes"] if node["node_type"] == "person")
    row = next(item for item in ops.property_node_summary(person["id"])["properties"]
               if item["property_id"] == "外貌")
    assert row["state"] == {"kind": "known", "values": ["身高一百八十公分"]}
    # 旧值仍留痕，但明确不是当前值。
    expired = [record for record in row["records"] if not record["is_current"]]
    assert [record["value"] for record in expired] == ["矮小黑瘦"]
    assert row["is_current"] is True


def test_a_superseded_value_does_not_get_its_own_row(ops):
    person = next(node for node in ops.graph_snapshot()["nodes"] if node["node_type"] == "person")
    payload = ops.property_node_summary(person["id"])
    aspects = [item["property_id"] for item in payload["properties"]]
    assert aspects.count("外貌") == 1
    group = next(category for category in payload["categories"] if category["key"] == "外貌")
    assert [item["property_id"] for item in group["items"]] == ["外貌"]


def test_browse_row_does_not_render_the_disclosure_history():
    """展示层的守卫：浏览区的属性行不许把 row.records 铺出来。"""
    item = (ROOT / "src" / "narrative" / "qml" / "PropertyItem.qml").read_text(encoding="utf-8")
    assert "row.records" not in item
    assert "model: item.row.records" not in item
