"""Exact flat storyboard edit contracts shared by tools and Operations."""
import json
from copy import deepcopy
from typing import Any

from .segment_motion import CAMERA_MOVES, CAMERA_SPEEDS
from .storyboard_camera import EASINGS, POSE_FIELDS, PRESETS
from .storyboard_layout import KINDS as LAYOUT_KINDS

_ID = {'type': 'string', 'minLength': 1}
_MS = {'type': 'integer', 'minimum': 0}
_OBJECT = {
    'name': {'type': 'string', 'minLength': 1}, 'kind': {'type': 'string', 'enum': ['person', 'prop', 'set']},
    'parent_id': {'type': ['string', 'null'], 'minLength': 1}, 'asset_id': {'type': ['string', 'null'], 'minLength': 1},
    'x': {'type': 'number', 'minimum': 0, 'maximum': 1}, 'y': {'type': 'number', 'minimum': 0, 'maximum': 1},
    'rotation': {'type': 'number'}, 'depth': {'type': 'number'}, 'elevation': {'type': 'number'}, 'height': {'type': 'number', 'exclusiveMinimum': 0}, 'state': {'type': 'object'},
}
_KEY = {key: value for key, value in _OBJECT.items() if key in ('x', 'y', 'rotation', 'depth', 'elevation', 'height', 'state')}
_KEY.update(time_ms=_MS, interpolation={'type': 'string', 'enum': ['hold', 'linear']})
_VISUAL = {'type': 'object', 'additionalProperties': False, 'required': ['kind', 'name', 'usage', 'asset_id'],
           'properties': {'kind': {'type': 'string', 'enum': ['person', 'prop', 'set']}, 'name': _ID, 'usage': {'type': 'string'},
                          'asset_id': {'type': ['string', 'null'], 'minLength': 1},
                          # 复用键：subject 指向提取/改编节点，variant 是形态名（见 ASSET_STANDARD.md）。
                          'subject': {'type': 'string'}, 'variant': {'type': 'string'}}}
_SHOT = {'sound_notes': {'type': 'string'}, 'description': {'type': 'string'}, 'shot_language': {'type': 'string'}, 'visual_elements': {'type': 'array', 'items': _VISUAL}, 'sound_intent_ids': {'type': 'array', 'items': _ID, 'uniqueItems': True}, 'title': {'type': 'string', 'minLength': 1}, 'duration_ms': {'type': 'integer', 'minimum': 1},
         'source_block_ids': {'type': 'array', 'items': _ID, 'uniqueItems': True}, 'action_start_ms': _MS,
         'duration_locked': {'type': 'boolean'}, 'action_start_marker_id': {'type': ['string', 'null']}}


def _action(op, fields, required):
    return {'type': 'object', 'additionalProperties': False,
            'properties': {'op': {'const': op, 'type': 'string'}, **deepcopy(fields)}, 'required': ['op', *required]}


_ACTIONS = [
    _action('create_shot', _SHOT, ['title']),
    _action('update_shot', {'shot_id': _ID, **_SHOT}, ['shot_id']),
    _action('move_shot', {'shot_id': _ID, 'index': _MS}, ['shot_id', 'index']),
    _action('delete_shot', {'shot_id': _ID}, ['shot_id']),
    _action('set_keyframe', {'shot_id': _ID, 'offset_ms': _MS, 'sketch_path': _ID, 'style_path': _ID}, ['shot_id', 'offset_ms']),
    _action('move_keyframe', {'shot_id': _ID, 'frame_id': _ID, 'offset_ms': _MS}, ['shot_id', 'frame_id', 'offset_ms']),
    _action('delete_keyframe', {'shot_id': _ID, 'frame_id': _ID}, ['shot_id', 'frame_id']),
    _action('set_approved', {'approved': {'type': 'boolean'}}, ['approved']),
    _action('set_fps', {'fps': {'type': 'integer', 'minimum': 1, 'maximum': 120}}, ['fps']),
    _action('create_object', _OBJECT, ['name', 'kind']),
    _action('update_object', {'object_id': _ID, **_OBJECT}, ['object_id']),
    _action('delete_object', {'object_id': _ID}, ['object_id']),
    _action('set_action_keyframe', {'object_id': _ID, **_KEY}, ['object_id', 'time_ms']),
    _action('move_action_keyframe', {'object_id': _ID, 'keyframe_id': _ID, 'time_ms': _MS}, ['object_id', 'keyframe_id', 'time_ms']),
    _action('delete_action_keyframe', {'object_id': _ID, 'keyframe_id': _ID}, ['object_id', 'keyframe_id']),
]
_POSE: dict[str, Any] = {field: {'type': 'number'} for field in POSE_FIELDS}
_POSE['fov_deg'] = {'type': 'number', 'minimum': 1, 'exclusiveMaximum': 179}
_POSE['focus_distance'] = {'type': 'number', 'exclusiveMinimum': 0}
_EASING = {'type': 'string', 'enum': list(EASINGS)}
_HANDHELD = {'type': ['object', 'null'], 'additionalProperties': False, 'required': ['amplitude', 'frequency_hz', 'seed'],
             'properties': {'amplitude': {'type': 'number', 'minimum': 0, 'maximum': .05},
                            'frequency_hz': {'type': 'number', 'minimum': .1, 'maximum': 20},
                            'seed': {'type': 'integer', 'minimum': 0, 'maximum': 2147483647}}}
_POSE['pitch'] = {'type': 'number', 'minimum': -90, 'maximum': 90}
_NULL_ID = {'type': ['string', 'null'], 'minLength': 1}
_ACTIONS.extend([
    _action('create_marker', {'name': _ID, 'time_ms': _MS}, ['name', 'time_ms']),
    _action('update_marker', {'marker_id': _ID, 'name': _ID, 'time_ms': _MS}, ['marker_id']),
    _action('delete_marker', {'marker_id': _ID}, ['marker_id']),
    _action('bind_keyframe_marker', {'shot_id': _ID, 'frame_id': _ID, 'marker_id': _NULL_ID}, ['shot_id', 'frame_id', 'marker_id']),
    _action('bind_action_keyframe_marker', {'object_id': _ID, 'keyframe_id': _ID, 'marker_id': _NULL_ID}, ['object_id', 'keyframe_id', 'marker_id']),
    _action('set_camera_keyframe', {'shot_id': _ID, 'offset_ms': _MS, **_POSE, 'interpolation': {'enum': ['hold', 'linear'], 'type': 'string'}, 'easing': _EASING}, ['shot_id', 'offset_ms']),
    _action('move_camera_keyframe', {'shot_id': _ID, 'keyframe_id': _ID, 'offset_ms': _MS}, ['shot_id', 'keyframe_id', 'offset_ms']),
    _action('delete_camera_keyframe', {'shot_id': _ID, 'keyframe_id': _ID}, ['shot_id', 'keyframe_id']),
    _action('set_camera_overlays', {'shot_id': _ID, 'handheld': _HANDHELD, 'focus_target_object_id': _NULL_ID}, ['shot_id']),
    _action('set_camera_preset', {'shot_id': _ID, 'preset': {'type': 'string', 'enum': list(PRESETS)}, 'target_object_id': _NULL_ID,
                                'start_ms': _MS, 'end_ms': _MS, 'amount': {'type': 'number'}, 'easing': _EASING, 'handheld': _HANDHELD,
                                'start': {'type': 'object', 'additionalProperties': False, 'properties': _POSE},
                                'end': {'type': 'object', 'additionalProperties': False, 'properties': _POSE}}, ['shot_id', 'preset']),
])
_INTENT = {'source_block_id': {'type': ['string', 'null'], 'minLength': 1}, 'kind': {'type': 'string', 'enum': ['dialogue', 'effect', 'silence']}, 'speaker': {'type': 'string'}, 'text': {'type': 'string'}}
_ACTIONS.extend([_action('create_sound_intent', _INTENT, ['kind', 'text']), _action('update_sound_intent', {'intent_id': _ID, **_INTENT}, ['intent_id']), _action('delete_sound_intent', {'intent_id': _ID}, ['intent_id'])])
_SOUND = {'intent_id': {'type': ['string', 'null'], 'minLength': 1}, 'kind': {'type': 'string', 'enum': ['dialogue', 'effect', 'silence']}, 'label': {'type': 'string'},
          'time_ms': _MS, 'marker_id': _ID, 'duration_ms': {'type': 'integer', 'minimum': 1}, 'source_in_ms': _MS, 'audio_path': _ID}
_ACTIONS.extend([
    _action('create_sound', {'shot_id': _ID, **_SOUND}, ['shot_id', 'kind']),
    _action('update_sound', {'shot_id': _ID, 'sound_id': _ID, **_SOUND}, ['shot_id', 'sound_id']),
    _action('delete_sound', {'shot_id': _ID, 'sound_id': _ID}, ['shot_id', 'sound_id']),
])
_PANEL = {'intent': {'type': 'string'}, 'offset_ms': _MS, 'frame_id': _NULL_ID,
          'base': {'type': 'object'}, 'marks': {'type': 'array', 'items': {'type': 'object'}}}
_ACTIONS.extend([
    _action('create_panel', {'shot_id': _ID, **_PANEL}, ['shot_id']),
    _action('update_panel', {'shot_id': _ID, 'panel_id': _ID, **_PANEL}, ['shot_id', 'panel_id']),
    _action('use_panel_frame', {'shot_id': _ID, 'panel_id': _ID}, ['shot_id', 'panel_id']),
    _action('delete_panel', {'shot_id': _ID, 'panel_id': _ID}, ['shot_id', 'panel_id']),
    _action('add_panel_candidate', {'shot_id': _ID, 'panel_id': _ID, 'image_path': _ID}, ['shot_id', 'panel_id', 'image_path']),
    _action('adopt_panel_candidate', {'shot_id': _ID, 'panel_id': _ID, 'candidate_id': _NULL_ID}, ['shot_id', 'panel_id', 'candidate_id']),
])
# B0 资产需求：需求（想要什么）与引用（visual_elements 的 asset_id）分开存，
# 所以需求自己一组动作，永远不改引用。
_NEED = {'type': 'object', 'additionalProperties': False, 'required': ['kind', 'name'],
         'properties': {'kind': {'type': 'string', 'enum': ['person', 'prop', 'location', 'material']},
                        'name': _ID, 'variant': {'type': 'string'}, 'note': {'type': 'string'}}}
_ACTIONS.extend([
    _action('set_asset_needs', {'needs': {'type': 'array', 'items': _NEED}}, ['needs']),
    _action('set_shot_asset_needs', {'shot_id': _ID, 'needs': {'type': 'array', 'items': _NEED}}, ['shot_id', 'needs']),
])
# B1a 每镜内部时间轴：offset_ms 一律相对镜首；segments 只在显式覆盖时才落盘。
_PLAN = {'offset_ms': _MS, 'moment_name': {'type': 'string'}, 'description': {'type': 'string'}}
_ACTIONS.extend([
    _action('add_timeline_keyframe', {'shot_id': _ID, **_PLAN}, ['shot_id', 'offset_ms']),
    _action('update_timeline_keyframe', {'shot_id': _ID, 'new_offset_ms': _MS, **_PLAN}, ['shot_id', 'offset_ms']),
    _action('delete_timeline_keyframe', {'shot_id': _ID, 'offset_ms': _MS}, ['shot_id', 'offset_ms']),
    _action('set_timeline_segments', {'shot_id': _ID, 'segments': {'type': 'array', 'items': {'type': 'array', 'items': _MS, 'minItems': 2, 'maxItems': 2}}},
            ['shot_id', 'segments']),
    # B1c 跨镜衔接：from_shot 为 null 表示取消衔接。
    _action('set_shot_handoff', {'shot_id': _ID, 'from_shot': _NULL_ID, 'ref_frame': {'type': 'string', 'enum': ['last', 'first']}},
            ['shot_id', 'from_shot']),
])
# B2 框图：挂在已有关键帧上的结构化构图（归一化 bbox＋机位＋景深），
# 形状与校验都在 storyboard_layout；这里只发布给模型看的闭集形状。
_LAYOUT_CAMERA = {'type': 'object', 'additionalProperties': False,
                  'properties': {'x': {'type': 'number'}, 'y': {'type': 'number'}, 'z': {'type': 'number'},
                                 'yaw': {'type': 'number'}, 'pitch': {'type': 'number', 'minimum': -90, 'maximum': 90},
                                 'lens_mm': {'type': 'number', 'exclusiveMinimum': 0},
                                 'framing': {'type': 'string', 'minLength': 1}}}
_LAYOUT_OBJECT = {'type': 'object', 'additionalProperties': False, 'required': ['asset_id', 'bbox', 'depth', 'kind'],
                  'properties': {'asset_id': _ID,
                                 'bbox': {'type': 'array', 'minItems': 4, 'maxItems': 4,
                                          'items': {'type': 'number', 'minimum': 0, 'maximum': 1}},
                                 'depth': {'type': 'number'},
                                 'kind': {'type': 'string', 'enum': list(LAYOUT_KINDS)}}}
_LAYOUT = {'type': ['object', 'null'],
           'properties': {'shot_id': _ID, 'offset_ms': _MS, 'camera': _LAYOUT_CAMERA,
                          'objects': {'type': 'array', 'items': _LAYOUT_OBJECT},
                          'unmet_needs': {'type': 'array', 'items': _NEED},
                          'source': {'type': 'string', 'enum': ['previs', 'manual', 'subjects']},
                          'hash': {'type': 'string'}},
           'description': 'layout=null 清除该帧框图。bbox 为归一化画面坐标[x0,y0,x1,y1]（0..1，原点左上，须x0<x1、y0<y1）；depth 为相对镜头的景深（米）；hash 由程序重算，不用填'}
_ACTIONS.extend([
    _action('set_keyframe_layout', {'shot_id': _ID, 'offset_ms': _MS, 'layout': _LAYOUT},
            ['shot_id', 'offset_ms', 'layout']),
    _action('clear_keyframe_layouts', {'shot_id': _ID}, ['shot_id']),
])
# B2c 站位：这一帧谁站在场景哪个点（投影的输入，不是结果）。x/z 为场景平面米数
# （y 向上、x/z 为地面，与 .director 同一原点），y 为脚下高度，height 缺省按成人，
# facing 为朝向角（度，0＝面向 -z），kind 与视觉元素同一套三分类。
_SUBJECT = {'type': 'object', 'additionalProperties': False, 'required': ['asset_id', 'x', 'z'],
            'properties': {'asset_id': _ID, 'x': {'type': 'number'}, 'z': {'type': 'number'},
                           'y': {'type': 'number'},
                           'height': {'type': 'number', 'exclusiveMinimum': 0},
                           'facing': {'type': 'number'},
                           'kind': {'type': 'string', 'enum': ['person', 'prop', 'set']}}}
_ACTIONS.append(_action('set_keyframe_subjects', {
    'shot_id': _ID, 'offset_ms': _MS,
    'subjects': {'type': 'array', 'items': _SUBJECT,
                 'description': '整体替换这一帧的站位；空数组清空。站位不动框图，框图由derive_keyframe_layout从站位推'}},
    ['shot_id', 'offset_ms', 'subjects']))
# B2b 分段运镜：运动在段之间，只发布人给的那两项（kind/speed）与一句备注；
# 主体位移、方向、景别差与机位位移全部由前后两帧框图现算，不接受模型填数字。
_SEGMENT_MOVE = {'type': 'object', 'additionalProperties': False,
                 'properties': {'kind': {'type': 'string', 'enum': list(CAMERA_MOVES)},
                                'speed': {'type': 'string', 'enum': list(CAMERA_SPEEDS)}},
                 'description': '运镜种类与速度都可省；都不给＝这一段按框图推导，不声明运镜'}
_ACTIONS.append(_action('set_segment_motion', {
    'shot_id': _ID, 'from_ms': _MS, 'to_ms': _MS,
    'camera_move': _SEGMENT_MOVE | {'type': ['object', 'null'],
                                    'description': '传 null 或空对象＝取消这一段的运镜标注'},
    'notes': {'type': 'string', 'description': '人工备注，进视频提示词的「环境」一段'}},
    ['shot_id', 'from_ms', 'to_ms']))
for _schema in _ACTIONS:
    if _schema['properties']['op']['const'] == 'set_keyframe':
        _schema['oneOf'] = [{'required': ['sketch_path'], 'not': {'required': ['style_path']}},
                            {'required': ['style_path'], 'not': {'required': ['sketch_path']}}]
ACTION_SCHEMAS = {row['properties']['op']['const']: row for row in _ACTIONS}
ACTION_FIELDS = {op: set(schema['properties']) - {'op'} for op, schema in ACTION_SCHEMAS.items()}


def storyboard_change_schema():
    """Schema for a single changes item; callers supply their array wrapper."""
    return {'type': 'object', 'oneOf': deepcopy(_ACTIONS), 'description': '每项是扁平动作对象，op必填；字段直接放在动作上，不使用keyframe等嵌套包装。'}


def validate_action_shape(change):
    op = change.get('op') if isinstance(change, dict) else None
    if not isinstance(op, str) or op not in ACTION_SCHEMAS:
        raise ValueError('分镜操作op无效；允许：' + '、'.join(ACTION_SCHEMAS))
    schema = ACTION_SCHEMAS[op]
    extras = set(change) - set(schema['properties'])
    missing = set(schema['required']) - set(change)
    paths_invalid = op == 'set_keyframe' and (('sketch_path' in change) == ('style_path' in change))
    if extras or missing or paths_invalid:
        example: dict[str, Any] = {'op': op}
        for key in schema['required'][1:]:
            field = schema['properties'][key]
            example[key] = field['enum'][0] if 'enum' in field else True if field.get('type') == 'boolean' else max(field.get('minimum', 0), 0) if field.get('type') == 'integer' else key.replace('_id', '-1')
        if op == 'set_keyframe':
            example['sketch_path'] = 'C:/images/sketch.png'
        if op == 'set_keyframe_layout':
            # layout 是嵌套对象，通用示例会把它填成字符串；给一个能直接照着写的空框图。
            example['layout'] = {'camera': {'framing': 'medium'}, 'objects': [], 'unmet_needs': []}
        problems = []
        if extras:
            problems.append('多余字段：' + '、'.join(sorted(extras)))
        if missing:
            problems.append('缺少字段：' + '、'.join(sorted(missing)))
        if paths_invalid:
            problems.append('sketch_path与style_path必须且只能提供一个')
        raise ValueError(f"分镜动作{op}参数无效（{'；'.join(problems)}）。允许字段：{'、'.join(schema['properties'])}；扁平示例：{json.dumps(example, ensure_ascii=False)}")
    return op
