"""Optional attribution does not pretend unknown scope is known-unused."""
import copy
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('optional_usage_qa', scripts / 'accept_optional_screenplay_usage.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample():
    fixture = {'event_ids': ['E1', 'E2'], 'timeline_id': 'local-script'}
    document = {'segments': [{'units': [{'id': 'unit-1', 'kind': 'scene', 'enabled': True,
        'plan': {'source_event_ids': []}, 'source_reference_text': '',
        'blocks': [{'type': 'action', 'payload': {'text': '林岚核对申请获准的记录。'}}]}]}],
        'event_usage': {'status': 'completed', 'timeline_id': 'local-script', 'known': False,
                        'used_event_ids': [], 'unused_event_ids': ['E2'], 'unknown_event_ids': ['E1']}}
    return document, fixture, [{'kind': 'scene', 'text': '清晨档案室'}, {'kind': 'action', 'text': '核对。'}], {'text': '清晨档案室\n林岚核对。'}


def test_partial_unknown_and_known_unused_are_distinct(acceptance):
    assert all(acceptance.assess_usage(*sample()).values())


@pytest.mark.parametrize('status', ['running', 'cancelled', 'incomplete', 'not_generated', 'unknown'])
def test_noncompleted_task_does_not_pass_usage_display_gate(acceptance, status):
    document, fixture, elements, outputs = sample()
    document['event_usage']['status'] = status
    assert not acceptance.assess_usage(document, fixture, elements, outputs)['completed_persistent_generation_status']


def test_missing_binding_must_not_be_counted_as_used(acceptance):
    document, fixture, elements, outputs = sample()
    document['event_usage']['used_event_ids'] = ['E1']
    assert not acceptance.assess_usage(document, fixture, elements, outputs)['unbound_scope_is_unknown_not_used']


def test_unknown_event_must_not_also_be_unused(acceptance):
    document, fixture, elements, outputs = sample()
    document['event_usage']['unused_event_ids'] = ['E1', 'E2']
    assert not acceptance.assess_usage(document, fixture, elements, outputs)['unbound_scope_is_unknown_not_used']


def test_binding_cannot_be_inserted_to_make_fixture_known(acceptance):
    document, fixture, elements, outputs = sample()
    document['segments'][0]['units'][0]['plan']['source_event_ids'] = ['E1']
    assert not acceptance.assess_usage(document, fixture, elements, outputs)['optional_sources_remain_empty']


@pytest.mark.parametrize('plan', [{'summary': '概要'}, {'summary': '概要', 'source_event_ids': []}])
def test_omitted_or_empty_source_array_both_mean_no_binding(acceptance, plan):
    assert acceptance.raw_calls_leave_sources_unbound([[{'plan': plan}]])


def test_raw_binding_or_missing_call_cannot_be_accepted_as_optional_omission(acceptance):
    assert not acceptance.raw_calls_leave_sources_unbound([[{'plan': {'source_event_ids': ['E1']}}]])
    assert not acceptance.raw_calls_leave_sources_unbound([])


@pytest.mark.parametrize('mutation', ['body', 'timeline', 'annotation', 'warning'])
def test_checker_rejects_missing_body_or_wrong_or_leaked_display_data(acceptance, mutation):
    document, fixture, elements, outputs = sample()
    key = {'body': 'one_scene_has_real_body', 'timeline': 'coverage_uses_current_local_timeline',
           'annotation': 'no_missing_source_notes_or_body_annotations', 'warning': 'no_missing_source_notes_or_body_annotations'}[mutation]
    if mutation == 'body':
        document['segments'][0]['units'][0]['blocks'] = []
    elif mutation == 'timeline':
        document['event_usage']['timeline_id'] = 'other-timeline'
    elif mutation == 'annotation':
        elements.append({'kind': 'source_reference', 'text': '来源'})
    else:
        outputs['text'] += '\n尚未标注'
    assert not acceptance.assess_usage(document, fixture, elements, outputs)[key]


def test_seed_has_two_timeline_events_but_only_first_event_in_segment(acceptance, tmp_path, monkeypatch):
    from narrative.operations import Operations
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    ops = Operations.create_graph_project(tmp_path / 'isolated.nproj')
    fixture = acceptance.seed(ops)
    document = ops.read_screenplay(fixture['screenplay_id'])
    first, second = fixture['event_ids']
    assert {n['id'] for n in document['timeline']['nodes'] if n.get('node_type') == 'event'} == {first, second}
    assert document['segments'][0]['span']['input']['source_event_ids'] == [first]
    assert document['event_usage']['unknown_event_ids'] == [first]
    assert document['event_usage']['unused_event_ids'] == [second]
    assert document['event_usage']['status'] == 'not_generated'


def test_task_requests_no_binding_without_dictating_body_content(acceptance):
    assert '省略plan.source_event_ids' in acceptance.TASK
    assert '一个简短动作正文块' in acceptance.TASK
    assert '夜间车站事件' in acceptance.TASK
    before = copy.deepcopy(sample())
    acceptance.assess_usage(*before)
    assert before == sample()
