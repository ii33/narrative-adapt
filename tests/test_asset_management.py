"""资产管理：检索、依赖、局部修改、强制删除、绑定。"""
import pytest
from test_screenplay_source_references import prepared as prepared
from test_storyboard_assets import board as board

from narrative.content_errors import ContentGuardError

pytestmark = pytest.mark.slow


def make_person(ops, title='甲·形象', node_id='', domain='adaptation'):
    return ops.save_asset({'category': 'appearance', 'title': title, 'node_id': node_id, 'domain': domain})


def test_search_filters_by_keyword_category_node_and_usage(board):
    ops, _script, unit, b = board
    card = make_person(ops, '乙·形象', node_id='')
    make_person(ops, '丙·空间')
    ops.save_asset({'category': 'location', 'title': '车站·空间'})

    by_word = ops.search_assets(keyword='乙')
    assert [item['id'] for item in by_word['assets']] == [card['id']]
    assert ops.search_assets(category='location')['count'] == 1
    assert ops.search_assets(has_images=False)['count'] >= 2
    assert ops.search_assets(has_images=True)['count'] == 0

    # 绑定之后就能按「已被使用」筛出来。
    ops.bind_storyboard_element(unit, b['shots'][0]['id'], 0, card['id'])
    assert [item['id'] for item in ops.search_assets(unbound=True)['assets']] != [card['id']]
    assert ops.search_assets(node_id='')  # 空节点不做筛选


def test_get_asset_lists_who_uses_it(board):
    ops, _script, unit, b = board
    card = make_person(ops)
    assert ops.get_asset(card['id'])['used_by'] == []
    ops.bind_storyboard_element(unit, b['shots'][0]['id'], 0, card['id'])
    used = ops.get_asset(card['id'])['used_by']
    assert used and used[0]['unit_id'] == unit and used[0]['shot_id'] == b['shots'][0]['id']
    assert used[0]['name'] == '甲'


def test_update_asset_changes_fields_without_resending_the_whole_card(board):
    ops, _script, unit, b = board
    card = make_person(ops, '甲·形象')
    updated = ops.update_asset(card['id'], expected_revision=card['revision'],
                               changes={'title': '甲·改造后', 'detail': '左臂有紫色纹路'})
    assert updated['title'] == '甲·改造后' and updated['detail'] == '左臂有紫色纹路'
    assert updated['category'] == 'appearance'


def test_update_asset_rejects_unknown_fields(board):
    ops, _script, unit, b = board
    card = make_person(ops)
    with pytest.raises(Exception) as error:
        ops.update_asset(card['id'], changes={'id': 'appearance-999'})
    assert '不允许修改' in str(error.value)


def test_delete_refuses_while_referenced_and_force_clears_bindings(board):
    ops, _script, unit, b = board
    card = make_person(ops)
    ops.bind_storyboard_element(unit, b['shots'][0]['id'], 0, card['id'])
    with pytest.raises(ValueError, match='仍被分镜引用'):
        ops.delete_asset(card['id'])
    current = ops.read_storyboard(unit)
    result = ops.delete_asset(card['id'], force=True)
    assert result['cleared_bindings']
    after = ops.read_storyboard(unit)
    assert after['shots'][0]['visual_elements'][0]['asset_id'] is None
    assert current['revision'] < after['revision']


def test_bind_and_unbind_element_respects_category(board):
    ops, _script, unit, b = board
    shot = b['shots'][0]['id']
    card = make_person(ops)
    ops.bind_storyboard_element(unit, shot, 0, card['id'])
    assert ops.read_storyboard(unit)['shots'][0]['visual_elements'][0]['asset_id'] == card['id']
    place = ops.save_asset({'category': 'location', 'title': '车站·空间'})
    with pytest.raises(ContentGuardError, match='类别不符'):
        ops.bind_storyboard_element(unit, shot, 0, place['id'])
    ops.unbind_storyboard_element(unit, shot, 0)
    assert ops.read_storyboard(unit)['shots'][0]['visual_elements'][0]['asset_id'] is None


def test_generate_asset_sketch_requires_asset_or_node(board):
    ops, _script, unit, b = board
    with pytest.raises(ContentGuardError, match='资产编号或节点编号'):
        ops.generate_asset_sketch()
    with pytest.raises(ContentGuardError, match='还没有资产卡'):
        ops.generate_asset_sketch(node_id='P404')
