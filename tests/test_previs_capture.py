"""阶段 C：预演接进分镜的三个写入口径。

钉住的是「预演怎么变成框图」这条链上的行为，不是投影数学（那在 test_previs_scene）：

* 找不到 `.director` 时错误消息要说清「放哪、还缺什么」，而不是只报「不存在」；
* `set_shot_previs` 只写 `shot.previs` 这一条映射，能绑能解，过期修订被拒；
* `capture_previs_layout` 写进去的框图必须能被 `read_storyboard_layout` 读到、
  `timeline.keyframes[i].layout` 不为 null、bbox 在 0..1、objects 按 depth 从远到近；
* 已有**手工**框图默认不许覆盖，`overwrite=true` 才换；
* 改框图会让已生成的帧判「依据已变化」，改二维调度不会（与阶段 B 的依据口径一致）。
"""
import json

import pytest
from PIL import Image
from test_previs_scene import ASPECT, actor, camera, flat
from test_screenplay_writing import project as project
from test_storyboard_operations import setup_board

from narrative.project_package import layout

pytestmark = pytest.mark.slow

CAMERA_ID = 'cam-a'
MANUAL_BOX = {'camera': {'x': 0.4, 'y': 1.6, 'z': 0.2, 'yaw': 12, 'pitch': -4, 'lens_mm': 35, 'framing': 'medium'},
              'objects': [{'asset_id': 'A1', 'bbox': [0.12, 0.24, 0.4, 0.92], 'depth': 2.4, 'kind': 'person'}],
              'unmet_needs': [], 'source': 'manual'}


def previs_dir(ops, unit):
    """预演目录＝工程数据根/previs/<场次编号>；与 previs_tools 的约定同一处。"""
    return layout(ops._store.path).root / 'previs' / unit


def write_previs(ops, unit, payload, name='scene.director'):
    directory = previs_dir(ops, unit)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return path


def shot_with_keyframes(ops, unit, block, offsets=(0,), duration=4000):
    """建一镜并给定时点建时间轴关键帧：框图只能挂在这些时刻上。"""
    shot = ops.apply_storyboard_changes(unit, revision(ops, unit), [
        {'op': 'create_shot', 'title': '窗边等待', 'duration_ms': duration, 'source_block_ids': [block]}])['shots'][-1]['id']
    for offset in offsets:
        ops.apply_storyboard_changes(unit, ops.read_storyboard(unit)['revision'], [
            {'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': offset}])
    return shot


def revision(ops, unit):
    return ops.read_storyboard(unit)['revision']


def codes(ops, unit):
    return {row['code'] for row in ops.check_storyboard(unit)['diagnostics']}


# -- read_previs --------------------------------------------------------------

def test_read_previs_says_exactly_where_the_project_goes(project):
    ops, unit, _block = setup_board(project)
    directory = previs_dir(ops, unit)
    with pytest.raises(ValueError) as error:
        ops.read_previs(unit)
    message = str(error.value)
    assert str(directory) in message                       # 放哪
    assert '.director' in message                          # 放什么文件
    assert 'NARRATIVE_PREVIS_PROJECT_ROOT' in message      # 还能换到哪
    assert '还没有预演工程' in message
    assert not directory.exists()                          # 只读工具不建目录


def test_read_previs_summarizes_the_scene(project):
    ops, unit, _block = setup_board(project)
    write_previs(ops, unit, flat([actor(), camera()], duration=12, name='窗边'))
    summary = ops.read_previs(unit)
    assert summary['name'] == '窗边' and summary['duration_s'] == 12.0
    assert summary['fps'] == 24 and summary['aspect'] == ASPECT
    assert summary['camera_count'] == 1 and summary['entity_count'] == 1 and summary['cut_count'] == 1
    assert summary['path'].endswith('scene.director')
    assert summary['files'] == ['scene.director']
    assert summary['cameras'] == [{'camera_id': CAMERA_ID, 'name': 'A · 主机位', 'focal': 35.0,
                                   'position': [0.0, 0.875, 5.0]}]
    assert summary['cuts'] == [{'time': 0.0, 'camera_id': CAMERA_ID}]
    assert summary['bindings'] == []


def test_multiple_projects_need_a_scene_id(project):
    ops, unit, _block = setup_board(project)
    write_previs(ops, unit, flat([actor(), camera()], name='第一场'), 'first.director')
    write_previs(ops, unit, flat([actor(), camera(focal=50)], name='第二场'), 'second.director')
    with pytest.raises(ValueError, match='多个预演工程'):
        ops.read_previs(unit)
    # 文件名就是场景编号：选了它就读那一份。
    assert ops.read_previs(unit, 'second')['name'] == '第二场'
    assert ops.read_previs(unit, 'second')['cameras'][0]['focal'] == 50.0
    # 选不到时把可选项列出来。
    with pytest.raises(ValueError, match='没有场景third'):
        ops.read_previs(unit, 'third')


def test_previs_root_can_be_overridden_by_environment(project, tmp_path, monkeypatch):
    ops, unit, _block = setup_board(project)
    somewhere = tmp_path / 'elsewhere'
    (somewhere / unit).mkdir(parents=True)
    (somewhere / unit / 'scene.director').write_text(
        json.dumps(flat([actor(), camera()], name='外部目录'), ensure_ascii=False), encoding='utf-8')
    monkeypatch.setenv('NARRATIVE_PREVIS_PROJECT_ROOT', str(somewhere))
    summary = ops.read_previs(unit)
    assert summary['name'] == '外部目录'
    assert summary['directory'] == str(somewhere / unit)


# -- set_shot_previs ----------------------------------------------------------

def test_set_shot_previs_binds_and_unbinds(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block)
    write_previs(ops, unit, flat([actor(), camera()]))
    bound = ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID, time_offset_ms=2000)
    assert bound['previs'] == {'scene_id': 'scene', 'camera_id': CAMERA_ID, 'time_offset_ms': 2000}
    # 绑定落盘：读视图与读库看到同一份。
    assert ops.read_storyboard(unit)['shots'][0]['previs'] == bound['previs']
    assert ops.read_previs(unit)['bindings'] == [{'shot_id': shot, 'title': '窗边等待', 'previs': bound['previs']}]
    # 过期修订不让写。
    with pytest.raises(ValueError, match='刷新'):
        ops.set_shot_previs(unit, bound['revision'] - 1, shot, CAMERA_ID)
    # 显式 null 解绑；省略 camera_id 是「没写」，不能被当成解绑。
    unbound = ops.set_shot_previs(unit, bound['revision'], shot, None)
    assert unbound['previs'] is None
    assert 'previs' not in ops.read_storyboard(unit)['shots'][0]
    with pytest.raises(ValueError, match='显式传null'):
        ops.set_shot_previs(unit, unbound['revision'], shot)
    # 解绑不涨过期：修订号一路推进。
    assert unbound['revision'] == bound['revision'] + 1


def test_set_shot_previs_refuses_a_camera_that_is_not_in_the_scene(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block)
    write_previs(ops, unit, flat([actor(), camera()]))
    before = ops.read_storyboard(unit)
    with pytest.raises(ValueError, match='没有摄影机cam-z'):
        ops.set_shot_previs(unit, before['revision'], shot, 'cam-z')
    assert ops.read_storyboard(unit) == before


def test_set_shot_previs_selects_the_scene_it_was_told(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block)
    write_previs(ops, unit, flat([actor(), camera()]), 'first.director')
    write_previs(ops, unit, flat([actor(), camera('cam-b', name='B · 侧机位')]), 'second.director')
    bound = ops.set_shot_previs(unit, revision(ops, unit), shot, 'cam-b', scene_id='second')
    assert bound['previs']['scene_id'] == 'second' and bound['previs']['camera_id'] == 'cam-b'


# -- 工具注册 ----------------------------------------------------------------

def test_previs_tools_are_registered_and_callable(project):
    """三个工具要走同一套 public API：只读的不许标 writes，写的能被工具层调到。"""
    from narrative import tool_api

    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block)
    write_previs(ops, unit, flat([actor(), camera()], name='窗边'))
    entry = layout(ops.project_info().path).entry
    for name, writes in (('read_previs', False), ('set_shot_previs', True), ('capture_previs_layout', True)):
        assert tool_api.TOOLS[name].writes is writes
    summary = tool_api.call('read_previs', {'unit_id': unit}, project=entry)
    assert summary['name'] == '窗边' and summary['camera_count'] == 1
    bound = tool_api.call('set_shot_previs', {'unit_id': unit, 'revision': revision(ops, unit),
                                              'shot_id': shot, 'camera_id': CAMERA_ID}, project=entry)
    assert bound['previs'] == {'scene_id': 'scene', 'camera_id': CAMERA_ID, 'time_offset_ms': 0}
    captured = tool_api.call('capture_previs_layout',
                             {'unit_id': unit, 'shot_id': shot, 'offset_ms': 0}, project=entry)
    assert captured['layout']['source'] == 'previs'
    # 显式 null 经工具层就是解绑。
    unbound = tool_api.call('set_shot_previs', {'unit_id': unit, 'revision': captured['revision'],
                                                'shot_id': shot, 'camera_id': None}, project=entry)
    assert unbound['previs'] is None
    # 省略 camera_id 要报「说清怎么办」的错，不能静默当成解绑。
    with pytest.raises(tool_api.ToolError, match='camera_id'):
        tool_api.call('set_shot_previs', {'unit_id': unit, 'revision': unbound['revision'], 'shot_id': shot},
                      project=entry)


# -- capture_previs_layout ----------------------------------------------------

def test_capture_writes_a_layout_the_read_view_returns(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block)
    write_previs(ops, unit, flat([actor(), camera()]))
    ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID)
    captured = ops.capture_previs_layout(unit, shot, 0)
    assert captured['camera_id'] == CAMERA_ID and captured['time_ms'] == 0
    assert captured['framing'] == captured['layout']['camera']['framing']
    # 读视图：这一帧的 layout 不再是 null（界面据此画近似 previs）。
    frame = ops.read_storyboard(unit)['shots'][0]['timeline']['keyframes'][0]
    assert frame['layout'] is not None and frame['layout']['source'] == 'previs'
    # 只读工具给出同一份框图与哈希。
    read = ops.read_storyboard_layout(unit, shot)
    assert read['count'] == 1 and read['layouts'][0]['offset_ms'] == 0
    assert read['layouts'][0]['layout'] == frame['layout']
    saved = frame['layout']
    assert saved['shot_id'] == shot and saved['offset_ms'] == 0 and saved['hash']
    assert saved['camera']['lens_mm'] == 35.0
    assert saved['camera']['framing'] in {'wide', 'medium', 'close'}
    # 每个对象：bbox 归一化且在画面内、depth 有值、按从远到近排序、来源标成预演实体。
    assert saved['objects']
    for row in saved['objects']:
        x0, y0, x1, y1 = row['bbox']
        assert 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0
        assert row['depth'] > 0 and row['kind'] in {'person', 'prop'}
        assert row['asset_id'].startswith('previs:')
    depths = [row['depth'] for row in saved['objects']]
    assert depths == sorted(depths, reverse=True)


def test_capture_uses_the_time_offset_of_the_binding(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block, offsets=(0, 1000))
    write_previs(ops, unit, flat([actor(), camera()], duration=10))
    ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID, time_offset_ms=3000)
    captured = ops.capture_previs_layout(unit, shot, 1000)
    # 镜内 1000 ms + 绑定起点 3000 ms = 预演第 4000 ms。
    assert captured['time_ms'] == 4000 and captured['layout']['offset_ms'] == 1000
    assert ops.read_storyboard_layout(unit, shot)['layouts'][0]['offset_ms'] == 1000


def test_capture_explains_a_missing_binding_or_keyframe(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block, offsets=(0,))
    write_previs(ops, unit, flat([actor(), camera()]))
    with pytest.raises(ValueError, match='还没有绑定预演机位'):
        ops.capture_previs_layout(unit, shot, 0)
    ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID)
    with pytest.raises(ValueError, match='没有关键帧'):
        ops.capture_previs_layout(unit, shot, 900)
    # 镜内时刻超出预演场景时长时，说清是哪一个数超了。
    short = shot_with_keyframes(ops, unit, block, offsets=(0, 1000))
    write_previs(ops, unit, flat([actor(), camera()], duration=2))
    ops.set_shot_previs(unit, revision(ops, unit), short, CAMERA_ID, time_offset_ms=1500)
    with pytest.raises(ValueError, match='超出了场景时长'):
        ops.capture_previs_layout(unit, short, 1000)


def test_capture_keeps_a_manual_layout_unless_overwrite_is_explicit(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block)
    write_previs(ops, unit, flat([actor(), camera()]))
    manual = ops.apply_storyboard_changes(unit, revision(ops, unit), [
        {'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0, 'layout': MANUAL_BOX}])
    assert manual['shots'][0]['timeline']['keyframes'][0]['layout']['source'] == 'manual'
    ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID)
    before = ops.read_storyboard(unit)
    with pytest.raises(ValueError, match='手工框图'):
        ops.capture_previs_layout(unit, shot, 0)
    assert ops.read_storyboard(unit) == before          # 拒绝时一个字都没改
    replaced = ops.capture_previs_layout(unit, shot, 0, overwrite=True)
    assert replaced['layout']['source'] == 'previs'
    assert replaced['layout']['objects'] != MANUAL_BOX['objects']


def test_capture_recomputes_its_own_previous_projection(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block)
    write_previs(ops, unit, flat([actor(), camera()]))
    ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID)
    first = ops.capture_previs_layout(unit, shot, 0)
    # 上一次是预演投影（source=previs），不传 overwrite 也能重算——它不是人写的。
    second = ops.capture_previs_layout(unit, shot, 0)
    assert second['layout']['hash'] == first['layout']['hash']
    assert second['revision'] == first['revision'] + 1


def test_layout_change_marks_the_frame_but_staging_does_not(project, monkeypatch, tmp_path):
    """框图进依据、二维调度不进依据：这条口径在阶段 C 不能被预演写入口绕开。"""
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block, duration=4000)
    image = tmp_path / 'sketch.png'
    Image.new('RGB', (20, 12), 'gray').save(image)
    ops.apply_storyboard_changes(unit, revision(ops, unit), [
        {'op': 'set_keyframe', 'shot_id': shot, 'offset_ms': 0, 'sketch_path': str(image)}])

    def render(guide, prompt, copies_to, reference_images=()):
        Image.new('RGB', (24, 14), 'gray').save(copies_to)
        return {'provider': 'stub-only', 'job_id': 'job-previs'}

    monkeypatch.setattr('narrative.storyboard_style_provider.render_sketch_image', render)
    ops.generate_storyboard_sketch(unit, shot, 0, {}, revision(ops, unit))
    assert 'sketch_basis_changed' not in codes(ops, unit)
    # 1) 二维调度怎么改都不算依据变化。
    ops.apply_storyboard_changes(unit, revision(ops, unit), [
        {'op': 'create_object', 'name': '甲', 'kind': 'person', 'x': .2, 'y': .8}])
    actor_id = ops.read_storyboard(unit)['objects'][0]['id']
    ops.apply_storyboard_changes(unit, revision(ops, unit), [
        {'op': 'update_object', 'object_id': actor_id, 'x': .7, 'y': .3},
        {'op': 'set_action_keyframe', 'object_id': actor_id, 'time_ms': 0, 'x': .2, 'interpolation': 'linear'}])
    assert 'sketch_basis_changed' not in codes(ops, unit)
    # 2) 预演投影改的是框图 → 已生成的帧被判「依据已变化」，旧图原样保留。
    write_previs(ops, unit, flat([actor(), camera()]))
    ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID)
    before = ops.read_storyboard(unit)['shots'][0]['keyframes'][0]
    ops.capture_previs_layout(unit, shot, 0)
    after = ops.read_storyboard(unit)['shots'][0]['keyframes'][0]
    assert 'sketch_basis_changed' in codes(ops, unit)
    assert after['sketch_basis_hash'] == before['sketch_basis_hash']
    assert after['sketch_path'] == before['sketch_path']
