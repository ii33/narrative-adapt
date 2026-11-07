"""Verify one representative quote without chapter-boundary evidence quotas."""
import copy
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('cross_chapter_qa', scripts / 'accept_cross_chapter_boundaries.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def span(module, quote):
    start = module.TEXT.index(quote)
    return {'source_id': 'source-register', 'start': start, 'end': start + len(quote), 'quote': quote}


def graph(module):
    evidence = [span(module, '林岚在档案室向周宁办理借阅登记。')]
    return {'nodes': [
        {'id': 'scene-registration', 'node_type': 'scene', 'events': ['event-registration'],
         'evidence_spans': copy.deepcopy(evidence)},
        {'id': 'event-registration', 'node_type': 'event', 'evidence_spans': evidence},
    ]}


def test_one_representative_quote_passes(acceptance):
    checks, _ = acceptance.assess_boundaries(graph(acceptance), 'source-register')
    assert all(checks.values())


def test_single_span_covering_entire_process_also_passes(acceptance):
    data = graph(acceptance)
    for node in data['nodes']:
        node['evidence_spans'] = [span(acceptance, acceptance.TEXT)]
    assert all(acceptance.assess_boundaries(data, 'source-register')[0].values())


def test_scene_may_cite_context_and_completion_instead_of_event_detail(acceptance):
    data = graph(acceptance)
    data['nodes'][0]['evidence_spans'] = [
        span(acceptance, '林岚在档案室向周宁办理借阅登记。'),
        span(acceptance, '周宁仍坐在原处，接着核对签名，录入系统，确认本次借阅登记完成'),
    ]
    assert all(acceptance.assess_boundaries(data, 'source-register')[0].values())


def test_scene_chapter_headings_alone_do_not_cover_chapter_bodies(acceptance):
    data = graph(acceptance)
    data['nodes'][0]['evidence_spans'] = [span(acceptance, '第一章'), span(acceptance, '第二章')]
    checks, _ = acceptance.assess_boundaries(data, 'source-register')
    assert checks['exact_scene_and_event_evidence']
    assert not checks['scene_has_exact_body_evidence']


@pytest.mark.parametrize('kind', ['scene', 'event'])
def test_chapter_boundary_cannot_create_extra_node(acceptance, kind):
    data = graph(acceptance)
    data['nodes'].append({'id': 'extra-' + kind, 'node_type': kind,
                          'evidence_spans': [span(acceptance, acceptance.ANCHORS[1])]})
    checks, _ = acceptance.assess_boundaries(data, 'source-register')
    key = 'one_continuous_scene' if kind == 'scene' else 'one_complete_registration_event'
    assert not checks[key]


@pytest.mark.parametrize('index', [0, 1])
def test_one_chapter_body_quote_does_not_require_other_chapter_evidence(acceptance, index):
    data = graph(acceptance)
    data['nodes'][index]['evidence_spans'] = [span(acceptance, acceptance.ANCHORS[0])]
    checks, _ = acceptance.assess_boundaries(data, 'source-register')
    assert checks['one_continuous_scene'] and checks['one_complete_registration_event']
    key = 'scene_has_exact_body_evidence' if index == 0 else 'event_has_exact_body_evidence'
    assert checks[key]
    assert all(checks.values())


def test_single_quote_from_second_chapter_is_also_sufficient(acceptance):
    data = graph(acceptance)
    for node in data['nodes']:
        node['evidence_spans'] = [span(acceptance, acceptance.ANCHORS[1])]
    checks, evidence = acceptance.assess_boundaries(data, 'source-register')
    assert all(checks.values())
    assert evidence['anchor_coverage']['event-registration'] == [False, True]


def test_multiple_event_quotes_do_not_satisfy_this_single_quote_probe(acceptance):
    data = graph(acceptance)
    data['nodes'][1]['evidence_spans'] = [span(acceptance, quote) for quote in acceptance.ANCHORS]
    checks, _ = acceptance.assess_boundaries(data, 'source-register')
    assert checks['exact_scene_and_event_evidence']
    assert not checks['event_uses_one_representative_quote']


def test_event_heading_is_not_representative_body_evidence(acceptance):
    data = graph(acceptance)
    data['nodes'][1]['evidence_spans'] = [span(acceptance, '第二章')]
    checks, _ = acceptance.assess_boundaries(data, 'source-register')
    assert checks['event_uses_one_representative_quote']
    assert not checks['event_has_exact_body_evidence']


@pytest.mark.parametrize('mutation', ['quote', 'source', 'bounds', 'missing'])
def test_invalid_evidence_is_not_counted_as_cross_chapter(acceptance, mutation):
    data = graph(acceptance)
    evidence = data['nodes'][1]['evidence_spans']
    if mutation == 'quote':
        evidence[0]['quote'] = '概述而非引文'
    elif mutation == 'source':
        evidence[0]['source_id'] = 'other-source'
    elif mutation == 'bounds':
        evidence[0]['start'] = -1
    else:
        evidence.clear()
    checks, _ = acceptance.assess_boundaries(data, 'source-register')
    assert not checks['exact_scene_and_event_evidence']
    assert not checks['event_has_exact_body_evidence']


def test_same_counts_with_unattached_event_fails(acceptance):
    data = graph(acceptance)
    data['nodes'][0]['events'] = []
    assert not acceptance.assess_boundaries(data, 'source-register')[0]['event_belongs_to_cross_chapter_scene']


def test_default_action_fragment_fails(acceptance):
    data = graph(acceptance)
    data['nodes'].append({'id': 'action-sign', 'node_type': 'action'})
    assert not acceptance.assess_boundaries(data, 'source-register')[0]['no_default_actions']


def test_task_does_not_tell_expected_count_or_evidence_answers(acceptance):
    assert '一个事件' not in acceptance.TASK and '一个场景' not in acceptance.TASK
    assert not any(anchor in acceptance.TASK for anchor in acceptance.ANCHORS)
