"""地点布景资产（``asset['set']``）与场依赖（``unit.set_ref``）：数据层＋假预演页。

钉住的行为：

* 布景存/读、``set.revision`` 只由程序 +1、非法字段整份拒绝（拒绝时不留半套布景）；
* 场绑定/解绑只写 ``set_ref``，绑到非地点资产被拒，绑与解绑都过 revision 校验；
* 依赖状态五种取值（bound/stale/no_set/missing_asset/unbound）全部由现存数据现算；
* 缺口视图出现 ``set_missing``，``set_stale`` 只作提示且不掺进 ``counts``；
* ``apply_location_set`` 只发一次 ``applyBatch``；批量失败时页面/资产/场都不变；
  铺成功后页面生成的实体编号写回资产（机位按 ``camera_id``、盒体按序号）；
* 同一套布景被两场复用：两场的绑定与状态一致，改版后一起变 stale。
"""
from __future__ import annotations

import math

import pytest
from test_previs_host import FakePage, install
from test_screenplay_writing import project as project
from test_screenplay_writing import table as scene_table
from test_storyboard_operations import setup_board

from narrative import previs_host
from narrative.content_errors import ContentConflictError, ContentGuardError
from narrative.previs_host import PrevisHost

# 一套「办公室」：两面墙 + 两台机位。坐标用米、y 向上，与预演同一套口径。
OFFICE_BLOCKS = [{'kind': 'prop', 'x': 0.0, 'z': -3.0, 'w': 4.0, 'd': 0.2, 'h': 2.8},
                 {'kind': 'prop', 'x': 1.5, 'z': 0.5, 'w': 0.8, 'd': 0.8, 'h': 0.75, 'rotation': 90}]
OFFICE_CAMERAS = [{'name': 'A · 主机位', 'position': [0.0, 1.6, 4.0], 'target': [0.0, 1.2, 0.0], 'focal': 35},
                  {'camera_id': 'cam-b', 'name': 'B · 过肩', 'position': [2.0, 1.5, 1.0],
                   'target': [0.0, 1.4, -1.0], 'focal': 85, 'framing': 'close'}]
OFFICE_ROOM = {'w': 6.0, 'd': 5.0, 'h': 3.0}


@pytest.fixture(autouse=True)
def _no_leftover_host():
    """进程内预演宿主是单例：每个用例前后都清掉，免得互相串。"""
    previs_host.uninstall()
    yield
    previs_host.uninstall()


def office(ops):
    return ops.save_asset({'category': 'location', 'title': '办公室'})


def write_set(ops, asset_id, **overrides):
    params = {'blocks': OFFICE_BLOCKS, 'cameras': OFFICE_CAMERAS, 'room': OFFICE_ROOM}
    return ops.save_location_set(asset_id, **(params | overrides))


def test_set_round_trip_and_revision_is_program_owned(project):
    ops, _unit, _block = setup_board(project)
    asset = office(ops)
    assert ops.get_asset(asset['id']).get('set') is None

    first = write_set(ops, asset['id'])
    row = first['set']
    assert first['set_revision'] == row['revision'] == 1
    assert row['room'] == {'w': 6.0, 'd': 5.0, 'h': 3.0}
    assert [block['kind'] for block in row['blocks']] == ['prop', 'prop']
    # 省略的高度按预演默认补上（道具 1 米），y 缺省是地面。
    assert row['blocks'][0]['y'] == 0.0 and row['blocks'][0]['h'] == 2.8
    # 机位编号由程序发放；给了 camera_id 的照用，位置与焦距规范成浮点。
    assert [camera['camera_id'] for camera in row['cameras']] == ['cam-1', 'cam-b']
    assert row['cameras'][1]['framing'] == 'close'
    assert row['cameras'][0]['focal'] == 35.0
    # updated_at 是程序给的时间戳，不是调用方传进来的。
    assert row['updated_at']

    card = ops.get_asset(asset['id'])
    assert card['set'] == row
    read = ops.read_location_set(asset['id'])
    assert read['set'] == row and read['used_by'] == []
    assert read['asset_revision'] == card['revision']

    # 再写一次：revision 只由程序 +1；同名机位沿用原编号（改名不改身份，镜头绑定才稳）。
    second = write_set(ops, asset['id'])
    assert second['set_revision'] == 2
    assert [camera['camera_id'] for camera in second['set']['cameras']] == ['cam-1', 'cam-b']
    # 布景不能随资产卡一起保存：那条路会绕开 revision 自增与校验。
    with pytest.raises(ContentGuardError, match='save_location_set'):
        ops.save_asset({'id': asset['id'], 'title': '办公室', 'set': row})


def test_invalid_set_fields_are_rejected_without_writing(project):
    ops, _unit, _block = setup_board(project)
    asset = office(ops)
    cases = [
        ({'room': {'w': 0, 'd': 5, 'h': 3}}, 'room.w'),
        ({'room': {'w': 6, 'd': 5}}, 'room须为'),
        ({'blocks': [{'kind': 'wall', 'x': 0, 'z': 0, 'w': 1, 'd': 1}]}, 'kind须为'),
        ({'blocks': [{'kind': 'prop', 'x': 0, 'z': 0, 'w': -1, 'd': 1}]}, 'blocks第1项的w'),
        ({'blocks': [{'kind': 'prop', 'x': 0, 'z': 0, 'w': 1, 'd': 1, 'name': '墙'}]}, '不认识的字段'),
        ({'blocks': {'kind': 'prop'}}, 'blocks须为数组'),
        ({'cameras': [{'name': 'A', 'position': [0, 1, 0]}]}, 'target'),
        ({'cameras': [{'name': 'A', 'position': [0, 1, 0], 'target': [0, 1, 1], 'focal': 0}]}, 'focal须大于0'),
        ({'cameras': [{'name': 'A', 'position': [0, 1, 0], 'target': [0, 1, 1], 'focal': 35},
                      {'name': 'A', 'position': [1, 1, 0], 'target': [0, 1, 1], 'focal': 35}]}, 'name不能重复'),
        ({'cameras': [{'name': 'A', 'position': [0, 1, 0], 'target': [0, 1, 1], 'focal': 35,
                       'previs_entity_id': 'gen-1'}]}, '不认识的字段'),
    ]
    for overrides, message in cases:
        with pytest.raises(ValueError, match=message):
            write_set(ops, asset['id'], **overrides)
        # 整份拒绝：一次失败的写入不该在资产里留下半套布景。
        assert ops.get_asset(asset['id']).get('set') is None
    # 只有地点资产才有布景这回事。
    item = ops.save_asset({'category': 'item', 'title': '雨伞'})
    with pytest.raises(ContentGuardError, match='地点'):
        ops.save_location_set(item['id'], blocks=[], cameras=[])
    with pytest.raises(ContentGuardError, match='地点'):
        ops.read_location_set(item['id'])


def test_bind_and_unbind_unit_set(project):
    ops, unit, _block = setup_board(project)
    asset = office(ops)
    board = ops.read_storyboard(unit)
    assert board['set_ref'] is None and ops.unit_set_status(unit)['status'] == 'unbound'
    # 省略 asset_id 与显式 null 是两件事：一个是忘写，一个是解绑。
    with pytest.raises(ValueError, match='asset_id'):
        ops.bind_unit_set(unit, board['revision'])
    with pytest.raises(ContentConflictError, match='刷新'):
        ops.bind_unit_set(unit, board['revision'] + 1, asset['id'])
    item = ops.save_asset({'category': 'item', 'title': '雨伞'})
    with pytest.raises(ContentGuardError, match='地点'):
        ops.bind_unit_set(unit, board['revision'], item['id'])
    assert ops.read_storyboard(unit)['set_ref'] is None

    # 资产还没写布景时也允许绑：状态是 no_set，缺口视图会提示先写一套。
    bound = ops.bind_unit_set(unit, board['revision'], asset['id'])
    assert bound['set_ref'] == {'asset_id': asset['id'], 'revision': 0}
    assert bound['dependency']['status'] == 'no_set'
    assert bound['storyboard_revision'] == board['revision'] + 1
    assert ops.read_storyboard(unit)['set_ref'] == {'asset_id': asset['id'], 'revision': 0}

    write_set(ops, asset['id'])
    current = ops.read_storyboard(unit)
    rebound = ops.bind_unit_set(unit, current['revision'], asset['id'])
    assert rebound['set_ref']['revision'] == 1 and rebound['dependency']['status'] == 'bound'

    unbound = ops.bind_unit_set(unit, rebound['storyboard_revision'], None)
    assert unbound['set_ref'] is None and unbound['dependency']['status'] == 'unbound'
    assert ops.read_storyboard(unit)['set_ref'] is None


def test_dependency_status_has_five_values(project):
    ops, unit, _block = setup_board(project)
    asset = office(ops)
    assert ops.unit_set_status(unit)['status'] == 'unbound'

    ops.bind_unit_set(unit, ops.read_storyboard(unit)['revision'], asset['id'])
    assert ops.unit_set_status(unit)['status'] == 'no_set'

    write_set(ops, asset['id'])
    # 绑在「还没有布景」的第 0 版，资产已到第 1 版：这正是 stale 该提醒的事。
    stale = ops.unit_set_status(unit)
    assert stale['status'] == 'stale' and stale['bound_revision'] == 0 and stale['revision'] == 1
    assert '提示' in stale['message']

    ops.bind_unit_set(unit, ops.read_storyboard(unit)['revision'], asset['id'])
    assert ops.unit_set_status(unit)['status'] == 'bound'
    write_set(ops, asset['id'])
    assert ops.unit_set_status(unit)['status'] == 'stale'

    # 资产被删掉：绑定还在，资产没了 → missing_asset（不是异常）。
    doomed = office(ops)
    ops.bind_unit_set(unit, ops.read_storyboard(unit)['revision'], doomed['id'])
    ops.delete_asset(doomed['id'])
    missing = ops.unit_set_status(unit)
    assert missing['status'] == 'missing_asset' and missing['asset_id'] == doomed['id']

    assert ops.bind_unit_set(unit, ops.read_storyboard(unit)['revision'], None)['dependency']['status'] == 'unbound'


def test_gap_view_lists_set_missing_and_keeps_stale_as_a_hint(project):
    ops, unit, _block = setup_board(project)
    asset = office(ops)
    gap = ops.read_asset_needs(unit)
    assert [row['status'] for row in gap['set_missing']] == ['unbound'] and gap['set_stale'] == []
    # counts 只说需求与引用：布景并进去但它们不能多出键（界面按固定字段读）。
    assert gap['counts'] == {'unit': 0, 'shot': 0, 'total': 0, 'resolved': 0,
                             'unresolved': 0, 'dangling': 0}
    assert gap['set_dependency']['status'] == 'unbound'

    ops.bind_unit_set(unit, ops.read_storyboard(unit)['revision'], asset['id'])
    assert [row['status'] for row in ops.read_asset_needs(unit)['set_missing']] == ['no_set']

    write_set(ops, asset['id'])
    ops.bind_unit_set(unit, ops.read_storyboard(unit)['revision'], asset['id'])
    gap = ops.read_asset_needs(unit)
    assert gap['set_missing'] == [] and gap['set_stale'] == []
    assert gap['set_dependency']['status'] == 'bound'

    write_set(ops, asset['id'])
    gap = ops.read_asset_needs(unit)
    # 资产改过布景：只提示 stale，不算缺口（不拦写作、不进 set_missing）。
    assert gap['set_missing'] == []
    assert [row['status'] for row in gap['set_stale']] == ['stale']
    assert gap['counts']['total'] == 0


def test_apply_location_set_pours_once_and_remembers_entity_ids(project):
    ops, unit, _block = setup_board(project)
    asset = office(ops)
    write_set(ops, asset['id'])
    ops.bind_unit_set(unit, ops.read_storyboard(unit)['revision'], asset['id'])
    page = FakePage([])
    install(PrevisHost(page))

    applied = ops.apply_location_set(unit)
    assert len(page.sequences) == 1                     # 一次 applyBatch，没有中间态
    sent = page.sequences[0]
    assert [row['op'] for row in sent] == ['add', 'add', 'add', 'add']   # 2 面墙 + 2 台机位
    assert sent[0]['kind'] == 'prop' and sent[0]['scale'] == [4.0, 2.8, 0.2]
    assert sent[1]['rotation'] == [0.0, math.radians(90), 0.0]           # 角度制 → 预演弧度
    assert sent[2]['kind'] == 'camera' and sent[2]['camera']['focal'] == 35.0
    assert applied['camera_map'] == {'cam-1': 'gen-3', 'cam-b': 'gen-4'}
    assert applied['camera_ids_written'] == [{'camera_id': 'cam-1', 'previs_entity_id': 'gen-3'},
                                             {'camera_id': 'cam-b', 'previs_entity_id': 'gen-4'}]
    assert [row['index'] for row in applied['block_ids_written']] == [0, 1]
    # camera_id 映射落进资产；但布景内容没变，set.revision 不该跳（否则自己制造 stale）。
    stored = ops.read_location_set(asset['id'])['set']
    assert [camera['previs_entity_id'] for camera in stored['cameras']] == ['gen-3', 'gen-4']
    assert stored['revision'] == applied['set_revision'] == 1
    assert [block['previs_entity_id'] for block in stored['blocks']] == ['gen-1', 'gen-2']

    # 再铺一次不会把同一套布景添第二遍：带编号就地更新。
    again = ops.apply_location_set(unit)
    second = page.sequences[1]
    assert [row['op'] for row in second] == ['update', 'update', 'setCamera', 'setCamera']
    assert len(page.scene['entities']) == 4
    assert again['camera_map'] == {'cam-1': 'gen-3', 'cam-b': 'gen-4'}
    assert again['dependencies']['status'] == 'bound'


def test_failed_batch_leaves_asset_and_unit_untouched(project):
    ops, unit, _block = setup_board(project)
    asset = office(ops)
    write_set(ops, asset['id'])
    ops.bind_unit_set(unit, ops.read_storyboard(unit)['revision'], asset['id'])
    page = FakePage([])
    page.batch_error = '第2条操作不生效：实体gen-9不存在；整批已回滚'
    install(PrevisHost(page))
    before = ops.read_storyboard(unit)
    with pytest.raises(ValueError, match='第2条操作'):
        ops.apply_location_set(unit)
    assert page.sequences == []                          # 页面没应用任何一批
    assert ops.read_storyboard(unit) == before           # 场一个字节没动
    stored = ops.read_location_set(asset['id'])['set']
    assert stored['revision'] == 1
    # 失败不该留下半个映射：机位与盒体都没有实体编号。
    assert all('previs_entity_id' not in camera for camera in stored['cameras'])
    assert all('previs_entity_id' not in block for block in stored['blocks'])


def test_apply_requires_a_bound_set_and_an_open_page(project):
    ops, unit, _block = setup_board(project)
    page = FakePage([])
    install(PrevisHost(page))
    with pytest.raises(ContentGuardError, match='bind_unit_set'):
        ops.apply_location_set(unit)
    asset = office(ops)
    ops.bind_unit_set(unit, ops.read_storyboard(unit)['revision'], asset['id'])
    with pytest.raises(ContentGuardError, match='save_location_set'):
        ops.apply_location_set(unit)
    assert page.sequences == []
    # 预演台没开：交给 set_scene_layout 报「先打开预演页」，不在这里另造一句。
    write_set(ops, asset['id'])
    previs_host.uninstall()
    with pytest.raises(ValueError, match='预演台'):
        ops.apply_location_set(unit)


def test_one_set_is_shared_by_two_units(project):
    ops, _screenplay, segment_id = project
    units = [row['id'] for row in scene_table(ops, segment_id)['units']]
    first, second = units[0], units[1]
    asset = office(ops)
    write_set(ops, asset['id'])
    for unit in (first, second):
        ops.bind_unit_set(unit, ops.read_storyboard(unit)['revision'], asset['id'])
    one, two = ops.read_unit_set(first), ops.read_unit_set(second)
    assert one['status'] == two['status'] == 'bound'
    assert one['set_ref'] == two['set_ref'] == {'asset_id': asset['id'], 'revision': 1}
    assert one['block_count'] == two['block_count'] == 2
    assert one['camera_count'] == two['camera_count'] == 2
    assert one['asset']['title'] == two['asset']['title'] == '办公室'
    # 资产改版：两场同时变 stale，状态一致（同一套布景，两个场看到的是同一份判断）。
    write_set(ops, asset['id'])
    assert ops.unit_set_status(first)['status'] == ops.unit_set_status(second)['status'] == 'stale'
    assert {row['unit_id'] for row in ops.read_location_set(asset['id'])['used_by']} == {first, second}


def test_tools_are_registered_and_read_only_ones_do_not_write(project):
    from narrative import tool_api
    from narrative.project_package import layout

    ops, unit, _block = setup_board(project)
    asset = office(ops)
    names = {'save_location_set', 'read_location_set', 'bind_unit_set', 'read_unit_set', 'apply_location_set'}
    assert names <= tool_api.TOOLS.keys()
    assert tool_api.TOOLS['read_unit_set'].writes is False
    assert tool_api.TOOLS['read_location_set'].writes is False
    assert tool_api.TOOLS['bind_unit_set'].writes is True and tool_api.TOOLS['apply_location_set'].writes is True

    entry = layout(ops.project_info().path).entry
    saved = tool_api.call('save_location_set', {'asset_id': asset['id'], 'room': OFFICE_ROOM,
                                                'blocks': OFFICE_BLOCKS,
                                                'cameras': [OFFICE_CAMERAS[1]]}, project=entry)
    assert saved['set_revision'] == 1
    revision = tool_api.call('read_storyboard', {'unit_id': unit}, project=entry)['revision']
    tool_api.call('bind_unit_set', {'unit_id': unit, 'revision': revision, 'asset_id': asset['id']},
                  project=entry)
    read = tool_api.call('read_unit_set', {'unit_id': unit}, project=entry)
    assert read['status'] == 'bound' and read['camera_count'] == 1
    assert read['cameras'][0]['camera_id'] == 'cam-b'
    # 显式 null 走解绑（省略 asset_id 是另一件事，见上面的直接调用用例）。
    unbound = tool_api.call('bind_unit_set', {'unit_id': unit, 'revision': read['storyboard_revision'],
                                              'asset_id': None}, project=entry)
    assert unbound['set_ref'] is None and unbound['dependency']['status'] == 'unbound'
    # 嵌套字段照发布 schema 校验：布景盒体不许夹带私货（name 由程序生成，不由调用方给）。
    from narrative.tool_api import ToolError

    with pytest.raises(ToolError, match='不认识的字段'):
        tool_api.call('save_location_set', {'asset_id': asset['id'], 'cameras': [],
                                            'blocks': [OFFICE_BLOCKS[0] | {'name': '墙'}]}, project=entry)
