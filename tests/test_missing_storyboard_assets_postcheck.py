"""The explicit program button postcheck must not credit nonempty or partial cards."""
import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def postcheck(monkeypatch):
    scripts = Path(__file__).resolve().parents[1]/'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('missing_assets_postcheck', scripts/'check_missing_storyboard_assets.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample():
    confirmed = {'kind': 'set', 'name': '档案室', 'usage': '已确认背景', 'asset_id': 'location-design-1'}
    before = {'shots': [{'id': 'shot-1', 'description': '林岚收伞', 'assets_analyzed': True,
        'visual_elements': [confirmed, {'kind': 'person', 'name': '林岚', 'usage': '灰色雨衣', 'asset_id': None},
            {'kind': 'prop', 'name': '折叠伞', 'usage': '收起', 'asset_id': None}], 'asset_elements': []},
        {'id': 'shot-2', 'description': '空走廊', 'visual_elements': []}]}
    after = deepcopy(before)
    after['shots'][0]['visual_elements'][1]['asset_id'] = 'appearance-1'
    after['shots'][0]['visual_elements'][2]['asset_id'] = 'item-design-1'
    after['shots'][0]['asset_elements'] = [dict(row, status='image_missing') for row in after['shots'][0]['visual_elements']]
    old = [{'id': 'location-design-1', 'title': '背景', 'category': 'location', 'detail': '既有设定'}]
    assets = deepcopy(old) + [{'id': 'appearance-1', 'title': '林岚-镜头1形象', 'category': 'appearance',
        'detail': '', 'prompt': '', 'images': [], 'reference_images': [], 'node_id': '', 'valid_from': '', 'valid_to': ''},
        {'id': 'item-design-1', 'title': '折叠伞-镜头1资产', 'category': 'item',
        'detail': '', 'prompt': '', 'images': [], 'reference_images': [], 'node_id': '', 'valid_from': '', 'valid_to': ''}]
    return before, after, ['shot-1'], old, assets, {'created_count': 2}


def test_empty_cards_and_atomic_bindings_preserve_existing_material(postcheck):
    args = sample()
    original = deepcopy(args)
    assert all(postcheck.assess_click(*args).values())
    assert args == original


@pytest.mark.parametrize('field,value', [('detail', 'invented detail'), ('prompt', 'generated prompt'),
    ('images', [{'path': 'new.png'}]), ('reference_images', [{'path': 'ref.png'}]), ('node_id', 'invented-fact'),
    ('valid_from', 'event-1'), ('valid_to', 'event-2')])
def test_empty_card_must_not_contain_generated_content_or_fact_link(postcheck, field, value):
    args = sample()
    args[4][1][field] = value
    assert not postcheck.assess_click(*args)['new_cards_are_empty_except_identity']


@pytest.mark.parametrize('mutation,key', [
    ('same_asset', 'all_pending_rows_bound_to_new_distinct_cards'),
    ('unbound', 'all_pending_rows_bound_to_new_distinct_cards'),
    ('category', 'new_card_categories_match_element_kinds'),
    ('description', 'selected_shot_authored_content_unchanged'),
    ('usage', 'element_requirements_preserved'),
    ('confirmed', 'previous_confirmed_bindings_preserved'),
    ('old_asset', 'previous_asset_cards_unchanged'),
    ('unselected', 'unselected_shots_unchanged'),
    ('partial', 'button_created_one_card_per_pending_element'),
])
def test_postcheck_rejects_partial_or_unrelated_mutation(postcheck, mutation, key):
    args = sample()
    before, after, ids, old, assets, receipt = args
    if mutation == 'same_asset':
        after['shots'][0]['visual_elements'][2]['asset_id'] = 'appearance-1'
    elif mutation == 'unbound':
        after['shots'][0]['visual_elements'][2]['asset_id'] = None
    elif mutation == 'category':
        assets[1]['category'] = 'item'
    elif mutation == 'description':
        after['shots'][0]['description'] += ' changed'
    elif mutation == 'usage':
        after['shots'][0]['visual_elements'][1]['usage'] = 'rewritten demand'
    elif mutation == 'confirmed':
        after['shots'][0]['visual_elements'][0]['asset_id'] = 'changed'
    elif mutation == 'old_asset':
        assets[0]['detail'] = 'changed'
    elif mutation == 'unselected':
        after['shots'][1]['description'] = 'changed'
    elif mutation == 'partial':
        receipt['created_count'] = 1
    assert not postcheck.assess_click(before, after, ids, old, assets, receipt)[key]
