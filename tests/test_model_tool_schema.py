"""Model contracts hide only host provenance; public business contracts stay exact."""
from dataclasses import replace

import pytest

from narrative import assistant, tool_api
from narrative.model_tool_schema import PROVENANCE_TOOLS, model_tool_schema
from narrative.tool_execution_context import bind_tool_execution_metadata
from narrative.tool_loading import load_tools


@pytest.mark.parametrize('name', sorted(PROVENANCE_TOOLS))
def test_model_projection_and_unbound_loading_share_contract_without_mutation(name):
    tool = tool_api.TOOLS[name]
    public = tool.schema()
    schema = model_tool_schema(tool)
    assert {'task_id', 'manifest_ref'} <= set(public['properties'])
    assert not {'task_id', 'manifest_ref'} & set(schema['properties'])
    assert next(t for t in assistant.tool_catalog([name]) if t['name'] == name)['parameters'] == schema
    assert load_tools(names=[name])['tools'][0]['inputSchema'] == schema
    assert tool.schema() == public
    for key in public['properties'].keys() - {'task_id', 'manifest_ref'}:
        assert schema['properties'][key] == public['properties'][key]


@pytest.mark.parametrize('name', ['read_usage_statistics', 'read_task_write_summary'])
def test_query_task_id_is_preserved_under_current_task_context(tmp_path, monkeypatch, name):
    from narrative.operations import Operations
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    entry = tmp_path / 'query.nproj'
    Operations.create_graph_project(entry)
    captured = []
    monkeypatch.setitem(tool_api.TOOLS, name, replace(tool_api.TOOLS[name],
        handler=lambda ops, args: captured.append(args.copy()) or {'ok': True}))
    assert 'task_id' in model_tool_schema(tool_api.TOOLS[name])['properties']
    with bind_tool_execution_metadata({'task_id': 'current-task', 'manifest_ref': 'current-manifest'}):
        tool_api.call(name, {'task_id': 'previous-task'}, project=entry)
    assert captured == [{'task_id': 'previous-task'}]


@pytest.mark.parametrize('name', sorted(PROVENANCE_TOOLS))
def test_host_provenance_still_overrides_model_values(tmp_path, monkeypatch, name):
    from narrative.operations import Operations
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    entry = tmp_path / 'provenance.nproj'
    Operations.create_graph_project(entry)
    captured = []
    tool = tool_api.TOOLS[name]
    # Keep the real parameter set; the handler spy isolates provenance from domain fixture creation.
    monkeypatch.setitem(tool_api.TOOLS, name, replace(tool,
        params={k: {**v, 'required': False} for k, v in tool.params.items()},
        handler=lambda ops, args: captured.append(args.copy()) or {'ok': True}))
    with bind_tool_execution_metadata({'task_id': 'host-task', 'manifest_ref': 'host-manifest'}):
        tool_api.call(name, {'task_id': 'forged', 'manifest_ref': 'forged'}, project=entry)
    assert captured == [{'task_id': 'host-task', 'manifest_ref': 'host-manifest'}]


def test_real_local_sdk_provider_contract_and_native_host_provenance(tmp_path, monkeypatch):
    import json

    from test_chat_scroll import transcript_shell
    from test_native_model_diagnostics import require_runtime, stub_model, tool_delta

    from narrative.operations import Operations

    require_runtime()
    monkeypatch.setenv('MODEL_SCHEMA_TEST_KEY', 'offline-only')
    captured = []
    name = 'mark_writing_basis_current'
    monkeypatch.setitem(tool_api.TOOLS, name, replace(tool_api.TOOLS[name],
        handler=lambda ops, args: captured.append(args.copy()) or {'status': 'unchanged'}))
    def provider(number, request):
        functions = {item['function']['name']: item['function'] for item in request.get('tools', [])}
        for function in functions.values():
            if function['name'] in PROVENANCE_TOOLS:
                assert not {'task_id', 'manifest_ref'} & set(function['parameters']['properties'])
        if number == 1:
            return tool_delta('load_tools', json.dumps({'names': sorted(PROVENANCE_TOOLS)}), 'load-writing')
        if number == 2:
            assert PROVENANCE_TOOLS <= set(functions)
            return tool_delta(name, json.dumps({'target_kind': 'unit', 'target_id': 'unit-controlled',
                'expected_revision': 0, 'reason': '离线宿主关联测试'}), 'host-provenance')
        assert number == 3
        return {'role': 'assistant', 'content': '本地合同验证完成。'}, 'stop'
    with stub_model(provider) as (url, requests, errors):
        with transcript_shell(tmp_path, monkeypatch) as ui:
            ops = Operations.open(ui.bridge._project_path)
            ops.save_provider_profile({'provider': 'custom', 'model': 'local-stub', 'base_url': url,
                'key_env': 'MODEL_SCHEMA_TEST_KEY', 'timeout_seconds': 10})
            ui.bridge.newChat()
            ui.wait(lambda: ui.panel.property('chatId') not in {ui.a['id'], ui.b['id'], ''})
            ui.results.clear()
            ui.bridge.assistantTurn('加载写作工具并完成受控宿主关联合同检查。', '')
            ui.wait(lambda: errors or ui.errors or any(k == 'assistant' for k, _ in ui.results))
            assert not errors and not ui.errors, (errors, ui.errors)
            assert len(requests) == 3 and len(captured) == 1
            result = next(value for kind, value in ui.results if kind == 'assistant')
            assert captured[0]['task_id'] == result['task_id']
            assert captured[0]['manifest_ref'].endswith('task.json')
            assert captured[0]['target_id'] == 'unit-controlled' and captured[0]['expected_revision'] == 0
