"""Offline location QA checks; no model output is synthesized as real success."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('event_locations_acceptance', scripts / 'accept_event_locations.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def state_fixture(value='location-room', state_kind='known', *, anchor='event-check', scope='world'):
    graph = {'nodes': [{'kind': 'node', 'id': 'event-check', 'node_type': 'event'},
        {'kind': 'node', 'id': 'location-room', 'node_type': 'location'}]}
    raw = {'kind': 'property_change', 'id': 'change-check', 'node_id': 'event-check',
        'event_id': anchor, 'property_id': '地点', 'scope': scope, 'op': 'set', 'value': value}
    definition = {'kind': 'property_definition', 'id': '地点', 'value_type': 'either'}
    derived = raw | {'state_kind': state_kind, 'active_change_ids': ['change-check']}
    return graph, [definition, raw], [derived]


@pytest.mark.parametrize(('value', 'kind', 'expected'), [
    ('location-room', 'known', 'location'), ('无', 'known', 'none'),
    ('未知', 'known', 'unknown'), (None, 'unknown', 'unknown')])
def test_explicit_three_states_require_own_world_fact(acceptance, value, kind, expected):
    checks, states = acceptance.assess_location_rows(*state_fixture(value, kind))
    assert all(checks.values()) and states == {'event-check': expected}


@pytest.mark.parametrize('case', ['missing', 'other_anchor', 'claim', 'absent', 'wrong_type', 'no_contributor'])
def test_missing_or_nonworld_or_other_event_never_becomes_unknown(acceptance, case):
    graph, records, derived = state_fixture()
    if case == 'missing':
        derived = []
    elif case == 'other_anchor':
        records[-1]['event_id'] = 'other-event'
    elif case == 'claim':
        derived[0]['scope'] = 'claim'
    elif case == 'absent':
        derived[0]['state_kind'] = 'absent'
    elif case == 'wrong_type':
        records[0]['value_type'] = 'string'
    else:
        derived[0]['active_change_ids'] = []
    checks, _ = acceptance.assess_location_rows(graph, records, derived)
    assert not all(checks.values())


def test_missing_location_staged_then_one_append_patch_atomically_commits(acceptance, tmp_path, monkeypatch):
    from narrative.operations import Operations
    from narrative.temporal_state import projected_changes
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    ops = Operations.create_graph_project(tmp_path / 'locations.nproj')
    source = ops.import_source('地点暂存离线样本', acceptance.TEXT)
    first, second = acceptance.TEXT.split('\n')
    draft = {'nodes': [
        {'type': 'location', 'name': '档案室', 'quotes': ['档案室']},
        {'type': 'scene', 'name': '名单暂停后的调查', 'quotes': [acceptance.TEXT]},
        {'type': 'event', 'name': '暂停名单并核查授权', 'scene': '@2', 'quotes': [first]},
        {'type': 'event', 'name': '收到冒名消息另行调查', 'scene': '@2', 'quotes': [second]}],
        'properties': [{'node': '@3', 'property': '地点', 'op': 'set', 'at_event': '@3',
            'value': '@1', 'value_type': 'either', 'scope': 'world', 'quotes': [first]}]}
    before = ops.graph_snapshot(), ops.property_records()
    staged = ops.submit_extraction(source, draft)
    assert staged['status'] == 'staged' and staged['problems']
    assert (ops.graph_snapshot(), ops.property_records()) == before
    result = ops.patch_extraction_submission(staged['submission_id'], staged['submission_revision'],
        [{'op': 'append', 'section': 'properties', 'value': {'node': '@4', 'property': '地点',
            'op': 'unknown', 'at_event': '@4', 'value_type': 'either', 'scope': 'world', 'quotes': [second]}}],
        '补充未交代地点的明确未知状态')
    assert result['status'] == 'accepted' and result['submission_status'] == 'committed'
    graph, records = ops.graph_snapshot(), ops.property_records()
    checks, states = acceptance.assess_location_rows(graph, records, projected_changes(graph, records))
    assert all(checks.values()) and set(states.values()) == {'location', 'unknown'}
    assert ops.source(source)['text'] == acceptance.TEXT
