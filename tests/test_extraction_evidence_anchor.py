"""只有事件需要定位依据：`anchor` 或逐字 `quotes` 二选一，其它节点与属性不填引文。

概念（`U-09`）：每个节点、每个属性都以事件为基础；事件是时间点，事件链就是时间轴。
所以事件是唯一自带原文位置的对象，实体在时间轴上的位置＝它最早挂靠的那个事件的位置。
这条文件守四件事：

* 事件用 `anchor{start,end}` 时，程序按偏移截取逐字原文（`evidence.quote == source[start:end]`），
  锚点越界、反向或不在本批范围都直接拒收，路径指到 `anchor`；
* 非事件节点与属性携带的 `quotes` 是多余参数：只丢弃并回一条折叠后的 `info`（`U-10`），
  不整批拒收；事件两者都没有仍然拒收；
* 没有引文的节点照常入库：实体的 `evidence`／`disclosure` 由它最早挂靠的事件派生，
  形状与逐字引文完全一致；
* 没有引文的属性照常入库：`sequence` 取 `at_event` 事件的位置，同事件内按提交顺序递增。
"""
from __future__ import annotations

import pytest

from narrative.operations import Operations

TEXT = "林岚在档案室交出铜钥匙。周宁收下钥匙离开档案室。"
FIRST, SECOND = "交出钥匙", "收下钥匙"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    ops = Operations.create_graph_project(tmp_path / "anchor.nproj")
    return ops, ops.import_source("锚点", TEXT)


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


def paths(result: dict) -> set[tuple]:
    return {tuple(row["path"]) for row in result.get("diagnostics") or result.get("problems") or []}


def nodes_by(ops) -> dict[str, dict]:
    return {node["node_type"] + node["name"]: node for node in ops.graph_snapshot()["nodes"]}


def test_event_with_only_anchor_stores_the_sliced_text(project):
    ops, source = project
    result = ops.submit_extraction(source, draft())
    assert result["status"] == "accepted", result
    event = nodes_by(ops)["event" + FIRST]
    assert event["evidence"] == {"source_id": source, "quote": TEXT}
    assert event["disclosure"] == {"source_id": source, "start": 0}
    assert event["evidence_spans"] == [{"source_id": source, "start": 0, "end": len(TEXT), "quote": TEXT}]


def test_entities_without_quotes_take_the_position_of_their_earliest_event(project):
    ops, source = project
    assert ops.submit_extraction(source, draft())["status"] == "accepted"
    nodes = nodes_by(ops)
    event = nodes["event" + FIRST]
    # 人物走参与者、地点走事件的 world 地点、物品走事件处的持有变更：三种落点都要能定位。
    for key in ("person林岚", "person周宁", "location档案室", "item铜钥匙"):
        assert nodes[key]["evidence"] == event["evidence"], key
        assert nodes[key]["disclosure"] == event["disclosure"], key


def test_later_participation_does_not_move_the_earliest_anchor(project):
    ops, source = project
    assert ops.submit_extraction(source, draft())["status"] == "accepted"
    nodes = nodes_by(ops)
    second = {"nodes": [
        {"type": "scene", "name": "离开"},
        {"type": "event", "name": SECOND, "scene": "@1", "participants": [nodes["person周宁"]["id"]],
         "location": nodes["location档案室"]["id"], "anchor": {"start": 10, "end": 18}},
    ], "properties": []}
    assert ops.submit_extraction(source, second)["status"] == "accepted", second
    assert nodes_by(ops)["person周宁"]["disclosure"]["start"] == 0


def test_event_without_anchor_and_quotes_is_rejected_at_the_anchor_slot(project):
    ops, source = project
    raw = draft()
    del raw["nodes"][5]["anchor"]
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "staged"
    assert ("nodes", 5, "anchor") in paths(result)
    assert not ops.graph_snapshot()["nodes"]


@pytest.mark.parametrize("bad", [
    {"start": 0, "end": len(TEXT) + 5},
    {"start": -1, "end": 4},
    {"start": 4, "end": 4},
    {"start": 5, "end": 2},
])
def test_bad_anchor_is_rejected_with_its_own_path(project, bad):
    ops, source = project
    raw = draft()
    raw["nodes"][5]["anchor"] = bad
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "staged"
    assert ("nodes", 5, "anchor") in paths(result)


def test_anchor_outside_the_submitted_range_is_rejected(project):
    ops, source = project
    # 本批只提交前 9 个字，锚点却指向第 10 个字之后。
    result = ops.submit_extraction(source, {"nodes": [
        {"type": "event", "name": FIRST, "participants": [], "location": "未知",
         "anchor": {"start": 10, "end": 18}}], "properties": []}, start=0, end=9)
    assert result["status"] == "staged"
    assert ("nodes", 0, "anchor") in paths(result)
    assert any("范围" in row["message"] for row in result["problems"])


def test_anchor_wins_over_quotes_and_says_so(project):
    ops, source = project
    raw = draft()
    raw["nodes"][5]["quotes"] = ["原文里没有这句话"]
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "accepted", result
    assert nodes_by(ops)["event" + FIRST]["evidence"]["quote"] == TEXT
    assert any(row.get("code") == "EXTRACTION_INFO" and "anchor" in row["message"]
               for row in result.get("info") or [])


@pytest.mark.parametrize("kind", ["person", "location", "item", "scene"])
def test_quotes_on_non_event_nodes_are_dropped_with_one_info(project, kind):
    """多余引文只丢弃并提醒（`U-10`）：这一行本身看得懂，不该为它整批废掉。"""
    ops, source = project
    raw = draft()
    index = next(position for position, row in enumerate(raw["nodes"]) if row["type"] == kind)
    raw["nodes"][index]["quotes"] = [TEXT[:9]]
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "accepted", result
    notices = [row for row in result.get("info") or []
               if row.get("path") == ["nodes"] and row.get("field") == "quotes"]
    # 同一 (section, field) 折叠成一条：count 记处数，indexes 指出是哪一行。
    assert len(notices) == 1, result
    assert (notices[0]["code"], notices[0]["count"], notices[0]["indexes"]) == ("EXTRACTION_INFO", 1, [index])
    # 引文没有进证据：这条实体的证据仍来自它最早挂靠的那个事件。
    event = nodes_by(ops)["event" + FIRST]
    stored = next(node for node in ops.graph_snapshot()["nodes"]
                  if node["name"] == raw["nodes"][index]["name"])
    assert stored["evidence_spans"] == event["evidence_spans"]


def test_property_quotes_are_dropped_with_one_info(project):
    ops, source = project
    raw = draft()
    raw["properties"][0]["quotes"] = [TEXT[:9]]
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "accepted", result
    notices = [row for row in result.get("info") or []
               if row.get("path") == ["properties"] and row.get("field") == "quotes"]
    assert len(notices) == 1, result
    assert (notices[0]["code"], notices[0]["count"], notices[0]["indexes"]) == ("EXTRACTION_INFO", 1, [0])
    # 属性不接受自带引文：证据与披露位置都取 at_event 指向的那个事件。
    change = next(record for record in ops.property_records() if record.get("kind") == "property_change")
    assert change["evidence"] == nodes_by(ops)["event" + FIRST]["evidence"]


def test_quoted_event_that_does_not_match_the_source_is_still_rejected(project):
    ops, source = project
    raw = draft()
    del raw["nodes"][5]["anchor"]
    raw["nodes"][5]["quotes"] = ["原文里没有这句话"]
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "staged"
    assert ("nodes", 5, "quotes", 0) in paths(result)


def test_property_without_quotes_is_ordered_by_its_event_and_submission_order(project):
    ops, source = project
    raw = draft()
    # 显式写地点属性（不给快捷字段），三条都没有引文。
    raw["properties"] = [
        {"node": "@6", "property": "地点", "category": "基本信息", "value": "@3",
         "value_type": "either", "scope": "world", "at_event": "@6", "op": "set"},
        {"node": "@4", "property": "持有者", "category": "未分类", "value": "@1",
         "value_type": "either", "scope": "world", "at_event": "@6", "op": "set"},
        {"node": "@1", "property": "所在位置", "category": "未分类", "value": "档案室",
         "value_type": "string", "scope": "world", "at_event": "@6", "op": "set"},
    ]
    raw["nodes"][5].pop("location")
    result = ops.submit_extraction(source, raw)
    assert result["status"] == "accepted", result
    changes = [row for row in ops.property_records() if row.get("kind") == "property_change"]
    assert len(changes) == 3
    # 事件的披露起点是 0，同事件三条无引文变更按提交顺序依次递增：序号确定、不随顺序漂移。
    assert sorted(row["sequence"] for row in changes) == [0, 1, 2]
    # 属性不填引文：它的证据与披露位置就是 at_event 事件的（形状与逐字引文一致）。
    event = nodes_by(ops)["event" + FIRST]
    assert all(row["evidence"] == event["evidence"] and row["disclosure"] == event["disclosure"]
               for row in changes)


def test_location_shortcut_reuses_the_event_anchor_text(project):
    ops, source = project
    result = ops.submit_extraction(source, draft())
    assert result["status"] == "accepted", result
    event = nodes_by(ops)["event" + FIRST]
    row = next(record for record in ops.property_records()
               if record.get("kind") == "property_change" and record.get("node_id") == event["id"]
               and record.get("property_id") == "地点")
    assert row["evidence"]["quote"] == TEXT
    assert row["disclosure"]["start"] == 0
