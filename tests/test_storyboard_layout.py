"""B2 框图：结构校验、哈希稳定、进依据、清除与覆盖。

要点：
* bbox 是归一化画面坐标（0..1，原点左上，x0<x1、y0<y1）；depth 是数值；
* 框图只能挂在已存在的时间轴关键帧上，改时刻/删关键帧时它跟着走；
* 哈希由内容算：同内容同哈希，改一点即变；
* frame_diagram 进生成依据：改框图才让已生成的帧判「依据已变化」。
"""
import copy
import json

import pytest
from PIL import Image
from test_keyframe_plan import board as board  # 同一套「三镜＋每镜首帧」夹具
from test_screenplay_writing import project as project
from test_storyboard_operations import setup_board

from narrative.storyboard_art import changed_basis_keys
from narrative.storyboard_layout import layout_hash, normalize_layout

pytestmark = pytest.mark.slow

BOX = {'shot_id': 'shot-placeholder', 'offset_ms': 0,
       'camera': {'x': 0.4, 'y': 1.6, 'z': 0.2, 'yaw': 12, 'pitch': -4, 'lens_mm': 35, 'framing': 'medium'},
       'objects': [{'asset_id': 'A1', 'bbox': [0.12, 0.24, 0.4, 0.92], 'depth': 2.4, 'kind': 'person'},
                   {'asset_id': 'A2', 'bbox': [0.55, 0.5, 0.86, 0.78], 'depth': 5.1, 'kind': 'prop'}],
       'unmet_needs': [{'kind': 'prop', 'name': '伞'}], 'source': 'manual'}


def box(shot_id='shot-1', offset_ms=0, **changes):
    return copy.deepcopy(BOX | {'shot_id': shot_id, 'offset_ms': offset_ms, **changes})


def apply(ops, unit, changes):
    """改框图走内容白名单：非法内容进暂存（返回 staged），合法内容直接入库。"""
    return ops.apply_storyboard_changes(unit, ops.read_storyboard(unit)['revision'], changes)


def staged(ops, unit, changes):
    result = apply(ops, unit, changes)
    assert result['status'] == 'staged', result
    assert result['problems'], result
    return result


def stored(ops, unit):
    with ops._store.connect() as db:
        return json.loads(db.execute("SELECT value FROM meta WHERE key=?", ("storyboard:" + unit,)).fetchone()[0])


def test_layout_round_trip_view_and_storage(board):
    ops, unit, _b = board
    shot = ops.read_storyboard(unit)['shots'][0]['id']
    written = apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0,
                                'layout': box(shot, 0)}])
    assert written['status'] in (None, 'accepted', 'unchanged'), written
    view = ops.read_storyboard(unit)['shots'][0]
    layout = view['timeline']['keyframes'][0]['layout']
    # 机位补默认值、数值一律 float（int/float 混存会算出两个哈希）。
    assert layout['camera'] == {'x': 0.4, 'y': 1.6, 'z': 0.2, 'yaw': 12.0, 'pitch': -4.0,
                                'lens_mm': 35.0, 'framing': 'medium'}
    assert layout['objects'][1]['bbox'] == [0.55, 0.5, 0.86, 0.78]
    assert layout['unmet_needs'] == [{'kind': 'prop', 'name': '伞'}]
    assert layout['shot_id'] == shot and layout['offset_ms'] == 0
    assert layout['hash'] == layout_hash(layout)
    # 存储键是该帧时刻的字符串形式：正好对齐 JSON 对象键。
    assert list(stored(ops, unit)['shots'][0]['layouts']) == ['0']
    # 只读工具给出同一份框图与哈希。
    read = ops.read_storyboard_layout(unit, shot)
    assert read['count'] == 1 and read['layouts'][0]['offset_ms'] == 0
    assert read['layouts'][0]['hash'] == layout['hash'] and read['layouts'][0]['layout'] == layout
    assert read['keyframe_offsets'] == [0]


@pytest.mark.parametrize('layout, message', [
    ({'objects': [{'asset_id': 'A1', 'bbox': [0.1, 0.2, 1.4, 0.9], 'depth': 1, 'kind': 'person'}]}, 'bbox'),
    ({'objects': [{'asset_id': 'A1', 'bbox': [0.6, 0.2, 0.4, 0.9], 'depth': 1, 'kind': 'person'}]}, 'x0<x1'),
    ({'objects': [{'asset_id': 'A1', 'bbox': [0.1, 0.5, 0.4, 0.4], 'depth': 1, 'kind': 'person'}]}, 'x0<x1'),
    ({'objects': [{'asset_id': 'A1', 'bbox': [0.1, 0.2, 0.4, 0.9], 'depth': 'far', 'kind': 'person'}]}, 'depth'),
    ({'objects': [{'asset_id': 'A1', 'bbox': [0.1, 0.2, 0.4, 0.9], 'depth': 1}]}, 'kind'),
    ({'objects': [{'asset_id': 'A1', 'bbox': [0.1, 0.2, 0.4, 0.9], 'depth': 1, 'kind': 'vehicle'}]}, 'kind'),
    ({'objects': [{'bbox': [0.1, 0.2, 0.4, 0.9], 'depth': 1, 'kind': 'person'}]}, 'asset_id'),
    ({'camera': {'lens_mm': 0}}, 'lens_mm'),
    ({'unmet_needs': [{'kind': 'vehicle', 'name': '车'}]}, '需求'),
])
def test_layout_content_is_validated_before_anything_is_written(board, layout, message):
    ops, unit, _b = board
    shot = ops.read_storyboard(unit)['shots'][0]['id']
    before = ops.read_storyboard(unit)
    result = staged(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0, 'layout': layout}])
    assert any(message in str(problem.get('message', '')) for problem in result['problems']), result['problems']
    assert ops.read_storyboard(unit) == before


def test_layout_offset_must_be_an_existing_keyframe(board):
    ops, unit, _b = board
    shot = ops.read_storyboard(unit)['shots'][0]['id']
    before = ops.read_storyboard(unit)
    result = staged(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 900,
                                'layout': box(shot, 900)}])
    assert any('已存在的关键帧' in problem['message'] for problem in result['problems'])
    assert ops.read_storyboard(unit) == before
    # 建了该时刻的计划关键帧就成立。
    apply(ops, unit, [{'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 900, 'moment_name': '递伞'}])
    written = apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 900,
                                'layout': box(shot, 900)}])
    assert written['status'] in (None, 'accepted', 'unchanged'), written
    assert ops.read_storyboard_layout(unit, shot)['layouts'][0]['offset_ms'] == 900
    with pytest.raises(ValueError, match='不属于当前场'):
        apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': 'shot-999', 'offset_ms': 0,
                          'layout': box('shot-999', 0)}])


def test_hash_is_stable_and_changes_with_content():
    first = normalize_layout(box())
    # 字段顺序不同、整数字面不同都该算同一张框图：哈希只认内容。
    reordered = dict(reversed(list(box().items())))
    reordered['camera'] = dict(reversed(list(box()['camera'].items())))
    assert layout_hash(first) == layout_hash(normalize_layout(reordered)) == first['hash']
    assert layout_hash(first) == layout_hash(first | {'hash': 'ignored'})
    moved = normalize_layout(box() | {'objects': [dict(box()['objects'][0], bbox=[0.12, 0.24, 0.41, 0.92]),
                                               box()['objects'][1]]})
    assert moved['hash'] != first['hash']
    deeper = normalize_layout(box() | {'objects': [dict(box()['objects'][0], depth=2.5), box()['objects'][1]]})
    assert deeper['hash'] != first['hash']
    angled = normalize_layout(box() | {'camera': box()['camera'] | {'yaw': 13}})
    assert angled['hash'] != first['hash']
    assert len(first['hash']) == 16


def test_layout_feeds_the_sketch_basis_and_only_its_change_marks_the_frame(project, monkeypatch, tmp_path):
    ops, unit, block = setup_board(project)
    image = tmp_path / 'frame.png'
    Image.new('RGB', (20, 12), 'gray').save(image)
    shot = ops.apply_storyboard_changes(unit, 0, [
        {'op': 'create_shot', 'title': '窗边等待', 'duration_ms': 4000, 'source_block_ids': [block]}])['shots'][0]['id']
    ops.apply_storyboard_changes(unit, ops.read_storyboard(unit)['revision'],
                                 [{'op': 'set_keyframe', 'shot_id': shot, 'offset_ms': 0, 'sketch_path': str(image)}])
    apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0, 'layout': box(shot, 0)}])
    current = ops.read_storyboard(unit)
    row = next(item for item in current['shots'] if item['id'] == shot)
    basis, _references = ops._storyboard_sketch_basis(current, row, 0, ops.read_script_unit_context(unit))
    diagram = basis['frame_diagram']
    # 有框图时 sha256 用框图哈希，草图文件哈希另记一栏；frame_id 是可读的「镜号@时刻」。
    assert diagram['frame_id'] == f'{shot}@0' and diagram['sha256'] == row['timeline']['keyframes'][0]['layout']['hash']
    assert diagram['sketch_sha256'] and diagram['sketch_sha256'] != diagram['sha256']
    monkeypatch.setattr('narrative.storyboard_style_provider.render_sketch_image', stub())
    generated = ops.generate_storyboard_sketch(unit, shot, 0, {}, current['revision'])
    assert 'sketch_basis_changed' not in codes(ops, unit)
    recorded = generated['shots'][0]['keyframes'][0]['sketch_basis_hash']
    # 只改框图（不动草图文件）：依据变，帧被判需要重画，旧图原样保留。
    apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0,
                      'layout': box(shot, 0) | {'camera': box(shot, 0)['camera'] | {'yaw': 40}}}])
    after = ops.read_storyboard(unit)['shots'][0]
    assert 'sketch_basis_changed' in codes(ops, unit)
    current_basis, _ = ops._storyboard_sketch_basis(ops.read_storyboard(unit), after, 0,
                                                   ops.read_script_unit_context(unit))
    assert changed_basis_keys(basis, current_basis) == ['frame_diagram']
    assert after['keyframes'][0]['sketch_basis_hash'] == recorded
    assert after['keyframes'][0]['sketch_path'] == generated['shots'][0]['keyframes'][0]['sketch_path']


def stub():
    def render(guide, prompt, copies_to, reference_images=()):
        Image.new('RGB', (24, 14), 'gray').save(copies_to)
        return {'provider': 'stub-only', 'job_id': 'job-layout'}
    return render


def codes(ops, unit):
    return {row['code'] for row in ops.check_storyboard(unit)['diagnostics']}


def test_overwrite_clear_and_clear_all(board):
    ops, unit, _b = board
    shot = ops.read_storyboard(unit)['shots'][0]['id']
    first = apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0,
                              'layout': box(shot, 0)}])['shots'][0]['timeline']['keyframes'][0]['layout']
    second = apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0,
                               'layout': box(shot, 0) | {'camera': box(shot, 0)['camera'] | {'framing': 'close'}}}]
                   )['shots'][0]['timeline']['keyframes'][0]['layout']
    assert second['camera']['framing'] == 'close' and second['hash'] != first['hash']
    # 同一镜另一帧的框图互不影响。
    apply(ops, unit, [{'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 900}])
    apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 900, 'layout': box(shot, 900)}])
    assert [row['offset_ms'] for row in ops.read_storyboard_layout(unit, shot)['layouts']] == [0, 900]
    # layout=null 清该帧；清全部用 clear_keyframe_layouts。
    cleared = apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0, 'layout': None}])
    assert [row['offset_ms'] for row in ops.read_storyboard_layout(unit, shot)['layouts']] == [900]
    assert cleared['shots'][1]['timeline']['keyframes'][0]['layout'] is None
    apply(ops, unit, [{'op': 'clear_keyframe_layouts', 'shot_id': shot}])
    assert ops.read_storyboard_layout(unit, shot)['layouts'] == []
    assert 'layouts' not in stored(ops, unit)['shots'][0]
    # 其它镜头一点没动。
    assert 'layouts' not in stored(ops, unit)['shots'][1]


def test_layout_follows_keyframe_move_and_delete(board):
    ops, unit, _b = board
    shot = ops.read_storyboard(unit)['shots'][0]['id']
    apply(ops, unit, [{'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': 900}])
    apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 900, 'layout': box(shot, 900)}])
    # 时间轴 op 是即时命令（不在暂存白名单），返回的就是读视图本身。
    apply(ops, unit, [{'op': 'update_timeline_keyframe', 'shot_id': shot, 'offset_ms': 900,
                      'new_offset_ms': 1200}])
    assert [row['offset_ms'] for row in ops.read_storyboard_layout(unit, shot)['layouts']] == [1200]
    assert ops.read_storyboard_layout(unit, shot)['layouts'][0]['layout']['offset_ms'] == 1200
    # 关键帧删掉，它的框图不能变成读不到的孤儿数据。
    apply(ops, unit, [{'op': 'delete_timeline_keyframe', 'shot_id': shot, 'offset_ms': 1200}])
    assert ops.read_storyboard_layout(unit, shot)['layouts'] == []


def test_derived_timeline_layout_follows_the_still_frame(board):
    """老场次没有显式计划时，时间轴由静帧推导：静帧动了/删了，框图跟着走，不留孤儿。"""
    ops, unit, b = board
    shot = b['shots'][0]['id']
    apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0, 'layout': box(shot, 0)}])
    frame_id = ops.read_storyboard(unit)['shots'][0]['keyframes'][0]['id']
    ops.apply_storyboard_changes(unit, ops.read_storyboard(unit)['revision'],
                                 [{'op': 'move_keyframe', 'shot_id': shot, 'frame_id': frame_id, 'offset_ms': 500}])
    assert [row['offset_ms'] for row in ops.read_storyboard_layout(unit, shot)['layouts']] == [500]
    ops.apply_storyboard_changes(unit, ops.read_storyboard(unit)['revision'],
                                 [{'op': 'delete_keyframe', 'shot_id': shot, 'frame_id': frame_id}])
    assert ops.read_storyboard_layout(unit, shot)['layouts'] == []


def test_preview_simulates_layout_edits_without_writing(board):
    ops, unit, _b = board
    shot = ops.read_storyboard(unit)['shots'][0]['id']
    apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0, 'layout': box(shot, 0)}])
    before = ops.read_storyboard(unit)
    preview = ops.preview_storyboard_changes(unit, before['revision'], [
        {'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0,
         'layout': box(shot, 0) | {'camera': box(shot, 0)['camera'] | {'framing': 'wide'}}}])
    assert preview['valid'] and preview['conflicts'] == []
    assert preview['preview']['shots'][0]['timeline']['keyframes'][0]['layout']['camera']['framing'] == 'wide'
    assert ops.read_storyboard(unit) == before


def test_approved_scene_must_be_unapproved_first(board):
    ops, unit, b = board
    shot = b['shots'][0]['id']
    framed = apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0, 'layout': box(shot, 0)}])
    approved = ops.apply_storyboard_changes(unit, framed['revision'], [{'op': 'set_approved', 'approved': True}])
    with pytest.raises(ValueError, match='解除定案'):
        ops.apply_storyboard_changes(unit, approved['revision'], [
            {'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0, 'layout': box(shot, 0)}])


def test_tool_schema_exposes_layout_ops_and_layout_read(board):
    from narrative import tool_api
    from narrative.project_package import layout as package_layout

    ops, unit, b = board
    entry = package_layout(ops.project_info().path).entry
    shot = b['shots'][0]['id']
    written = tool_api.call('apply_storyboard_changes', {'unit_id': unit, 'revision': b['revision'], 'changes': [
        {'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0, 'layout': box(shot, 0)}]}, project=entry)
    assert written['shots'][0]['timeline']['keyframes'][0]['layout']['hash']
    read = tool_api.call('read_storyboard_layout', {'unit_id': unit, 'shot_id': shot}, project=entry)
    assert read['count'] == 1 and read['layouts'][0]['hash'] == written['shots'][0]['timeline']['keyframes'][0]['layout']['hash']
