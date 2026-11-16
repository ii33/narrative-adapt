"""Explicit event.location expands mechanically without changing the persisted raw draft."""
from copy import deepcopy

import pytest

from narrative.operations import Operations

TEXT = '大厅的灯熄灭，会议结束。'


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    ops = Operations.create_graph_project(tmp_path / 'shortcut.nproj')
    return ops, ops.import_source('地点快捷', TEXT)


def draft(location='大厅'):
    return {'nodes': [{'type': 'scene', 'name': '会场'},
        # 原文只有厅与灯，没有出场人物：参与者键必填但内容确实为空。
        {'type': 'event', 'name': '会议结束', 'scene': '会场', 'quotes': [TEXT], 'location': location,
         'participants': []},
        {'type': 'location', 'name': '大厅'}], 'properties': []}


@pytest.mark.parametrize('location,op', [('大厅', 'set'), ('无', 'set'), ('未知', 'unknown')])
def test_explicit_shortcut_commits_self_anchored_state_and_preserves_raw(project, location, op):
    ops, sid = project
    raw = draft(location)
    original = deepcopy(raw)
    receipt = ops.submit_extraction(sid, raw)
    assert receipt['status'] == 'accepted', receipt
    assert raw == original
    entry = ops.read_extraction_submission(receipt['submission_id'], section='nodes', indices=[1])
    assert entry['entries'][0]['item'] == raw['nodes'][1]
    assert ops.read_extraction_submission(receipt['submission_id'], section='properties')['entries'] == []
    event = next(n for n in ops.graph_snapshot()['nodes'] if n['node_type'] == 'event')
    changes = [r for r in ops.property_records() if r['kind'] == 'property_change']
    assert len(changes) == 1
    change = changes[0]
    assert change['node_id'] == change['event_id'] == event['id']
    assert change['scope'] == 'world' and change['op'] == op and type(change['sequence']) is int
    if location == '大厅':
        assert change['value'] == next(n['id'] for n in ops.graph_snapshot()['nodes'] if n['node_type'] == 'location')
    elif op == 'unknown':
        assert 'value' not in change
    else:
        assert change['value'] == '无'


@pytest.mark.parametrize('invalid,fragment', [
    ('@1', '不使用 @ 索引，请写名称或正式 ID'),
    ('@99', '不使用 @ 索引，请写名称或正式 ID'),
    ('', '仅event可填非空地点引用'),
    (5, '仅event可填非空地点引用')])
def test_bad_shortcut_stages_without_facts_and_can_be_locally_repaired(project, invalid, fragment):
    ops, sid = project
    result = ops.submit_extraction(sid, draft(invalid))
    assert result['status'] == 'staged', result
    assert any(fragment in p['message'] for p in result['problems']), result['problems']
    assert not ops.graph_snapshot()['nodes'] and not ops.property_records()
    fixed = ops.patch_extraction_submission(result['submission_id'], result['submission_revision'], [
        {'op': 'set', 'section': 'nodes', 'index': 1, 'field': 'location', 'value': '未知'}], '修正地点意图')
    assert fixed['status'] == 'accepted', fixed


def test_omission_never_supplies_unknown_and_non_event_field_is_dropped_with_notice(project):
    ops, sid = project
    raw = draft()
    del raw['nodes'][1]['location']
    missing = ops.submit_extraction(sid, raw)
    assert missing['status'] == 'staged' and not ops.graph_snapshot()['nodes']
    # 地点只属于事件：写在人物行上的 location 不整批拒收，丢弃并回一条 info（U-10）。
    raw = draft()
    raw['nodes'][0]['location'] = '未知'
    dropped = ops.submit_extraction(sid, raw)
    assert dropped['status'] == 'accepted', dropped
    assert any('location' in str(p.get('message')) and '丢弃' in str(p.get('message'))
               for p in dropped['problems'] + dropped.get('info', []))


@pytest.mark.parametrize('property_id', ['地点', '发生地点', 'event_location'])
def test_shortcut_plus_explicit_location_keeps_the_explicit_row(project, property_id):
    '''双写按 U-10 处理：保留模型写的地点属性，丢掉程序生成的快捷行，并回一条 info。'''
    ops, sid = project
    raw = draft()
    raw['properties'] = [{'node': '会议结束', 'at_event': '会议结束', 'property': property_id , "category": "未分类",
        'op': 'unknown', 'value_type': 'either'}]
    result = ops.submit_extraction(sid, raw)
    assert result['status'] == 'accepted', result
    assert any('快捷字段' in str(row.get('message')) for row in (result.get('info') or [])), result.get('info')
    rows = [r for r in ops.property_records() if r.get('kind') == 'property_change'
            and r.get('property_id') == property_id]
    assert len(rows) == 1 and rows[0].get('op') == 'unknown'
