"""An event must explicitly own a world location state, independently of its scene."""
import pytest

from narrative.event_location import event_location_issues


def node(identifier, node_type, **extra):
    return {'kind': 'node', 'id': identifier, 'node_type': node_type, 'name': identifier,
            'context_id': 'canon', **extra}


NODES = [node('E1', 'event'), node('E2', 'event'), node('S1', 'scene'),
         node('L1', 'location'), node('L2', 'location'), node('P1', 'person')]


def records(op='set', value='L1', **extra):
    definition = {'kind': 'property_definition', 'id': '地点', 'value_type': 'either', 'cardinality': 'one'}
    change = {'kind': 'property_change', 'id': 'PC1', 'node_id': 'E1', 'property_id': '地点',
              'event_id': 'E1', 'op': op, 'sequence': 0, 'scope': 'world', 'context_id': 'canon', **extra}
    if op in {'set', 'add', 'remove'}:
        change['value'] = value
    return [definition, change]


def issues(rows, nodes=None):
    return [r for r in event_location_issues(nodes or NODES, rows) if r['object_id'] == 'E1']


@pytest.mark.parametrize('op,value', [('set', 'L1'), ('set', '无'), ('set', '未知'), ('unknown', None)])
def test_explicit_reference_none_and_unknown_are_valid(op, value):
    assert issues(records(op, value)) == []


@pytest.mark.parametrize('value', ['无', '未知'])
def test_literal_string_sentinels_are_explicit_location_states(value):
    rows = records(value=value)
    rows[0]['value_type'] = 'string'
    assert issues(rows) == []


def test_literal_place_name_does_not_replace_a_location_reference():
    rows = records(value='档案室')
    rows[0]['value_type'] = 'string'
    assert 'EVENT_LOCATION_VALUE' in {r['code'] for r in issues(rows)}


@pytest.mark.parametrize('change', [[], records('clear'), records('remove'),
    records(scope='cognition', holder_id='P1'), records(scope='claim', holder_id='P1'),
    records(node_id='S1'), records(context_id='other')])
def test_missing_or_other_scope_never_becomes_unknown(change):
    assert 'EVENT_LOCATION_REQUIRED' in {r['code'] for r in issues(change)}


@pytest.mark.parametrize('value', ['P1', 'missing', '', [], True])
def test_invalid_location_values_are_rejected(value):
    assert 'EVENT_LOCATION_VALUE' in {r['code'] for r in issues(records(value=value))}


@pytest.mark.parametrize('flag', ['suppressed', 'retracted'])
def test_inactive_locations_and_events(flag):
    nodes = [r | ({flag: True} if r['id'] == 'L1' else {}) for r in NODES]
    assert issues(records(), nodes)[0]['code'] == 'EVENT_LOCATION_VALUE'
    nodes = [r | ({flag: True} if r['id'] == 'E1' else {}) for r in NODES]
    assert issues([], nodes) == []


def test_anchor_conflict_multi_locations_alias_and_semantic():
    assert {r['code'] for r in issues(records(event_id='E2'))} == {
        'EVENT_LOCATION_REQUIRED', 'EVENT_LOCATION_ANCHOR'}
    rows = records()
    rows.append(rows[-1] | {'id': 'PC2', 'value': 'L2'})
    assert issues(rows)[0]['code'] == 'EVENT_LOCATION_CONFLICT'
    rows = records(value=['L1', 'L2'])
    rows[0]['cardinality'] = 'many'
    assert issues(rows) == []
    for name in ['发生地点', 'location_of_event', 'event_location', '自由命名']:
        rows = records()
        rows[0].update(id=name, semantic='event_location')
        rows[1]['property_id'] = name
        assert issues(rows) == []


def test_later_event_cannot_clear_or_supply_another_event_location():
    rows = records()
    rows.append(rows[-1] | {'id': 'PC2', 'node_id': 'E2', 'event_id': 'E2', 'op': 'unknown'})
    assert event_location_issues(NODES, rows) == []
    rows.append(rows[1] | {'id': 'PC3', 'event_id': 'E2', 'op': 'clear', 'sequence': 1})
    assert issues(rows)[0]['code'] == 'EVENT_LOCATION_ANCHOR'


def test_diagnostics_keep_precise_owner_and_record_ids():
    finding = issues(records(value='P1'))[0]
    assert finding['object_id'] == 'E1' and finding['record_ids'] == ['PC1']
    assert finding['property_id'] == '地点' and finding['severity'] == 'error'


def create_valid_project(tmp_path, monkeypatch):
    from narrative.graph_models import Node
    from narrative.operations import Operations

    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    ops = Operations.create_graph_project(tmp_path / 'location.nproj')
    text = '甲在大厅宣布会议结束。'
    sid = ops.import_source('地点', text)
    def obj(identifier, kind, **extra):
        return Node.model_validate({'kind': 'node', 'id': identifier, 'node_type': kind,
            'name': identifier, 'context_id': 'canon', 'disclosure': {'source_id': sid, 'start': 0},
            'evidence': {'source_id': sid, 'quote': text},
            'evidence_spans': [{'source_id': sid, 'start': 0, 'end': len(text), 'quote': text}], **extra}).model_dump()
    objects = [obj('S1', 'scene', events=['E1']), obj('E1', 'event'), obj('L1', 'location')]
    result = ops.apply_changes(objects, [], ops.graph_project_info()['revision'], '地点约束测试')
    assert result['status'] == 'staged', result
    assert ops.graph_snapshot()['nodes'] == []
    assert any(p.get('code') == 'EVENT_LOCATION_REQUIRED' for p in result['problems'])
    changes = records()
    changes[-1].update(disclosure={'source_id': sid, 'start': 0}, evidence={'source_id': sid, 'quote': text})
    repaired = ops.patch_content_submission(result['submission_id'], result['submission_revision'], [
        {'op': 'append', 'path': ['property_records'], 'value': row} for row in changes], reason='补同事件地点')
    assert repaired['status'] == 'accepted', repaired
    assert not [i for i in ops.read_project_state()['diagnostics'] if i['code'].startswith('EVENT_LOCATION')]

    return ops, sid, text


def test_operations_missing_location_is_staged_and_local_patch_commits(tmp_path, monkeypatch):
    create_valid_project(tmp_path, monkeypatch)


@pytest.mark.parametrize('op', ['add', 'remove'])
def test_collection_without_baseline_is_not_explicit_unknown(op):
    rows = records(op, ['L1'])
    rows[0]['cardinality'] = 'many'
    assert issues(rows)[0]['code'] == 'EVENT_LOCATION_VALUE'
    rows.insert(1, rows[1] | {'id': 'PC0', 'sequence': 0, 'op': 'unknown'})
    rows[-1]['sequence'] = 1
    assert issues(rows) == []


@pytest.mark.parametrize('op', ['add', 'remove'])
def test_collection_without_baseline_is_staged_via_operations(tmp_path, monkeypatch, op):
    ops, sid, text = create_valid_project(tmp_path, monkeypatch)
    before = ops.property_records()
    revision = ops.graph_project_info()['revision']
    # A new recognized alias has no explicit baseline: a partial collection is not explicit unknown.
    rows = records(op, ['L1'])
    rows[0].update(id='发生地点', cardinality='many')
    rows[1].update(id='PC-no-baseline', property_id='发生地点',
        disclosure={'source_id': sid, 'start': 0}, evidence={'source_id': sid, 'quote': text})
    result = ops.apply_changes([], rows, revision, '不应把不完整集合当未知')
    assert result['status'] == 'staged', result
    assert any(p.get('code') == 'EVENT_LOCATION_VALUE' for p in result['problems'])
    assert ops.property_records() == before
    assert ops.graph_project_info()['revision'] == revision


@pytest.mark.parametrize('operation', ['clear', 'correct_reference', 'delete_change', 'delete_location'])
def test_all_property_entrypoints_preserve_formal_location_on_invalid_change(tmp_path, monkeypatch, operation):
    ops, _sid, _text = create_valid_project(tmp_path, monkeypatch)
    before = ops.graph_snapshot(), ops.property_records()
    revision = ops.graph_project_info()['revision']
    if operation == 'clear':
        result = ops.record_property_change('E1', '地点', 'E1', 'clear',
            expected_revision=revision, reason='清空测试')
        assert result['status'] == 'staged', result
    elif operation == 'correct_reference':
        result = ops.correct_property('PC1', {'value': 'S1'}, '错类型地点测试', expected_revision=revision)
        assert result['status'] == 'staged', result
    elif operation == 'delete_change':
        with pytest.raises(ValueError):
            ops.delete_property_records(['PC1'], '删除地点状态测试', expected_revision=revision)
    else:
        result = ops.apply_changes([next(r for r in before[0]['nodes'] if r['id'] == 'L1') | {'retracted': True}],
            [], revision, '撤销地点测试')
        assert result['status'] == 'staged', result
    assert (ops.graph_snapshot(), ops.property_records()) == before
    assert ops.graph_project_info()['revision'] == revision


@pytest.mark.parametrize('op', ['add', 'remove'])
def test_explicit_unknown_does_not_mask_invalid_active_collection_reference(op):
    rows = records('unknown')
    rows[0]['cardinality'] = 'many'
    rows.append(rows[-1] | {'id': 'PC-bad', 'sequence': 1, 'op': op, 'value': ['P1']})
    finding = issues(rows)[0]
    assert finding['code'] == 'EVENT_LOCATION_VALUE'
    assert finding['record_ids'] == ['PC-bad']
