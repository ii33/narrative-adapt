"""分段运镜：从前后两帧的框图推导运动，再把推导折成视频提示词。

要点：
* 主体只按 asset_id 配对（同一资产前后都在才算一对）；方向看框中心 x（阈值 0.05），
  表观运动看框面积（相对变化 15%）；
* 景别与 ``previs_scene.framing_of`` 同一口径，本模块不另写阈值；
* 标注的运镜与框图对不上只报 warning（不改数据、不拦写入）：kind 是手写的，最容易和构图脱节；
* 提示词按 H3 官方规范（``docs/MINIMAX_H3_PROMPT.md``）：运镜用官方英文词表写在句内、
  首尾帧模式先放官方对齐句、毫秒只用 ``MM:SS.mmm``、约束段只写正向保持项；
* 只有 video 的生成任务带 motion，草图与风格帧仍是「这一帧」的静态描述。
"""
import json
from copy import deepcopy

import pytest
from PIL import Image
from test_screenplay_writing import project as project
from test_storyboard_operations import setup_board

from narrative.generation_task import build_task, task_prompt
from narrative.segment_motion import (
    derive_segment,
    segment_prompt,
    validate_segment,
)


# ------------------------------------------------------------------ 纯函数用的迷你分镜
def box(asset_id, bbox, *, depth=2.0, kind='person'):
    return {'asset_id': asset_id, 'bbox': list(bbox), 'depth': depth, 'kind': kind}


def layout(objects, *, x=0.0, z=0.0, yaw=0.0, pitch=0.0, lens_mm=35.0):
    return {'camera': {'x': x, 'y': 1.6, 'z': z, 'yaw': yaw, 'pitch': pitch, 'lens_mm': lens_mm,
                       'framing': 'medium'},
            'objects': objects, 'unmet_needs': [], 'source': 'manual'}


def shot(*, duration=2000, offsets=(0,), layouts=None, plan=None, motions=None, aspect='16:9'):
    frames = [{'id': f'frame-{offset}', 'offset_ms': offset, 'sketch_path': 'sketch.png',
               'style_path': 'style.png'} for offset in offsets]
    timeline = {'keyframes': [{'offset_ms': offset, 'moment_name': name, 'description': text}
                              for offset, name, text in (plan or [(row, '', '') for row in offsets])]}
    row = {'id': 'shot-1', 'title': '窗边', 'duration_ms': duration, 'source_block_ids': [],
           'action_start_ms': 0, 'keyframes': frames, 'timeline': timeline,
           'layouts': {str(offset): value for offset, value in (layouts or {}).items()}}
    if aspect:
        row['aspect_ratio'] = aspect
    if motions:
        row['segment_motions'] = motions
    return row


def board(row):
    return {'unit_id': 'unit-1', 'revision': 1, 'approved': False, 'fps': 24, 'objects': [],
            'markers': [], 'asset_needs': [], 'shots': [row]}


# ------------------------------------------------------------------ 派生
def test_derive_pairs_subjects_and_reads_direction_and_apparent_motion():
    start = layout([
        box('A1', [0.10, 0.30, 0.30, 0.86]),   # 中心x .20、面积 .112、高 .56 → 中景
        box('A2', [0.60, 0.20, 0.70, 0.40]),   # 中心x .65、面积 .020
        box('A3', [0.80, 0.55, 0.90, 0.72]),   # 只在首帧出现：配不上对，不进清单
    ])
    end = layout([
        box('A1', [0.24, 0.30, 0.44, 0.86]),   # 中心右移 .14、面积不变 → 横向移动
        box('A2', [0.58, 0.12, 0.72, 0.52]),   # 面积 .056（+180%）→ 靠近
    ])
    motion = derive_segment(board(shot(layouts={0: start, 1200: end})), 'shot-1', 0, 1200)
    assert motion['available'] is True
    assert (motion['from_ms'], motion['to_ms'], motion['duration_ms']) == (0, 1200, 1200)
    # 按首帧面积从大到小排：主要主体排在最前，提示词里的「第一个主体」就是它。
    assert [row['subject'] for row in motion['subject_moves']] == ['A1', 'A2']
    first, second = motion['subject_moves']
    assert first['direction'] == 'right' and first['apparent_motion'] == 'lateral'
    assert first['from_bbox'] == [0.10, 0.30, 0.30, 0.86]
    assert first['to_bbox'] == [0.24, 0.30, 0.44, 0.86]
    assert second['apparent_motion'] == 'approach' and second['area_ratio'] > 0.15
    # 景别按两帧里最高的主体（A1 高 .56）判：中景，两帧没变。
    assert (motion['camera_move']['from_framing'], motion['camera_move']['to_framing']) == (
        'medium', 'medium')
    assert motion['framing_change'] == 'same'
    assert motion['warnings'] == []


def test_derive_reports_framing_change_and_the_center_case():
    start = layout([box('A1', [0.20, 0.45, 0.40, 0.62])])   # 高 .17 → 全景
    end = layout([box('A1', [0.26, 0.02, 0.52, 0.95])])     # 高 .93 且贴上沿 → 近景
    motion = derive_segment(board(shot(layouts={0: start, 1200: end})), 'shot-1', 0, 1200)
    assert motion['camera_move']['from_framing'] == 'wide'
    assert motion['camera_move']['to_framing'] == 'close'
    assert motion['framing_change'] == 'closer'
    assert motion['subject_moves'][0]['apparent_motion'] == 'approach'
    # 没横移又停在画面中间 → center；停在边上才是 none。
    still = layout([box('A1', [0.40, 0.30, 0.60, 0.70])])
    middle = derive_segment(board(shot(layouts={0: still, 1200: still})), 'shot-1', 0, 1200)
    assert middle['subject_moves'][0]['direction'] == 'center'
    assert middle['subject_moves'][0]['apparent_motion'] == 'none'
    aside = layout([box('A1', [0.72, 0.30, 0.92, 0.70])])
    edge = derive_segment(board(shot(layouts={0: aside, 1200: aside})), 'shot-1', 0, 1200)
    assert edge['subject_moves'][0]['direction'] == 'none'


def test_derive_explains_missing_layout_and_unpaired_subjects():
    only_start = board(shot(layouts={0: layout([box('A1', [0.2, 0.3, 0.4, 0.7])])}))
    missing = derive_segment(only_start, 'shot-1', 0, 1200)
    assert missing['available'] is False and '框图' in missing['reason']
    # 两端都有主体却没有同一个 asset_id：不静默给空清单，而是说清配对失败。
    left = layout([box('A1', [0.20, 0.30, 0.40, 0.70])])
    right = layout([box('B1', [0.22, 0.30, 0.42, 0.70])])
    unpaired = derive_segment(board(shot(layouts={0: left, 1200: right})), 'shot-1', 0, 1200)
    assert unpaired['available'] is True and unpaired['subject_moves'] == []
    assert [row['code'] for row in unpaired['warnings']] == ['NO_SUBJECT_PAIR']


# ------------------------------------------------------------------ 一致性
def test_labeled_camera_move_is_checked_against_the_two_frames():
    same = layout([box('A1', [0.30, 0.20, 0.50, 0.70])])

    def with_label(kind, end_layout):
        return derive_segment(board(shot(
            layouts={0: same, 1200: end_layout},
            motions=[{'from_ms': 0, 'to_ms': 1200, 'camera_move': {'kind': kind}}])),
            'shot-1', 0, 1200)

    # 标了 push_in，但焦距、景别与主体面积都没变 → 对不上。
    flat = with_label('push_in', deepcopy(same))
    assert [row['code'] for row in flat['warnings']] == ['MOTION_KIND_CONFLICT']
    assert 'push_in' in flat['warnings'][0]['message']
    # 标了 static，但机位横向挪了 .4 米 → 同样对不上。
    moved = with_label('static', layout([box('A1', [0.30, 0.20, 0.50, 0.70])], x=0.4))
    assert [row['code'] for row in moved['warnings']] == ['MOTION_KIND_CONFLICT']
    assert 'static' in moved['warnings'][0]['message']
    # 焦距真的变长就一致：同一个 push_in 不再报。
    assert with_label('push_in', layout([box('A1', [0.30, 0.20, 0.50, 0.70])], lens_mm=55))['warnings'] == []


# ------------------------------------------------------------------ 提示词
def motion_fixture():
    start = layout([box('A1', [0.10, 0.30, 0.30, 0.86])], lens_mm=35, z=0.0)
    end = layout([box('A1', [0.12, 0.02, 0.36, 0.97])], lens_mm=55, z=0.5)  # 机身推进、收紧到近景
    return derive_segment(board(shot(
        layouts={0: start, 1200: end},
        plan=[(0, '起手', '甲抬头'), (1200, '递伞', '手指接触')],
        motions=[{'from_ms': 0, 'to_ms': 1200, 'notes': '窗外雨更大',
                  'camera_move': {'kind': 'push_in', 'speed': 'slow'}}])), 'shot-1', 0, 1200)


def test_segment_prompt_uses_the_official_h3_shapes():
    motion = motion_fixture()
    assert motion['warnings'] == []
    prompt = segment_prompt(motion, 'video', duration_ms=5000)
    lines = prompt.splitlines()
    # 首行逐字用官方 FL2VA 对齐句（S.SS 两位小数秒），空一行，再写单镜头路径。
    assert lines[0] == ('How the reference pictures align with the target video — '
                        'Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; '
                        'Picture 2 (from Shot 1) aligns with the 5.00-second mark of the target video.')
    assert lines[1] == ''
    assert lines[2].startswith('[Shot 1] 运镜：')
    assert [row.split('：')[0] for row in lines[2:]] == ['[Shot 1] 运镜', '主体', '环境', '约束']
    # 运镜用官方英文词表写在句内：类型＋幅度＋速度，不并列中文运镜词、不用旧版方括号命令。
    camera = lines[2]
    assert 'Push In' in camera and 'with large amplitude' in camera and 'at slow speed' in camera
    assert '推近' not in camera and '[' not in camera.replace('[Shot 1]', '')
    # 环境一段来自两端关键帧描述的差量＋人工备注。
    assert '甲抬头' in prompt and '手指接触' in prompt and '窗外雨更大' in prompt
    # 数字只留在结构化字段：没有 bbox、没有裸毫秒，时长只以 MM:SS.mmm 出现。
    for value in ('0.1', '0.12', '0.3', '0.36', '0.86', '0.97', '0.55', '5000', '1200'):
        assert value not in prompt
    assert '00:00.000' in prompt and '00:05.000' in prompt
    # 约束段去否定化：官方没有负面提示词清单，只写正向保持项。
    constraints = lines[-1]
    assert '不得' not in constraints and '不许' not in constraints
    assert '保持不变' in constraints and '单镜头' in constraints and 'Picture 2' in constraints
    for banned in ('不得', '不许', '视频模型不接受运镜参数'):
        assert banned not in prompt
    # 静态帧（草图／风格图）没有对齐句，也不带运镜。
    still = segment_prompt(motion, 'sketch')
    assert still.splitlines()[0] == '运镜：静帧：只表现这一帧的构图与机位，画面停在这一刻。'
    assert 'Picture 1' not in still and '不得' not in still
    with pytest.raises(ValueError, match='提示词类型'):
        segment_prompt(motion, 'animatic')


def test_segment_prompt_picks_the_alignment_line_by_picture_count():
    motion = motion_fixture()
    single = segment_prompt(motion, 'video', duration_ms=1200, pictures=1)
    # 只有首帧时不能凭空写 Picture 2：改用官方 I2VA 那句（逐字）。
    assert single.splitlines()[0] == ('For the target video, at 0.00 seconds into the target video, '
                                      '<Picture 1> (from [Shot 1]) is fully referenced.')
    assert 'Picture 2' not in single.splitlines()[0]
    with pytest.raises(ValueError, match='首尾帧数量'):
        segment_prompt(motion, 'video', pictures=3)


# ------------------------------------------------------------------ 校验
def test_validate_segment_rejects_out_of_shape_segments():
    with pytest.raises(ValueError, match='单段上限'):
        validate_segment({'from_ms': 0, 'to_ms': 3000}, 2000)
    # 默认上限就是 2000ms；场级放宽后同一个段合法（storyboard_operations 读的就是这个参数）。
    with pytest.raises(ValueError, match='单段上限'):
        validate_segment({'from_ms': 0, 'to_ms': 2500})
    assert validate_segment({'from_ms': 0, 'to_ms': 2500}, 3000) is None
    with pytest.raises(ValueError, match='起点须小于终点'):
        validate_segment({'from_ms': 1200, 'to_ms': 1200})
    with pytest.raises(ValueError, match='运镜种类'):
        validate_segment({'from_ms': 0, 'to_ms': 900, 'camera_move': {'kind': 'zoom'}})
    with pytest.raises(ValueError, match='运镜速度'):
        validate_segment({'from_ms': 0, 'to_ms': 900, 'camera_move': {'kind': 'static',
                                                                    'speed': 'slowest'}})


# ------------------------------------------------------------------ 任务包
def test_build_task_carries_motion_only_for_video():
    current = board(shot(offsets=(0, 1200),
                         layouts={0: layout([box('A1', [0.10, 0.30, 0.30, 0.86])], lens_mm=35),
                                  1200: layout([box('A1', [0.12, 0.02, 0.36, 0.97])], lens_mm=55)},
                         plan=[(0, '起手', '甲抬头'), (1200, '递伞', '手指接触')],
                         motions=[{'from_ms': 0, 'to_ms': 1200,
                                   'camera_move': {'kind': 'push_in'}}]))
    video = build_task('video', shot_id='shot-1', offset_ms=0, board=current)
    assert video['motion']['available'] is True
    assert video['motion']['subject_moves'][0]['apparent_motion'] == 'approach'
    # 只有焦距变了（机位没动）→ 官方叫 Zoom In，不叫 Push In。
    lines = video['prompt'].splitlines()
    assert lines[0].startswith('How the reference pictures align') and lines[1] == ''
    assert 'Zoom In' in lines[2] and 'at fast speed' not in video['prompt']
    # 对齐句里的 S.SS 用请求时长（默认镜长 2000ms → 2.00），回执说明里单独成段。
    assert 'aligns with the 2.00-second mark' in lines[0]
    assert '00:02.000' in video['prompt'] and '2000' not in video['prompt']
    rendered = task_prompt(video)
    assert '运动衔接' in rendered and '[Shot 1] 运镜：' in rendered
    # 草图与风格帧是静态图：带上运镜只会让模型把运动画进这一帧。
    for kind in ('sketch', 'style'):
        static = build_task(kind, shot_id='shot-1', offset_ms=0, board=current)
        assert 'motion' not in static and static['prompt'] == ''


def test_video_prompt_keeps_the_human_words_on_top_of_the_derived_block():
    current = board(shot(layouts={0: layout([box('A1', [0.10, 0.30, 0.30, 0.86])]),
                                  1200: layout([box('A1', [0.12, 0.02, 0.36, 0.97])])}))
    derived = build_task('video', shot_id='shot-1', offset_ms=0, board=current)['prompt']
    task = build_task('video', shot_id='shot-1', offset_ms=0, board=current,
                      prompt='雨伞始终在画面外')
    # 人工写在最后并声明优先；机器四段一个字都没被改写掉。
    assert task['prompt'].startswith(derived)
    assert task['prompt'].endswith('雨伞始终在画面外') and '以这里为准' in task['prompt']
    assert len(task['prompt'].splitlines()) == len(derived.splitlines()) + 1


def test_video_task_explains_a_segment_it_cannot_derive():
    current = board(shot(layouts={0: layout([box('A1', [0.10, 0.30, 0.30, 0.86])])}))
    task = build_task('video', shot_id='shot-1', offset_ms=0, board=current)
    assert task['motion']['available'] is False and '框图' in task['motion']['reason']
    # 本镜只有一张风格帧：对齐句用官方 I2VA 那句，不能凭空写 Picture 2。
    lines = task['prompt'].splitlines()
    assert lines[0] == ('For the target video, at 0.00 seconds into the target video, '
                        '<Picture 1> (from [Shot 1]) is fully referenced.')
    assert '框图' in task['prompt']


# ------------------------------------------------------------------ 进读视图与工具
def apply(ops, unit, changes):
    return ops.apply_storyboard_changes(unit, ops.read_storyboard(unit)['revision'], changes)


def problem(ops, unit, changes):
    """非法内容可能进暂存（staged），也可能直接抛：两种都要拿到那句说明。"""
    try:
        result = apply(ops, unit, changes)
    except ValueError as error:
        return str(error)
    assert result.get('status') == 'staged' and result['problems'], result
    return json.dumps(result['problems'], ensure_ascii=False)


@pytest.mark.slow
def test_storyboard_view_and_tool_carry_the_segment_motion(project, tmp_path):
    ops, unit, block = setup_board(project)
    image = tmp_path / 'frame.png'
    Image.new('RGB', (16, 9), 'gray').save(image)
    created = ops.apply_storyboard_changes(unit, 0, [
        {'op': 'create_shot', 'title': '推近', 'duration_ms': 3000, 'source_block_ids': [block]}])
    shot_id = created['shots'][0]['id']
    ops.apply_storyboard_changes(unit, created['revision'], [
        {'op': 'set_keyframe', 'shot_id': shot_id, 'offset_ms': 0, 'sketch_path': str(image)},
        {'op': 'set_keyframe', 'shot_id': shot_id, 'offset_ms': 1200, 'sketch_path': str(image)},
        {'op': 'set_keyframe_layout', 'shot_id': shot_id, 'offset_ms': 0,
         'layout': {'camera': {'lens_mm': 35}, 'objects': [box('A1', [0.10, 0.30, 0.30, 0.86])],
                    'unmet_needs': []}},
        {'op': 'set_keyframe_layout', 'shot_id': shot_id, 'offset_ms': 1200,
         'layout': {'camera': {'lens_mm': 55}, 'objects': [box('A1', [0.12, 0.02, 0.36, 0.97])],
                    'unmet_needs': []}}])
    annotated = apply(ops, unit, [
        {'op': 'set_segment_motion', 'shot_id': shot_id, 'from_ms': 0, 'to_ms': 1200,
         'camera_move': {'kind': 'push_in', 'speed': 'slow'}, 'notes': '窗外雨更大'}])
    # 读视图：每段都带 motion（第二段缺终点框图，所以是 available=false）。
    first, second = annotated['shots'][0]['segments']
    assert (first['from_ms'], first['to_ms'], first['duration_ms']) == (0, 1200, 1200)
    assert first['motion']['available'] is True
    assert first['motion']['camera_move']['kind'] == 'push_in'
    assert first['motion']['camera_move']['from_framing'] == 'medium'
    assert second['motion']['available'] is False and '框图' in second['motion']['reason']
    # 落库的只有人给的那几项：派生字段一律现算。
    with ops._store.connect() as db:
        stored = json.loads(db.execute('SELECT value FROM meta WHERE key=?',
                                       ('storyboard:' + unit,)).fetchone()[0])
    assert stored['shots'][0]['segment_motions'] == [
        {'from_ms': 0, 'to_ms': 1200, 'camera_move': {'kind': 'push_in', 'speed': 'slow'},
         'notes': '窗外雨更大'}]
    # 只读工具给出同一份推导＋拼好的提示词与校验结论。
    detail = ops.read_segment_motion(unit, shot_id, 0, 1200)
    assert detail['duration_limit_ms'] == 2000 and detail['warnings'] == []
    assert detail['motion'] == first['motion']
    # 机位没动、只改了焦距 → 官方 Zoom In；对齐句用段长当请求时长（1.20 秒）。
    lines = detail['prompt'].splitlines()
    assert lines[0].startswith('How the reference pictures align')
    assert 'Zoom In' in lines[2] and '窗外雨更大' in detail['prompt']
    assert 'aligns with the 1.20-second mark' in lines[0] and '1200' not in detail['prompt']
    assert '不得' not in detail['prompt'] and '不许' not in detail['prompt']
    # 段不存在就不许标注：挂在一条不存在的区间上谁也读不到。
    assert '分段' in problem(ops, unit, [{'op': 'set_segment_motion', 'shot_id': shot_id,
                                          'from_ms': 0, 'to_ms': 900}])
    assert '运镜种类' in problem(ops, unit, [{'op': 'set_segment_motion', 'shot_id': shot_id,
                                              'from_ms': 0, 'to_ms': 1200,
                                              'camera_move': {'kind': 'zoom'}}])
    # kind 与 notes 都不给＝取消这一段的标注（不存一行空标注）。
    cleared = apply(ops, unit, [{'op': 'set_segment_motion', 'shot_id': shot_id,
                                 'from_ms': 0, 'to_ms': 1200, 'camera_move': None, 'notes': ''}])
    assert 'segment_motions' not in cleared['shots'][0]
    assert cleared['shots'][0]['segments'][0]['motion']['camera_move']['kind'] is None
