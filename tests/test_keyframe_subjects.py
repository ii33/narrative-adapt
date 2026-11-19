"""B2d 站位（subjects）：没有三维预演的镜头也能出框图。

钉住的是「站位 → 框图」这条链上的口径，不是投影数学本身（那在 test_previs_scene）：

* 站位是投影的**输入**：写在时间轴关键帧上、整体替换、``[]`` 清空，且完全不碰 ``layouts``；
* 校验：同帧 asset_id 重复、越出本场房间、不是关键帧的时刻、height≤0 都要拒绝；
  房间未知（没有预演快照）时不校验越界——没有尺寸可依据；
* 没有预演时按站位推导框图：盒体宽 0.45 m、高 height，机位用该帧已写的框图机位，
  bbox 与 depth 见 ``test_no_previs_derives_the_frame_by_hand`` 里的算式；
* 有预演时 ``derive_keyframe_layout`` 走预演那条路，``source`` 标成 previs；
* 两者都没有时，错误要把「缺机位」和「缺站位」分别说清；
* 站位一改、框图重算 → 已生成的帧判「依据已变化」；只改 moment_name 不触发。
"""
import json

import pytest
from PIL import Image
from test_previs_scene import actor, camera, flat
from test_screenplay_writing import project as project
from test_storyboard_operations import setup_board

from narrative.project_package import layout

pytestmark = pytest.mark.slow

CAMERA_ID = 'cam-a'
# 手写进框图的机位：35 mm、正对 -z、离原点 5 m、高 0.875 m（人在画面里上下对称）。
CAMERA_POSE = {'x': 0.0, 'y': 0.875, 'z': 5.0, 'yaw': 0.0, 'pitch': 0.0,
               'lens_mm': 35.0, 'framing': 'medium'}
# 站位盒体的半宽：与 previs_scene.SHOULDER_WIDTH_M / 2 同一口径。
HALF_WIDTH = 0.225


def revision(ops, unit):
    return ops.read_storyboard(unit)['revision']


def apply(ops, unit, changes):
    """改分镜走内容白名单：非法内容进暂存（返回 staged），合法内容直接入库。"""
    return ops.apply_storyboard_changes(unit, revision(ops, unit), changes)


def staged(ops, unit, changes):
    result = apply(ops, unit, changes)
    assert result['status'] == 'staged', result
    assert result['problems'], result
    return result


def shot_with_frames(ops, unit, block, offsets=(0,), duration=4000):
    """建一镜并给定时点建**时间轴**关键帧：站位只能挂在这些时刻上。"""
    shot = apply(ops, unit, [{'op': 'create_shot', 'title': '窗边等待', 'duration_ms': duration,
                              'source_block_ids': [block]}])['shots'][-1]['id']
    for offset in offsets:
        apply(ops, unit, [{'op': 'add_timeline_keyframe', 'shot_id': shot, 'offset_ms': offset}])
    return shot


def frame(ops, unit, shot_id, offset_ms):
    view = ops.read_storyboard(unit)
    shot = next(row for row in view['shots'] if row['id'] == shot_id)
    return next(row for row in shot['timeline']['keyframes'] if row['offset_ms'] == offset_ms)


def codes(ops, unit):
    return {row['code'] for row in ops.check_storyboard(unit)['diagnostics']}


def previs_dir(ops, unit):
    """预演目录＝工程数据根/previs/<场次编号>；与 previs_tools 的约定同一处。"""
    return layout(ops._store.path).root / 'previs' / unit


def write_previs(ops, unit, payload, name='scene.director'):
    directory = previs_dir(ops, unit)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return path


def manual_box(**changes):
    return {'camera': CAMERA_POSE, 'objects': [], 'unmet_needs': []} | changes


# -- 写入、整体替换、清空 -------------------------------------------------------

def test_subjects_round_trip_replace_and_clear(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_frames(ops, unit, block, offsets=(0, 1000))
    written = ops.set_keyframe_subjects(unit, revision(ops, unit), shot, 0, [
        {'asset_id': 'A1', 'x': 1.5, 'z': -2.0},
        {'asset_id': 'A2', 'x': -1.0, 'z': 0.5, 'height': 1.6, 'kind': 'prop', 'facing': 90}])
    # 缺省补齐：y=0（脚下）、height=1.75、kind=person；给过的照抄。
    assert written['subjects'] == [
        {'asset_id': 'A1', 'x': 1.5, 'y': 0.0, 'z': -2.0, 'height': 1.75, 'kind': 'person'},
        {'asset_id': 'A2', 'x': -1.0, 'y': 0.0, 'z': 0.5, 'height': 1.6, 'kind': 'prop', 'facing': 90.0}]
    # 落在时间轴关键帧上，读视图逐帧看得到；站位不写框图（两者互不影响）。
    assert frame(ops, unit, shot, 0)['subjects'] == written['subjects']
    assert frame(ops, unit, shot, 0)['layout'] is None
    assert 'subjects' not in frame(ops, unit, shot, 1000)
    # 整体替换而非追加。
    replaced = ops.set_keyframe_subjects(unit, revision(ops, unit), shot, 0,
                                         [{'asset_id': 'A1', 'x': 0.0, 'z': 0.0}])
    assert [row['asset_id'] for row in replaced['subjects']] == ['A1']
    # [] 清空，且不留一个空列表。
    cleared = ops.set_keyframe_subjects(unit, revision(ops, unit), shot, 0, [])
    assert cleared['subjects'] == [] and 'subjects' not in frame(ops, unit, shot, 0)
    # 只读工具逐帧给出站位与框图来源（这一帧还没有框图 → source=null）。
    read = ops.read_storyboard_layout(unit, shot)
    assert read['keyframes'] == [{'offset_ms': 0, 'subjects': [], 'source': None},
                                 {'offset_ms': 1000, 'subjects': [], 'source': None}]
    # 过期修订不让写。
    with pytest.raises(ValueError, match='刷新'):
        ops.set_keyframe_subjects(unit, cleared['revision'] - 1, shot, 0, [])


@pytest.mark.parametrize('subjects, message', [
    ([{'asset_id': 'A1', 'x': 0, 'z': 0}, {'asset_id': 'A1', 'x': 1, 'z': 0}], '不能重复'),
    ([{'asset_id': 'A1', 'x': 0, 'z': 0, 'height': 0}], '大于0'),
    ([{'asset_id': 'A1', 'x': 0, 'z': 0, 'height': -1.5}], '大于0'),
    ([{'asset_id': 'A1', 'x': 0}], '需要z'),
    ([{'x': 0, 'z': 0}], 'asset_id'),
    ([{'asset_id': 'A1', 'x': 0, 'z': 0, 'kind': 'vehicle'}], 'kind'),
    ([{'asset_id': 'A1', 'x': 0, 'z': 0, 'weight': 3}], '不认识的字段'),
    ({'asset_id': 'A1', 'x': 0, 'z': 0}, '须为数组'),
])
def test_subjects_content_is_validated_before_anything_is_written(project, subjects, message):
    ops, unit, block = setup_board(project)
    shot = shot_with_frames(ops, unit, block)
    before = ops.read_storyboard(unit)
    result = staged(ops, unit, [{'op': 'set_keyframe_subjects', 'shot_id': shot, 'offset_ms': 0,
                                'subjects': subjects}])
    assert any(message in str(problem.get('message', '')) for problem in result['problems']), result['problems']
    assert ops.read_storyboard(unit) == before


def test_subjects_need_an_existing_timeline_keyframe(project, tmp_path):
    ops, unit, block = setup_board(project)
    shot = shot_with_frames(ops, unit, block)
    subjects = [{'asset_id': 'A1', 'x': 0.0, 'z': 0.0}]
    # 900ms 不是这一镜的关键帧。
    result = staged(ops, unit, [{'op': 'set_keyframe_subjects', 'shot_id': shot, 'offset_ms': 900,
                                'subjects': subjects}])
    assert any('关键帧' in problem['message'] for problem in result['problems'])
    # 只有静帧、没有时间轴关键帧的时刻也不行：写在推导出来的计划行上会立刻被读视图冲掉。
    still = apply(ops, unit, [{'op': 'create_shot', 'title': '反打', 'duration_ms': 2000,
                               'source_block_ids': [block]}])['shots'][-1]['id']
    image = tmp_path / 'still.png'
    Image.new('RGB', (16, 9), 'gray').save(image)
    apply(ops, unit, [{'op': 'set_keyframe', 'shot_id': still, 'offset_ms': 0,
                      'sketch_path': str(image)}])
    only_still = staged(ops, unit, [{'op': 'set_keyframe_subjects', 'shot_id': still, 'offset_ms': 0,
                                     'subjects': subjects}])
    assert any('只有静帧' in problem['message'] for problem in only_still['problems'])
    # 镜头不属于本场。
    with pytest.raises(ValueError, match='不属于当前场'):
        ops.set_keyframe_subjects(unit, revision(ops, unit), 'shot-999', 0, subjects)


def test_subjects_outside_the_room_are_rejected_only_when_the_room_is_known(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_frames(ops, unit, block)
    # 房间未知（本场没有预演快照）：不校验越界——没有尺寸可依据，编个范围只会拒绝合法站位。
    written = ops.set_keyframe_subjects(unit, revision(ops, unit), shot, 0,
                                        [{'asset_id': 'A1', 'x': 9.0, 'z': 0.0}])
    assert written['subjects'][0]['x'] == 9.0
    # 预演快照里有房间（宽4、深6、以原点为中心）：x=9 站到墙外。
    directory = previs_dir(ops, unit)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'scene.state.json').write_text(
        json.dumps({'room': {'width': 4.0, 'depth': 6.0, 'height': 2.8}}), encoding='utf-8')
    before = ops.read_storyboard(unit)
    result = staged(ops, unit, [{'op': 'set_keyframe_subjects', 'shot_id': shot, 'offset_ms': 0,
                                'subjects': [{'asset_id': 'A1', 'x': 9.0, 'z': 0.0}]}])
    assert any('超出房间' in problem['message'] for problem in result['problems']), result['problems']
    assert ops.read_storyboard(unit) == before
    # 房间里当然可以：x=1.5 在 ±2 m 之内。
    inside = ops.set_keyframe_subjects(unit, revision(ops, unit), shot, 0,
                                       [{'asset_id': 'A1', 'x': 1.5, 'z': -2.0}])
    assert inside['subjects'][0]['x'] == 1.5


# -- 没有预演也能出框图 ---------------------------------------------------------

def test_no_previs_derives_the_frame_by_hand(project):
    """机位 (0,0.875,5) 朝 -z、35 mm、16:9，两个站位：手算与结果逐位对上。

    算式（与 DirectorDesk 的 filmGauge=36 mm 同口径，见 previs_scene._fov_extents）：
        tan(hFOV/2) = 36 / (2·35) = 0.514286
        tan(vFOV/2) = tan(hFOV/2) / (16/9) = 0.289286
    站位在 (0,0,0)、高 1.75 m → 盒体 x∈[-0.225,0.225]、z∈[-0.225,0.225]、y∈[0,1.75]。
    最近的盒面是 z=+0.225（朝机位的一面），到机位的距离 5-0.225 = 4.775 m：
        半画幅宽 = 4.775 × 0.514286 = 2.455714 m → 半宽占比 0.225/2.455714/2 = 0.04581
        半画幅高 = 4.775 × 0.289286 = 1.381240 m → 半高占比 (1.75/2)/1.381240/2 = 0.31672
    机位 y 与盒体中心同为 0.875 m，所以上下对称：
        bbox = [0.5-0.04581, 0.5-0.31672, 0.5+0.04581, 0.5+0.31672] = [0.4542, 0.1833, 0.5458, 0.8167]
    景深＝盒体中心到机位的沿视线距离：|0-5| = 5.0 m。站位退到 z=-3（更远）时面在 7.775 m：
        半宽占比 0.225/(7.775×0.514286)/2 = 0.02813 → 0.4719..0.5281
        半高占比 (1.75/2)/(7.775×0.289286)/2 = 0.19451 → 0.3055..0.6945
    景深 8.0 m；boxes 按从远到近排，所以更远的那个排前面。
    """
    ops, unit, block = setup_board(project)
    shot = shot_with_frames(ops, unit, block)
    # 机位先写进这一帧的框图（人写的）：没有预演时，构图机位就从这里来。
    apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0,
                      'layout': manual_box()}])
    ops.set_keyframe_subjects(unit, revision(ops, unit), shot, 0, [
        {'asset_id': 'A1', 'x': 0.0, 'z': 0.0},
        {'asset_id': 'A2', 'x': 0.0, 'z': -3.0, 'kind': 'prop'}])
    derived = ops.derive_keyframe_layout(unit, shot, 0, overwrite=True)
    assert derived['source'] == 'subjects' and derived['derived_from'] == 'subjects'
    diagram = derived['layout']
    assert diagram['source'] == 'subjects' and diagram['shot_id'] == shot and diagram['offset_ms'] == 0
    # 机位沿用这一帧已有的框图机位；景别按最近的演员（占高 0.633）判成 medium。
    assert diagram['camera'] == CAMERA_POSE
    tan_h = 36 / (2 * 35)
    tan_v = tan_h / (16 / 9)

    def hand(z):
        near = 5.0 - (z + HALF_WIDTH)          # 盒体朝机位那一面到机位的距离
        half_u = HALF_WIDTH / (near * tan_h) / 2
        half_v = (1.75 / 2) / (near * tan_v) / 2
        return [round(0.5 - half_u, 4), round(0.5 - half_v, 4),
                round(0.5 + half_u, 4), round(0.5 + half_v, 4)]

    far, near = diagram['objects']              # 从远到近
    assert far['asset_id'] == 'A2' and far['kind'] == 'prop'
    assert near['asset_id'] == 'A1' and near['kind'] == 'person'
    assert near['bbox'] == hand(0.0) == [0.4542, 0.1833, 0.5458, 0.8167]
    assert far['bbox'] == hand(-3.0) == [0.4719, 0.3055, 0.5281, 0.6945]
    assert (near['depth'], far['depth']) == (5.0, 8.0)
    # 写进去的框图能被读视图读到，且标着「从站位推的」。
    assert frame(ops, unit, shot, 0)['layout']['hash'] == diagram['hash']
    assert ops.read_storyboard_layout(unit, shot)['keyframes'][0]['source'] == 'subjects'


def test_derive_keeps_a_manual_layout_unless_overwrite(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_frames(ops, unit, block)
    manual = apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0,
                                'layout': manual_box()}])['shots'][0]['timeline']['keyframes'][0]['layout']
    assert manual['source'] == 'manual'
    ops.set_keyframe_subjects(unit, revision(ops, unit), shot, 0, [{'asset_id': 'A1', 'x': 0.0, 'z': 0.0}])
    before = ops.read_storyboard(unit)
    with pytest.raises(ValueError, match='手工框图'):
        ops.derive_keyframe_layout(unit, shot, 0)
    assert ops.read_storyboard(unit) == before      # 拒绝时一个字都没改
    replaced = ops.derive_keyframe_layout(unit, shot, 0, overwrite=True)
    assert replaced['layout']['source'] == 'subjects' and replaced['layout']['objects']


def test_derive_says_which_half_is_missing(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_frames(ops, unit, block)
    # 1) 只有站位、没有机位也没有预演：说清缺机位，并说清预演那条路也没绑。
    ops.set_keyframe_subjects(unit, revision(ops, unit), shot, 0, [{'asset_id': 'A1', 'x': 0.0, 'z': 0.0}])
    with pytest.raises(ValueError) as error:
        ops.derive_keyframe_layout(unit, shot, 0)
    message = str(error.value)
    assert '没有机位' in message and '预演机位' in message
    # 2) 有机位、没站位：说清缺站位（机位是人工写进去的，所以要显式 overwrite）。
    apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0,
                      'layout': manual_box()}])
    ops.set_keyframe_subjects(unit, revision(ops, unit), shot, 0, [])
    with pytest.raises(ValueError, match='没有站位'):
        ops.derive_keyframe_layout(unit, shot, 0, overwrite=True)
    # 3) 预演绑了但读不到（工程文件被删），站位也没写：两边的缺口都要报出来。
    path = write_previs(ops, unit, flat([actor(), camera()]))
    ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID)
    apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0, 'layout': None}])
    path.unlink()
    with pytest.raises(ValueError) as error:
        ops.derive_keyframe_layout(unit, shot, 0)
    message = str(error.value)
    assert '没有站位' in message and '预演这条路也不通' in message


# -- 有预演时优先预演 -----------------------------------------------------------

def test_derive_uses_previs_when_the_shot_is_bound(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_frames(ops, unit, block)
    write_previs(ops, unit, flat([actor(), camera()]))
    ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID)
    # 同时写了站位也不影响：预演可达时优先用预演投影。
    ops.set_keyframe_subjects(unit, revision(ops, unit), shot, 0, [{'asset_id': 'A1', 'x': 0.0, 'z': 0.0}])
    derived = ops.derive_keyframe_layout(unit, shot, 0)
    assert derived['source'] == 'previs' and derived['derived_from'] == 'previs'
    assert derived['previs_problem'] == '' and derived['camera_id'] == CAMERA_ID
    assert derived['layout']['source'] == 'previs'
    # 预演投影画的是预演实体（asset_id 带 previs: 前缀），不是站位。
    assert derived['layout']['objects']
    assert all(row['asset_id'].startswith('previs:') for row in derived['layout']['objects'])
    # 读视图说得出「这帧框图是从预演抓的」。
    read = ops.read_storyboard_layout(unit, shot)
    assert read['keyframes'][0]['source'] == 'previs'
    assert read['keyframes'][0]['subjects'][0]['asset_id'] == 'A1'


# -- 依据稳定性 ----------------------------------------------------------------

def test_changing_subjects_changes_the_diagram_but_renaming_the_moment_does_not(project, monkeypatch, tmp_path):
    ops, unit, block = setup_board(project)
    shot = shot_with_frames(ops, unit, block, duration=4000)
    image = tmp_path / 'frame.png'
    Image.new('RGB', (20, 12), 'gray').save(image)
    apply(ops, unit, [{'op': 'set_keyframe', 'shot_id': shot, 'offset_ms': 0, 'sketch_path': str(image)}])
    apply(ops, unit, [{'op': 'set_keyframe_layout', 'shot_id': shot, 'offset_ms': 0,
                      'layout': manual_box()}])
    ops.set_keyframe_subjects(unit, revision(ops, unit), shot, 0, [{'asset_id': 'A1', 'x': 0.0, 'z': 0.0}])
    ops.derive_keyframe_layout(unit, shot, 0, overwrite=True)

    def render(guide, prompt, copies_to, reference_images=()):
        Image.new('RGB', (24, 14), 'gray').save(copies_to)
        return {'provider': 'stub-only', 'job_id': 'job-subjects'}

    monkeypatch.setattr('narrative.storyboard_style_provider.render_sketch_image', render)
    generated = ops.generate_storyboard_sketch(unit, shot, 0, {}, revision(ops, unit))
    assert 'sketch_basis_changed' not in codes(ops, unit)
    recorded = generated['shots'][0]['keyframes'][0]['sketch_basis_hash']
    # 只改命名时刻：不进依据，不触发。
    apply(ops, unit, [{'op': 'update_timeline_keyframe', 'shot_id': shot, 'offset_ms': 0,
                      'moment_name': '递伞'}])
    assert 'sketch_basis_changed' not in codes(ops, unit)
    # 站位一改、框图重算：框图哈希变 → 已生成的帧判「依据已变化」，旧图原样保留。
    first = ops.derive_keyframe_layout(unit, shot, 0)['layout']['hash']
    ops.set_keyframe_subjects(unit, revision(ops, unit), shot, 0, [{'asset_id': 'A1', 'x': 1.0, 'z': 0.0}])
    second = ops.derive_keyframe_layout(unit, shot, 0)['layout']['hash']
    assert second != first
    assert 'sketch_basis_changed' in codes(ops, unit)
    after = next(row for row in ops.read_storyboard(unit)['shots'] if row['id'] == shot)
    assert after['keyframes'][0]['sketch_basis_hash'] == recorded
    assert after['keyframes'][0]['sketch_path'] == generated['shots'][0]['keyframes'][0]['sketch_path']


# -- 工具注册 ------------------------------------------------------------------

def test_subject_tools_are_registered_and_callable(project):
    from narrative import tool_api

    for name in ('set_keyframe_subjects', 'derive_keyframe_layout'):
        assert tool_api.TOOLS[name].writes is True
    ops, unit, block = setup_board(project)
    shot = shot_with_frames(ops, unit, block)
    entry = layout(ops.project_info().path).entry
    written = tool_api.call('set_keyframe_subjects', {
        'unit_id': unit, 'revision': revision(ops, unit), 'shot_id': shot, 'offset_ms': 0,
        'subjects': [{'asset_id': 'A1', 'x': 0.0, 'z': 0.0}]}, project=entry)
    assert written['subjects'] == [{'asset_id': 'A1', 'x': 0.0, 'y': 0.0, 'z': 0.0,
                                   'height': 1.75, 'kind': 'person'}]
    # 站位有了、机位还没有：工具层的错误要说得清缺什么。
    with pytest.raises(tool_api.ToolError, match='没有机位'):
        tool_api.call('derive_keyframe_layout',
                      {'unit_id': unit, 'shot_id': shot, 'offset_ms': 0}, project=entry)
    # 内容不合法的站位给暂存回执（不抛错、也不悄悄写进去）：调用方按 problems 局部修补。
    staged = tool_api.call('set_keyframe_subjects', {
        'unit_id': unit, 'revision': revision(ops, unit), 'shot_id': shot, 'offset_ms': 0,
        'subjects': [{'asset_id': 'A1', 'x': 0.0, 'z': 0.0, 'height': 0}]}, project=entry)
    assert staged.get('status') == 'staged' and staged.get('problems'), staged
