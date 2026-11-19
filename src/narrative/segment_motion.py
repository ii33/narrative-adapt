"""镜头运动（段与段之间的差量）：把前后两帧的框图折成一段能送进视频模型的结构化描述。

为什么单独一层：帧是静态的（框图＋关键帧描述），运动只发生在**段之间**。把运动挂到帧上
说不清「从哪一帧动到哪一帧」；把段里的数值当人工输入存起来，又会跟框图脱节——改一帧构图，
那段数字立刻过期。所以本模块只做两件事：

* ``derive_segment``：从段两端两帧的框图与机位**算出**主体位移、方向、画面占比变化、景别差
  与机位位移；人只给运镜种类、速度与一句备注（见 ``storyboard_operations.set_segment_motion``）。
  派生字段一律现算不落库，所以同一个段在界面、助手与生成任务里读到的是同一份推导。
* ``segment_prompt``：把这些字段折成「运镜／主体／环境／约束」四段。H3 没有结构化的运镜
  参数，运镜要写进提示词正文的自然英语动作里，且用官方词表（类型＋幅度＋速度，见
  ``docs/MINIMAX_H3_PROMPT.md``）：``endpoints``（首尾帧）先放官方 FL2VA 对齐句，
  再写单镜头路径。

阈值集中在下面几个常量里；景别一律问 ``previs_scene.framing_of``——那是全工程唯一的景别
口径（1.75 m 的人在取景框里占多高），在这里另写一套 0.45/0.90 只会让两边慢慢走散。
"""
from __future__ import annotations

from math import hypot

from .previs_scene import framing_of
from .storyboard_layout import CAMERA_DEFAULTS, layout_at

# 运镜种类：给 schema、校验与警告消息共用一张表；中文说明只给人看（提示词用 H3 官方词表
# H3_CAMERA_TERMS 折成英文，见 docs/MINIMAX_H3_PROMPT.md）。
CAMERA_MOVES = {
    'push_in': '推近：机位前移或焦距变长，主体在画面里持续变大',
    'pull_out': '拉远：机位后移或焦距变短，主体在画面里持续变小',
    'pan_left': '左摇：机位原地左转，景物整体向右滑过画面',
    'pan_right': '右摇：机位原地右转，景物整体向左滑过画面',
    'tilt_up': '上摇：俯仰抬高，地平线下沉、上方景物进入画面',
    'tilt_down': '下摇：俯仰压低，地平线上浮、下方景物进入画面',
    'track': '跟移：机位横向随主体平移，主体在画面里的位置基本不变、背景整体后退',
    'static': '固定：机位与焦距都不动，只有主体自身的动作',
}
CAMERA_SPEEDS = {'slow': '缓慢', 'medium': '匀速', 'fast': '快速'}
# 画面归一化坐标下的判定阈值（框图 bbox 就是 0..1 的画面坐标，见 storyboard_layout）。
LATERAL_THRESHOLD = 0.05   # 框中心 x 变化超过 5% 才算「横移」，低于它就只是取景抖动
CENTER_BAND = (0.45, 0.55)  # 没横移又停在这条带里＝「居中不动」，否则是「不动但不在中心」
AREA_THRESHOLD = 0.15      # 框面积相对变化 15% 才说「靠近／远离」，再小读成「横向」更诚实
LENS_TOL_MM = 1.0          # 等效焦距差 1mm 起算变焦；更小的差多半是补默认值时的抖动
YAW_TOL_DEG = 2.0          # 偏航差 2° 起算摇镜
PITCH_TOL_DEG = 1.5        # 俯仰差 1.5° 起算上下摇
SHIFT_TOL_M = 0.10         # 机位水平位移 0.1m 起算「动了」：再小就是同一机位的抖动
# 与 storyboard_operations 共用同一个上限默认值：分镜静帧本来就不该一格里塞满两秒以上的动作。
DEFAULT_MAX_SEGMENT_MS = 2000
# 景别由松到紧的顺序：只用来判「这一段在收紧还是松开」，阈值仍由 framing_of 决定。
FRAMING_ORDER = {'wide': 0, 'medium': 1, 'close': 2}
# H3 官方运镜词表（docs/MINIMAX_H3_PROMPT.md §3）：我们的种类折成官方表达。运镜写进正文的
# 自然英语动作里，用「类型＋幅度＋速度」，不在句尾堆标签；中文运镜词模型不认识。
H3_CAMERA_TERMS = {
    'push_in': 'Push In',
    'pull_out': 'Pull Out',
    'pan_left': 'Pan Left',
    'pan_right': 'Pan Right',
    'tilt_up': 'Tilt Up',
    'tilt_down': 'Tilt Down',
    'track': 'Tracking Shot',
    'static': 'Static Shot',
}
H3_SPEEDS = {'slow': 'at slow speed', 'fast': 'at fast speed'}
H3_FRAMING_TERMS = {'wide': 'Wide Shot', 'medium': 'Medium Shot', 'close': 'Close-Up'}
# 官方 FL2VA / I2VA 对齐句：必须逐字照抄，只换时长（S.SS 两位小数秒）与镜号。
H3_FL2VA_LINE = ('How the reference pictures align with the target video — '
                 'Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; '
                 'Picture 2 (from Shot 1) aligns with the {seconds}-second mark of the target video.')
H3_I2VA_LINE = ('For the target video, at 0.00 seconds into the target video, '
                '<Picture 1> (from [Shot 1]) is fully referenced.')
# 幅度的判据（官方的「幅度」是构图变化范围）：跨一档景别或画面占比变化过半算大，几乎没变算小，
# 其余不写——官方默认中等幅度，明写「中等」只是噪音。
LARGE_AREA_RATIO = 0.5
LARGE_YAW_DEG = 20.0
SMALL_YAW_DEG = 5.0
LARGE_PITCH_DEG = 15.0
SMALL_PITCH_DEG = 4.0
LARGE_SHIFT_M = 0.6
SMALL_SHIFT_M = 0.2
_DIRECTIONS = ('left', 'right', 'center', 'none')
_ORDINALS = ('第一个主体', '第二个主体', '第三个主体')
_KINDS = ('sketch', 'style', 'video')


def camera_move_kinds():
    """运镜种类表（种类 → 中文说明）：schema 的 enum 与校验读这一份。

    提示词不读这里的说明：它要用 H3 官方英文词表（H3_CAMERA_TERMS），中文运镜词模型不认识。
    """
    return dict(CAMERA_MOVES)


def camera_move_speeds():
    """运镜速度表（slow/medium/fast → 中文）：只描述快慢，不换算成毫秒。

    官方口径把速度写成 ``at slow speed``／``at fast speed``，中等速默认省略。
    """
    return dict(CAMERA_SPEEDS)


# -- 分段 ---------------------------------------------------------------------
def derive_segments(offsets, duration):
    """相邻关键帧成段：缺省覆盖整镜，首尾补到0与时长，免得留下没人管的空档。

    这条规则只在段这一层说一次：读视图、生成任务与校验都问它，不各自再算一遍。
    """
    points = sorted({offset for offset in offsets if 0 <= offset <= duration})
    if not points or points[0] != 0:
        points.insert(0, 0)
    if points[-1] != duration:
        points.append(duration)
    return [[start, end] for start, end in zip(points, points[1:], strict=False) if end > start]


def segments_of(shot):
    """该镜的分段（[from, to] 毫秒区间）：显式覆盖优先，否则按已有帧推导。"""
    duration = shot.get('duration_ms')
    if type(duration) is not int or duration < 1:
        raise ValueError('镜头时长无效，无法推导分段')
    stored = _plan(shot).get('segments')
    if stored:
        return [[int(row[0]), int(row[1])] for row in stored]
    offsets = {row['offset_ms'] for row in _plan(shot).get('keyframes') or []}
    offsets |= {frame['offset_ms'] for frame in shot.get('keyframes') or []}
    return derive_segments(offsets, duration)


def segment_at(board, shot, offset_ms):
    """该时刻所在的那一段（优先取以它为起点的一段）的运动推导。

    视频是按「首帧→尾帧」生成的，offset_ms 通常就是段的起点；找不到起点就退到包含它的段。
    算不出来（缺框图、时间轴自相矛盾）时返回 ``{available: false, reason}``，不抛错：
    任务包要能照样组出来，把「为什么没有运镜」写给模型与人看。
    """
    shot = _shot_of(board, shot)
    offset = _offset(offset_ms, '镜内位置')
    rows = segments_of(shot)
    pair = next((row for row in rows if row[0] == offset), None)
    if pair is None:
        pair = next((row for row in rows if row[0] <= offset <= row[1]), None)
    if pair is None:
        return {'available': False, 'from_ms': offset, 'to_ms': offset, 'duration_ms': 0,
                'reason': f'镜内{offset}ms不在任何分段里；先 read_storyboard 核对时间轴与关键帧'}
    try:
        return derive_segment(board, shot, pair[0], pair[1])
    except ValueError as error:
        return {'available': False, 'from_ms': pair[0], 'to_ms': pair[1],
                'duration_ms': pair[1] - pair[0], 'reason': str(error)}


def segment_views(board, shot):
    """读视图用的分段清单：每一段都附带运动推导，算不出来就带 ``{available: false, reason}``。"""
    rows = []
    for start, end in segments_of(shot):
        try:
            motion = derive_segment(board, shot, start, end)
        except ValueError as error:
            motion = {'available': False, 'reason': str(error)}
        rows.append({'from_ms': start, 'to_ms': end, 'duration_ms': end - start, 'motion': motion})
    return rows


def stored_motion(shot, from_ms, to_ms):
    """人工标注的那几项（kind/speed/notes）：派生字段不落库，所以这里不可能读到别的。"""
    for row in shot.get('segment_motions') or []:
        if row.get('from_ms') == from_ms and row.get('to_ms') == to_ms:
            return row
    return None


# -- 推导 ---------------------------------------------------------------------
def derive_segment(board, shot, from_ms, to_ms):
    """从段两端两帧的框图算出这一段「发生了什么」。

    输出形状（``available`` 为真时）::

        {from_ms, to_ms, duration_ms,
         camera_move: {kind, speed, from_framing, to_framing},
         subject_moves: [{subject, kind, from_bbox, to_bbox, direction, apparent_motion,
                          area_ratio, depth_delta}],
         camera: {dx_m, dz_m, shift_m, lens_delta_mm, yaw_delta_deg, pitch_delta_deg},
         framing_change: closer|farther|same,
         from_description, to_description, notes, warnings: [{code, message, ...}]}

    ``board`` 除了在 ``shot`` 传镜头编号时用来查那条镜头以外不参与推导：推导只认这一镜的
    框图与关键帧，免得同一段在两个入口算出两个结果。
    """
    shot = _shot_of(board, shot)
    start, end = _offset(from_ms, '分段起点'), _offset(to_ms, '分段终点')
    if start >= end:
        raise ValueError(f'分段{start}..{end}ms的起点须小于终点')
    from_layout, to_layout = layout_at(shot, start), layout_at(shot, end)
    stored = stored_motion(shot, start, end) or {}
    given = stored.get('camera_move') if isinstance(stored.get('camera_move'), dict) else {}
    camera_move = {'kind': given.get('kind'), 'speed': given.get('speed'),
                   'from_framing': None, 'to_framing': None}
    common = {'from_ms': start, 'to_ms': end, 'duration_ms': end - start, 'camera_move': camera_move,
              'notes': stored.get('notes') or '',
              'from_description': _description(shot, start), 'to_description': _description(shot, end)}
    missing = [label for label, row in (('起点帧', from_layout), ('终点帧', to_layout)) if row is None]
    if missing:
        return {'available': False, **common,
                'reason': '、'.join(missing) + '没有框图（layout），推导不出运动；先为两端的帧写入框图'}
    aspect = _aspect(shot)
    # 景别按「这一帧里最高的主体」判：谁占得多，观众看到的就是谁的景别。
    camera_move['from_framing'] = _framing(from_layout, aspect)
    camera_move['to_framing'] = _framing(to_layout, aspect)
    camera = _camera_deltas(from_layout, to_layout)
    moves = _subject_moves(from_layout, to_layout)
    change = _framing_change(camera_move['from_framing'], camera_move['to_framing'])
    warnings = _consistency(camera_move, moves, change, camera)
    if not moves and (_subjects(from_layout) or _subjects(to_layout)):
        # 前后两帧都有主体却配不上对：多半是 asset_id 换了（换了卡或手写框图），
        # 这时方向与占比变化算不出来，要说清楚而不是静默给一份空清单。
        warnings.append({'code': 'NO_SUBJECT_PAIR',
                         'message': '前后两帧没有同一个asset_id，主体配不上对，方向与画面占比变化算不出来',
                         'shot_id': shot.get('id')})
    return {'available': True, **common, 'subject_moves': moves, 'camera': camera,
            'framing_change': change, 'warnings': warnings}


def validate_segment(segment, max_ms=DEFAULT_MAX_SEGMENT_MS):
    """校验一段运动（人工标注行或推导结果）：只查数据是否成立，不判创作意图。"""
    if not isinstance(segment, dict):
        raise ValueError('分段须为对象')
    if type(max_ms) is not int or max_ms < 1:
        raise ValueError('单段上限须为正整数毫秒')
    start = _offset(segment.get('from_ms'), '分段起点')
    end = _offset(segment.get('to_ms'), '分段终点')
    if start >= end:
        raise ValueError(f'分段{start}..{end}ms的起点须小于终点')
    if end - start > max_ms:
        raise ValueError(f'分段{start}..{end}ms超过单段上限{max_ms}ms；拆成两段，或调整场级上限')
    move = segment.get('camera_move')
    if move is not None:
        if not isinstance(move, dict):
            raise ValueError('camera_move须为对象，只放kind与speed')
        extra = set(move) - {'kind', 'speed', 'from_framing', 'to_framing'}
        if extra:
            raise ValueError('camera_move有不认识的字段：' + '、'.join(sorted(extra)) + '；只存kind与speed')
        kind = move.get('kind')
        if kind is not None and kind not in CAMERA_MOVES:
            raise ValueError('运镜种类无效；允许：' + '、'.join(CAMERA_MOVES))
        speed = move.get('speed')
        if speed is not None and speed not in CAMERA_SPEEDS:
            raise ValueError('运镜速度无效；允许：' + '、'.join(CAMERA_SPEEDS))
    notes = segment.get('notes')
    if notes is not None and not isinstance(notes, str):
        raise ValueError('运镜备注须为文本')


def validate_segment_motions(shot, max_ms=DEFAULT_MAX_SEGMENT_MS):
    """校验存储里的运镜标注：只在「段还在、编号对得上」时才有意义。"""
    rows = shot.get('segment_motions')
    if rows is None:
        return
    if not isinstance(rows, list):
        raise ValueError(f'镜头{shot["id"]}的运镜标注须为列表')
    pairs = {tuple(pair) for pair in segments_of(shot)}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {'from_ms', 'to_ms', 'camera_move', 'notes'}:
            raise ValueError('运镜标注只保存from_ms、to_ms、camera_move与notes；派生字段一律现算')
        validate_segment(row, max_ms)
        key = (row['from_ms'], row['to_ms'])
        if key in seen:
            raise ValueError(f'运镜标注{key[0]}..{key[1]}ms重复')
        if key not in pairs:
            raise ValueError(f'运镜标注{key[0]}..{key[1]}ms不在镜头{shot["id"]}的分段上；'
                             '先用关键帧或 set_timeline_segments 确定分段')
        seen.add(key)


# -- 提示词 -------------------------------------------------------------------
def segment_prompt(segment, kind='video', *, duration_ms=None, pictures=2):
    """把一段运动折成四段：运镜／主体／环境／约束。

    为什么分四段：模型对「镜头怎么动」「人怎么动」「环境变没变」「有什么硬约束」的误读方式
    不一样（混写时最容易把运镜读成主体动作），分开写才能逐条核对、逐条改。四段是我们的内部
    结构，进请求前整体放进 ``integrated_multimodal_description``（见官方规范 §7）。

    为什么运镜写英文：H3 没有结构化的运镜参数，运镜写在正文的自然英语动作里，且用官方词表
    （类型＋幅度＋速度）。中文运镜词模型不认识，句尾堆标签也不是官方口径。

    ``duration_ms`` 是这次请求要生成的时长（首尾帧模式必须与对齐句一致）；省略就用本段长度。
    ``pictures`` 为 2 时用官方 FL2VA 对齐句，只有首帧时用 I2VA 那句（逐字，见规范 §2.1）。
    """
    if not isinstance(segment, dict):
        raise ValueError('分段须为对象')
    if kind not in _KINDS:
        raise ValueError('提示词类型须为' + '、'.join(_KINDS))
    if pictures not in (1, 2):
        raise ValueError('首尾帧数量只能是1（只有首帧）或2（首帧＋尾帧）')
    span = duration_ms if type(duration_ms) is int and duration_ms > 0 else _duration(segment)
    sections = ['运镜：' + _camera_sentence(segment, kind),
                '主体：' + _subject_sentence(segment, kind),
                '环境：' + _environment_sentence(segment),
                '约束：' + _constraint_sentence(segment, kind, span)]
    if kind != 'video':
        return '\n'.join(sections)
    # 首尾帧模式：第一行逐字用官方对齐句、空一行、再写单镜头路径（规范 §7 规则 4）。
    return '\n'.join([_alignment_line(span, pictures), '', '[Shot 1] ' + sections[0],
                      sections[1], sections[2], sections[3]])


def _alignment_line(span, pictures):
    """官方首行指令：逐字照抄模板，只替换时长（S.SS 两位小数秒）。"""
    if pictures < 2:
        return H3_I2VA_LINE
    return H3_FL2VA_LINE.format(seconds=_seconds(span))


def _camera_sentence(segment, kind):
    if kind != 'video':
        return '静帧：只表现这一帧的构图与机位，画面停在这一刻。'
    move = segment.get('camera_move') or {}
    camera = segment.get('camera') or {}
    framed = _framing_tail(move.get('from_framing'), move.get('to_framing'))
    if not segment.get('available'):
        return _sentence('The camera keeps one continuous path between the two defined frames', framed)
    term = _h3_term(segment, camera)
    if term is None:
        return _sentence('The camera holds a Static Shot unless the subject itself moves', framed)
    if term == 'Static Shot':
        return _sentence('The camera holds a Static Shot', framed)
    words = [f'The camera {term}']
    amplitude = _h3_amplitude(segment, camera)
    if amplitude:
        words.append(amplitude)
    speed = H3_SPEEDS.get(move.get('speed') or '')
    if speed:
        words.append(speed)
    return _sentence(' '.join(words), framed)


def _h3_term(segment, camera):
    """我们的种类 → 官方表达；能分辨的再细分一次（推 vs 变焦、truck vs tracking）。"""
    kind = (segment.get('camera_move') or {}).get('kind')
    if kind is None:
        return None
    lens = camera.get('lens_delta_mm') or 0.0
    shift = camera.get('shift_m') or 0.0
    if kind in ('push_in', 'pull_out') and shift < SHIFT_TOL_M and abs(lens) >= LENS_TOL_MM:
        # 机位几乎没动、只改了焦距：官方叫 Zoom In/Out，不叫 Push In/Pull Out。
        return 'Zoom In' if lens > 0 else 'Zoom Out'
    if kind == 'track':
        dx = camera.get('dx_m') or 0.0
        # 机位横向平移是 Truck；机位没平移只是跟着主体动，才是 Tracking Shot。
        if abs(dx) >= SHIFT_TOL_M:
            return 'Truck Right' if dx > 0 else 'Truck Left'
        return 'Tracking Shot'
    return H3_CAMERA_TERMS.get(kind)


def _h3_amplitude(segment, camera):
    kind = (segment.get('camera_move') or {}).get('kind')
    if kind in ('pan_left', 'pan_right'):
        return _scale(abs(camera.get('yaw_delta_deg') or 0.0), LARGE_YAW_DEG, SMALL_YAW_DEG)
    if kind in ('tilt_up', 'tilt_down'):
        return _scale(abs(camera.get('pitch_delta_deg') or 0.0), LARGE_PITCH_DEG, SMALL_PITCH_DEG)
    if kind == 'track':
        return _scale(abs(camera.get('shift_m') or 0.0), LARGE_SHIFT_M, SMALL_SHIFT_M)
    ratios = [abs(row.get('area_ratio') or 0.0) for row in segment.get('subject_moves') or []]
    largest = max(ratios) if ratios else 0.0
    if segment.get('framing_change') not in (None, 'same') or largest >= LARGE_AREA_RATIO:
        return 'with large amplitude'
    return 'with small amplitude' if largest <= AREA_THRESHOLD else ''


def _scale(value, large, small):
    if value >= large:
        return 'with large amplitude'
    return 'with small amplitude' if value < small else ''


def _sentence(head, tail):
    return head + (f', {tail}' if tail else '') + '.'


def _subject_sentence(segment, kind):
    moves = segment.get('subject_moves') or []
    if not moves:
        if segment.get('available') is False:
            return '没有框图可比，只保持前后两帧的主体身份、数量与朝向一致。'
        return '本段没有可配对的主体（前后两帧缺同一个资产），只表现环境的变化。'
    parts = []
    for index, row in enumerate(moves):
        name = _ORDINALS[index] if index < len(_ORDINALS) else f'第{index + 1}个主体'
        parts.append(name + _move_words(row, kind))
    return '；'.join(parts) + '。'


def _move_words(row, kind):
    direction = row.get('direction')
    if kind != 'video':
        place = {'left': '偏画面左侧', 'right': '偏画面右侧', 'center': '在画面正中'}.get(direction, '')
        return f'{place}，本帧位置固定'
    motion = row.get('apparent_motion')
    if motion == 'approach':
        return '向镜头靠近（在画面里明显变大）'
    if motion == 'recede':
        return '远离镜头（在画面里明显变小）'
    if motion == 'lateral':
        return '向画面右侧横移' if direction == 'right' else '向画面左侧横移'
    return '位置基本不动，只做自身动作'


def _environment_sentence(segment):
    before = (segment.get('from_description') or '').strip()
    after = (segment.get('to_description') or '').strip()
    notes = (segment.get('notes') or '').strip()
    if before and after and before != after:
        text = f'场所与光线由「{before}」转为「{after}」，保持同一场景与光位'
    elif after and not before:
        text = f'画面转为「{after}」，场所沿用同一个'
    elif before:
        text = f'环境保持「{before}」，场景与光位沿用首帧'
    else:
        text = '环境沿用首帧的场所、光线与天气'
    if notes:
        text += f'；人工备注：{notes}'
    return text + '。'


def _constraint_sentence(segment, kind, span):
    """约束段只写正向保持项与格式硬约束：官方没有负面提示词清单（规范 §3、§7 规则 3）。"""
    if kind != 'video':
        return '单帧静态画面：只表现这一帧的构图、机位与光影。'
    return (f'单镜头；主体身份、数量、服装与场所保持不变；只表现从 Picture 1 到 Picture 2 的'
            f'这一段过渡（{_timestamp(0)} 至 {_timestamp(span)}），结尾落在 Picture 2。')


# -- 小工具 -------------------------------------------------------------------
def _plan(shot):
    timeline = shot.get('timeline')
    return timeline if isinstance(timeline, dict) else {}


def _shot_of(board, shot):
    if isinstance(shot, dict):
        return shot
    row = next((item for item in (board or {}).get('shots') or [] if item.get('id') == shot), None)
    if row is None:
        raise ValueError(f'镜头{shot}不属于本场')
    return row


def _offset(value, label='镜内位置'):
    if type(value) is not int or value < 0:
        raise ValueError(f'{label}必须是大于等于 0 的整数毫秒')
    return value


def _aspect(shot):
    """画幅只认镜头字段（与 storyboard_operations 同一口径）；没有就交回 framing_of 的默认值。"""
    for field in ('aspect_ratio', 'frame_aspect'):
        value = shot.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _description(shot, offset_ms):
    """该时刻关键帧写的「这一帧是什么」（命名＋描述）：环境差量靠它，不靠 bbox。"""
    row = next((item for item in _plan(shot).get('keyframes') or []
                if item.get('offset_ms') == offset_ms), None) or {}
    name, text = (row.get('moment_name') or '').strip(), (row.get('description') or '').strip()
    return '：'.join(part for part in (name, text) if part)


def _camera(layout):
    camera = (layout or {}).get('camera')
    camera = camera if isinstance(camera, dict) else {}
    return {field: float(camera.get(field, default))
            for field, default in CAMERA_DEFAULTS.items() if field != 'framing'}


def _subjects(layout):
    """该帧的主体框：同一资产在一帧里出现多次时取面积最大的那块，它才是这个主体。"""
    result = {}
    for row in (layout or {}).get('objects') or []:
        identifier, box = row.get('asset_id'), row.get('bbox')
        if not isinstance(identifier, str) or not isinstance(box, list) or len(box) != 4:
            continue
        current = result.get(identifier)
        if current is None or _area(box) > _area(current['bbox']):
            result[identifier] = row
    return result


def _framing(layout, aspect):
    """按「最高的主体」判景别：阈值一律走 framing_of，别在这里再写一份。"""
    boxes = [row['bbox'] for row in _subjects(layout).values()]
    return framing_of(max(boxes, key=_height) if boxes else None, aspect)


def _framing_change(before, after):
    if FRAMING_ORDER[after] > FRAMING_ORDER[before]:
        return 'closer'
    return 'farther' if FRAMING_ORDER[after] < FRAMING_ORDER[before] else 'same'


def _framing_tail(before, after):
    """景别的去向：官方写法是在运镜同一句里交代构图收紧或松开到哪一档。"""
    head, tail = H3_FRAMING_TERMS.get(before or ''), H3_FRAMING_TERMS.get(after or '')
    if not head or not tail or head == tail:
        return ''
    closer = FRAMING_ORDER[after] > FRAMING_ORDER[before]
    return ('tightening the framing to ' if closer else 'opening the framing to ') + tail


def _camera_deltas(from_layout, to_layout):
    before, after = _camera(from_layout), _camera(to_layout)
    dx, dz = after['x'] - before['x'], after['z'] - before['z']
    return {'dx_m': round(dx, 3), 'dz_m': round(dz, 3), 'shift_m': round(hypot(dx, dz), 3),
            'lens_delta_mm': round(after['lens_mm'] - before['lens_mm'], 3),
            'yaw_delta_deg': round(after['yaw'] - before['yaw'], 3),
            'pitch_delta_deg': round(after['pitch'] - before['pitch'], 3)}


def _subject_moves(from_layout, to_layout):
    """同一 asset_id 在前后两帧都出现才算一对：只有它才谈得上「从哪动到哪」。"""
    before, after = _subjects(from_layout), _subjects(to_layout)
    rows = []
    for identifier, row in sorted(before.items(), key=lambda item: _area(item[1]['bbox']), reverse=True):
        other = after.get(identifier)
        if other is None:
            continue
        start = [float(value) for value in row['bbox']]
        end = [float(value) for value in other['bbox']]
        direction = _direction(_center_x(start), _center_x(end))
        motion, ratio = _apparent(_area(start), _area(end), direction)
        rows.append({'subject': identifier, 'kind': row.get('kind') or other.get('kind'),
                     'from_bbox': start, 'to_bbox': end, 'direction': direction,
                     'apparent_motion': motion, 'area_ratio': round(ratio, 3),
                     'depth_delta': round(_depth(other) - _depth(row), 3)})
    return rows


def _consistency(camera_move, moves, framing_change, camera):
    """标注的运镜和算出来的位移是否对得上；对不上只报 warning，不改数据也不拦写入。

    为什么要核对：kind 是人手写的，最容易和框图脱节（改完构图忘了改备注）。这里把两条路
    的证据摆在一起——焦距／景别／面积、偏航／俯仰、机位位移——对不上时说清「差在哪」，
    让人自己决定是改运镜还是改框图。
    """
    kind = camera_move.get('kind')
    if kind is None:
        return []
    grew = (camera['lens_delta_mm'] >= LENS_TOL_MM or framing_change == 'closer'
            or any(row['area_ratio'] >= AREA_THRESHOLD for row in moves))
    shrank = (camera['lens_delta_mm'] <= -LENS_TOL_MM or framing_change == 'farther'
              or any(row['area_ratio'] <= -AREA_THRESHOLD for row in moves))
    moved = camera['shift_m'] >= SHIFT_TOL_M
    directions = {row['direction'] for row in moves}
    problem = None
    if kind == 'push_in' and not grew:
        problem = '焦距与主体画面占比都没有变大、景别也没有收紧'
    elif kind == 'pull_out' and not shrank:
        problem = '焦距与主体画面占比都没有变小、景别也没有松开'
    elif kind == 'pan_right' and not (camera['yaw_delta_deg'] >= YAW_TOL_DEG or directions == {'left'}):
        problem = '偏航角没有右转，画面里也没有主体向左滑过'
    elif kind == 'pan_left' and not (camera['yaw_delta_deg'] <= -YAW_TOL_DEG or directions == {'right'}):
        problem = '偏航角没有左转，画面里也没有主体向右滑过'
    elif kind == 'tilt_up' and camera['pitch_delta_deg'] < PITCH_TOL_DEG:
        problem = '俯仰角没有抬高'
    elif kind == 'tilt_down' and camera['pitch_delta_deg'] > -PITCH_TOL_DEG:
        problem = '俯仰角没有压低'
    elif kind == 'track' and not moved:
        problem = f'机位水平位移只有{camera["shift_m"]:g}米，看不出跟移'
    elif kind == 'static' and (moved or abs(camera['lens_delta_mm']) >= LENS_TOL_MM
                               or abs(camera['yaw_delta_deg']) >= YAW_TOL_DEG
                               or abs(camera['pitch_delta_deg']) >= PITCH_TOL_DEG):
        problem = '机位、焦距或朝向在前后两帧之间明显变了'
    if problem is None:
        return []
    return [{'code': 'MOTION_KIND_CONFLICT', 'kind': kind,
             'message': f'标注的运镜是{kind}（{CAMERA_MOVES[kind]}），但前后两帧对不上：' + problem
                        + '。要么改运镜种类，要么补上对应的框图与机位',
             'evidence': dict(camera)}]


def _center_x(box):
    return (box[0] + box[2]) / 2


def _height(box):
    return box[3] - box[1]


def _area(box):
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _depth(row):
    value = row.get('depth')
    return float(value) if type(value) in (int, float) else 0.0


def _duration(segment):
    value = segment.get('duration_ms')
    if type(value) is int and value > 0:
        return value
    start, end = segment.get('from_ms'), segment.get('to_ms')
    return end - start if type(start) is int and type(end) is int and end > start else 0


def _timestamp(ms):
    """毫秒只以镜内切点记法 MM:SS.mmm 出现在提示词里（官方记法，裸毫秒不进正文）。"""
    total = max(0, int(ms))
    minutes, rest = divmod(total, 60000)
    seconds, millis = divmod(rest, 1000)
    return f'{minutes:02d}:{seconds:02d}.{millis:03d}'


def _seconds(ms):
    """官方对齐句里的 S.SS：两位小数秒（8 秒写 8.00）。"""
    return f'{max(0, int(ms)) / 1000:.2f}'


def _direction(before_x, after_x):
    """水平方向：看框中心 x 的变化（阈值 LATERAL_THRESHOLD）。

    没横移时还要区分 ``center`` 与 ``none``：停在画面中央的多半是推拉或定机位的主体，
    停在边上的则说明构图本来就偏——两者对「这一段该怎么写」是不同的信息。
    """
    delta = after_x - before_x
    if delta > LATERAL_THRESHOLD:
        return 'right'
    if delta < -LATERAL_THRESHOLD:
        return 'left'
    return 'center' if CENTER_BAND[0] <= after_x <= CENTER_BAND[1] else 'none'


def _apparent(area_before, area_after, direction):
    """画面里的「表观运动」：面积变了就是靠近／远离，面积没变而横向走了就是横移。"""
    ratio = (area_after - area_before) / area_before if area_before else 0.0
    if ratio > AREA_THRESHOLD:
        return 'approach', ratio
    if ratio < -AREA_THRESHOLD:
        return 'recede', ratio
    return ('lateral' if direction in _DIRECTIONS[:2] else 'none'), ratio
