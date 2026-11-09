"""关系结构契约：对称映射与关系类型审计。"""
from copy import deepcopy

import pytest
from agent_fixtures import TEXT, draft_result
from agent_fixtures import project as project
from test_relationship_state import setup_relations

from narrative import tool_api
from narrative.project_package import layout


def test_map_uses_explicit_symmetry_without_creating_reverse_values(project):
    setup_relations(project)
    entry = layout(project.project_info().path).entry
    before = project.property_records()
    definition = next(row for row in before if row["id"] == "朋友")
    revision = project.graph_project_info()["revision"]
    result = project.apply_changes([], [definition | {"symmetric": True}], revision, "明确该定义是对称关系")
    assert result["status"] == "accepted"
    edges = tool_api.call("relation_map", {}, project=entry)["edges"]
    assert len(edges) == 2 and all(edge["symmetric"] for edge in edges)
    assert [row for row in project.property_records() if row["kind"] == "property_value"] == [
        row for row in before if row["kind"] == "property_value"]


def test_known_relation_name_without_symmetry_is_not_guessed(project):
    setup_relations(project)
    edges = tool_api.call("relation_map", {}, project=layout(project.project_info().path).entry)["edges"]
    assert edges and all(edge["symmetric"] is False for edge in edges)


@pytest.mark.parametrize(('value_type', 'category', 'expected'), [
    ('string', '关系', True), ('any', '关系', True),
    ('string', '其他', False), ('any', '其他', False),
    ('node', '关系', False), ('node', None, False), ('either', '关系', False),
])
def test_relation_id_literal_warns_only_for_explicit_relation_category(project, value_type, category, expected):
    sid = project.import_source('合成关系原文', TEXT)
    assert project.submit_extraction(sid, draft_result())['status'] == 'accepted'
    nodes = {node['name']: node['id'] for node in project.graph_snapshot()['nodes']}
    record = {'kind': 'property_change', 'op': 'set', 'sequence': 0, 'node_id': nodes['甲'], 'property_id': '查证对象',
              'value': nodes['乙'], 'event_id': nodes['甲约好接乙'],
              'evidence': {'source_id': sid, 'quote': '甲约好接乙。'}}
    if category is not None:
        record['category'] = category
    definition = {'kind': 'property_definition', 'id': '查证对象', 'value_type': value_type, 'cardinality': 'one'}
    assert project.apply_changes([], [definition, record], project.graph_project_info()['revision'], reason='明确类型和分类')['status'] == 'accepted'
    before, rows = deepcopy(project.graph_snapshot()), deepcopy(project.property_records())
    report = project.property_audit()
    findings = [row for row in report['issues'] if row['code'] == 'relation_untyped_reference']
    assert bool(findings) == expected
    if expected:
        assert findings[0]['severity'] == 'warning'
        assert findings[0]['node_id'] == nodes['甲'] and findings[0]['record_ids']
        assert '不会显示' in findings[0]['message']
        checks = project.check_project()['issues']
        assert any(row['code'] == 'relation_untyped_reference' for row in checks)
    if value_type in {'node', 'either'}:
        from narrative.project_package import layout
        from narrative.tool_api import call
        result = call('relation_map', project=layout(project.project_info().path).entry)
        assert any(edge['kind'] == '查证对象' for edge in result['edges'])
    assert project.graph_snapshot() == before and project.property_records() == rows
