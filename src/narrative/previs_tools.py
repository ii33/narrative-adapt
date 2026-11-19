"""把三维预演（DirectorDesk）接进分镜：读摘要、绑机位、铺布景、把一帧投影成框图。

两条数据来源，先用页面再看文件：

* **页面**：预演页里正在编辑的场景才是当前布景（``previs_host.PrevisHost``）。人工在
  页面上拖过机位之后，``.director`` 还是旧文件，照文件投影会得到和预演页不一样的框图；
* **文件**：页面不可达（没开预演台、页面已换、接口版本不对）时回落 ``.director`` 解析
  （``previs_scene``）。那时文件是唯一拿得到的东西，也得能出图。

为什么单独一层而不塞进 ``apply_storyboard_changes``：预演是**外部工程**，
它的读取、场景选择与投影都不属于「分镜编辑动作目录」（``storyboard_contract``
的闭集动作表）；而且绑机位只是给镜头记一条映射，不碰构图数据。放在这里，
几个工具各管一件事，框图仍然只有 ``set_keyframe_layout`` 一个写入口。

文件约定（唯一一条，错误消息里也会重复它）：

* 预演工程放在**工程数据根**下的 ``previs/<场次编号>/``（与该场的 ``material/``
  同级），一个 ``*.director`` 对应一个场景；
* 同目录下的 ``scene.state.json`` 是**我们自己的**场景快照摘要（房间、盒体、机位、
  cut、``revision`` 与来源），由 ``previs_host`` 在每次 ``snapshot``/批量写之后刷新；
  分镜稿按它的 ``revision`` 判断画面依据有没有变。
* 环境变量 ``NARRATIVE_PREVIS_PROJECT_ROOT`` 可整体替换这条根目录：绝对路径照用，
  相对路径按工程数据根解析（不按进程当前目录，免得换个工作目录就读不到）；
* 目录里有多个 ``.director`` 时用 ``scene_id`` 选：先按文件名（``<scene_id>.director``），
  再按文件里的场景编号/场景名。

时刻映射：``shot.previs.time_offset_ms`` 是**镜头第 0 毫秒对应的预演时刻**，
于是镜内 ``offset_ms`` 那帧对应的预演时刻是 ``time_offset_ms + offset_ms``。
这样一镜里各帧的预演时刻与剪辑点无关，镜头整体前移后移也只要改这一个数。

框图里的 ``asset_id`` 写 ``previs:<实体编号>``：预演实体不是资产卡，
明确标出来源，界面按资产取参考图时自然取不到，也就不会误配。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from uuid import uuid4

from . import previs_host
from .content_errors import ContentConflictError, ContentGuardError
from .graph_store import GraphStore
from .previs_scene import (
    DEFAULT_FOCAL_MM,
    camera_at,
    derive_layout_from_subjects,
    framing_of,
    load_scene,
    project,
)
from .project_package import layout
from .storyboard_layout import SUBJECT_SOURCE

ENV_ROOT = 'NARRATIVE_PREVIS_PROJECT_ROOT'
PREVIS_DIR = 'previs'
SUFFIX = '.director'
PREVIS_SOURCE = 'previs'
MANUAL_SOURCE = 'manual'
ASSET_PREFIX = 'previs:'
CAMERA_DIGITS = 3
BLOCK_KINDS = ('actor', 'prop')
# 页面 setCamera 的 framing 是四个预设名；框图口径是 wide/medium/close，两边都收，
# 送给页面时换成它的预设名（映射见 _page_framing）。
FRAMINGS = ('wide', 'medium', 'close', '全景', '中景', '近景', '特写')
FRAMING_PRESETS = {'wide': '全景', 'medium': '中景', 'close': '近景'}
BLOCK_FIELDS = ('id', 'kind', 'name', 'asset', 'position', 'rotation', 'scale', 'height')
CAMERA_FIELDS = ('id', 'name', 'position', 'target', 'focal', 'framing')
# 省略 camera_id 与显式传 null 是两件事（一个是忘写，一个是解绑），用哨兵区分开。
_UNSET = object()


def normalize_previs(raw):
    """校验 ``shot.previs`` 的形状：``{scene_id, camera_id, time_offset_ms}`` 或 None。

    ``scene_id`` 允许为空：单场景工程没有场景编号，绑定时就只认真机位。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError('预演的镜头绑定须为{scene_id, camera_id, time_offset_ms}对象或null')
    extra = set(raw) - {'scene_id', 'camera_id', 'time_offset_ms'}
    if extra:
        raise ValueError('预演绑定有不认识的字段：' + '、'.join(sorted(extra))
                         + '；允许：scene_id、camera_id、time_offset_ms')
    camera_id = raw.get('camera_id')
    if not isinstance(camera_id, str) or not camera_id.strip():
        raise ValueError('预演绑定的camera_id须为非空文本（取消绑定用null，而不是空串）')
    scene_id = raw.get('scene_id')
    if scene_id is not None and (not isinstance(scene_id, str) or not scene_id.strip()):
        raise ValueError('预演绑定的scene_id须为非空文本或null')
    offset = raw.get('time_offset_ms', 0)
    if type(offset) is not int or offset < 0:
        raise ValueError('预演绑定的time_offset_ms须为大于等于0的整数毫秒')
    return {'scene_id': scene_id.strip() if isinstance(scene_id, str) else None,
            'camera_id': camera_id.strip(), 'time_offset_ms': offset}


class StoryboardPrevisMixin:
    """预演接入：只读摘要、镜头↔机位绑定、按帧投影写框图。"""

    _store: GraphStore

    # -- 只读 -----------------------------------------------------------------

    def read_previs(self, unit_id, scene_id=None):
        """只读本场预演工程摘要：场景名、时长、机位数、实体数、cut 数、文件路径。

        目录里只有一个 ``.director`` 时 ``scene_id`` 省略即可；有多个时用它选择
        （文件名或文件里的场景编号），省略则报错并列出可选项。
        """
        directory = self._previs_dir(unit_id)
        file, chosen = self._previs_file(scene_id, directory)
        scene = load_scene(file, chosen)
        board = self.read_storyboard(unit_id)
        return {'unit_id': unit_id, 'path': str(scene['path']), 'directory': str(directory),
                'scene_id': scene['scene_id'], 'name': scene['name'],
                'duration_s': scene['duration_s'], 'fps': scene['fps'], 'aspect': scene['aspect'],
                'camera_count': len(scene['cameras']), 'entity_count': len(scene['entities']),
                'cut_count': len(scene['cuts']),
                'cameras': [{'camera_id': row['id'], 'name': row['name'], 'focal': row['focal'],
                             'position': list(row['position'])} for row in scene['cameras']],
                'cuts': [{'time': row['time'], 'camera_id': row['camera_id']} for row in scene['cuts']],
                'scenes': scene['scenes'],
                'files': [path.name for path in self._previs_files(directory)],
                'bindings': [{'shot_id': shot['id'], 'title': shot['title'], 'previs': shot['previs']}
                             for shot in board['shots'] if shot.get('previs')]}

    # -- 读：页面快照摘要 -----------------------------------------------------

    def read_scene_set(self, unit_id):
        """本场布景的依赖摘要：房间尺寸、盒体数、机位清单、revision、有没有预演工程。

        为什么先问页面：预演页上正在编辑的场景才是「当前布景」，``.director`` 只是导出的
        快照。页面不可达时退回落盘的 ``scene.state.json``（上次看到的布景），再没有就
        老实说「还没有预演工程」。这是只读工具，缺东西不算错误——分镜稿据此显示依赖，
        不能因为预演台没开就让整页报错。

        **不落盘**：页面里的布景与上次入账的快照不一致时，报 ``pending_change=true`` 与
        ``pending_revision``，真正入账（``onChange`` 后 refresh 或一次批量写）才涨 revision
        ——只读工具不该写工程数据。
        """
        directory = self._previs_dir(unit_id)
        files = self._previs_files(directory)
        host = previs_host.installed()
        failure = ''
        if host is not None:
            try:
                probe = host.peek(unit_id, state_dir=directory)
            except previs_host.PrevisHostError as error:
                failure = str(error)
            else:
                summary = _scene_summary(unit_id, probe['state'], page=True,
                                         directory=directory, files=files)
                summary.update({'revision': probe['revision'],
                                'pending_change': probe['pending_change'],
                                'pending_revision': probe['pending_revision']})
                return summary
        state = previs_host.read_state(previs_host.state_file(directory))
        if state:
            summary = _scene_summary(unit_id, state, page=False, directory=directory, files=files)
            summary['error'] = failure or '预演页不可达，下面是上次看到的布景快照'
            return summary
        return {'unit_id': unit_id, 'has_project': bool(files), 'page': False, 'revision': 0,
                'source': '', 'updated_at': '', 'scene_id': '', 'name': '', 'duration_s': 0.0,
                'fps': 0, 'aspect': '', 'room': None, 'block_count': 0, 'blocks': [],
                'camera_count': 0, 'cameras': [], 'cut_count': 0, 'cuts': [], 'cut_warnings': [],
                'pending_change': False, 'pending_revision': 0,
                'directory': str(directory),
                'state_path': str(previs_host.state_file(directory)),
                'files': [path.name for path in files], 'error': failure,
                'hint': ('把 DirectorDesk 导出的 .director 放进本场预演目录，再在界面里打开预演台'
                         if not files else
                         '本场有预演工程文件，但预演台还没打开或不可达；打开预演台后这里会显示页面里的实时布景')}

    # -- 写：绑定 -------------------------------------------------------------

    def set_shot_previs(self, unit_id, revision, shot_id, camera_id=_UNSET, scene_id=None, time_offset_ms=None):
        """把镜头绑到预演里的某台摄影机；``camera_id=null`` 取消绑定。

        只写 ``shot.previs`` 这一条映射，不动框图、不动时间轴。绑定前先核对场景里
        真有这台机位，免得写进一个点开就报错的映射。
        """
        if camera_id is _UNSET:
            raise ValueError('请明确camera_id：给预演里的摄影机编号表示绑定，显式传null表示取消绑定（省略无法区分）')
        from .storyboard_operations import _load, _shot_in_scene

        root = layout(self._store.path).material / 'storyboard'
        with self._store.connect(write=True) as db:
            board = _load(db, unit_id, root)
            if type(revision) is not int or revision != board['revision']:
                raise ContentConflictError('分镜已修改，请刷新后重试')
            shot = _shot_in_scene(board, shot_id)
            if camera_id is None:
                shot.pop('previs', None)
                reason = '取消预演机位绑定'
            else:
                if not isinstance(camera_id, str) or not camera_id.strip():
                    raise ValueError('camera_id须为非空文本或null；取消绑定请显式传null')
                directory = self._previs_dir(unit_id)
                file, chosen = self._previs_file(scene_id, directory)
                scene = load_scene(file, chosen)
                camera = next((row for row in scene['cameras'] if row['id'] == camera_id.strip()), None)
                if camera is None:
                    raise ValueError('预演场景' + scene['name'] + '里没有摄影机' + camera_id.strip() + '；现有：'
                                     + '、'.join(row['name'] + '(' + row['id'] + ')' for row in scene['cameras']))
                offset = _offset(time_offset_ms, 'time_offset_ms', 0)
                if offset > scene['duration_s'] * 1000:
                    raise ValueError(f'time_offset_ms={offset}超出场景时长{scene["duration_s"]}s；'
                                     '请给出镜头第0毫秒在预演里的实际时刻')
                shot['previs'] = normalize_previs({'scene_id': scene['scene_id'], 'camera_id': camera['id'],
                                                   'time_offset_ms': offset})
                reason = '绑定预演机位'
            board['revision'] += 1
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                       ('storyboard:' + unit_id, json.dumps(board, ensure_ascii=False)))
            graph_revision = db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
            summary = {'unit_id': unit_id, 'shot_id': shot_id, 'storyboard_revision': board['revision'],
                       'previs': shot.get('previs')}
            db.execute('INSERT INTO actions(id,revision,action,reason,summary) VALUES(?,?,?,?,?)',
                       (uuid4().hex, graph_revision, 'set_shot_previs', reason,
                        json.dumps(summary, ensure_ascii=False)))
        written = self._board_view(board, self.get_asset)
        written['previs'] = shot.get('previs')
        return written

    # -- 写：铺布景（一次批量） -----------------------------------------------

    def set_scene_layout(self, unit_id, blocks, cameras, room=None, replace=True):
        """用**一次** ``applyBatch`` 把本场布景铺成 ``blocks``/``cameras`` 描述的样子。

        为什么必须一次批量：页面按事务应用（全部成功或全部不生效），中途失败整批回滚，
        不会留下「只加了一半墙」的中间态；**成功之后**才刷新快照并让 revision 自增——
        失败时快照与 revision 一个字都不动，分镜稿因此不会误以为画面依据变了。

        ``replace=True``：没再出现在 ``blocks``/``cameras`` 里的实体被删掉（整场就是这个
        样子）；``False`` 只增改不删。带 ``id`` 的条目按 id 就地更新（机位走 ``setCamera``），
        不带 id 的是新增、编号由页面生成。

        ``room`` 只做一致性校验：契约 v1 的 ``applyBatch`` 没有「改房间」这个操作，
        尺寸不一致时整批拒绝并说明，而不是假装改成功（缺口已列入交付报告）。
        """
        host = previs_host.installed()
        if host is None:
            raise ValueError(
                '铺布景要直接写预演页，但预演台还没打开或没就绪：请先在界面里打开本场预演页，'
                '再让助手铺场景（没打开时也可以用 .director + 读工具查看已有布景）')
        try:
            return self._apply_scene_layout(host, unit_id, blocks, cameras, room, replace)
        except previs_host.PrevisHostError as error:
            # 工具层只翻 ValueError/OSError：预演页的原因已经可读，包成 ValueError 保住原文。
            raise ValueError(str(error)) from error

    def _apply_scene_layout(self, host, unit_id, blocks, cameras, room, replace):
        state_dir = self._previs_dir(unit_id)
        before = host.snapshot(unit_id, state_dir=state_dir)
        room_row = _layout_room(room)
        if room_row is not None and not _same_room(before.get('room'), room_row):
            raise previs_host.PrevisHostError(
                '预演接口 v1 的 applyBatch 没有「改房间」的操作，房间尺寸改不了：页面现在是'
                f'{before.get("room")!r}，这次请求是{room_row!r}；请把 room 传 null'
                '（房间由预演页自己维护），只铺盒体与机位')
        block_rows = [_layout_block(row, index)
                      for index, row in enumerate(_layout_items(blocks, 'blocks'))]
        camera_rows = [_layout_camera(row, index)
                       for index, row in enumerate(_layout_items(cameras, 'cameras'))]
        operations, warnings = _layout_operations(before, block_rows, camera_rows,
                                                 replace=bool(replace))
        reply = host.apply_batch(operations)
        after = host.snapshot(unit_id, state_dir=state_dir, source=previs_host.SOURCE_BATCH,
                              bump=True)
        warnings.extend(after['state'].get('cut_warnings') or [])
        return {'unit_id': unit_id, 'revision': after['revision'],
                'scene_id': after['scene_id'], 'snapshot_source': 'page',
                'replace': bool(replace), 'operation_count': len(operations),
                'operations': operations,
                'results': reply.get('results') if isinstance(reply, dict) else None,
                'removed': [row['id'] for row in operations if row['op'] == 'remove'],
                'room': after['state'].get('room'),
                'blocks': after['state'].get('blocks') or [],
                'cameras': after['state'].get('cameras') or [],
                'cuts': after['state'].get('cuts') or [],
                'state_path': after.get('state_path') or '',
                'warnings': warnings}

    # -- 写：按帧投影 ---------------------------------------------------------

    def capture_previs_layout(self, unit_id, shot_id, offset_ms, overwrite=False):
        """按该镜的预演映射取机位与时刻，投影成框图并写入该帧。

        默认不覆盖已有**手工**框图（``source=manual``）：手工构图是人写的，机器不该
        悄悄改掉它；确认要换必须显式 ``overwrite=true``。上一次投影留下的框图
        （``source=previs``）可以直接重算。写入仍走 ``set_keyframe_layout``，
        所以哈希、校验、依据口径与手工框图完全一致。

        场景优先取页面快照（预演页上正在编辑的那一场），页面不可达才回落 ``.director``；
        投影数学两条路共用 ``previs_scene``，所以框图的形状与单位没有第二套。
        """
        from .storyboard_operations import _shot_in_scene

        board = self.read_storyboard(unit_id)
        shot = _shot_in_scene(board, shot_id)
        previs = shot.get('previs')
        if not isinstance(previs, dict) or not previs.get('camera_id'):
            raise ContentGuardError('该镜还没有绑定预演机位：先read_previs看本场预演里有哪些摄影机，'
                                    '再用set_shot_previs(unit_id, revision, shot_id, camera_id)绑定')
        offset = _offset(offset_ms, '镜内时刻')
        offsets = [row['offset_ms'] for row in shot['timeline']['keyframes']]
        if offset not in offsets:
            raise ValueError(f'该镜在{offset}ms没有关键帧；先用apply_storyboard_changes的'
                             f'add_timeline_keyframe或set_keyframe建立该时刻（现有：{offsets}ms）')
        existing = (shot.get('layouts') or {}).get(str(offset))
        if isinstance(existing, dict) and existing.get('source') == MANUAL_SOURCE and not overwrite:
            raise ContentGuardError(f'该帧已有手工框图（hash {existing.get("hash")}），未覆盖；'
                                    '确认要用预演投影替换请传overwrite=true')
        scene, snapshot_source = self._capture_scene(unit_id, previs)
        camera_id = previs['camera_id']
        if not any(row['id'] == camera_id for row in scene['cameras']):
            # 机位编号是绑定的唯一锚点：页面上删了/重建了它，这里的映射就失效了。
            raise ValueError('本镜绑定的机位' + camera_id + '在本场布景里已不存在，请重新选择（现有：'
                             + ('、'.join(row['name'] + '(' + row['id'] + ')' for row in scene['cameras'])
                                or '无') + '）')
        time_ms = previs.get('time_offset_ms', 0) + offset
        time_s = time_ms / 1000
        duration = float(scene.get('duration_s') or 0.0)
        if duration and time_s > duration:
            raise ValueError(f'该镜在预演里超出了场景时长：{time_s:.3f}s > {duration}s；'
                             '请把time_offset_ms调小或延长预演场景')
        camera = camera_at(scene, time_s, camera_id)
        objects = project(scene, camera, time_s, scene['aspect'])
        framing = _framing(scene, objects)
        payload = {'shot_id': shot_id, 'offset_ms': offset, 'source': PREVIS_SOURCE,
                   'camera': {'x': round(camera['position'][0], CAMERA_DIGITS),
                              'y': round(camera['position'][1], CAMERA_DIGITS),
                              'z': round(camera['position'][2], CAMERA_DIGITS),
                              'yaw': round(camera['yaw'], CAMERA_DIGITS),
                              'pitch': round(camera['pitch'], CAMERA_DIGITS),
                              'lens_mm': round(camera['focal'], CAMERA_DIGITS), 'framing': framing},
                   # 预演不知道这一帧还缺什么资产，需求层由需求声明负责，这里留空。
                   'objects': [{'asset_id': ASSET_PREFIX + row['entity_id'], 'bbox': row['bbox'],
                                'depth': row['depth'], 'kind': row['kind']} for row in objects],
                   'unmet_needs': []}
        written = self._write_storyboard(unit_id, board['revision'], [
            {'op': 'set_keyframe_layout', 'shot_id': shot_id, 'offset_ms': offset, 'layout': payload}])
        stored = None
        for row in written['shots']:
            if row['id'] != shot_id:
                continue
            stored = next((frame['layout'] for frame in row['timeline']['keyframes']
                           if frame['offset_ms'] == offset), None)
            break
        return {'unit_id': unit_id, 'shot_id': shot_id, 'offset_ms': offset, 'revision': written['revision'],
                'scene_id': scene['scene_id'], 'path': str(scene.get('path') or ''), 'camera_id': camera_id,
                'time_ms': time_ms, 'framing': framing, 'projected': objects, 'layout': stored,
                'snapshot_source': snapshot_source, 'state_path': scene.get('state_path') or ''}

    # -- 写：站位（投影的输入）与按站位投影 -----------------------------------

    def set_keyframe_subjects(self, unit_id, revision, shot_id, offset_ms, subjects):
        """整体替换某一帧的站位（投影的**输入**，不是框图）。

        为什么单开一个工具而不让模型直接写 ``apply_storyboard_changes``：站位是有预演/没预演
        两条路的同一个入口，和它配套的还有 ``derive_keyframe_layout``；放在同一组工具里，
        模型才找得到「这台机器没有预演时，空间依据靠站位给」这一步。写法本身仍走同一条
        原子写入口，不另存一份数据，也不碰 ``layouts``。
        """
        offset = _offset(offset_ms, '镜内时刻', 0)
        # 走公开写入口而不是 _write_storyboard：内容校验没过时它会返回暂存回执（problems 可局部修补），
        # 而直接写内部入口会把这类内容问题变成一次硬错误，同一条数据两种处理口径。
        written = self.apply_storyboard_changes(unit_id, revision, [
            {'op': 'set_keyframe_subjects', 'shot_id': shot_id, 'offset_ms': offset, 'subjects': subjects}])
        frame = _written_frame(written, shot_id, offset)
        if frame is None:
            # 内容没过校验：写入口给的是暂存回执（status=staged＋problems），原样交回去让人修。
            return written
        return {'unit_id': unit_id, 'shot_id': shot_id, 'offset_ms': offset,
                'revision': written['revision'], 'subjects': frame.get('subjects') or [],
                'layout': frame.get('layout'),
                'note': '站位是投影的输入；要出框图再调derive_keyframe_layout'}

    def derive_keyframe_layout(self, unit_id, shot_id, offset_ms, overwrite=False):
        """算出这一帧的框图并写入：**有预演走预演，没预演用站位推**。

        为什么一个工具管两条路：调用方要的是「这一帧的框图」这个结果，而「机位从哪来」
        恰恰是它最容易判断错的地方（预演绑了但预演台没开、绑的机位被删了、文件还没放进来）。
        两种来源分开标在框图里：``source=previs`` 是从预演抓的，``source=subjects`` 是从站位推的
        ——界面因此说得出这一帧是谁算的。

        已有**手工**框图默认不覆盖（``source=manual`` 是人写的），确认替换传 ``overwrite=true``；
        上一次机器算出来的（previs/subjects）可以直接重算。
        """
        from .storyboard_operations import _shot_in_scene

        board = self.read_storyboard(unit_id)
        shot = _shot_in_scene(board, shot_id)
        offset = _offset(offset_ms, '镜内时刻', 0)
        offsets = [row['offset_ms'] for row in shot['timeline']['keyframes']]
        if offset not in offsets:
            raise ValueError(f'该镜在{offset}ms没有关键帧；先用apply_storyboard_changes的'
                             f'add_timeline_keyframe或set_keyframe建立该时刻（现有：{offsets}ms）')
        existing = (shot.get('layouts') or {}).get(str(offset))
        if isinstance(existing, dict) and existing.get('source') == MANUAL_SOURCE and not overwrite:
            raise ContentGuardError(f'该帧已有手工框图（hash {existing.get("hash")}），未覆盖；'
                                    '确认要用机位重算请传overwrite=true')
        previs = shot.get('previs') if isinstance(shot.get('previs'), dict) else None
        previs_problem = ''
        if previs and previs.get('camera_id'):
            try:
                captured = self.capture_previs_layout(unit_id, shot_id, offset, overwrite=True)
            except ValueError as error:
                # 预演绑了但读不到（没开预演台、工程文件不在、机位被删、超出场景时长）：
                # 记下原因继续试站位；真算不出来时两边的缺口都要报出来，否则调用方不知道补哪边。
                previs_problem = str(error)
            else:
                return captured | {'source': PREVIS_SOURCE, 'derived_from': PREVIS_SOURCE,
                                   'previs_problem': ''}
        try:
            payload = derive_layout_from_subjects(board, shot, offset)
        except ValueError as error:
            missing = str(error)
            if previs_problem:
                missing += '；预演这条路也不通：' + previs_problem
            elif not (previs and previs.get('camera_id')):
                missing += '；该镜也没有绑定预演机位（set_shot_previs）'
            raise ValueError('算不出这一帧的框图：' + missing) from error
        written = self.apply_storyboard_changes(unit_id, board['revision'], [
            {'op': 'set_keyframe_layout', 'shot_id': shot_id, 'offset_ms': offset, 'layout': payload}])
        frame = _written_frame(written, shot_id, offset)
        if frame is None:
            return written  # 暂存回执：同上，交给调用方按 problems 修补
        return {'unit_id': unit_id, 'shot_id': shot_id, 'offset_ms': offset, 'revision': written['revision'],
                'source': SUBJECT_SOURCE, 'derived_from': SUBJECT_SOURCE, 'layout': frame.get('layout'),
                'camera': payload['camera'], 'objects': payload['objects'],
                'subjects': frame.get('subjects') or [],
                'previs_problem': previs_problem}

    def _subject_room(self, unit_id):
        """站位校验用的房间尺寸（``{width, depth, height}``）；房间未知时返回 ``None``。

        为什么从预演快照取：房间尺寸只有预演那边知道，而分镜写入口不该去开页面；
        ``previs/<场次>/scene.state.json`` 是预演台自己落的那份，读它不需要页面在跑。
        没预演、预演台从没落过快照时就是「房间未知」，站位不校验越界——没有尺寸可依据。
        """
        state = previs_host.read_state(previs_host.state_file(self._previs_dir(unit_id)))
        room = state.get('room') if isinstance(state, dict) else None
        return room if isinstance(room, dict) else None

    def _capture_scene(self, unit_id, previs):
        """抓帧要用的场景：先问页面，不可达再读 ``.director``。

        页面优先的理由：人工在预演页上拖过之后 ``.director`` 还是旧文件，照旧文件投影会
        得到和预演页不一样的框图。页面不可达（没开预演台、页面已换、协议不匹配）时文件
        是唯一拿得到的东西，所以回落——但那时页面自己的场景校验（``scene_id``）还得做。
        """
        host = previs_host.installed()
        failure = ''
        if host is not None:
            try:
                return host.snapshot(unit_id, state_dir=self._previs_dir(unit_id)), 'page'
            except previs_host.PrevisHostError as error:
                failure = str(error)
        directory = self._previs_dir(unit_id)
        try:
            file, chosen = self._previs_file(previs.get('scene_id'), directory)
            scene = load_scene(file, chosen)
        except ValueError as error:
            if failure:
                raise ValueError(f'{failure}；同时本场也读不到预演工程：{error}') from error
            raise
        if previs.get('scene_id') and scene['scene_id'] != previs['scene_id']:
            raise ValueError('该镜绑定的场景' + previs['scene_id'] + '不在本场预演目录里（当前文件是场景'
                             + scene['scene_id'] + '）；请把文件放回或重新set_shot_previs')
        return scene, 'file'

    # -- 定位预演工程 ---------------------------------------------------------

    def _previs_dir(self, unit_id):
        """预演目录：``<工程数据根>/previs/<场次编号>``，可被环境变量整体替换。"""
        override = os.environ.get(ENV_ROOT, '').strip()
        base = Path(override) if override else Path(PREVIS_DIR)
        if not base.is_absolute():
            # 相对路径按工程数据根解析而不是进程当前目录：换个 cwd 不该换个工程。
            base = layout(self._store.path).root / base
        return base / str(unit_id)

    @staticmethod
    def _previs_files(directory):
        if not directory.is_dir():
            return []
        return sorted((path for path in directory.iterdir()
                       if path.is_file() and path.suffix.lower() == SUFFIX), key=lambda path: path.name)

    def _previs_file(self, scene_id, directory):
        """找到工程文件与场景编号；找不到就报清楚「放哪、还缺什么」。"""
        files = self._previs_files(directory)
        if not files:
            raise ValueError(f'本场还没有预演工程：请把DirectorDesk导出的.director放进{directory}'
                             f'（一个场景一个文件，文件名用场景编号，例如scene-main{SUFFIX}），'
                             f'或用环境变量{ENV_ROOT}指到别的预演根目录')
        if scene_id is None:
            if len(files) == 1:
                return files[0], None
            raise ValueError('本场有多个预演工程：' + '、'.join(path.name for path in files)
                             + '；请用scene_id指定其中一个（文件名或文件里的场景编号）')
        for path in files:
            if path.stem == scene_id:
                return path, None
        for path in files:
            try:
                scene = load_scene(path)
            except ValueError:
                continue  # 坏文件留给「按场景编号找」失败后的统一报错，不在这里抢先报
            if any(row['id'] == scene_id or row['name'] == scene_id for row in scene['scenes']):
                return path, scene_id
        raise ValueError('本场预演里没有场景' + scene_id + '；可用：'
                         + '、'.join(_previs_scene_labels(files)))


def _written_frame(board, shot_id, offset_ms):
    """从写入口返回的读视图里摘出这一帧：站位与框图都挂在时间轴关键帧上。"""
    shot = next((row for row in board.get('shots') or [] if row['id'] == shot_id), None)
    timeline = (shot or {}).get('timeline') if isinstance((shot or {}).get('timeline'), dict) else {}
    return next((row for row in timeline.get('keyframes') or [] if row.get('offset_ms') == offset_ms), None)


def _framing(scene, objects):
    """景别：优先看最近的演员（objects 已按远→近排序），没人看面积最大的对象。"""
    subject = next((row for row in objects if row['kind'] == 'person'), None)
    if subject is None and objects:
        subject = max(objects, key=lambda row: (row['bbox'][2] - row['bbox'][0])
                      * (row['bbox'][3] - row['bbox'][1]))
    return framing_of(subject['bbox'] if subject else None, scene['aspect'])


# -- 布景描述 → 批操作 --------------------------------------------------------

def _scene_summary(unit_id, state, *, page, directory, files):
    """把落盘快照摊成分镜稿要的那几项：房间、盒体、机位、cut、revision、来源。"""
    counts = state.get('counts') or {}
    blocks = state.get('blocks') or []
    cameras = state.get('cameras') or []
    cuts = state.get('cuts') or []
    return {'unit_id': unit_id, 'has_project': True, 'page': page,
            'revision': int(state.get('revision') or 0), 'source': state.get('source') or '',
            'updated_at': state.get('updated_at') or '', 'pending_change': False,
            'pending_revision': int(state.get('revision') or 0),
            'scene_id': state.get('scene_id') or '', 'name': state.get('name') or '',
            'duration_s': state.get('duration_s') or 0.0, 'fps': state.get('fps') or 0,
            'aspect': state.get('aspect') or '', 'room': state.get('room'),
            'block_count': counts.get('blocks', len(blocks)), 'blocks': blocks,
            'camera_count': counts.get('cameras', len(cameras)), 'cameras': cameras,
            'cut_count': counts.get('cuts', len(cuts)), 'cuts': cuts,
            'cut_warnings': state.get('cut_warnings') or [],
            'directory': str(directory), 'state_path': str(previs_host.state_file(directory)),
            'files': [path.name for path in files], 'error': '', 'hint': ''}


def _layout_items(rows, label):
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError(f'{label} 须为数组；要用空清单请显式传 []')
    return rows


def _layout_block(row, index):
    """校验一个盒体描述：``{id?, kind=actor|prop, name?, asset?, position, rotation?, scale?, height?}``。"""
    label = f'blocks第{index + 1}项'
    if not isinstance(row, dict):
        raise ValueError(f'{label}须为对象')
    extra = set(row) - set(BLOCK_FIELDS)
    if extra:
        raise ValueError(f'{label}有不认识的字段：' + '、'.join(sorted(extra))
                         + '；允许：' + '、'.join(BLOCK_FIELDS))
    kind = row.get('kind')
    if kind not in BLOCK_KINDS:
        raise ValueError(f'{label}的kind须为actor或prop（机位请放进cameras），实际是{kind!r}')
    item = {'id': _optional_id(row.get('id'), label), 'kind': kind,
            'name': str(row.get('name') or ''),
            'asset': str(row['asset']) if row.get('asset') is not None else None,
            'position': _vec3(row.get('position'), f'{label}的position')}
    for key in ('rotation', 'scale'):
        item[key] = _vec3(row[key], f'{label}的{key}') if row.get(key) is not None else None
    item['height'] = _positive(row['height'], f'{label}的height') if row.get('height') is not None else None
    return item


def _layout_camera(row, index):
    """校验一个机位描述：``{id?, name?, position, target, focal, framing?}``。"""
    label = f'cameras第{index + 1}项'
    if not isinstance(row, dict):
        raise ValueError(f'{label}须为对象')
    extra = set(row) - set(CAMERA_FIELDS)
    if extra:
        raise ValueError(f'{label}有不认识的字段：' + '、'.join(sorted(extra))
                         + '；允许：' + '、'.join(CAMERA_FIELDS))
    framing = row.get('framing')
    if framing is not None and framing not in FRAMINGS:
        raise ValueError(f'{label}的framing须为' + '/'.join(FRAMINGS) + f'，实际是{framing!r}')
    return {'id': _optional_id(row.get('id'), label), 'name': str(row.get('name') or ''),
            'position': _vec3(row.get('position'), f'{label}的position'),
            'target': _vec3(row.get('target'), f'{label}的target'),
            'focal': _positive(row['focal'], f'{label}的focal') if row.get('focal') is not None else DEFAULT_FOCAL_MM,
            'framing': framing}


def _layout_room(room):
    """房间尺寸只做一致性校验：接口 v1 的批操作里没有「改房间」这一条。"""
    if room is None:
        return None
    if not isinstance(room, dict):
        raise ValueError('room 须为{width, depth, height}对象或null')
    extra = set(room) - {'width', 'depth', 'height', 'enabled'}
    if extra:
        raise ValueError('room 有不认识的字段：' + '、'.join(sorted(extra))
                         + '；允许：width、depth、height')
    return {key: _positive(room.get(key), f'room.{key}') for key in ('width', 'depth', 'height')}


def _same_room(current, wanted):
    if not isinstance(current, dict):
        return False
    try:
        return all(abs(float(current.get(key) or 0.0) - value) < 1e-6
                   for key, value in wanted.items())
    except (TypeError, ValueError):
        return False


def _layout_operations(before, blocks, cameras, *, replace):
    """把「这一场该是什么样」翻成一条批操作序列，顺序：删 → 增改盒体 → 改机位。

    顺序有意义：先删掉被替换的实体，再按 id 改（页面此时还能找到它们），新增跟在后面。
    带 id 的条目必须已经在快照里——否则要的是「新增」，让页面编号；悄悄忽略 id 会让
    调用方以为更新成功。
    """
    warnings = []
    by_block = {row['id']: row for row in blocks if row['id']}
    by_camera = {row['id']: row for row in cameras if row['id']}
    clash = sorted(set(by_block) & set(by_camera))
    if clash:
        raise ValueError('同一个id不能既是盒体又是机位：' + '、'.join(clash))
    existing = {row['id']: row for row in before['cameras'] + before['blocks']}
    unknown = sorted(identifier for identifier in [*by_block, *by_camera] if identifier not in existing)
    if unknown:
        raise ValueError('这些id不在本场布景里：' + '、'.join(unknown)
                         + '；要新增就别给id（编号由页面生成），要更新就用read_scene_set里的id')
    removed = []
    if replace:
        kept = set(by_block) | set(by_camera)
        removed = [identifier for identifier in existing if identifier not in kept]
    operations = [{'op': 'remove', 'id': identifier} for identifier in removed]
    for row in blocks:
        if row['id']:
            operations.append({'op': 'update', 'id': row['id'], 'patch': _block_fields(row)})
        else:
            operations.append({'op': 'add', 'kind': row['kind'], 'name': row['name'] or '盒体',
                               **_drop_none(_block_fields(row))})
    for row in cameras:
        if row['id']:
            operations.append({'op': 'setCamera', 'id': row['id'],
                               'spec': _camera_spec_fields(row)})
        else:
            operations.append({'op': 'add', 'kind': 'camera', 'name': row['name'] or '机位',
                               'position': list(row['position']),
                               'camera': _camera_add_fields(row)})
    # 被删掉的机位如果还在 cut 表里，**不用**在这里补一条 setCuts：页面的 remove 会自己
    # 把指向它的切点重指到还活着的机位上（host-scene 里 cutsRemapped 就是这件事）。
    # 我们另补一条只会把多机位剪辑表压成「只剩开头一个切点」。
    return operations, warnings


def _block_fields(row):
    """盒体的可变字段（update 的 patch / add 的主体），null 表示「不改」。"""
    fields = {'name': row['name'] or None, 'position': list(row['position']),
              'rotation': list(row['rotation']) if row['rotation'] is not None else None,
              'scale': list(row['scale']) if row['scale'] is not None else None,
              'height': row['height']}
    if row['asset'] is not None:
        fields['asset'] = row['asset']
    return fields


def _camera_spec_fields(row):
    """机位的取景参数：``setCamera`` 要 position/target/focal，framing 可选。"""
    fields = {'position': list(row['position']), 'target': list(row['target']),
              'focal': row['focal']}
    if row['framing'] is not None:
        fields['framing'] = _page_framing(row['framing'])
    return fields


def _camera_add_fields(row):
    """新增机位时的 ``camera`` 对象：朝向（target）与焦距，可选景别。

    位置在实体本身（``load_scene`` 与契约都是机位实体带 position、camera 里放取景参数）。
    """
    fields = {'target': list(row['target']), 'focal': row['focal']}
    if row['framing'] is not None:
        fields['framing'] = _page_framing(row['framing'])
    return fields


def _page_framing(framing):
    """框图口径的 wide/medium/close 换成页面预设名；已经是预设名的原样给。"""
    return FRAMING_PRESETS.get(framing, framing)


def _drop_none(fields):
    return {key: value for key, value in fields.items() if value is not None}


def _optional_id(value, label):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}的id须为非空文本或省略（省略表示新增，编号由页面生成）')
    return value.strip()


def _vec3(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f'{label}须为[x, y, z]三元数组，实际是{value!r}')
    return [_number(item, f'{label}[{index}]') for index, item in enumerate(value)]


def _positive(value, label):
    number = _number(value, label)
    if not number > 0:
        raise ValueError(f'{label}须大于0，实际是{value!r}')
    return number


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label}须为有限数值，实际是{value!r}')
    return float(value)


def _offset(value, label, default=None):
    if value is None:
        if default is None:
            raise ValueError(f'{label}缺失')
        return default
    if type(value) is not int or value < 0:
        raise ValueError(f'{label}须为大于等于0的整数毫秒，实际是{value!r}')
    return value


def _previs_scene_labels(files):
    """列「文件:场景」清单，给选场景失败的人看。坏文件也列出来并标明读不了。"""
    labels = []
    for path in files:
        try:
            scene = load_scene(path)
        except ValueError as error:
            labels.append(f'{path.name}（读不了：{error}）')
            continue
        labels.extend(f'{path.name}:{row["id"]}（{row["name"]}）' for row in scene['scenes'])
    return labels or ['（无）']
