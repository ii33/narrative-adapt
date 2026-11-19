"""预演页接口层的 Python 侧：调用器抽象、快照/revision、三个工具的接线。

默认不用 Qt：页面是一个**假调用器**（``FakePage``），它自己维护一张场景表、按契约方法
增删改，所以能断言「发了哪些调用、参数是什么、失败时快照有没有动」。只有 ``TestQtCaller``
借了 ``QtCore``（不需要 QtWebEngine）来验证「异步回调折成同步等待 + 超时」这段机制本身。
真起页面的端到端用例在文件末尾，标 slow，环境不具备就 skip。

钉住的行为：

* 每个契约方法只发一次调用，参数信封是「方法名 + 一个 JSON 对象」（数组包一层键名）；
* ``captureFrame`` 把 base64 解成 PNG 落盘，返回值是路径而不是 base64；
* ``protocol`` 不匹配、页面回 ``{error}``、调用器超时/未就绪：三种错误的消息互不相同；
* ``snapshot`` 的形状与 ``previs_scene.load_scene`` 对齐（同一份数据两条路，投影逐项相等）；
* ``set_scene_layout`` 只发一批；批量失败时快照与 revision 一个字都不动；
* 机位在页面布景里被删掉后，抓帧给出「已不存在，请重新选择」而不是内部错误。
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from test_previs_capture import revision, shot_with_keyframes
from test_previs_scene import ASPECT, actor, camera, flat, prop, written
from test_screenplay_writing import project as project
from test_storyboard_operations import setup_board

from narrative import previs_host
from narrative.previs_host import (
    PrevisHost,
    PrevisHostError,
    PrevisPageNotReady,
    PrevisTimeoutError,
)
from narrative.previs_scene import camera_at, load_scene
from narrative.previs_scene import project as project_scene
from narrative.project_package import layout

CAMERA_ID = 'cam-a'
# 与 load_scene 对齐时逐字段比的键：两条路必须给出同一套名字与单位。
BLOCK_KEYS = ('id', 'name', 'kind', 'position', 'rotation', 'scale', 'height', 'path')
CAMERA_KEYS = ('id', 'name', 'kind', 'position', 'rotation', 'path', 'focal', 'focal_keys',
               'target', 'target_id', 'target_height', 'target_path', 'aim', 'mode')


# -- 假页面 -------------------------------------------------------------------

def png_base64():
    """一张真 PNG 的 base64：宿主必须写出能读回来的文件，不能只是写点字节。"""
    buffer = io.BytesIO()
    Image.new('RGB', (4, 3), 'gray').save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode('ascii')


def page_scene(entities, *, cuts=None, name='单场景', duration_ms=10000, fps=24,
               aspect=ASPECT, scene_id='scene', revision=0, room=None):
    """把 ``test_previs_scene`` 造的实体行翻成页面 ``getScene()`` 的返回。

    形状照随工程打包的页面实现（``host-scene`` 的 ``sceneSnapshot``）：毫秒时长、
    ``revision``、``room``、轨迹点用 ``time_ms``、cut 用 ``{time_ms, cameraId}``。
    """
    rows = [_page_entity(row) for row in entities]
    cameras = [row['id'] for row in entities if row['kind'] == 'camera']
    if cuts is None:
        cuts = [{'time_ms': 0, 'cameraId': cameras[0]}] if cameras else []
    scene = {'sceneId': scene_id, 'revision': revision, 'name': name,
             'duration_ms': duration_ms, 'fps': fps, 'aspect': aspect,
             'entities': rows, 'cuts': cuts}
    if room is not None:
        scene['room'] = dict(room)
    return scene


def _page_entity(row):
    entity = {'id': row['id'], 'kind': row['kind'], 'name': row['name'], 'visible': True,
              'position': list(row['position']), 'rotation': list(row['rotation']),
              'scale': list(row['scale']), 'height': row.get('height') or 1.75,
              'path': _page_path(row.get('path'))}
    if row['kind'] == 'camera':
        settings = row['camera']
        entity['camera'] = {'focal': settings['focal'], 'target': list(settings['target']),
                            'aim': settings.get('aim', 'target'),
                            'mode': settings.get('mode', 'free'),
                            'targetId': settings.get('targetId') or '',
                            'targetHeight': settings.get('targetHeight', 0)}
    return entity


def _page_path(raw):
    """``.director`` 的轨迹（秒）→ 页面的轨迹（毫秒）。"""
    if not raw:
        return None
    return {'smooth': raw.get('smooth'),
            'points': [{'time_ms': round(row['time'] * 1000), 'position': list(row['position'])}
                       for row in raw['points']]}


class FakePage:
    """假页面：一张场景表 + 契约方法。

    只实现被用到的那部分语义：``add`` 分配新编号、``update`` 合并 patch、``remove`` 删实体、
    ``setCamera`` 改机位、``setCuts`` 换 cut 表；批量里任一条失败就整批不生效（与契约一致）。
    它同时是调用器：``call(method, payload)`` 就是宿主拿到的抽象。
    """

    def __init__(self, entities, cuts=None, **scene_fields):
        self.scene = page_scene(entities, cuts=cuts, **scene_fields)
        self.protocol = scene_fields.pop('protocol', 1)
        self.calls = []
        self.sequences = []       # 每一批 applyBatch 收到的 ops
        self.batch_error = None   # 设了文本：下一批直接回 {error}（模拟整批不生效）
        self.failure = None       # 设了异常：下一次调用直接抛（模拟超时/页面已换）
        self._counter = 0

    # -- 调用器接口 -------------------------------------------------------
    def call(self, method, payload):
        self.calls.append((method, payload))
        if self.failure is not None:
            raise self.failure
        handler = {'hello': self._hello, 'getScene': self._get_scene,
                   'listCameras': self._list_cameras, 'addEntity': self._add,
                   'updateEntity': self._update, 'removeEntity': self._remove,
                   'setCamera': self._set_camera, 'setCuts': self._set_cuts,
                   'setTime': lambda payload: {'ok': True},
                   'captureFrame': self._capture,
                   'onChange': lambda payload: {'ok': True},
                   'applyBatch': self._apply_batch}.get(method)
        if handler is None:
            return {'error': f'没有方法 {method}'}
        return handler(payload)

    def methods(self):
        return [name for name, _ in self.calls]

    def payload(self, method):
        return next(payload for name, payload in self.calls if name == method)

    # -- 契约方法 ---------------------------------------------------------
    def _hello(self, payload):
        return {'ok': True, 'appVersion': 'test-1.0', 'protocol': self.protocol}

    def _get_scene(self, payload):
        return json.loads(json.dumps(self.scene))

    def _list_cameras(self, payload):
        return [{'id': row['id'], 'name': row['name'],
                 'position': row['position'],
                 'target': (row.get('camera') or {}).get('target'),
                 'focal': (row.get('camera') or {}).get('focal')}
                for row in self.scene['entities'] if row['kind'] == 'camera']

    def _add(self, payload):
        self._counter += 1
        return {'id': self._append(self.scene, payload)}

    def _update(self, payload):
        target = self._find(payload.get('id'))
        if target is None:
            return {'error': f'实体{payload.get("id")}不存在'}
        target.update({key: value for key, value in (payload.get('patch') or {}).items()
                       if key != 'id'})
        return {'ok': True}

    def _remove(self, payload):
        target = self._find(payload.get('id'))
        if target is None:
            return {'error': f'实体{payload.get("id")}不存在'}
        self.scene['entities'].remove(target)
        return {'ok': True}

    def _set_camera(self, payload):
        target = self._find(payload.get('id'))
        if target is None or target.get('kind') != 'camera':
            return {'error': f'{payload.get("id")}不是机位'}
        spec = payload.get('spec') or {}
        target['position'] = list(spec.get('position') or target.get('position'))
        target['camera'] = dict(target.get('camera') or {}) | {
            key: value for key, value in spec.items() if key != 'position'}
        return {'ok': True}

    def _set_cuts(self, payload):
        self.scene['cuts'] = [{'time_ms': item['time_ms'], 'cameraId': item['cameraId']}
                              for item in payload.get('cuts') or []]
        return {'ok': True}

    def _capture(self, payload):
        return {'png_base64': png_base64()}

    def _apply_batch(self, payload):
        if self.batch_error:
            return {'error': self.batch_error}
        ops = payload.get('ops')
        if not isinstance(ops, list) or not ops:
            return {'error': 'applyBatch 需要至少一条操作'}
        staged = json.loads(json.dumps(self.scene))
        results = []
        for index, row in enumerate(ops, start=1):
            error = self._apply_op(staged, row, results)
            if error:
                return {'error': f'第{index}条操作不生效：{error}；整批已回滚'}
        staged['revision'] = int(staged.get('revision') or 0) + 1   # 页面的模型修订号也会跳
        self.scene = staged
        self.sequences.append(ops)
        return {'ok': True, 'results': results}

    def _apply_op(self, scene, row, results):
        op = row.get('op')
        if op == 'add':
            results.append({'id': self._append(scene, row)})
            return None
        if op == 'update':
            target = self._find(row.get('id'), scene)
            if target is None:
                return f'实体{row.get("id")}不存在'
            target.update({key: value for key, value in (row.get('patch') or {}).items()
                           if key != 'id'})
            results.append({'ok': True})
            return None
        if op == 'remove':
            target = self._find(row.get('id'), scene)
            if target is None:
                return f'实体{row.get("id")}不存在'
            scene['entities'].remove(target)
            results.append({'ok': True})
            return None
        if op == 'setCamera':
            target = self._find(row.get('id'), scene)
            if target is None or target.get('kind') != 'camera':
                return f'{row.get("id")}不是机位'
            spec = row.get('spec') or {}
            target['position'] = list(spec.get('position') or target.get('position'))
            target['camera'] = dict(target.get('camera') or {}) | {
                key: value for key, value in spec.items() if key != 'position'}
            results.append({'ok': True})
            return None
        if op == 'setCuts':
            scene['cuts'] = [{'time_ms': item['time_ms'], 'cameraId': item['cameraId']}
                             for item in row.get('cuts') or []]
            results.append({'ok': True})
            return None
        return f'不认识的op {op!r}'

    def _append(self, scene, spec):
        self._counter += 1
        identifier = f'gen-{self._counter}'
        entity = {'id': identifier, 'kind': spec.get('kind'),
                  'name': spec.get('name') or identifier,
                  'position': list(spec.get('position') or [0, 0, 0]),
                  'rotation': list(spec.get('rotation') or [0, 0, 0]),
                  'scale': list(spec.get('scale') or [1, 1, 1]),
                  'height': spec.get('height') or 1.75}
        if spec.get('camera') is not None:
            entity['camera'] = dict(spec['camera'])
        scene['entities'].append(entity)
        return identifier

    def _find(self, identifier, scene=None):
        rows = (scene or self.scene)['entities']
        return next((row for row in rows if row['id'] == identifier), None)


class StubCaller:
    """只记调用、答固定结果的调用器：钉「请求/返回映射」与错误语义时用它。"""

    def __init__(self, **replies):
        self.replies = {'hello': {'ok': True, 'protocol': 1, 'appVersion': 'stub'}, **replies}
        self.calls = []

    def call(self, method, payload):
        self.calls.append((method, payload))
        reply = self.replies.get(method)
        if isinstance(reply, BaseException):
            raise reply
        return {'ok': True} if reply is None else reply


@pytest.fixture(autouse=True)
def _no_leftover_host():
    """进程内宿主是单例：每个用例前后都清掉，免得互相串。"""
    previs_host.uninstall()
    yield
    previs_host.uninstall()


def install(host):
    """把宿主装成「当前预演页」，返回它方便链式写。"""
    return previs_host.install(host)


# -- 握手与错误语义 ------------------------------------------------------------

def test_hello_checks_the_protocol_and_says_how_to_fix_it():
    host = PrevisHost(StubCaller(hello={'ok': True, 'appVersion': '3.2', 'protocol': 2}))
    with pytest.raises(PrevisHostError) as error:
        host.hello()
    message = str(error.value)
    assert '版本不匹配' in message
    assert 'protocol=2' in message and 'protocol=1' in message
    assert 'dist' in message          # 告诉人怎么修，不是只报「不匹配」


def test_hello_is_asked_once_per_session():
    caller = StubCaller(getScene={'sceneId': 's1', 'entities': [], 'cuts': []})
    host = PrevisHost(caller)
    assert host.hello()['appVersion'] == 'stub'
    host.hello()
    host.get_scene()
    assert caller.calls == [('hello', {}), ('getScene', {})]


def test_page_error_becomes_a_readable_error():
    host = PrevisHost(StubCaller(getScene={'error': '场景还在加载，请稍后再试'}))
    with pytest.raises(PrevisHostError, match='场景还在加载'):
        host.get_scene()


def test_timeout_and_not_ready_say_different_things():
    timed_out = PrevisHost(StubCaller(getScene=TimeoutError('qt 超时')))
    with pytest.raises(PrevisTimeoutError) as timeout:
        timed_out.get_scene()
    assert '超时' in str(timeout.value) and 'getScene' in str(timeout.value)

    missing = PrevisHost(StubCaller(getScene=PrevisPageNotReady('预演页还没就绪：页面已换')))
    with pytest.raises(PrevisPageNotReady) as not_ready:
        missing.get_scene()
    assert '就绪' in str(not_ready.value) and '超时' not in str(not_ready.value)
    assert str(not_ready.value) != str(timeout.value)
    # 调用方只需要 catch 一个类型，但仍能分辨具体是哪一种。
    assert issubclass(PrevisTimeoutError, PrevisHostError)
    assert issubclass(PrevisPageNotReady, PrevisHostError)


def test_unexpected_caller_failure_is_wrapped_with_the_method_name():
    host = PrevisHost(StubCaller(getScene=RuntimeError('渲染进程崩了')))
    with pytest.raises(PrevisHostError, match='getScene'):
        host.get_scene()


# -- 方法映射 -----------------------------------------------------------------

def test_every_contract_method_maps_to_one_page_call(tmp_path):
    caller = StubCaller(getScene={'sceneId': 's1', 'entities': [], 'cuts': []},
                        listCameras=[{'id': CAMERA_ID}], addEntity={'id': 'gen-1'},
                        captureFrame={'png_base64': png_base64()})
    host = PrevisHost(caller)
    assert host.hello()['protocol'] == 1
    assert host.get_scene()['sceneId'] == 's1'
    assert host.list_cameras() == [{'id': CAMERA_ID}]
    assert host.add_entity({'kind': 'prop', 'name': '墙', 'position': [0, 0, -2]}) == {'id': 'gen-1'}
    host.update_entity('gen-1', {'height': 2.8})
    host.remove_entity('gen-1')
    host.set_camera(CAMERA_ID, {'position': [0, 1.6, 4], 'target': [0, 1.2, 0], 'focal': 50})
    host.set_cuts([{'time_ms': 2000, 'cameraId': 'cam-b'}, {'time_ms': 0, 'cameraId': CAMERA_ID}])
    host.set_time(1500)
    frame = host.capture_frame(tmp_path / 'frame.png', width=640, height=360,
                              camera_id=CAMERA_ID, time_ms=500)
    host.apply_batch([{'op': 'remove', 'id': 'gen-1'}])
    assert Path(frame).is_file()
    # 一次调用一个方法；数组参数包一层键名（applyBatch/setCuts），两个参数包 {id, patch/spec}。
    assert caller.calls == [
        ('hello', {}),
        ('getScene', {}),
        ('listCameras', {}),
        ('addEntity', {'kind': 'prop', 'name': '墙', 'position': [0.0, 0.0, -2.0]}),
        ('updateEntity', {'id': 'gen-1', 'patch': {'height': 2.8}}),
        ('removeEntity', {'id': 'gen-1'}),
        ('setCamera', {'id': CAMERA_ID, 'spec': {'position': [0.0, 1.6, 4.0],
                                                 'target': [0.0, 1.2, 0.0], 'focal': 50.0}}),
        ('setCuts', {'cuts': [{'time_ms': 0, 'cameraId': CAMERA_ID},
                              {'time_ms': 2000, 'cameraId': 'cam-b'}]}),
        ('setTime', {'time_ms': 1500}),
        ('captureFrame', {'width': 640, 'height': 360, 'cameraId': CAMERA_ID, 'time_ms': 500}),
        ('applyBatch', {'ops': [{'op': 'remove', 'id': 'gen-1'}]}),
    ]


def test_capture_frame_decodes_the_png_and_returns_only_the_path(tmp_path):
    host = PrevisHost(StubCaller(captureFrame={'png_base64': png_base64()}))
    path = host.capture_frame(tmp_path / 'frames' / 'shot-1.png')
    assert isinstance(path, str) and path.endswith('shot-1.png')
    assert Path(path).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    with Image.open(path) as image:
        assert image.size == (4, 3)      # 真能读回来，不是写了几百个字节就算
    assert 'base64' not in path


def test_capture_frame_refuses_a_payload_that_is_not_a_png(tmp_path):
    bad_base64 = PrevisHost(StubCaller(
        captureFrame={'png_base64': base64.b64encode(b'not a png').decode('ascii')}))
    target = tmp_path / 'x.png'
    with pytest.raises(PrevisHostError, match='PNG'):
        bad_base64.capture_frame(target)
    assert not target.exists()           # 坏图不落盘，别留一个打不开的文件
    empty = PrevisHost(StubCaller(captureFrame={'ok': True}))
    with pytest.raises(PrevisHostError, match='png_base64'):
        empty.capture_frame(target)


def test_batch_ops_are_validated_before_the_page_sees_them():
    host = PrevisHost(StubCaller())
    with pytest.raises(PrevisHostError, match='至少一条操作'):
        host.apply_batch([])
    with pytest.raises(PrevisHostError, match='op'):
        host.apply_batch([{'op': 'dropEverything'}])
    with pytest.raises(PrevisHostError, match='position'):
        host.apply_batch([{'op': 'add', 'kind': 'prop'}])


# -- 快照与 revision -----------------------------------------------------------

def test_snapshot_has_the_same_shape_as_load_scene(tmp_path):
    """同一份数据分别从 .director 与页面进来：字段与投影必须逐项一致。"""
    entities = [actor(), prop(), camera()]
    from_file = load_scene(written(tmp_path, flat(entities)))
    host = PrevisHost(FakePage(entities))
    snapshot = host.snapshot('unit-1')
    # blocks 是 entities 的同一个列表：工具层说「盒体」，投影代码读 entities。
    assert snapshot['blocks'] is snapshot['entities']
    assert [row['id'] for row in snapshot['blocks']] == [row['id'] for row in from_file['entities']]
    for snap_row, file_row in zip(snapshot['blocks'], from_file['entities'], strict=True):
        assert {key: snap_row[key] for key in BLOCK_KEYS} == {key: file_row[key] for key in BLOCK_KEYS}
    assert [row['id'] for row in snapshot['cameras']] == [row['id'] for row in from_file['cameras']]
    for snap_row, file_row in zip(snapshot['cameras'], from_file['cameras'], strict=True):
        assert {key: snap_row[key] for key in CAMERA_KEYS} == {key: file_row[key] for key in CAMERA_KEYS}
    # 场景级字段：单位换算（毫秒→秒）与画幅解析是同一个口径。
    assert snapshot['cuts'] == from_file['cuts'] == [{'time': 0.0, 'camera_id': CAMERA_ID}]
    assert snapshot['duration_s'] == from_file['duration_s'] == 10.0
    assert snapshot['fps'] == from_file['fps'] == 24
    assert snapshot['aspect'] == from_file['aspect'] == ASPECT
    assert snapshot['aspect_ratio'] == from_file['aspect_ratio']
    assert snapshot['scene_id'] == from_file['scene_id'] == 'scene'
    assert snapshot['name'] == from_file['name'] == '单场景'
    assert snapshot['raw']['sceneId'] == 'scene'          # 页面原样留着，排查契约差异用
    # 投影代码不分叉：同一时刻、同一机位，两条路给出同一份框图标量。
    for time_s in (0.0, 1.5):
        file_camera = camera_at(from_file, time_s, CAMERA_ID)
        snap_camera = camera_at(snapshot, time_s, CAMERA_ID)
        assert snap_camera == file_camera
        assert (project_scene(snapshot, snap_camera, time_s, snapshot['aspect'])
                == project_scene(from_file, file_camera, time_s, from_file['aspect']))


def test_snapshot_converts_trajectory_milliseconds_to_seconds(tmp_path):
    """页面轨迹点用毫秒、``.director`` 用秒：快照统一成秒，否则同一条轨迹差一千倍。"""
    route = {'smooth': False, 'points': [{'time': 0, 'position': [0, 0, 0]},
                                        {'time': 2, 'position': [2.0, 0, -1.0]}]}
    entities = [actor(path=route), camera(position=(0.0, 1.6, 6.0), target=(0.0, 1.2, 0.0))]
    from_file = load_scene(written(tmp_path, flat(entities, duration=4)))
    snapshot = PrevisHost(FakePage(entities)).snapshot('unit-1')
    assert snapshot['blocks'][0]['path'] == from_file['entities'][0]['path']
    assert [row['time'] for row in snapshot['blocks'][0]['path']] == [0.0, 2.0]
    # 轨迹插值也一致：1 秒处两条路取到同一点，投出来的框图逐项相等。
    file_camera = camera_at(from_file, 1.0, CAMERA_ID)
    snap_camera = camera_at(snapshot, 1.0, CAMERA_ID)
    assert (project_scene(snapshot, snap_camera, 1.0, snapshot['aspect'])
            == project_scene(from_file, file_camera, 1.0, from_file['aspect']))


def test_snapshot_persists_a_revision_that_only_moves_when_the_scene_does(tmp_path):
    caller = FakePage([actor(), camera()])
    host = PrevisHost(caller)
    state_file = tmp_path / 'scene.state.json'
    first = host.snapshot('unit-1', state_dir=tmp_path)
    assert first['revision'] == 1 and first['revision_source'] == 'page'
    state = json.loads(state_file.read_text(encoding='utf-8'))
    assert state['revision'] == 1 and state['source'] == 'page'
    assert state['digest'] == first['state']['digest']
    assert state['counts'] == {'blocks': 1, 'cameras': 1, 'cuts': 1}
    assert state['schema'] == previs_host.STATE_SCHEMA
    assert state['page_revision'] == 0            # 页面自己的模型修订号也记着
    # 页面没变：revision 不动，也不重写文件（人工只是在页面上看了看）。
    stamp = state_file.stat().st_mtime_ns
    assert host.snapshot('unit-1', state_dir=tmp_path)['revision'] == 1
    assert state_file.stat().st_mtime_ns == stamp
    # 页面上挪了机位（人工拖动或 onChange）：内容摘要变了，revision 自增。
    caller = FakePage([actor(), camera(position=(1.0, 0.875, 5.0))])
    host = PrevisHost(caller)
    moved = host.snapshot('unit-1', state_dir=tmp_path)
    assert moved['revision'] == 2
    assert json.loads(state_file.read_text(encoding='utf-8'))['revision'] == 2
    # 主程序刚写完一批：即使内容等价也涨一位，界面据此知道要重画。
    bumped = host.snapshot('unit-1', state_dir=tmp_path, source='apply_batch', bump=True)
    assert bumped['revision'] == 3
    assert json.loads(state_file.read_text(encoding='utf-8'))['source'] == 'apply_batch'
    # 页面自己的模型修订号跳了（人工在页面上动过）：内容摘要没变也算变过。
    caller.scene['revision'] = 9
    assert host.snapshot('unit-1', state_dir=tmp_path)['revision'] == 4
    # record_change 就是「onChange 之后拉一次快照」：内容没变不涨。
    assert host.record_change('unit-1', state_dir=tmp_path)['revision'] == 4


def test_snapshot_without_a_state_dir_still_works(tmp_path):
    """没接工程目录时不落盘：revision 只在本内存里记一位，文件一个都不写。"""
    host = PrevisHost(FakePage([actor(), camera()]))
    snapshot = host.snapshot('unit-1')
    assert snapshot['revision'] == 1 and snapshot['state_path'] is None
    assert not list(tmp_path.iterdir())


def test_snapshot_drops_a_cut_that_points_at_a_missing_camera():
    """页面上删了机位还留着旧切点：丢掉那一条并说明，不要每读一次场景就报错。"""
    host = PrevisHost(FakePage([actor()], cuts=[{'time_ms': 0, 'cameraId': 'cam-gone'}]))
    snapshot = host.snapshot('unit-1')
    assert snapshot['cuts'] == []
    assert snapshot['cut_warnings'] and 'cam-gone' in snapshot['cut_warnings'][0]


# -- 工具接线：set_scene_layout ------------------------------------------------

@pytest.mark.slow
def test_set_scene_layout_sends_exactly_one_batch_and_bumps_the_revision(project):
    ops, unit, _block = setup_board(project)
    page = FakePage([actor(), camera()])
    host = install(PrevisHost(page))
    assert host.snapshot(unit)['revision'] == 1
    result = ops.set_scene_layout(
        unit,
        blocks=[{'kind': 'prop', 'name': '墙', 'position': [0, 0, -3], 'scale': [4, 2.8, 0.2]}],
        cameras=[{'id': CAMERA_ID, 'name': 'A · 主机位', 'position': [0, 1.6, 4],
                  'target': [0, 1.2, 0], 'focal': 50}])
    assert len(page.sequences) == 1                     # 一次 applyBatch，没有中间态
    sent = page.sequences[0]
    # replace=True：没再出现的旧演员被删，新盒体是 add，留着的机位走 setCamera。
    assert [row['op'] for row in sent] == ['remove', 'add', 'setCamera']
    assert sent[0]['id'] == 'act-a'
    assert sent[1]['kind'] == 'prop' and sent[1]['name'] == '墙'
    assert sent[2]['id'] == CAMERA_ID and sent[2]['spec']['focal'] == 50.0
    assert result['removed'] == ['act-a']
    assert result['revision'] == 2 and result['snapshot_source'] == 'page'
    assert [row['id'] for row in result['blocks']] == ['gen-1']
    assert result['cameras'][0]['camera_id'] == CAMERA_ID
    assert result['cameras'][0]['focal'] == 50.0
    assert result['warnings'] == []
    # 快照落盘了：分镜稿读到的 revision 与返回值一致。
    assert ops.read_scene_set(unit)['revision'] == 2
    assert (ops._previs_dir(unit) / 'scene.state.json').is_file()


@pytest.mark.slow
def test_replace_false_only_adds_and_updates(project):
    ops, unit, _block = setup_board(project)
    page = FakePage([actor(), camera()])
    install(PrevisHost(page))
    result = ops.set_scene_layout(unit, blocks=[{'kind': 'prop', 'name': '椅子',
                                                'position': [1, 0, 0]}],
                                  cameras=[], replace=False)
    assert [row['op'] for row in page.sequences[0]] == ['add']
    assert 'act-a' not in result['removed']
    assert {row['kind'] for row in result['blocks']} == {'actor', 'prop'}


@pytest.mark.slow
def test_a_failed_batch_leaves_the_snapshot_and_revision_untouched(project):
    ops, unit, _block = setup_board(project)
    page = FakePage([actor(), camera()])
    host = install(PrevisHost(page))
    host.record_change(unit, state_dir=ops._previs_dir(unit))   # 先入账一份快照
    before = ops.read_scene_set(unit)                            # revision=1、无未入账改动
    state_file = ops._previs_dir(unit) / 'scene.state.json'
    raw_before = state_file.read_text(encoding='utf-8')
    page.batch_error = '第2条操作不生效：实体gen-9不存在；整批已回滚'
    with pytest.raises(ValueError, match='第2条操作'):
        ops.set_scene_layout(unit,
                             blocks=[{'kind': 'prop', 'name': '墙', 'position': [0, 0, -3]}],
                             cameras=[{'id': CAMERA_ID, 'position': [0, 1.6, 4],
                                       'target': [0, 1.2, 0], 'focal': 40}])
    # 页面没应用任何一批；快照文件一个字都没动；读回来的 revision 还是原来那位。
    assert page.sequences == []
    assert state_file.read_text(encoding='utf-8') == raw_before
    assert ops.read_scene_set(unit)['revision'] == before['revision'] == 1
    # 失败的批次不该留下半份新布景：页面里还是原来那一演员一机位。
    assert [row['id'] for row in page.scene['entities']] == ['act-a', CAMERA_ID]


@pytest.mark.slow
def test_set_scene_layout_needs_an_open_previs_page(project):
    ops, unit, _block = setup_board(project)
    with pytest.raises(ValueError, match='预演台还没打开'):
        ops.set_scene_layout(unit, blocks=[], cameras=[])


@pytest.mark.slow
def test_room_is_checked_but_cannot_be_written(project):
    """契约 v1 的 applyBatch 没有「改房间」这一条：尺寸不同就整批拒绝，不假装改成功。"""
    ops, unit, _block = setup_board(project)
    page = FakePage([actor(), camera()])
    page.scene['room'] = {'width': 4.8, 'depth': 4.2, 'height': 2.8}
    install(PrevisHost(page))
    with pytest.raises(ValueError, match='没有「改房间」的操作'):
        ops.set_scene_layout(unit, blocks=[], cameras=[],
                             room={'width': 8.0, 'depth': 6.0, 'height': 3.0})
    assert page.sequences == []
    # 与页面一致时不拦：房间本来就对，没什么可改的。
    result = ops.set_scene_layout(unit, blocks=[], cameras=[],
                                  room={'width': 4.8, 'depth': 4.2, 'height': 2.8})
    assert result['room'] == {'width': 4.8, 'depth': 4.2, 'height': 2.8}


@pytest.mark.slow
def test_framing_is_translated_to_the_page_presets(project):
    """页面 setCamera 的 framing 是「全景/中景/近景/特写」，框图口径是 wide/medium/close。"""
    ops, unit, _block = setup_board(project)
    page = FakePage([actor(), camera()])
    install(PrevisHost(page))
    ops.set_scene_layout(unit, blocks=[], replace=False,
                         cameras=[{'id': CAMERA_ID, 'position': [0, 1.6, 4],
                                   'target': [0, 1.2, 0], 'focal': 35, 'framing': 'medium'}])
    assert page.sequences[0][0]['spec']['framing'] == '中景'
    ops.set_scene_layout(unit, blocks=[], replace=False,
                         cameras=[{'id': CAMERA_ID, 'position': [0, 1.6, 4],
                                   'target': [0, 1.2, 0], 'focal': 35, 'framing': '近景'}])
    assert page.sequences[1][0]['spec']['framing'] == '近景'
    with pytest.raises(ValueError, match='framing'):
        ops.set_scene_layout(unit, blocks=[], replace=False,
                             cameras=[{'id': CAMERA_ID, 'position': [0, 1.6, 4],
                                       'target': [0, 1.2, 0], 'framing': 'hero-shot'}])


# -- 工具接线：read_scene_set --------------------------------------------------

@pytest.mark.slow
def test_read_scene_set_summarizes_the_page_snapshot(project):
    ops, unit, _block = setup_board(project)
    page = FakePage([actor(), camera()], name='窗边', duration_ms=12000)
    page.scene['room'] = {'width': 4.8, 'depth': 4.2, 'height': 2.8}
    host = install(PrevisHost(page))
    summary = ops.read_scene_set(unit)
    assert summary['has_project'] is True and summary['page'] is True
    assert summary['name'] == '窗边' and summary['duration_s'] == 12.0
    assert summary['room'] == {'width': 4.8, 'depth': 4.2, 'height': 2.8}
    assert summary['block_count'] == 1 and summary['blocks'][0] == {
        'id': 'act-a', 'name': '甲', 'kind': 'actor'}
    assert summary['camera_count'] == 1
    assert summary['cameras'][0]['camera_id'] == CAMERA_ID
    assert summary['cameras'][0]['focal'] == 35.0
    assert summary['cut_count'] == 1 and summary['cuts'] == [{'time': 0.0, 'camera_id': CAMERA_ID}]
    assert summary['state_path'].endswith('scene.state.json')
    assert summary['error'] == ''
    # 只读：还不落盘，所以 revision 还是 0，并把「入账后会变几」一并说出来。
    assert summary['revision'] == 0 and summary['pending_change'] is True
    assert summary['pending_revision'] == 1
    assert not (ops._previs_dir(unit) / 'scene.state.json').exists()
    # onChange 之后界面调 record_change 入账：revision 到位，pending 归零。
    host.record_change(unit, state_dir=ops._previs_dir(unit))
    settled = ops.read_scene_set(unit)
    assert settled['revision'] == 1 and settled['pending_change'] is False
    assert settled['source'] == 'onChange'
    assert (ops._previs_dir(unit) / 'scene.state.json').is_file()


@pytest.mark.slow
def test_read_scene_set_says_there_is_no_project_yet(project):
    ops, unit, _block = setup_board(project)
    summary = ops.read_scene_set(unit)
    assert summary['has_project'] is False and summary['page'] is False
    assert summary['revision'] == 0 and summary['cameras'] == [] and summary['room'] is None
    assert '.director' in summary['hint'] and 'scene.state.json' in summary['state_path']


# -- 工具接线：capture_previs_layout 走页面快照 --------------------------------

@pytest.mark.slow
def test_capture_previs_layout_prefers_the_page_snapshot(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block)
    _write_director(ops, unit)
    ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID)
    page = FakePage([actor(), camera(position=(0.4, 1.2, 3.0))])
    install(PrevisHost(page))
    captured = ops.capture_previs_layout(unit, shot, 0)
    assert captured['snapshot_source'] == 'page'
    assert captured['path'] == ''                      # 页面快照没有 .director 路径
    assert captured['scene_id'] == 'scene'
    assert captured['layout']['source'] == 'previs'
    # 机位来自页面（挪到 3 米处），不是文件里的 5 米。
    assert captured['camera_id'] == CAMERA_ID
    assert captured['layout']['camera']['z'] == pytest.approx(3.0, abs=1e-3)
    assert ops.read_scene_set(unit)['revision'] == 1


@pytest.mark.slow
def test_capture_previs_layout_falls_back_to_the_director_file(project):
    """页面不可达（没开/已换/接口不匹配）时文件是唯一拿得到的东西，也得能出图。"""
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block)
    _write_director(ops, unit)
    ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID)
    install(PrevisHost(StubCaller(getScene=PrevisPageNotReady('预演页还没就绪：页面已换'))))
    captured = ops.capture_previs_layout(unit, shot, 0)
    assert captured['snapshot_source'] == 'file'
    assert captured['path'].endswith('scene.director')
    assert captured['layout']['camera']['z'] == pytest.approx(5.0, abs=1e-3)


@pytest.mark.slow
def test_capture_previs_layout_explains_a_camera_that_left_the_scene(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block)
    _write_director(ops, unit)
    ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID)
    # 页面上那台机位被删了：绑定的 camera_id 在本场布景里已经找不到。
    install(PrevisHost(FakePage([actor()], cuts=[])))
    board_before = ops.read_storyboard(unit)
    with pytest.raises(ValueError) as error:
        ops.capture_previs_layout(unit, shot, 0)
    message = str(error.value)
    assert '本镜绑定的机位' + CAMERA_ID in message
    assert '已不存在' in message and '重新选择' in message
    assert ops.read_storyboard(unit) == board_before   # 报错时一个字都没写


@pytest.mark.slow
def test_capture_previs_layout_says_both_reasons_when_nothing_is_reachable(project):
    ops, unit, block = setup_board(project)
    shot = shot_with_keyframes(ops, unit, block)
    _write_director(ops, unit)
    ops.set_shot_previs(unit, revision(ops, unit), shot, CAMERA_ID)
    install(PrevisHost(StubCaller(getScene=PrevisPageNotReady('预演页还没就绪：页面已换'))))
    (ops._previs_dir(unit) / 'scene.director').unlink()
    with pytest.raises(ValueError) as error:
        ops.capture_previs_layout(unit, shot, 0)
    message = str(error.value)
    assert '就绪' in message and '读不到预演工程' in message


# -- 工具注册 -----------------------------------------------------------------

@pytest.mark.slow
def test_scene_set_tools_are_registered_and_callable(project):
    from narrative import tool_api

    ops, unit, _block = setup_board(project)
    page = FakePage([actor(), camera()])
    install(PrevisHost(page))
    entry = layout(ops.project_info().path).entry
    assert tool_api.TOOLS['read_scene_set'].writes is False
    assert tool_api.TOOLS['set_scene_layout'].writes is True
    summary = tool_api.call('read_scene_set', {'unit_id': unit}, project=entry)
    assert summary['camera_count'] == 1 and summary['pending_change'] is True
    written_layout = tool_api.call('set_scene_layout', {
        'unit_id': unit,
        'blocks': [{'kind': 'prop', 'name': '门', 'position': [1, 0, -3]}],
        'cameras': [{'id': CAMERA_ID, 'position': [0, 1.6, 4], 'target': [0, 1.2, 0],
                     'focal': 40}]}, project=entry)
    assert written_layout['revision'] == 2
    assert tool_api.call('read_scene_set', {'unit_id': unit}, project=entry)['revision'] == 2
    # 工具层只认 ValueError：预演页的原因必须原样变成 ToolError，不能变成裸 RuntimeError。
    previs_host.uninstall()
    with pytest.raises(tool_api.ToolError, match='预演台还没打开'):
        tool_api.call('set_scene_layout', {'unit_id': unit, 'blocks': [], 'cameras': []},
                      project=entry)


def _write_director(ops, unit):
    """放一份 .director：绑机位仍按文件核对机位编号（页面只看得到当前场景）。"""
    directory = ops._previs_dir(unit)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'scene.director'
    path.write_text(json.dumps(flat([actor(), camera()]), ensure_ascii=False), encoding='utf-8')
    return path


# -- 端到端（可选）-------------------------------------------------------------
# 真起页面的端到端用例**故意不放在这里**：tests/conftest.py 的收集规则把「源码里出现
# Qt 导入名」的模块整体忽略、并把其中每个用例标成 ui（默认与 --slow 都不跑），而本文件
# 的核心价值是「不装 Qt 也能跑」的那批假调用器用例——写进来会让它们一起跑不到。
# 想启用真页面自检，应当新建 tests/test_previs_host_ui.py（超出本次允许改的文件范围），
# 在那里 create QWebEnginePage → PrevisHost(QtWebEngineCaller(page)) → hello/getScene/
# applyBatch；本次交付里由 记录在报告的「手动自检」一节说明实际跑通的结论。


def test_page_snapshot_keeps_lights_out_of_boxes():
    """页面快照里的灯与隐藏实体不能混进 entities：否则灯会被当盒子投进框图。

    页面 getScene 的灯是普通 ``prop``（只在 asset 上能认出来）；这条守住「与 load_scene
    同口径」——灯进 scene['lights']，隐藏实体丢弃，可见盒子保留。
    """
    scene_payload = {
        'sceneId': 'scene-1', 'name': '测试场', 'duration_ms': 4000, 'fps': 24, 'aspect': '16:9',
        'entities': [
            {'id': 'box-1', 'kind': 'prop', 'name': '桌子', 'asset': 'table',
             'position': [0, 0, 0], 'rotation': [0, 0, 0], 'scale': [1, 0.8, 0.6], 'height': 1},
            {'id': 'light-1', 'kind': 'prop', 'name': '主光', 'asset': 'light-spot',
             'position': [1, 2, 1], 'rotation': [0, 0, 0], 'color': '#ffd9a0'},
            {'id': 'ghost', 'kind': 'prop', 'name': '隐藏道具', 'asset': 'crate', 'visible': False,
             'position': [2, 0, 2], 'rotation': [0, 0, 0]},
        ], 'cuts': []}

    def caller(method, payload):
        return {'ok': True, 'ready': True, 'protocol': 1} if method == 'hello' else scene_payload

    scene = PrevisHost(caller).snapshot('unit-1', state_dir=None)
    assert [row['id'] for row in scene['entities']] == ['box-1']
    assert [row['id'] for row in scene['lights']] == ['light-1']
