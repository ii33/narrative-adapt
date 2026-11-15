"""同一属性：后一次提及让前一次失效（`U-08`）；并列只在同时成立或不同方面。

用户裁定：「矮小黝黑」和后来的外貌**不应该并列，应该失效**——那是同一属性的值被取代，
不是两个值并存。所以这里钉住的是状态语义：同一属性的后一次提及覆盖前值，前面那条只留在
`history_changes` 里且 `is_current == False`，读取当前值只有新的那条。

并列只有两个合法来源，本文件各钉一处：① 同一事件点里**同时成立**的多个值装进一条
`many` 记录的多值数组；② 同一类别下的**不同方面**是不同属性名，本来就互不覆盖（已有用例：
`tests/test_property_category_contract.py::test_a_category_holds_several_properties_side_by_side`，
这里不重复）。没有第三种来源：定义上不再有任何「累积」开关，带旧字段的定义会被当未知字段拒收。

不涉及任何属性名单：这里出现「外貌」只是复现现场的最短例子。
"""
from __future__ import annotations

import copy

import pytest
from agent_fixtures import project as project

from narrative.property_core import reduce_changes

ORDER = {"初见": 0, "改造": 1, "如今": 2}


def definition(**extra):
    return {"kind": "property_definition", "id": "外貌", "value_type": "string", "cardinality": "one", **extra}


def many_definition(**extra):
    return {"kind": "property_definition", "id": "外貌", "value_type": "string", "cardinality": "many", **extra}


def change(identifier, event_id, op, value=None, *, sequence=0):
    row = {"kind": "property_change", "id": identifier, "node_id": "n1", "property_id": "外貌",
           "event_id": event_id, "sequence": sequence, "op": op}
    if op not in {"clear", "unknown"}:
        row["value"] = value
    return row


def reduce(rows, definition_row):
    return reduce_changes(rows, definition_row, {}, event_order=ORDER)


def test_a_later_mention_replaces_the_earlier_value():
    """同一属性两次 set：读取当前值只有后一条。"""
    rows = [change("c1", "初见", "set", "矮小黑瘦"), change("c2", "如今", "set", "一百八十公分")]
    result = reduce(rows, definition())
    assert result["state"] == {"kind": "known", "values": ["一百八十公分"]}
    assert result["active_change_ids"] == ["c2"]


def test_the_replaced_mention_stays_in_history_and_is_not_current():
    """前一条不会被删：它在 history_changes 里，但不是当前值。"""
    rows = [change("c1", "初见", "set", "矮小黑瘦"), change("c2", "如今", "set", "一百八十公分")]
    result = reduce(rows, definition())
    assert [row["id"] for row in result["history_changes"]] == ["c1", "c2"]
    assert "c1" not in result["active_change_ids"]
    assert result["diagnostics"] == []


def test_values_true_at_the_same_event_point_are_listed_side_by_side():
    """并列的第一处来源：同一事件点同时成立的多项，一条 `many` 记录装多值数组。"""
    rows = [change("c1", "初见", "set", ["黑发", "瘦"])]
    assert reduce(rows, many_definition())["state"] == {"kind": "known", "values": ["黑发", "瘦"]}


def test_add_merges_within_the_same_event_point():
    """同一事件点里 `set` 之后 `add` 追加也是并列，且两条都算当前值。"""
    rows = [change("c1", "初见", "set", ["黑发"]), change("c2", "初见", "add", ["瘦"], sequence=1)]
    result = reduce(rows, many_definition())
    assert result["state"] == {"kind": "known", "values": ["黑发", "瘦"]}
    assert result["active_change_ids"] == ["c1", "c2"]


def test_a_many_set_at_a_later_event_replaces_the_whole_set():
    """`many` 的 set 仍是整体替换：并列只属于同一个事件点。"""
    rows = [change("c1", "初见", "set", ["黑发", "瘦"]), change("c2", "如今", "set", ["白发"])]
    assert reduce(rows, many_definition())["state"]["values"] == ["白发"]


def test_remove_drops_only_the_named_value():
    rows = [change("c1", "初见", "set", ["黑发", "瘦"]), change("c2", "如今", "remove", ["瘦"], sequence=1)]
    assert reduce(rows, many_definition())["state"]["values"] == ["黑发"]


def test_clear_invalidates_the_whole_group():
    """`clear` 让整组失效：不是留一条「已清除」的并列值。"""
    rows = [change("c1", "初见", "set", "矮小黑瘦"), change("c2", "如今", "set", "一百八十公分"),
            change("c3", "如今", "clear", sequence=1)]
    assert reduce(rows, definition())["state"] == {"kind": "absent"}


@pytest.mark.parametrize("bad", [True, False])
def test_a_definition_can_no_longer_declare_the_retracted_flag(project, bad):
    """撤回不留兼容分支：定义上带旧的累积字段就是未知字段，整批原子拒收。"""
    before = copy.deepcopy(project.graph_snapshot())
    result = project.preview_changes([], [definition(accumulate=bad)], before["revision"])
    assert not result["valid"]
    assert any(row["code"] == "FORMAT" and "accumulate" in row["message"] for row in result["diagnostics"])
    assert project.graph_snapshot() == before
    assert not project.property_records()


TEXT = "那少年矮小黑瘦。三年后他改造过，如今身高一百八十公分。"


def _two_mention_payload(sid, *, value_type="string", value_of=("矮小黑瘦", "一百八十公分")):
    return {"nodes": [
        {"type": "person", "name": "少年"},
        {"type": "scene", "name": "初见与再见"},
        {"type": "event", "name": "初见", "scene": "初见与再见", "location": "未知",
         "participants": ["少年"], "quotes": ["那少年矮小黑瘦。"]},
        {"type": "event", "name": "再见", "scene": "初见与再见", "location": "未知",
         "participants": ["少年"], "quotes": ["三年后他改造过，如今身高一百八十公分。"]},
    ], "properties": [
        {"node": "少年", "property": "外貌", "value": value_of[0], "value_type": value_type,
         "category": "外貌", "op": "set", "at_event": "初见"},
        {"node": "少年", "property": "外貌", "value": value_of[1], "value_type": value_type,
         "category": "外貌", "op": "set", "at_event": "再见"},
    ]}


def test_two_events_each_setting_once_leave_only_the_later_value_current(project):
    """端到端：两个事件各 set 一次 → 当前值只有后一条，前一条留在历史且标记为非当前。"""
    sid = project.import_source("外貌", TEXT)
    result = project.submit_extraction(sid, _two_mention_payload(sid))
    assert result["status"] == "accepted", result
    person = next(node["id"] for node in project.graph_snapshot()["nodes"] if node.get("name") == "少年")
    state = project.property_state(person, "外貌")["state"]
    assert state == {"kind": "known", "values": ["一百八十公分"]}
    row = next(item for item in project.property_node_summary(person)["properties"]
               if item["property_id"] == "外貌")
    assert [record["value"] for record in row["history_changes"]] == ["矮小黑瘦", "一百八十公分"]
    assert [record["is_current"] for record in row["records"]] == [False, True]


def test_one_event_with_a_many_record_lists_both_values_as_current(project):
    """并列的合法来源：同一事件点同时成立，装成一条 `many` 记录的多值数组。"""
    sid = project.import_source("外貌", TEXT)
    payload = {"nodes": [
        {"type": "person", "name": "少年"},
        {"type": "scene", "name": "初见与再见"},
        {"type": "event", "name": "初见", "scene": "初见与再见", "location": "未知",
         "participants": ["少年"], "quotes": ["那少年矮小黑瘦。"]},
    ], "properties": [
        {"node": "少年", "property": "外貌", "value": ["矮小", "黝黑"], "value_type": "string",
         "cardinality": "many", "category": "外貌", "op": "set", "at_event": "初见"},
    ]}
    accepted = project.submit_extraction(sid, payload)
    assert accepted["status"] == "accepted", accepted
    person = next(node["id"] for node in project.graph_snapshot()["nodes"] if node.get("name") == "少年")
    assert project.property_state(person, "外貌")["state"] == {"kind": "known", "values": ["矮小", "黝黑"]}
