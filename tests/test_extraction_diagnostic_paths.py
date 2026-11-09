"""Extraction errors preserve exact raw slots and explain rejected references."""
from copy import deepcopy

import pytest

from narrative.operations import Operations


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    ops = Operations.create_graph_project(tmp_path / 'diagnostics.nproj')
    text = '林岚在会议室宣布会议结束。'
    return ops, ops.import_source('逐字段诊断', text), text


def draft(text):
    return {'nodes': [
        {'type': 'person', 'name': '林岚', 'quotes': ['林岚']},
        {'type': 'location', 'name': '会议室', 'quotes': ['会议室']},
        {'type': 'scene', 'name': '会议', 'quotes': [text]},
        {'type': 'event', 'name': '会议结束', 'scene': '@3', 'participants': ['@1'],
         'location': '@2', 'quotes': [text]}], 'properties': []}


def test_missing_array_and_all_missing_node_quotes_are_reported_together(project):
    ops, source, text = project
    raw = draft(text)
    del raw['properties']
    del raw['nodes'][0]['quotes']
    del raw['nodes'][1]['quotes']
    original = deepcopy(raw)
    result = ops.submit_extraction(source, raw)
    assert result['status'] == 'staged'
    paths = {tuple(row['path']) for row in result['diagnostics']}
    assert {('properties',), ('nodes', 0, 'quotes'), ('nodes', 1, 'quotes')} <= paths
    assert all(row['message'] for row in result['diagnostics'])
    assert raw == original
    assert not ops.graph_snapshot()['nodes']


def test_missing_node_evidence_and_property_value_collected_together(project):
    ops, source, text = project
    raw = draft(text)
    del raw['nodes'][0]['quotes']
    raw['properties'] = [{'node': '@1', 'at_event': '@4', 'property': '安排',
                          'op': 'set', 'value_type': 'string', 'quotes': [text]}]
    result = ops.submit_extraction(source, raw)
    assert {tuple(row['path']) for row in result['diagnostics']} >= {
        ('nodes', 0, 'quotes'), ('properties', 0, 'value')}


def test_invalid_location_evidence_explains_reference_root_cause(project):
    ops, source, text = project
    raw = draft(text)
    raw['nodes'][1]['quotes'] = ['原文没有的地点']
    result = ops.submit_extraction(source, raw)
    assert result['status'] == 'staged'
    root = [row for row in result['diagnostics'] if row.get('path') == ['nodes', 1, 'quotes', 0]]
    linked = [row for row in result['diagnostics'] if row.get('path') == ['nodes', 3, 'location']]
    assert root and linked
    assert any(['nodes', 1, 'quotes', 0] in row.get('related_paths', []) for row in linked)
    assert any('引用目标未通过校验' in row['message'] and '引文' in row['message'] for row in linked)
    assert not ops.graph_snapshot()['nodes']


def test_quote_position_preserves_blank_raw_array_slot(project):
    ops, source, text = project
    raw = draft(text)
    raw['nodes'][1]['quotes'] = ['会议室', '原文没有的地点']
    result = ops.preview_extraction(source, raw)
    assert any(row.get('path') == ['nodes', 1, 'quotes', 1] for row in result['diagnostics'])


def test_references_report_exact_participant_array_position(project):
    ops, source, text = project
    raw = draft(text)
    raw['nodes'][3]['participants'] = ['@1', '@99']
    result = ops.submit_extraction(source, raw)
    assert any(row.get('path') == ['nodes', 3, 'participants', 1]
               and '@99' in row['message'] for row in result['diagnostics'])


@pytest.mark.parametrize('kind,name,label', [('location', '会议室', '地点'), ('item', '钥匙', '物品')])
def test_non_subject_entities_are_rejected_as_participants_with_a_reason(project, kind, name, label):
    """参与者的语义是"参与事件的主体"：地点与物品各有自己的属性族，不能顶替。"""
    ops, source, text = project
    raw = draft(text)
    raw['nodes'].append({'type': kind, 'name': name, 'quotes': [text]})
    raw['nodes'][3]['participants'] = ['@1', '@5']
    result = ops.submit_extraction(source, raw)
    assert result['status'] == 'staged'
    problem = next(row for row in result['diagnostics']
                   if row.get('path') == ['nodes', 3, 'participants', 1])
    assert problem['code'] == 'EXTRACTION_CONTENT'
    assert f'{label}节点' in problem['message'] and '人物或群体' in problem['message']
    assert '持有者属性' in problem['message']
    assert not ops.graph_snapshot()['nodes']


def test_group_is_a_valid_event_participant(project):
    ops, source, text = project
    raw = draft(text)
    raw['nodes'].append({'type': 'group', 'name': '与会者', 'quotes': ['宣布']})
    raw['nodes'][3]['participants'] = ['@1', '@5']
    result = ops.submit_extraction(source, raw)
    assert result['status'] == 'accepted', result
    nodes = {n['name']: n for n in ops.graph_snapshot()['nodes']}
    event = nodes['会议结束']
    assert set(event['participants']) == {nodes['林岚']['id'], nodes['与会者']['id']}


def test_preview_preserves_related_paths_metadata(project):
    ops, source, text = project
    raw = draft(text)
    raw['nodes'][1]['quotes'] = ['不存在的逐字引文']
    result = ops.preview_extraction(source, raw)
    assert not result['valid']
    assert any(row.get('path') == ['nodes', 3, 'location'] and row.get('related_paths')
               for row in result['diagnostics'])

@pytest.mark.parametrize('section,index,field,value,expected', [
    ('nodes', 0, 'extra', 'x', ['nodes', 0, 'extra']),
    ('nodes', 0, 'scene', '@3', ['nodes', 0, 'scene']),
    ('nodes', 0, 'closing_quote', '林岚', ['nodes', 0, 'closing_quote']),
    ('nodes', 0, 'location', '未知', ['nodes', 0, 'location']),
    ('properties', 0, 'sequence', 1, ['properties', 0, 'sequence']),
])
def test_schema_errors_report_the_original_field(project, section, index, field, value, expected):
    ops, source, text = project
    raw = draft(text)
    raw['properties'] = [{'node': '@1', 'at_event': '@4', 'property': '安排', 'value': '结束',
                          'op': 'set', 'value_type': 'string', 'quotes': [text]}]
    raw[section][index][field] = value
    result = ops.submit_extraction(source, raw)
    assert any(row.get('path') == expected for row in result['diagnostics'])
    assert not ops.graph_snapshot()['nodes']


def test_root_and_remarks_errors_are_independently_collected(project):
    ops, source, text = project
    raw = draft(text) | {'extra_root': 1, 'uncertainties': [False], 'exclusions': 'not-an-array'}
    result = ops.submit_extraction(source, raw)
    assert {tuple(row['path']) for row in result['diagnostics']} >= {
        ('extra_root',), ('uncertainties', 0), ('exclusions',)}


def test_invalid_op_object_has_structured_error_instead_of_type_error(project):
    ops, source, text = project
    raw = draft(text)
    raw['properties'] = [{'node': '@1', 'at_event': '@4', 'property': '安排',
                          'op': {'value': 'set'}, 'value_type': 'string', 'quotes': [text]}]
    result = ops.submit_extraction(source, raw)
    assert any(row.get('path') == ['properties', 0, 'op'] for row in result['diagnostics'])
