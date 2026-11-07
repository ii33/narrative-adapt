"""All public transports retain missing-location drafts and repair them locally."""
import json

import pytest

from narrative import mcp_server, tool_api, tool_cli
from narrative.operations import Operations


@pytest.mark.parametrize('transport', ['tool', 'cli', 'mcp'])
def test_location_requirement_and_local_repair_share_operations(transport, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'profiles.sqlite3'))
    entry = tmp_path / 'locations.nproj'
    ops = Operations.create_graph_project(entry)
    text = '甲决定调整工作安排。'
    source = ops.import_source('未交代地点', text)

    def call(name, args):
        if transport == 'tool':
            return tool_api.call(name, args, project=entry)
        if transport == 'cli':
            assert tool_cli.main(['tool', 'call', name, json.dumps(args), '--project', str(entry), '--json']) == 0
            return json.loads(capsys.readouterr().out)
        response = mcp_server.handle({'id': 1, 'method': 'tools/call',
            'params': {'name': name, 'arguments': args}}, default_project=str(entry))
        assert not response['result']['isError']
        return json.loads(response['result']['content'][0]['text'])

    result = call('submit_extraction', {'source_id': source, 'result': {'nodes': [
        {'type': 'scene', 'name': '调整安排', 'quotes': [text]},
        {'type': 'event', 'name': '决定调整工作安排', 'scene': '@1', 'quotes': [text]},
    ], 'properties': []}})
    assert result['status'] == 'staged'
    assert any(p['code'] == 'EVENT_LOCATION_REQUIRED' and p['targets'] for p in result['problems'])
    assert ops.graph_snapshot()['nodes'] == []
    repaired = call('patch_extraction_submission', {'submission_id': result['submission_id'],
        'expected_revision': result['submission_revision'], 'reason': '明确地点未知',
        'patches': [{'op': 'append', 'section': 'properties', 'value': {
            'node': '@2', 'property': '地点', 'value_type': 'either', 'op': 'unknown',
            'scope': 'world', 'at_event': '@2', 'quotes': [text]}}]})
    assert repaired['status'] == 'accepted' and repaired['submission_status'] == 'committed'
    assert len(ops.graph_snapshot()['nodes']) == 2
    assert ops.check_project()['counts']['error'] == 0
