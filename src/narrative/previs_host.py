"""预演页（内嵌 DirectorDesk）的 JS 接口：Python 侧封装、场景快照与 revision。

为什么单独一层：``docs/PREVIS_HOST_API.md`` 定义的那套方法活在页面里
（``window.__narrativePrevisHost``），调用是异步的、结果是一坨 JSON。工具层不该知道
``runJavaScript``、超时、base64 PNG 这些事，也不该被 QtWebEngine 绑住——测试要在没装
PySide6 的进程里跑。所以这里切成两半：

* ``PrevisHost``：契约方法一对一封装 + 错误语义 + 场景快照/revision 落盘。只依赖一个
  ``call(method, payload)`` 的**调用器**。测试注入假调用器，完全不碰 Qt。
* ``QtWebEngineCaller``：真实调用器，真的去 ``QWebEnginePage.runJavaScript``。把异步
  回调折成同步等待的做法与原因写在它自己的说明里（那是这段代码最容易写错的地方）。
  PySide6 只在方法里 import，模块导入不需要 Qt。

调用信封（契约没写死的一层，这里定死；已对着随工程打包的页面实现核对过，页面同时
接受「一个信封对象」与「位置参数」两种写法，所以下面的形状是安全的）：

* 方法名就是 JS 侧的名字（``getScene``…），参数永远是一个 JSON **对象**；
* 数组参数包一层键名：``applyBatch({ops:[…]})``、``setCuts({cuts:[…]})``；两个参数的方法
  包成 ``{id, patch}`` / ``{id, spec}``；
* 结果原样回传（``listCameras`` 回数组，其余回对象），页面用 ``{ok:false, error}`` 表示失败；
* ``onChange(cb)`` 收的是 JS 函数，过不了 JSON 通道——订阅在 QML 侧做，Python 侧用
  :meth:`PrevisHost.record_change` 在回调之后拉快照（见 ``record_change`` 的说明）；
* ``captureFrame`` 的 ``png_base64`` 在这里解成 PNG 文件，返回值里不留 base64。

快照与 revision：``snapshot`` 调 ``getScene`` 把场景转成 ``previs_scene.load_scene`` 的
输出形状（投影代码因此不用分叉），按「页面 revision + 内容摘要」判断 revision 要不要
自增，并把 ``previs/<场次>/scene.state.json``（我们自己的快照，含 revision 与来源）
写下去，供分镜稿显示「画面依赖变了没有」。只读场合用 ``peek``：同样的比较，一个字节都不写。

界面侧接线只做两件事（这里不碰 QML 与桥）：

* 预演页加载完成、引导脚本注入之后：``install(PrevisHost(QtWebEngineCaller(view)))``
  （``view`` 可以是 QML 的 ``WebEngineView``、``QWebEnginePage``，或返回它们的函数）；
* 页面的 ``onChange`` 回调到了之后：``installed().record_change(unit_id, state_dir=…)``。
  页面重载后调 ``PrevisHost.reset()`` 清掉握手缓存。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import itertools
import json
import math
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

# 常量与 previs_scene 同源：画幅解析与默认焦距不另写一份，否则两条路会慢慢对不上。
from .previs_scene import DEFAULT_ASPECT, DEFAULT_FOCAL_MM, DEFAULT_HEIGHT_M, SKIP_ASSET_PREFIXES
from .previs_scene import _aspect_ratio as _scene_aspect_ratio

PROTOCOL_VERSION = 1
# 页面自己的初始化等待上限是 20 秒（超时才回「预演页尚未初始化完成」）；宿主必须比它长，
# 否则我们永远只看得到自己的「超时」，而看不到页面真正的原因。
DEFAULT_TIMEOUT_S = 25.0
DEFAULT_FRAME_WIDTH = 1280
DEFAULT_FRAME_HEIGHT = 720
# 轮询间隔：runJavaScript 一次往返在本机 IPC 上是毫秒级，10ms 足够密又不至于占满主线程。
POLL_MS = 10
STATE_FILE = 'scene.state.json'
STATE_SCHEMA = 'narrative-previs-scene-state/1'
SOURCE_PAGE = 'page'
SOURCE_BATCH = 'apply_batch'
SOURCE_ON_CHANGE = 'onChange'
# 只读探测：页面有布景但还没入账过，就先用这个来源标着（不落盘）。
SOURCE_PROBE = 'probe'
ENTITY_KINDS = ('actor', 'prop', 'camera')
BATCH_OPS = ('add', 'update', 'remove', 'setCamera', 'setCuts')
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
# 只算影响投影/框图的字段：页面里的瞬时状态（选中项、播放头…）不该让 revision 乱涨。
_DIGEST_CAMERA_FIELDS = ('id', 'name', 'position', 'rotation', 'focal', 'target', 'target_id',
                         'target_height', 'aim', 'mode', 'path', 'focal_keys')
_DIGEST_ENTITY_FIELDS = ('id', 'name', 'kind', 'position', 'rotation', 'scale', 'height', 'path')


class PrevisHostError(RuntimeError):
    """预演页给了可读原因，或宿主判定返回不合法。所有接口错误都是它的子类。"""


class PrevisTimeoutError(PrevisHostError):
    """等页面回复超时：页面没加载完、卡住、或已关闭。"""


class PrevisPageNotReady(PrevisHostError):
    """页面还没就绪：引导脚本没注入、方法不存在、页面已换。"""


class PrevisCaller(Protocol):
    """调用器：真实实现是 QtWebEngine，测试里是假的。

    ``call`` 返回页面给的 JSON 值（``listCameras`` 是数组，其余是对象）；页面回
    ``{error}`` 就原样交回来，由 :meth:`PrevisHost._call` 统一翻成 ``PrevisHostError``——
    错误语义只写一处，假调用器不必模仿它。
    """

    def call(self, method: str, payload: dict) -> Any:  # noqa: D102 - Protocol
        ...


class _FunctionCaller:
    """把裸函数当调用器：测试里写 ``PrevisHost(lambda m, p: {...})`` 更省事。"""

    def __init__(self, function: Callable[[str, dict], Any]):
        self._function = function

    def call(self, method: str, payload: dict) -> Any:
        return self._function(method, payload)


class PrevisHost:
    """页面内 JS 接口的 Python 侧：契约方法、错误语义、场景快照与 revision。

    ``caller`` 给一个带 ``call(method, payload)`` 的对象，或一个同签名的函数。
    ``state_dir`` 是快照落盘目录（可以是 ``unit_id -> Path`` 的函数）；不给就只算不落盘
    ——工具层知道工程数据根在哪，调用时用 ``state_dir=`` 现给。
    """

    def __init__(self, caller: PrevisCaller | Callable[[str, dict], Any], *,
                 timeout_s: float = DEFAULT_TIMEOUT_S, state_dir=None):
        self._caller = caller if hasattr(caller, 'call') else _FunctionCaller(caller)
        if not timeout_s or float(timeout_s) <= 0:
            raise ValueError('timeout_s 须大于 0')
        self.timeout_s = float(timeout_s)
        self.state_dir = state_dir
        self.calls = 0  # 已发出的契约调用数（界面诊断与测试用）
        self._handshake: dict | None = None

    # -- 调用与错误语义 -------------------------------------------------------

    def _call(self, method: str, payload: dict | None = None) -> Any:
        payload = {} if payload is None else payload
        if not isinstance(payload, dict):
            raise TypeError(f'{method} 的参数须为一个 JSON 对象，实际是 {type(payload).__name__}')
        self.calls += 1
        try:
            reply = self._caller.call(method, payload)
        except PrevisHostError:
            raise            # 超时/未就绪已经区分好了，不再套一层
        except TimeoutError as error:
            raise PrevisTimeoutError(self._timeout_message(method)) from error
        except Exception as error:  # noqa: BLE001 - Qt/页面侧什么异常都可能抛，一律翻成可读原因
            raise PrevisHostError(f'调用预演页 {method} 失败：{error}') from error
        if isinstance(reply, dict) and reply.get('error'):
            raise PrevisHostError(str(reply['error']))
        return reply

    def _timeout_message(self, method: str) -> str:
        return (f'等预演页回复 {method} 超时（{self.timeout_s:g} 秒）：页面可能还没加载完、'
                '脚本卡住或预演台已关闭；请刷新预演页后重试')

    def hello(self) -> dict:
        """握手并校验协议版本；一次会话只问一次（页面只有一份接口版本）。

        ``protocol`` 变化即视为不兼容（契约的「版本」约定），这里直接给出原因与怎么修，
        而不是让后面每个方法各自莫名失败。
        """
        if self._handshake is None:
            reply = self._call('hello', {})
            if not isinstance(reply, dict):
                raise PrevisHostError('预演页 hello 返回的不是对象（'
                                      + type(reply).__name__ + '）；引导脚本可能没注入完整')
            protocol = reply.get('protocol')
            if protocol != PROTOCOL_VERSION:
                raise PrevisHostError(
                    f'预演接口版本不匹配：页面是 protocol={protocol!r}，本程序只认 protocol={PROTOCOL_VERSION}；'
                    '请让预演页与本程序用同一版前端产物（previs-web 或 director-desk 的 dist），'
                    '不要手改接口版本号')
            if reply.get('ok') is False:
                raise PrevisHostError('预演页握手失败：'
                                      + str(reply.get('error') or '页面没有说原因'))
            self._handshake = reply
        return self._handshake

    def reset(self) -> None:
        """清掉握手缓存：页面重载（换了 dist、重新导航）之后调一次，下一个调用会重新 hello。"""
        self._handshake = None

    # -- 契约方法 -------------------------------------------------------------

    def get_scene(self) -> dict:
        """只读场景快照（``{sceneId, name, duration_ms, fps, aspect, entities[], cuts[]}``）。"""
        self.hello()
        reply = self._call('getScene', {})
        if not isinstance(reply, dict):
            raise PrevisHostError('预演页 getScene 返回的不是对象（' + type(reply).__name__ + '）')
        return reply

    def list_cameras(self) -> list:
        """机位清单（``[{id, name, position, target, focal}]``）。"""
        self.hello()
        reply = self._call('listCameras', {})
        if not isinstance(reply, list):
            raise PrevisHostError('预演页 listCameras 返回的不是数组（' + type(reply).__name__ + '）')
        return reply

    def add_entity(self, spec: dict) -> dict:
        """新增一个实体；返回页面给的回执（含新实体 ``id``）。"""
        row = _entity_spec(spec)
        self.hello()
        reply = _require_ok(self._call('addEntity', row), 'addEntity')
        identifier = reply.get('id')
        if not isinstance(identifier, str) or not identifier.strip():
            raise PrevisHostError('预演页 addEntity 没有返回实体 id；页面可能没把新实体记进场景')
        return reply

    def update_entity(self, entity_id: str, patch: dict) -> dict:
        """局部更新一个实体（``patch`` 是上述字段的子集）。"""
        identifier = _identifier(entity_id, 'entity_id')
        if not isinstance(patch, dict) or not patch:
            raise PrevisHostError('updateEntity 的 patch 须为非空对象；要清空字段请显式给 null')
        self.hello()
        return _require_ok(self._call('updateEntity', {'id': identifier, 'patch': dict(patch)}),
                           'updateEntity')

    def remove_entity(self, entity_id: str) -> dict:
        """删除一个实体。"""
        identifier = _identifier(entity_id, 'entity_id')
        self.hello()
        return _require_ok(self._call('removeEntity', {'id': identifier}), 'removeEntity')

    def set_camera(self, camera_id: str, spec: dict) -> dict:
        """改机位：``{position, target, focal, framing?}``；三项缺一项就定不下画面。"""
        identifier = _identifier(camera_id, 'camera_id')
        row = _camera_spec(spec)
        self.hello()
        return _require_ok(self._call('setCamera', {'id': identifier, 'spec': row}), 'setCamera')

    def set_cuts(self, cuts: list) -> dict:
        """设置时间→机位切换表（``[{time_ms, cameraId}]``，按时间排序后发送）。"""
        rows = _cut_specs(cuts)
        self.hello()
        return _require_ok(self._call('setCuts', {'cuts': rows}), 'setCuts')

    READABILITY_FLAGS = ('persons', 'roomOutlines', 'kindColors', 'labels')

    def set_readability(self, flags: dict) -> dict:
        """开关预演画面的可读性标注（人形标记/房间轮廓/分类配色/名称标签）。

        页面只提供了写、没有读——所以"当前是什么状态"由调用方自己记；
        这里只把给定的键原样下发，未知键直接拒绝（免得拼错一个键却以为生效了）。
        """
        if not isinstance(flags, dict) or not flags:
            raise ValueError('可读性开关要给出至少一项：' + '、'.join(self.READABILITY_FLAGS))
        unknown = [key for key in flags if key not in self.READABILITY_FLAGS and key != 'all']
        if unknown:
            raise ValueError('不认识的可读性开关：' + '、'.join(sorted(unknown))
                             + '（可用：' + '、'.join(self.READABILITY_FLAGS) + '、all）')
        payload = {key: bool(value) for key, value in flags.items()}
        self.hello()
        return _require_ok(self._call('setReadability', payload), 'setReadability')

    def set_time(self, time_ms: int) -> dict:
        """把播放头移到指定时刻（抓帧与预览对齐用）。"""
        value = _millis(time_ms, 'time_ms')
        self.hello()
        return _require_ok(self._call('setTime', {'time_ms': value}), 'setTime')

    def capture_frame(self, path, *, width: int = DEFAULT_FRAME_WIDTH,
                      height: int = DEFAULT_FRAME_HEIGHT, camera_id: str | None = None,
                      time_ms: int | None = None) -> str:
        """按指定机位渲染一帧（离屏，不改播放状态）并存成 PNG，返回写出的路径。

        返回路径而不是 base64：base64 只在传输里存在，落到磁盘以后谁再用谁读文件
        ——否则调用方一不留神就把几十 KB 的字符串塞进聊天记录或日志。
        """
        payload: dict = {'width': _count(width, 'width'), 'height': _count(height, 'height')}
        if camera_id is not None:
            payload['cameraId'] = _identifier(camera_id, 'camera_id')
        if time_ms is not None:
            payload['time_ms'] = _millis(time_ms, 'time_ms')
        self.hello()
        return _write_png(self._call('captureFrame', payload), path)

    def apply_batch(self, ops: list) -> dict:
        """批量事务：全部成功或全部不生效（页面负责原子性，这里只做形状校验）。"""
        rows = _batch_ops(ops)
        self.hello()
        reply = _require_ok(self._call('applyBatch', {'ops': rows}), 'applyBatch')
        results = reply.get('results')
        if results is not None and not isinstance(results, list):
            raise PrevisHostError('预演页 applyBatch 的 results 须为数组（' + type(results).__name__ + '）')
        return reply

    # -- 场景快照与 revision --------------------------------------------------

    def snapshot(self, unit_id: str, *, state_dir=None, source: str = SOURCE_PAGE,
                 bump: bool = False) -> dict:
        """把页面里的场景取成我们自己的结构，并把 revision 落盘。

        为什么每次都要落盘：分镜稿要显示「这一场的布景依赖是不是变了」。revision 记在
        ``previs/<场次>/scene.state.json``：场景内容（机位、盒体、cut、画幅…）的摘要或
        页面自己的修订号变了就自增；没变就原样返回；``bump=True`` 用于「主程序刚写完一批」
        ——即使写回的内容与原来等价，也让它涨一位，界面才知道要重画。

        返回 ``load_scene`` 形状的场景（另加 ``blocks``/``raw``/``revision``/``state``）。
        """
        scene, digest, path, previous, changed = self._compare(unit_id, state_dir)
        if bump or changed:
            revision = int(previous.get('revision') or 0) + 1 if previous else 1
            state = build_state(unit_id, scene, revision, source, digest)
            if path is not None:
                write_state(path, state)
        else:
            revision = int(previous['revision'])
            state = previous
        scene.update({'revision': revision,
                      'revision_source': state.get('source') or source,
                      'state_path': str(path) if path is not None else None,
                      'state': state})
        return scene

    def peek(self, unit_id: str, *, state_dir=None) -> dict:
        """只读探测：取回页面场景，但**一个字节都不写**。

        为什么需要它：``read_scene_set`` 是只读工具（界面/助手都可能在没有写权限的场合
        调它），不该顺手改工程数据。所以这里把「页面里是什么」与「上次入账的快照是什么」
        摊开给调用方：``revision`` 是已入账的那一位（还没入账过就是 0），
        ``pending_change`` 说明页面里的布景与入账快照不一样、``pending_revision`` 是
        真正入账（onChange / 批量写）之后它会变成几。入账由 ``snapshot``/``record_change``
        负责，读的人自己决定。
        """
        scene, digest, path, previous, changed = self._compare(unit_id, state_dir)
        state = build_state(unit_id, scene, 0, SOURCE_PROBE, digest) if previous is None else previous
        revision = int(state.get('revision') or 0)
        scene.update({'revision': revision, 'revision_source': state.get('source') or '',
                      'state_path': str(path) if path is not None else None, 'state': state})
        return {'scene': scene, 'state': state, 'revision': revision,
                'pending_change': bool(changed),
                'pending_revision': revision + 1 if changed else revision,
                'state_path': str(path) if path is not None else None}

    def _compare(self, unit_id: str, state_dir):
        """取页面场景并与入账快照比一比：返回 ``(scene, digest, 路径, 旧状态, 变没变)``。"""
        scene = scene_from_page(unit_id, self.get_scene())
        digest = scene_digest(scene)
        path = self.snapshot_path(unit_id, state_dir)
        previous = read_state(path) if path is not None else None
        # 两个信号任一变了就算变：页面自己的模型修订号（人工在页面上动过），与我们算的
        # 内容摘要（字段级差异，连页面修订号没跳的情形也盖住）。
        changed = (previous is None or previous.get('digest') != digest
                   or previous.get('page_revision') != scene.get('page_revision'))
        return scene, digest, path, previous, changed

    def record_change(self, unit_id: str, *, state_dir=None,
                      source: str = SOURCE_ON_CHANGE) -> dict:
        """页面 ``onChange`` 之后由界面调用：拉一次快照，内容真变了才涨 revision。

        为什么订阅不在 Python 侧：页面的 ``onChange`` 收的是 JS 函数（还限制最多 16 个
        监听器），函数过不了 JSON 通道。所以注册回调这件事必须由 QML / 桥槽位做，回调到了
        再调这里。等价地，任何「主程序确信页面动过」的时刻都可以直接调它。
        """
        return self.snapshot(unit_id, state_dir=state_dir, source=source)

    def snapshot_path(self, unit_id: str, state_dir=None) -> Path | None:
        """快照落盘位置；``state_dir`` 可以是目录，也可以是 ``unit_id -> 目录`` 的函数。"""
        target = state_dir if state_dir is not None else self.state_dir
        if callable(target):
            target = target(unit_id)
        if target is None:
            return None
        return state_file(target)


# -- 场景转换：页面 getScene → load_scene 的形状 --------------------------------

def scene_from_page(unit_id: str, data: object) -> dict:
    """把 ``getScene()`` 的返回值转成 ``previs_scene.load_scene`` 的输出形状。

    形状对齐不是审美问题：``previs_scene.camera_at`` / ``project`` 直接吃这个字典，
    工具层就不必在「页面快照」与「.director 解析」两条路上各算一遍投影。具体对齐：

    * 实体行与机位行的字段名、含义、单位一致（米、y 向上、rotation 弧度、focal 毫米），
      机位按 ``load_scene`` 的约定单独放进 ``cameras``，不进 ``entities``；
    * ``cuts`` 由页面的毫秒换成秒（``load_scene`` 用秒）并按时间排序；
    * ``aspect``/``aspect_ratio``/``duration_s``/``fps``/``scenes`` 同名字段齐备；
    * ``blocks`` 是 ``entities`` 的同一个列表（工具层说「盒体」，投影代码读 ``entities``），
      ``raw`` 留着页面原样，方便排查契约差异。
    """
    if not isinstance(data, dict):
        raise PrevisHostError('预演页 getScene 没有返回场景对象（'
                              + type(data).__name__ + '）；页面可能还没加载完')
    name = str(data.get('name') or unit_id)
    scene_id = str(data.get('sceneId') or data.get('scene_id') or unit_id)
    aspect = str(data.get('aspect') or DEFAULT_ASPECT)
    cameras: list[dict] = []
    entities: list[dict] = []
    lights: list[dict] = []
    for index, row in enumerate(_rows(data.get('entities'), f'场景{scene_id}的实体')):
        parsed = _entity_row(row, index, scene_id)
        if parsed is None:
            continue
        if 'kind' not in parsed:
            lights.append(parsed)          # 灯：有位置没有 kind，单独收（见 _light_row）
            continue
        (cameras if parsed['kind'] == 'camera' else entities).append(parsed)
    cuts, cut_warnings = _cut_rows(data.get('cuts'), {row['id'] for row in cameras}, scene_id)
    scene = {'path': None, 'unit_id': str(unit_id), 'scene_id': scene_id, 'name': name,
             'duration_s': _duration(data.get('duration_ms'), scene_id),
             'fps': _fps(data.get('fps'), scene_id), 'aspect': aspect,
             'aspect_ratio': _scene_aspect_ratio(aspect), 'cameras': cameras,
             'entities': entities, 'lights': lights, 'cuts': cuts, 'cut_warnings': cut_warnings,
             # 页面自己的模型修订号（getScene 给）：人工在页面上拖动会让它跳一格。
             'page_revision': _page_revision(data.get('revision'), scene_id),
             'room': _room(data.get('room')),
             'scenes': [{'id': scene_id, 'name': name}], 'raw': data}
    scene['blocks'] = scene['entities']
    return scene


def scene_digest(scene: dict) -> str:
    """场景内容摘要：只算影响投影/框图的字段，判断 revision 要不要自增。"""
    payload = {'scene_id': scene.get('scene_id'), 'duration_s': scene.get('duration_s'),
               'fps': scene.get('fps'), 'aspect_ratio': scene.get('aspect_ratio'),
               'room': scene.get('room'),
               'cameras': [[row.get(field) for field in _DIGEST_CAMERA_FIELDS]
                           for row in scene.get('cameras') or []],
               'entities': [[row.get(field) for field in _DIGEST_ENTITY_FIELDS]
                            for row in scene.get('entities') or []],
               'cuts': [[row.get('time'), row.get('camera_id')]
                        for row in scene.get('cuts') or []]}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def build_state(unit_id: str, scene: dict, revision: int, source: str, digest: str) -> dict:
    """落盘用的快照摘要：够分镜稿显示依赖（房间、盒体、机位、cut、revision、来源）。"""
    return {'schema': STATE_SCHEMA, 'unit_id': str(unit_id), 'revision': int(revision),
            'source': str(source), 'updated_at': datetime.now(UTC).isoformat(timespec='seconds'),
            'digest': digest, 'page_revision': scene.get('page_revision'),
            'scene_id': scene.get('scene_id') or '',
            'name': scene.get('name') or '', 'duration_s': scene.get('duration_s') or 0.0,
            'fps': scene.get('fps') or 0, 'aspect': scene.get('aspect') or '',
            'aspect_ratio': scene.get('aspect_ratio') or 0.0, 'room': scene.get('room'),
            'counts': {'blocks': len(scene.get('entities') or []),
                       'cameras': len(scene.get('cameras') or []),
                       'cuts': len(scene.get('cuts') or [])},
            'blocks': [{'id': row.get('id'), 'name': row.get('name'), 'kind': row.get('kind')}
                       for row in scene.get('entities') or []],
            'cameras': [{'camera_id': row.get('id'), 'name': row.get('name'),
                         'focal': row.get('focal'), 'position': list(row.get('position') or [])}
                        for row in scene.get('cameras') or []],
            'cuts': [{'time': row.get('time'), 'camera_id': row.get('camera_id')}
                     for row in scene.get('cuts') or []],
            'cut_warnings': list(scene.get('cut_warnings') or [])}


def state_file(directory) -> Path:
    """``previs/<场次>/scene.state.json``：我们自己的快照文件。"""
    return Path(directory) / STATE_FILE


def read_state(path) -> dict | None:
    """读落盘快照；文件不在或读不动就当没有（下一次 snapshot 会重写它）。"""
    if path is None:
        return None
    file = Path(path)
    try:
        data = json.loads(file.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_state(path, state: dict) -> None:
    """原子落盘：先写同目录临时文件再改名，读的人不会看到半份 JSON。"""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(file.name + '.part')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(temporary, file)


# -- 进程内“当前预演页” --------------------------------------------------------

_installed: PrevisHost | None = None


def install(host: PrevisHost | None) -> PrevisHost | None:
    """界面在预演页就绪后把宿主装上；传 None 等于卸载。

    为什么走进程内单例：同一时刻只有一个预演页（界面只有一个），工具层（``previs_tools``）
    拿不到页面对象，只能从这里取。装的是「宿主」而不是页面，测试因此能换成假的。
    """
    global _installed
    _installed = host
    return _installed


def uninstall() -> None:
    """卸载当前宿主（页面关闭、或测试收尾）。"""
    install(None)


def installed() -> PrevisHost | None:
    """当前装着的宿主；没装就 None（工具层据此决定要不要走页面）。"""
    return _installed


# -- QtWebEngine 真实调用器 ----------------------------------------------------

class QtWebEngineCaller:
    """把调用交给 QtWebEngine，并用嵌套事件循环把异步回调折成同步返回。

    ``page`` 可以是 ``QWebEnginePage``，也可以是 QML 的 ``WebEngineView`` 本身
    （``runJavaScript(script, callback)`` 两者都认）；也可以给一个返回它的函数，
    这样页面还没建好时就能先把宿主装好。

    为什么必须嵌套事件循环，而不是 ``sleep`` / ``threading.Event.wait()`` 死等：
    ``runJavaScript`` 的结果要由渲染进程走 IPC 回来，页面里的 Promise ``then``、页面
    自己的定时器、以及 Qt 的元对象调用都排在**主线程的事件循环**上。主线程一旦阻塞，
    这些回调永远送不回来，结果一定是「每次都超时」；而且阻塞期间界面会白屏。
    所以这里 ``QEventLoop.exec()`` 让事件照常分发，只用 ``QTimer`` 管两件事：
    轮询结果（``poll_ms``）与整体超时。等待期间用户可以继续操作，页面也不会僵住。

    为什么还要轮询：``runJavaScript`` 的回调是「脚本执行完」，不是「Promise 兑现」；
    页面把结果写进 ``window.__narrativePrevisReplies[token]``，我们再读回来——读的那一步
    同样得靠 ``runJavaScript`` + 事件循环。
    """

    def __init__(self, page, *, timeout_s: float = DEFAULT_TIMEOUT_S, poll_ms: int = POLL_MS):
        if not timeout_s or float(timeout_s) <= 0:
            raise ValueError('timeout_s 须大于 0')
        self._page = page
        self.timeout_s = float(timeout_s)
        self.poll_ms = max(1, int(poll_ms))
        self._tokens = itertools.count(1)

    def page(self):
        """当前页面对象（``page`` 若是函数则现取一次）。"""
        target = self._page
        return target() if callable(target) else target

    def call(self, method: str, payload: dict) -> Any:
        from PySide6.QtCore import QEventLoop, QTimer

        page = self.page()
        if page is None:
            raise PrevisPageNotReady('预演页还没建好（WebEngineView 未加载），无法调用 ' + method)
        token = str(next(self._tokens))
        state: dict = {'reply': None, 'timed_out': False}
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)

        def finish(reply) -> None:
            state['reply'] = reply
            if loop.isRunning():
                loop.quit()

        def on_timeout() -> None:
            state['timed_out'] = True
            if loop.isRunning():
                loop.quit()

        def poll() -> None:
            if not loop.isRunning():
                return
            page.runJavaScript(_POLL_SCRIPT.replace('__TOKEN__', token), on_poll)

        def on_poll(raw) -> None:
            if not loop.isRunning():
                return
            if isinstance(raw, str) and raw:
                try:
                    finish(json.loads(raw))
                except json.JSONDecodeError:
                    finish({'__narrativePrevisFailed': True,
                            'message': '预演页回复不是合法 JSON：' + raw[:200]})
                return
            QTimer.singleShot(self.poll_ms, poll)

        def on_dispatch(_result=None) -> None:
            # 不能在这里检查 loop 是否在跑：runJavaScript 的回执可能在 loop.exec() 之前
            # 就同步到达（QML 侧或假页面都可能这样），那也该照常安排第一次轮询。
            QTimer.singleShot(0, poll)

        timer.timeout.connect(on_timeout)
        timer.start(int(self.timeout_s * 1000))
        script = (_DISPATCH_SCRIPT
                  .replace('__TOKEN__', json.dumps(token))
                  .replace('__METHOD__', json.dumps(method))
                  .replace('__PAYLOAD__', json.dumps(payload, ensure_ascii=False)))
        self._run_script(page, script, on_dispatch)
        if state['reply'] is None and not state['timed_out']:
            loop.exec()
        timer.stop()
        if state['timed_out'] or state['reply'] is None:
            self._discard(page, token)
            raise PrevisTimeoutError(
                f'等预演页回复 {method} 超时（{self.timeout_s:g} 秒）：页面可能还没加载完、'
                '脚本卡住或预演台已关闭；请刷新预演页后重试')
        reply = state['reply']
        if isinstance(reply, dict) and reply.get('__narrativePrevisNotReady'):
            raise PrevisPageNotReady('预演页还没就绪：'
                                     + str(reply.get('message') or '页面没注入 window.__narrativePrevisHost'))
        if isinstance(reply, dict) and reply.get('__narrativePrevisFailed'):
            raise PrevisHostError('预演页执行 ' + method + ' 时抛错：'
                                  + str(reply.get('message') or '页面没有说原因'))
        if not isinstance(reply, dict):
            raise PrevisHostError('预演页 ' + method + ' 的回执不是对象（' + type(reply).__name__ + '）')
        return reply.get('result')

    @staticmethod
    def _run_script(page, script: str, callback) -> None:
        try:
            page.runJavaScript(script, callback)
        except TypeError:
            # 某些 PySide6 版本把 worldId 夹在中间：runJavaScript(script, worldId, callback)。
            page.runJavaScript(script, 0, callback)

    def _discard(self, page, token: str) -> None:
        """超时后顺手清掉页面里的结果槽：迟到一步的结果不该留在页面里越攒越多。"""
        try:
            self._run_script(page, _DISCARD_SCRIPT.replace('__TOKEN__', token), lambda *_: None)
        except Exception:  # noqa: BLE001 - 清理失败不影响已经确定的超时结论
            return


# 注入页面的三段脚本。注释写在 Python 侧：页面里的脚本越短越不容易被主题注入冲掉。
_DISPATCH_SCRIPT = """
(function () {
  var token = __TOKEN__, method = __METHOD__, payload = __PAYLOAD__;
  var slots = window.__narrativePrevisReplies = window.__narrativePrevisReplies || {};
  function settle(value) { slots[token] = JSON.stringify(value); }
  var host = window.__narrativePrevisHost;
  if (!host) {
    settle({__narrativePrevisNotReady: true,
            message: '页面里没有 window.__narrativePrevisHost（引导脚本没注入，或页面已换）'});
    return true;
  }
  if (typeof host[method] !== 'function') {
    settle({__narrativePrevisNotReady: true,
            message: '预演接口没有 ' + method + ' 这个方法（前端产物版本不对？）'});
    return true;
  }
  try {
    Promise.resolve(host[method](payload)).then(
      function (result) { settle({result: result === undefined ? null : result}); },
      function (error) {
        settle({__narrativePrevisFailed: true,
                message: String((error && error.message) || error)});
      }
    );
  } catch (error) {
    settle({__narrativePrevisFailed: true, message: String((error && error.message) || error)});
  }
  return true;
})();
"""

_POLL_SCRIPT = """
(function () {
  var slots = window.__narrativePrevisReplies || {};
  var raw = slots["__TOKEN__"];
  if (raw === undefined) { return null; }
  delete slots["__TOKEN__"];
  return raw;
})();
"""

_DISCARD_SCRIPT = """
(function () {
  var slots = window.__narrativePrevisReplies;
  if (slots) { delete slots["__TOKEN__"]; }
  return null;
})();
"""


# -- 参数校验 -----------------------------------------------------------------

def _identifier(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PrevisHostError(f'{label} 须为非空文本（实体/机位编号由页面生成）')
    return value.strip()


def _millis(value, label: str) -> int:
    if type(value) is not int or value < 0:
        raise PrevisHostError(f'{label} 须为大于等于 0 的整数毫秒，实际是 {value!r}')
    return value


def _count(value, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise PrevisHostError(f'{label} 须为大于 0 的整数像素，实际是 {value!r}')
    return value


def _require_ok(reply, method: str) -> dict:
    if not isinstance(reply, dict):
        raise PrevisHostError(f'预演页 {method} 返回的不是对象（{type(reply).__name__}）')
    if reply.get('ok') is False:
        raise PrevisHostError(f'预演页 {method} 失败：'
                              + str(reply.get('error') or '页面没有说原因'))
    return reply


def _entity_spec(spec: dict) -> dict:
    """``addEntity`` 的参数：``kind`` 只允许 actor/prop/camera，位置必须给。"""
    if not isinstance(spec, dict):
        raise PrevisHostError('addEntity 的参数须为对象')
    kind = spec.get('kind')
    if kind not in ENTITY_KINDS:
        raise PrevisHostError('addEntity 的 kind 只允许 ' + '/'.join(ENTITY_KINDS)
                              + f'，实际是 {kind!r}')
    row = {key: value for key, value in spec.items() if value is not None}
    row['kind'] = kind
    if spec.get('position') is None:
        raise PrevisHostError('addEntity 缺 position：实体放在哪必须给（米，y 向上）')
    row['position'] = list(_vector(spec.get('position'), 'addEntity 的 position'))
    for key in ('rotation', 'scale'):
        if row.get(key) is not None:
            row[key] = list(_vector(row[key], f'addEntity 的 {key}'))
    if row.get('height') is not None:
        row['height'] = _number(row['height'], 'addEntity 的 height')
    if row.get('camera') is not None:
        row['camera'] = _camera_settings(row['camera'], 'addEntity 的 camera')
    if row.get('name') is not None:
        row['name'] = str(row['name'])
    return row


def _camera_settings(raw, label: str) -> dict:
    if not isinstance(raw, dict):
        raise PrevisHostError(f'{label} 须为对象')
    row = {key: value for key, value in raw.items() if value is not None}
    for key in ('position', 'target'):
        if row.get(key) is not None:
            row[key] = list(_vector(row[key], f'{label}.{key}'))
    if row.get('focal') is not None:
        row['focal'] = _positive(row['focal'], f'{label}.focal')
    return row


def _camera_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise PrevisHostError('setCamera 的 spec 须为对象')
    row = _camera_settings(spec, 'setCamera 的 spec')
    missing = [key for key in ('position', 'target', 'focal') if row.get(key) is None]
    if missing:
        raise PrevisHostError('setCamera 的 spec 缺 ' + '、'.join(missing)
                              + '：这三项缺一项画面朝向/取景就定不下来')
    return row


def _cut_specs(cuts) -> list[dict]:
    if not isinstance(cuts, (list, tuple)) or not cuts:
        raise PrevisHostError('setCuts 需要至少一个切点（time_ms + cameraId）')
    rows = []
    for index, item in enumerate(cuts):
        label = f'setCuts 第{index + 1}项'
        if not isinstance(item, dict):
            raise PrevisHostError(f'{label}须为对象')
        camera_id = _identifier(item.get('cameraId') or item.get('camera_id'), f'{label}的cameraId')
        rows.append({'time_ms': _millis(item.get('time_ms'), f'{label}的time_ms'),
                     'cameraId': camera_id})
    rows.sort(key=lambda row: row['time_ms'])
    return rows


def _batch_ops(ops) -> list[dict]:
    if not isinstance(ops, (list, tuple)) or not ops:
        raise PrevisHostError('applyBatch 需要至少一条操作；空批次没有意义（也不会动场景）')
    rows = []
    for index, item in enumerate(ops):
        label = f'applyBatch 第{index + 1}条操作'
        if not isinstance(item, dict):
            raise PrevisHostError(f'{label}须为对象')
        op = item.get('op')
        if op not in BATCH_OPS:
            raise PrevisHostError(f'{label}的 op 须为 ' + '/'.join(BATCH_OPS) + f'，实际是 {op!r}')
        row = {key: value for key, value in item.items() if value is not None}
        if op in ('update', 'remove', 'setCamera'):
            row['id'] = _identifier(row.get('id'), f'{label}的id')
        if op == 'add':
            if row.get('kind') not in ENTITY_KINDS:
                raise PrevisHostError(f'{label}（add）的 kind 须为 '
                                      + '/'.join(ENTITY_KINDS) + f'，实际是 {row.get("kind")!r}')
            if row.get('position') is None:
                raise PrevisHostError(f'{label}（add）缺 position：实体放在哪必须给（米，y 向上）')
            row['position'] = list(_vector(row.get('position'), f'{label}的position'))
        if op == 'setCamera':
            row['spec'] = _camera_spec(row.get('spec') or {})
        if op == 'setCuts':
            row['cuts'] = _cut_specs(row.get('cuts'))
        rows.append(row)
    return rows


def _write_png(reply, path) -> str:
    """把 ``captureFrame`` 的 base64 解成 PNG 落盘，返回路径。"""
    if not isinstance(reply, dict):
        raise PrevisHostError('预演页 captureFrame 返回的不是对象（' + type(reply).__name__ + '）')
    encoded = reply.get('png_base64')
    if not isinstance(encoded, str) or not encoded.strip():
        fields = '、'.join(sorted(reply)) or '（空对象）'
        raise PrevisHostError('预演页 captureFrame 没有返回 png_base64（只给了：' + fields + '）')
    compact = ''.join(encoded.split())
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as error:
        raise PrevisHostError('预演页 captureFrame 的 png_base64 不是合法 base64：' + str(error)) from error
    if not raw.startswith(PNG_SIGNATURE):
        raise PrevisHostError('预演页 captureFrame 返回的不是 PNG（缺 PNG 文件头）；页面可能回传了别的东西')
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.part')
    temporary.write_bytes(raw)
    os.replace(temporary, target)
    return str(target)


# -- 页面字段解析（形状与 previs_scene 的读取口径一致） -------------------------

def _rows(value, label: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PrevisHostError(f'预演页 {label} 须为数组，实际是 {type(value).__name__}')
    return value


def _entity_row(row, index: int, scene_id: str) -> dict | None:
    """页面实体行 → ``load_scene`` 的行；不参与投影的 kind（分组、占位、灯、隐藏）返回 None。

    灯与隐藏实体必须在这里挡掉：页面 ``getScene`` 的灯是普通 ``prop``，且此前这里丢掉了
    ``asset``，导致灯会被当成盒子投进框图；``visible:false`` 的实体同理。
    灯本身不丢信息——收进 ``_light_row``，交给 ``scene['lights']``（与 ``load_scene`` 同口径）。
    """
    label = f'预演页场景{scene_id}的实体第{index + 1}项'
    if not isinstance(row, dict):
        raise PrevisHostError(f'{label}须为对象')
    kind = row.get('kind')
    if kind not in ENTITY_KINDS:
        return None
    identifier = row.get('id')
    if not isinstance(identifier, str) or not identifier.strip():
        raise PrevisHostError(f'{label}缺少 id（实体编号是框图的 anchor，不能为空）')
    identifier = identifier.strip()
    name = str(row.get('name') or identifier)
    position = _vector(row.get('position'), f'{label}的position')
    rotation = _vector(row.get('rotation'), f'{label}的rotation')
    if kind == 'camera':
        return _camera_row(row, identifier, name, position, rotation, label)
    asset = str(row.get('asset') or '')
    if row.get('visible') is False:
        return None  # 隐藏实体不产生可见画面
    if asset.startswith(SKIP_ASSET_PREFIXES):
        return _light_row(row, identifier, name, asset)
    height = _number(row.get('height'), f'{label}的height', DEFAULT_HEIGHT_M)
    return {'id': identifier, 'name': name, 'kind': kind, 'asset': asset,
            'position': position, 'rotation': rotation,
            'scale': _vector(row.get('scale'), f'{label}的scale', (1.0, 1.0, 1.0)),
            'height': height if height > 0 else DEFAULT_HEIGHT_M,
            'path': _points(row.get('path'), f'{label}的轨迹')}


def _light_row(row: dict, identifier: str, name: str, asset: str) -> dict | None:
    """灯实体 → ``scene['lights']`` 的行。

    页面 ``getScene`` 只给位置（没有强度/色温/聚光角），所以这里只收拿得到的字段，
    其余留 None：``lighting.py`` 遇到缺参数时只说方向与影子，不猜硬软与光比。
    """
    try:
        position = _vector(row.get('position'), f'灯{name}的position')
    except PrevisHostError:
        return None
    light = row.get('light') if isinstance(row.get('light'), dict) else {}
    def number(key):
        value = light.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return {'id': identifier, 'name': name, 'asset': asset, 'position': position,
            'intensity': number('intensity'), 'temperature': number('temperature'),
            'angle': number('angle'), 'range': number('range'), 'color': row.get('color')}


def _camera_row(row: dict, identifier: str, name: str, position: tuple, rotation: tuple,
                label: str) -> dict:
    camera = row.get('camera') if isinstance(row.get('camera'), dict) else {}
    target = camera.get('target')
    return {'id': identifier, 'name': name, 'kind': 'camera', 'position': position,
            'rotation': rotation, 'path': _points(row.get('path'), f'{label}的机位轨迹'),
            'focal': _positive(camera.get('focal', DEFAULT_FOCAL_MM), f'{label}的焦距（毫米）'),
            'focal_keys': _focal_keys(camera.get('effects'), f'{label}的焦距关键帧'),
            'target': _vector(target, f'{label}的注视点') if target is not None else None,
            'target_id': str(camera.get('targetId') or '').strip() or None,
            'target_height': _number(camera.get('targetHeight'), f'{label}的targetHeight', 0.0),
            'target_path': _points(camera.get('targetPath'), f'{label}的视线轨迹'),
            'aim': str(camera.get('aim') or 'target'), 'mode': str(camera.get('mode') or 'free')}


def _focal_keys(effects, label: str) -> list[dict]:
    """焦距关键帧：与 ``load_scene._number_keys`` 同一口径（数值或 ``{keys:[…]}``）。"""
    channels = effects.get('channels') if isinstance(effects, dict) else None
    raw = channels.get('focal') if isinstance(channels, dict) else None
    if raw is None:
        return []
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return [{'time': 0.0, 'value': float(raw), 'easing': None}]
    if not isinstance(raw, dict):
        raise PrevisHostError(f'{label} 须为数值或{{keys:[{{time, value}}]}}对象')
    rows = []
    for index, item in enumerate(_rows(raw.get('keys'), f'{label}.keys')):
        if not isinstance(item, dict):
            raise PrevisHostError(f'{label}.keys第{index + 1}项须为对象')
        rows.append({'time': _number(item.get('time'), f'{label}.keys[{index}].time'),
                     'value': _positive(item.get('value'), f'{label}.keys[{index}].value'),
                     'easing': item.get('easing')})
    rows.sort(key=lambda row: row['time'])
    return rows


def _cut_rows(value, camera_ids: set, scene_id: str) -> tuple[list[dict], list[str]]:
    """页面 cut（毫秒）→ ``load_scene`` 的 cut（秒）。

    指向已不存在机位的 cut 不中断整份快照，而是丢掉并留一句说明：人工在页面上删机位时
    常常还留着旧切点，这时每一次读场景都报错比「这份 cut 已失效」更难用。
    """
    rows, warnings = [], []
    for index, item in enumerate(_rows(value, f'场景{scene_id}的cuts')):
        if not isinstance(item, dict):
            raise PrevisHostError(f'场景{scene_id}的cut第{index + 1}项须为对象')
        time_s = _cut_time(item, index, scene_id)
        camera_id = item.get('cameraId') or item.get('camera_id')
        if camera_id not in camera_ids:
            warnings.append(f'cut@{time_s:g}s 指向不存在的机位{camera_id!r}，已忽略')
            continue
        rows.append({'time': time_s, 'camera_id': camera_id})
    rows.sort(key=lambda row: row['time'])
    return rows, warnings


def _cut_time(item: dict, index: int, scene_id: str) -> float:
    if item.get('time_ms') is not None:
        return _number(item['time_ms'], f'场景{scene_id}的cut第{index + 1}项时间（毫秒）') / 1000
    if item.get('time') is not None:
        return _number(item['time'], f'场景{scene_id}的cut第{index + 1}项时间')
    raise PrevisHostError(f'场景{scene_id}的cut第{index + 1}项缺少 time_ms')


def _duration(value, scene_id: str) -> float:
    """页面只给毫秒时长；缺了就记 0（表示未知），时间越界检查由工具层按 >0 判断。"""
    if value is None:
        return 0.0
    return max(0.0, _number(value, f'场景{scene_id}的时长（毫秒）') / 1000)


def _fps(value, scene_id: str) -> int:
    if value is None:
        return 0
    if type(value) is not int or not 1 <= value <= 120:
        raise PrevisHostError(f'场景{scene_id}的帧率须为 1..120 的整数，实际是 {value!r}')
    return value


def _room(value) -> dict | None:
    """房间尺寸（契约 v1 没定义，页面愿意给就收下）；读不出就不当它有。"""
    if not isinstance(value, dict):
        return None
    try:
        room = {'width': _positive(value.get('width'), 'room.width'),
                'depth': _positive(value.get('depth'), 'room.depth'),
                'height': _positive(value.get('height'), 'room.height')}
    except PrevisHostError:
        return None
    if isinstance(value.get('enabled'), bool):
        room['enabled'] = value['enabled']
    return room


def _points(raw, label: str) -> list[dict]:
    """轨迹点 → ``[{time(秒), position, easing}]``。

    页面的轨迹点时间是**毫秒**（``time_ms``），``.director`` 里是秒（``time``）；这里统一
    换算成秒，与 ``load_scene`` 的插值口径一致（否则同一条轨迹两条路差一千倍）。
    """
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise PrevisHostError(f'{label}须为{{smooth, points:[{{time_ms, position}}]}}对象')
    rows = []
    for index, item in enumerate(_rows(raw.get('points'), f'{label}.points')):
        if not isinstance(item, dict):
            raise PrevisHostError(f'{label}.points第{index + 1}项须为对象')
        rows.append({'time': _point_time(item, index, label),
                     'position': _vector(item.get('position'), f'{label}.points[{index}].position'),
                     'easing': item.get('easing')})
    rows.sort(key=lambda row: row['time'])
    return rows


def _point_time(item: dict, index: int, label: str) -> float:
    if item.get('time_ms') is not None:
        return _number(item['time_ms'], f'{label}.points[{index}].time_ms') / 1000
    if item.get('time') is not None:
        return _number(item['time'], f'{label}.points[{index}].time')
    raise PrevisHostError(f'{label}.points[{index}]缺少 time_ms')


def _page_revision(value, scene_id: str) -> int | None:
    """页面自己的模型修订号：只当「场景变过」的信号用，不参与投影；缺了就不比较。"""
    if value is None:
        return None
    if type(value) is not int:
        raise PrevisHostError(f'场景{scene_id}的revision须为整数，实际是 {value!r}')
    return value


def _number(value, label: str, default=None) -> float:
    if value is None:
        if default is None:
            raise PrevisHostError(f'预演页数据缺 {label}')
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PrevisHostError(f'{label}须为有限数值，实际是 {value!r}')
    return float(value)


def _positive(value, label: str) -> float:
    result = _number(value, label)
    if not result > 0:
        raise PrevisHostError(f'{label}须大于 0，实际是 {value!r}')
    return result


def _vector(value, label: str, default=(0.0, 0.0, 0.0)) -> tuple:
    if value is None:
        return tuple(float(item) for item in default)
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PrevisHostError(f'{label}须为[x, y, z]三元数组，实际是 {value!r}')
    return tuple(_number(item, f'{label}[{index}]') for index, item in enumerate(value))
