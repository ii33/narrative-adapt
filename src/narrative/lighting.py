"""把预演里的灯光读成一句中文，接进生图提示词。

为什么单独一层：同一场戏的多镜头必须共享同一份光影（同一台主光、同一个色温、影子
朝同一边），否则逐帧出图拼起来像是几场戏。预演（DirectorDesk 的 ``.director``）里
已经有这份事实：灯是 ``kind='prop'``、``asset='light-*'`` 的实体，参数在实体自己的
``light`` 里；而整场的环境亮度/曝光/背景是**另一份**数据（场景的 ``lighting``，见
``.runtime/vendor/director-desk/src/lighting/model.ts``）。两者都不该由提示词作者凭
印象写，所以这里把三维事实翻成一句可以贴进提示词的话。

纯函数、不依赖 Qt；也不读文件——场景从 ``previs_scene.load_scene``（读 ``.director``）
或 ``previs_host.snapshot``（读预演页）来，形状差异由 ``lights_from_scene`` 抹平。

三条口径（刻意如此，改动前先读一遍）：

* **不编光。** 没灯、灯全被排除、或者读不到预演，一律返回空字符串；绝不补一个
  「默认三点布光」。提示词里多一句没根据的光，比少一句更糟。
* **读不到的参数就不说。** 预演页的 ``getScene`` 不带灯的强度/色温/聚光角（只带
  asset 与位置，见 ``.runtime/vendor/director-desk/src/host/host-scene.ts`` 的
  ``entitySnapshot``），那时描述里只有方向与影子，不猜「硬光」「高光比」。
* **强度先归一化再比。** three.js 的点/聚光与平行光不是一个单位：DirectorDesk 的
  缺省强度是点/聚光 100、太阳/面光 4（``lighting/model.ts::defaultLight``）。直接比
  数字，太阳永远输给一盏 100 瓦的点光，所以先按各自的参考值归一（见
  ``INTENSITY_REFERENCE``），再判主光与光比。
"""

from __future__ import annotations

import math

LIGHT_PREFIX = 'light-'
KINDS = ('key', 'fill', 'rim', 'ambient')
DETAILS = ('sketch', 'style')
# 两档描述的硬上限（字）：素描要说清光的方向就够，风格帧才带色温与光比。
LIMITS = {'sketch': 40, 'style': 90}

# -- 判定阈值 ---------------------------------------------------------------
# 方位：把「灯相对主体的水平方向」与「机位视线反方向」的夹角切成五段。
# 30° 以内算正面（灯几乎在机位这一侧，照的是脸的正面）；75° 起才叫侧面；105° 之后
# 光已经在耳朵后面了；超过 150° 就是纯逆光/轮廓光。这些角度对应影视灯位图上的
# 「正面光 0°／侧前 45°／侧面 90°／侧后 135°／背面 180°」，取各段中点当边界即可。
FRONT_DEG = 30.0
SIDE_FRONT_DEG = 75.0
SIDE_DEG = 105.0
SIDE_BACK_DEG = 150.0
# 高度：55° 已高过头顶（眼窝、鼻下、下颌都进暗部），读作顶光；-10° 以下才是底光
# （夜戏手电、桌面反光）。两者之外仍按方位说，避免把「高位侧光」说成顶光。
TOP_ELEVATION_DEG = 55.0
BOTTOM_ELEVATION_DEG = -10.0
# 轮廓光：灯在主体水平后方 120° 以外，勾边会盖过正面造型。
RIM_AZIMUTH_DEG = 120.0
# 硬软：聚光角 ≤30° 是光斑边缘锐利的硬光（典型 key light 的 25°—40° 取窄端）；
# ≥45° 光斑边缘宽，读作柔光。中间带宽要看衰减。
HARD_ANGLE_DEG = 30.0
SOFT_ANGLE_DEG = 45.0
# 衰减距离（米）：≤3 m 意味着在人与墙的距离里就掉光，暗部边缘紧、对比强（读作硬）；
# ≥8 m 在室内等于几乎不衰减，明暗过渡平缓（读作柔）。
NEAR_FALLOFF_M = 3.0
FAR_FALLOFF_M = 8.0
# 光比：主光强度 / 其余灯之和。4 倍以上（约两档）暗部基本只靠反射，算高光比；
# 1.5 倍以下接近平光（补光几乎追平主光），算低光比。
HIGH_RATIO = 4.0
LOW_RATIO = 1.5
# 色温：偏暖/偏冷的判定。色值先按 R-B 的差分档（24/255≈9%，常看的暖色偏黄橙都在
# 这个量级以上），灯上写了显式色温（开尔文）时以它为准：4000K 以下偏暖，6000K 以上
# 偏冷，中间是中性（日光 5600K 在中间）。
WARM_RB = 24
WARM_KELVIN = 4000.0
COOL_KELVIN = 6000.0
# 影子的横向分量占水平分量的比例：低于 25% 就当「正对/正背」，只说上/下不说左右。
SHADOW_SIDE_SHARE = 0.25
# 暗部占比（写进风格帧版）：从光比反推的近似值。高光比的暗部约占画面四成（脸的一侧
# 与背景全落进暗部），中等三成，低光比两成——这是给画师的量级提示，不是测光表读数。
DARK_SHARE = {'high': '四成', 'medium': '三成', 'low': '两成'}
# 强度归一的参考值：与 DirectorDesk 的缺省值同源（点/聚光 100，太阳/面光 4）。
INTENSITY_REFERENCE = {'light-point': 100.0, 'light-spot': 100.0,
                       'light-sun': 4.0, 'light-area': 4.0}
# 光源类型对硬软的默认口径：平行光投影锐利（太阳），面光源本身就是大面积发散光；
# 点光没有光斑边界，按衰减距离判。
HARD_ASSETS = ('light-sun',)
SOFT_ASSETS = ('light-area',)
# 提示词里插光线的位置：先找素描规范自带的「画法」节，再找已有的「光线/光照」节。
SKETCH_ANCHOR = '画法'
LIGHT_ANCHORS = ('光线', '光照', '光位')
LIGHT_LABEL = '光线：'
# 方位与影子的中文词：描述里只出现这几档，读的人不必再换算角度。
AZIMUTH_LABELS = ('正面', '侧前方', '侧面', '侧后方', '背面')
DARK_SHARE_KEYS = {'高光比': 'high', '中等': 'medium', '低光比': 'low'}


# -- 抽取 ---------------------------------------------------------------------
def lights_from_scene(scene):
    """从场景里抽灯：``[{kind, color, intensity, position, target, angle, distance, ...}]``。

    ``kind`` 是粗判：``key|fill|rim|ambient``。有景内机位时按「灯相对机位视线」判
    （背后 120° 以外是轮廓光，其余里最亮的是主光，剩下的补光）；连机位都读不到时
    只按亮度取主光，其余归 ``fill``——**未知的归 fill**，不假装知道它的角色。

    数据来源按顺序取第一个有的：

    1. ``scene['lights']``：``previs_scene.load_scene`` 给的灯光视图（字段全，来自 ``.director``）；
    2. ``scene['entities']`` 里 ``asset`` 以 ``light-`` 开头或 ``kind=='light'`` 的行；
    3. ``scene['raw']['entities']``：预演页 ``getScene`` 的原始行（只有 asset 与位置，
       强度/色温/聚光角都没有——页面快照本来就不带这些），所以这类灯的
       ``intensity``/``angle``/``distance``/``color`` 都是 ``None`` 或空串。

    环境光来自 ``scene['lighting']``（整场的 ``LightingConfig``），单独一条 ``ambient``：
    它没有位置，却说得出「均匀环境光」。``lighting`` 缺失时**不补默认值**。
    """
    lights = [_light_row(row) for row in _raw_lights(scene)]
    lights.append(_ambient_row(scene.get('lighting')))
    lights = [row for row in lights if row is not None]
    _assign_kinds(lights, focus=_focus(scene))
    return lights


def _raw_lights(scene):
    """场景里的灯实体：按来源优先级取第一个非空的列表（见 ``lights_from_scene``）。"""
    rows = [row for row in scene.get('lights') or [] if isinstance(row, dict)]
    if rows:
        return rows
    rows = [row for row in scene.get('entities') or []
            if isinstance(row, dict) and (str(row.get('asset') or '').startswith(LIGHT_PREFIX)
                                          or row.get('kind') == 'light')]
    if rows:
        return rows
    raw = scene.get('raw') if isinstance(scene.get('raw'), dict) else {}
    return [row for row in raw.get('entities') or []
            if isinstance(row, dict) and str(row.get('asset') or '').startswith(LIGHT_PREFIX)]


def _light_row(row):
    """一条灯实体 → 统一形状；缺的参数留 ``None``（后果见模块说明：读不到就不说）。"""
    asset = str(row.get('asset') or '').strip()
    position = _vector(row.get('position'))
    target = _vector(row.get('target'))
    if target is None and position is not None:
        # DirectorDesk 的灯朝实体自己的 -z 方向照（makeLight 把 target 放在局部 (0,0,-1)）；
        # 有 rotation 时顺手换算成世界注视点，没有就不知道它照哪儿。
        target = _aim(position, _vector(row.get('rotation')))
    intensity = _number(row.get('intensity'))
    angle = _number(row.get('angle'))
    distance = _number(row.get('distance'))
    if distance is None:
        distance = _number(row.get('range'))  # .director 里叫 range（衰减距离，米）
    brightness = _brightness(asset, intensity)
    return {'kind': 'fill', 'color': _color(row.get('color')), 'intensity': intensity,
            'position': position, 'target': target, 'angle': angle, 'distance': distance,
            # 下面几项是判定与排查用的辅助信息（不属于对外约定的那八个键，但没有它们
            # 光比归一、色温与「哪盏灯」就说不准）：
            'id': str(row.get('id') or ''), 'name': str(row.get('name') or ''),
            'asset': asset, 'brightness': brightness, 'temperature': _number(row.get('temperature'))}


def _ambient_row(lighting):
    """整场的环境亮度 → 一条 ``ambient``；没有 ``lighting`` 就返回 ``None``（不编光）。"""
    if not isinstance(lighting, dict):
        return None
    intensity = _number(lighting.get('ambient'))
    if intensity is None or intensity <= 0:
        return None
    return {'kind': 'ambient', 'color': '', 'intensity': intensity, 'position': None,
            'target': None, 'angle': None, 'distance': None, 'id': '', 'name': '',
            'asset': '', 'brightness': None, 'temperature': None}


def _assign_kinds(lights, focus):
    """就地填 ``kind``：``rim``（灯在主体背后）、``key``（正面这一侧最亮）、其余 ``fill``。"""
    if not lights:
        return
    forward, point = focus
    body = [row for row in lights if row['kind'] != 'ambient']
    if not body:
        return
    for row in body:
        row['kind'] = 'fill'
        if forward is None or point is None or row['position'] is None:
            continue  # 机位或灯位未知：角色只能报「未知」，按约定归 fill
        if _azimuth(row['position'], point, forward) >= RIM_AZIMUTH_DEG:
            row['kind'] = 'rim'
    candidates = [row for row in body if row['kind'] != 'rim']
    key = _key_of(candidates or body)
    if key is not None:
        key['kind'] = 'key'


def _key_of(rows):
    """主光：归一化亮度最高的那一盏；亮度未知时退到最近的那一盏。"""
    if not rows:
        return None
    known = [row for row in rows if row['brightness'] is not None]
    if known:
        return max(known, key=lambda row: row['brightness'])
    return min(rows, key=lambda row: _distance(row))


def _focus(scene):
    """场景自带的参照：第一台机位的视线方向 + 它的注视点（没有机位就是 ``(None, None)``）。"""
    cameras = [row for row in scene.get('cameras') or [] if isinstance(row, dict)]
    if not cameras:
        return None, None
    # cuts[0] 的机位就是第 0 秒正在拍的那台；没有 cuts 就用第一台。
    cuts = [row for row in scene.get('cuts') or [] if isinstance(row, dict)]
    chosen = cameras[0]
    if cuts:
        first = cuts[0].get('camera_id')
        chosen = next((row for row in cameras if row.get('id') == first), cameras[0])
    position, target = _vector(chosen.get('position')), _vector(chosen.get('target'))
    if position is None or target is None:
        return None, None
    forward = _normalize(_sub(target, position))
    return forward, target


# -- 描述 ---------------------------------------------------------------------
def light_description(scene, camera, subjects, *, detail='sketch'):
    """这一帧的光线，一句中文；没有灯就返回空字符串（不编光）。

    ``camera`` 是 ``previs_scene.camera_at`` 的返回值（只要 ``position``/``target``）；
    ``subjects`` 是主体在世界坐标里的位置（见 ``_subject_point``），可以省略——那样
    改用机位注视点当主体，因为「这一帧是关于那个点的」。

    ``detail='sketch'``：≤40 字，只说主光方向、硬软、影子朝哪（素描是排镜用，不该带
    色温和光比，免得画师按风格帧的口径去画）；``detail='style'``：≤90 字，补上色温、
    光比与暗部占比——这两样正是把草图画成最终画面时最容易被忽略、最容易前后不一致的。
    """
    if detail not in DETAILS:
        raise ValueError('光线描述的详细程度须为' + '、'.join(DETAILS))
    if not isinstance(scene, dict):
        return ''  # 没有场景就没有灯：调用方少传东西时不该抛错
    lights = lights_from_scene(scene)
    body = [row for row in lights if row['kind'] != 'ambient']
    if not lights:
        return ''  # 预演里没有灯：一个字都不加（不编默认光）
    if not body:
        return '均匀环境光'  # 只有整场环境亮度：说得出均匀，说不出方向
    forward, up, point = _frame(camera, subjects)
    for row in body:
        row['kind'] = 'fill'
    for row in body:
        if _is_rim(row, forward, point):
            row['kind'] = 'rim'
    key = _key_of([row for row in body if row['kind'] != 'rim'] or body)
    if key is None:
        return ''
    key['kind'] = 'key'
    parts = _sketch_parts(key, forward, up, point)
    if detail == 'style':
        parts = _style_parts(key, body, forward, up, point)
    return _fit(parts, LIMITS[detail])


def _sketch_parts(key, forward, up, point):
    """素描版的三段：主光方向 / 硬软 / 影子方向；每一段可能取不到（那就不说）。"""
    parts = []
    direction = _direction_label(key, forward, point)
    if direction:
        parts.append('主光自' + direction)
    hardness = _hardness(key)
    if hardness:
        parts.append(hardness)
    shadow = _shadow_label(key, forward, up, point)
    if shadow:
        parts.append('投影朝' + shadow)
    return parts


def _style_parts(key, body, forward, up, point):
    """风格帧版：在素描那三项之外加色温、光比与暗部占比。"""
    parts = []
    head = []
    direction = _direction_label(key, forward, point)
    if direction:
        head.append('主光自' + direction)
    hardness = _hardness(key)
    warmth = _warmth(key)
    modifiers = [item for item in (hardness, warmth) if item]
    if modifiers:
        head.append('、'.join(modifiers))
    parts.append('，'.join(head) if head else '')
    ratio = _ratio_label(key, body)
    if ratio:
        parts.append(ratio + '（暗部约' + DARK_SHARE[DARK_SHARE_KEYS[ratio]] + '）')
    shadow = _shadow_label(key, forward, up, point)
    if shadow:
        parts.append('投影朝' + shadow)
    return [row for row in parts if row]


def _fit(parts, limit):
    """按优先级拼句并压到字数上限：方向最要紧，其次影子，最后硬软/色温/光比。

    「光照要在主体上产生结果」是素描规范的要求（见 prompt_defaults 的 sketch_style），
    所以宁可丢掉「硬光」这类修饰，也要保住方向与影子。
    """
    text = '，'.join(parts)
    while parts and len(text) > limit:
        parts = parts[:-1]
        text = '，'.join(parts)
    return text + '。' if text else ''


def _direction_label(row, forward, point):
    """主光方向：按灯相对主体、绕机位视线的方位角与仰角分档（见文件头的阈值）。"""
    if row['position'] is None or forward is None or point is None:
        return ''
    vector = _sub(row['position'], point)
    elevation = _elevation(vector)
    if elevation >= TOP_ELEVATION_DEG:
        return '顶部'
    if elevation <= BOTTOM_ELEVATION_DEG:
        return '底部'
    # 正面/背面是相对**机位视线**说的：灯在机位这一侧（灯的方位与视线相反）是正面光。
    azimuth = _azimuth(row['position'], point, forward)
    for limit, label in ((FRONT_DEG, AZIMUTH_LABELS[0]), (SIDE_FRONT_DEG, AZIMUTH_LABELS[1]),
                         (SIDE_DEG, AZIMUTH_LABELS[2]), (SIDE_BACK_DEG, AZIMUTH_LABELS[3])):
        if azimuth <= limit:
            return label
    return AZIMUTH_LABELS[4]


def _hardness(row):
    """硬软：聚光角与衰减距离（面光/太阳按光源类型直接给口径）。"""
    asset = row['asset']
    if asset in SOFT_ASSETS:
        return '柔光'
    angle, distance = row['angle'], row['distance']
    if asset in HARD_ASSETS:
        return '硬光'
    if angle is not None:
        if angle <= HARD_ANGLE_DEG:
            return '硬光'
        if angle >= SOFT_ANGLE_DEG:
            return '柔光'
        # 30°—45° 的中间带：看光掉得快不快（近距离衰减的明暗落差读作硬）。
        if distance is not None:
            return '硬光' if distance <= NEAR_FALLOFF_M else '柔光'
        return '柔光'
    if distance is None:
        return ''  # 既没有聚光角也没有衰减距离：说不出硬软就不说
    if distance <= NEAR_FALLOFF_M:
        return '硬光'
    return '柔光' if distance >= FAR_FALLOFF_M else ''


def _warmth(row):
    """色温：显式色温（开尔文）优先，否则按色值的暖冷倾向判。"""
    kelvin = row.get('temperature')
    if kelvin is not None:
        if kelvin < WARM_KELVIN:
            return '偏暖'
        return '偏冷' if kelvin > COOL_KELVIN else '中性'
    red, green, blue = _rgb(row['color'])
    if red is None:
        return ''
    if red - blue >= WARM_RB:
        return '偏暖'
    if blue - red >= WARM_RB:
        return '偏冷'
    return '中性'


def _ratio_label(key, body):
    """光比：主光归一化亮度 / 其余灯之和 → 高光比/中等/低光比；数值缺一个都不判。"""
    others = [row['brightness'] for row in body if row is not key]
    if key['brightness'] is None or any(value is None for value in others):
        return ''
    rest = sum(others)
    if rest <= 0:
        return '高光比'  # 只有一盏主光：没有补光，暗部就是全黑
    ratio = key['brightness'] / rest
    if ratio >= HIGH_RATIO:
        return '高光比'
    return '低光比' if ratio < LOW_RATIO else '中等'


def _shadow_label(row, forward, up, point):
    """影子朝哪：主光相对主体的水平方位 + 灯在主体的哪一侧（见 SHADOW_SIDE_SHARE）。"""
    if row['position'] is None or forward is None or up is None or point is None:
        return ''
    vector = _sub(row['position'], point)
    right = _normalize(_cross(forward, (0.0, 1.0, 0.0)))
    if right is None:
        right = _normalize(_cross(forward, (0.0, 0.0, 1.0)))
    if right is None:
        return ''
    side = _dot(vector, right)
    depth = _dot(vector, forward)
    height = _dot(vector, up)
    horizontal = math.hypot(side, depth)
    label = ''
    if horizontal > 1e-6 and abs(side) / horizontal >= SHADOW_SIDE_SHARE:
        label += '左' if side > 0 else '右'  # 灯在画面右侧 → 影子落向左侧
    if height < 0:
        label += '上'  # 灯在主体下方：影子被推到上方（墙面/天花板）
    elif depth > 0:
        label += '下'  # 灯比主体更靠里（画面深处）：影子朝机位倒，画面上落在下方
    else:
        label += '上'  # 灯在机位与主体之间：影子朝画面深处倒，画面上落在上方
    return label


def _is_rim(row, forward, point):
    """这盏灯是不是轮廓光：在主体水平后方 120° 以外。"""
    if row['position'] is None or forward is None or point is None:
        return False
    return _azimuth(row['position'], point, forward) >= RIM_AZIMUTH_DEG


# -- 拼进提示词 ---------------------------------------------------------------
def merge_into_prompt(base_prompt, description):
    """把光线描述作为独立一行插进提示词；``description`` 为空就原样返回。

    插在哪由 ``base_prompt`` 自己的节标题决定（这里只有两个参数，判断依据只能是它）：

    * 有「画法：」节（素描规范自带，见 ``prompt_defaults`` 的 ``sketch_style``）→ 紧跟其后。
      光就是画法的结果，贴着画法写，画师不会漏看；
    * 否则有「光线/光照/光位」节（项目风格里人写的那段）→ 放在它后面**并列**，绝不覆盖
      人写的光线段落，两段一起交给模型；
    * 都没有 → 补在末尾，原有每一行一个字都不动。
    """
    if not isinstance(base_prompt, str):
        raise ValueError('提示词须为文本')
    text = (description or '').strip()
    if not text:
        return base_prompt  # 没有光线描述：一个字节都不改
    line = LIGHT_LABEL + text
    lines = base_prompt.split('\n')
    if any(row.strip() == line for row in lines):
        return base_prompt  # 已经插过（同一份提示词被拼两次时不重复）
    anchor = _anchor(lines)
    if anchor is None:
        return base_prompt + ('' if not base_prompt or base_prompt.endswith('\n') else '\n') + line
    lines.insert(anchor + 1, line)
    return '\n'.join(lines)


def _anchor(lines):
    """找到要插在其后的那一行：先「画法」，再已有的光线节；都没有返回 ``None``。"""
    for index, row in enumerate(lines):
        if row.strip().startswith(SKETCH_ANCHOR):
            return index
    for index, row in enumerate(lines):
        stripped = row.strip()
        if any(stripped.startswith(anchor) for anchor in LIGHT_ANCHORS):
            return index
    return None


# -- 小工具 -------------------------------------------------------------------
def _frames_of(subject):
    """一个主体 → 世界坐标里的代表点：三数元组、或带 ``position`` 的映射都收。"""
    if isinstance(subject, dict):
        point = _vector(subject.get('position'))
        if point is None:
            return None
        height = _number(subject.get('height'))
        # 人的 position 是脚底；取半身高度当「主体中心」，影子与受光面都从那儿算。
        return (point[0], point[1] + (height / 2 if height else 0.0), point[2])
    return _vector(subject)


def _subject_point(subjects, camera):
    """主体中心：给了就用它们的中点；没给用注视点（这一帧就是关于那个点的）。"""
    points = [row for row in (_frames_of(subject) for subject in subjects or []) if row is not None]
    if points:
        return (sum(row[0] for row in points) / len(points),
                sum(row[1] for row in points) / len(points),
                sum(row[2] for row in points) / len(points))
    target = _vector((camera or {}).get('target')) if isinstance(camera, dict) else None
    return target


def _frame(camera, subjects):
    """机位的 (前, 上) 与主体点；朝向未知时前/上为 ``None``（描述里就只剩能说的部分）。"""
    position = _vector((camera or {}).get('position')) if isinstance(camera, dict) else None
    target = _vector((camera or {}).get('target')) if isinstance(camera, dict) else None
    forward = _normalize(_sub(target, position)) if position and target else None
    right = _normalize(_cross(forward, (0.0, 1.0, 0.0))) if forward else None
    up = _normalize(_cross(right, forward)) if forward and right else None
    return forward, up, _subject_point(subjects, camera)


def _azimuth(position, point, forward):
    """灯相对主体的水平方向，绕机位视线的夹角（度，0=正面光，180=纯逆光）。"""
    vector = _sub(position, point)
    horizontal = (vector[0], 0.0, vector[2])
    facing = (-forward[0], 0.0, -forward[2])  # 从主体看向机位的方向＝正面光的方向
    a, b = _normalize(horizontal), _normalize(facing)
    if a is None or b is None:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, _dot(a, b)))))


def _elevation(vector):
    """灯相对主体的仰角（度）：正数越高，负数在主体下方。"""
    horizontal = math.hypot(vector[0], vector[2])
    if horizontal < 1e-6 and abs(vector[1]) < 1e-6:
        return 0.0
    return math.degrees(math.atan2(vector[1], horizontal))


def _aim(position, rotation):
    """灯的世界注视点：three.js Euler XYZ 作用在局部 (0,0,-1) 上（与 previs_scene 同口径）。"""
    if rotation is None:
        return None
    x, y, _z = rotation
    forward = (-math.sin(y), math.sin(x) * math.cos(y), -math.cos(x) * math.cos(y))
    return tuple(position[index] + forward[index] for index in range(3))


def _brightness(asset, intensity):
    """归一化亮度：不同光源单位不可比，先按各自参考值归一（见 INTENSITY_REFERENCE）。"""
    if intensity is None:
        return None
    reference = INTENSITY_REFERENCE.get(asset)
    return None if not reference else intensity / reference


def _distance(row):
    """灯到它自己注视点的距离（没有注视点时当作 0，用于「亮度未知时取最近」）。"""
    if row['position'] is None or row['target'] is None:
        return 0.0
    return _length(_sub(row['target'], row['position']))


def _vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    rows = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            return None
        rows.append(float(item))
    return tuple(rows)


def _number(value):
    value = _animated(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _animated(value):
    """``AnimatedNumber``（数值或 ``{keys:[{time,value}]}``）→ 首个关键帧的值。"""
    if isinstance(value, dict):
        keys = value.get('keys')
        if isinstance(keys, list) and keys and isinstance(keys[0], dict):
            return keys[0].get('value')
        return None
    return value


def _color(value):
    """色值：``#rrggbb``（大小写都收）；其它写法返回空串（色温就判不出来）。"""
    text = str(value or '').strip()
    if len(text) == 7 and text.startswith('#'):
        try:
            int(text[1:], 16)
        except ValueError:
            return ''
        return text.lower()
    return ''


def _rgb(value):
    if len(value) != 7:
        return None, None, None
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _length(value):
    return math.sqrt(_dot(value, value))


def _normalize(value):
    length = _length(value)
    return None if length < 1e-12 else tuple(item / length for item in value)
