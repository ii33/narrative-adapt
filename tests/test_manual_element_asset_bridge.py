"""Explicit asset creation reports its own request and never binds a shot."""
from types import SimpleNamespace

from narrative.native_bridge import NativeBridge
from narrative.operations import Operations


def test_manual_creation_returns_card_without_fact_or_storyboard_write(tmp_path):
    ops = Operations.create_graph_project(tmp_path / 'manual.nproj')
    received = []
    bridge = SimpleNamespace(_project_path=str(tmp_path / 'manual.nproj'),
        _project_task=lambda kind, work: received.append((kind, work(ops))))
    NativeBridge.saveStoryboardElementAsset(bridge, {'category': 'item', 'title': '折叠伞'}, 0, 12)
    kind, result = received[0]
    assert kind == 'storyboard_element_asset' and result['token'] == 12
    assert result['asset']['title'] == '折叠伞' and not result['asset']['node_id']
    assert ops.graph_snapshot()['nodes'] == []
    assert len(ops.list_assets('item')['assets']) == 1


def test_manual_creation_conflict_keeps_request_identity_and_creates_nothing(tmp_path):
    ops = Operations.create_graph_project(tmp_path / 'manual.nproj')
    received = []
    bridge = SimpleNamespace(_project_path='manual.nproj',
        _project_task=lambda kind, work: received.append(work(ops)))
    NativeBridge.saveStoryboardElementAsset(bridge, {'category': 'item', 'title': '折叠伞'}, 99, 14)
    assert received[0]['token'] == 14 and received[0]['project_path'] == 'manual.nproj'
    assert received[0]['error_type'] == 'ContentConflictError'
    assert ops.list_assets('item')['assets'] == []
