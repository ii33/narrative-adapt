"""Analysis-only acceptance never credits automatic asset creation or binding."""
import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1]/'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('shot_assets_qa', scripts/'accept_storyboard_assets.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample():
    existing = {'kind': 'set', 'name': '档案室门口', 'usage': '已确定背景', 'asset_id': 'location-design-1'}
    original = {'approved': False, 'shots': [
        {'id': 'shot-1', 'description': '林岚收伞', 'duration_ms': 3000, 'visual_elements': [existing], 'keyframes': []},
        {'id': 'shot-2', 'description': '空走廊', 'duration_ms': 2000, 'visual_elements': [], 'keyframes': []}]}
    board = deepcopy(original)
    board['shots'][0]['visual_elements'] += [
        {'kind': 'person', 'name': '林岚', 'usage': '灰色雨衣形象', 'asset_id': None},
        {'kind': 'prop', 'name': '折叠伞', 'usage': '收起', 'asset_id': None}]
    board['shots'][0]['assets_analyzed'] = True
    board['shots'][0]['asset_elements'] = [dict(row, status='image_missing' if row['asset_id'] else 'unbound') for row in board['shots'][0]['visual_elements']]
    assets = [{'id': 'location-design-1', 'category': 'location', 'title': '档案室门口测试环境', 'node_id': ''}]
    fixture = {'selected_shot_ids': ['shot-1'], 'board_before': original, 'assets_before': deepcopy(assets)}
    return board, fixture, assets


def test_unbound_requirements_confirmed_binding_and_unchanged_library_pass(acceptance):
    args = sample()
    before = deepcopy(args)
    assert all(acceptance.assess(*args).values())
    assert args == before


@pytest.mark.parametrize('mutation,key', [
    ('autobind', 'new_elements_not_automatically_bound'),
    ('asset_create', 'asset_library_completely_unchanged'),
    ('asset_update', 'asset_library_completely_unchanged'),
    ('confirmed_delete', 'confirmed_bindings_preserved'),
    ('confirmed_update', 'confirmed_bindings_preserved'),
    ('appearance', 'person_has_specific_appearance_requirement'),
    ('unselected', 'unselected_shots_unchanged'),
    ('description', 'only_selected_elements_changed'),
    ('delete', 'same_two_shots_and_order'),
    ('reorder', 'same_two_shots_and_order'),
    ('empty', 'all_selected_shots_analyzed'),
    ('approved', 'draft_not_approved'),
    ('image', 'no_generated_media'),
    ('pending_view', 'unconfirmed_elements_show_pending'),
    ('unanalysed', 'all_selected_shots_analyzed'),
    ('fps', 'shared_scene_content_unchanged'),
])
def test_each_material_failure_is_rejected(acceptance, mutation, key):
    board, fixture, assets = sample()
    if mutation == 'autobind':
        board['shots'][0]['visual_elements'][1]['asset_id'] = 'new-asset'
    elif mutation == 'asset_create':
        assets.append({'id': 'new-asset', 'title': 'new'})
    elif mutation == 'asset_update':
        assets[0]['title'] = 'changed'
    elif mutation == 'confirmed_delete':
        board['shots'][0]['visual_elements'].pop(0)
    elif mutation == 'confirmed_update':
        board['shots'][0]['visual_elements'][0]['usage'] = 'rewritten'
    elif mutation == 'appearance':
        board['shots'][0]['visual_elements'][1]['usage'] = '常态'
    elif mutation == 'unselected':
        board['shots'][1]['visual_elements'] = deepcopy(board['shots'][0]['visual_elements'])
    elif mutation == 'description':
        board['shots'][0]['description'] += ' changed'
    elif mutation == 'delete':
        board['shots'].pop()
    elif mutation == 'reorder':
        board['shots'].reverse()
    elif mutation == 'empty':
        board['shots'][0]['visual_elements'] = []
    elif mutation == 'approved':
        board['approved'] = True
    elif mutation == 'image':
        board['shots'][0]['keyframes'] = [{'sketch_path': 'fake.png'}]
    elif mutation == 'pending_view':
        board['shots'][0]['asset_elements'][1]['status'] = 'ready'
    elif mutation == 'unanalysed':
        board['shots'][0]['assets_analyzed'] = False
    elif mutation == 'fps':
        board['fps'] = 30
    assert not acceptance.assess(board, fixture, assets)[key]


def test_multiple_selected_shots_must_all_receive_analysis(acceptance):
    board, fixture, assets = sample()
    fixture['selected_shot_ids'] = ['shot-1', 'shot-2']
    assert not acceptance.assess(board, fixture, assets)['all_selected_shots_analyzed']
    for key in ('visual_elements', 'asset_elements', 'assets_analyzed'):
        board['shots'][1][key] = deepcopy(board['shots'][0][key])
    # A selected mirror may not be automatically bound to the confirmed asset of a different shot.
    board['shots'][1]['visual_elements'] = [row for row in board['shots'][1]['visual_elements'] if row['asset_id'] is None]
    board['shots'][1]['asset_elements'] = [row for row in board['shots'][1]['asset_elements'] if row['asset_id'] is None]
    assert all(acceptance.assess(board, fixture, assets).values())


def test_target_freezes_selected_ids(acceptance):
    fixture = {'unit_id': 'unit-1', 'screenplay_id': 'script-1', 'selected_shot_ids': ['shot-1', 'shot-3']}
    assert acceptance.target(fixture) == {'kind': 'storyboard_assets', 'ids': ['unit-1'],
        'shot_ids': ['shot-1', 'shot-3'], 'screenplay_id': 'script-1'}


def test_content_snapshot_ignores_only_derived_task_status(acceptance):
    document = {'timeline': {'nodes': []}, 'event_usage': {'status': 'completed'},
        'segments': [{'id': 'segment-1', 'title': '场段', 'units': [{'id': 'unit-1', 'kind': 'scene',
            'enabled': True, 'plan': {'title': '场'}, 'blocks': [{'payload': {'text': '正文'}}]}]}]}
    original = acceptance.screenplay_content(document)
    document['event_usage']['status'] = 'running'
    assert acceptance.screenplay_content(document) == original
    document['segments'][0]['units'][0]['blocks'][0]['payload']['text'] = '改动'
    assert acceptance.screenplay_content(document) != original


def test_seed_has_confirmed_background_but_no_person_or_prop_asset(acceptance, tmp_path, monkeypatch):
    from narrative.operations import Operations
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path/'providers.sqlite3'))
    ops = Operations.create_graph_project(tmp_path/'isolated.nproj')
    fixture = acceptance.seed(ops)
    board = ops.read_storyboard(fixture['unit_id'])
    assert len(board['shots']) == 2
    assert fixture['selected_shot_ids'] == [board['shots'][0]['id']]
    assert fixture['protected_shot_id'] == board['shots'][1]['id']
    assert len(board['shots'][0]['visual_elements']) == 1
    assert board['shots'][0]['visual_elements'][0]['asset_id']
    assert not board['shots'][1]['visual_elements']
    assert board['shots'][1]['asset_elements_status'] == 'unanalysed'
    assert acceptance.all_assets(ops) == fixture['assets_before']
    assert len(fixture['assets_before']) == 1
    assert fixture['assets_before'][0]['category'] == 'location'
    assert not any(node['name'] == '折叠伞' for node in ops.graph_snapshot()['nodes'])


def test_derived_asset_counts_do_not_count_as_authored_shot_edits(acceptance):
    board, fixture, assets = sample()
    fixture['board_before']['shots'][0]['asset_elements_missing_count'] = 0
    fixture['board_before']['shots'][0]['asset_elements_image_pending_count'] = 1
    board['shots'][0]['asset_elements_missing_count'] = 2
    board['shots'][0]['asset_elements_image_pending_count'] = 1
    assert acceptance.assess(board, fixture, assets)['only_selected_elements_changed']
    board['shots'][0]['description'] = 'changed authored description'
    assert not acceptance.assess(board, fixture, assets)['only_selected_elements_changed']


def test_unregistered_asset_prefixed_field_is_not_ignored(acceptance):
    board, fixture, assets = sample()
    board['shots'][0]['asset_elements_arbitrary_business_field'] = 'must be checked'
    assert not acceptance.assess(board, fixture, assets)['only_selected_elements_changed']


def test_prompt_requires_analysis_only_without_asset_id_field(acceptance):
    assert '不调用save_asset' in acceptance.TASK
    assert '每项仅kind/name/usage，不填asset_id' in acceptance.TASK
    assert '已有confirmed_elements由程序保留' in acceptance.TASK


def test_confirmed_asset_cannot_be_automatically_bound_to_another_shot(acceptance):
    board, fixture, assets = sample()
    fixture['selected_shot_ids'] = ['shot-1', 'shot-2']
    board['shots'][1]['visual_elements'] = [deepcopy(board['shots'][0]['visual_elements'][0])]
    assert not acceptance.assess(board, fixture, assets)['new_elements_not_automatically_bound']
