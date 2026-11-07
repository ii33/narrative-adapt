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


def draft(location='@3'):
    return {'nodes': [{'type': 'scene', 'name': '会场', 'quotes': [TEXT]},
        {'type': 'event', 'name': '会议结束', 'scene': '@1', 'quotes': [TEXT], 'location': location},
        {'type': 'location', 'name': '大厅', 'quotes': ['大厅']}], 'properties': []}


@pytest.mark.parametrize('location,op', [('@3', 'set'), ('无', 'set'), ('未知', 'unknown')])
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
    if location == '@3':
        assert change['value'] == next(n['id'] for n in ops.graph_snapshot()['nodes'] if n['node_type'] == 'location')
    elif op == 'unknown':
        assert 'value' not in change
    else:
        assert change['value'] == '无'


@pytest.mark.parametrize('invalid', ['@1', '@99', '', 5])
def test_bad_shortcut_stages_without_facts_and_can_be_locally_repaired(project, invalid):
    ops, sid = project
    result = ops.submit_extraction(sid, draft(invalid))
    assert result['status'] == 'staged', result
    assert not ops.graph_snapshot()['nodes'] and not ops.property_records()
    fixed = ops.patch_extraction_submission(result['submission_id'], result['submission_revision'], [
        {'op': 'set', 'section': 'nodes', 'index': 1, 'field': 'location', 'value': '未知'}], '修正地点意图')
    assert fixed['status'] == 'accepted', fixed


def test_omission_never_supplies_unknown_and_non_event_field_is_rejected(project):
    ops, sid = project
    raw = draft()
    del raw['nodes'][1]['location']
    missing = ops.submit_extraction(sid, raw)
    assert missing['status'] == 'staged' and not ops.graph_snapshot()['nodes']
    raw = draft()
    raw['nodes'][0]['location'] = '未知'
    invalid = ops.submit_extraction(sid, raw)
    assert invalid['status'] == 'staged'
    assert any('nodes[0]' in p['message'] for p in invalid['problems'])


@pytest.mark.parametrize('property_id', ['地点', '发生地点', 'event_location'])
def test_shortcut_plus_explicit_location_is_never_silently_selected(project, property_id):
    ops, sid = project
    raw = draft()
    raw['properties'] = [{'node': '@2', 'at_event': '@2', 'property': property_id,
        'op': 'unknown', 'value_type': 'either', 'quotes': [TEXT]}]
    result = ops.submit_extraction(sid, raw)
    assert result['status'] == 'staged', result
    assert any('nodes[1].location' in p['message'] and '双写' in p['message'] for p in result['problems'])
    assert not ops.graph_snapshot()['nodes']
    fixed = ops.patch_extraction_submission(result['submission_id'], result['submission_revision'], [
        {'op': 'remove', 'section': 'nodes', 'index': 1, 'field': 'location'}], '使用显式地点属性')
    assert fixed['status'] == 'accepted', fixed
