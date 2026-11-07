"""The real QA checker verifies provenance identity, not only visible labels."""
import copy
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('source_nodes_qa', scripts / 'accept_screenplay_source_nodes.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    return {'event_ids': ['E1', 'E2'], 'event_names': {'E1': '批准借阅', 'E2': '夜间会合'},
            'timeline_id': 'script-timeline', 'timeline_revision': 3}


def sample():
    f = fixture()
    units, elements, notes = [], [], []
    for index, identifier in enumerate(f['event_ids']):
        text = '依据（剧本改编时间线）：' + identifier + ' · ' + f['event_names'][identifier]
        unit = {'id': 'unit-' + str(index + 1), 'kind': 'scene', 'enabled': True,
                'plan': {'source_event_ids': [identifier]},
                'source_references': {'timeline_id': f['timeline_id'], 'timeline_revision': f['timeline_revision'],
                    'nodes': [{'id': identifier, 'name': f['event_names'][identifier],
                               'source_node_id': identifier, 'status': 'available'}]},
                'source_reference_text': text,
                'blocks': [{'type': 'action', 'payload': {'text': '她点头。'}}] if index == 0 else []}
        units.append(unit)
        elements += [{'kind': 'scene', 'unit_id': unit['id'], 'text': '场头'},
                     {'kind': 'source_reference', 'unit_id': unit['id'], 'text': text}]
        notes.append(text)
    document = {'segments': [{'units': units}]}
    output = '\n'.join(notes)
    outputs = {key: output for key in ('text', 'html', 'qt_preview', 'legacy_text')}
    outputs['fountain'] = '\n'.join('[[' + text + ']]' for text in notes)
    return document, f, elements, outputs


def test_exact_program_resolved_current_timeline_references_pass(acceptance):
    checks, _ = acceptance.assess_sources(*sample())
    assert all(checks.values())


@pytest.mark.parametrize('mutation', ['id', 'name', 'source_node_id', 'status'])
def test_visible_labels_cannot_hide_wrong_reference_identity(acceptance, mutation):
    data = sample()
    row = data[0]['segments'][0]['units'][0]['source_references']['nodes'][0]
    row[mutation] = 'wrong'
    checks, _ = acceptance.assess_sources(*data)
    assert not checks['references_are_program_resolved_id_name_and_upstream']


@pytest.mark.parametrize('field', ['timeline_id', 'timeline_revision'])
def test_original_or_stale_timeline_reference_fails(acceptance, field):
    data = sample()
    data[0]['segments'][0]['units'][0]['source_references'][field] = 'old'
    checks, _ = acceptance.assess_sources(*data)
    assert not checks['references_resolve_current_script_timeline']


def test_scene_mapping_is_not_just_same_union_of_events(acceptance):
    data = sample()
    units = data[0]['segments'][0]['units']
    units[0]['plan']['source_event_ids'], units[1]['plan']['source_event_ids'] = ['E2'], ['E1']
    assert not acceptance.assess_sources(*data)[0]['two_distinct_scenes_mapped_in_reading_order']


def test_note_must_follow_each_scene_heading(acceptance):
    data = sample()
    data[2][0], data[2][1] = data[2][1], data[2][0]
    assert not acceptance.assess_sources(*data)[0]['one_compiled_note_after_each_heading']


def test_missing_note_in_any_output_fails(acceptance):
    data = sample()
    data[3]['qt_preview'] = ''
    assert not acceptance.assess_sources(*data)[0]['export_and_compile_show_each_source_note']


def test_fountain_requires_nonperformed_note_syntax(acceptance):
    data = sample()
    data[3]['fountain'] = data[3]['text']
    assert not acceptance.assess_sources(*data)[0]['fountain_notes_use_note_syntax']


def test_fountain_note_delimiter_whitespace_is_not_a_false_failure(acceptance):
    data = sample()
    data[3]['fountain'] = data[3]['fountain'].replace('[[', '[[ ').replace(']]', ' ]]')
    assert all(acceptance.assess_sources(*data)[0].values())


def test_body_cannot_be_used_to_fake_source_annotation(acceptance):
    data = sample()
    unit = data[0]['segments'][0]['units'][0]
    unit['blocks'][0]['payload']['text'] = unit['source_reference_text']
    assert not acceptance.assess_sources(*data)[0]['source_notes_are_not_body_blocks']


def test_fixture_has_two_explicit_event_locations_without_model_extraction(acceptance):
    assert acceptance.FIRST in acceptance.TEXT and acceptance.SECOND in acceptance.TEXT
    assert 'source_event_ids' in acceptance.TASK
    assert 'E1' not in acceptance.TASK and 'E2' not in acceptance.TASK


def test_checker_does_not_mutate_document_or_output(acceptance):
    data = sample()
    before = copy.deepcopy(data)
    acceptance.assess_sources(*data)
    assert data == before


def test_seed_isolated_valid_two_event_timeline(acceptance, tmp_path, monkeypatch):
    from narrative.operations import Operations
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    ops = Operations.create_graph_project(tmp_path / 'source-labels.nproj')
    prepared = acceptance.seed(ops)
    timeline = ops.read_screenplay_timeline(prepared['screenplay_id'])
    assert [n['id'] for n in timeline['nodes'] if n['node_type'] == 'event'] == prepared['event_ids']
    assert len(prepared['event_ids']) == 2
    assert ops.check_project()['counts']['error'] == 0
    assert ops.read_screenplay(prepared['screenplay_id'])['segments'][0]['units'] == []
