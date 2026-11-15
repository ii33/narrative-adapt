"""「状态型 vs 累积型」两类属性（`U-08`）：累积型属性的每次提及都并列生效。

判据只有一句：**这个属性的旧值会不会被新值取代？** 会取代是状态型（默认，后写覆盖
前值）；不会取代、多个值可同时成立是累积型（定义标 `accumulate: true`）。这里用
「同一人物外貌被多次提及」这个现场把两类行为都钉住，不涉及任何属性名单。
"""
from __future__ import annotations

import copy

import pytest
from agent_fixtures import project as project

from narrative.property_core import reduce_changes

ORDER = {"初见": 0, "改造": 1, "如今": 2}


def definition(**extra):
    return {"kind": "property_definition", "id": "外貌", "value_type": "string", "cardinality": "one", **extra}


def change(identifier, event_id, op, value=None, *, sequence=0):
    row = {"kind": "property_change", "id": identifier, "node_id": "n1", "property_id": "外貌",
           "event_id": event_id, "sequence": sequence, "op": op}
    if op not in {"clear", "unknown"}:
        row["value"] = value
    return row


def reduce(rows, definition_row):
    return reduce_changes(rows, definition_row, {}, event_order=ORDER)


def test_two_set_mentions_of_an_accumulating_property_stay_side_by_side():
    """累积型：两次 set 不互相取代，读取列出两条并列值。"""
    rows = [change("c1", "初见", "set", "矮小黑瘦"), change("c2", "如今", "set", "一百八十公分")]
    assert reduce(rows, definition(accumulate=True))["state"] == {
        "kind": "known", "values": ["矮小黑瘦", "一百八十公分"]}


def test_set_and_add_are_the_same_move_for_an_accumulating_property():
    rows = [change("c1", "初见", "set", "矮小黑瘦"), change("c2", "改造", "add", "改造后变高")]
    assert reduce(rows, definition(accumulate=True))["state"]["values"] == ["矮小黑瘦", "改造后变高"]


def test_two_mentions_in_the_same_event_merge_without_a_conflict():
    """同一事件内多条也合并：累积型没有「谁取代谁」，不报 STATE_CONFLICT。"""
    rows = [change("c1", "初见", "set", "矮小黑瘦", sequence=3),
            change("c2", "初见", "set", "黑瘦", sequence=3)]
    result = reduce(rows, definition(accumulate=True))
    assert result["state"]["values"] == ["矮小黑瘦", "黑瘦"]
    assert result["diagnostics"] == []


def test_remove_drops_only_the_named_value():
    rows = [change("c1", "初见", "set", "矮小黑瘦"), change("c2", "如今", "set", "一百八十公分"),
            change("c3", "如今", "remove", "矮小黑瘦")]
    assert reduce(rows, definition(accumulate=True))["state"]["values"] == ["一百八十公分"]


def test_removing_a_value_that_was_never_listed_changes_nothing():
    rows = [change("c1", "初见", "set", "矮小黑瘦"), change("c2", "如今", "remove", "并不存在的描述")]
    assert reduce(rows, definition(accumulate=True))["state"]["values"] == ["矮小黑瘦"]


def test_clear_drops_every_accumulated_value():
    rows = [change("c1", "初见", "set", "矮小黑瘦"), change("c2", "如今", "set", "一百八十公分"),
            change("c3", "如今", "clear", sequence=1)]
    assert reduce(rows, definition(accumulate=True))["state"] == {"kind": "absent"}


def test_state_property_still_lets_the_later_value_replace_the_earlier_one():
    """状态型（没有 accumulate）行为不变：后写覆盖前值。"""
    rows = [change("c1", "初见", "set", "矮小黑瘦"), change("c2", "如今", "set", "一百八十公分")]
    assert reduce(rows, definition())["state"] == {"kind": "known", "values": ["一百八十公分"]}


def test_many_state_property_still_replaces_the_whole_set():
    """既有 `many` 语义不变：没有 accumulate 时 set 仍整体替换。"""
    rows = [change("c1", "初见", "set", ["黑发", "瘦"]), change("c2", "如今", "set", ["白发"])]
    definition_row = {"kind": "property_definition", "id": "外貌", "value_type": "string", "cardinality": "many"}
    assert reduce(rows, definition_row)["state"]["values"] == ["白发"]


@pytest.mark.parametrize("bad", [1, 0, "true", None, [], {}])
def test_non_boolean_accumulate_is_rejected_atomically(project, bad):
    before = copy.deepcopy(project.graph_snapshot())
    result = project.preview_changes([], [definition(accumulate=bad)], before["revision"])
    assert not result["valid"]
    assert any(row["code"] == "DEFINITION" for row in result["diagnostics"])
    assert project.graph_snapshot() == before
    assert not project.property_records()


def test_boolean_accumulate_is_accepted_by_the_same_write_boundary(project):
    before = project.graph_snapshot()
    assert project.preview_changes([], [definition(accumulate=True)], before["revision"])["valid"]


TEXT = "那少年矮小黑瘦。三年后他改造过，如今身高一百八十公分。"


def test_two_events_each_setting_once_end_up_as_two_current_values(project):
    """端到端：两个事件各 set 一次累积型属性，读取得到两条并列值。"""
    sid = project.import_source("外貌", TEXT)
    payload = {"nodes": [
        {"type": "person", "name": "少年", "quotes": ["那少年矮小黑瘦。"]},
        {"type": "scene", "name": "初见与再见", "quotes": [TEXT]},
        {"type": "event", "name": "初见", "scene": "初见与再见", "location": "未知",
         "participants": ["少年"], "quotes": ["那少年矮小黑瘦。"]},
        {"type": "event", "name": "再见", "scene": "初见与再见", "location": "未知",
         "participants": ["少年"], "quotes": ["三年后他改造过，如今身高一百八十公分。"]},
    ], "properties": [
        {"node": "少年", "property": "外貌", "value": "矮小黑瘦", "value_type": "string",
         "accumulate": True, "category": "外貌", "op": "set", "at_event": "初见",
         "quotes": ["那少年矮小黑瘦。"]},
        {"node": "少年", "property": "外貌", "value": "一百八十公分", "value_type": "string",
         "accumulate": True, "category": "外貌", "op": "set", "at_event": "再见",
         "quotes": ["三年后他改造过，如今身高一百八十公分。"]},
    ]}
    result = project.submit_extraction(sid, payload)
    assert result["status"] == "accepted", result
    stored = next(row for row in project.property_records() if row["kind"] == "property_definition")
    assert stored["accumulate"] is True
    mentions = [row for row in project.property_records()
                if row["kind"] == "property_change" and row["property_id"] == "外貌"]
    assert len(mentions) == 2
    person = next(node["id"] for node in project.graph_snapshot()["nodes"] if node.get("name") == "少年")
    state = project.property_state(person, "外貌")["state"]
    assert state["kind"] == "known"
    assert state["values"] == ["矮小黑瘦", "一百八十公分"]


def test_record_property_change_can_declare_the_definition_accumulating(tmp_path, monkeypatch):
    """人工/工具入口也能把定义标成累积型：不然只有提取草稿能开这个开关。"""
    from narrative.operations import Operations

    text = "李强今年二十九岁，个头矮小，皮肤黝黑。"
    event_name = "李强出场"
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    ops = Operations.create_graph_project(tmp_path / "accumulate.nproj")
    source = ops.import_source("外貌", text)
    accepted = ops.submit_extraction(source, {"nodes": [
        {"type": "person", "name": "李强", "quotes": ["李强"]},
        {"type": "scene", "name": "出场", "quotes": [text]},
        {"type": "event", "name": event_name, "scene": "出场", "participants": ["李强"],
         "location": "未知", "quotes": [text]},
    ], "properties": []})
    assert accepted["status"] == "accepted", accepted
    person = next(node for node in ops.graph_snapshot()["nodes"] if node["node_type"] == "person")
    event = next(node for node in ops.graph_snapshot()["nodes"] if node["node_type"] == "event")
    source_id = ops.graph_snapshot()["sources"][0]["id"]
    first = ops.record_property_change(
        person["id"], "外貌", event["id"], "set", value=["矮小黑瘦"],
        value_type="string", cardinality="many", accumulate=True,
        expected_revision=ops.graph_project_info()["revision"], reason="合成外貌记录",
        quote=text, source_id=source_id)
    assert first["status"] == "accepted", first
    definition = next(row for row in ops.property_records()
                      if row.get("kind") == "property_definition" and row["id"] == "外貌")
    assert definition["accumulate"] is True
    second = ops.record_property_change(
        person["id"], "外貌", event["id"], "set", value=["一百八十公分"],
        expected_revision=ops.graph_project_info()["revision"], reason="合成外貌记录二",
        quote="个头矮小", source_id=source_id)
    assert second["status"] == "accepted", second
    row = next(item for item in ops.property_node_summary(person["id"])["properties"]
               if item["property_id"] == "外貌")
    assert row["state"]["values"] == ["矮小黑瘦", "一百八十公分"]
