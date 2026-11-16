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
        {'type': 'person', 'name': '林岚'},
        {'type': 'location', 'name': '会议室'},
        {'type': 'scene', 'name': '会议'},
        {'type': 'event', 'name': '会议结束', 'scene': '会议', 'participants': ['林岚'],
         'location': '会议室', 'quotes': [text]}], 'properties': []}


def test_missing_array_and_event_without_locator_are_reported_together(project):
    """缺数组与缺事件定位依据要在同一次提交里一起报出来（`U-09`）。"""
    ops, source, text = project
    raw = draft(text)
    del raw['properties']
    del raw['nodes'][3]['quotes']  # 事件既没有 anchor 也没有 quotes
    original = deepcopy(raw)
    result = ops.submit_extraction(source, raw)
    assert result['status'] == 'staged'
    paths = {tuple(row['path']) for row in result['diagnostics']}
    assert {('properties',), ('nodes', 3, 'anchor')} <= paths
    assert all(row['message'] for row in result['diagnostics'])
    assert raw == original
    assert not ops.graph_snapshot()['nodes']


def test_missing_event_locator_and_property_value_collected_together(project):
    ops, source, text = project
    raw = draft(text)
    del raw['nodes'][3]['quotes']
    raw['properties'] = [{'node': '林岚', 'at_event': '会议结束', 'property': '安排' , "category": "未分类",
                          'op': 'set', 'value_type': 'string'}]
    result = ops.submit_extraction(source, raw)
    assert {tuple(row['path']) for row in result['diagnostics']} >= {
        ('nodes', 3, 'anchor'), ('properties', 0, 'value')}


def test_location_shortcut_points_at_its_own_field_when_the_reference_is_wrong(project):
    """location 快捷字段引用了一个不是地点的节点：问题报在 location 上，不静默丢弃。"""
    ops, source, text = project
    raw = draft(text)
    raw['nodes'][1]['type'] = 'person'  # 会议室不再是地点
    result = ops.submit_extraction(source, raw)
    assert result['status'] == 'staged'
    linked = [row for row in result['diagnostics'] if row.get('path') == ['nodes', 3, 'location']]
    assert linked
    assert any('location' in row['message'] and 'location节点' in row['message'] for row in linked)
    assert not ops.graph_snapshot()['nodes']


def test_quote_position_preserves_blank_raw_array_slot():
    """引文路径指向**原始数组槽**：空串不参与校验，也不许让后面的下标漂移。

    定位不到原文的引文已经改成「丢弃 + info」（`U-11`），所以这里看仍然会整批拒收的
    「同一句在本批出现多处」：空串在 [1] 不占诊断，歧义那条仍报在 `quotes[2]`，并给出
    候选起点（`positions_sample`），不任取首处。
    """
    from narrative.extraction import parse_nodes
    text = '林岚在会议室宣布会议结束。'
    payload = {'nodes': [{'type': 'event', 'name': '会议结束', 'participants': [],
                          'quotes': ['会议结束', '  ', '会议']}], 'properties': []}
    parsed = parse_nodes(payload, text)
    assert [tuple(row['path']) for row in parsed['rejected']] == [('nodes', 0, 'quotes', 2)]
    assert len(parsed['rejected'][0]['positions_sample']) > 1
    assert parsed['rejected'][0]['position_basis'] == 'submission_relative'


def test_references_report_exact_participant_array_position(project):
    ops, source, text = project
    raw = draft(text)
    raw['nodes'][3]['participants'] = ['林岚', '@99']
    result = ops.submit_extraction(source, raw)
    assert any(row.get('path') == ['nodes', 3, 'participants', 1]
               and '@99' in row['message'] and '不使用 @ 索引' in row['message']
               for row in result['diagnostics'])


@pytest.mark.parametrize('kind,name,label', [('location', '会议室门口', '地点'), ('item', '钥匙', '物品')])
def test_non_subject_entities_are_rejected_as_participants_with_a_reason(project, kind, name, label):
    """参与者的语义是"参与事件的主体"：地点与物品各有自己的属性族，不能顶替。"""
    ops, source, text = project
    raw = draft(text)
    raw['nodes'].append({'type': kind, 'name': name})
    raw['nodes'][3]['participants'] = ['林岚', name]
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
    raw['nodes'].append({'type': 'group', 'name': '与会者'})
    raw['nodes'][3]['participants'] = ['林岚', '与会者']
    result = ops.submit_extraction(source, raw)
    assert result['status'] == 'accepted', result
    nodes = {n['name']: n for n in ops.graph_snapshot()['nodes']}
    event = nodes['会议结束']
    assert set(event['participants']) == {nodes['林岚']['id'], nodes['与会者']['id']}


def test_preview_reports_the_dropped_shortcut_with_its_reason(project):
    """快捷字段与显式地点属性双写：保留属性行、丢弃快捷行，并说明丢弃的是哪一条（U-10）。"""
    ops, source, text = project
    raw = draft(text)
    raw['properties'] = [{'node': '会议结束', 'property': '地点', "category": "基本信息", 'value': '会议室',
                          'value_type': 'either', 'scope': 'world', 'at_event': '会议结束', 'op': 'set'}]
    result = ops.preview_extraction(source, raw)
    assert result['valid']
    assert any('快捷字段' in str(row.get('message')) for row in (result.get('info') or [])), result.get('info')


@pytest.mark.parametrize('section,index,field,value,expected', [
    ('nodes', 0, 'scene', '会议', ['nodes', 0, 'scene']),
    # 地点只属于事件：写在人物行上是多余参数，提交时丢弃并提示（`U-10`），
    # 所以这里换成真正"看不懂"的一种——空名称。
    ('nodes', 0, 'name', '', ['nodes', 0, 'name']),
    ('properties', 0, 'op', {'value': 'set'}, ['properties', 0, 'op']),
])
def test_schema_errors_report_the_original_field(project, section, index, field, value, expected):
    ops, source, text = project
    raw = draft(text)
    raw['properties'] = [{'node': '林岚', 'at_event': '会议结束', 'property': '安排' , "category": "未分类", 'value': '结束',
                          'op': 'set', 'value_type': 'string'}]
    raw[section][index][field] = value
    result = ops.submit_extraction(source, raw)
    assert any(row.get('path') == expected for row in result['diagnostics'])
    assert not ops.graph_snapshot()['nodes']


@pytest.mark.parametrize('section,index,field,value', [
    ('nodes', 0, 'location', '未知'),
    ('nodes', 0, 'extra', 'x'),
    ('nodes', 0, 'closing_quote', '林岚'),
    ('properties', 0, 'sequence', 1),
])
def test_dropped_fields_point_at_their_section_and_row(project, section, index, field, value):
    """多余字段不再是 schema 错误（`U-10`）：折叠到 section 上，`field`/`indexes` 指出是哪一条。"""
    ops, source, text = project
    raw = draft(text)
    raw['properties'] = [{'node': '林岚', 'at_event': '会议结束', 'property': '安排' , "category": "未分类", 'value': '结束',
                          'op': 'set', 'value_type': 'string'}]
    raw[section][index][field] = value
    result = ops.submit_extraction(source, raw)
    assert result['status'] == 'accepted', result
    folded = [row for row in result['info']
              if row.get('code') == 'EXTRACTION_INFO' and row.get('path') == [section]
              and row.get('field') == field]
    assert len(folded) == 1, result
    assert folded[0]['indexes'] == [index]


def test_root_and_remarks_errors_are_independently_collected(project):
    ops, source, text = project
    raw = draft(text) | {'extra_root': 1, 'uncertainties': [False], 'exclusions': 'not-an-array'}
    result = ops.submit_extraction(source, raw)
    assert {tuple(row['path']) for row in result['diagnostics']} >= {
        ('uncertainties', 0), ('exclusions',)}
    # 顶层未知字段不再是错误：丢弃并回一条 info，同一个提交里的真错误照旧收集（`U-10`）。
    assert any(row.get('path') == ['extra_root'] for row in result['info'])


def test_invalid_op_object_has_structured_error_instead_of_type_error(project):
    ops, source, text = project
    raw = draft(text)
    raw['properties'] = [{'node': '林岚', 'at_event': '会议结束', 'property': '安排' , "category": "未分类",
                          'op': {'value': 'set'}, 'value_type': 'string'}]
    result = ops.submit_extraction(source, raw)
    assert any(row.get('path') == ['properties', 0, 'op'] for row in result['diagnostics'])
