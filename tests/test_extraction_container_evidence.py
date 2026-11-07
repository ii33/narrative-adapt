"""Model-authored containers derive exact evidence through Operations."""
from copy import deepcopy

import pytest

from narrative.operations import Operations


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    return Operations.create_graph_project(tmp_path / 'containers.nproj')


def hierarchy(quote):
    return {'nodes': [
        {'type': 'storyline', 'name': '登记故事完结', 'children': ['@2']},
        {'type': 'big_event', 'name': '办理登记', 'children': ['@3']},
        {'type': 'scene', 'name': '登记处办理', 'children': ['@4']},
        {'type': 'event', 'name': '登记完成', 'quotes': [quote], 'location': '未知'},
    ], 'properties': []}


def test_all_upper_levels_derive_without_model_quotes_and_close_at_child_end(project):
    quote = '甲完成登记。'
    source = project.import_source('登记', quote + '无关尾注。')
    raw = hierarchy(quote)
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
    raw = hierarchy(quote)
    for row in raw['nodes'][:3]:
        row.pop('children')
    raw['nodes'][1]['storyline'] = '@1'
    raw['nodes'][2]['big_event'] = '@2'
    raw['nodes'][3]['scene'] = '@3'
    result = project.submit_extraction(source, raw)
    assert result['status'] == 'accepted', result
    graph = project.graph_snapshot()
    nodes = {n['node_type']: n for n in graph['nodes']}
    assert nodes['scene']['events'] == [nodes['event']['id']]
    assert nodes['big_event']['events'] == [nodes['scene']['id']]
    assert nodes['storyline']['events'] == [nodes['big_event']['id']]


def test_model_upper_quotes_are_not_independent_evidence_or_validation_requirements(project):
    quote = '丙完成登记。'
    source = project.import_source('登记', quote)
    raw = hierarchy(quote)
    for row in raw['nodes'][:3]:
        row['quotes'] = ['模型不应填写的概括而非原文']
    result = project.submit_extraction(source, raw)
    assert result['status'] == 'accepted', result
    for n in project.graph_snapshot()['nodes']:
        assert all(s['quote'] == quote for s in n['evidence_spans'])


def test_existing_children_outside_submission_range_and_source_are_referenced(project):
    first = project.import_source('首篇', '甲完成登记。')
    seed = hierarchy('甲完成登记。')
    seed['nodes'] = seed['nodes'][2:]
    seed['nodes'][0]['children'] = ['@2']
    accepted = project.submit_extraction(first, seed)
    assert accepted['status'] == 'accepted', accepted
    scene = next(n for n in project.graph_snapshot()['nodes'] if n['node_type'] == 'scene')
    second = project.import_source('后记', '后记中只归并此前已完成的故事。')
    raw = {'nodes': [
        {'type': 'big_event', 'name': '登记办理', 'children': [scene['id']]},
        {'type': 'storyline', 'name': '登记完结', 'children': ['@1']},
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
        {'type': 'scene', 'name': '场景甲', 'children': ['@2']},
        {'type': 'scene', 'name': '场景乙', 'children': ['@1']},
    ], 'properties': []})
    assert result['status'] == 'staged'
    assert any(p['path'] in (['nodes', 0, 'children', 0], ['nodes', 1, 'children', 0])
               and '成环' in p['message'] for p in result['problems'])
    assert not project.graph_snapshot()['nodes']


def test_invalid_leaf_evidence_remains_root_cause(project):
    source = project.import_source('坏证据', '正文。')
    raw = hierarchy('不存在的引文')
    result = project.submit_extraction(source, raw)
    assert result['status'] == 'staged'
    assert any(p['path'] == ['nodes', 3, 'quotes', 0] for p in result['problems'])
    assert not any(p['path'] == ['nodes', 0, 'quotes'] for p in result['problems'])


def test_missing_leaf_quotes_is_not_excused_by_ancestor_derivation(project):
    source = project.import_source('缺证据', '正文。')
    raw = hierarchy('正文。')
    raw['nodes'][3].pop('quotes')
    result = project.submit_extraction(source, raw)
    assert result['status'] == 'staged'
    assert [p['path'] for p in result['problems']] == [['nodes', 3, 'quotes']]


def test_wrong_child_type_stages_at_original_child_reference(project):
    source = project.import_source('错类型', '甲完成登记。')
    result = project.submit_extraction(source, {'nodes': [
        {'type': 'big_event', 'name': '登记', 'children': ['@2']},
        {'type': 'person', 'name': '甲', 'quotes': ['甲完成登记。']},
    ], 'properties': []})
    assert result['status'] == 'staged'
    assert any(p['path'] == ['nodes', 0, 'children', 0] for p in result['problems'])


def test_existing_event_new_source_evidence_keeps_original_container_start(project):
    first = project.import_source('首段', '甲开始办理。')
    raw = {'nodes': [
        {'type': 'scene', 'name': '持续办理'},
        {'type': 'event', 'name': '办理事项', 'scene': '@1', 'quotes': ['甲开始办理。'], 'location': '未知'},
    ], 'properties': []}
    assert project.submit_extraction(first, raw)['status'] == 'accepted'
    nodes = {n['node_type']: n for n in project.graph_snapshot()['nodes']}
    second_quote = '甲结束办理。'
    second = project.import_source('续段', second_quote + '尾注。')
    result = project.submit_extraction(second, {'nodes': [
        {'type': 'scene', 'name': '持续办理', 'existing_id': nodes['scene']['id']},
        {'type': 'event', 'name': '办理事项', 'existing_id': nodes['event']['id'],
         'scene': '@1', 'quotes': [second_quote]},
        {'type': 'big_event', 'name': '办理阶段', 'children': ['@1']},
        {'type': 'storyline', 'name': '办理故事', 'children': ['@3']},
    ], 'properties': []})
    assert result['status'] == 'accepted', result
    current = {n['node_type']: n for n in project.graph_snapshot()['nodes']}
    assert current['scene']['disclosure'] == {'source_id': first, 'start': 0}
    assert current['big_event']['disclosure'] == {'source_id': first, 'start': 0}
    assert [s['source_id'] for s in current['storyline']['evidence_spans']] == [first, second]
    assert current['storyline']['completion'] == {'source_id': second, 'start': len(second_quote)}


def test_same_name_unquoted_containers_with_different_children_remain_distinct(project):
    source = project.import_source('同名场景', '甲办理第一件事。乙办理第二件事。')
    result = project.submit_extraction(source, {'nodes': [
        {'type': 'scene', 'name': '办公室'},
        {'type': 'event', 'name': '第一件事', 'scene': '@1', 'quotes': ['甲办理第一件事。'], 'location': '未知'},
        {'type': 'scene', 'name': '办公室'},
        {'type': 'event', 'name': '第二件事', 'scene': '@3', 'quotes': ['乙办理第二件事。'], 'location': '未知'},
    ], 'properties': []})
    assert result['status'] == 'accepted', result
    scenes = [n for n in project.graph_snapshot()['nodes'] if n['node_type'] == 'scene']
    assert len(scenes) == 2
    assert scenes[0]['events'] != scenes[1]['events']
    assert scenes[0]['evidence_spans'] != scenes[1]['evidence_spans']
