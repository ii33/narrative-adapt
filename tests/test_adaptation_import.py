"""Selected extraction imports must not expand to a source's neighboring graph."""
from __future__ import annotations

import copy

import pytest
from agent_fixtures import project as project

TEXT = "甲在屋内换上青衣。甲来到桥边等候。甲换上白衣。丙在远处独自休息。"
QUOTES = ["甲在屋内换上青衣。", "甲来到桥边等候。", "甲换上白衣。", "丙在远处独自休息。"]


def setup(project, *, item=False):
    sid = project.import_source("选择导入合成", TEXT)
    nodes = [
        {"type": "person", "name": "甲"},
        {"type": "person", "name": "丙"},
        {"type": "location", "name": "屋内"},
        {"type": "location", "name": "桥边"},
        {"type": "location", "name": "远处"},
        {"type": "big_event", "name": "等候"},
    ]
    for index, quote in enumerate(QUOTES):
        event = {"type": "event", "name": f"事件{index}", "scene": f"场景{index}",
                 "participants": ["甲" if index < 3 else "丙"], "quotes": [quote]}
        if index in (0, 2):
            # Explicit world location state; events 1/3 carry it through properties below.
            event["location"] = "未知"
        nodes += [{"type": "scene", "name": f"场景{index}", "big_event": "等候"},
                  event]
    properties = [
        {"node": "甲", "property": "服装", "category": "未分类", "value": "青衣", "value_type": "string", "at_event": "事件0"},
        {"node": "甲", "property": "服装", "category": "未分类", "value": "白衣", "value_type": "string", "at_event": "事件2"},
        {"node": "事件1", "property": "地点", "category": "未分类", "value": "桥边", "value_type": "node", "at_event": "事件1"},
        {"node": "事件3", "property": "地点", "category": "未分类", "value": "远处", "value_type": "node", "at_event": "事件3"},
    ]
    if item:
        nodes.append({"type": "item", "name": "纸条"})
        properties.extend([
            # 纸条的持有状态记在它自己身上（物品属性自由命名），由下面那条非提取写入
            # 把它拉进导入范围：事件类节点的属性名受五层白名单约束（`U-12`），
            # 「事件涉及纸条」不能再写成事件侧的自由属性行。
            {"node": "纸条", "property": "持有者", "category": "未分类", "value": "甲", "value_type": "either", "scope": "world",
             "at_event": "事件0"},
            {"node": "纸条", "property": "持有者", "category": "未分类", "value": "无", "value_type": "either", "scope": "world",
             "at_event": "事件2"},
        ])
    for row in properties:
        row["op"]="set"
    result = project.submit_extraction(sid, {"nodes": nodes, "properties": properties})
    assert result["status"] == "accepted", result
    ids = {n["name"]: n["id"] for n in project.graph_snapshot()["nodes"]}
    if item:
        # 参与者域已收窄为人物/群体，物品不能顶替参与者；这条引用是人工/程序侧写入
        # （不走提取草稿），用来表达「这个事件涉及纸条」并把物品拉进导入闭包。
        applied = project.apply_changes([], [
            {"kind": "property_definition", "id": "涉及物品", "value_type": "node", "cardinality": "one"},
            {"kind": "property_change", "id": "PC涉及物品1", "node_id": ids["事件1"], "property_id": "涉及物品",
             "op": "set", "value": ids["纸条"], "event_id": ids["事件1"], "sequence": 1,
             "category": "未分类"},
        ], project.graph_project_info()["revision"], "合成：这个事件涉及纸条")
        assert applied["status"] == "accepted", applied
    return {n["name"]: n["id"] for n in project.graph_snapshot()["nodes"]}


def test_candidates_are_hierarchical_read_only_source_moments(project):
    ids = setup(project)
    before = project.adaptation_snapshot()
    candidates = project.import_adaptation_candidates()
    rows = {r["id"]: r for r in candidates}
    assert ids["甲"] not in rows
    assert rows[ids["事件1"]]["parent_id"] == ids["场景1"]
    assert rows[ids["场景1"]]["parent_id"] == ids["等候"]
    assert rows[ids["事件1"]]["depth"] == 2
    assert rows[ids["场景1"]]["children"] == [ids["事件1"]]
    assert [r["id"] for r in candidates if r["node_type"] == "event"] == [ids[f"事件{i}"] for i in range(4)]
    assert project.adaptation_snapshot() == before


def test_selected_point_imports_effective_state_boundaries_and_location_only(project):
    ids = setup(project)
    extraction = copy.deepcopy(project.graph_snapshot())
    records = copy.deepcopy(project.property_records())
    state = project.import_adaptation([ids["事件1"]])
    imported = {n["id"]: n for n in state["nodes"]}
    assert ids["事件1"] in imported
    assert ids["事件0"] not in imported and ids["事件2"] not in imported
    assert {row["id"] for row in state["state_anchors"]} == {ids["事件0"]}
    assert ids["事件3"] not in imported and ids["场景3"] not in imported
    assert ids["丙"] not in imported and ids["远处"] not in imported
    assert ids["桥边"] in imported
    assert ids["屋内"] not in imported
    assert imported[ids["等候"]]["events"] == [ids["场景1"]]
    coat = [p for p in state["properties"] if p.get("property_id") == "服装"]
    assert len(coat) == 1 and coat[0]["value"] == "青衣"
    assert coat[0]["event_id"] == ids["事件0"] and coat[0]["op"] == "set"
    assert all("end_event_id" not in row for row in coat)
    assert state["import_summary"]["imported_moment_ids"] == [ids["事件1"]]
    assert project.check_adaptation()["ok"]
    assert project.graph_snapshot() == extraction
    assert project.property_records() == records


def test_container_selection_includes_descendants_without_siblings(project):
    ids = setup(project)
    state = project.import_adaptation([ids["场景3"]])
    imported = {n["id"] for n in state["nodes"]}
    assert imported == {ids["等候"], ids["场景3"], ids["事件3"], ids["丙"], ids["远处"]}
    assert set(state["import_summary"]["imported_moment_ids"]) == {ids["场景3"], ids["事件3"]}


def test_reimport_is_idempotent_preserves_local_changes_and_records_source_conflicts(project):
    ids = setup(project)
    state = project.import_adaptation([ids["事件1"]])
    before_nodes, before_values = state["nodes"], state["properties"]
    state = project.import_adaptation([ids["事件1"], ids["事件1"]])
    assert state["nodes"] == before_nodes and state["properties"] == before_values
    state = project.edit_adaptation_node(ids["事件1"], {"name": "改编桥边"})
    row = next(p for p in state["properties"] if p.get("property_id") == "服装")
    project.set_adaptation_property(ids["甲"], "服装", row["event_id"], "set", value="红衣", record_id=row["id"])
    source_node = copy.deepcopy(next(n for n in project.graph_snapshot()["nodes"] if n["id"] == ids["事件1"]))
    source_row = copy.deepcopy(next(p for p in project.property_records() if p["id"] == row["id"]))
    source_node["name"], source_row["value"] = "原文桥边", "蓝衣"
    assert project.apply_changes([source_node], [source_row], project.graph_project_info()["revision"], reason="合成源勘误")["status"] == "accepted"
    state = project.import_adaptation([ids["事件1"]])
    assert next(n for n in state["nodes"] if n["id"] == ids["事件1"])["name"] == "改编桥边"
    assert next(p for p in state["properties"] if p["id"] == row["id"])["value"] == "红衣"
    assert {(c["id"], c["field"]) for c in state["conflicts"]} == {(ids["事件1"], "name"), (row["id"], "value")}
    prior_conflicts = state["conflicts"]
    state = project.import_adaptation([ids["事件3"]])
    assert state["conflicts"] == prior_conflicts
    assert next(n for n in state["nodes"] if n["id"] == ids["等候"])["events"] == [ids["场景1"], ids["场景3"]]
    assert project.check_adaptation()["ok"]


def test_multi_select_and_item_current_holder_are_atomic_and_complete(project):
    ids = setup(project, item=True)
    state = project.import_adaptation([ids["事件1"], ids["事件3"]])
    holders = [p for p in state["properties"] if p.get("node_id") == ids["纸条"]]
    assert {p["value"] for p in holders} == {ids["甲"], "无"}
    assert project.check_adaptation()["ok"]
    before = project.adaptation_snapshot()
    with pytest.raises(ValueError, match="修订"):
        project.import_adaptation([ids["事件1"]], expected_revision=0)
    with pytest.raises(ValueError, match="时间点"):
        project.import_adaptation([ids["甲"]])
    with pytest.raises(ValueError, match="时间点"):
        project.import_adaptation([])
    assert project.adaptation_snapshot() == before
