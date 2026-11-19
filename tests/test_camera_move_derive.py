"""运镜改从预演机位轨迹推导：量什么、阈值多少、跟框图差量/人工标注怎么排优先级。

要点：
* 预演里的机位轨迹是运镜的真值（``source="previs"``）：焦距→zoom、三轴位移→dolly/truck/
  pedestal、朝向→pan/tilt（阈值见 ``segment_motion`` 里的 PREVIS_* 与 YAW/PITCH_TOL_DEG）；
* 组合运镜要留着**全部分量**并按幅度排出 ``primary``，不能只剩主运镜；
* 段内切过机位（``cuts``）时这一段真值本身就跨两个镜头：``kind="cut"`` + 切开提示；
* 没有预演才回落框图差量（``source="layout"``，只给证据不给 kind）；人工标注显式给 kind 时
  覆盖两者（``source="manual"``），但分量照样返回供人核对；
* ``source="previs"`` 不与自己的推导比对（没有第二种证据）；只有人工标注才报
  ``MOTION_KIND_CONFLICT``；
* 提示词把主次分量都写进「运镜」句，用官方英文词表，句内不出现任何数字，也不用否定词。
"""
import pytest

from narrative import previs_host
from narrative.segment_motion import (
    CAMERA_MOVES,
    camera_move_from_scene,
    derive_segment,
    segment_prompt,
    validate_segment,
)

PREVIS = {'scene_id': 'scene-1', 'camera_id': 'cam-1', 'time_offset_ms': 0}


# ------------------------------------------------------------------ 假的预演快照
def point(time_s, position):
    return {'time': time_s, 'position': list(position), 'easing': None}


def focal_key(time_s, value):
    return {'time': time_s, 'value': value, 'easing': None}


def camera(*, camera_id='cam-1', position=(0.0, 1.6, 0.0), target=(0.0, 1.6, -5.0), focal=35.0,
           path=(), target_path=(), focal_keys=()):
    """一台机位：字段与 ``previs_scene.load_scene`` 同口径（米、y 向上、焦距毫米、角度度）。"""
    return {'id': camera_id, 'name': camera_id, 'kind': 'camera', 'position': list(position),
            'rotation': [0.0, 0.0, 0.0], 'path': [dict(row) for row in path], 'focal': focal,
            'focal_keys': [dict(row) for row in focal_keys], 'target': list(target),
            'target_id': None, 'target_height': 0.0,
            'target_path': [dict(row) for row in target_path], 'aim': 'target', 'mode': 'free'}


def scene(*cameras, cuts=()):
    return {'path': None, 'scene_id': 'scene-1', 'name': '测试场', 'duration_s': 6.0, 'fps': 24,
            'aspect': '16:9', 'aspect_ratio': 16 / 9, 'cameras': list(cameras), 'entities': [],
            'cuts': [dict(row) for row in cuts], 'scenes': []}


def layout():
    """两端框图：机位没动、焦距也没动（框图差量读不出运镜，正好用来分辨优先级）。"""
    return {'camera': {'x': 0.0, 'y': 1.6, 'z': 0.0, 'yaw': 0.0, 'pitch': 0.0, 'lens_mm': 35.0,
                       'framing': 'medium'},
            'objects': [{'asset_id': 'A1', 'bbox': [0.30, 0.30, 0.50, 0.80], 'depth': 2.0,
                         'kind': 'person'}],
            'unmet_needs': [], 'source': 'previs'}


def shot(*, previs=None, layouts=None, motions=None, duration=2000):
    frames = {0: layout(), 2000: layout()} if layouts is None else dict(layouts)
    offsets = sorted(frames) or [0]
    keyframes = [{'id': f'frame-{offset}', 'offset_ms': offset} for offset in offsets]
    timeline = {'keyframes': [{'offset_ms': offset, 'moment_name': '', 'description': ''}
                              for offset in offsets]}
    row = {'id': 'shot-1', 'title': '推镜', 'duration_ms': duration, 'source_block_ids': [],
           'action_start_ms': 0, 'keyframes': keyframes, 'timeline': timeline,
           'layouts': {str(offset): value for offset, value in frames.items()},
           'aspect_ratio': '16:9'}
    if previs:
        row['previs'] = dict(previs)
    if motions:
        row['segment_motions'] = motions
    return row


def board(row):
    return {'unit_id': 'unit-1', 'revision': 1, 'approved': False, 'fps': 24, 'objects': [],
            'markers': [], 'asset_needs': [], 'shots': [row]}


def move_of(scene_, *, previs=PREVIS, from_ms=0, to_ms=2000):
    return camera_move_from_scene(shot(previs=previs), {'from_ms': from_ms, 'to_ms': to_ms}, scene_)


# ------------------------------------------------------------------ 七种情形
def test_pure_lateral_move_is_a_truck_right():
    """机位与注视点一起右移 0.35m（朝向不变）：横向分量，中等幅度。"""
    snapshot = scene(camera(path=[point(0, (0.0, 1.6, 0.0)), point(2, (0.35, 1.6, 0.0))],
                            target_path=[point(0, (0.0, 1.6, -5.0)), point(2, (0.35, 1.6, -5.0))]))
    move = move_of(snapshot)
    assert move['source'] == 'previs' and move['kind'] == 'truck_right'
    assert move['components'] == [{'kind': 'truck_right', 'amplitude': 'medium', 'value': 0.35,
                                  'unit': 'm'}]
    assert move['primary'] == move['components'][0]
    assert move['deltas'] == {'focal_mm': 0.0, 'dolly_m': 0.0, 'truck_m': 0.35, 'pedestal_m': 0.0,
                              'yaw_deg': 0.0, 'pitch_deg': 0.0}
    # 往左走就是另一支：方向由分量的符号定，不靠猜。
    assert move_of(scene(camera(path=[point(0, (0.0, 1.6, 0.0)), point(2, (-0.4, 1.6, 0.0))],
                                 target_path=[point(0, (0.0, 1.6, -5.0)),
                                              point(2, (-0.4, 1.6, -5.0))])))['kind'] == 'truck_left'


def test_pure_zoom_reads_the_focal_change():
    """焦距 35→50mm：变焦推近；15/35≈43% 的相对变化落在「中等幅度」。"""
    snapshot = scene(camera(focal_keys=[focal_key(0, 35.0), focal_key(2, 50.0)]))
    move = move_of(snapshot)
    assert move['kind'] == 'zoom_in'
    assert move['components'] == [{'kind': 'zoom_in', 'amplitude': 'medium', 'value': 15.0,
                                  'unit': 'mm'}]
    assert move['deltas']['focal_mm'] == 15.0
    # 机身一点没动：不会顺带报出 dolly/truck/pedestal。
    assert (move['deltas']['dolly_m'], move['deltas']['truck_m'], move['deltas']['pedestal_m']) == (
        0.0, 0.0, 0.0)


def test_pure_tilt_up_reads_the_target_rise():
    """注视点抬高 0.5m（机位不动）：俯仰角 5.71°，上摇，中等幅度。"""
    snapshot = scene(camera(target_path=[point(0, (0.0, 1.6, -5.0)), point(2, (0.0, 2.1, -5.0))]))
    move = move_of(snapshot)
    assert move['kind'] == 'tilt_up'
    assert move['components'] == [{'kind': 'tilt_up', 'amplitude': 'medium', 'value': 5.711,
                                  'unit': 'deg'}]
    assert move['deltas']['pitch_deg'] == 5.711 and move['deltas']['yaw_deg'] == 0.0


def test_target_moving_sideways_reads_as_a_pan_left():
    """注视点向左移：yaw 变正（看 -x 方向）＝左摇；机位不动，所以没有位移分量。"""
    snapshot = scene(camera(target_path=[point(0, (0.0, 1.6, -5.0)), point(2, (-1.0, 1.6, -5.0))]))
    move = move_of(snapshot)
    assert move['kind'] == 'pan_left' and move['components'][0]['unit'] == 'deg'
    assert move['deltas']['yaw_deg'] > 0 and move['deltas']['pitch_deg'] == 0.0


def test_pure_dolly_in_reads_the_position_step_along_the_sight_line():
    """机位沿视线靠近主体 1.0m：推拉分量，超过 0.6m → 大幅。"""
    snapshot = scene(camera(position=(0.0, 1.6, -2.0), target=(0.0, 1.6, -5.0),
                            path=[point(0, (0.0, 1.6, -2.0)), point(2, (0.0, 1.6, -3.0))]))
    move = move_of(snapshot)
    assert move['kind'] == 'dolly_in'
    assert move['components'] == [{'kind': 'dolly_in', 'amplitude': 'large', 'value': 1.0,
                                  'unit': 'm'}]
    # 三轴正交分解：前进 1m 不该被顺带算成升降或横移。
    assert move['deltas']['pedestal_m'] == 0.0 and move['deltas']['truck_m'] == 0.0


def test_a_still_camera_is_static_with_no_components():
    move = move_of(scene(camera()))
    assert move['kind'] == 'static'
    assert move['components'] == [] and move['primary'] is None
    assert move['source'] == 'previs'  # 来源仍是预演：这是「量出来的不动」，不是「没量」


def test_a_combined_move_keeps_every_component_and_picks_the_biggest_as_primary():
    """右移 0.6m ＋ 变焦 35→40mm：两个分量都要在，主运镜是幅度更大的那个。"""
    snapshot = scene(camera(path=[point(0, (0.0, 1.6, 0.0)), point(2, (0.6, 1.6, 0.0))],
                            target_path=[point(0, (0.0, 1.6, -5.0)), point(2, (0.6, 1.6, -5.0))],
                            focal_keys=[focal_key(0, 35.0), focal_key(2, 40.0)]))
    move = move_of(snapshot)
    assert [row['kind'] for row in move['components']] == ['truck_right', 'zoom_in']
    assert move['kind'] == 'truck_right' and move['primary']['kind'] == 'truck_right'
    assert (move['primary']['amplitude'], move['components'][1]['amplitude']) == ('large', 'small')
    # 次要分量没被丢掉：5mm 的变焦照样在清单里，提示词才有话可写。
    assert move['components'][1] == {'kind': 'zoom_in', 'amplitude': 'small', 'value': 5.0,
                                     'unit': 'mm'}


def test_a_segment_that_crosses_a_cut_is_not_a_camera_move():
    """段内切过机位：真值跨了两个镜头，返回 cut 并说清该怎么拆段。"""
    snapshot = scene(camera(camera_id='cam-1'),
                     camera(camera_id='cam-2', position=(2.0, 1.6, 0.0), target=(2.0, 1.6, -5.0)),
                     cuts=[{'time': 0.0, 'camera_id': 'cam-1'},
                           {'time': 1.0, 'camera_id': 'cam-2'}])
    move = move_of(snapshot)
    assert move['kind'] == 'cut' and move['components'] == [] and move['primary'] is None
    # 镜内毫秒与预演毫秒都给：前者用来拆段，后者用来回预演里对时间。
    assert move['cuts'] == [{'time_ms': 1000, 'previs_ms': 1000, 'camera_id': 'cam-2'}]
    assert '拆成两段' in move['note']
    # 镜头从预演 0.5s 起算时，同一个切点落在镜内 500ms。
    shifted = move_of(snapshot, previs={'scene_id': 'scene-1', 'camera_id': 'cam-1',
                                       'time_offset_ms': 500})
    assert shifted['cuts'] == [{'time_ms': 500, 'previs_ms': 1000, 'camera_id': 'cam-2'}]
    # 段起点上正好有 cut 不算段内切换（它属于这一段的第一帧）。
    assert move_of(snapshot, from_ms=1000, to_ms=2000)['kind'] == 'static'


# ------------------------------------------------------------------ 阈值边界
def test_lens_threshold_edges():
    at = move_of(scene(camera(focal_keys=[focal_key(0, 35.0), focal_key(2, 36.5)])))
    assert (at['kind'], at['components'][0]['value']) == ('zoom_in', 1.5)
    below = move_of(scene(camera(focal_keys=[focal_key(0, 35.0), focal_key(2, 36.4)])))
    assert below['kind'] == 'static' and below['components'] == []


def test_dolly_threshold_edges():
    def dolly_in(meters):
        return scene(camera(position=(0.0, 1.6, -2.0), target=(0.0, 1.6, -5.0),
                            path=[point(0, (0.0, 1.6, -2.0)), point(2, (0.0, 1.6, -2.0 - meters))]))
    assert move_of(dolly_in(0.10))['kind'] == 'dolly_in'
    assert move_of(dolly_in(0.08))['kind'] == 'static'


def test_truck_and_pedestal_threshold_edges():
    def truck(meters):
        return scene(camera(path=[point(0, (0.0, 1.6, 0.0)), point(2, (meters, 1.6, 0.0))],
                            target_path=[point(0, (0.0, 1.6, -5.0)),
                                         point(2, (meters, 1.6, -5.0))]))

    def pedestal(meters):
        # 注视点跟着一起升，俯仰角保持 0：只留升降一个分量，边界才干净。
        return scene(camera(path=[point(0, (0.0, 1.6, 0.0)), point(2, (0.0, 1.6 + meters, 0.0))],
                            target_path=[point(0, (0.0, 1.6, -5.0)),
                                         point(2, (0.0, 1.6 + meters, -5.0))]))

    assert move_of(truck(0.10))['kind'] == 'truck_right'
    assert move_of(truck(0.09))['kind'] == 'static'
    assert move_of(pedestal(0.10))['kind'] == 'pedestal_up'
    assert move_of(pedestal(0.05))['kind'] == 'static'


def test_yaw_and_pitch_threshold_edges():
    def pan(degrees):
        # 注视点在 5m 外，横移 5·tan(角度) 就是那个角度。
        return scene(camera(target_path=[point(0, (0.0, 1.6, -5.0)),
                                         point(2, (-5.0 * _tan(degrees), 1.6, -5.0))]))

    def tilt(degrees):
        return scene(camera(target_path=[point(0, (0.0, 1.6, -5.0)),
                                         point(2, (0.0, 1.6 + 5.0 * _tan(degrees), -5.0))]))

    assert move_of(pan(2.0))['kind'] == 'pan_left'
    assert move_of(pan(1.9))['kind'] == 'static'
    assert move_of(tilt(1.5))['kind'] == 'tilt_up'
    assert move_of(tilt(1.4))['kind'] == 'static'


def _tan(degrees):
    from math import radians, tan
    return tan(radians(degrees))


# ------------------------------------------------------------------ 优先级
def test_previs_wins_and_layout_is_the_fallback():
    snapshot = scene(camera(path=[point(0, (0.0, 1.6, 0.0)), point(2, (0.35, 1.6, 0.0))],
                            target_path=[point(0, (0.0, 1.6, -5.0)), point(2, (0.35, 1.6, -5.0))]))
    bound = derive_segment(board(shot(previs=PREVIS)), 'shot-1', 0, 2000, scene=snapshot)
    move = bound['camera_move']
    assert move['source'] == 'previs' and move['kind'] == 'truck_right'
    # 景别仍由 framing_of 判（预演轨迹里没有 bbox，这条路不冒充景别）。
    assert (move['from_framing'], move['to_framing']) == ('medium', 'medium')
    # 预演是真值，不与自己的推导比对。
    assert bound['warnings'] == [] and bound['previs_problem'] == ''
    # 没绑预演就回落框图差量：只给证据，不给 kind。
    plain = derive_segment(board(shot()), 'shot-1', 0, 2000, scene=snapshot)
    assert plain['camera_move']['source'] == 'layout'
    assert plain['camera_move']['kind'] is None and plain['camera_move']['components'] == []
    # 绑了预演却拿不到快照：回落框图差量，并说清为什么（不能静默换来源）。
    offline = derive_segment(board(shot(previs=PREVIS)), 'shot-1', 0, 2000)
    assert offline['camera_move']['source'] == 'layout'
    assert '预演' in offline['previs_problem']
    assert [row['code'] for row in offline['warnings']] == ['PREVIS_UNAVAILABLE']


def test_manual_kind_overrides_but_the_components_stay():
    snapshot = scene(camera(path=[point(0, (0.0, 1.6, 0.0)), point(2, (0.35, 1.6, 0.0))],
                            target_path=[point(0, (0.0, 1.6, -5.0)), point(2, (0.35, 1.6, -5.0))]))
    manual = shot(previs=PREVIS, motions=[{'from_ms': 0, 'to_ms': 2000, 'notes': '跟着她转',
                                          'camera_move': {'kind': 'pan_left', 'speed': 'slow'}}])
    motion = derive_segment(board(manual), 'shot-1', 0, 2000, scene=snapshot)
    move = motion['camera_move']
    assert (move['source'], move['kind'], move['speed']) == ('manual', 'pan_left', 'slow')
    # 覆盖的是结论，不是证据：量出来的横向分量照样在，供人核对。
    assert [row['kind'] for row in move['components']] == ['truck_right']
    assert move['primary']['kind'] == 'truck_right'
    conflicts = [row for row in motion['warnings'] if row['code'] == 'MOTION_KIND_CONFLICT']
    assert len(conflicts) == 1 and 'pan_left' in conflicts[0]['message']
    assert conflicts[0]['evidence']['source'] == 'previs'


def test_manual_label_in_the_same_family_is_not_a_conflict():
    """人工写 track、预演量出 truck_right：官方把它们当同一类（机位横移），不算冲突。"""
    snapshot = scene(camera(path=[point(0, (0.0, 1.6, 0.0)), point(2, (0.35, 1.6, 0.0))],
                            target_path=[point(0, (0.0, 1.6, -5.0)), point(2, (0.35, 1.6, -5.0))]))
    manual = shot(previs=PREVIS, motions=[{'from_ms': 0, 'to_ms': 2000,
                                          'camera_move': {'kind': 'track'}}])
    motion = derive_segment(board(manual), 'shot-1', 0, 2000, scene=snapshot)
    assert motion['camera_move']['source'] == 'manual'
    assert motion['warnings'] == []


def test_layout_source_still_warns_about_a_label_that_contradicts_the_frames():
    """没有预演时，人工标注仍按框图差量比对（原来的行为不变）。"""
    flat = shot(motions=[{'from_ms': 0, 'to_ms': 2000, 'camera_move': {'kind': 'push_in'}}])
    motion = derive_segment(board(flat), 'shot-1', 0, 2000)
    assert motion['camera_move']['source'] == 'manual'
    assert [row['code'] for row in motion['warnings']] == ['MOTION_KIND_CONFLICT']


def test_a_segment_with_a_cut_is_flagged_at_the_segment_level():
    snapshot = scene(camera(camera_id='cam-1'),
                     camera(camera_id='cam-2', position=(2.0, 1.6, 0.0), target=(2.0, 1.6, -5.0)),
                     cuts=[{'time': 0.0, 'camera_id': 'cam-1'},
                           {'time': 1.0, 'camera_id': 'cam-2'}])
    motion = derive_segment(board(shot(previs=PREVIS)), 'shot-1', 0, 2000, scene=snapshot)
    assert motion['camera_move']['kind'] == 'cut'
    codes = [row['code'] for row in motion['warnings']]
    assert codes == ['SEGMENT_CROSSES_CUT']
    assert '拆成两段' in motion['warnings'][0]['message']
    # 正文只能写一个连续镜头：把「这一段有两个镜头」写进正文，模型也做不到。
    camera_line = segment_prompt(motion, 'video').splitlines()[2]
    assert 'holds one continuous take' in camera_line


def test_derivation_asks_the_previs_page_when_no_snapshot_is_given():
    """没传快照、镜头绑了预演：自己向预演页要一份 getScene（读路径，不落盘）。"""
    payload = {'name': '测试场', 'sceneId': 'scene-1', 'duration_ms': 6000, 'fps': 24,
               'aspect': '16:9', 'cuts': [],
               'entities': [{'id': 'cam-1', 'name': 'cam-1', 'kind': 'camera',
                             'position': [0.0, 1.6, 0.0], 'rotation': [0.0, 0.0, 0.0],
                             'camera': {'focal': 35.0, 'target': [0.0, 1.6, -5.0]},
                             'path': {'points': [{'time_ms': 0, 'position': [0.0, 1.6, 0.0]},
                                                 {'time_ms': 2000, 'position': [0.0, 1.6, -1.0]}]}}]}

    def caller(method, _payload):
        return {'protocol': 1, 'ok': True} if method == 'hello' else payload

    host = previs_host.PrevisHost(caller)
    previs_host.install(host)
    try:
        motion = derive_segment(board(shot(previs=PREVIS)), 'shot-1', 0, 2000)
    finally:
        previs_host.uninstall()
    assert motion['camera_move']['source'] == 'previs'
    assert motion['camera_move']['kind'] == 'dolly_in'
    assert previs_host.installed() is None


# ------------------------------------------------------------------ 提示词
def test_prompt_writes_both_components_with_official_words():
    """组合运镜：主分量「动词短语＋幅度＋速度」，次要分量跟在 while 后面；句内不出现数字。"""
    snapshot = scene(camera(path=[point(0, (0.0, 1.6, 0.0)), point(2, (0.6, 1.6, 0.0))],
                            target_path=[point(0, (0.0, 1.6, -5.0)), point(2, (0.6, 1.6, -5.0))],
                            focal_keys=[focal_key(0, 35.0), focal_key(2, 40.0)]))
    motion = derive_segment(board(shot(previs=PREVIS, motions=[
        {'from_ms': 0, 'to_ms': 2000, 'camera_move': {'kind': 'truck_right', 'speed': 'slow'}}])),
        'shot-1', 0, 2000, scene=snapshot)
    assert motion['warnings'] == []
    prompt = segment_prompt(motion, 'video', duration_ms=2000)
    camera_line = prompt.splitlines()[2]
    assert camera_line.startswith('[Shot 1] 运镜：')
    assert ('The camera trucks right with large amplitude at slow speed '
            'while zooming in slightly.') in camera_line
    # 官方词表：用动词短语写动作，不并列大写的术语名，也不堆标签。
    assert 'Truck Right' not in camera_line and 'Zoom In' not in camera_line
    # 裸数字只留在结构化字段里：运镜句里一个数字都没有（镜号 [Shot 1] 是官方记法，不算）。
    sentence = camera_line.split('运镜：', 1)[1]
    assert not any(character.isdigit() for character in sentence)
    for banned in ('不得', '不许'):
        assert banned not in prompt


def test_prompt_of_a_pure_previs_move_needs_no_human_label():
    """光有预演推导（没有任何人工标注）也要写得出来：幅度由分量给，速度不写。"""
    snapshot = scene(camera(focal_keys=[focal_key(0, 35.0), focal_key(2, 60.0)]))
    motion = derive_segment(board(shot(previs=PREVIS)), 'shot-1', 0, 2000, scene=snapshot)
    assert motion['camera_move']['source'] == 'previs'
    camera_line = segment_prompt(motion, 'video').splitlines()[2]
    # 35→60mm 是 71% 的相对变化 → 大幅；没有人工速度就不写速度。
    assert 'The camera zooms in with large amplitude.' in camera_line
    assert 'at slow speed' not in camera_line
    assert '不得' not in camera_line


# ------------------------------------------------------------------ 校验与词表
def test_derived_moves_pass_validation_without_being_treated_as_annotations():
    """推导结果带 source/components/cut，校验不该把它当成「人手写的标注」拒掉。"""
    snapshot = scene(camera(path=[point(0, (0.0, 1.6, 0.0)), point(2, (0.35, 1.6, 0.0))],
                            target_path=[point(0, (0.0, 1.6, -5.0)), point(2, (0.35, 1.6, -5.0))]))
    move = move_of(snapshot)
    assert validate_segment({'from_ms': 0, 'to_ms': 2000, 'camera_move': move}) is None
    # 手写的标注仍只认词表里的种类（缺失的写下不认识的种类照样报错）。
    with pytest.raises(ValueError, match='运镜种类'):
        validate_segment({'from_ms': 0, 'to_ms': 900, 'camera_move': {'kind': 'zoom'}})
    with pytest.raises(ValueError, match='不认识的字段'):
        validate_segment({'from_ms': 0, 'to_ms': 900,
                          'camera_move': {'kind': 'pan_left', 'components': []}})


def test_the_new_kinds_are_in_the_vocabulary_and_in_the_official_terms():
    for kind in ('zoom_in', 'zoom_out', 'dolly_in', 'dolly_out', 'truck_left', 'truck_right',
                 'pedestal_up', 'pedestal_down'):
        assert kind in CAMERA_MOVES


def test_ops_forwards_the_injected_snapshot(monkeypatch):
    """``read_segment_motion(..., scene_snapshot=…)`` 必须真的把快照交给推导。

    这条守住「快照是正式形参」：不靠开预演页、也不靠测试去改环境变量。
    """
    from narrative import storyboard_operations as so

    captured = {}

    def fake_derive(board_row, shot_row, from_ms, to_ms, *, scene=None):
        captured['scene'] = scene
        captured['range'] = (from_ms, to_ms)
        return {'from_ms': from_ms, 'to_ms': to_ms, 'available': True, 'warnings': [],
                'camera_move': {'kind': 'truck_right', 'source': 'previs', 'components': []}}

    class FakeOps(so.StoryboardOperationsMixin):
        def read_storyboard(self, unit_id):
            return board(shot())

    monkeypatch.setattr(so, 'derive_segment', fake_derive)
    monkeypatch.setattr(so, 'validate_segment', lambda motion, limit: None)
    monkeypatch.setattr(so, 'segment_prompt', lambda motion, kind: '提示词')

    snapshot = scene(camera(path=[point(0, (0.0, 1.6, 0.0)), point(2, (0.35, 1.6, 0.0))]))
    result = FakeOps().read_segment_motion('unit-1', 'shot-1', 0, 2000, scene_snapshot=snapshot)

    assert captured['scene'] is snapshot
    assert captured['range'] == (0, 2000)
    assert result['motion']['camera_move']['source'] == 'previs'
