"""Model-authored containers derive exact evidence through Operations."""
from copy import deepcopy

import pytest

from narrative.operations import Operations


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    return Operations.create_graph_project(tmp_path / 'containers.nproj')


def hierarchy(quote, participant):
    # 事件参与者必填：此处给出原文中真正动手的登记人，按唯一名引用，
    # 使夹具在本文件内被截取/重排后依然有效。
    return {'nodes': [
        {'type': 'storyline', 'name': '登记故事完结', 'children': ['办理登记']},
        {'type': 'big_event', 'name': '办理登记', 'children': ['登记处办理']},
        {'type': 'scene', 'name': '登记处办理', 'children': ['登记完成']},
        {'type': 'event', 'name': '登记完成', 'quotes': [quote], 'location': '未知',
         'participants': [participant]},
        {'type': 'person', 'name': participant},
    ], 'properties': []}


def test_all_upper_levels_derive_without_model_quotes_and_close_at_child_end(project):
    quote = '甲完成登记。'
    source = project.import_source('登记', quote + '无关尾注。')
    raw = hierarchy(quote, '甲')
    before = deepcopy(raw)
    result = project.submit_extraction(source, raw)
    assert result['status'] == 'accepted', result
    assert raw == before
    staged = project.read_extraction_submission(result['submission_id'], section='nodes')['entries']
    assert [r['item'] for r in staged] == before['nodes']
    nodes = {n['node_type']: n for n in project.graph_snapshot()['nodes']}
    for kind in ('scene', 'big_event', 'storyline'):
        assert nodes[kind]['evidence_spans'] == nodes['event']['evidence_spans']
        assert nodes[kind]['disclosure'] == {'source_id': source, 'start': 0}
    assert nodes['storyline']['completion'] == {'source_id': source, 'start': len(quote)}


def test_reverse_parent_fields_supply_direct_children(project):
    quote = '乙完成登记。'
    source = project.import_source('登记', quote)
    raw = hierarchy(quote, '乙')
    for row in raw['nodes'][:3]:
        row.pop('children')
    raw['nodes'][1]['storyline'] = '登记故事完结'
    raw['nodes'][2]['big_event'] = '办理登记'
    raw['nodes'][3]['scene'] = '登记处办理'
    result = project.submit_extraction(source, raw)
    assert result['status'] == 'accepted', result
    graph = project.graph_snapshot()
    nodes = {n['node_type']: n for n in graph['nodes']}
    assert nodes['scene']['events'] == [nodes['event']['id']]
    assert nodes['big_event']['events'] == [nodes['scene']['id']]
    assert nodes['storyline']['events'] == [nodes['big_event']['id']]


def test_model_upper_quotes_are_dropped_and_never_become_evidence(project):
    """容器不填引文（`U-09`／`I-10`）：模型给容器写的 quotes 只丢弃并提醒（`U-10`）。

    丢弃不等于采纳：容器证据仍然只能由已验证的下级派生，模型写的那几句不会变成证据。
    """
    quote = '丙完成登记。'
    source = project.import_source('登记', quote)
    raw = hierarchy(quote, '丙')
    for row in raw['nodes'][:3]:
        row['quotes'] = ['模型不应填写的概括而非原文']
    result = project.submit_extraction(source, raw)
    assert result['status'] == 'accepted', result
    notices = [row for row in result.get('info') or []
               if row.get('path') == ['nodes'] and row.get('field') == 'quotes']
    # 三处同类丢弃折叠成一条：count 是处数，indexes 指出是哪几行。
    assert len(notices) == 1, result
    assert (notices[0]['count'], notices[0]['indexes'][:3]) == (3, [0, 1, 2])
    for n in project.graph_snapshot()['nodes']:
        assert all(s['quote'] == quote for s in n['evidence_spans'])


def test_existing_children_outside_submission_range_and_source_are_referenced(project):
    first = project.import_source('首篇', '甲完成登记。')
    seed = hierarchy('甲完成登记。', '甲')
    seed['nodes'] = seed['nodes'][2:]
    seed['nodes'][0]['children'] = ['登记完成']
    accepted = project.submit_extraction(first, seed)
    assert accepted['status'] == 'accepted', accepted
    scene = next(n for n in project.graph_snapshot()['nodes'] if n['node_type'] == 'scene')
    second = project.import_source('后记', '后记中只归并此前已完成的故事。')
    raw = {'nodes': [
        {'type': 'big_event', 'name': '登记办理', 'children': [scene['id']]},
        {'type': 'storyline', 'name': '登记完结', 'children': ['登记办理']},
    ], 'properties': []}
    result = project.submit_extraction(second, raw, 2, 5)
    assert result['status'] == 'accepted', result
    story = next(n for n in project.graph_snapshot()['nodes'] if n['node_type'] == 'storyline')
    assert story['disclosure']['source_id'] == first
    assert story['completion'] == {'source_id': first, 'start': len('甲完成登记。')}
    assert all(s['source_id'] == first for s in story['evidence_spans'])


def test_empty_upper_container_stages_at_children_path(project):
    source = project.import_source('空容器', '正文。')
    result = project.submit_extraction(source, {'nodes': [{'type': 'scene', 'name': '无成员'}], 'properties': []})
    assert result['status'] == 'staged'
    assert any(p['path'] == ['nodes', 0, 'children'] and '下级证据' in p['message'] for p in result['problems'])
    assert not project.graph_snapshot()['nodes']


def test_cycles_report_actual_child_field_without_writing(project):
    source = project.import_source('环', '正文。')
    result = project.submit_extraction(source, {'nodes': [
        {'type': 'scene', 'name': '场景甲', 'children': ['场景乙']},
        {'type': 'scene', 'name': '场景乙', 'children': ['场景甲']},
    ], 'properties': []})
    assert result['status'] == 'staged'
    assert any(p['path'] in (['nodes', 0, 'children', 0], ['nodes', 1, 'children', 0])
               and '成环' in p['message'] for p in result['problems'])
    assert not project.graph_snapshot()['nodes']


def test_invalid_leaf_evidence_remains_root_cause(project):
    """叶节点引文定位不到只丢引文（`U-11`），但容器证据只能从已验证下级派生（`I-10`）：
    整层没有证据仍然拒收，拒收原因直接说「没有可用的直属下级证据」，丢引文这件事在
    `info` 里点名到行，不当成含糊的「引文不符」。
    """
    source = project.import_source('坏证据', '正文。')
    raw = hierarchy('不存在的引文', '甲')
    result = project.submit_extraction(source, raw)
    assert result['status'] == 'staged'
    blocked = [p for p in result['problems'] if '没有可用的直属下级证据' in p['message']]
    assert blocked, result['problems']
    # 折叠成一条：场景／大事件／主要故事三层都缺证据，报在第一处并注明共几处。
    assert tuple(blocked[0]['path']) == ('nodes', 2, 'children')
    assert '3 处' in blocked[0]['message']
    # 丢引文不是错的：事件本身不再被拒（没有落在节点 3 上的 error），但在 info 里点名。
    assert not any(tuple(p['path'])[:2] == ('nodes', 3) for p in result['problems'])
    notices = [row for row in result.get('info') or [] if row.get('note') == 'dropped']
    assert [(row['count'], row['indexes']) for row in notices] == [(1, [3])]


def test_missing_leaf_locator_is_not_excused_by_ancestor_derivation(project):
    """事件没有定位依据（anchor 或 quotes）时，不能靠上层容器派生糊过去。"""
    source = project.import_source('缺证据', '正文。')
    raw = hierarchy('正文。', '甲')
    raw['nodes'][3].pop('quotes')
    result = project.submit_extraction(source, raw)
    assert result['status'] == 'staged'
    assert [p['path'] for p in result['problems']] == [['nodes', 3, 'anchor']]


def test_wrong_child_type_stages_at_original_child_reference(project):
    source = project.import_source('错类型', '甲完成登记。')
    result = project.submit_extraction(source, {'nodes': [
        {'type': 'big_event', 'name': '登记', 'children': ['甲']},
        {'type': 'person', 'name': '甲'},
    ], 'properties': []})
    assert result['status'] == 'staged'
    assert any(p['path'] == ['nodes', 0, 'children', 0] for p in result['problems'])


def test_existing_event_new_source_evidence_keeps_original_container_start(project):
    first = project.import_source('首段', '甲开始办理。')
    raw = {'nodes': [
        {'type': 'scene', 'name': '持续办理'},
        {'type': 'event', 'name': '办理事项', 'scene': '持续办理', 'quotes': ['甲开始办理。'], 'location': '未知',
         'participants': ['甲']},
        {'type': 'person', 'name': '甲'},
    ], 'properties': []}
    assert project.submit_extraction(first, raw)['status'] == 'accepted'
    nodes = {n['node_type']: n for n in project.graph_snapshot()['nodes']}
    second_quote = '甲结束办理。'
    second = project.import_source('续段', second_quote + '尾注。')
    result = project.submit_extraction(second, {'nodes': [
        {'type': 'scene', 'name': '持续办理', 'existing_id': nodes['scene']['id']},
        {'type': 'event', "location": "未知", 'name': '办理事项', 'existing_id': nodes['event']['id'],
         'scene': '持续办理', 'quotes': [second_quote], 'participants': [nodes['person']['id']]},
        {'type': 'big_event', 'name': '办理阶段', 'children': ['持续办理']},
        {'type': 'storyline', 'name': '办理故事', 'children': ['办理阶段']},
    ], 'properties': []})
    assert result['status'] == 'accepted', result
    current = {n['node_type']: n for n in project.graph_snapshot()['nodes']}
    assert current['scene']['disclosure'] == {'source_id': first, 'start': 0}
    assert current['big_event']['disclosure'] == {'source_id': first, 'start': 0}
    assert [s['source_id'] for s in current['storyline']['evidence_spans']] == [first, second]
    assert current['storyline']['completion'] == {'source_id': second, 'start': len(second_quote)}


def test_same_name_unquoted_containers_with_different_children_remain_distinct(project):
    """同名容器不能静默并成一个，也不能猜父级挂在谁身上：必须先改名区分。
    改名之后两个场景各自只挂自己的事件，证据互不串。
    """
    source = project.import_source('同名场景', '甲办理第一件事。乙办理第二件事。')
    raw = {'nodes': [
        {'type': 'scene', 'name': '办公室'},
        {'type': 'event', 'name': '第一件事', 'scene': '办公室', 'quotes': ['甲办理第一件事。'], 'location': '未知',
         'participants': ['甲']},
        {'type': 'scene', 'name': '办公室'},
        {'type': 'event', 'name': '第二件事', 'scene': '办公室', 'quotes': ['乙办理第二件事。'], 'location': '未知',
         'participants': ['乙']},
        {'type': 'person', 'name': '甲'},
        {'type': 'person', 'name': '乙'},
    ], 'properties': []}
    checked = project.preview_extraction(source, raw)
    assert not checked['valid'], checked
    assert any('有 2 个「办公室」' in row['message'] for row in checked['diagnostics']), checked
    fixed = deepcopy(raw)
    fixed['nodes'][0]['name'] = '办公室前厅'
    fixed['nodes'][1]['scene'] = '办公室前厅'
    fixed['nodes'][2]['name'] = '办公室后厅'
    fixed['nodes'][3]['scene'] = '办公室后厅'
    result = project.submit_extraction(source, fixed)
    assert result['status'] == 'accepted', result
    scenes = [n for n in project.graph_snapshot()['nodes'] if n['node_type'] == 'scene']
    assert {n['name'] for n in scenes} == {'办公室前厅', '办公室后厅'}
    assert len(scenes) == 2
    assert scenes[0]['events'] != scenes[1]['events']
    assert scenes[0]['evidence_spans'] != scenes[1]['evidence_spans']


def test_scene_without_quotes_can_declare_its_big_event(project):
    """U-09 之后场景与实体都不带引文：解析父级时不能假设 quotes[0] 存在（实跑里这曾抛 IndexError）。"""
    ops = project
    source = ops.import_source('父子', '林岚宣布会议结束。')
    draft = {'nodes': [
        {'type': 'big_event', 'name': '第一篇章'},
        {'type': 'scene', 'name': '会场', 'big_event': '第一篇章'},
        {'type': 'person', 'name': '林岚'},
        {'type': 'location', 'name': '会议室'},
        {'type': 'event', 'name': '会议结束', 'scene': '会场', 'participants': ['林岚'],
         'location': '会议室', 'quotes': ['林岚宣布会议结束。']},
    ], 'properties': []}
    result = ops.submit_extraction(source, draft)
    assert result.get('status') in {'accepted', 'staged'}, result
    assert not any(row.get('code') == 'tool_failed' for row in (result.get('problems') or []))
    scene = next((n for n in ops.graph_snapshot()['nodes'] if n['node_type'] == 'scene'
                  and n['name'] == '会场'), None)
    if result.get('status') == 'accepted':
        assert scene is not None
