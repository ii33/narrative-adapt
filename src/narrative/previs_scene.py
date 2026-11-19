"""预演工程（DirectorDesk 的 ``.director``）读取与机位投影：把三维预演变成一帧框图。

为什么单独一层：``.director`` 是三维世界（米、y 向上、焦距毫米），框图
（``storyboard_layout``）是二维归一化画面坐标。两者的换算只依赖工程文件本身，
不该掺进 Qt、数据库或渲染服务——所以这一层是纯函数：给定文件、时刻、画幅，
回答「这一帧谁在画面哪一块、离机位多远」。测试因此只要能手算的小场景。

坐标与角度约定（照 DirectorDesk 自己的相机实现来，别自己再定一套）：

* ``position`` 是 ``[x, y, z]``，单位米，**y 向上**（地面是 x-z 平面），见
  ``.runtime/vendor/director-desk/src/model.ts`` 的 ``+Y 向上、+Z 向前``；
* 相机 ``yaw``=0 时看向 **-z**；正 yaw 绕 +y 轴转，视线由 -z 转向 -x（也就是向左），
  反之注视点落在画面右侧时 yaw 为负；``pitch`` 为正表示抬头。单位为度。
  两项都由「注视点 - 机位」求出（``sample-camera.ts`` 里 aim 不是 manual
  时就是 ``camera.lookAt(target)``），aim=manual 时由实体 rotation 求方向；
* 画面坐标 ``bbox`` 是归一化的 ``[x0, y0, x1, y1]``：原点左上、x 向右、y 向下、0..1，
  与 ``spatial/geometry.ts::pinholeFrameBounds`` 的 ``(x/w+1)/2``、``(1-y/w)/2`` 同一口径；
* 视角：``cinematography/sample-camera.ts::configureCamera`` 把 three.js 的
  ``filmGauge`` 固定成 36 mm 再 ``setFocalLength(focal)``，于是横画幅的等效物是
  「传感器宽 36 mm」——``tan(hFOV/2) = 36/(2·focal)``，竖直方向按画幅比换算。
  竖画幅（9:16）下 three.js 把 36 mm 当短边，本模块照抄同一分支，所以两种画幅都对得上。

已知的近似（都只影响 bbox 的第几位小数，不是错的，是刻意的）：

* 人物当成 0.45 m 见方的立柱（``SHOULDER_WIDTH_M``），忽略转身与姿态；
* 道具当成 ``scale`` 大小的世界轴对齐盒体，底面贴在实体 y 上（DirectorDesk 的
  ``assets/terrain.ts`` 正是把地面等道具建成局部 y∈[0,1] 的盒体再乘 scale）；
* 忽略实体 ``rotation``（两个样例里的道具 rotation 全是 0；人物足迹是方柱，转动不改变占位）；
* 忽略 ``camera.hiddenEntityIds`` / ``hideWalls``（隐藏实体仍会被投影出来）；
* 轨迹只做线性插值：DirectorDesk 在 ``path.smooth`` 时用 Catmull-Rom 空间曲线，
  本模块按时段线性插值位置，但时间参数沿用它的 easing 语义（见 ``_eased``）。
  任务记录下来的轨迹点很密，两者差值在厘米级；这是与渲染帧对照时要留意的唯一系统性偏差。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

FORMAT = 'director-desk'
# 与 DirectorDesk 的 camera.filmGauge 一致：传感器短边 36 mm（横画幅即宽度）。
SENSOR_GAUGE_MM = 36.0
DEFAULT_FOCAL_MM = 35.0
DEFAULT_ASPECT = '16:9'
# 与 configureCamera 的 camera.near 一致；跨过它的盒子要在这一面裁开再投影。
NEAR_PLANE_M = 0.025
SHOULDER_WIDTH_M = 0.45
DEFAULT_HEIGHT_M = 1.75
# 灯光没有可见几何（只有一次投射），按盒体投影只会变成挡住画面的假盒子。
SKIP_ASSET_PREFIXES = ('light-',)
# actor/prop 落到框图的对象分类上：框图只认 person/prop/location/material。
LAYOUT_KINDS = {'actor': 'person', 'prop': 'prop'}
# 景别粗判：见 framing_of 的说明，阈值按「1.75 m 的人在取景框里占多高」定。
CLOSE_HEIGHT = 0.90
MEDIUM_HEIGHT = 0.45
EDGE_MARGIN = 0.02
WIDE_ASPECT = 1.9
BBOX_DIGITS = 4
DEPTH_DIGITS = 3

# 盒体的 8 个角与 6 个面：面用来对视锥六面做实心裁剪，见 _project_box。
_CORNERS = tuple((x, y, z) for x in (0, 1) for y in (0, 1) for z in (0, 1))
# 与 DirectorDesk 的 spatial/geometry.ts::faces 同一组面（角点编号也一致：bit0=x、bit1=y、bit2=z）。
_BOX_FACES = ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3))


# -- 读取 ---------------------------------------------------------------------

def load_scene(path, scene_id=None):
    """读一个 ``.director``，返回场景的扁平视图（见模块说明的字段）。

    ``scene_id`` 只对多场景文件有意义：给场景编号（或场景名）就取那一个，
    省略就取 ``activeSceneId``，再退到第一个场景。单场景文件不给它。
    """
    file = Path(path)
    if not file.is_file():
        raise ValueError(f'预演工程文件不存在：{file}；请确认路径，或先用 DirectorDesk 另存一份 .director')
    try:
        data = json.loads(file.read_text(encoding='utf-8-sig'))
    except UnicodeError as error:
        raise ValueError(f'预演工程 {file.name} 不是 UTF-8 文本，无法读取') from error
    except json.JSONDecodeError as error:
        raise ValueError(f'预演工程 {file.name} 不是合法 JSON（第{error.lineno}行第{error.colno}列：{error.msg}）') from error
    if not isinstance(data, dict):
        raise ValueError(f'预演工程 {file.name} 的顶层须为对象')
    if data.get('format') != FORMAT:
        raise ValueError(f'{file.name} 不是 DirectorDesk 预演工程（format={data.get("format")!r}，'
                         f'需要 {FORMAT!r}）；请从 DirectorDesk 另存工程文件，不要手改后缀')
    state, identity, scenes = _scene_state(data, file, scene_id)
    duration = _positive(state.get('duration'), f'场景{identity["name"]}的时长（秒）')
    fps = state.get('fps', 24)
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ValueError(f'场景{identity["name"]}的帧率须为 1..120 的整数，实际是 {fps!r}')
    aspect = state.get('aspect') or DEFAULT_ASPECT
    ratio = _aspect_ratio(aspect)
    cameras, entities = [], []
    for index, row in enumerate(_rows(state.get('entities'), f'场景{identity["name"]}的实体')):
        parsed = _entity(row, index, identity['name'])
        if parsed is None:
            continue
        (cameras if parsed['kind'] == 'camera' else entities).append(parsed)
    cuts = _cuts(state.get('cuts'), {row['id'] for row in cameras}, identity['name'])
    return {'path': str(file), 'scene_id': identity['id'], 'name': identity['name'],
            'duration_s': duration, 'fps': fps, 'aspect': str(aspect), 'aspect_ratio': ratio,
            'cameras': cameras, 'entities': entities, 'cuts': cuts, 'scenes': scenes}


def _scene_state(data, file, scene_id):
    """兼容两种工程结构：扁平（version 1）与多场景（version ≥ 2 的 scenes[].state）。"""
    scenes = data.get('scenes')
    if isinstance(scenes, list) and scenes:
        rows = [row for row in scenes if isinstance(row, dict)]
        names = [{'id': str(row.get('id') or ''), 'name': str(row.get('name') or '')} for row in rows]
        chosen = None
        if scene_id is not None:
            chosen = next((row for row in rows if scene_id in (row.get('id'), row.get('name'))), None)
            if chosen is None:
                raise ValueError('预演工程里没有场景' + str(scene_id) + '；现有场景：'
                                 + ('、'.join(row['name'] or row['id'] for row in names) or '（无）'))
        else:
            active = data.get('activeSceneId')
            chosen = next((row for row in rows if row.get('id') == active), None) or rows[0]
        state = chosen.get('state')
        if not isinstance(state, dict):
            raise ValueError(f'预演工程 {file.name} 的场景{chosen.get("name") or chosen.get("id")}缺少 state 数据，'
                             '文件可能被裁剪过；请在 DirectorDesk 里重新另存')
        identity = {'id': str(chosen.get('id') or file.stem), 'name': str(chosen.get('name') or chosen.get('id') or file.stem)}
        return state, identity, names
    # 扁平结构：顶层就是场景本身，没有 scenes 索引。
    if scene_id is not None:
        raise ValueError(f'预演工程 {file.name} 是单场景结构（实体与 cut 直接在顶层），'
                         '没有可选场景编号；请去掉 scene_id，或在 DirectorDesk 里建立多场景工程')
    identity = {'id': file.stem, 'name': str(data.get('name') or file.stem)}
    return data, identity, [identity]


def _rows(value, label):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f'{label}须为数组')
    return value


def _entity(row, index, scene_name):
    label = f'场景{scene_name}的实体第{index + 1}项'
    if not isinstance(row, dict):
        raise ValueError(f'{label}须为对象')
    kind = row.get('kind')
    if kind not in ('actor', 'prop', 'camera'):
        return None  # 分组、占位等其它 kind 不参与投影
    identifier = row.get('id')
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError(f'{label}缺少 id（实体编号是块图的 anchor，不能为空）')
    asset = row.get('asset')
    name = str(row.get('name') or identifier)
    if kind != 'camera' and (row.get('visible') is False or str(asset or '').startswith(SKIP_ASSET_PREFIXES)):
        return None  # 隐藏实体与灯光不产生可见画面
    position = _vector(row.get('position'), f'{label}的position')
    rotation = _vector(row.get('rotation'), f'{label}的rotation')
    if kind == 'camera':
        camera = row.get('camera')
        if not isinstance(camera, dict):
            raise ValueError(f'{label}（摄影机{name}）缺少 camera 设置，无法取焦距与注视点')
        camera_effects = camera.get('effects') if isinstance(camera.get('effects'), dict) else {}
        channels = camera_effects.get('channels') if isinstance(camera_effects.get('channels'), dict) else {}
        return {'id': identifier.strip(), 'name': name, 'kind': 'camera', 'position': position,
                'rotation': rotation, 'path': _points(row.get('path'), f'{label}的机位轨迹'),
                'focal': _positive(camera.get('focal', DEFAULT_FOCAL_MM), f'{label}的焦距（毫米）'),
                'focal_keys': _number_keys(channels.get('focal'), f'{label}的焦距关键帧'),
                'target': _vector(camera['target'], f'{label}的注视点') if 'target' in camera else None,
                'target_id': str(camera.get('targetId') or '').strip() or None,
                'target_height': _number(camera.get('targetHeight'), f'{label}的targetHeight', 0.0),
                'target_path': _points(camera.get('targetPath'), f'{label}的视线轨迹'),
                'aim': str(camera.get('aim') or 'target'), 'mode': str(camera.get('mode') or 'free')}
    height = _number(row.get('height'), f'{label}的height', DEFAULT_HEIGHT_M)
    return {'id': identifier.strip(), 'name': name, 'kind': kind, 'position': position,
            'rotation': rotation, 'scale': _vector(row.get('scale'), f'{label}的scale', (1.0, 1.0, 1.0)),
            'height': height if height > 0 else DEFAULT_HEIGHT_M,
            'path': _points(row.get('path'), f'{label}的轨迹')}


def _cuts(value, camera_ids, scene_name):
    """时间 → 摄影机的切换表；按时间排序，指向不存在的摄影机就直说。"""
    rows = []
    for index, item in enumerate(_rows(value, f'场景{scene_name}的cuts')):
        if not isinstance(item, dict):
            raise ValueError(f'场景{scene_name}的cut第{index + 1}项须为对象')
        time_s = _number(item.get('time'), f'场景{scene_name}的cut第{index + 1}项时间')
        camera_id = item.get('cameraId')
        if camera_id not in camera_ids:
            raise ValueError(f'场景{scene_name}的cut@{time_s}s指向不存在的摄影机{camera_id!r}；'
                             '请在 DirectorDesk 里重新指定机位或删除该切点')
        rows.append({'time': time_s, 'camera_id': camera_id})
    rows.sort(key=lambda row: row['time'])
    return rows


def _points(raw, label):
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ValueError(f'{label}须为{{smooth, points:[{{time, position}}]}}对象')
    rows = []
    for index, item in enumerate(_rows(raw.get('points'), f'{label}.points')):
        if not isinstance(item, dict):
            raise ValueError(f'{label}.points第{index + 1}项须为对象')
        rows.append({'time': _number(item.get('time'), f'{label}.points[{index}].time'),
                     'position': _vector(item.get('position'), f'{label}.points[{index}].position'),
                     'easing': item.get('easing')})
    rows.sort(key=lambda row: row['time'])
    return rows


def _number_keys(raw, label):
    if raw is None:
        return []
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return [{'time': 0.0, 'value': float(raw), 'easing': None}]
    if not isinstance(raw, dict):
        raise ValueError(f'{label}须为数值或{{keys:[{{time, value}}]}}对象')
    rows = []
    for index, item in enumerate(_rows(raw.get('keys'), f'{label}.keys')):
        if not isinstance(item, dict):
            raise ValueError(f'{label}.keys第{index + 1}项须为对象')
        rows.append({'time': _number(item.get('time'), f'{label}.keys[{index}].time'),
                     'value': _positive(item.get('value'), f'{label}.keys[{index}].value'),
                     'easing': item.get('easing')})
    rows.sort(key=lambda row: row['time'])
    return rows


# -- 机位 ---------------------------------------------------------------------

def camera_at(scene, time_s, camera_id=None):
    """取该时刻的机位：按 ``cuts`` 选机，再按机位轨迹插值位置。

    返回 ``{position, target, focal, yaw, pitch, camera_id, name}``；角度单位为度。
    ``camera_id`` 给定时强制用那一台（不理会 cuts），供「这一镜就绑这台机器」使用。
    """
    time_s = _time(time_s)
    cameras = {row['id']: row for row in scene['cameras']}
    if not cameras:
        raise ValueError('预演里没有摄影机；请先在 DirectorDesk 里放置机位并切镜')
    chosen = camera_id if camera_id is not None else _active_camera_id(scene, time_s)
    camera = cameras.get(chosen)
    if camera is None:
        raise ValueError(f'预演里没有摄影机{chosen}；现有：'
                         + '、'.join(f'{row["name"]}({row["id"]})' for row in scene['cameras']))
    position = _sample(camera['path'], time_s, camera['position'])
    target = _target_at(scene, camera, time_s)
    focal = _number_at(camera['focal_keys'], time_s, camera['focal'])
    yaw, pitch = _angles(position, target)
    return {'camera_id': camera['id'], 'name': camera['name'], 'position': list(position),
            'target': list(target), 'focal': focal, 'yaw': yaw, 'pitch': pitch}


def _active_camera_id(scene, time_s):
    """DirectorDesk 的 activeCameraId：从第一台开始，遇到大于当前时刻的 cut 就停。"""
    cuts = scene.get('cuts') or []
    if not cuts:
        return scene['cameras'][0]['id']
    active = cuts[0]['camera_id']
    for cut in cuts:
        if cut['time'] > time_s:
            break
        active = cut['camera_id']
    return active


def _target_at(scene, camera, time_s):
    """注视点：视线轨迹 > 跟踪目标 + targetHeight > 写死的 target > 实体 rotation。"""
    if camera['target_path']:
        return _sample(camera['target_path'], time_s, camera['position'])
    if camera['target_id']:
        target = next((row for row in scene['entities'] if row['id'] == camera['target_id']), None)
        if target is None:
            raise ValueError(f'摄影机{camera["name"]}跟踪的实体{camera["target_id"]}不在预演里；'
                             '请在 DirectorDesk 里重新指定跟踪目标或清空 targetId')
        base = _sample(target['path'], time_s, target['position'])
        return (base[0], base[1] + camera['target_height'], base[2])
    if camera['target'] is not None and camera['aim'] != 'manual':
        return camera['target']
    # aim=manual：朝向由实体自己的 rotation 决定（three.js Euler XYZ 作用在 (0,0,-1) 上）。
    x, y, _z = camera['rotation']
    forward = (-math.sin(y), math.sin(x) * math.cos(y), -math.cos(x) * math.cos(y))
    position = _sample(camera['path'], time_s, camera['position'])
    return tuple(position[i] + forward[i] for i in range(3))


def _angles(position, target):
    """由「注视点 - 机位」求 yaw/pitch（度）：yaw=0 看向 -z，pitch 为正即抬头。"""
    dx, dy, dz = (target[i] - position[i] for i in range(3))
    horizontal = math.hypot(dx, dz)
    if horizontal < 1e-9 and abs(dy) < 1e-9:
        raise ValueError('摄影机位置与注视点重合，无法确定朝向；请在预演里把机位挪开或改注视点')
    yaw = math.degrees(math.atan2(-dx, -dz)) if horizontal >= 1e-9 else 0.0
    pitch = math.degrees(math.atan2(dy, horizontal))
    return yaw, pitch


# -- 投影 ---------------------------------------------------------------------

def project(scene, camera, time_s, aspect=None):
    """把可见实体投影成归一化 bbox 与景深，返回按 depth **从远到近**排序的行。

    ``camera`` 是 ``camera_at`` 的返回值（只要 position/target/focal 三项）；
    ``aspect`` 省略时用场景自己的画幅。
    行：``{entity_id, name, kind, bbox, depth}``，``kind`` 已映射成框图的 person/prop。
    """
    time_s = _time(time_s)
    ratio = _aspect_ratio(aspect if aspect is not None else scene.get('aspect_ratio') or scene.get('aspect') or DEFAULT_ASPECT)
    position = _vector(camera.get('position'), '摄影机位置')
    target = _vector(camera.get('target'), '摄影机注视点')
    focal = _positive(camera.get('focal', DEFAULT_FOCAL_MM), '焦距（毫米）')
    tan_h, tan_v = _fov_extents(focal, ratio)
    basis = _basis(position, target)
    rows = []
    for entity in scene['entities']:
        low, high = _entity_box(entity, time_s)
        hit = _project_box(low, high, position, basis, tan_h, tan_v)
        if hit is None:
            continue
        bbox, depth = hit
        rows.append({'entity_id': entity['id'], 'name': entity['name'],
                     'kind': LAYOUT_KINDS.get(entity['kind'], 'prop'),
                     'bbox': bbox, 'depth': depth})
    # 框图约定按 depth 从远到近：先画远的，近的压在上面。
    rows.sort(key=lambda row: (-row['depth'], row['entity_id']))
    return rows


def _fov_extents(focal, ratio):
    """返回 ``(tan(hFOV/2), tan(vFOV/2))``，与 three.js 的 filmGauge=36 mm 同口径。

    横画幅（ratio ≥ 1）时传感器宽就是 36 mm：``tan(hFOV/2) = 36/(2·focal)``，
    竖直方向再按画幅比折算；竖画幅下 three.js 把 36 mm 当短边，这里照抄。
    """
    width = SENSOR_GAUGE_MM * min(ratio, 1.0)
    height = SENSOR_GAUGE_MM / max(ratio, 1.0)
    return width / (2 * focal), height / (2 * focal)


def _basis(position, target):
    """相机的 (forward, right, up)：与 three.js ``lookAt`` 同一右手系。"""
    forward = _normalize(tuple(target[i] - position[i] for i in range(3)))
    if forward is None:
        raise ValueError('摄影机位置与注视点重合，无法确定画面朝向；请在预演里把机位挪开或改注视点')
    right = _cross(forward, (0.0, 1.0, 0.0))
    if _length(right) < 1e-9:
        # 正俯视/仰视时上向量与视线平行，改拿 +z 当参考（three.js 的 lookAt 也在这里扰动 up）。
        right = _cross(forward, (0.0, 0.0, 1.0))
        if _length(right) < 1e-9:
            right = (1.0, 0.0, 0.0)
    right = _normalize(right)
    up = _cross(right, forward)
    return forward, right, up


def _entity_box(entity, time_s):
    """实体的世界盒体 ``(min, max)``：人是方柱，道具是按 scale 的盒体、底面贴在实体 y 上。"""
    position = _sample(entity['path'], time_s, entity['position'])
    if entity['kind'] == 'actor':
        half = SHOULDER_WIDTH_M / 2
        return ((position[0] - half, position[1], position[2] - half),
                (position[0] + half, position[1] + entity['height'], position[2] + half))
    scale = entity['scale']
    return ((position[0] - scale[0] / 2, position[1], position[2] - scale[2] / 2),
            (position[0] + scale[0] / 2, position[1] + scale[1], position[2] + scale[2] / 2))


def _project_box(low, high, position, basis, tan_h, tan_v):
    """盒体 → (bbox, depth)；整体出画或在相机背后时返回 None。

    必须先按视锥六面把盒面裁开再取包围盒，不能只投影 8 个角点求 min/max：画面外的
    部分会把 bbox 撑大（地面贴着画幅下沿时，它最左角其实在画面外，那个 u 并不是
    可见像素的边）。DirectorDesk 也是先裁剪再取包围盒（``spatial/geometry.ts`` 的
    ``pinholeFrameBounds``），这里在相机空间做同一件事——用 forward 距离当分母，
    不必像它那样走齐次坐标，也就不会在相机背后出现 w<0 的镜像点。

    相机被盒子包住时（站在地面/围墙里），六个面全被裁空，但这一帧确实整幅都是它：
    按整幅 ``[0,0,1,1]`` 处理，与 geometry.ts 的 frustumInside 分支同一个结论。
    """
    forward, right, up = basis
    corners = []
    for corner in _CORNERS:
        point = tuple((high[i] if corner[i] else low[i]) - position[i] for i in range(3))
        corners.append((_dot(point, right), _dot(point, up), _dot(point, forward)))
    planes = (((0.0, 0.0, 1.0), -NEAR_PLANE_M), ((1.0, 0.0, tan_h), 0.0), ((-1.0, 0.0, tan_h), 0.0),
              ((0.0, 1.0, tan_v), 0.0), ((0.0, -1.0, tan_v), 0.0))
    points = []
    for face in _BOX_FACES:
        polygon = [corners[index] for index in face]
        for normal, offset in planes:
            polygon = _clip_polygon(polygon, normal, offset)
            if not polygon:
                break
        points.extend(polygon)
    if not points:
        if all(low[i] <= position[i] <= high[i] for i in range(3)):
            return [0.0, 0.0, 1.0, 1.0], round(NEAR_PLANE_M, DEPTH_DIGITS)
        return None
    us, vs = [], []
    for x_camera, y_camera, depth in points:
        us.append(0.5 + 0.5 * (x_camera / (depth * tan_h)))
        vs.append(0.5 - 0.5 * (y_camera / (depth * tan_v)))
    bbox = [round(_clamp(min(us)), BBOX_DIGITS), round(_clamp(min(vs)), BBOX_DIGITS),
            round(_clamp(max(us)), BBOX_DIGITS), round(_clamp(max(vs)), BBOX_DIGITS)]
    if not bbox[0] < bbox[2] or not bbox[1] < bbox[3]:
        return None  # 只擦到画面一条边，投影后没有面积
    center = tuple((low[i] + high[i]) / 2 for i in range(3))
    depth = _dot(tuple(center[i] - position[i] for i in range(3)), forward)
    if depth < NEAR_PLANE_M:
        # 盒体把相机包住了：中心在机位背后，改取最近一个可见点的景深。
        depth = min(point[2] for point in points)
    return bbox, round(depth, DEPTH_DIGITS)


def _clip_polygon(polygon, normal, offset):
    """Sutherland–Hodgman：保留 ``dot(normal, p) + offset >= 0`` 的一侧。"""
    result = []
    for index, current in enumerate(polygon):
        previous = polygon[index - 1]
        d_current = _dot(normal, current) + offset
        d_previous = _dot(normal, previous) + offset
        if d_current >= 0:
            if d_previous < 0:
                result.append(_crossing(previous, current, d_previous, d_current))
            result.append(current)
        elif d_previous >= 0:
            result.append(_crossing(previous, current, d_previous, d_current))
    return result


def _crossing(previous, current, d_previous, d_current):
    u = d_previous / (d_previous - d_current)
    return tuple(a + (b - a) * u for a, b in zip(previous, current, strict=True))


def framing_of(bbox, aspect=None):
    """按主体在画面中的占比粗判景别：``wide|medium|close``。

    规则（写死在这里，日后要调就改这三个常量）：

    1. 没有 bbox（画面里没有主体）或退化 → ``wide``；
    2. ``h = y1 - y0``（归一化高度）；贴边＝被取景框切掉（``y0 ≤ 0.02`` 或 ``y1 ≥ 0.98``，
       宽画幅 ``ratio ≥ 1.9`` 时左右贴边也算）——被切掉说明主体比画面大，
       此时 h 只反映「看见了多少」；
    3. ``h ≥ 0.90`` 或（``h ≥ 0.45`` 且贴边）→ ``close``；``h ≥ 0.45`` → ``medium``；否则 ``wide``。

    为什么是 0.45/0.90：1.75 m 的人占满画面高度即 h≈1；h≈0.80 对应取景高度约 2.2 m
    （DirectorDesk 自带「全景」预设的取景高度），h≈0.45 对应约 3.9 m（远景/全景边界），
    而被切到只剩 1.1 m 左右就是「中景」预设那种半身取景。``aspect`` 参与贴边判定：
    16:9 里左右出框在群戏里很常见，不足以说明是近景；超宽画幅里横向出框基本就是近景/特写。
    """
    if not bbox or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return 'wide'
    try:
        x0, y0, x1, y1 = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return 'wide'
    height = y1 - y0
    if not height > 0 or not x1 > x0:
        return 'wide'
    ratio = _aspect_ratio(aspect if aspect is not None else DEFAULT_ASPECT)
    cropped = y0 <= EDGE_MARGIN or y1 >= 1 - EDGE_MARGIN
    if not cropped and ratio >= WIDE_ASPECT:
        cropped = x0 <= EDGE_MARGIN or x1 >= 1 - EDGE_MARGIN
    if height >= CLOSE_HEIGHT or (cropped and height >= MEDIUM_HEIGHT):
        return 'close'
    return 'medium' if height >= MEDIUM_HEIGHT else 'wide'


# -- 小工具 -------------------------------------------------------------------

def _eased(value, easing):
    """DirectorDesk 的速度曲线（``animation/channels.ts::eased``）的同一套语义。

    自定义 bezier 曲线不在这里重解（那要复制它的二分求根），按匀速处理并在模块说明里说明。
    """
    t = min(1.0, max(0.0, value))
    if isinstance(easing, dict):
        return t
    if easing == 'smooth':
        return t * t * (3 - 2 * t)
    if easing == 'ease-in':
        return t * t
    if easing == 'ease-out':
        return 1 - (1 - t) ** 2
    if easing == 'hold':
        return 0.0 if t < 1 else 1.0
    if easing == 'whip':
        return 8 * t ** 4 if t < .5 else 1 - 8 * (1 - t) ** 4
    return t


def _sample(points, time_s, fallback):
    """轨迹取样：首末点外保持端点，区间内线性插值位置、时间参数按后一点 easing。"""
    if not points:
        return tuple(fallback)
    if time_s <= points[0]['time']:
        return points[0]['position']
    if time_s >= points[-1]['time']:
        return points[-1]['position']
    for index in range(1, len(points)):
        if time_s < points[index]['time']:
            start, end = points[index - 1], points[index]
            span = end['time'] - start['time']
            u = _eased((time_s - start['time']) / span if span > 0 else 1.0, end.get('easing'))
            return tuple(a + (b - a) * u for a, b in zip(start['position'], end['position'], strict=True))
    return points[-1]['position']


def _number_at(keys, time_s, fallback):
    """数值关键帧（焦距）：与 ``channels.ts::numberAt`` 一样按后一点 easing 插值。"""
    if not keys:
        return fallback
    if time_s <= keys[0]['time']:
        return keys[0]['value']
    for index in range(1, len(keys)):
        if time_s < keys[index]['time']:
            start, end = keys[index - 1], keys[index]
            span = end['time'] - start['time']
            u = _eased((time_s - start['time']) / span if span > 0 else 1.0, end.get('easing'))
            return start['value'] + (end['value'] - start['value']) * u
    return keys[-1]['value']


def _aspect_ratio(value):
    """画幅：接受 ``'16:9'`` 这类文本，也接受直接的比例数值。"""
    if isinstance(value, str) and ':' in value:
        width, _, height = value.partition(':')
        try:
            w, h = float(width), float(height)
        except ValueError as error:
            raise ValueError(f'画幅须为「宽:高」形式，例如 16:9，实际是 {value!r}') from error
        if not w > 0 or not h > 0:
            raise ValueError(f'画幅的宽高须大于 0，实际是 {value!r}')
        return w / h
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    raise ValueError(f'画幅须为「宽:高」文本或大于 0 的比值，实际是 {value!r}')


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'预演时刻须为秒数，实际是 {value!r}')
    if value < 0:
        raise ValueError(f'预演时刻不能为负，实际是 {value!r}')
    return float(value)


def _number(value, label, default=None):
    if value is None:
        if default is None:
            raise ValueError(f'{label}缺失')
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label}须为有限数值，实际是 {value!r}')
    return float(value)


def _positive(value, label):
    result = _number(value, label)
    if not result > 0:
        raise ValueError(f'{label}须大于 0，实际是 {value!r}')
    return result


def _vector(value, label, default=(0.0, 0.0, 0.0)):
    if value is None:
        return tuple(float(item) for item in default)
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f'{label}须为[x, y, z]三元数组，实际是 {value!r}')
    return tuple(_number(item, f'{label}[{index}]') for index, item in enumerate(value))


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _length(value):
    return math.sqrt(_dot(value, value))


def _normalize(value):
    length = _length(value)
    return None if length < 1e-12 else tuple(item / length for item in value)


def _clamp(value):
    return min(1.0, max(0.0, value))
