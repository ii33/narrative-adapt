"""B0 资产需求层：需求（想要什么）与引用（绑到哪张卡）分开存、分开读。

要点：
* 声明需求不改 visual_elements，也不建卡；
* 缺口视图把「需求未落实」和「引用悬空」算成两类；
* 没有任何需求时照样能绑资产（老流程不受影响）；
* 补建资产并绑上以后缺口消失；
* 场级与镜级需求共存，各自只在本场／本镜范围内落实。
"""
import json

import pytest
from PIL import Image
from test_screenplay_source_references import prepared as prepared
from test_screenplay_source_references import submit
from test_screenplay_writing import body

from narrative.content_errors import ContentConflictError, ContentGuardError, ContentValidationError
from narrative.tool_api import TOOLS

pytestmark = pytest.mark.slow


@pytest.fixture
def board(prepared):
    ops, script, segment, _events = prepared
    scene = submit(ops, segment, [{'title': '站前', 'summary': '甲在车站等候。'}])['units'][0]
    unit = scene['id']
    block = body(ops, unit, [{'type': 'action', 'payload': {'text': '甲拿着一把伞。'}}])['blocks'][0]['id']
    ops.apply_storyboard_changes(unit, 0, [
        {'op': 'create_shot', 'title': '等候', 'source_block_ids': [block]},
        {'op': 'create_shot', 'title': '空景', 'source_block_ids': [block]}])
    return ops, script, unit, ops.read_storyboard(unit)


def inject(ops, unit, mutate):
    """直接改存储里的分镜 JSON：模拟外部工具留下的悬空引用（只读诊断要能报出来）。"""
    with ops._store.connect(write=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key=?", ("storyboard:" + unit,)).fetchone()
        data = json.loads(row[0])
        mutate(data)
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                   ("storyboard:" + unit, json.dumps(data, ensure_ascii=False)))


def test_needs_are_stored_apart_from_references(board):
    ops, _script, unit, b = board
    first = b['shots'][0]['id']
    needs = [{'kind': 'person', 'name': '甲', 'variant': '便装', 'note': '需要一把伞'},
             {'kind': 'material', 'name': '磨砂金属'}]
    written = ops.declare_asset_needs(unit, b['revision'], unit_needs=[{'kind': 'location', 'name': '车站'}],
                                      shot_needs=[{'shot_id': first, 'needs': needs}])
    assert written['revision'] == b['revision'] + 1
    now = ops.read_storyboard(unit)
    # 需求按声明原样落盘（空的 variant/note 直接不写），引用一条都没多出来。
    assert now['shots'][0]['asset_needs'] == needs
    assert now['shots'][0]['visual_elements'] == []
    assert now['asset_needs'] == [{'kind': 'location', 'name': '车站'}]
    assert now['shots'][1]['asset_needs'] == []
    assert {'read_asset_needs', 'declare_asset_needs'} <= TOOLS.keys()


def test_gap_view_counts_unresolved_needs_and_dangling_references(board):
    ops, _script, unit, b = board
    first, second = [shot['id'] for shot in b['shots']]
    card = ops.save_asset({'category': 'appearance', 'title': '形象甲'})
    # 引用一：绑了资产但还没有参考图 → 引用悬空(image_missing)。
    ops.apply_storyboard_changes(unit, b['revision'], [
        {'op': 'update_shot', 'shot_id': first,
         'visual_elements': [{'kind': 'person', 'name': '甲', 'usage': '站在门边', 'asset_id': card['id']}]}])
    view = ops.read_asset_needs(unit)
    assert view['counts'] == {'unit': 0, 'shot': 0, 'total': 0, 'resolved': 0, 'unresolved': 0, 'dangling': 1}
    assert view['dangling'] == [{'shot_id': first, 'asset_id': card['id'], 'reason': 'image_missing',
                                 'index': 0, 'name': '甲', 'message': '资产已建立，待添加参考图'}]
    # 需求未落实：人物需求对得上引用（有资产编号），道具需求没人管。
    current = ops.read_storyboard(unit)
    written = ops.declare_asset_needs(unit, current['revision'], unit_needs=[{'kind': 'prop', 'name': '伞'}],
                                      shot_needs=[{'shot_id': first, 'needs': [{'kind': 'person', 'name': '甲'}]}])
    gap = written['asset_needs_gap']
    assert gap['counts'] == {'unit': 1, 'shot': 1, 'total': 2, 'resolved': 1, 'unresolved': 1, 'dangling': 1}
    assert {row['scope'] for row in gap['needs']} == {'unit', 'shot'}
    assert next(row for row in gap['needs'] if row['scope'] == 'shot')['bound_asset_id'] == card['id']
    assert next(row for row in gap['needs'] if row['scope'] == 'unit')['bound_asset_id'] is None
    # 引用指向不存在的资产（外部工具或历史数据留下的）→ 引用悬空(missing)，需求侧不动。
    inject(ops, unit, lambda data: next(shot for shot in data['shots'] if shot['id'] == second)['visual_elements'].append(
        {'kind': 'prop', 'name': '伞', 'usage': '手持', 'asset_id': 'item-999'}))
    dangling = ops.read_asset_needs(unit)['dangling']
    assert {'shot_id': second, 'asset_id': 'item-999', 'reason': 'missing', 'index': 0, 'name': '伞',
            'message': '绑定的资产对象不存在'} in dangling
    assert ops.read_asset_needs(unit)['counts']['dangling'] == 2


def test_binding_works_without_any_declared_need(board):
    ops, _script, unit, b = board
    first = b['shots'][0]['id']
    asset = ops.save_asset({'category': 'item', 'title': '独立雨伞'})
    written = ops.apply_storyboard_changes(unit, b['revision'], [
        {'op': 'update_shot', 'shot_id': first,
         'visual_elements': [{'kind': 'prop', 'name': '伞', 'usage': '手持', 'asset_id': asset['id']}]}])
    assert written['submission_status'] == 'committed'
    view = ops.read_asset_needs(unit)
    assert view['needs'] == [] and view['counts']['total'] == 0
    # 没声明需求也照样能绑；这张卡还没有参考图，所以只报引用悬空。
    assert [row['reason'] for row in view['dangling']] == ['image_missing']


def test_creating_missing_assets_clears_the_unresolved_need(board):
    ops, _script, unit, b = board
    first = b['shots'][0]['id']
    declared = ops.declare_asset_needs(unit, b['revision'],
                                       shot_needs=[{'shot_id': first, 'needs': [{'kind': 'person', 'name': '甲'}]}])
    assert declared['asset_needs_gap']['counts']['unresolved'] == 1
    # 需求本身不绑卡：元素列表里显式写出这一条，再由显式命令补建空卡。
    current = ops.read_storyboard(unit)
    ops.apply_storyboard_changes(unit, current['revision'], [
        {'op': 'update_shot', 'shot_id': first,
         'visual_elements': [{'kind': 'person', 'name': '甲', 'usage': '出场', 'asset_id': None}]}])
    filled = ops.create_missing_storyboard_assets(unit, [first], ops.read_storyboard(unit)['revision'])
    assert filled['created_count'] == 1
    view = ops.read_asset_needs(unit)
    assert view['counts']['unresolved'] == 0
    assert view['needs'][0]['bound_asset_id'] == filled['created_assets'][0]['id']
    # 卡建好了但还没有参考图：仍算引用悬空，只是不再是「需求未落实」。
    assert [row['reason'] for row in view['dangling']] == ['image_missing']


def test_unit_and_shot_needs_coexist_and_scope_binding(board):
    ops, _script, unit, b = board
    first, second = [shot['id'] for shot in b['shots']]
    asset = ops.save_asset({'category': 'appearance', 'title': '形象乙'})
    # 只在第二镜绑了「乙」；场级需求可由任意镜落实，镜级需求只认本镜。
    ops.apply_storyboard_changes(unit, b['revision'], [
        {'op': 'update_shot', 'shot_id': second,
         'visual_elements': [{'kind': 'person', 'name': '乙', 'usage': '出场', 'asset_id': asset['id']}]}])
    current = ops.read_storyboard(unit)
    written = ops.declare_asset_needs(unit, current['revision'], unit_needs=[{'kind': 'person', 'name': '乙'}],
                                      shot_needs=[{'shot_id': first, 'needs': [{'kind': 'person', 'name': '乙'}]}])
    gap = written['asset_needs_gap']
    assert gap['counts']['unit'] == 1 and gap['counts']['shot'] == 1
    unit_row = next(row for row in gap['needs'] if row['scope'] == 'unit')
    shot_row = next(row for row in gap['needs'] if row['scope'] == 'shot')
    assert unit_row['bound_asset_id'] == asset['id']
    assert unit_row['shot_id'] is None
    assert shot_row['shot_id'] == first and shot_row['bound_asset_id'] is None
    assert gap['counts']['unresolved'] == 1


def test_declare_replace_appends_only_when_asked(board):
    ops, _script, unit, b = board
    first = b['shots'][0]['id']
    base = [{'kind': 'person', 'name': '甲'}]
    ops.declare_asset_needs(unit, b['revision'], shot_needs=[{'shot_id': first, 'needs': base}])
    appended = ops.declare_asset_needs(unit, ops.read_storyboard(unit)['revision'], replace=False,
                                       shot_needs=[{'shot_id': first, 'needs': base + [{'kind': 'prop', 'name': '伞'}]}])
    assert appended['shots'][0]['asset_needs'] == [{'kind': 'person', 'name': '甲'}, {'kind': 'prop', 'name': '伞'}]
    # 默认整体替换：只给一条就只剩一条。
    replaced = ops.declare_asset_needs(unit, appended['revision'], shot_needs=[{'shot_id': first, 'needs': base}])
    assert replaced['shots'][0]['asset_needs'] == base
    # 没给的那一侧保持不动。
    ops.declare_asset_needs(unit, replaced['revision'], unit_needs=[{'kind': 'location', 'name': '车站'}])
    assert ops.read_storyboard(unit)['shots'][0]['asset_needs'] == base
    assert ops.read_storyboard(unit)['asset_needs'] == [{'kind': 'location', 'name': '车站'}]


def test_declare_refuses_bad_input_without_writing(board):
    ops, _script, unit, b = board
    first = b['shots'][0]['id']
    for declaration, error in [
        ({'unit_needs': [{'kind': 'set', 'name': '车站'}]}, ContentValidationError),
        ({'unit_needs': [{'kind': 'person'}]}, ContentValidationError),
        ({'unit_needs': [{'kind': 'person', 'name': '甲'}, {'kind': 'person', 'name': ' 甲 '}]}, ContentValidationError),
        ({'shot_needs': [{'shot_id': 'shot-999', 'needs': []}]}, ContentValidationError),
        ({'shot_needs': [{'shot_id': first, 'needs': []}, {'shot_id': first, 'needs': []}]}, ContentValidationError),
        ({}, ValueError),
    ]:
        with pytest.raises(error):
            ops.declare_asset_needs(unit, b['revision'], **declaration)
        assert ops.read_storyboard(unit) == b


def test_declare_needs_revision_and_approval_guards(board, tmp_path):
    ops, _script, unit, b = board
    with pytest.raises(ContentConflictError, match='刷新'):
        ops.declare_asset_needs(unit, b['revision'] + 1, unit_needs=[{'kind': 'person', 'name': '甲'}])
    image = tmp_path / 'frame.png'
    Image.new('RGB', (8, 8), 'gray').save(image)
    framed = ops.apply_storyboard_changes(unit, b['revision'], [
        {'op': 'set_keyframe', 'shot_id': shot['id'], 'offset_ms': 0, 'sketch_path': str(image)} for shot in b['shots']])
    approved = ops.apply_storyboard_changes(unit, framed['revision'], [{'op': 'set_approved', 'approved': True}])
    with pytest.raises(ContentGuardError, match='解除定案'):
        ops.declare_asset_needs(unit, approved['revision'], unit_needs=[{'kind': 'person', 'name': '甲'}])


def test_assets_context_carries_gap_summary(board):
    ops, _script, unit, b = board
    first = b['shots'][0]['id']
    ops.declare_asset_needs(unit, b['revision'], unit_needs=[{'kind': 'prop', 'name': '伞'}])
    context = ops.read_storyboard_assets_context(unit, [first])
    assert context['asset_needs']['counts']['unit'] == 1
    assert context['asset_needs']['needs'][0]['name'] == '伞'
    assert context['asset_needs']['dangling'] == []
    # 需求摘要之外，原有分析上下文一项不少。
    assert context['shot_ids'] == [first] and context['assets'] is not None


def test_tool_entrypoints_declare_and_read_needs(board):
    from narrative import tool_api
    from narrative.project_package import layout

    ops, _script, unit, b = board
    entry = layout(ops.project_info().path).entry
    first = b['shots'][0]['id']
    written = tool_api.call('declare_asset_needs', {
        'unit_id': unit, 'revision': b['revision'], 'unit_needs': [{'kind': 'prop', 'name': '伞'}],
        'shot_needs': [{'shot_id': first, 'needs': [{'kind': 'person', 'name': '甲'}]}]}, project=entry)
    assert written['asset_needs_gap']['counts'] == {'unit': 1, 'shot': 1, 'total': 2,
                                                    'resolved': 0, 'unresolved': 2, 'dangling': 0}
    read = tool_api.call('read_asset_needs', {'unit_id': unit}, project=entry)
    assert read['counts'] == written['asset_needs_gap']['counts']
    assert {row['scope'] for row in read['needs']} == {'unit', 'shot'}
