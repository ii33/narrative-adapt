"""Fixture-specific semantic grouping checks, never product count quotas."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('balanced_event_qa', scripts / 'accept_balanced_event_granularity.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(module, identifier, quote):
    start = module.TEXT.index(quote)
    return {'id': identifier, 'node_type': 'event', 'name': identifier,
        'evidence_spans': [{'source_id': 'source-meeting', 'start': start,
            'end': start + len(quote), 'quote': quote}]}


def graph(module):
    return {'nodes': [{'id': 'scene-meeting', 'node_type': 'scene'}]
        + [event(module, f'event-unit-{i}', unit) for i, unit in enumerate(module.UNITS)]}


def test_three_complete_units_inside_one_scene_pass(acceptance):
    checks, evidence = acceptance.assess_events(graph(acceptance), 'source-meeting')
    assert all(checks.values())
    assert evidence['unit_event_ids'] == [['event-unit-0'], ['event-unit-1'], ['event-unit-2']]


def test_entire_meeting_merged_fails_even_with_all_outcomes(acceptance):
    data = {'nodes': [{'id': 'scene-meeting', 'node_type': 'scene'},
        event(acceptance, 'event-whole-scene', acceptance.TEXT)]}
    checks, _ = acceptance.assess_events(data, 'source-meeting')
    assert checks['exact_event_evidence']
    assert not checks['distinct_narrative_units_not_merged']


def test_extra_gesture_fragment_fails(acceptance):
    data = graph(acceptance)
    data['nodes'].append(event(acceptance, 'event-gesture', '摊在桌上'))
    checks, _ = acceptance.assess_events(data, 'source-meeting')
    assert not checks['process_details_not_fragmented']


def test_same_event_count_with_missing_unit_and_fragment_does_not_pass(acceptance):
    data = graph(acceptance)
    data['nodes'][-1] = event(acceptance, 'event-gesture', '摊在桌上')
    checks, _ = acceptance.assess_events(data, 'source-meeting')
    assert not checks['distinct_narrative_units_not_merged']
    assert not checks['process_details_not_fragmented']


@pytest.mark.parametrize('mutation', ['quote', 'source', 'bounds', 'missing'])
def test_nonexact_or_unbound_evidence_fails(acceptance, mutation):
    data = graph(acceptance)
    span = data['nodes'][1]['evidence_spans'][0]
    if mutation == 'quote':
        span['quote'] = '改写的引文'
    elif mutation == 'source':
        span['source_id'] = 'other-source'
    elif mutation == 'bounds':
        span['start'] = -1
    else:
        data['nodes'][1]['evidence_spans'] = []
    checks, _ = acceptance.assess_events(data, 'source-meeting')
    assert not checks['exact_event_evidence']


def test_artificial_scene_splitting_fails(acceptance):
    data = graph(acceptance)
    data['nodes'].append({'id': 'scene-artificial', 'node_type': 'scene'})
    checks, _ = acceptance.assess_events(data, 'source-meeting')
    assert not checks['continuous_meeting_not_split_into_scenes']


def test_task_has_no_expected_count_or_unit_answers(acceptance):
    assert '三个' not in acceptance.TASK and '3个' not in acceptance.TASK
    assert not any(pivot in acceptance.TASK for pivot in acceptance.PIVOTS)
