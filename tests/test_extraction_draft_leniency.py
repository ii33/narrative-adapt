"""草稿里的无效参数只丢弃并提醒（`U-10`），只有"看不懂"的输入才整批拒收。

用户裁定：**无效参数只丢弃然后提醒**。以前草稿里带一个不该带的字段（非事件的 `quotes`、
属性上的 `quotes`／`anchor`、草稿行里的未知字段）就让整批 `DRAFT_SCHEMA` 作废——一处多余
全废，和项目"留白、不过度防御"的原则冲突。现在的口径分两边：

* **丢弃 + info**：多余字段、摆在没意义的字段上的东西、未知字段。同类 `(section, field)`
  折叠成一条 `EXTRACTION_INFO`，`count` 是实际处数，`indexes` 指出涉及的条目（最多 12 个，
  口径同 `extraction_staging._compact`），不铺几十行一样的提示；
* **拒收**：缺必填、类型不对、引用解析不到、事件没有定位依据——这些是"看不懂"，不是"多余"。

`tests/test_extraction_evidence_anchor.py` 守引文契约本身，这里守"丢弃与拒收的边界"。
"""
from __future__ import annotations

import json

import pytest

from narrative.operations import Operations

TEXT = "林岚在档案室交出铜钥匙。周宁收下钥匙离开档案室。"
FIRST = "交出钥匙"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    ops = Operations.create_graph_project(tmp_path / "lenient.nproj")
    return ops, ops.import_source("宽容", TEXT)


def draft() -> dict:
    return {"nodes": [
        {"type": "person", "name": "林岚"},
        {"type": "person", "name": "周宁"},
        {"type": "location", "name": "档案室"},
        {"type": "item", "name": "铜钥匙"},
        {"type": "scene", "name": "档案室交接"},
        {"type": "event", "name": FIRST, "scene": "@5", "participants": ["@1", "@2"],
         "location": "@3", "anchor": {"start": 0, "end": len(TEXT)}},
    ], "properties": [
        {"node": "@4", "property": "持有者", "category": "未分类", "value": "@1",
         "value_type": "either", "scope": "world", "at_event": "@6", "op": "set"},
    ]}


def notices(result: dict, *, field: str | None = None, section: str | None = None) -> list[dict]:
    return [row for row in result.get("info") or []
            if row.get("code") == "EXTRACTION_INFO"
            and (field is None or row.get("field") == field)
            and (section is None or row.get("path") == [section])]


def paths(result: dict) -> set[tuple]:
    return {tuple(row["path"]) for row in result.get("problems") or result.get("diagnostics") or []}


def test_unknown_fields_are_dropped_in_one_folded_info(project):
    """未知字段折叠成一条：count 记处数、indexes 记条目，其余内容照常入库。"""
    ops, source = project
    raw = draft()
    raw["nodes"][0]["identity_note"] = "存储层的字段"
    raw["nodes"][1]["identity_note"] = "存储层的字段"
    raw["nodes"][4]["为什么"] = "多余的说明"
    raw["properties"][0]["sequence"] = 3
    raw["comments"] = "顶层多余的键"
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "accepted", result
    assert not [row for row in result.get("diagnostics") or [] if row.get("severity") == "error"]
    folded = notices(result, field="identity_note", section="nodes")
    assert len(folded) == 1, result
    assert (folded[0]["count"], folded[0]["indexes"]) == (2, [0, 1])
    # 每个 (section, field) 各一条，不铺开：3 个未知字段就是 3 条，不是 5 条。
    assert len([row for row in notices(result) if row.get("path") in (["nodes"], ["properties"])]) == 3
    assert notices(result, section="comments")
    # 丢弃是真丢弃：那些值一个都没进图。
    stored = json.dumps(ops.graph_snapshot()["nodes"] + ops.property_records(), ensure_ascii=False)
    assert "存储层的字段" not in stored and "多余的说明" not in stored and "顶层多余的键" not in stored
    # 能理解的部分照常入库。
    assert {"林岚", "周宁", "档案室", "铜钥匙"} <= {node["name"] for node in ops.graph_snapshot()["nodes"]}


def test_one_invalid_parameter_does_not_sink_the_whole_batch(project):
    """一处多余 + 一处有效同批：有效部分入库，多余部分只报 info。"""
    ops, source = project
    raw = draft()
    raw["nodes"][0]["quotes"] = [TEXT[:6]]        # 人物不该带引文
    raw["properties"][0]["quotes"] = [TEXT[:6]]   # 属性不该带引文
    raw["properties"][0]["anchor"] = {"start": 0, "end": 3}
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "accepted", result
    assert not [row for row in result.get("diagnostics") or [] if row.get("severity") == "error"]
    assert (notices(result, field="quotes", section="nodes")[0]["indexes"]) == [0]
    assert (notices(result, field="quotes", section="properties")[0]["indexes"]) == [0]
    assert notices(result, field="anchor", section="properties")
    event = next(node for node in ops.graph_snapshot()["nodes"] if node["node_type"] == "event")
    change = next(record for record in ops.property_records() if record.get("kind") == "property_change")
    # 有效部分真的写进去了：事件的证据是锚点截取的原文，属性的证据取它的事件。
    assert event["evidence"] == {"source_id": source, "quote": TEXT}
    assert change["evidence"] == event["evidence"]


def test_event_without_any_locator_is_still_rejected(project):
    """事件没有定位依据是"看不懂"，不是"多余"：仍然拒收。"""
    ops, source = project
    raw = draft()
    del raw["nodes"][5]["anchor"]
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "staged"
    assert ("nodes", 5, "anchor") in paths(result)
    assert not ops.graph_snapshot()["nodes"]


@pytest.mark.parametrize("anchor", [{"start": 30, "end": 4}, {"start": 0, "end": len(TEXT) + 3}])
def test_bad_anchor_is_still_rejected(project, anchor):
    ops, source = project
    raw = draft()
    raw["nodes"][5]["anchor"] = anchor
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "staged"
    assert ("nodes", 5, "anchor") in paths(result)
    assert not ops.graph_snapshot()["nodes"]


@pytest.mark.parametrize("break_it", ["missing_category", "missing_value", "unknown_reference"])
def test_broken_required_parts_are_still_rejected(project, break_it):
    """缺必填、类型不对、引用解析不到都还是拒收——丢弃只针对多余的东西。"""
    ops, source = project
    raw = draft()
    if break_it == "missing_category":
        del raw["properties"][0]["category"]
        expected = ("properties", 0, "category")
    elif break_it == "missing_value":
        del raw["properties"][0]["value"]
        expected = ("properties", 0, "value")
    else:
        raw["nodes"][5]["participants"] = ["@99"]
        expected = ("nodes", 5, "participants", 0)
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "staged"
    assert expected in paths(result)
    assert not ops.graph_snapshot()["nodes"]


def test_row_stripped_to_nothing_is_rejected_as_missing_required(project):
    """剥完以后这一行没有任何有效内容（只给了 quotes 的人物）：按缺必填拒收，不静默丢成空节点。"""
    ops, source = project
    raw = draft()
    del raw["nodes"][0]["name"]
    raw["nodes"][0]["quotes"] = [TEXT[:4]]
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "staged"
    assert ("nodes", 0, "name") in paths(result)
    assert not ops.graph_snapshot()["nodes"]
    # 被剥掉的 quotes 仍然报一声：程序确实忽略了它，没有假装没看见。
    assert notices(result, field="quotes", section="nodes")


def test_folded_info_caps_the_index_list_at_twelve(project):
    """同类丢弃太多时不铺行：count 是处数，indexes 最多列 12 个。"""
    ops, source = project
    raw = draft()
    raw["nodes"] += [{"type": "person", "name": f"路人{i}", "quotes": [TEXT[:4]]} for i in range(15)]
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "accepted", result
    folded = notices(result, field="quotes", section="nodes")
    assert len(folded) == 1, result
    assert folded[0]["count"] == 15
    assert folded[0]["indexes"] == list(range(6, 18))
