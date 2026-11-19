"""框图（previs frame diagram）：一帧里「谁站在画面哪一块、机位在哪」的结构化记录。

为什么单独一层：草图与风格图是像素，模型和界面都没法据此核对「这一帧该有什么」。
框图把主体框、景深、机位写成数字，能同时供两处使用——

* 出图依据：``_storyboard_sketch_basis`` 的 ``frame_diagram`` 用本模块的哈希，
  框图一改，已生成的帧才会被判「依据已变化」（见 B3 的稳定性合同）；
* 读视图：随 ``shot.timeline.keyframes[i].layout`` 一起返回，界面据此画近似 previs。

坐标约定（务必按这个读，别自己再定一套）：

* ``objects[].bbox`` 是**归一化画面坐标** ``[x0, y0, x1, y1]``：原点在画面左上角，
  x 向右、y 向下，四个值都落在 0..1，且必须 ``x0 < x1``、``y0 < y1``。
  归一化是为了换画幅不用重算；像素换算（乘以实际宽高）交给成像端。
* ``depth`` 是相对本镜镜头的景深（米），越大越远；只用于排序与遮挡提示，不参与成像。
* ``camera`` 是机位：``x``/``y``/``z`` 为世界坐标（米，y 向上），``yaw``/``pitch`` 为度，
  ``lens_mm`` 为等效焦距，``framing`` 是景别标签。这里刻意不换算 hFOV：
  换算要传感器尺寸，那是成像端的事，框图只存写下来的数。
* ``unmet_needs`` 沿用需求层（person/prop/location/material）：这一帧还缺哪些资产。

存哪：挂在该镜 ``shot['layouts'][str(offset_ms)]``。键是镜内时刻（相对镜首），必须
是该镜时间轴上已存在的关键帧——框图没有独立的时间轴，它就是那一帧的一部分。
因此本模块只管数据形状与哈希，不谈「哪个时刻合法」：那要问时间轴（见
``storyboard_operations.validate_layouts``）。
"""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy

from .asset_needs import NEED_KINDS, validate_needs

# 框图的全部字段。sharing/source/hash 都由程序补，模型只写 camera/objects/unmet_needs。
FIELDS = ('shot_id', 'offset_ms', 'camera', 'objects', 'unmet_needs', 'source', 'hash')
CAMERA_FIELDS = ('x', 'y', 'z', 'yaw', 'pitch', 'lens_mm', 'framing')
OBJECT_FIELDS = ('asset_id', 'bbox', 'depth', 'kind')
SOURCES = ('previs', 'manual')
KINDS = NEED_KINDS
CAMERA_DEFAULTS = {'x': 0.0, 'y': 1.6, 'z': 0.0, 'yaw': 0.0, 'pitch': 0.0, 'lens_mm': 35.0, 'framing': 'medium'}
# 哈希只算前 16 位：够区分，又不至于让读视图和错误消息被长串十六进制淹掉。
HASH_CHARS = 16


def _number(value, label, *, minimum=None, maximum=None):
    """数值一律转 float：int 0 与 float 0.0 的 JSON 文本不同，混着存会算出两个哈希。"""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{label}必须是有限数值')
    result = float(value)
    if minimum is not None and result < minimum or maximum is not None and result > maximum:
        raise ValueError(f'{label}须在{minimum if minimum is not None else "-∞"}..'
                         f'{maximum if maximum is not None else "+∞"}之间')
    return result


def _offset(value, label='框图所在的镜内时刻'):
    if type(value) is not int or value < 0:
        raise ValueError(f'{label}必须是大于等于 0 的整数毫秒')
    return value


def _camera(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('框图机位须为对象')
    extra = set(raw) - set(CAMERA_FIELDS)
    if extra:
        raise ValueError('框图机位有不认识的字段：' + '、'.join(sorted(extra))
                         + '；允许：' + '、'.join(CAMERA_FIELDS))
    result = {}
    for field, default in CAMERA_DEFAULTS.items():
        value = raw.get(field, default)
        if field == 'framing':
            if not isinstance(value, str) or not value.strip():
                raise ValueError('框图景别须为非空文本，例如 wide/medium/close')
            result[field] = value.strip()
            continue
        limits = {'lens_mm': (1.0, None), 'pitch': (-90.0, 90.0)}.get(field, (None, None))
        result[field] = _number(value, f'机位{field}', minimum=limits[0], maximum=limits[1])
    return result


def _object(row, index):
    label = f'框图对象第{index + 1}项'
    if not isinstance(row, dict):
        raise ValueError(f'{label}须为对象')
    extra = set(row) - set(OBJECT_FIELDS)
    if extra:
        raise ValueError(f'{label}有不认识的字段：' + '、'.join(sorted(extra))
                         + '；允许：' + '、'.join(OBJECT_FIELDS))
    identifier = row.get('asset_id')
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError(f'{label}需要asset_id（已绑定的资产卡编号）')
    kind = row.get('kind')
    if kind not in KINDS:
        raise ValueError(f'{label}的kind须为' + '、'.join(KINDS))
    box = row.get('bbox')
    if not isinstance(box, list) or len(box) != 4:
        raise ValueError(f'{label}的bbox须为归一化画面坐标[x0,y0,x1,y1]四元素数组')
    x0, y0, x1, y1 = (_number(value, f'{label}的bbox[{position}]', minimum=0.0, maximum=1.0)
                      for position, value in enumerate(box))
    if not x0 < x1 or not y0 < y1:
        raise ValueError(f'{label}的bbox须满足x0<x1且y0<y1（原点在画面左上，y向下）')
    if 'depth' not in row:
        raise ValueError(f'{label}需要depth（相对镜头的景深，米）')
    return {'asset_id': identifier.strip(), 'bbox': [x0, y0, x1, y1],
            'depth': _number(row['depth'], f'{label}的depth'), 'kind': kind}


def body(layout):
    """哈希的输入＝除 ``hash`` 以外的全部字段；哈希不能把自己算进去。"""
    return {key: value for key, value in layout.items() if key != 'hash'}


def layout_hash(layout):
    """稳定序列化后的 sha256 前 16 位：同内容同哈希，改一点即变。"""
    text = json.dumps(body(layout), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:HASH_CHARS]


def normalize_layout(raw, *, shot_id=None, offset_ms=None):
    """校验框图并补齐默认值；``hash`` 一律按内容重算。

    重算而不核对传入的 ``hash``：暂存修补（patch_content_submission）改的是内容，
    不会顺手改哈希；这时报「哈希不匹配」只会让人多改一处，而不是更安全。
    真正要防的是「存进去的框图和它的哈希不一致」，那由 ``validate_layouts`` 在入库前查。
    """
    if not isinstance(raw, dict):
        raise ValueError('框图须为对象')
    extra = set(raw) - set(FIELDS)
    if extra:
        raise ValueError('框图有不认识的字段：' + '、'.join(sorted(extra))
                         + '；允许：' + '、'.join(FIELDS))
    declared_shot = raw.get('shot_id')
    if declared_shot is not None and (not isinstance(declared_shot, str) or not declared_shot.strip()):
        raise ValueError('框图的shot_id须为非空文本')
    if shot_id is None:
        shot_id = declared_shot
    elif declared_shot is not None and declared_shot != shot_id:
        raise ValueError('框图的shot_id与目标镜头不一致')
    if shot_id is None:
        raise ValueError('框图需要shot_id（所属镜头编号）')
    declared_offset = raw.get('offset_ms')
    if declared_offset is not None:
        offset_ms = _offset(declared_offset)
    if offset_ms is None:
        raise ValueError('框图需要offset_ms（镜内时刻）')
    offset_ms = _offset(offset_ms)
    source = raw.get('source', 'manual')
    if source not in SOURCES:
        raise ValueError('框图来源须为' + '、'.join(SOURCES))
    objects = raw.get('objects')
    if objects is None:
        objects = []
    if not isinstance(objects, list):
        raise ValueError('框图对象须为列表')
    result = {'shot_id': shot_id, 'offset_ms': offset_ms, 'camera': _camera(raw.get('camera')),
              'objects': [_object(row, index) for index, row in enumerate(objects)],
              'unmet_needs': validate_needs(raw.get('unmet_needs') or [], ('unmet_needs',)),
              'source': source}
    result['hash'] = layout_hash(result)
    return result


def layout_at(shot, offset_ms):
    """取该时刻已存的框图（深拷贝，调用方改不到存储）；没有就是 None。"""
    layouts = (shot or {}).get('layouts')
    if not isinstance(layouts, dict):
        return None
    row = layouts.get(str(offset_ms))
    if row is None:
        row = layouts.get(offset_ms)
    return deepcopy(row) if isinstance(row, dict) else None


def _ordered(layouts):
    """键按镜内时刻排序：存储里的 JSON 因此稳定，diff 与测试都好读。"""
    return {key: layouts[key] for key in sorted(layouts, key=lambda item: int(item))}


def store_layout(shot, layout):
    """整体写入该时刻的框图（覆盖旧值）。"""
    layouts = shot.get('layouts') if isinstance(shot.get('layouts'), dict) else {}
    layouts[str(layout['offset_ms'])] = deepcopy(layout)
    shot['layouts'] = _ordered(layouts)


def drop_layout(shot, offset_ms):
    """删掉该时刻的框图；没有就什么都不做。"""
    layouts = shot.get('layouts')
    if not isinstance(layouts, dict):
        return
    layouts.pop(str(offset_ms), None)
    if layouts:
        shot['layouts'] = _ordered(layouts)
    else:
        shot.pop('layouts')


def move_layout(shot, old_offset_ms, new_offset_ms):
    """关键帧改时刻时框图跟着走：框图不单独存在，它就是那一帧的一部分。"""
    layout = layout_at(shot, old_offset_ms)
    if layout is None:
        return
    drop_layout(shot, old_offset_ms)
    layout['offset_ms'] = _offset(new_offset_ms)
    layout['hash'] = layout_hash(layout)
    store_layout(shot, layout)


def validate_layouts(board):
    """入库前查存储不变量：键必须是已有关键帧，内容必须仍是自己的规范化结果。

    这里只查「存坏了」，不查创作意图；哪一帧该有框图是用户的事。
    """
    for shot in board.get('shots') or []:
        layouts = shot.get('layouts')
        if layouts is None:
            continue
        if not isinstance(layouts, dict):
            raise ValueError(f'镜头{shot["id"]}的框图须以镜内时刻为键存成对象')
        timeline = shot.get('timeline') if isinstance(shot.get('timeline'), dict) else {}
        anchors = {row['offset_ms'] for row in timeline.get('keyframes') or []}
        # 没有显式计划时，仍存的关键帧本身就是锚点（老场次只有静帧）。
        anchors |= {frame['offset_ms'] for frame in shot.get('keyframes') or []}
        for key, layout in layouts.items():
            if not isinstance(key, str) or not key.isdigit() or int(key) not in anchors:
                raise ValueError(f'框图的时刻{key}ms不在镜头{shot["id"]}的关键帧上；'
                                 '先建立该时刻的关键帧，或清掉这条框图')
            if normalize_layout(layout, shot_id=shot['id'], offset_ms=int(key)) != layout:
                raise ValueError(f'框图{shot["id"]}@{key}ms的内容与哈希不一致，请重新写入')
