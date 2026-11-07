"""Writing failures carry request identity instead of leaving selection busy."""
import threading
from types import SimpleNamespace

from narrative.native_bridge import NativeBridge


class Signal:
    def __init__(self):
        self.events = []

    def emit(self, *args):
        self.events.append(args)


def test_preparation_failure_reports_fixed_target_and_does_not_start():
    target = {'kind': 'storyboard_assets', 'ids': ['unit-1'], 'shot_ids': ['shot-1'], 'client_request_token': 7}
    def reject(*args, **kwargs):
        raise ValueError('镜头已删除')
    responses = []
    bridge = SimpleNamespace(
        _project_path='project.nproj', _assistant_chat='chat-1',
        _capture_assistant_access=lambda chat: (chat, 'standard'),
        _assistant_turn=lambda *a, **kw: responses.append('must not start'),
        resultReady=Signal(), errorOccurred=Signal(),
        _project_task=lambda kind, work, apply: responses.append(apply(work(SimpleNamespace(prepare_script_writing_task=reject)))),
    )
    NativeBridge.screenplayWritingTurn(bridge, '分析', target)
    kind, failure = bridge.resultReady.events[0]
    assert kind == 'screenplay_writing_failed'
    assert failure['target'] == target and failure['project_path'] == 'project.nproj'
    assert responses == [{'target': target, 'started': False, 'error': '镜头已删除'}]


def test_worker_failure_retains_target_identity_but_stale_project_does_not_publish():
    for epoch in (0, 1):
        target = {'kind': 'storyboard_assets', 'client_request_token': 9}
        bridge = SimpleNamespace(
            _assistant_lock=threading.Lock(),
            _assistant_jobs={1: {'task_id': 'task-1', 'writing_target': target, 'project_path': 'project.nproj'}},
            _requests={1: ('assistant', epoch, None, None, 0, '')},
            _closed=False, _epoch=0, _query_epoch=0, _project_path='project.nproj',
            _content_forms=SimpleNamespace(take_conflict=lambda form: None),
            resultReady=Signal(), errorOccurred=Signal(), busyChanged=Signal(),
        )
        NativeBridge._on_worker_done(bridge, 1, None, RuntimeError('启动失败'))
        if epoch:
            assert not bridge.resultReady.events
        else:
            kind, event = bridge.resultReady.events[0]
            assert kind == 'screenplay_writing_failed'
            assert event == {'target': target, 'task_id': 'task-1', 'project_path': 'project.nproj', 'error': '启动失败'}
