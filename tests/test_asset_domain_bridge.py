"""Asset navigation reads the requested fact domain without changing either."""
from types import SimpleNamespace

import pytest
from test_screenplay_source_references import prepared as prepared

from narrative.native_bridge import NativeBridge
from narrative.task_write_summary import build_submission_summary


@pytest.mark.parametrize("domain", ["adaptation", "extraction"])
def test_asset_browser_routes_nodes_and_moments_to_selected_domain(prepared, domain):
    ops, _, _, _ = prepared
    before = (ops.graph_snapshot(), ops.adaptation_snapshot())
    received = []
    bridge = SimpleNamespace(_project_task=lambda kind, work: received.append((kind, work(ops))))
    NativeBridge.loadAssets(bridge, "appearance", "", "", domain)
    kind, result = received[0]
    expected = before[0 if domain == "extraction" else 1]
    assert kind == "assets"
    assert result["domain"] == domain
    assert result["nodes"] == expected["nodes"]
    assert result["moments"] == ops.asset_moments(domain)
    assert (ops.graph_snapshot(), ops.adaptation_snapshot()) == before


def test_invalid_asset_domain_is_rejected_before_snapshot_read(prepared):
    ops, _, _, _ = prepared
    bridge = SimpleNamespace(_project_task=lambda kind, work: work(ops))
    with pytest.raises(ValueError):
        NativeBridge.loadAssets(bridge, "appearance", "", "", "not-a-domain")


def test_asset_analysis_commit_has_program_generated_summary():
    summary = build_submission_summary([{
        'tool': 'submit_storyboard_assets', 'ok': True,
        'args': {'unit_id': 'unit-1', 'shot_ids': ['shot-1']},
        'result': {'status': 'accepted', 'unit_id': 'unit-1', 'revision': 2},
    }])
    assert summary['committed_operations'] == 1
    assert len(summary['changes']) == 1
    assert summary['changes'][0]['object_type'] == 'storyboard'
