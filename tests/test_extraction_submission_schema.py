"""Published extraction drafts cannot be confused with stored graph objects."""
import json

import pytest
from agent_fixtures import TEXT, draft_result
from agent_fixtures import project as project

from narrative.tool_api import TOOLS


def current_draft():
    draft = draft_result()
    for node in draft['nodes']:
        if node['type'] == 'event':
            node['location'] = '未知'
        elif node['type'] in {'scene', 'big_event', 'storyline'}:
            node.pop('quotes', None)
    return draft


def test_graph_schema_publishes_same_complete_draft_contract_as_tools(project):
    stored = project.get_schema()
    contract = stored["extraction_submission"]
    assert contract["input_schema"] == TOOLS["submit_extraction"].schema() == TOOLS["preview_extraction"].schema()
    result = contract["input_schema"]["properties"]["result"]
    assert result["required"] == ["nodes", "properties"]
    assert result == contract["result_schema"]
    assert not result["additionalProperties"]
    assert "start" in contract["input_schema"]["properties"] and "start" not in result["properties"]
    assert "identity_note" in stored["fields"]["node"]["optional"]
    assert "identity_note" not in result["properties"]["nodes"]["items"]["properties"]
    assert not result["properties"]["nodes"]["items"]["additionalProperties"]


@pytest.mark.parametrize("mistake", ["storage_field", "nested_range"])
def test_storage_only_fields_are_dropped_instead_of_failing_the_batch(project, mistake):
    """未知字段只丢弃并回一条 info（`U-10`）：一处多余不该让整批作废。"""
    sid = project.import_source("合同边界", TEXT)
    draft = current_draft()
    if mistake == "storage_field":
        draft["nodes"][0]["identity_note"] = "此字段属于存储节点"
        expected_path, expected_field = ['nodes'], 'identity_note'
    else:
        draft["start"] = 0
        expected_path, expected_field = ['start'], 'start'
    result = project.submit_extraction(sid, draft)
    assert result['status'] == 'accepted', result
    notices = [row for row in result.get('info') or []
               if row.get('path') == expected_path and row.get('field') == expected_field]
    assert len(notices) == 1, result
    assert notices[0]['code'] == 'EXTRACTION_INFO' and notices[0]['count'] == 1
    # 丢弃是真丢弃：草稿里那份存储字段的值没进图（`start` 作为批次参数也不受草稿里的同名键影响）。
    stored = project.graph_snapshot()['nodes'] + project.property_records()
    assert '此字段属于存储节点' not in json.dumps(stored, ensure_ascii=False)
    assert {'甲', '乙'} <= {node['name'] for node in project.graph_snapshot()['nodes']}


def test_semantic_batches_reuse_previous_nodes_and_close_later_interval(project):
    first, second = "甲在门口等乙。", "乙来到门口，两人一起离开。"
    sid = project.import_source("按语境增量", first + second)
    result = project.submit_extraction(sid, {"nodes": [
        {"type": "person", "name": "甲"},
        {"type": "person", "name": "乙"},
        {"type": "location", "name": "门口"},
        {"type": "scene", "name": "门口等候与汇合"},
        {"type": "event", "name": "等候", "scene": "@4", "participants": ["@1"],
         "location": "@3", "quotes": [first]}],
        "properties": [{"node": "@1", "property": "等候对象"  , "category": "未分类", "value": "@2", "value_type": "node", "op": "set", "at_event": "@5"}]},
        start=0, end=len(first))
    assert result["status"] == "accepted"
    ids = result["node_ids"]
    record = next(row for row in project.property_records()
                  if row.get("kind") == "property_change" and row.get('property_id') == '等候对象')
    result = project.submit_extraction(sid, {"nodes": [
        {"type": "event", "name": "汇合离开", "scene": ids["@4"],
         "location": ids['@3'], "participants": [ids["@1"], ids["@2"]], "quotes": [second]}],
        "properties": [{"node": ids["@1"], "property": "等候对象"  , "category": "未分类", "op": "clear", "at_event": "@1"}]},
        start=len(first), end=len(first + second))
    assert result["status"] == "accepted"
    nodes = project.graph_snapshot()["nodes"]
    assert len(nodes) == 6 and {node["id"] for node in nodes if node["node_type"] == "person"} == {ids["@1"], ids["@2"]}
    assert project.property_state(ids["@1"], "等候对象")["state"] == {"kind": "absent"}
    assert next(row for row in project.property_records() if row["id"] == record["id"])["op"] == "set"
