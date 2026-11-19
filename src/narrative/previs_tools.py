"""把三维预演（DirectorDesk 的 ``.director``）接进分镜：读摘要、绑机位、把一帧投影成框图。

为什么单独一层而不塞进 ``apply_storyboard_changes``：预演是**外部工程文件**，
它的读取、场景选择与投影都不属于「分镜编辑动作目录」（``storyboard_contract``
的闭集动作表）；而且绑机位只是给镜头记一条映射，不碰构图数据。放在这里，
三个工具各管一件事，框图仍然只有 ``set_keyframe_layout`` 一个写入口。

文件约定（唯一一条，错误消息里也会重复它）：

* 预演工程放在**工程数据根**下的 ``previs/<场次编号>/``（与该场的 ``material/``
  同级），一个 ``*.director`` 对应一个场景；
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
import os
from pathlib import Path
from uuid import uuid4

from .content_errors import ContentConflictError, ContentGuardError
from .graph_store import GraphStore
from .previs_scene import camera_at, framing_of, load_scene, project
from .project_package import layout

ENV_ROOT = 'NARRATIVE_PREVIS_PROJECT_ROOT'
PREVIS_DIR = 'previs'
SUFFIX = '.director'
PREVIS_SOURCE = 'previs'
MANUAL_SOURCE = 'manual'
ASSET_PREFIX = 'previs:'
CAMERA_DIGITS = 3
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

    # -- 写：按帧投影 ---------------------------------------------------------

    def capture_previs_layout(self, unit_id, shot_id, offset_ms, overwrite=False):
        """按该镜的预演映射取机位与时刻，投影成框图并写入该帧。

        默认不覆盖已有**手工**框图（``source=manual``）：手工构图是人写的，机器不该
        悄悄改掉它；确认要换必须显式 ``overwrite=true``。上一次投影留下的框图
        （``source=previs``）可以直接重算。写入仍走 ``set_keyframe_layout``，
        所以哈希、校验、依据口径与手工框图完全一致。
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
        directory = self._previs_dir(unit_id)
        file, chosen = self._previs_file(previs.get('scene_id'), directory)
        scene = load_scene(file, chosen)
        if previs.get('scene_id') and scene['scene_id'] != previs['scene_id']:
            raise ValueError('该镜绑定的场景' + previs['scene_id'] + '不在本场预演目录里（当前文件是场景'
                             + scene['scene_id'] + '）；请把文件放回或重新set_shot_previs')
        camera_id = previs['camera_id']
        if not any(row['id'] == camera_id for row in scene['cameras']):
            raise ValueError('预演场景' + scene['name'] + '里已经没有摄影机' + camera_id
                             + '（文件被改过？）；请重新set_shot_previs')
        time_ms = previs.get('time_offset_ms', 0) + offset
        time_s = time_ms / 1000
        if time_s > scene['duration_s']:
            raise ValueError(f'该镜在预演里超出了场景时长：{time_s:.3f}s > {scene["duration_s"]}s；'
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
                'scene_id': scene['scene_id'], 'path': str(scene['path']), 'camera_id': camera_id,
                'time_ms': time_ms, 'framing': framing, 'projected': objects, 'layout': stored}

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


def _framing(scene, objects):
    """景别：优先看最近的演员（objects 已按远→近排序），没人看面积最大的对象。"""
    subject = next((row for row in objects if row['kind'] == 'person'), None)
    if subject is None and objects:
        subject = max(objects, key=lambda row: (row['bbox'][2] - row['bbox'][0])
                      * (row['bbox'][3] - row['bbox'][1]))
    return framing_of(subject['bbox'] if subject else None, scene['aspect'])


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
