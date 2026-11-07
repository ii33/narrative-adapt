"""Current container evidence follows child changes without a separate write path."""
from copy import deepcopy

import pytest
from agent_fixtures import project as project

from narrative.container_evidence import ordered_spans, refresh_container_evidence, validated_spans


def node(identifier, kind, children=None, span=None):
    return {'id': identifier, 'kind': 'node', 'node_type': kind, 'name': identifier,
            'events': children or [], 'evidence_spans': [span] if span else []}


def fixture():
    sources = {'SRC1': {'text': '甲乙丙丁'}}
    nodes = [node('E1', 'event', span={'source_id': 'SRC1', 'start': 0, 'end': 1, 'quote': '甲'}),
             node('E2', 'event', span={'source_id': 'SRC1', 'start': 1, 'end': 2, 'quote': '乙'}),
             node('S1', 'scene', ['E1']), node('S2', 'scene', ['E2']),
             node('B1', 'big_event', ['S1', 'S2']), node('T1', 'storyline', ['B1'])]
    graph = {'nodes': nodes, 'objects': deepcopy(nodes), 'sources': [{'id': 'SRC1'}],
             'source_orders': [{'source_id': 'SRC1', 'ordinal': 0}]}
    return graph, sources


def by_id(graph):
    return {n['id']: n for n in graph['nodes']}


def test_child_correction_refreshes_all_ancestors_without_mutating_input():
    graph, sources = fixture()
    original = refresh_container_evidence(graph, sources)
    changed = deepcopy(original)
    by_id(changed)['E1']['evidence_spans'] = [{'source_id': 'SRC1', 'start': 2, 'end': 3, 'quote': '丙'}]
    frozen = deepcopy(changed)
    result = refresh_container_evidence(changed, sources, original)
    rows = by_id(result)
    assert rows['S1']['disclosure'] == {'source_id': 'SRC1', 'start': 2}
    assert rows['T1']['disclosure'] == {'source_id': 'SRC1', 'start': 1}
    assert rows['T1']['completion'] == {'source_id': 'SRC1', 'start': 3}
    assert rows['T1']['evidence'] == {'source_id': 'SRC1', 'quote': '乙'}
    assert by_id(original)['T1']['completion']['start'] == 2
    assert changed == frozen
    assert by_id(result)['S2'] == by_id(original)['S2']


def test_move_refreshes_old_and_new_parent_and_keeps_all_other_evidence():
    graph, sources = fixture()
    original = refresh_container_evidence(graph, sources)
    changed = deepcopy(original)
    rows = by_id(changed)
    rows['S1']['events'] = []
    rows['S2']['events'] = ['E1', 'E2']
    result = by_id(refresh_container_evidence(changed, sources, original))
    assert result['S1']['evidence_spans'] == []
    assert 'disclosure' not in result['S1'] and 'evidence' not in result['S1']
    assert [s['quote'] for s in result['S2']['evidence_spans']] == ['甲', '乙']
    assert result['T1']['completion']['start'] == 2


def test_source_order_not_identifier_controls_container_start_and_completion():
    graph, sources = fixture()
    sources['SRC9'] = {'text': '前'}
    graph['sources'].append({'id': 'SRC9'})
    graph['source_orders'] = [{'source_id': 'SRC1', 'ordinal': 1}, {'source_id': 'SRC9', 'ordinal': 0}]
    by_id(graph)['E2']['evidence_spans'] = [{'source_id': 'SRC9', 'start': 0, 'end': 1, 'quote': '前'}]
    result = by_id(refresh_container_evidence(graph, sources))
    assert result['T1']['disclosure'] == {'source_id': 'SRC9', 'start': 0}
    assert result['T1']['completion'] == {'source_id': 'SRC1', 'start': 1}


def test_completion_uses_maximum_end_not_last_start_when_spans_overlap():
    graph, sources = fixture()
    rows = by_id(graph)
    rows['E1']['evidence_spans'] = [{'source_id': 'SRC1', 'start': 0, 'end': 4, 'quote': '甲乙丙丁'}]
    assert by_id(refresh_container_evidence(graph, sources))['T1']['completion']['start'] == 4


def test_plain_evidence_uses_disclosure_but_never_invents_coordinates():
    sources = {'SRC1': {'text': '甲乙'}}
    good = {'evidence': {'source_id': 'SRC1', 'quote': '乙'},
            'disclosure': {'source_id': 'SRC1', 'start': 1}}
    assert validated_spans(good, sources, strict=True)[0]['start'] == 1
    with pytest.raises(ValueError, match='起点'):
        validated_spans({'evidence': good['evidence']}, sources, strict=True)


def test_invalid_span_is_explicit_in_strict_derivation():
    with pytest.raises(ValueError, match='不一致'):
        validated_spans({'evidence_spans': [{'source_id': 'SRC1', 'start': 0,
                         'end': 1, 'quote': '伪'}]}, {'SRC1': '真'}, strict=True)


def test_unregistered_source_cannot_be_sorted_by_guessed_identifier_order():
    with pytest.raises(ValueError, match='阅读顺序'):
        ordered_spans([{'source_id': 'missing', 'start': 0, 'end': 1, 'quote': '字'}], {'sources': []})


def test_operations_child_evidence_update_commits_derived_ancestors_atomically(project):
    text = '甲在站台办妥登记。随后确认登记已完成。尾注。'
    source = project.import_source('派生更新', text)
    accepted = project.submit_extraction(source, {'nodes': [
        {'type': 'person', 'name': '甲', 'quotes': ['甲在站台办妥登记。']},
        {'type': 'location', 'name': '站台', 'quotes': ['站台']},
        {'type': 'event', 'name': '办妥登记', 'quotes': ['甲在站台办妥登记。'],
         'scene': '@4', 'participants': ['@1'], 'location': '@2'},
        {'type': 'scene', 'name': '站台登记', 'big_event': '@5'},
        {'type': 'big_event', 'name': '登记办理', 'storyline': '@6'},
        {'type': 'storyline', 'name': '完成登记', 'children': ['@5']},
    ], 'properties': []})
    assert accepted['status'] == 'accepted', accepted
    before = project.graph_snapshot()
    event = next(n for n in before['nodes'] if n['node_type'] == 'event')
    extra = '随后确认登记已完成。'
    start = text.index(extra)
    update = event | {'evidence_spans': [*event['evidence_spans'],
        {'source_id': source, 'start': start, 'end': start + len(extra), 'quote': extra}]}
    result = project.apply_changes([update], [], before['revision'], '补充同一事件后续证据')
    assert result['status'] == 'accepted', result
    after = project.graph_snapshot()
    assert after['revision'] == before['revision'] + 1
    for node in after['nodes']:
        if node['node_type'] in {'scene', 'big_event', 'storyline'}:
            assert any(s['quote'] == extra for s in node['evidence_spans'])
            if node['node_type'] == 'storyline':
                assert node['completion'] == {'source_id': source, 'start': start + len(extra)}
    assert project.check_project()['counts']['error'] == 0
    assert project.source(source)['text'] == text
