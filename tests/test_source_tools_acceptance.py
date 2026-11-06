"""Offline validators; synthetic SDK records are not real-model acceptance."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def acceptance(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('source_tools_acceptance', scripts / 'accept_source_tools.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def slices(module):
    return [{'id': 'SRC1-SL1', 'start': 0, 'end': module.BOUNDARY},
        {'id': 'SRC1-SL2', 'start': module.BOUNDARY, 'end': len(module.TEXT)}]


def sdk_pair(name, call_id, result, *, is_error=False):
    return [{'data': {'message': {'role': 'assistant', 'content': [
        {'type': 'tool-call', 'id': call_id, 'name': name, 'arguments': '{}'}]}}},
        {'data': {'message': {'role': 'tool', 'toolCallId': call_id, 'isError': is_error,
            'content': [{'type': 'text', 'text': json.dumps(result)}]}}}]


def successful_rows(module):
    parts = slices(module)
    rows = sdk_pair('create_source_slices', 'create', {'source_id': 'SRC1', 'slices': parts,
        'status': 'accepted', 'submission_status': 'committed'})
    for i, part in enumerate(parts):
        rows += sdk_pair('read_source_slice', f'read-{i}', part | {'text': module.TEXT[part['start']:part['end']]})
    return rows


def test_unicode_chapter_boundaries_and_sdk_received_results(acceptance):
    assert all(acceptance.assess_slices(slices(acceptance)).values())
    checks, _ = acceptance.assess_sdk(successful_rows(acceptance), 'SRC1', slices(acceptance))
    assert all(checks.values())
    assert '😀' in acceptance.TEXT and 45 <= len(acceptance.TEXT) <= 70


def test_sdk_rejected_create_is_not_host_success(acceptance):
    rows = successful_rows(acceptance)
    rows[1]['data']['message']['isError'] = True
    checks, _ = acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))
    assert not checks['sdk_create_committed']
    del rows[1]['data']['message']['isError']
    assert not acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))[0]['sdk_create_committed']


def test_staged_not_committed_and_unmatched_response_rejected(acceptance):
    rows = successful_rows(acceptance)
    result = json.loads(rows[1]['data']['message']['content'][0]['text'])
    result.update(status='staged', submission_status='staged')
    rows[1]['data']['message']['content'][0]['text'] = json.dumps(result)
    assert not acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))[0]['sdk_create_committed']
    rows = successful_rows(acceptance)
    rows[1]['data']['message']['toolCallId'] = 'different-call'
    assert not acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))[0]['sdk_create_committed']


def test_read_text_or_range_mismatch_and_missing_second_read_fail(acceptance):
    rows = successful_rows(acceptance)
    rows[-1]['data']['message']['content'][0]['text'] = json.dumps(slices(acceptance)[1] | {'text': 'wrong'})
    assert not acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))[0]['sdk_read_each_slice_exact_text']
    assert not acceptance.assess_sdk(successful_rows(acceptance)[:-2], 'SRC1', slices(acceptance))[0]['sdk_read_each_slice_exact_text']
    parts = slices(acceptance)
    parts[0]['end'] -= 1
    assert not acceptance.assess_slices(parts)['two_chapters_exactly_covered']
    rows = successful_rows(acceptance)
    part = slices(acceptance)[1]
    rows[-1]['data']['message']['content'][0]['text'] = json.dumps(part | {
        'source_id': 'other-source', 'text': acceptance.TEXT[part['start']:part['end']]})
    assert not acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))[0]['sdk_read_each_slice_exact_text']


def test_source_registration_fingerprint_is_saved_not_sent(acceptance, tmp_path):
    report = {}
    acceptance.record_implementation(None, None, report, tmp_path, None)
    recorded = json.loads((tmp_path / 'source-tool-implementation.json').read_text())
    assert set(recorded) == {'src/narrative/tool_api.py', 'src/narrative/source_tools.py', 'scripts/accept_source_tools.py'}
    assert recorded == report['source_tool_implementation']
    assert all(len(value) == 64 for value in recorded.values())


def test_seed_locks_authorized_ark_subscription_model_and_real_operations_receipt(acceptance, tmp_path, monkeypatch):
    from narrative import tool_api
    from narrative.operations import Operations
    from narrative.providers import preset
    monkeypatch.setenv('NARRATIVE_PROVIDER_DB', str(tmp_path / 'providers.sqlite3'))
    ops = Operations.create_graph_project(tmp_path / 'source.nproj')
    fixture = acceptance.seed(ops)
    assert ops.harness_settings()['provider'] == 'ark'
    assert ops.harness_settings()['model'] == acceptance.MODEL == 'deepseek-v4.1-flash-economy'
    assert ops.harness_settings()['base_url'] == preset('ark')['base_url'] == 'https://ark.cn-beijing.volces.com/api/plan/v3'
    assert ops.harness_settings()['reasoning_effort'] == 'low'
    assert ops.harness_settings()['key_env'] == 'ARK_API_KEY'
    before = ops.source(fixture['source_id'])
    result = tool_api.call('create_source_slices', {'source_id': fixture['source_id'], 'slices': [
        {'title': '来客', 'start': 0, 'end': acceptance.BOUNDARY},
        {'title': '归途', 'start': acceptance.BOUNDARY, 'end': len(acceptance.TEXT)}]}, project=tmp_path / 'source.nproj')
    assert result['status'] == 'accepted' and result['submission_status'] == 'committed'
    assert all(acceptance.assess_slices(result['slices']).values())
    assert ops.source(fixture['source_id']) == before
    rows = sdk_pair('create_source_slices', 'actual-create', result)
    for i, part in enumerate(result['slices']):
        actual = tool_api.call('read_source_slice', {'source_id': fixture['source_id'], 'slice_id': part['id']},
            project=tmp_path / 'source.nproj')
        rows += sdk_pair('read_source_slice', f'actual-read-{i}', actual)
    assert all(acceptance.assess_sdk(rows, fixture['source_id'], result['slices'])[0].values())
