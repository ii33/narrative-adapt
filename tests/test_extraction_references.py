"""Typed extraction references retain raw slot identity and existing-node semantics."""
from __future__ import annotations

import copy

import pytest
from agent_fixtures import project as project

from narrative.extraction import parse_nodes

TEXT = '甲先走。甲再停。'


def draft():
    return {'nodes': [
        {'type': 'person', 'name': '甲'},
        {'type': 'scene', 'name': '行走停步'},
        {'type': 'event', "location": "未知", 'name': '行走', 'scene': '行走停步', 'participants': ['甲'], 'quotes': ['甲先走。']},
        {'type': 'event', "location": "未知", 'name': '停步', 'scene': '行走停步', 'participants': ['甲'], 'quotes': ['甲再停。']},
    ], 'properties': []}


def test_original_indices_survive_sorting_and_rejected_nodes():
    # 被拒收的行仍然是「原文数组的第几个」：坏行是事件，缺 participants 是真正
    # 「看不懂」的输入（`I-08`；引文定位不到已经只丢引文 + info，`U-11`），
    # 它被拒之后后面那行的原始槽位标签不变。
    payload = {'nodes': [{'type': 'event', 'name': '坏事件', 'quotes': ['甲先走。']},
                         {'type': 'event', "location": "未知", 'name': '后发生', 'participants': [],
                          'quotes': ['甲再停。']},
                         {'type': 'scene', 'name': '先披露'}], 'properties': []}
    parsed = parse_nodes(payload, TEXT)
    assert [(row['name'], row['reference']) for row in parsed['nodes']] == [('先披露', '@3'), ('后发生', '@2')]
    assert {tuple(row['path']) for row in parsed['rejected']} == {('nodes', 0, 'participants')}


def test_node_owner_event_target_and_existing_definition_type_are_preserved(project):
    # 事件类的属性名受五层白名单约束（`U-12`），所以这条引用类型用例把主体放在
    # 人物上（人物属性自由命名），目标仍然是一个事件节点。
    sid = project.import_source('事件引用', TEXT)
    payload = draft()
    payload['properties'] = [{'node': '甲', 'property': '对应过程', "category": "未分类" , 'value': '停步', 'value_type': 'node',
                              'op': 'set', 'at_event': '行走', 'related_nodes': ['停步']}]
    original = copy.deepcopy(payload)
    result = project.submit_extraction(sid, payload)
    assert result['status'] == 'accepted', result
    assert payload == original
    first, second = result['node_ids']['甲'], result['node_ids']['停步']
    row = next(row for row in project.property_records() if row['kind'] == 'property_change' and row['property_id'] == '对应过程')
    assert (row['node_id'], row['value'], row['event_id']) == (first, second, result['node_ids']['行走'])
    assert row['related_nodes'] == [second]
    follow = {'nodes': [], 'properties': [{'node': first, 'property': '对应过程', "category": "未分类"  , 'value': second,
                                          'op': 'set', 'at_event': result['node_ids']['停步']}]}
    assert project.submit_extraction(sid, follow)['status'] == 'accepted'
    values = [row for row in project.property_records() if row['kind'] == 'property_change' and row['property_id'] == '对应过程']
    assert len(values) == 2 and all(row['value'] == second for row in values)


def test_node_selector_expiry_resolves_local_target_to_formal_id(project):
    sid = project.import_source('指向失效', TEXT)
    payload = draft()
    payload['properties'] = [{'node': '甲', 'property': '对应过程', "category": "未分类" , 'value': '行走', 'value_type': 'node',
                              'op': 'set', 'at_event': '行走'}]
    payload['properties'].append({'node': '甲', 'property': '对应过程' , "category": "未分类", 'op': 'clear', 'at_event': '停步'})
    result = project.submit_extraction(sid, payload)
    assert result['status'] == 'accepted', result
    row = next(row for row in project.property_records() if row['kind'] == 'property_change' and row['property_id'] == '对应过程')
    assert project.property_state(result['node_ids']['甲'], '对应过程')['state'] == {'kind': 'absent'}
    assert any(r.get('op') == 'clear' and r['event_id'] == result['node_ids']['停步'] for r in project.property_records())
    assert row['value'] == result['node_ids']['行走']


def test_duplicate_identity_conflicting_participants_is_rejected_without_writes(project):
    sid = project.import_source('重复身份冲突', TEXT)
    payload = draft()
    payload['nodes'].append(payload['nodes'][2] | {'participants': []})
    before = project.graph_snapshot()
    result = project.preview_extraction(sid, payload)
    assert not result['valid']
    assert any('不同身份' in row['message'] for row in result['diagnostics'])
    assert project.graph_snapshot() == before


@pytest.mark.parametrize('field', ['participants', 'related_nodes'])
def test_unknown_explicit_reference_never_silently_disappears(project, field):
    sid = project.import_source('显式错误引用', TEXT)
    payload = draft()
    if field == 'participants':
        payload['nodes'][2][field] = ['不存在的人']
    else:
        payload['properties'] = [{'node': '甲', 'property': '状态' , "category": "未分类", 'value': '行走', 'op': 'set', 'at_event': '行走',
                                  'related_nodes': ['不存在的人']}]
    result = project.preview_extraction(sid, payload)
    assert not result['valid']
    assert any('找不到引用「不存在的人」' in row['message'] for row in result['diagnostics'])
    assert not project.property_records()
