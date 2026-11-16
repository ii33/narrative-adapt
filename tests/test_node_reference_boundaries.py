"""Black-box Operations checks for typed IDs, draft slots and literal boundaries."""
from __future__ import annotations

import copy
import json

import pytest
from agent_fixtures import project as project


def values(ops):
    """草稿自己写的属性变更；事件自动补的「地点」行（op=unknown，无 value）不算。"""
    return [row for row in ops.property_records()
            if row["kind"] == "property_change" and row.get("property_id") != "地点"]


def submit_unchanged_input(ops, source_id, payload):
    original=copy.deepcopy(payload)
    checked=ops.preview_extraction(source_id,payload)
    assert checked["valid"], checked
    assert "node_ids" not in checked
    assert payload == original
    result=ops.submit_extraction(source_id,payload)
    assert result["status"] == "accepted", result
    assert payload == original
    return result


def test_identical_names_of_different_types_resolve_by_field_type_and_report_ambiguity(project):
    """同名跨类型：带类型约束的字段按字段筛；无约束的字段只能报歧义，不猜。"""
    text="名叫渡口的人在渡口等候。"
    sid=project.import_source("同名跨类型",text)
    payload={"nodes":[
        {"type":"person","name":"渡口"},
        {"type":"location","name":"渡口"},
        {"type":"scene","name":"渡口等候"},
        {"type":"event", "location": "未知","name":"在渡口等候","scene":"渡口等候","participants":["渡口"],"quotes":[text]},
    ],"properties":[]}
    submit_unchanged_input(project,sid,payload)
    nodes=project.graph_snapshot()["nodes"]
    person=next(row for row in nodes if row["node_type"] == "person")
    location=next(row for row in nodes if row["node_type"] == "location")
    event=next(row for row in nodes if row["node_type"] == "event")
    scene=next(row for row in nodes if row["node_type"] == "scene")
    assert person["name"] == location["name"] == "渡口"
    assert person["id"] != location["id"]
    # 参与者只要人物/群体、场景字段只要场景：类型把同名的另一个筛掉。
    assert event["participants"] == [person["id"]]
    assert set(scene["events"]) == {event["id"]}
    # 属性的 node/value/related_nodes 没有类型约束，同名跨类型时挑不出唯一一个：
    # 报错要列出候选，让模型改名或写正式 ID，而不是静默绑到数组里的某一格。
    ambiguous={"nodes":[],"properties":[{"node":"渡口","op":"set","property":"位置", "category": "未分类"  ,"value":"渡口",
                                         "value_type":"node","at_event":"在渡口等候","related_nodes":["渡口"]}]}
    checked=project.preview_extraction(sid,ambiguous)
    assert not checked["valid"], checked
    reason=json.dumps(checked,ensure_ascii=False)
    assert "有 2 个「渡口」" in reason and "人物" in reason and "地点" in reason


def test_identical_event_names_at_different_occurrences_keep_exact_at_event_slots(project):
    """同名事件必须改名区分：at_event 认得唯一名字后才锚到各自那一次。"""
    first,second="甲上午进门。","甲下午进门。"
    sid=project.import_source("两次进门",first+second)
    payload={"nodes":[
        {"type":"person","name":"甲"},
        {"type":"scene","name":"两次进门"},
        {"type":"event", "location": "未知","name":"进门","scene":"两次进门","participants":["甲"],"quotes":[first]},
        {"type":"event", "location": "未知","name":"进门","scene":"两次进门","participants":["甲"],"new_occurrence":True,"quotes":[second]},
    ],"properties":[
        {"node":"甲","op":"set","property":"进门时段"  , "category": "未分类","value":"上午","at_event":"进门"},
        {"node":"甲","op":"set","property":"进门时段", "category": "未分类","value":"下午","at_event":"进门"},
    ]}
    checked=project.preview_extraction(sid,payload)
    assert not checked["valid"], checked
    assert "有 2 个「进门」" in json.dumps(checked,ensure_ascii=False)
    fixed=copy.deepcopy(payload)
    fixed["nodes"][2]["name"]="上午进门"
    fixed["nodes"][3]["name"]="下午进门"
    fixed["properties"][0]["at_event"]="上午进门"
    fixed["properties"][1]["at_event"]="下午进门"
    submit_unchanged_input(project,sid,fixed)
    nodes=project.graph_snapshot()["nodes"]
    events=[row for row in nodes if row["node_type"] == "event"]
    assert len(events) == 2 and {row["name"] for row in events} == {"上午进门","下午进门"}
    by_start={row["disclosure"]["start"]:row["id"] for row in events}
    records={row["value"]:row for row in values(project) if row["property_id"] == "进门时段"}
    assert records["上午"]["event_id"] == by_start[0]
    assert records["下午"]["event_id"] == by_start[len(first)]
    scene=next(row for row in nodes if row["node_type"] == "scene")
    assert set(scene["events"]) == set(by_start.values())


def seed(project):
    text="甲站在门口。"
    sid=project.import_source("已有人物",text)
    draft={"nodes":[
        {"type":"person","name":"甲"},
        {"type":"scene","name":"门口等候"},
        {"type":"event", "location": "未知","name":"甲站在门口","scene":"门口等候","participants":["甲"],"quotes":[text]},
    ],"properties":[]}
    submit_unchanged_input(project,sid,draft)
    nodes=project.graph_snapshot()["nodes"]
    return sid,text,next(row["id"] for row in nodes if row["node_type"] == "person"),next(row["id"] for row in nodes if row["node_type"] == "event")


def test_string_and_any_reference_looking_literals_and_original_payload_are_not_rewritten(project):
    text="甲说@1和P1只是文字。"
    sid=project.import_source("字面值",text)
    literal={"node":"@1","event_id":"P1","nested":["@1","P1",{"holder":"@1"}]}
    payload={"nodes":[
        {"type":"person","name":"甲"},
        {"type":"scene","name":"字符说明"},
        {"type":"event", "location": "未知","name":"甲说明字符","scene":"字符说明","participants":["甲"],"quotes":[text]},
    ],"properties":[
        {"node":"甲","op":"set","property":"字面文本"  , "category": "未分类","value":"@1","value_type":"string","at_event":"甲说明字符"},
        {"node":"甲","op":"set","property":"字面编号", "category": "未分类","value":"P1","value_type":"string","at_event":"甲说明字符"},
        {"node":"甲","op":"set","property":"原始结构", "category": "未分类","value":literal,"value_type":"any","at_event":"甲说明字符"},
    ]}
    submit_unchanged_input(project,sid,payload)
    records={row["property_id"]:row for row in values(project)}
    assert records["字面文本"]["value"] == "@1"
    assert records["字面编号"]["value"] == "P1"
    assert records["原始结构"]["value"] == literal


@pytest.mark.parametrize("invalid,fragment",[("E404","正式节点ID不存在"),
                                             ("@99","不使用 @ 索引"),
                                             ("不存在的事件","找不到引用「不存在的事件」")])
def test_explicit_invalid_at_event_never_falls_back_to_the_quotation(project,invalid,fragment):
    sid,text,person,_event=seed(project)
    before=copy.deepcopy((project.graph_snapshot(),project.property_records()))
    payload={"nodes":[],"properties":[{"node":person,"op":"set","property":"位置"  , "category": "未分类","value":"门口","at_event":invalid}]}
    original=copy.deepcopy(payload)
    checked=project.preview_extraction(sid,payload)
    assert not checked["valid"], checked
    assert fragment in json.dumps(checked,ensure_ascii=False), checked
    staged=project.submit_extraction(sid,payload)
    assert staged["status"] == "staged" and staged["problems"]
    assert any(invalid in json.dumps(row,ensure_ascii=False) or
               row.get("path") == ["properties", 0, "at_event"] for row in staged["problems"])
    assert payload == original
    assert (project.graph_snapshot(),project.property_records()) == before


def test_empty_nodes_can_add_existing_id_property_without_repeating_any_node(project):
    sid,text,person,event=seed(project)
    before=copy.deepcopy(project.graph_snapshot()["nodes"])
    payload={"nodes":[],"properties":[{"node":person,"op":"set","property":"位置"  , "category": "未分类","value":"门口","at_event":event}]}
    committed=submit_unchanged_input(project,sid,payload)
    assert "node_ids" not in committed
    assert project.graph_snapshot()["nodes"] == before
    position_values=[row for row in values(project) if row["property_id"] == "位置"]
    assert len(position_values) == 1
    assert position_values[0]["node_id"] == person and position_values[0]["event_id"] == event


def test_repeated_same_identity_slots_deduplicate_to_one_formal_node(project):
    text="甲站在门口。"
    sid=project.import_source("重复声明同一人",text)
    declaration={"type":"person","name":"甲"}
    payload={"nodes":[
        copy.deepcopy(declaration),copy.deepcopy(declaration),
        {"type":"scene","name":"门口等候"},
        {"type":"event", "location": "未知","name":"甲站在门口","scene":"门口等候","participants":["甲","甲"],"quotes":[text]},
    ],"properties":[
        {"node":"甲","op":"set","property":"位置"  , "category": "未分类","value":"门口","at_event":"甲站在门口"},
        {"node":"甲","op":"set","property":"姿态", "category": "未分类","value":"站着","at_event":"甲站在门口"},
    ]}
    committed=submit_unchanged_input(project,sid,payload)
    persons=[row for row in project.graph_snapshot()["nodes"] if row["node_type"] == "person"]
    assert len(persons) == 1
    # 两行同名声明归并成一个正式节点，返回映射按名称给键（位置编号不进结果）。
    assert committed["node_ids"]["甲"] == persons[0]["id"]
    before=copy.deepcopy((project.graph_snapshot(),project.property_records()))
    original=copy.deepcopy(payload)
    repeated=project.submit_extraction(sid,payload)
    assert repeated["status"] == "unchanged"
    assert repeated["node_ids"] == committed["node_ids"]
    assert payload == original
    assert (project.graph_snapshot(),project.property_records()) == before
    records=[row for row in values(project) if row["property_id"] in {"位置","姿态"}]
    assert len(records) == 2 and {row["property_id"] for row in records} == {"位置","姿态"}
    assert {row["node_id"] for row in records} == {persons[0]["id"]}


def test_clear_event_preserves_prior_literal_batch_reference_text(project):
    first, second = '甲说@1', '随后停止标注'
    text = first+'，'+second+'。'
    sid = project.import_source('字面标记结束', text)
    payload = {'nodes': [
        {'type': 'person', 'name': '甲'},
        {'type': 'scene', 'name': '记录后停止'},
        {'type': 'event', "location": "未知", 'name': '甲记录标注', 'scene': '记录后停止', 'participants': ['甲'], 'quotes': [first]},
        {'type': 'event', "location": "未知", 'name': '停止标注', 'scene': '记录后停止', 'participants': ['甲'], 'quotes': [second]}],
        'properties': [
            {'node': '甲', 'property': '标注'  , "category": "未分类", 'op': 'set', 'value': '@1', 'value_type': 'string', 'at_event': '甲记录标注'},
            {'node': '甲', 'property': '标注' , "category": "未分类", 'op': 'clear', 'at_event': '停止标注'}]}
    submit_unchanged_input(project, sid, payload)
    nodes = project.graph_snapshot()['nodes']
    person = next(n['id'] for n in nodes if n['node_type'] == 'person')
    events = {n['name']: n['id'] for n in nodes if n['node_type'] == 'event'}
    assert next(r for r in values(project) if r['op'] == 'set')['value'] == '@1'
    assert project.property_state(person, '标注', events['甲记录标注'])['state']['values'] == ['@1']
    assert project.property_state(person, '标注')['state'] == {'kind': 'absent'}
