"""镜头运动（段与段之间的差量）：把前后两帧的框图折成一段能送进视频模型的结构化描述。

为什么单独一层：帧是静态的（框图＋关键帧描述），运动只发生在**段之间**。把运动挂到帧上
说不清「从哪一帧动到哪一帧」；把段里的数值当人工输入存起来，又会跟框图脱节——改一帧构图，
那段数字立刻过期。所以本模块只做两件事：

* ``derive_segment``：从段两端两帧的框图与机位**算出**主体位移、方向、画面占比变化、景别差
  与机位位移；人只给运镜种类、速度与一句备注（见 ``storyboard_operations.set_segment_motion``）。
  派生字段一律现算不落库，所以同一个段在界面、助手与生成任务里读到的是同一份推导。
* ``camera_move_from_scene``：镜头绑了预演机位时，运镜**直接按预演的机位轨迹量**（焦距、
  三轴位移、朝向），``source="previs"``。为什么它优先：框图的机位、焦距与朝向本来就是从预演
  投影来的，再从两帧框图反推运镜等于把真值投影两次再猜回来——同一个推镜可能读成「主体变大」
  也可能读成「焦距变长」。没有预演才回落到框图差量（``source="layout"``）；人工标注显式给了
  ``kind`` 时覆盖两者（``source="manual"``），但量出来的分量照样返回，供人核对。
* ``segment_prompt``：把这些字段折成「运镜／主体／环境／约束」四段。H3 没有结构化的运镜
  参数，运镜要写进提示词正文的自然英语动作里，且用官方词表（类型＋幅度＋速度，见
  ``docs/MINIMAX_H3_PROMPT.md``）：``endpoints``（首尾帧）先放官方 FL2VA 对齐句，
  再写单镜头路径。

阈值集中在下面几个常量里；景别一律问 ``previs_scene.framing_of``——那是全工程唯一的景别
口径（1.75 m 的人在取景框里占多高），在这里另写一套 0.45/0.90 只会让两边慢慢走散。
"""
from __future__ import annotations

from math import hypot, sqrt

from .previs_scene import camera_at, framing_of
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
    # 预演推导用的是下面这几个更细的种类（官方词表 §3 本来就把 Zoom/Truck/Pedestal 单列）。
    # 上面那几个留给人工标注，两套并存：判定时按「同一族」比较（见 _MOTION_FAMILIES），
    # 所以人手写 push_in、机器量出 dolly_in 不算冲突。
    'zoom_in': '变焦推近：机身不动，焦距变长，主体在画面里变大',
    'zoom_out': '变焦拉远：机身不动，焦距变短，主体在画面里变小',
    'dolly_in': '机身推进：机位沿视线向主体靠近（push_in 的预演口径）',
    'dolly_out': '机身后拉：机位沿视线远离主体（pull_out 的预演口径）',
    'truck_left': '左移：机位沿自身右轴的反方向水平横移',
    'truck_right': '右移：机位沿自身右轴水平横移',
    'pedestal_up': '升：整机沿世界竖直轴上升',
    'pedestal_down': '降：整机沿世界竖直轴下降',
}
# 运镜的来源：预演轨迹 / 框图差量 / 人工标注。三种都放进 camera_move.source，
# 读的人一眼看得出这个 kind 是真值、证据还是人写的。
PREVIS_SOURCE = 'previs'
LAYOUT_SOURCE = 'layout'
MANUAL_SOURCE = 'manual'
CAMERA_MOVE_SOURCES = (PREVIS_SOURCE, LAYOUT_SOURCE, MANUAL_SOURCE)
CAMERA_SPEEDS = {'slow': '缓慢', 'medium': '匀速', 'fast': '快速'}
# 画面归一化坐标下的判定阈值（框图 bbox 就是 0..1 的画面坐标，见 storyboard_layout）。
LATERAL_THRESHOLD = 0.05   # 框中心 x 变化超过 5% 才算「横移」，低于它就只是取景抖动
CENTER_BAND = (0.45, 0.55)  # 没横移又停在这条带里＝「居中不动」，否则是「不动但不在中心」
AREA_THRESHOLD = 0.15      # 框面积相对变化 15% 才说「靠近／远离」，再小读成「横向」更诚实
LENS_TOL_MM = 1.0          # 等效焦距差 1mm 起算变焦；更小的差多半是补默认值时的抖动
# 朝向：偏航 2°、俯仰 1.5° 起算摇镜。理由——35mm 基准镜头的水平半视角是 27.2°，
# 2° 偏航约等于画面内容横移 3.4% 宽（tan2°/tan27.2°），与框图口径的 LATERAL_THRESHOLD 5%
# 同一量级；俯仰阈值取 1.5°，因为在 3m 工作距离上它对应画面上约 8cm 的纵向移动，再小就与
# 轨迹取样抖动分不开。预演那条路也用这两个值，两条路的「摇了没有」因此一致。
YAW_TOL_DEG = 2.0          # 偏航差 2° 起算摇镜
PITCH_TOL_DEG = 1.5        # 俯仰差 1.5° 起算上下摇
SHIFT_TOL_M = 0.10         # 机位水平位移 0.1m 起算「动了」：再小就是同一机位的抖动
# ---- 预演机位轨迹（``camera_move_from_scene``）的阈值 ----
# 为什么另起一组常量：上面那几条量的是**二维框图差量**（归一化画面坐标、从框图反解出的
# 机位），下面这几条量的是**三维预演真值**（米、毫米、度）。量纲与数据来源都不同，混用会
# 让同一个机位动作在两条路上判得不一样；但**量级刻意对齐**（见每条的说明），所以同一次改动
# 在两条路上给出的「动了没有」不该打架。
PREVIS_LENS_TOL_MM = 1.5   # 变焦：1.5mm 以下不认。35mm 基准镜头上 1.5mm 是 4.3% 的视角变化，
#                            与框图口径的 LENS_TOL_MM=1.0 同一量级；比它小的差多半来自轨迹
#                            插值取整与关键帧吸附，读成「变了焦」只是噪音。
ZOOM_SMALL_RATIO = 0.15    # 变焦幅度按**相对量**（Δf / 起始焦距）分级：同样是 5mm，在 18mm
#                            镜头上是大幅、在 85mm 上几乎看不出。0.15 与 AREA_THRESHOLD 同源
#                            （画面占比 15% 才看得出变化），0.50 与 LARGE_AREA_RATIO 同源
#                            （占比过半即「大」）——变焦与「主体在画面里变大」本是同一件事的两种量法。
ZOOM_LARGE_RATIO = 0.50
# 机位位移：三轴都按 0.10 m。理由——一张手掌宽（≈0.1m）在 3m 工作距离上占画面约 3.3%，
# 与变焦阈值那 4.3% 同一量级；同时与框图差量法的 SHIFT_TOL_M 对齐，免得同一个机位动作在
# 预演那条路上「动了」、在框图那条路上「没动」。三条分开写是为了以后能单轴调。
DOLLY_TOL_M = 0.10         # 沿视线（推拉）
TRUCK_TOL_M = 0.10         # 横向（机位自身右轴）
PEDESTAL_TOL_M = 0.10      # 竖直（世界 y 轴）
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
    'zoom_in': 'Zoom In',
    'zoom_out': 'Zoom Out',
    'dolly_in': 'Push In',
    'dolly_out': 'Pull Out',
    'truck_left': 'Truck Left',
    'truck_right': 'Truck Right',
    'pedestal_up': 'Pedestal Up',
    'pedestal_down': 'Pedestal Down',
}
# 句内写法（官方 §3 的三条示例都是「The camera <动词短语> with small amplitude at slow speed」）：
# 主分量用第三人称动词短语，次要分量用动名词从句跟在 while 后面。为什么不用大写的术语名：
# 术语名是词表条目（Truck Right），摆在句子里就成了标签堆叠，官方明确要求写成镜内动作。
H3_MOTION_PHRASES = {
    'push_in': 'pushes in', 'pull_out': 'pulls out', 'dolly_in': 'pushes in', 'dolly_out': 'pulls out',
    'zoom_in': 'zooms in', 'zoom_out': 'zooms out',
    'pan_left': 'pans left', 'pan_right': 'pans right', 'tilt_up': 'tilts up', 'tilt_down': 'tilts down',
    'truck_left': 'trucks left', 'truck_right': 'trucks right',
    'pedestal_up': 'pedestals up', 'pedestal_down': 'pedestals down',
    'track': 'tracks the subject', 'static': 'holds a Static Shot',
}
H3_MOTION_GERUNDS = {
    'push_in': 'pushing in', 'pull_out': 'pulling out', 'dolly_in': 'pushing in', 'dolly_out': 'pulling out',
    'zoom_in': 'zooming in', 'zoom_out': 'zooming out',
    'pan_left': 'panning left', 'pan_right': 'panning right',
    'tilt_up': 'tilting up', 'tilt_down': 'tilting down',
    'truck_left': 'trucking left', 'truck_right': 'trucking right',
    'pedestal_up': 'pedestaling up', 'pedestal_down': 'pedestaling down',
    'track': 'tracking the subject',
}
# 次要分量的幅度副词：官方只有 small/medium/large 三档，中等默认不写。
H3_AMPLITUDE_ADVERBS = {'small': 'slightly', 'large': 'strongly'}
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
# camera_move 的字段：落库的人工标注只有前四个；带 source 的推导结果还能多带下面的派生字段
# （不落库，见 validate_segment）。
_MOVE_FIELDS = frozenset({'kind', 'speed', 'from_framing', 'to_framing'})
_DERIVED_MOVE_FIELDS = frozenset({'source', 'components', 'primary', 'cuts', 'deltas', 'note'})
# 同一族运镜：人工写 push_in、预演量出 dolly_in 不算冲突（官方把它们当同一类「机身推进」），
# 但 push 与 zoom 是两件事（机身动 vs 机身不动），zoom 归自己一族。
_MOTION_FAMILIES = {
    'push_in': 'dolly_in', 'pull_out': 'dolly_out', 'dolly_in': 'dolly_in', 'dolly_out': 'dolly_out',
    'zoom_in': 'zoom_in', 'zoom_out': 'zoom_out',
    'pan_left': 'pan_left', 'pan_right': 'pan_right',
    'tilt_up': 'tilt_up', 'tilt_down': 'tilt_down',
    'track': 'lateral', 'truck_left': 'lateral', 'truck_right': 'lateral',
    'pedestal_up': 'pedestal_up', 'pedestal_down': 'pedestal_down',
    'static': 'static',
}
# 量的单位 → 中文单位（只用在给人看的警告里；提示词里一个数字都不出现）。
_UNIT_WORDS = {'mm': '毫米', 'm': '米', 'deg': '度'}


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


# -- 预演推导 -----------------------------------------------------------------
def camera_move_from_scene(shot, segment, scene):
    """按预演的机位轨迹量这一段「镜头怎么动的」（只算不写）。

    为什么以预演为准：框图的机位、焦距与朝向本来就是从预演投影来的；再从两帧框图反推运镜
    （``derive_segment`` 的差量法）等于把真值投影两次再猜回来——同一个推镜可能被读成「主体
    变大」也可能被读成「焦距变长」。这里直接量预演里的机位轨迹，判定只做一次换算。

    判定（阈值见上面那组 PREVIS_* 常量与 YAW/PITCH_TOL_DEG）：

    * 焦距变化 ≥ ``PREVIS_LENS_TOL_MM`` → ``zoom_in``/``zoom_out``；
    * 机位位移分解成三轴 → 沿视线 ``dolly_in``/``dolly_out``、横向 ``truck_left``/``truck_right``、
      竖直 ``pedestal_up``/``pedestal_down``（三轴阈值各写在上面的常量里，单位米）；
    * 朝向变化（注视点或 ``targetPath`` 变了，取 ``camera_at`` 求出的 yaw/pitch）→
      ``pan_left``/``pan_right``、``tilt_up``/``tilt_down``（阈值以度计）；
    * 什么都没动 → ``static``。

    返回值与 ``derive_segment`` 的 ``camera_move`` 同形（kind/speed/from_framing/to_framing），多出：

    * ``source``：``"previs"``——读的人据此知道 kind 是真值，不是从框图猜的；
    * ``components``：**全部分量**，每项 ``{kind, amplitude, value, unit}``，按幅度从大到小排
      （同一档幅度再按「超出阈值几倍」排）。组合运镜必须留着次要分量：只报主运镜，提示词就
      只剩一半动作；
    * ``primary``：``components[0]``（幅度最大的一项），没有分量时为 None；``kind`` 就是它；
    * ``deltas``：焦距/三轴位移/角度的原始量（带符号），报警与核对时引用；
    * ``cuts``：段内切过机位时列出切点，此时 ``kind`` 为 ``"cut"``。

    ``value`` 带符号，正负与 ``kind`` 一致（正＝朝那个 kind 的方向）；``unit`` 是 ``mm``/``m``/``deg``。
    ``from_framing``/``to_framing``/``speed`` 一律留空：景别口径是全工程唯一的 ``framing_of``
    （要 bbox），速度是人的判断（官方「慢/快」没有可量的分界），两者都不该由机位轨迹冒充。
    """
    if not isinstance(shot, dict):
        raise ValueError('按预演推导运镜需要镜头对象（那一镜的 shot，带 previs 绑定）')
    previs = shot.get('previs') if isinstance(shot.get('previs'), dict) else None
    if not previs or not previs.get('camera_id'):
        raise ValueError('该镜没有绑定预演机位；先用 set_shot_previs 绑定，或让推导回落框图差量')
    if not isinstance(scene, dict) or not scene.get('cameras'):
        raise ValueError('预演场景快照里没有摄影机；先用 read_previs 核对本场预演工程')
    if not isinstance(segment, dict):
        raise ValueError('分段须为对象（至少给 from_ms 与 to_ms）')
    start = _offset(segment.get('from_ms'), '分段起点')
    end = _offset(segment.get('to_ms'), '分段终点')
    if start >= end:
        raise ValueError(f'分段{start}..{end}ms的起点须小于终点')
    offset = previs.get('time_offset_ms', 0)
    if type(offset) is not int or offset < 0:
        raise ValueError('预演时刻偏移 time_offset_ms 须为大于等于 0 的整数毫秒')
    camera_id = previs['camera_id']
    # 镜内时刻 → 预演时刻（秒）：shot.previs.time_offset_ms 是「镜头 0ms 对应的预演时刻」。
    from_s, to_s = (offset + start) / 1000, (offset + end) / 1000
    move = {'kind': 'static', 'speed': None, 'from_framing': None, 'to_framing': None,
            'source': PREVIS_SOURCE, 'components': [], 'primary': None, 'deltas': {}}
    cuts = _cuts_in(scene, from_s, to_s, offset)
    if cuts:
        # 段内切过机位：这一段的真值本身就跨了两个镜头，不假装成某一种运镜——把切点原样
        # 交出去，让人把段拆开（见 derive_segment 的 SEGMENT_CROSSES_CUT 警告）。
        move.update({'kind': 'cut', 'cuts': cuts,
                     'note': '段内切过机位：首尾帧视频只有一个连续镜头，把这一段拆成两段分别生成'})
        return move
    before = camera_at(scene, from_s, camera_id)
    after = camera_at(scene, to_s, camera_id)
    components, deltas = _pose_components(before, after)
    move['deltas'] = deltas
    move['components'] = components
    move['primary'] = dict(components[0]) if components else None
    if components:
        move['kind'] = components[0]['kind']
    return move


def _cuts_in(scene, from_s, to_s, offset_ms):
    """窗口里真正换了机位的 cut（镜内毫秒 + 预演毫秒都给出）。

    只算「换了机位」的 cut：``cuts[0]`` 是开场用哪台，不算切换。窗口取左开右闭
    ``(from_s, to_s]``——正好落在段起点上的 cut 属于这一段的第一帧，不是段内的切换。
    """
    cuts = sorted((row for row in scene.get('cuts') or [] if type(row.get('time')) in (int, float)),
                  key=lambda row: row['time'])
    if len(cuts) < 2:
        return []
    rows = []
    active = cuts[0].get('camera_id')
    for cut in cuts[1:]:
        time_s = float(cut['time'])
        if cut.get('camera_id') != active and from_s < time_s <= to_s:
            previs_ms = int(round(time_s * 1000))
            rows.append({'time_ms': previs_ms - offset_ms, 'previs_ms': previs_ms,
                         'camera_id': cut.get('camera_id')})
        active = cut.get('camera_id')
    return rows


def _pose_components(before, after):
    """两帧机位 → ``(分量清单, 原始量)``；三分量分解与幅度分级都在这里。

    三轴怎么分：沿视线（推拉）、沿机位自身右轴（横移）、沿世界竖直轴（升降）。为什么升降
    用世界 y 而不是机位自身的 up：官方的 Pedestal 就是整机上下，不随俯仰倾斜跟着转。
    为什么先扣掉推拉在 y 上的投影：视线有俯仰角时，「推近」本身就带一点下降，不扣掉就会
    把一个推镜读成「推近＋下降」两个动作。
    """
    step = [float(after['position'][index]) - float(before['position'][index]) for index in range(3)]
    forward = _unit([float(before['target'][index]) - float(before['position'][index])
                     for index in range(3)])
    if forward is None:
        raise ValueError('机位与注视点重合，量不出这一段的推拉方向；请在预演里把机位挪开或改注视点')
    # right 与 previs_scene._basis 同一右手系（forward × 世界 up）：镜头朝 -z 时 right=+x，
    # 与「机位向右平移，画面里的景物向左滑」是同一边。正俯视/仰视时 up 与视线平行，改拿 +z 当参考。
    right = _unit(_cross(forward, (0.0, 1.0, 0.0))) or _unit(_cross(forward, (0.0, 0.0, 1.0)))
    if right is None:
        right = (1.0, 0.0, 0.0)
    # 先按 3 位小数取整再判阈值（与 _camera_deltas 同一精度）：否则 0.1 米的位移会被浮点
    # 算成 0.099999 米，明明卡在阈值上却报「没动」。
    dolly = round(_dot(step, forward), 3)
    truck = round(_dot(step, right), 3)
    pedestal = round(step[1] - dolly * forward[1], 3)
    lens = round(float(after['focal']) - float(before['focal']), 3)
    yaw = round(_wrapped(float(after['yaw']) - float(before['yaw'])), 3)
    pitch = round(float(after['pitch']) - float(before['pitch']), 3)
    deltas = {'focal_mm': lens, 'dolly_m': dolly, 'truck_m': truck, 'pedestal_m': pedestal,
              'yaw_deg': yaw, 'pitch_deg': pitch}
    rows = []
    if abs(lens) >= PREVIS_LENS_TOL_MM:
        focal = float(before['focal'])
        ratio = abs(lens) / focal if focal else 0.0
        rows.append(({'kind': 'zoom_in' if lens > 0 else 'zoom_out',
                      'amplitude': _grade(ratio, ZOOM_SMALL_RATIO, ZOOM_LARGE_RATIO),
                      'value': lens, 'unit': 'mm'},
                     abs(lens) / PREVIS_LENS_TOL_MM))
    for value, tol, forward_kind, backward_kind in (
            (dolly, DOLLY_TOL_M, 'dolly_in', 'dolly_out'),
            (truck, TRUCK_TOL_M, 'truck_right', 'truck_left'),
            (pedestal, PEDESTAL_TOL_M, 'pedestal_up', 'pedestal_down')):
        if abs(value) >= tol:
            rows.append(({'kind': forward_kind if value > 0 else backward_kind,
                          'amplitude': _grade(abs(value), SMALL_SHIFT_M, LARGE_SHIFT_M),
                          'value': value, 'unit': 'm'},
                         abs(value) / tol))
    if abs(yaw) >= YAW_TOL_DEG:
        rows.append(({'kind': 'pan_left' if yaw > 0 else 'pan_right',
                      'amplitude': _grade(abs(yaw), SMALL_YAW_DEG, LARGE_YAW_DEG),
                      'value': yaw, 'unit': 'deg'},
                     abs(yaw) / YAW_TOL_DEG))
    if abs(pitch) >= PITCH_TOL_DEG:
        rows.append(({'kind': 'tilt_up' if pitch > 0 else 'tilt_down',
                      'amplitude': _grade(abs(pitch), SMALL_PITCH_DEG, LARGE_PITCH_DEG),
                      'value': pitch, 'unit': 'deg'},
                     abs(pitch) / PITCH_TOL_DEG))
    # 排「主分量」：先比幅度档（官方的幅度就是构图变化范围），同档再比超出阈值几倍——不同单位
    # 的量不能直接比大小，除掉各自的阈值就把单位约掉了。
    rank = {'small': 0, 'medium': 1, 'large': 2}
    rows.sort(key=lambda row: (rank[row[0]['amplitude']], row[1]), reverse=True)
    return [row[0] for row in rows], deltas


def _grade(value, small, large):
    """幅度分级：低于 small 算小，达到 large 算大，其余中等（官方「中等」默认不写进正文）。"""
    if value < small:
        return 'small'
    return 'large' if value >= large else 'medium'


def _wrapped(degrees):
    """角度差折到 (-180, 180]：摇过 180° 时 yaw 会从 +179 跳到 -179，差值不能当成 -358°。"""
    return (degrees + 180.0) % 360.0 - 180.0


def _unit(vector):
    length = sqrt(sum(value * value for value in vector))
    return None if length < 1e-9 else tuple(value / length for value in vector)


def _cross(left, right):
    return (left[1] * right[2] - left[2] * right[1],
            left[2] * right[0] - left[0] * right[2],
            left[0] * right[1] - left[1] * right[0])


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right, strict=True))


# -- 推导 ---------------------------------------------------------------------
def derive_segment(board, shot, from_ms, to_ms, *, scene=None):
    """从段两端两帧的框图算出这一段「发生了什么」。

    运镜有三种来源，优先级从高到低（见 ``camera_move.source``）：

    1. ``manual``：人工在 ``segment_motions`` 里显式给了 ``kind``，它覆盖推导——人是意图，
       机器是事实。被覆盖的推导分量仍在 ``components`` 里，供人核对「我写的和量出来的差在哪」；
    2. ``previs``：镜头绑了预演机位（``shot.previs``）且拿得到场景快照时，运镜直接按预演轨迹量
       （``camera_move_from_scene``）；
    3. ``layout``：没有预演才退回框图差量法（焦距差／面积变化／景别变化）。这一路只给证据、
       不给 ``kind``——两帧框图读不出「这是推镜还是变焦」。

    ``scene`` 省略时，镜头绑了预演就试着向预演页要一份只读快照（见 ``_page_scene``）；
    取不到就回落框图差量，并在 ``previs_problem`` 里说明为什么。

    输出形状（``available`` 为真时）::

        {from_ms, to_ms, duration_ms,
         camera_move: {kind, speed, from_framing, to_framing, source,
                       components: [{kind, amplitude, value, unit}], primary},
         subject_moves: [{subject, kind, from_bbox, to_bbox, direction, apparent_motion,
                          area_ratio, depth_delta}],
         camera: {dx_m, dz_m, shift_m, lens_delta_mm, yaw_delta_deg, pitch_delta_deg},
         framing_change: closer|farther|same,
         from_description, to_description, notes, previs_problem, warnings: [{code, message, ...}]}

    ``board`` 除了在 ``shot`` 传镜头编号时用来查那条镜头以外不参与推导：推导只认这一镜的
    框图、关键帧与预演绑定，免得同一段在两个入口算出两个结果。
    """
    shot = _shot_of(board, shot)
    start, end = _offset(from_ms, '分段起点'), _offset(to_ms, '分段终点')
    if start >= end:
        raise ValueError(f'分段{start}..{end}ms的起点须小于终点')
    from_layout, to_layout = layout_at(shot, start), layout_at(shot, end)
    stored = stored_motion(shot, start, end) or {}
    given = stored.get('camera_move') if isinstance(stored.get('camera_move'), dict) else {}
    previs = _previs_move(board, shot, start, end, scene)
    move = previs['move']
    camera_move = {'kind': (move or {}).get('kind'), 'speed': given.get('speed'),
                   'from_framing': None, 'to_framing': None,
                   'source': PREVIS_SOURCE if move else LAYOUT_SOURCE,
                   'components': [dict(row) for row in (move or {}).get('components') or []],
                   'primary': dict(move['primary']) if (move or {}).get('primary') else None}
    if move and move.get('cuts'):
        camera_move['cuts'] = [dict(row) for row in move['cuts']]
    if move and move.get('deltas'):
        camera_move['deltas'] = dict(move['deltas'])
    if given.get('kind'):
        # 人工标注优先：kind 换成人的，但推导出的分量照样带上（不丢次要分量），
        # 也照样去比对、照样报冲突——覆盖的是结论，不是证据。
        camera_move['kind'] = given['kind']
        camera_move['source'] = MANUAL_SOURCE
    common = {'from_ms': start, 'to_ms': end, 'duration_ms': end - start, 'camera_move': camera_move,
              'notes': stored.get('notes') or '',
              'from_description': _description(shot, start), 'to_description': _description(shot, end),
              'previs_problem': previs['problem']}
    missing = [label for label, row in (('起点帧', from_layout), ('终点帧', to_layout)) if row is None]
    if missing:
        return {'available': False, **common,
                'reason': '、'.join(missing) + '没有框图（layout），推导不出运动；先为两端的帧写入框图'}
    aspect = _aspect(shot)
    # 景别按「这一帧里最高的主体」判：谁占得多，观众看到的就是谁的景别。预演那条路也要在这里
    # 才拿到景别——机位轨迹不含 bbox，景别口径只认 framing_of 这一份。
    camera_move['from_framing'] = _framing(from_layout, aspect)
    camera_move['to_framing'] = _framing(to_layout, aspect)
    camera = _camera_deltas(from_layout, to_layout)
    moves = _subject_moves(from_layout, to_layout)
    change = _framing_change(camera_move['from_framing'], camera_move['to_framing'])
    warnings = _motion_warnings(shot, camera_move, previs, moves, change, camera)
    if not moves and (_subjects(from_layout) or _subjects(to_layout)):
        # 前后两帧都有主体却配不上对：多半是 asset_id 换了（换了卡或手写框图），
        # 这时方向与占比变化算不出来，要说清楚而不是静默给一份空清单。
        warnings.append({'code': 'NO_SUBJECT_PAIR',
                         'message': '前后两帧没有同一个asset_id，主体配不上对，方向与画面占比变化算不出来',
                         'shot_id': shot.get('id')})
    return {'available': True, **common, 'subject_moves': moves, 'camera': camera,
            'framing_change': change, 'warnings': warnings}


def _previs_move(board, shot, start, end, scene):
    """试着按预演量这一段的运镜：返回 ``{'move': …|None, 'problem': 为什么没有}``。

    为什么默认自己去要快照：``read_segment_motion`` 这类只读工具没有工程数据根，拿不到
    ``previs/<场次>/scene.state.json``；预演页开着时 ``getScene`` 是唯一现成的来源。取不到
    不是错误——回落框图差量，并把原因写进 ``previs_problem``，让人知道这次为什么没走预演。
    """
    previs = shot.get('previs')
    if not isinstance(previs, dict) or not previs.get('camera_id'):
        return {'move': None, 'problem': ''}
    snapshot = scene if scene is not None else _page_scene(board)
    if snapshot is None:
        return {'move': None,
                'problem': '本镜绑定了预演机位，但既没有传入场景快照、也连不上预演页；'
                           '本次运镜按框图差量推导（打开预演页或在测试里注入快照可改走预演）'}
    try:
        return {'move': camera_move_from_scene(shot, {'from_ms': start, 'to_ms': end}, snapshot),
                'problem': ''}
    except ValueError as error:
        return {'move': None, 'problem': '按预演推导运镜失败：' + str(error)}


def _page_scene(board):
    """向预演页要一份只读场景快照；没装宿主／不可达就返回 None（调用方回落框图差量）。

    为什么用 ``getScene`` 而不是 ``snapshot``：推导是只读路径，``snapshot`` 会顺手写
    ``previs/<场次>/scene.state.json`` 并涨 revision——读一次运动不该改工程数据。
    """
    try:
        from . import previs_host
    except ImportError:      # 裁剪构建里没有预演这一层：回落框图差量
        return None
    host = previs_host.installed()
    if host is None:
        return None
    try:
        return previs_host.scene_from_page(str((board or {}).get('unit_id') or ''), host.get_scene())
    except previs_host.PrevisHostError:
        return None


def validate_segment(segment, max_ms=DEFAULT_MAX_SEGMENT_MS):
    """校验一段运动（人工标注行或推导结果）：只查数据是否成立，不判创作意图。

    落库的人工标注只存 ``kind`` 与 ``speed``（派生字段一律现算）；推导结果多带 ``source``/
    ``components``/``primary`` 等字段，还可能带 ``cut`` 这种「预演里这一段跨了两个镜头」的
    种类——那不是人写的标注，所以带 ``source`` 时不做字段白名单与种类检查，只校验时间轴。
    """
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
        derived = move.get('source') in CAMERA_MOVE_SOURCES
        extra = set(move) - _MOVE_FIELDS - (_DERIVED_MOVE_FIELDS if derived else set())
        if extra:
            raise ValueError('camera_move有不认识的字段：' + '、'.join(sorted(extra))
                             + '；只存kind与speed（推导出的分量由程序现算，不落库）')
        if not derived:
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
    if move.get('kind') == 'cut':
        # 段内切过机位：首尾帧视频本来就只能是一个连续镜头，正文就照这个写——「这一段有两个
        # 镜头」写进正文模型也做不到，矛盾留在 SEGMENT_CROSSES_CUT 警告里让人拆段。
        return _sentence('The camera holds one continuous take between the two defined frames', framed)
    components = [row for row in move.get('components') or [] if row.get('kind')]
    if components:
        return _sentence(_components_sentence(components, move), framed)
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


def _components_sentence(components, move):
    """有分量清单时的运镜句：主分量「动词短语＋幅度＋速度」，次要分量跟在 ``while`` 后面。

    为什么次要分量也要写：组合运镜（右移＋变焦）只写主运镜，模型会把次要那个丢掉，出来的
    镜头就和机位轨迹差一截。为什么不写数值：官方只有 small/medium/large 三档幅度，句子里
    出现「0.6 米」「5 毫米」这类裸数字，模型只会去追字面而不是看画面。
    """
    head, rest = components[0], components[1:]
    words = ['The camera ' + H3_MOTION_PHRASES.get(head['kind'], head['kind'])]
    amplitude = _amplitude_words(head.get('amplitude'))
    if amplitude:
        words.append(amplitude)
    speed = H3_SPEEDS.get(move.get('speed') or '')
    if speed:
        words.append(speed)
    text = ' '.join(words)
    clauses = [_motion_clause(row) for row in rest]
    if clauses:
        # 多个次要分量也只用一个 while：官方的示例正是「主句 + while 从句」。
        text += ' while ' + _joined(clauses)
    return text


def _motion_clause(component):
    """次要分量：动名词短语 + 幅度副词（小→slightly、大→strongly、中等不写）。"""
    phrase = H3_MOTION_GERUNDS.get(component['kind'], component['kind'])
    adverb = H3_AMPLITUDE_ADVERBS.get(component.get('amplitude') or '', '')
    return f'{phrase} {adverb}' if adverb else phrase


def _amplitude_words(amplitude):
    """幅度修饰词：官方默认中等幅度、不写；小／大才写（与 _h3_amplitude 同一口径）。"""
    return {'small': 'with small amplitude', 'large': 'with large amplitude'}.get(amplitude or '', '')


def _joined(parts):
    if len(parts) < 2:
        return parts[0]
    return ', '.join(parts[:-1]) + ' and ' + parts[-1]


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


def _motion_warnings(shot, camera_move, previs, moves, framing_change, camera):
    """这一段该报哪些运动警告：切镜、预演取不到、人工标注与证据不一致。

    为什么 ``source="previs"`` 不比对：``kind`` 就是机位轨迹量出来的那一个，再拿它和同一份
    轨迹比一次，只会得到「推导和推导不一致」的噪音。只有人工标注才需要比对——那是唯一
    可能和事实脱节的一份数据。
    """
    kind = camera_move.get('kind')
    warnings = []
    if camera_move.get('cuts'):
        cuts = camera_move['cuts']
        warnings.append({'code': 'SEGMENT_CROSSES_CUT',
                         'message': '这一段里预演切过机位（'
                                    + '、'.join(f'镜内{row["time_ms"]}ms→{row["camera_id"]}'
                                                for row in cuts)
                                    + '）：首尾帧视频只能是一个连续镜头，把这一段拆成两段分别生成，'
                                      '或把切镜挪到段边界上',
                         'shot_id': shot.get('id'), 'cuts': cuts})
    if previs['problem']:
        warnings.append({'code': 'PREVIS_UNAVAILABLE', 'message': previs['problem'],
                         'shot_id': shot.get('id')})
    if camera_move.get('source') != MANUAL_SOURCE or kind is None:
        return warnings
    if previs['move'] is not None:
        return warnings + _previs_conflict(shot, kind, previs['move'])
    return warnings + _consistency(camera_move, moves, framing_change, camera)


def _previs_conflict(shot, kind, move):
    """人工标的运镜与预演量出来的对不对得上（只报 warning，不改数据、不拦写入）。

    比对按「族」而不是字面：人手写 push_in、机器量出 dolly_in 是同一件事的两种叫法；
    但 push 与 zoom 是两件事（机身动 / 机身不动），zoom 单独一族。人工标的那一种只要在
    预演的分量清单里出现过就不算冲突——组合运镜里次分量的种类也算数。
    """
    families = {_MOTION_FAMILIES.get(row.get('kind'), row.get('kind'))
                for row in move.get('components') or []}
    families.add(_MOTION_FAMILIES.get(move.get('kind'), move.get('kind')))
    if _MOTION_FAMILIES.get(kind, kind) in families:
        return []
    measured = '、'.join(f'{row["kind"]} {row["value"]:g}{_UNIT_WORDS[row["unit"]]}'
                        for row in move.get('components') or []) or str(move.get('kind'))
    return [{'code': 'MOTION_KIND_CONFLICT', 'kind': kind,
             'message': f'标注的运镜是{kind}（{CAMERA_MOVES.get(kind, "")}），但预演机位轨迹量出来是'
                        + measured + f'（主运镜{move.get("kind")}）。要么改标注，要么改机位轨迹',
             'evidence': {'source': PREVIS_SOURCE, 'components': move.get('components') or [],
                          'deltas': move.get('deltas') or {}, 'shot_id': shot.get('id')}}]


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
    if kind in ('push_in', 'dolly_in', 'zoom_in') and not grew:
        problem = '焦距与主体画面占比都没有变大、景别也没有收紧'
    elif kind in ('pull_out', 'dolly_out', 'zoom_out') and not shrank:
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
    elif kind in ('truck_left', 'truck_right') and not moved:
        problem = f'机位水平位移只有{camera["shift_m"]:g}米，看不出横移'
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
