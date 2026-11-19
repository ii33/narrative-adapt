"""预演工程（.director）读取与投影：纯函数，不需要工程库。

每个用例自己造一份小工程（只按样例文件的**结构**构造，不读 .runtime 里的真实文件），
这样每条断言都能手算：

* 焦距与画幅 → 画面里一米的物体占多高（见 test_projection_matches_the_hand_computed_frame）；
* cuts → 哪台摄影机在拍；path.points → 机位与实体走到哪；
* 相机背后剔除、完全出画剔除、跨画幅裁到 0..1；
* bbox 按 depth 从远到近排序（与框图约定一致）；
* framing_of 的三档与贴边修正。
"""
import json
import math

import pytest

from narrative.previs_scene import camera_at, framing_of, load_scene, project

ASPECT = '16:9'
ASPECT_RATIO = 16 / 9


def camera(camera_id='cam-a', name='A · 主机位', position=(0.0, 0.875, 5.0),
           target=(0.0, 0.875, 0.0), focal=35, path=None, **camera_fields):
    settings = {'aim': 'target', 'focal': focal, 'target': list(target), 'targetId': '', 'targetHeight': 1.2,
                'mode': 'free', 'offset': [0, 1.6, -1.8], 'inheritRotation': True, 'hideWalls': []}
    settings.update(camera_fields)
    return {'id': camera_id, 'kind': 'camera', 'asset': 'camera', 'name': name,
            'position': list(position), 'rotation': [0, 0, 0], 'scale': [1, 1, 1], 'visible': True,
            'height': 1.75, 'path': path, 'camera': settings}


def actor(entity_id='act-a', name='甲', position=(0.0, 0.0, 0.0), height=1.75, path=None):
    return {'id': entity_id, 'kind': 'actor', 'asset': 'person', 'name': name,
            'position': list(position), 'rotation': [0, 0, 0], 'scale': [1, 1, 1], 'visible': True,
            'height': height, 'path': path, 'clips': [], 'pose': {}, 'poseKeys': [], 'camera': None}


def prop(entity_id='prop-a', name='箱子', position=(0.0, 0.0, 0.0), scale=(0.5, 0.5, 0.5), asset='cube'):
    return {'id': entity_id, 'kind': 'prop', 'asset': asset, 'name': name,
            'position': list(position), 'rotation': [0, 0, 0], 'scale': list(scale), 'visible': True,
            'height': 1.75, 'path': None, 'clips': [], 'pose': {}, 'poseKeys': [], 'camera': None}


def flat(entities, cuts=None, duration=10, fps=24, name='单场景'):
    return {'format': 'director-desk', 'version': 1, 'name': name, 'duration': duration, 'fps': fps,
            'aspect': ASPECT, 'room': {'enabled': False, 'width': 8, 'depth': 6, 'height': 2.8},
            'entities': entities, 'cuts': cuts if cuts is not None else [{'time': 0, 'cameraId': entities_camera(entities, 0)}],
            'references': []}


def entities_camera(entities, index):
    return [row['id'] for row in entities if row['kind'] == 'camera'][index]


def multi(scenes, active=None, name='多场景'):
    return {'format': 'director-desk', 'version': 3, 'name': name,
            'activeSceneId': active if active is not None else scenes[0]['id'],
            'scenes': scenes, 'projectId': 'p-1', 'resources': []}


def state(entities, cuts=None, duration=10, fps=24):
    return {'duration': duration, 'fps': fps, 'aspect': ASPECT,
            'room': {'enabled': False, 'width': 4.8, 'depth': 4.2, 'height': 2.8},
            'entities': entities,
            'cuts': cuts if cuts is not None else [{'time': 0, 'cameraId': entities_camera(entities, 0)}],
            'references': [], 'lighting': {'ambient': 0.45}}


def written(tmp_path, payload, name='scene.director'):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return path


def scene_of(tmp_path, entities, **flat_fields):
    return load_scene(written(tmp_path, flat(entities, **flat_fields)))


# -- 读取 ---------------------------------------------------------------------

def test_flat_file_is_read_as_one_scene(tmp_path):
    entities = [actor(position=(0, 0.2, 0)), prop(scale=(1.6, 1, 1.6), asset='ground'), camera(focal=32)]
    scene = load_scene(written(tmp_path, flat(entities, duration=12, name='舞台')))
    assert scene['scene_id'] == 'scene' and scene['name'] == '舞台'
    assert scene['duration_s'] == 12.0 and scene['fps'] == 24
    assert scene['aspect'] == ASPECT and scene['aspect_ratio'] == pytest.approx(ASPECT_RATIO)
    assert [row['kind'] for row in scene['entities']] == ['actor', 'prop']
    machine = scene['cameras'][0]
    assert machine['id'] == 'cam-a' and machine['focal'] == 32.0
    assert machine['position'] == (0.0, 0.875, 5.0) and machine['target'] == (0.0, 0.875, 0.0)
    assert machine['path'] == [] and machine['focal_keys'] == []
    # 扁平结构也要给出「一条场景」的清单，上层不必分两套写法。
    assert scene['scenes'] == [{'id': 'scene', 'name': '舞台'}]
    assert scene['cuts'] == [{'time': 0.0, 'camera_id': 'cam-a'}]


def test_multi_scene_file_picks_active_or_named_scene(tmp_path):
    payload = multi([{'id': 's1', 'name': '第一场', 'state': state([actor(), camera()], duration=8)},
                     {'id': 's2', 'name': '第二场', 'state': state([actor(name='乙'), camera(focal=50)], duration=15)}],
                    active='s2')
    path = written(tmp_path, payload, 'two.director')
    active = load_scene(path)
    assert active['scene_id'] == 's2' and active['name'] == '第二场' and active['duration_s'] == 15.0
    assert active['cameras'][0]['focal'] == 50.0
    # 场景编号或场景名都能选；选出来的 name 属于那个场景。
    first = load_scene(path, 's1')
    assert first['scene_id'] == 's1' and first['duration_s'] == 8.0
    assert load_scene(path, '第二场')['scene_id'] == 's2'
    assert [row['id'] for row in active['scenes']] == ['s1', 's2']
    with pytest.raises(ValueError, match='没有场景s9'):
        load_scene(path, 's9')


@pytest.mark.parametrize('payload, message', [
    ({'format': 'other-desk'}, '不是 DirectorDesk'),
    ({'format': 'director-desk', 'entities': []}, '时长'),
])
def test_flat_payload_problems_are_named(tmp_path, payload, message):
    with pytest.raises(ValueError, match=message):
        load_scene(written(tmp_path, payload))


def test_unreadable_files_say_what_is_wrong(tmp_path):
    missing = tmp_path / 'nope.director'
    with pytest.raises(ValueError, match='不存在'):
        load_scene(missing)
    broken = tmp_path / 'broken.director'
    broken.write_text('{"format": "director-desk", ', encoding='utf-8')
    with pytest.raises(ValueError, match='不是合法 JSON'):
        load_scene(broken)
    flat_path = written(tmp_path, flat([camera()]), 'flat.director')
    # 单场景结构没有可选场景：给了 scene_id 就得说清楚，不能假装选到了什么。
    with pytest.raises(ValueError, match='单场景结构'):
        load_scene(flat_path, 's1')
    # 多场景文件里某个场景没有 state：同样直说。
    no_state = written(tmp_path, multi([{'id': 's1', 'name': '第一场'}]), 'bad.director')
    with pytest.raises(ValueError, match='state'):
        load_scene(no_state)


def test_cut_pointing_at_a_missing_camera_is_rejected(tmp_path):
    payload = flat([camera()], cuts=[{'time': 0, 'cameraId': 'ghost'}])
    with pytest.raises(ValueError, match='不存在的摄影机'):
        load_scene(written(tmp_path, payload))


# -- 机位 ---------------------------------------------------------------------

def two_camera_scene(tmp_path):
    entities = [camera('cam-a', 'A · 主机位', position=(0, 2, 5), target=(0, 1, 0)),
                camera('cam-b', 'B · 侧机位', position=(5, 2, 5), target=(0, 1, 0))]

    def cut(time, camera_id):
        return {'time': time, 'cameraId': camera_id}

    return load_scene(written(tmp_path, flat(entities, cuts=[cut(0, 'cam-a'), cut(4, 'cam-b')])))


def test_camera_at_follows_the_cut_table(tmp_path):
    scene = two_camera_scene(tmp_path)
    assert camera_at(scene, 0)['camera_id'] == 'cam-a'
    assert camera_at(scene, 3.9)['camera_id'] == 'cam-a'
    assert camera_at(scene, 4)['camera_id'] == 'cam-b'
    assert camera_at(scene, 9)['camera_id'] == 'cam-b'
    # 第一刀之前仍按第一刀的机位（DirectorDesk 的 activeCameraId 就是这么取的）。
    later = load_scene(written(tmp_path, flat([camera('cam-a', position=(0, 2, 5)),
                                              camera('cam-b', position=(5, 2, 5))],
                                             cuts=[{'time': 2, 'cameraId': 'cam-b'}]), 'later.director'))
    assert camera_at(later, 0)['camera_id'] == 'cam-b'


def test_camera_at_interpolates_the_motion_path(tmp_path):
    path = {'smooth': False, 'points': [{'time': 0, 'position': [0, 1.6, 10]},
                                        {'time': 10, 'position': [10, 1.6, 10]}]}
    scene = load_scene(written(tmp_path, flat([camera(position=(0, 1.6, 10), path=path)])))
    assert camera_at(scene, 0)['position'] == pytest.approx([0, 1.6, 10])
    assert camera_at(scene, 5)['position'] == pytest.approx([5, 1.6, 10])
    assert camera_at(scene, 20)['position'] == pytest.approx([10, 1.6, 10])


def test_camera_at_can_be_forced_and_rejects_unknown_ids(tmp_path):
    scene = two_camera_scene(tmp_path)
    forced = camera_at(scene, 0, 'cam-b')
    assert forced['camera_id'] == 'cam-b' and forced['position'] == [5.0, 2.0, 5.0]
    with pytest.raises(ValueError, match='没有摄影机cam-z'):
        camera_at(scene, 0, 'cam-z')


def test_camera_angles_use_the_target_and_degrees(tmp_path):
    scene = scene_of(tmp_path, [actor(), camera(position=(0, 1.6, 5), target=(0, 1.6, 0))])
    level = camera_at(scene, 0)
    assert level['yaw'] == pytest.approx(0.0) and level['pitch'] == pytest.approx(0.0)
    # 注视点在画面右侧：yaw 为负（yaw=0 看向 -z，正 yaw 的视线转向 -x 即向左）。
    to_the_right = scene_of(tmp_path, [actor(), camera(position=(0, 1.6, 5), target=(3, 1.6, 0))])
    machine = camera_at(to_the_right, 0)
    assert machine['yaw'] == pytest.approx(math.degrees(math.atan2(-3, 5)))
    up = scene_of(tmp_path, [actor(), camera(position=(0, 1.6, 5), target=(0, 3.6, 0))])
    assert camera_at(up, 0)['pitch'] == pytest.approx(math.degrees(math.atan2(2, 5)))


def test_camera_tracks_a_target_entity_offset_by_target_height(tmp_path):
    entities = [actor('act-a', position=(2, 0, 0), height=1.8),
                camera(target=(0, 0, 0), targetId='act-a', targetHeight=0.5)]
    scene = load_scene(written(tmp_path, flat(entities)))
    # targetId 压过写死的 target：注视点＝实体位置＋targetHeight。
    assert camera_at(scene, 0)['target'] == pytest.approx([2.0, 0.5, 0.0])


def test_manual_aim_uses_the_entity_rotation(tmp_path):
    scene = load_scene(written(tmp_path, flat([actor(), camera(aim='manual')])))
    # rotation 全 0 → three.js 的 -z 朝向：yaw=0、pitch=0。
    machine = camera_at(scene, 0)
    assert machine['yaw'] == pytest.approx(0.0) and machine['pitch'] == pytest.approx(0.0)
    turned = load_scene(written(tmp_path, flat([
        actor(), camera(aim='manual') | {'rotation': [0, -math.pi / 2, 0]}]), 'turned.director'))
    # 绕 +y 转 -90°：视线从 -z 转到 +x，而 yaw=-90 对应的正是 +x（两者同一套约定）。
    assert camera_at(turned, 0)['yaw'] == pytest.approx(-90.0)


# -- 投影 ---------------------------------------------------------------------

def test_projection_matches_the_hand_computed_frame_height(tmp_path):
    """35 mm、16:9、正前方 5 m、高 1.75 m 的人：薄片高约占 0.605，盒体占 0.633。

    算式（35 mm 全画幅，与 DirectorDesk 的 filmGauge=36 mm 同口径）：
        tan(hFOV/2) = 36 / (2·35) = 0.514286
        tan(vFOV/2) = tan(hFOV/2) / (16/9) = 0.289286
        5 m 处的半画幅高 = 5 × 0.289286 = 1.44643 m → 整幅高 2.89286 m
        薄片：1.75 / 2.89286 = 0.60494
    人物盒体有 0.45 m 进深（肩宽当方柱的一边），近面在 4.775 m、远面在 5.225 m，
    包围盒取的是并集，所以实际高度由近面决定：
        1.75 / (4.775 × 2 × 0.289286) = 0.63342
    相机高度取 0.875 m（人物身高的正中），主体上下对称：
        y0 = 0.5 − 0.63342/2 = 0.18329，y1 = 0.5 + 0.63342/2 = 0.81671
    """
    tan_v = 36 / (2 * 35) / ASPECT_RATIO
    thin = 1.75 / (5 * 2 * tan_v)
    near = 1.75 / (4.775 * 2 * tan_v)
    scene = scene_of(tmp_path, [actor(), camera(position=(0, 0.875, 5), target=(0, 0.875, 0))])
    rows = project(scene, camera_at(scene, 0), 0, ASPECT)
    assert len(rows) == 1
    x0, y0, x1, y1 = rows[0]['bbox']
    assert thin < y1 - y0 == pytest.approx(near, abs=0.005)
    assert y0 == pytest.approx(0.5 - near / 2, abs=0.005)
    assert y1 == pytest.approx(0.5 + near / 2, abs=0.005)
    assert 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0
    assert rows[0]['depth'] == pytest.approx(5.0, abs=0.01)
    assert rows[0]['kind'] == 'person' and rows[0]['entity_id'] == 'act-a'


def test_wider_aspect_makes_the_same_subject_taller(tmp_path):
    scene = scene_of(tmp_path, [actor(), camera(position=(0, 0.875, 5), target=(0, 0.875, 0))])
    machine = camera_at(scene, 0)
    standard = project(scene, machine, 0, '16:9')[0]['bbox']
    ultrawide = project(scene, machine, 0, '21:9')[0]['bbox']
    # 21:9 的水平视野不变、竖直视野更窄 → 同一个人占得更高。
    assert ultrawide[3] - ultrawide[1] > standard[3] - standard[1] + 0.1


def test_projection_is_sorted_far_to_near(tmp_path):
    scene = scene_of(tmp_path, [actor('act-near', position=(0, 0, 0)),
                                actor('act-mid', position=(0, 0, -3)),
                                actor('act-far', position=(0, 0, -6)),
                                camera(position=(0, 0.875, 5), target=(0, 0.875, 0))])
    rows = project(scene, camera_at(scene, 0), 0)
    assert [row['entity_id'] for row in rows] == ['act-far', 'act-mid', 'act-near']
    assert [row['depth'] for row in rows] == sorted((row['depth'] for row in rows), reverse=True)
    assert rows[0]['depth'] == pytest.approx(11.0, abs=0.05)


def test_entities_behind_the_camera_are_dropped(tmp_path):
    scene = scene_of(tmp_path, [actor('act-front', position=(0, 0, 0)),
                                actor('act-behind', position=(0, 0, 8)),
                                camera(position=(0, 0.875, 5), target=(0, 0.875, 0))])
    rows = project(scene, camera_at(scene, 0), 0)
    assert [row['entity_id'] for row in rows] == ['act-front']


def test_out_of_frame_entities_are_dropped_and_edges_are_clipped(tmp_path):
    # 5 m 处的半画幅宽 = 5 × 0.514286 = 2.57143 m：站在这里的人物正好贴住右边缘，
    # 再往右就该整块出画。
    edge = scene_of(tmp_path, [actor(position=(2.57143, 0, 0)),
                               camera(position=(0, 0.875, 5), target=(0, 0.875, 0))], name='edge.director')
    bbox = project(edge, camera_at(edge, 0), 0)[0]['bbox']
    assert bbox[2] == 1.0 and bbox[0] < 1.0  # 右边缘裁到 0..1，左边还在画面里
    outside = scene_of(tmp_path, [actor(position=(3.5, 0, 0)),
                                  camera(position=(0, 0.875, 5), target=(0, 0.875, 0))])
    assert project(outside, camera_at(outside, 0), 0) == []


def test_props_are_boxes_sitting_on_their_own_y(tmp_path):
    # 盒子 0.5 m 见方，底面贴在 y=1 上（悬空的道具不会掉到地面）。
    scene = scene_of(tmp_path, [prop(position=(0, 1, 0), scale=(0.5, 0.5, 0.5)),
                                camera(position=(0, 1.25, 5), target=(0, 1.25, 0))])
    rows = project(scene, camera_at(scene, 0), 0)
    assert len(rows) == 1 and rows[0]['kind'] == 'prop'
    y0, y1 = rows[0]['bbox'][1], rows[0]['bbox'][3]
    # 0.5 m 高、5 m 远、画幅高 2.89286 m → 占 0.1729，中心对着画面中心。
    assert y1 - y0 == pytest.approx(0.5 / 2.89286, abs=0.01)
    assert (y0 + y1) / 2 == pytest.approx(0.5, abs=0.01)


def test_entity_path_moves_the_bbox(tmp_path):
    path = {'smooth': True, 'points': [{'time': 0, 'position': [0, 0, 0]},
                                       {'time': 4, 'position': [1, 0, 0]}]}
    scene = scene_of(tmp_path, [actor(path=path), camera(position=(0, 0.875, 5), target=(0, 0.875, 0))])
    machine = camera_at(scene, 0)
    left = project(scene, machine, 0)[0]['bbox']
    right = project(scene, machine, 2)[0]['bbox']
    assert right[0] > left[0] and right[2] > left[2]


def test_lights_and_hidden_entities_never_reach_the_frame(tmp_path):
    light = prop('light-a', name='暖色主光', position=(1, 3, 1), asset='light-spot') | {'light': {'intensity': 200}}
    hidden = actor('act-hidden', position=(0, 0, 0)) | {'visible': False}
    scene = scene_of(tmp_path, [actor(), light, hidden, camera(position=(0, 0.875, 5), target=(0, 0.875, 0))])
    rows = project(scene, camera_at(scene, 0), 0)
    assert [row['entity_id'] for row in rows] == ['act-a']


# -- 景别 ---------------------------------------------------------------------

@pytest.mark.parametrize('bbox, expected', [
    ([0.3, 0.02, 0.7, 0.97], 'close'),   # 高度 0.95，几乎占满画幅
    ([0.3, 0.2, 0.7, 0.8], 'medium'),    # 高度 0.60：5 m / 35 mm 那种半身
    ([0.3, 0.45, 0.7, 0.65], 'wide'),    # 高度 0.20：人在环境里只占一小块
    ([0.2, 0.05, 0.4, 0.30], 'wide'),    # 高度 0.25：贴左但不是近景
])
def test_framing_of_three_tiers(bbox, expected):
    assert framing_of(bbox, ASPECT) == expected


def test_framing_of_uses_both_height_and_position():
    # 高度只有 0.5，但上下都被切掉（主体比画面大）→ 近景。
    assert framing_of([0.2, 0.0, 0.8, 0.5], ASPECT) == 'close'
    # 同样高度、四边都在画面内 → 中景。
    assert framing_of([0.2, 0.25, 0.8, 0.75], ASPECT) == 'medium'
    # 16:9 里左右贴边很常见（群戏），不足以判近景。
    assert framing_of([0.0, 0.25, 0.6, 0.75], ASPECT) == 'medium'
    # 超宽画幅里横向出框基本就是近景/特写。
    assert framing_of([0.0, 0.25, 0.6, 0.75], '21:9') == 'close'


def test_framing_of_survives_missing_or_broken_boxes():
    assert framing_of(None) == 'wide'
    assert framing_of([]) == 'wide'
    assert framing_of([0.5, 0.5, 0.5, 0.5]) == 'wide'
    assert framing_of(['a', 'b', 'c', 'd']) == 'wide'
