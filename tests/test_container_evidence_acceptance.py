"""Container QA must validate derivation rather than just counting nodes."""
import copy
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('container_evidence_qa', scripts / 'accept_container_evidence.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def span(module, quote):
    start = module.TEXT.index(quote)
    return {'source_id': 'source-registration', 'start': start, 'end': start + len(quote), 'quote': quote}


def graph(module):
    evidence = [span(module, '林岚签收并道谢，借阅办理至此结束。')]
    def node(identifier, kind, children):
        return {'id': identifier, 'name': identifier, 'node_type': kind, 'events': children,
                'evidence_spans': copy.deepcopy(evidence),
                'disclosure': {'source_id': 'source-registration', 'start': evidence[0]['start']}}
    story = node('story-registration', 'storyline', ['big-registration'])
    story['completion'] = {'source_id': 'source-registration', 'start': evidence[0]['end']}
    return {'nodes': [story, node('big-registration', 'big_event', ['scene-registration']),
        node('scene-registration', 'scene', ['event-registration']), node('event-registration', 'event', [])]}


def test_exact_child_derived_evidence_and_completion_pass(acceptance):
    assert all(acceptance.assess_container_graph(graph(acceptance), 'source-registration')[0].values())


@pytest.mark.parametrize('kind', ['storyline', 'big_event', 'scene'])
def test_missing_level_or_child_cannot_pass(acceptance, kind):
    data = graph(acceptance)
    data['nodes'] = [n for n in data['nodes'] if n['node_type'] != kind]
    checks, _ = acceptance.assess_container_graph(data, 'source-registration')
    assert not checks['all_container_levels_created']
    if kind != 'storyline':
        assert not checks['containers_have_typed_children']


def test_empty_container_fails(acceptance):
    data = graph(acceptance)
    data['nodes'][2]['events'] = []
    checks, _ = acceptance.assess_container_graph(data, 'source-registration')
    assert not checks['containers_have_typed_children']
    assert not checks['container_evidence_is_child_derived']


def test_exact_but_independently_selected_parent_quote_fails(acceptance):
    data = graph(acceptance)
    data['nodes'][2]['evidence_spans'] = [span(acceptance, '林岚在档案室向周宁申请借阅。')]
    checks, _ = acceptance.assess_container_graph(data, 'source-registration')
    assert checks['exact_body_evidence_excludes_tail']
    assert not checks['container_evidence_is_child_derived']


@pytest.mark.parametrize('mutation', ['quote', 'source', 'offset', 'tail'])
def test_invalid_or_tail_evidence_fails(acceptance, mutation):
    data = graph(acceptance)
    evidence = data['nodes'][3]['evidence_spans'][0]
    if mutation == 'quote':
        evidence['quote'] = '不存在的引文'
    elif mutation == 'source':
        evidence['source_id'] = 'other-source'
    elif mutation == 'offset':
        evidence['start'] = -1
    else:
        data['nodes'][3]['evidence_spans'] = [span(acceptance, acceptance.TAIL.strip())]
    assert not acceptance.assess_container_graph(data, 'source-registration')[0]['exact_body_evidence_excludes_tail']


def test_disclosure_must_be_first_child_evidence(acceptance):
    data = graph(acceptance)
    data['nodes'][2]['disclosure']['start'] += 1
    assert not acceptance.assess_container_graph(data, 'source-registration')[0]['container_disclosure_is_child_derived']


def test_story_closure_cannot_use_source_end(acceptance):
    data = graph(acceptance)
    data['nodes'][0]['completion']['start'] = len(acceptance.TEXT)
    assert not acceptance.assess_container_graph(data, 'source-registration')[0]['story_completion_is_child_derived']


def test_event_still_uses_one_representative_quote(acceptance):
    data = graph(acceptance)
    data['nodes'][3]['evidence_spans'].append(span(acceptance, '周宁核验登记后交付资料'))
    assert not acceptance.assess_container_graph(data, 'source-registration')[0]['events_have_one_representative_evidence']


def raw_draft():
    return {'nodes': [
        {'type': 'storyline', 'name': '办理完成', 'children': ['@2']},
        {'type': 'big_event', 'name': '办理', 'children': ['@3']},
        {'type': 'scene', 'name': '档案室办理', 'children': ['@4']},
        {'type': 'event', 'name': '借阅办理', 'quotes': ['借阅办理至此结束。']},
    ]}


def test_original_model_draft_omits_container_evidence_fields(acceptance):
    assert all(acceptance.assess_drafts([raw_draft()]).values())


@pytest.mark.parametrize('field', ['quotes', 'closing_quote'])
def test_model_authored_container_quote_does_not_count_as_program_derivation(acceptance, field):
    draft = raw_draft()
    draft['nodes'][0][field] = [] if field == 'quotes' else '办理结束'
    assert not acceptance.assess_drafts([draft])['model_did_not_author_container_quotes']


def test_absent_model_container_rows_cannot_vacuously_pass(acceptance):
    assert not acceptance.assess_drafts([])['model_did_not_author_container_quotes']


def test_task_does_not_prescribe_node_counts_or_a_quote_answer(acceptance):
    assert '一个事件' not in acceptance.TASK and '一个场景' not in acceptance.TASK
    assert '林岚签收并道谢' not in acceptance.TASK
