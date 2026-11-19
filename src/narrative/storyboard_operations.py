"""Scene-local storyboard editing through an atomic, revision-checked operation."""
from __future__ import annotations

import json
import shutil
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image

from .asset_needs import gap_view, merge_needs
from .asset_needs import validate_needs as validate_asset_needs
from .content_errors import (
    ContentConflictError,
    ContentGuardError,
    ContentValidationError,
    content_item,
    content_write,
)
from .graph_store import GraphStore, active_connection, on_rollback
from .previs_tools import StoryboardPrevisMixin
from .project_package import layout
from .readable_ids import allocate_id
from .segment_motion import (
    DEFAULT_MAX_SEGMENT_MS,
    derive_segment,
    derive_segments,
    segment_prompt,
    segment_views,
    segments_of,
    validate_segment,
    validate_segment_motions,
)
from .storyboard_art import StoryboardArtMixin, changed_basis_keys
from .storyboard_assets import StoryboardAssetsMixin, element_views, validate_elements
from .storyboard_audio import (
    AUDIO_FIELDS,
    apply_audio,
    audio_conflicts,
    audio_paths,
    sound_time,
    validate_audio,
)
from .storyboard_camera import (
    CAMERA_FIELDS,
    apply_camera,
    resolve_markers,
    timing_conflicts,
    validate_camera,
)
from .storyboard_contract import ACTION_FIELDS, validate_action_shape
from .storyboard_layout import (
    drop_layout,
    layout_at,
    move_layout,
    normalize_layout,
    normalize_subjects,
    store_layout,
    validate_layouts,
)
from .storyboard_panels import StoryboardPanelsMixin, apply_panel, panel_paths, validate_panels
from .storyboard_script import apply_intent, script_fields, validate_script
from .storyboard_timing import STAGING_FIELDS, apply_staging, preview, validate_timing
from .storyboard_video import StoryboardVideoMixin, media_paths, validate_media

# 每镜内部时间轴与跨镜衔接：offset_ms 一律相对镜首，躲开「剪辑时间／镜内时间」再混一层。
TIMELINE_FIELDS = {
    'add_timeline_keyframe': {'shot_id', 'offset_ms', 'moment_name', 'description'},
    'update_timeline_keyframe': {'shot_id', 'offset_ms', 'new_offset_ms', 'moment_name', 'description'},
    'delete_timeline_keyframe': {'shot_id', 'offset_ms'},
    'set_timeline_segments': {'shot_id', 'segments'},
    'set_shot_handoff': {'shot_id', 'from_shot', 'ref_frame'},
}
HANDOFF_FIELDS = {'from_shot', 'ref_frame'}
REF_FRAMES = ('last', 'first')
# B2 框图：整体写入／清除，挂在该镜已有关键帧的时刻上（见 storyboard_layout）。
LAYOUT_FIELDS = {
    'set_keyframe_layout': {'shot_id', 'offset_ms', 'layout'},
    'clear_keyframe_layouts': {'shot_id'},
}
NEED_FIELDS = {'set_asset_needs': {'needs'}, 'set_shot_asset_needs': {'shot_id', 'needs'}}
# 站位：整体替换某一帧的主体位置（投影的输入）。它不动框图——框图是结果，
# 由 derive_keyframe_layout 从站位现算，两者各存一份、互不覆盖。
SUBJECT_FIELDS = {'set_keyframe_subjects': {'shot_id', 'offset_ms', 'subjects'}}
# 分段运镜：只存人给的那几个字段（kind/speed/notes）。主体位移、景别差与机位位移都由前后
# 两帧的框图现算（见 segment_motion），落库就等于存了第二份会过期的真相。
MOTION_FIELDS = {'set_segment_motion': {'shot_id', 'from_ms', 'to_ms', 'camera_move', 'notes'}}


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label}必须是大于等于 {minimum} 的整数")
    return value


def _image_path(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("关键帧需要本地图片的完整路径")
    path = Path(value).resolve()
    if not path.is_file() or path.stat().st_size == 0 or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        raise ValueError("关键帧图片不存在或格式不受支持")
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, SyntaxError, ValueError) as error:
        raise ValueError("关键帧文件无法作为图片读取") from error
    return str(path)


def _frame_paths(board):
    for shot in board["shots"]:
        for frame in shot["keyframes"]:
            for field in ("sketch_path", "style_path"):
                if frame.get(field):
                    yield frame, field


def _media_paths(board):
    yield from panel_paths(board)
    yield from _frame_paths(board)
    yield from audio_paths(board)
    yield from media_paths(board)


def _load(db, unit_id, root):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='script_units'").fetchone() or not db.execute(
        "SELECT 1 FROM script_units WHERE id=?", (unit_id,)
    ).fetchone():
        raise ContentGuardError("请先选择本工程的场次")
    row = db.execute("SELECT value FROM meta WHERE key=?", ("storyboard:" + unit_id,)).fetchone()
    board = json.loads(row[0]) if row else {"unit_id": unit_id, "revision": 0, "approved": False, "shots": [], "fps": 24, "objects": [], "markers": [], "asset_needs": []}

    for frame, field in _media_paths(board):
        frame[field] = str(root / frame[field])
    return board


def _shot_fields(change, previous, block_ids):
    title = change.get("title", previous.get("title"))
    if not isinstance(title, str) or not title.strip():
        raise ValueError("请填写镜头内容")
    duration = _integer(change.get("duration_ms", previous.get("duration_ms", 3000)), "镜头时长", 1)
    refs = change.get("source_block_ids", previous.get("source_block_ids", []))
    if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in block_ids for ref in refs) or len(set(refs)) != len(refs):
        raise ValueError("正文依据须为本场有效且无重复的正文块")
    action_start = _integer(change.get("action_start_ms", previous.get("action_start_ms", 0)), "动作起点")
    return {**script_fields(change, previous), "title": title.strip(), "duration_ms": duration, "source_block_ids": refs, "action_start_ms": action_start,
            "action_start_marker_id": change.get("action_start_marker_id", previous.get("action_start_marker_id")),
            "duration_locked": change.get("duration_locked", previous.get("duration_locked", False))}


def _approve(board, value, block_ids):
    if type(value) is not bool:
        raise ValueError("定案状态须为布尔值")
    if value:
        if not board["shots"]:
            raise ValueError("空轨道无法定案")
        for shot in board["shots"]:
            if any(ref not in block_ids for ref in shot["source_block_ids"]):
                raise ValueError("请先重新定位失效的正文依据")
            if not shot["keyframes"] or shot["keyframes"][0]["offset_ms"] != 0:
                raise ValueError("定案前每个镜头都需要起始草图")
            for frame in shot["keyframes"]:
                _image_path(frame["sketch_path"])
    board["approved"] = value


def _set_frame(db, shot, change, approved):
    offset = _integer(change.get("offset_ms"), "镜内位置")
    if offset >= shot["duration_ms"]:
        raise ValueError("关键帧位置须在镜头时长内")
    frame = next((f for f in shot["keyframes"] if f["offset_ms"] == offset), None)
    if "style_path" in change:
        if "sketch_path" in change or not approved or not frame:
            raise ValueError("风格图须绑定定案方案中已有的草图")
        frame["style_path"] = _image_path(change["style_path"])
    else:
        if approved:
            raise ContentGuardError("请先解除定案再编辑草图")
        path = _image_path(change.get("sketch_path"))
        if frame is None:
            frame = {"id": allocate_id(db, "storyboard", "frame-"), "offset_ms": offset}
            shot["keyframes"].append(frame)
            shot["keyframes"].sort(key=lambda f: f["offset_ms"])
        frame.pop("sketch_source", None)
        frame.pop("sketch_basis_hash", None)
        frame.update(sketch_path=path, style_path="")


_FIELDS = ACTION_FIELDS


def _shot_in_scene(board, identifier):
    shot = next((row for row in board['shots'] if row['id'] == identifier), None)
    if shot is None:
        raise ContentGuardError('指定的镜头不属于本场')
    return shot


def _timeline_plan(shot):
    """镜内时间轴只存「显式写过的东西」：关键帧计划与分段。

    duration_ms、声音节拍与摄影机关键点都能从既有数据派生（见 timeline_view）。
    再存一份就会有两套时长，缩镜头时必然打架，所以这里不落盘。
    """
    timeline = shot.setdefault('timeline', {})
    timeline.setdefault('keyframes', [])
    return timeline


def _plan_row(shot, offset_ms):
    offset = _integer(offset_ms, '关键帧计划位置')
    row = next((item for item in (shot.get('timeline') or {}).get('keyframes') or [] if item['offset_ms'] == offset), None)
    if row is None:
        raise ValueError('此镜没有该时刻的计划关键帧；先 add_timeline_keyframe 再改')
    return row


def apply_timeline(board, change):
    """镜内时间轴与跨镜衔接的编辑；只写用户显式给的东西，其余交给推导。"""
    shot = _shot_in_scene(board, change.get('shot_id'))
    op = change['op']
    if op == 'set_shot_handoff':
        source = change.get('from_shot')
        if source is None:
            shot.pop('handoff', None)
            return
        shot['handoff'] = {'from_shot': source, 'ref_frame': change.get('ref_frame', 'last')}
        return
    timeline = _timeline_plan(shot)
    if op == 'add_timeline_keyframe':
        offset = _integer(change.get('offset_ms'), '关键帧计划位置')
        if any(row['offset_ms'] == offset for row in timeline['keyframes']):
            raise ValueError('该时刻已有计划关键帧；改命名或描述用 update_timeline_keyframe')
        timeline['keyframes'].append({'offset_ms': offset, 'moment_name': change.get('moment_name', ''),
                                      'description': change.get('description', '')})
        timeline['keyframes'].sort(key=lambda row: row['offset_ms'])
        return
    if op == 'delete_timeline_keyframe':
        row = _plan_row(shot, change.get('offset_ms'))
        timeline['keyframes'].remove(row)
        # 框图不独立存在：关键帧没了，它的框图一起走，免得留一帧谁也读不到的孤儿数据。
        drop_layout(shot, row['offset_ms'])
        return
    if op == 'set_timeline_segments':
        segments = change.get('segments')
        if not isinstance(segments, list):
            raise ValueError('分段须为[a,b]区间列表；空列表表示回到关键帧推导')
        for row in segments:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError('分段须为[a,b]两元素区间')
        if segments:
            timeline['segments'] = [[row[0], row[1]] for row in segments]
        else:
            # 显式清空＝交还给推导，而不是「存了一张空分段表」（那等于整镜没有时间轴）。
            timeline.pop('segments', None)
        return
    row = _plan_row(shot, change.get('offset_ms'))
    if 'moment_name' in change:
        row['moment_name'] = change['moment_name']
    if 'description' in change:
        row['description'] = change['description']
    if 'new_offset_ms' in change:
        offset = _integer(change['new_offset_ms'], '关键帧计划位置')
        if any(item is not row and item['offset_ms'] == offset for item in timeline['keyframes']):
            raise ValueError('该时刻已有计划关键帧，不能把两条计划并成一条')
        previous = row['offset_ms']
        row['offset_ms'] = offset
        timeline['keyframes'].sort(key=lambda item: item['offset_ms'])
        # 框图跟着它的关键帧走，否则改个时刻就把构图丢了。
        move_layout(shot, previous, offset)


def apply_layout(board, change):
    """框图的整体写入／清除。

    框图是「那一帧的构图」，不是独立对象：时刻必须已经是该镜时间轴上的关键帧，
    否则它既不会出现在读视图里，也不会进任何一帧的出图依据——写进去就是坏数据。
    """
    shot = _shot_in_scene(board, change.get('shot_id'))
    if change['op'] == 'clear_keyframe_layouts':
        shot.pop('layouts', None)
        return
    offset = _integer(change.get('offset_ms'), '框图所在的镜内时刻')
    timeline = shot.get('timeline') if isinstance(shot.get('timeline'), dict) else {}
    frames = {row['offset_ms'] for row in timeline.get('keyframes') or []}
    frames |= {frame['offset_ms'] for frame in shot.get('keyframes') or []}
    if offset not in frames:
        raise ValueError('框图必须挂在该镜已存在的关键帧上；先用 add_timeline_keyframe 或 set_keyframe 建立该时刻')
    layout = change.get('layout')
    if layout is None:
        drop_layout(shot, offset)
        return
    store_layout(shot, normalize_layout(layout, shot_id=shot['id'], offset_ms=offset))


def apply_subjects(board, change, room=None):
    """站位整体替换：只写「这一帧谁站在哪」，不碰框图。

    ``room`` 是本场房间尺寸（``{width, depth, height}``）；``None`` 表示房间未知，
    这时不查越界（见 ``storyboard_layout._check_room``）。

    为什么必须落在**显式**的时间轴关键帧上：站位存在计划行里，而读视图在没有计划时会
    从静帧重新推导计划行——写在推导行上的站位下一秒就没了，等于存了一条读不到的数据。
    只有静帧的场次，先 ``add_timeline_keyframe`` 建立这一帧。
    """
    shot = _shot_in_scene(board, change.get('shot_id'))
    offset = _integer(change.get('offset_ms'), '站位所在的镜内时刻')
    timeline = shot.get('timeline') if isinstance(shot.get('timeline'), dict) else {}
    row = next((item for item in timeline.get('keyframes') or [] if item.get('offset_ms') == offset), None)
    if row is None:
        stills = {frame['offset_ms'] for frame in shot.get('keyframes') or []}
        hint = '该时刻只有静帧、还没有时间轴关键帧' if offset in stills else '该时刻没有关键帧'
        raise ValueError(f'{hint}：站位挂在时间轴关键帧上，先用add_timeline_keyframe建立{offset}ms这一帧')
    subjects = normalize_subjects(change.get('subjects'), room=room)
    if subjects:
        row['subjects'] = subjects
    else:
        # 空数组＝清空：不留一个空列表，读的人就不用猜「空是真没站位还是写过又清掉」。
        row.pop('subjects', None)


def _max_segment_ms(board):
    """单段上限可配：场级 timeline_max_segment_ms 覆盖默认 2000ms。"""
    limit = board.get('timeline_max_segment_ms', DEFAULT_MAX_SEGMENT_MS)
    if type(limit) is not int or not 1 <= limit <= 600000:
        raise ValueError('单段上限须为1至600000的整数毫秒')
    return limit


def _segment_pairs(shot):
    """该镜的分段（毫秒区间）：显式覆盖优先，否则按已有帧推导（与读视图同一份口径）。"""
    return [tuple(row) for row in segments_of(shot)]


def apply_segment_motion(board, change):
    """分段运镜标注：只写人给的那几项（kind/speed/notes），派生字段一律现算不落库。

    为什么要求「必须是已有的段」：运镜是两帧之间的差别，挂在一条不存在的区间上的标注
    谁也读不到——它既不会出现在读视图里，也不会进任何一次生成，写进去就是坏数据。
    """
    shot = _shot_in_scene(board, change.get('shot_id'))
    start = _integer(change.get('from_ms'), '分段起点')
    end = _integer(change.get('to_ms'), '分段终点')
    if (start, end) not in _segment_pairs(shot):
        raise ValueError(f'{start}..{end}ms不是镜头{shot["id"]}的分段；先确定关键帧，或 set_timeline_segments')
    camera_move, notes = change.get('camera_move'), change.get('notes')
    row = {'from_ms': start, 'to_ms': end}
    if camera_move is not None:
        if not isinstance(camera_move, dict):
            raise ValueError('camera_move须为对象，只放kind与speed')
        row['camera_move'] = {key: camera_move[key] for key in ('kind', 'speed') if key in camera_move}
    if notes is not None:
        row['notes'] = notes
    # 校验在写之前：非法种类/速度不该等到下一次 read 才被发现。
    validate_segment(row, _max_segment_ms(board))
    rows = [item for item in shot.get('segment_motions') or []
            if (item.get('from_ms'), item.get('to_ms')) != (start, end)]
    # 空对象/空串不算标注：camera_move 与 notes 都没给就是「取消这一段的标注」，
    # 而不是存一行空标注——两种状态分开存，读的人就不用再猜「空是真没填还是填了空」。
    if row.get('camera_move') or (row.get('notes') or '').strip():
        rows.append(row)
    rows.sort(key=lambda item: item['from_ms'])
    if rows:
        shot['segment_motions'] = rows
    else:
        shot.pop('segment_motions', None)


def _shot_aspect(shot):
    """画幅只认镜头字段；老工程没有这个字段，就不校验。"""
    for field in ('aspect_ratio', 'frame_aspect'):
        value = shot.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _check_aspect(row, aspect):
    if not aspect:
        return
    for field in ('aspect_ratio', 'frame_aspect'):
        if row.get(field) and row[field] != aspect:
            raise ValueError('同一镜头的画幅须与镜头字段一致，不能在一镜里换画幅')


def _validate_segments(segments, duration, anchors, limit, shot):
    if not isinstance(segments, list):
        raise ValueError('分段须为[a,b]区间列表')
    previous = None
    for row in segments:
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError('分段须为[a,b]两元素区间')
        start = _integer(row[0], '分段起点')
        end = _integer(row[1], '分段终点')
        if start >= end:
            raise ValueError(f'分段{start}..{end}ms的起点须小于终点')
        if end > duration:
            raise ValueError(f'分段{start}..{end}ms超出镜头{shot["id"]}时长{duration}ms')
        if end - start > limit:
            raise ValueError(f'分段{start}..{end}ms超过单段上限{limit}ms')
        for edge in (start, end):
            if edge not in anchors:
                raise ValueError(f'分段端点{edge}ms必须落在已有关键帧上')
        if previous is not None and start < previous:
            raise ValueError('分段须按时间顺序且不能重叠')
        previous = end


def validate_handoff(board, shot):
    handoff = shot.get('handoff')
    if handoff is None:
        return
    if not isinstance(handoff, dict) or set(handoff) != HANDOFF_FIELDS:
        raise ValueError('衔接帧须含from_shot与ref_frame')
    source = handoff['from_shot']
    if not isinstance(source, str) or not source:
        raise ValueError('衔接帧来源须为镜头编号；取消衔接用from_shot=null')
    if source == shot['id']:
        raise ValueError('衔接帧不能指向本镜自己')
    if not any(row['id'] == source for row in board['shots']):
        raise ValueError('衔接帧来源镜头不属于同一场')
    if handoff['ref_frame'] not in REF_FRAMES:
        raise ValueError('衔接帧只取last（前一镜末帧）或first（前一镜首帧）')


def validate_timeline(board):
    """只查数据合法性：越界、段长上限、端点、重叠、画幅一致，不猜创作意图。"""
    limit = _max_segment_ms(board)
    for shot in board['shots']:
        duration = _integer(shot.get('duration_ms'), '镜头时长', 1)
        aspect = _shot_aspect(shot)
        anchors = {_integer(frame['offset_ms'], '关键帧位置') for frame in shot.get('keyframes') or []}
        timeline = shot.get('timeline')
        if timeline is not None:
            # 读视图会把 duration_ms/sound_cues/camera_keys 贴在同一个对象上；校验只认存储字段，
            # 这样拿读视图再校验（预览、检查）不会因为派生字段而误判。
            if not isinstance(timeline, dict) or set(timeline) - {'keyframes', 'segments', 'duration_ms', 'sound_cues', 'camera_keys'}:
                raise ValueError('镜内时间轴只保存关键帧计划与显式分段')
            plan = timeline.get('keyframes', [])
            if not isinstance(plan, list):
                raise ValueError('关键帧计划须为列表')
            offsets = set()
            for row in plan:
                if (not isinstance(row, dict) or set(row) - {'offset_ms', 'moment_name', 'description', 'subjects'}
                        or 'offset_ms' not in row):
                    raise ValueError('关键帧计划须含offset_ms，可选moment_name、description与subjects')
                offset = _integer(row['offset_ms'], '关键帧计划位置')
                if offset > duration:
                    raise ValueError(f'关键帧计划位置{offset}ms超出镜头{shot["id"]}时长{duration}ms')
                if offset in offsets:
                    raise ValueError('同一镜头的关键帧计划时刻不能重复')
                offsets.add(offset)
                for field in ('moment_name', 'description'):
                    if field in row and not isinstance(row[field], str):
                        raise ValueError('时刻命名与描述须为文本')
                if 'subjects' in row:
                    # 入库前再查一遍形状：站位是坐标数据，写到墙外或重复资产都算存坏了。
                    # 这里不查房间（房间尺寸要有预演快照才知道），越界在写入口就拒掉了。
                    normalize_subjects(row['subjects'])
                _check_aspect(row, aspect)
            anchors |= offsets
            if 'segments' in timeline:
                _validate_segments(timeline['segments'], duration, anchors, limit, shot)
        # 运镜标注挂在段上（shot['segment_motions']）：它的合法性依赖分段，所以和分段一起查。
        validate_segment_motions(shot, limit)
        validate_handoff(board, shot)


def timeline_view(board, shot):
    """读视图：老数据没有 timeline 时按 duration_ms 与现有关键帧推导，不迁移也不改写存储。"""
    duration = shot['duration_ms']
    stored = shot.get('timeline') if isinstance(shot.get('timeline'), dict) else {}
    plan = []
    for row in stored.get('keyframes') or []:
        item = {'offset_ms': row['offset_ms'], 'moment_name': row.get('moment_name', ''),
                'description': row.get('description', '')}
        if isinstance(row.get('subjects'), list) and row['subjects']:
            # 站位是存下来的输入，读视图原样带出；没有就不带，老场次的读视图一个字不变。
            item['subjects'] = deepcopy(row['subjects'])
        plan.append(item)
    if not plan:
        # 没有关键帧计划就看静帧：老场次只有关键帧，照样能说出「这一镜内部有几段」。
        plan = sorted(({'offset_ms': frame['offset_ms'], 'moment_name': '', 'description': ''}
                       for frame in shot.get('keyframes') or []), key=lambda row: row['offset_ms'])
    segments = stored.get('segments')
    if segments is None:
        segments = derive_segments([row['offset_ms'] for row in plan], duration)
    cues = []
    for sound in shot.get('sounds') or []:
        try:
            offset = sound_time(board, shot, sound)
        except (ValueError, TypeError):
            offset = None
        cues.append({'sound_id': sound['id'], 'offset_ms': offset, 'kind': sound.get('kind'),
                     'label': sound.get('label', '')})
    cues.sort(key=lambda row: (row['offset_ms'] is None, row['offset_ms'] or 0))
    keys = [{'keyframe_id': row['id'], 'offset_ms': row['offset_ms']}
            for row in (shot.get('camera') or {}).get('keys') or []]
    keys.sort(key=lambda row: row['offset_ms'])
    return {'duration_ms': duration, 'keyframes': plan,
            'segments': [[row[0], row[1]] for row in segments],
            'sound_cues': cues, 'camera_keys': keys}


def _timeline_derived(shot):
    """这一段是显式存的还是推导出来的；界面要据此提示「恢复推导」。"""
    stored = shot.get('timeline') if isinstance(shot.get('timeline'), dict) else {}
    return {'keyframes': not (stored.get('keyframes') or []), 'segments': 'segments' not in stored}


def apply_timeline_views(board):
    """把派生视图贴到读结果上；存储里的 board 不动，老数据因此保持原样。"""
    board.setdefault('asset_needs', [])
    for shot in board['shots']:
        shot.setdefault('asset_needs', [])
        shot.setdefault('handoff', None)
        derived = _timeline_derived(shot)
        shot['timeline'] = timeline_view(board, shot)
        shot['timeline_derived'] = derived
        # 框图随关键帧一起返回：界面要按帧画近似 previs，助手要核对这一帧该有什么。
        # 存储里它挂在 shot['layouts'][时刻] 上，这里是它的读视图（深拷贝，改不到存储）。
        for row in shot['timeline']['keyframes']:
            row['layout'] = layout_at(shot, row['offset_ms'])
        # 每段附带运动推导：段是「两帧之间」，差量就挂在段上——界面画变速、助手核对、
        # 生成任务拼提示词，读到的都是这一份，不各自再算一遍。
        shot['segments'] = segment_views(board, shot)
    return board


def strip_timeline_views(board):
    """预览要在原始计划上做算术：先摘掉推导出来的部分，否则一编辑就把推导值当显式值写回。"""
    for shot in board['shots']:
        derived = shot.pop('timeline_derived', None)
        # 分段清单和它上面挂的运动都是推导出来的，不是存储字段。
        shot.pop('segments', None)
        timeline = shot.get('timeline')
        if not isinstance(timeline, dict):
            continue
        # layout 是读视图字段（框图存在 shot['layouts'] 里，不挂在计划行上）：
        # 不摘掉的话，validate_timeline 会把派生形状当成已存字段而拒绝。
        for row in timeline.get('keyframes') or []:
            if isinstance(row, dict):
                row.pop('layout', None)
        if not derived:
            for key in ('duration_ms', 'sound_cues', 'camera_keys'):
                timeline.pop(key, None)
            continue
        if derived.get('keyframes'):
            timeline.pop('keyframes', None)
        if derived.get('segments'):
            timeline.pop('segments', None)
        if not timeline:
            shot.pop('timeline')
    return board

def _provider_summary(receipt):
    """回执摘要：原始请求/响应留在审计里，这里只给可核对的身份与产物字段。"""
    keep = ('provider', 'job_id', 'workflow_file', 'workflow_sha256', 'output_file', 'output_sha256',
            'input_image_count', 'usage', 'task_id', 'request_sha256', 'model', 'resolution')
    return {key: receipt[key] for key in keep if key in receipt} if isinstance(receipt, dict) else {}


def _video_generation_row(shot, task, receipt, basis_hash, prompt_text):
    """把一次已提交的视频任务写成既有片段回执（shot['generations']）里的一行。

    形状对齐 prepare_storyboard_video 写的那一行：之后 refresh_storyboard_video 能按
    同一份记录查询、下载、复核依据是否过期，不需要另造一套回执表。
    """
    provider = receipt['provider_receipt']
    images = [path for path in (task['constraints'].get('first_frame'), task['constraints'].get('last_frame')) if path]
    return {'id': f'{shot["id"]}-generation-{len(shot.get("generations", [])) + 1}',
            'mode': 'endpoints', 'model': provider.get('model'), 'resolution': provider.get('resolution'),
            'duration_ms': task['constraints']['duration_ms'], 'prompt': prompt_text,
            'inputs': [{'kind': 'style', 'path': path} for path in images],
            'basis_hash': basis_hash, 'base_url': provider.get('base_url'), 'key_env': provider.get('key_env'),
            'status': 'submitted', 'provider_task_id': provider.get('task_id'),
            'provider_response': provider.get('response'), 'usage': None,
            'request_path': provider.get('request_path'), 'request_sha256': provider.get('request_sha256'),
            'generated_by': {'skill': receipt['skill']['name'], 'kind': 'video'}}


def _apply(db, board, change, block_ids, room=None):
    raw_op = change.get('op') if isinstance(change, dict) else None
    if board['approved'] and raw_op != 'set_approved' and not (raw_op == 'set_keyframe' and 'style_path' in change):
        raise ContentGuardError('请先解除定案再编辑镜头方案')
    collections = {'shot_id': board['shots'], 'object_id': board.get('objects', []), 'marker_id': board.get('markers', []),
                   'sound_id': [r for shot in board['shots'] for r in shot.get('sounds', [])],
                   'intent_id': board.get('sound_intents', []), 'panel_id': [r for shot in board['shots'] for r in shot.get('panels', [])]}
    for field, rows in collections.items():
        if isinstance(change, dict) and field in change and change[field] is not None and not any(r['id'] == change[field] for r in rows):
            raise ContentGuardError(f'{field}不属于当前场，未执行跨目标修改')
    op = validate_action_shape(change)
    if op == "set_approved":
        _approve(board, change.get("approved"), block_ids)
        return
    if op in {"create_sound_intent", "update_sound_intent", "delete_sound_intent"}:
        apply_intent(board, change, lambda prefix: allocate_id(db, "storyboard", prefix))
        return
    if op in TIMELINE_FIELDS:
        apply_timeline(board, change)
        return
    if op in LAYOUT_FIELDS:
        apply_layout(board, change)
        return
    if op in SUBJECT_FIELDS:
        # 站位是框图的输入，自己一组动作：写它不改 layouts，写 layouts 不改它。
        apply_subjects(board, change, room=room)
        return
    if op in MOTION_FIELDS:
        apply_segment_motion(board, change)
        return
    if op in NEED_FIELDS:
        # 需求与引用分开存：这里只碰需求，visual_elements 的 asset_id 一律不动。
        if op == 'set_asset_needs':
            board['asset_needs'] = validate_asset_needs(change.get('needs'), ['asset_needs'])
        else:
            shot = _shot_in_scene(board, change.get('shot_id'))
            shot['asset_needs'] = validate_asset_needs(change.get('needs'), ['shots', board['shots'].index(shot), 'asset_needs'])
        return
    if op in AUDIO_FIELDS:
        apply_audio(board, change, lambda prefix: allocate_id(db, "storyboard", prefix))
        return
    if op in CAMERA_FIELDS:
        apply_camera(board, change, lambda prefix: allocate_id(db, "storyboard", prefix))
        return
    if op in STAGING_FIELDS:
        apply_staging(board, change, lambda prefix: allocate_id(db, "storyboard", prefix))
        return
    if op == 'use_panel_frame':
        shot = next((row for row in board['shots'] if row['id'] == change['shot_id']), None)
        panel = next((row for row in (shot or {}).get('panels', []) if row['id'] == change['panel_id']), None)
        candidate = next((row for row in (panel or {}).get('candidates', []) if row['id'] == (panel or {}).get('adopted_candidate_id')), None)
        if candidate is None or panel is None or shot is None:
            raise ValueError('请先采用此格候选图再用于编排')
        _set_frame(db, shot, {'offset_ms': panel['offset_ms'], 'sketch_path': candidate['path']}, board['approved'])
        selected_frame = next(row for row in shot['keyframes'] if row['offset_ms'] == panel['offset_ms'])
        panel['frame_id'] = selected_frame['id']
        if type(candidate.get('simple_color')) is bool:
            selected_frame['sketch_source'] = {'source': 'panel', 'panel_id': panel['id'], 'candidate_id': candidate['id'], 'simple_color': candidate['simple_color']}
        return
    if op in {"create_panel", "update_panel", "delete_panel", "add_panel_candidate", "adopt_panel_candidate"}:
        apply_panel(board, change, lambda prefix: allocate_id(db, "storyboard", prefix), _image_path)
        return
    shots = board["shots"]
    if op == "create_shot":
        fields = _shot_fields(change, {}, block_ids)
        shots.append({"id": allocate_id(db, "storyboard", "shot-"), **fields, "keyframes": []})
        return
    shot = next((s for s in shots if s["id"] == change.get("shot_id")), None)
    if not shot:
        raise ContentGuardError("指定的镜头不属于本场")
    if op == "update_shot":
        shot.update(_shot_fields(change, shot, block_ids))
    elif op == "delete_shot":
        shots.remove(shot)
    elif op == "move_shot":
        index = _integer(change.get("index"), "镜头顺序")
        if index >= len(shots):
            raise ValueError("镜头顺序超出范围")
        shots.remove(shot)
        shots.insert(index, shot)
    elif op == "set_keyframe":
        _set_frame(db, shot, change, board["approved"])
    elif op in ("delete_keyframe", "move_keyframe"):
        frame = next((f for f in shot["keyframes"] if f["id"] == change.get("frame_id")), None)
        if frame is None:
            raise ContentGuardError("指定的关键帧不属于此镜头")
        # 有显式时间轴计划时，那个时刻的主人是计划行；只有「时间轴由静帧推导」的老场次，
        # 静帧自己才是锚点，这时框图得跟着它走，否则就成一帧谁也读不到的孤儿数据。
        planned = frame["offset_ms"] in {row['offset_ms'] for row in (shot.get('timeline') or {}).get('keyframes') or []}
        previous = frame["offset_ms"]
        if op == "move_keyframe":
            if frame.get("marker_id"):
                raise ValueError("此帧绑定命名时刻，请移动标记或先解除绑定")
            frame["offset_ms"] = _integer(change.get("offset_ms"), "关键帧位置")
            shot["keyframes"].sort(key=lambda row: row["offset_ms"])
            if not planned:
                move_layout(shot, previous, frame["offset_ms"])
        else:
            shot["keyframes"].remove(frame)
            if not planned:
                drop_layout(shot, previous)


class StoryboardOperationsMixin(StoryboardPrevisMixin, StoryboardAssetsMixin, StoryboardPanelsMixin, StoryboardArtMixin, StoryboardVideoMixin):
    _store: GraphStore

    def read_storyboard_usage_tips(self):
        from .storyboard_usage_tips import read_usage_tips
        return read_usage_tips()

    def export_storyboard_script(self, unit_id, format, path, illustrated=True, annotated=True):
        from .storyboard_manuscript import export_scene
        return export_scene(self, unit_id, format, path, illustrated, annotated)

    def read_storyboard(self, unit_id):
        with self._store.connect() as db:
            if not db.in_transaction:
                db.execute("BEGIN")
            return self._board_view(_load(db, unit_id, layout(self._store.path).material / "storyboard"), self.get_asset)

    @staticmethod
    def _board_view(board, lookup):
        """读视图＝元素视图＋镜内时间轴／衔接／需求的派生形状；存储本身不动。"""
        return apply_timeline_views(element_views(board, lookup))

    def read_storyboard_assets_context(self, unit_id, shot_ids):
        """资产分析上下文＋缺口摘要。

        为什么在这里覆盖：缺口视图属于需求层（asset_needs.py），资产模块本身正在另一路改动，
        在导演台的组合 MRO 里补一层比改对方文件更安全。
        """
        context = super().read_storyboard_assets_context(unit_id, shot_ids)
        context['asset_needs'] = gap_view(self.read_storyboard(unit_id), self.get_asset)
        return context

    def read_asset_needs(self, unit_id):
        """只读：本场需求、已落实的绑定、引用悬空与计数。"""
        return gap_view(self.read_storyboard(unit_id), self.get_asset)

    def read_storyboard_layout(self, unit_id, shot_id):
        """只读：本镜全部框图、站位与哈希。

        `read_storyboard` 已经把框图贴在 `shot.timeline.keyframes[i].layout` 上；
        这里再单列一份「按时刻排序 + 明确哈希」的清单，供界面画近似 previs、助手核对
        「这一帧该有什么」时不必自己翻时间轴。

        `keyframes` 逐帧给 `subjects`（投影的输入）与 `source`（框图从哪来：
        previs＝从预演抓的、subjects＝从站位推的、manual＝人写的），界面据此说得出
        「这帧的框图是怎么来的」。没有站位/框图的帧也在这里，不会因为layouts为空而消失。
        """
        board = self.read_storyboard(unit_id)
        shot = _shot_in_scene(board, shot_id)
        layouts = shot.get('layouts') if isinstance(shot.get('layouts'), dict) else {}
        rows = [{'offset_ms': int(key), 'hash': row['hash'], 'layout': row}
                for key, row in sorted(layouts.items(), key=lambda item: int(item[0]))]
        frames = [{'offset_ms': row['offset_ms'], 'subjects': row.get('subjects') or [],
                   'source': (row.get('layout') or {}).get('source')}
                  for row in shot['timeline']['keyframes']]
        return {'unit_id': unit_id, 'shot_id': shot_id, 'revision': board['revision'],
                'keyframe_offsets': [row['offset_ms'] for row in shot['timeline']['keyframes']],
                'layouts': rows, 'count': len(rows), 'keyframes': frames}

    def read_segment_motion(self, unit_id, shot_id, from_ms, to_ms):
        """只读：某一段的运动推导、校验问题与它对应的视频提示词。

        派生值不落库，所以这里每次都现算：同一段读两次的结果只应因为框图或机位变了而变。
        校验问题与一致性警告分开给（``warnings`` 里带 code）：前者是「这段数据不成立」，
        后者是「标注的运镜和框图对不上」，处理方式不一样，不能混成一句。

        ``prompt`` 里的官方对齐句用本段长度当请求时长（这里还不知道真要与服务要多长）；
        真正发请求时 build_task 会用 constraints.duration_ms 重算那一行。
        """
        board = self.read_storyboard(unit_id)
        shot = _shot_in_scene(board, shot_id)
        motion = derive_segment(board, shot, _integer(from_ms, '分段起点'), _integer(to_ms, '分段终点'))
        limit = _max_segment_ms(board)
        warnings = list(motion.get('warnings') or [])
        try:
            validate_segment(motion, limit)
        except ValueError as error:
            warnings.append({'code': 'INVALID_SEGMENT', 'message': str(error)})
        return {'unit_id': unit_id, 'shot_id': shot_id, 'revision': board['revision'],
                'from_ms': _integer(from_ms, '分段起点'), 'to_ms': _integer(to_ms, '分段终点'),
                'duration_limit_ms': limit, 'motion': motion,
                'prompt': segment_prompt(motion, 'video'), 'warnings': warnings}

    def run_generation_task(self, unit_id, revision, kind, shot_id, offset_ms, prompt='', layout=None, moment=None,
                            constraints=None):
        """按技能声明的生成工作流执行一次真实生成，并把结果绑回关键帧。

        生成是即时命令（不进内容暂存）：失败不绑定、保留旧图，错误里说清是技能缺失、
        输入不合法还是服务失败。任务包与回执都随返回值给出，界面和助手看到同一份输入。
        """
        from tempfile import TemporaryDirectory

        from . import generation_task

        if kind not in generation_task.KINDS:
            raise ValueError('生成类型须为' + '、'.join(generation_task.KINDS))
        board = self.read_storyboard(unit_id)
        if type(revision) is not int or revision != board['revision']:
            raise ContentConflictError('分镜已修改，请刷新后重试')
        shot = _shot_in_scene(board, shot_id)
        offset = _integer(offset_ms, '镜内位置')
        # 显式计划与静帧都算有效帧：读视图把两者并进时间轴，任务包也按这个口径收，
        # 这里再只认计划就会出现「set_keyframe 建过帧却被说不存在」的自相矛盾。
        offsets = generation_task._frame_offsets(shot)
        if offset not in offsets:
            raise ValueError('该时刻还没有关键帧；先用 set_keyframe 建帧或 add_timeline_keyframe 建立计划')
        # 定案前后能改什么由既有规则决定：草图要解除定案，风格帧要已定案。先查，
        # 免得白跑一次生成再被拒。
        if kind == 'sketch' and board['approved']:
            raise ContentGuardError('请先解除定案再生成草图')
        if kind == 'style' and not board['approved']:
            raise ContentGuardError('请先定案本场分镜再生成风格帧')
        context = self.read_script_unit_context(unit_id)
        style_text = context['project_style']['text']
        # 风格帧必须有项目风格（转图靠它），草图不强制：既有草图流程也不要求先填。
        if kind == 'style' and not style_text.strip():
            raise ValueError('请在创作约定中填写项目风格')
        basis, references = self._storyboard_sketch_basis(board, shot, offset, context)
        skill = generation_task.select_skill(kind)
        task = generation_task.build_task(
            kind, shot_id=shot_id, offset_ms=offset, board=board, layout=layout, moment=moment,
            assets=generation_task.assets_from_cards(basis['assets']),
            style={'text': style_text, 'references': [str(path) for path in references]},
            constraints=constraints, prompt=prompt)
        credentials, video_basis_hash = None, None
        if kind == 'video':
            from .storyboard_video import _profile, _signature
            credentials = _profile(self)
            # 视频依据在提交前算好：提交之后再算，一旦变化就说不清「提交的到底是哪一版」。
            video_basis_hash = _signature(self, board, shot)
        prompt_text = generation_task.task_prompt(task)
        with TemporaryDirectory(prefix=f'generation-task-{kind}-') as folder:
            receipt = generation_task.execute(task, skill, work_dir=Path(folder), credentials=credentials)
            written = self._bind_generation(unit_id, revision, kind, shot, task, receipt, basis, context,
                                           prompt_text, video_basis_hash)
        frames = next((row for row in written['shots'] if row['id'] == shot_id), {}).get('keyframes') or []
        bound = next((row for row in frames if row['offset_ms'] == offset), {})
        # 只回本次 kind 绑定的那一栏：风格帧也带草图，按存在与否取会报错图。
        field = {'sketch': 'sketch_path', 'style': 'style_path'}.get(kind)
        return {'unit_id': unit_id, 'kind': kind, 'shot_id': shot_id, 'offset_ms': offset,
                'storyboard_revision': written['revision'], 'bound_to': task['expect']['bind_to'],
                'output_path': bound.get(field) if field else None,
                'skill': receipt['skill'], 'warnings': receipt['warnings'],
                'provider': _provider_summary(receipt['provider_receipt']),
                'started_at': receipt['started_at'], 'finished_at': receipt['finished_at'], 'task': task}

    def _bind_generation(self, unit_id, revision, kind, shot, task, receipt, basis, context,
                         prompt_text, video_basis_hash=None):
        """把生成结果写回关键帧；这里不吞异常——失败即不绑定，旧图原样保留。"""
        provider = receipt['provider_receipt']
        shot_id, offset = shot['id'], task['offset_ms']
        if kind == 'sketch':
            return self._write_storyboard(
                unit_id, revision,
                [{'op': 'set_keyframe', 'shot_id': shot_id, 'offset_ms': offset,
                  'sketch_path': receipt['output_path']}],
                # provider 回执里带原始请求/响应；落盘时 _write_storyboard 只取身份字段。
                sketch_receipt={'provider': provider, 'prompt': prompt_text,
                                'user_prompt': task['prompt'], 'simple_color': False,
                                'layout': deepcopy(task['layout'] or {})},
                sketch_basis=basis)
        if kind == 'style':
            return self._write_storyboard(
                unit_id, revision,
                [{'op': 'set_keyframe', 'shot_id': shot_id, 'offset_ms': offset,
                  'style_path': receipt['output_path']}],
                render_receipt=provider, expected_style=context['project_style']['text'],
                expected_body_revision=context['unit']['body_revision'])
        return self._write_storyboard(
            unit_id, revision, [],
            media_patch={'shot_id': shot_id, 'values': {'generations': [
                *shot.get('generations', []),
                _video_generation_row(shot, task, receipt, video_basis_hash, prompt_text)]}},
            media_audit={'operation': 'generation_task_submitted', 'shot_id': shot_id},
            sketch_basis=basis)

    @content_write
    def declare_asset_needs(self, unit_id, revision, unit_needs=None, shot_needs=None, replace=True):
        """声明场级／镜级资产需求；需求只描述「要什么」，从不自动建卡或绑卡。

        replace=True（默认）时，给出的列表整体替换该范围原有需求；replace=False 时按
        同对象同形态去重后追加。没给的那一侧保持不动。
        """
        if type(replace) is not bool:
            raise ValueError('replace须为布尔值：true整体替换，false追加')
        if unit_needs is None and shot_needs is None:
            raise ValueError('请提供unit_needs（场级）或shot_needs（镜级）')
        board = self.read_storyboard(unit_id)
        if type(revision) is not int or revision != board['revision']:
            raise ContentConflictError('分镜已修改，请刷新后重试')
        if board.get('approved'):
            raise ContentGuardError('请先解除定案再修改资产需求')
        changes = []
        if unit_needs is not None:
            rows = validate_asset_needs(unit_needs, ['unit_needs'])
            if not replace:
                rows = merge_needs(validate_asset_needs(board.get('asset_needs') or []), rows)
            changes.append({'op': 'set_asset_needs', 'needs': rows})
        if shot_needs is not None:
            if not isinstance(shot_needs, list):
                raise ValueError('shot_needs须为[{shot_id, needs}]列表')
            shots = {shot['id']: shot for shot in board['shots']}
            seen = set()
            for index, entry in enumerate(shot_needs):
                if not isinstance(entry, dict) or set(entry) != {'shot_id', 'needs'}:
                    raise ValueError('shot_needs每项须含shot_id与needs')
                identifier = entry['shot_id']
                if not isinstance(identifier, str) or identifier not in shots:
                    raise ValueError('镜级需求的目标镜头不属于本场')
                if identifier in seen:
                    raise ValueError('同一镜头只能声明一次；重复目标请合并成一个列表')
                seen.add(identifier)
                rows = validate_asset_needs(entry['needs'], ['shot_needs', index, 'needs'])
                if not replace:
                    rows = merge_needs(validate_asset_needs(shots[identifier].get('asset_needs') or []), rows)
                changes.append({'op': 'set_shot_asset_needs', 'shot_id': identifier, 'needs': rows})
        written = self._write_storyboard(unit_id, revision, changes)
        written['asset_needs_gap'] = gap_view(written, self.get_asset)
        return written

    def check_storyboard(self, unit_id):
        from .storyboard_review import check_storyboard
        return check_storyboard(self, self.read_storyboard(unit_id))

    def preview_storyboard(self, unit_id, time_ms):
        board = self.read_storyboard(unit_id)
        resolve_markers(board)
        validate_camera(board)
        validate_audio(board, check_media=False)
        validate_script(board, self.get_asset, {row['id']: row for row in self.read_script_unit_context(unit_id)['unit']['blocks']})
        validate_timing(board)
        # 校验只认存储里的显式分段：读视图的分段是推导出来的，端点不必落在关键帧上。
        validate_timeline(strip_timeline_views(board))
        return preview(board, time_ms)

    def preview_storyboard_changes(self, unit_id, revision, changes):
        """Simulate existing-object edits without allocating IDs or persisting anything."""
        original = self.read_storyboard(unit_id)
        if _integer(revision, '修订号') != original['revision']:
            raise ContentConflictError('分镜已修改，请刷新')
        if not isinstance(changes, list) or not changes:
            raise ValueError('请提供待预览变更')
        allowed = {'update_marker', 'delete_marker', 'update_shot', 'move_shot', 'move_keyframe', 'delete_keyframe',
                   'update_object', 'set_camera_overlays', 'move_action_keyframe', 'delete_action_keyframe', 'bind_keyframe_marker',
                   'bind_action_keyframe_marker', 'move_camera_keyframe', 'delete_camera_keyframe',
                   'add_timeline_keyframe', 'update_timeline_keyframe', 'delete_timeline_keyframe', 'set_timeline_segments',
                   'set_shot_handoff', 'set_asset_needs', 'set_shot_asset_needs',
                   'set_keyframe_layout', 'clear_keyframe_layouts', 'set_segment_motion'}
        board = strip_timeline_views(deepcopy(original))
        # 编辑与比较都在原始计划上做；派生字段最后再贴回返回值，免得被当成已存字段。
        strip_timeline_views(original)
        conflicts = []
        with self._store.connect() as db:
            blocks = {row[0] for row in db.execute('SELECT id FROM script_blocks WHERE unit_id=?', (unit_id,))}
        try:
            for change in changes:
                op = validate_action_shape(change)
                if op not in allowed:
                    raise ValueError('预览只支持已有对象的更新、移动、绑定与删除，不分配新编号')
                _apply(None, board, change, blocks)
            resolve_markers(board)
            conflicts.extend(timing_conflicts(board))
            conflicts.extend(audio_conflicts(board))
            if not conflicts:
                validate_camera(board)
                validate_audio(board, check_media=False)
                validate_script(board, self.get_asset, {row['id']: row for row in self.read_script_unit_context(unit_id)['unit']['blocks']})
                validate_timing(board)
                validate_timeline(board)
                validate_layouts(board)
        except (ValueError, TypeError) as error:
            conflicts.append({'code': 'invalid_storyboard_change', 'message': str(error)})
        old_shots = {row['id']: row for row in original['shots']}
        old_objects = {row['id']: row for row in original.get('objects', [])}
        affected_objects = [{'object_id': row['id'], 'name': row['name']} for row in board.get('objects', []) if row != old_objects.get(row['id'])]
        affected_shots = []
        affected_frames = []
        for index, shot in enumerate(board['shots']):
            old = old_shots[shot['id']]
            changed = shot != old or bool(affected_objects) or original['shots'][index]['id'] != shot['id']
            if changed:
                affected_shots.append({'shot_id': shot['id'], 'title': shot['title'], 'action_start_ms': shot['action_start_ms']})
                prior = {frame['id']: frame for frame in old['keyframes']}
                for frame in shot['keyframes']:
                    if frame != prior.get(frame['id']) or shot.get('camera') != old.get('camera') or affected_objects or shot['action_start_ms'] != old['action_start_ms']:
                        affected_frames.append({'shot_id': shot['id'], 'frame_id': frame['id'], 'offset_ms': frame['offset_ms']})
        return {'unit_id': unit_id, 'revision': revision, 'valid': not conflicts, 'conflicts': conflicts,
                'affected_shots': affected_shots, 'affected_frames': affected_frames, 'affected_objects': affected_objects,
                'preview': apply_timeline_views(board) if not conflicts else None}

    def load_storyboard_camera_presets(self):
        with self._store.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='storyboard_camera_presets'").fetchone()
        return json.loads(row[0]) if row else {'revision': 0, 'presets': []}

    @content_write
    def save_storyboard_camera_preset(self, name, settings, expected_revision, reason):
        from .storyboard_camera import EASINGS, POSE_FIELDS, PRESETS, validate_handheld
        from .storyboard_timing import number
        if not isinstance(reason, str) or not reason.strip():
            raise ContentGuardError('保存项目预设需要明确原因')
        if not isinstance(name, str) or not name.strip():
            raise ContentValidationError('保存项目预设需要名称', problems=[{'code': 'CONTENT_VALIDATION', 'message': '预设名称不能为空', 'path': ['name']}])
        if not isinstance(settings, dict) or set(settings) - {'preset', 'amount', 'start', 'end', 'easing', 'handheld'} or settings.get('preset') not in PRESETS:
            raise ValueError('项目预设仅保存preset/amount/start/end/easing/handheld，不保存场内目标编号')
        if settings.get('easing', 'linear') not in EASINGS:
            raise ValueError('摄影机速度曲线无效')
        validate_handheld(settings.get('handheld'))
        for side in ('start', 'end'):
            if side in settings:
                if not isinstance(settings[side], dict) or set(settings[side]) - POSE_FIELDS:
                    raise ValueError('预设摄影机姿态字段无效')
                for field, value in settings[side].items():
                    number(value, field)
                pose = settings[side]
                if pose.get('focus_distance', 1) <= 0:
                    raise ValueError('对焦距离须大于0')
                if not 1 <= pose.get('fov_deg', 50) < 179 or not -90 <= pose.get('pitch', 0) <= 90:
                    raise ValueError('预设视野或俯仰超出范围')
        if 'amount' in settings:
            number(settings['amount'], '运镜幅度')
        with self._store.connect(write=True) as db:
            row = db.execute("SELECT value FROM meta WHERE key='storyboard_camera_presets'").fetchone()
            data = json.loads(row[0]) if row else {'revision': 0, 'presets': []}
            if type(expected_revision) is not int or expected_revision != data['revision']:
                raise ContentConflictError('项目摄影机预设已修改，请刷新')
            preset: dict[str, Any] | None = next((row for row in data['presets'] if row['name'] == name.strip()), None)
            if preset is None:
                preset = {'id': allocate_id(db, 'storyboard', 'camera-preset-'), 'name': name.strip()}
                data['presets'].append(preset)
            preset['settings'] = deepcopy(settings)
            data['revision'] += 1
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('storyboard_camera_presets',?)", (json.dumps(data, ensure_ascii=False),))
            graph_revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
            db.execute('INSERT INTO actions(id,revision,action,reason,summary) VALUES(?,?,?,?,?)',
                       (uuid4().hex, graph_revision, 'save_storyboard_camera_preset', reason,
                        json.dumps({'preset_id': preset['id'], 'name': preset['name'], 'revision': data['revision']}, ensure_ascii=False)))
            return data

    def render_storyboard_style(self, unit_id, shot_id, frame_id, expected_revision):
        """Transform the approved sketch and bind only if its plan still matches."""
        from tempfile import TemporaryDirectory

        from .storyboard_style_provider import render_style_image

        board = self.read_storyboard(unit_id)
        if board["revision"] != _integer(expected_revision, "修订号") or not board["approved"]:
            raise ValueError("请刷新并确认分镜已经定案")
        shot = next((s for s in board["shots"] if s["id"] == shot_id), None)
        frame = next((f for f in (shot or {}).get("keyframes", []) if f["id"] == frame_id), None)
        if not shot or not frame:
            raise ValueError("关键帧不属于当前镜头")
        context = self.read_script_unit_context(unit_id)
        generation_basis, references = self._storyboard_sketch_basis(board, shot, frame['offset_ms'], context)
        paragraphs = [b["payload"].get("text", "") for b in context["unit"]["blocks"] if b["id"] in shot["source_block_ids"]]
        style = context["project_style"]["text"]
        if not style.strip():
            raise ValueError("请在创作约定中填写项目风格")
        prompt = (f"Transform this storyboard sketch into the final production image. "
                  f"Preserve viewpoint, framing, silhouette, blocking and main light direction. "
                  f"Remove sketch lines unless required by the art style. Shot: {shot['title']}. "
                  + 'Shot writing and visual usage: ' + json.dumps({key: generation_basis[key] for key in ('description', 'shot_language', 'visual_elements')}, ensure_ascii=False) + '. '
                  f"Screenplay: {' '.join(paragraphs)}. Art direction: {style}. "
                  'Asset appearance references: ' + json.dumps([{key: card[key] for key in ('id','category','title','detail','prompt') if key in card}
                                                               for card in generation_basis['assets']], ensure_ascii=False))
        with TemporaryDirectory(prefix="storyboard-style-") as directory:
            output = Path(directory) / "style.png"
            receipt = (render_style_image(Path(frame["sketch_path"]), prompt, output, reference_images=references)
                       if references else render_style_image(Path(frame["sketch_path"]), prompt, output))
            change = {"op": "set_keyframe", "shot_id": shot_id, "offset_ms": frame["offset_ms"], "style_path": str(output)}
            return self._write_storyboard(unit_id, expected_revision, [change], render_receipt=receipt,
                                          expected_style=style, expected_body_revision=context["unit"]["body_revision"],
                                          sketch_basis=generation_basis)

    @content_write
    def apply_storyboard_changes(self, unit_id, revision, changes):
        return self._write_storyboard(unit_id, revision, changes)

    def _write_storyboard(self, unit_id, revision, changes, *, render_receipt=None, expected_style=None, expected_body_revision=None, sketch_receipt=None, sketch_basis=None, media_patch=None, media_audit=None, asset_analysis=False):
        if sketch_receipt is not None and sketch_basis is None:
            raise ValueError('生成草图须提供完整生成依据')
        if media_patch is not None and (not isinstance(media_patch, dict) or set(media_patch) != {'shot_id', 'values'}
                                       or not isinstance(media_patch['values'], dict)
                                       or set(media_patch['values']) - {'generations', 'selected_video', 'animation'}):
            raise ValueError('内部媒体写入字段无效')
        if media_audit is not None and not isinstance(media_audit, dict):
            raise ValueError('媒体审计须为对象')
        root = layout(self._store.path).material / "storyboard"
        with ExitStack() as rollback, ExitStack() as dependency_locks, self._store.connect(write=True) as db:
            board = _load(db, unit_id, root)
            if type(revision) is not int or revision < 0:
                raise ContentConflictError('修订号须为当前非负整数')
            if revision != board["revision"]:
                raise ContentConflictError("分镜已修改，请刷新后重试")
            if not isinstance(changes, list) or (not changes and media_patch is None):
                raise ValueError('请提供分镜变更')
            if asset_analysis:
                unit_row = db.execute('SELECT data FROM script_units WHERE id=?', (unit_id,)).fetchone()
                if unit_row is None or json.loads(unit_row[0])['body_revision'] != expected_body_revision:
                    raise ContentConflictError('正文依据已变化，请重新分析')
            if render_receipt is not None:
                from .project_style import effective_fields, read_defaults, render_agreement
                source = db.execute(
                    "SELECT s.data,u.data FROM script_units u JOIN script_segments g ON g.id=u.segment_id "
                    "JOIN screenplays s ON s.id=g.screenplay_id WHERE u.id=?", (unit_id,)).fetchone()
                style = render_agreement(effective_fields(read_defaults(db)["fields"], json.loads(source[0])))
                if style != expected_style or json.loads(source[1])["body_revision"] != expected_body_revision:
                    raise ContentConflictError("生成期间正文或项目风格已变化，请重新核对")
            asset_lookup = self.get_asset
            asset_db = None
            needs_asset_lock = sketch_basis is not None or any(row.get('asset_id') for shot in board['shots'] for row in shot.get('visual_elements', [])) or any(change.get('asset_id') or any(row.get('asset_id') for row in change.get('visual_elements', []) if isinstance(row, dict)) for change in changes)
            if needs_asset_lock:
                # Serialize asset writers while checking actual selected cards/images and binding.
                asset_db = dependency_locks.enter_context(self._asset_db(write=True))
                def locked_asset(identifier):
                    row = asset_db.execute("SELECT data FROM assets WHERE id=?", (identifier,)).fetchone()
                    if row is None:
                        raise ContentGuardError("资产不存在")
                    return json.loads(row[0])
                asset_lookup = locked_asset
            if sketch_basis is not None:
                assert asset_db is not None
                shot = next(row for row in board['shots'] if row['id'] == sketch_basis['shot_id'])
                current_basis, _ = self._storyboard_sketch_basis(
                    board, shot, sketch_basis['offset_ms'], self.read_script_unit_context(unit_id), asset_db)
                if current_basis != sketch_basis:
                    # 报错要说出「具体哪个键变了」：模型与用户据此判断该重画还是该回退。
                    raise ContentConflictError('生成期间出图依据已变化，请重新生成（变化的依据字段：'
                                               + '、'.join(changed_basis_keys(sketch_basis, current_basis)) + '）')
            if media_patch is not None:
                media_shot = next((shot for shot in board['shots'] if shot['id'] == media_patch['shot_id']), None)
                if media_shot is None:
                    raise ValueError('媒体所属镜头不属于本场')
                media_shot.update(deepcopy(media_patch['values']))
            block_ids = {r[0] for r in db.execute("SELECT id FROM script_blocks WHERE unit_id=?", (unit_id,))}
            # 房间尺寸只有站位校验用得上，而取值要问预演层（见 _subject_room）：没有
            # set_keyframe_subjects 的写入不该为它付一次文件读。
            room = (self._subject_room(unit_id)
                    if any(change.get('op') in SUBJECT_FIELDS for change in changes if isinstance(change, dict))
                    else None)
            for index, change in enumerate(changes):
                with content_item(['changes', index], change):
                    if 'visual_elements' in change:
                        validate_elements(change['visual_elements'], asset_lookup, path=['elements' if asset_analysis else 'changes', index, 'visual_elements'], required=False)
                    _apply(db, board, change, block_ids, room=room)
                    if asset_analysis and change.get('op') == 'update_shot':
                        next(shot for shot in board['shots'] if shot['id'] == change['shot_id'])['assets_analyzed'] = True
            if sketch_receipt is not None:
                from .storyboard_review import sketch_signature
                assert sketch_basis is not None
                generated_shot = next(row for row in board['shots'] if row['id'] == sketch_basis['shot_id'])
                generated_frame = next(row for row in generated_shot['keyframes'] if row['offset_ms'] == sketch_basis['offset_ms'])
                generated_frame['sketch_source'] = {
                    'user_prompt': sketch_receipt.get('user_prompt', ''), 'layout': deepcopy(sketch_receipt.get('layout', {})),
                    'simple_color': sketch_receipt['simple_color'], 'provider': sketch_receipt['provider'].get('provider'),
                    'job_id': sketch_receipt['provider'].get('job_id'), 'body_revision': sketch_basis['body_revision'],
                    'asset_ids': [row['id'] for row in sketch_basis['assets']]}
                # 签名按写入后的依据算：框图哈希此刻已经是刚绑上的新图，check_storyboard 重算才
                # 对得上；请求时的依据（旧框图）只留在审计里说明「当时依据是什么」。
                settled_basis, _ = self._storyboard_sketch_basis(
                    board, generated_shot, sketch_basis['offset_ms'],
                    self.read_script_unit_context(unit_id), asset_db)
                generated_frame['sketch_basis_hash'] = sketch_signature(settled_basis)
            if media_audit is not None and media_audit.get('panel_generation'):
                generation = media_audit['panel_generation']
                generated_panel = next(panel for shot in board['shots'] for panel in shot.get('panels', []) if panel['id'] == generation['panel_id'])
                generated_panel['candidates'][-1]['generation_basis'] = deepcopy(generation['basis'])
                generated_panel['candidates'][-1]['generation_prompt'] = generation['prompt']
                generated_panel['candidates'][-1]['simple_color'] = generation['simple_color']
            resolve_markers(board)
            validate_camera(board)
            validate_audio(board)
            validate_script(board, asset_lookup, {row['id']: row for row in self.read_script_unit_context(unit_id)['unit']['blocks']})
            validate_timing(board, asset_lookup)
            validate_timeline(board)
            validate_layouts(board)
            validate_media(board)
            validate_panels(board)
            board["revision"] += 1
            for frame, field in _media_paths(board):
                source = Path(frame[field])
                copied = False
                if source.parent != root:
                    root.mkdir(parents=True, exist_ok=True)
                    target = root / (uuid4().hex + source.suffix.lower())
                    with target.open("xb") as output:
                        rollback.callback(target.unlink, missing_ok=True)
                        on_rollback(self._store.path, lambda path=target: path.unlink(missing_ok=True))
                        with source.open("rb") as original:
                            shutil.copyfileobj(original, output)
                    source = target
                    copied = True
                if copied and field == 'audio_path':
                    from .storyboard_audio import probe_audio
                    if probe_audio(str(source))['media_sha256'] != frame['media_sha256']:
                        raise ValueError('复制期间音频源文件已变化，请重新导入')
                if copied and field in ('path', 'output_path') and isinstance(frame.get('media'), dict):
                    import hashlib
                    with source.open('rb') as media_file:
                        digest = hashlib.file_digest(media_file, 'sha256').hexdigest()
                    if digest != frame['media'].get('media_sha256'):
                        raise ValueError('复制期间视频源文件已变化，请重新导入')
                frame[field] = source.name
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                       ("storyboard:" + unit_id, json.dumps(board, ensure_ascii=False)))
            graph_revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
            summary = {"unit_id": unit_id, "storyboard_revision": board["revision"], "operations": [c["op"] for c in changes]}
            if media_audit is not None:
                summary.update(deepcopy(media_audit))
            if render_receipt is not None:
                summary["style_render"] = render_receipt
            if sketch_receipt is not None:
                summary["sketch_generation"] = sketch_receipt
                summary["sketch_basis"] = sketch_basis
            db.execute("INSERT INTO actions(id,revision,action,reason,summary) VALUES(?,?,?,?,?)",
                       (uuid4().hex, graph_revision, "apply_storyboard_changes", "编辑分镜导演台", json.dumps(summary, ensure_ascii=False)))
            for frame, field in _media_paths(board):
                frame[field] = str(root / frame[field])
            if render_receipt is not None:
                board["style_render"] = {key: value for key, value in render_receipt.items() if not key.startswith("raw_")}
            if sketch_receipt is not None:
                assert sketch_basis is not None
                board["sketch_render"] = {key: sketch_receipt["provider"].get(key) for key in ("provider", "job_id", "usage", "output_file", "input_image_count")}
                rendered_shot = next(shot for shot in board['shots'] if shot['id'] == sketch_basis['shot_id'])
                board['sketch_render']['output_file'] = next(frame['sketch_path'] for frame in rendered_shot['keyframes']
                                                            if frame['offset_ms'] == sketch_basis['offset_ms'])
            if active_connection(self._store.path) is None:
                db.commit()
            rollback.pop_all()
            return self._board_view(board, asset_lookup)

