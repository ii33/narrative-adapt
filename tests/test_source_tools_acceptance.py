"""Offline validators; synthetic SDK records are not real-model acceptance."""
import importlib.util
import json
from copy import deepcopy
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
    return [{'id': 'SRC1-SL1', 'source_id': 'SRC1', 'title': '来客', 'start': 0, 'end': module.BOUNDARY, 'length': module.BOUNDARY},
        {'id': 'SRC1-SL2', 'source_id': 'SRC1', 'title': '归途', 'start': module.BOUNDARY, 'end': len(module.TEXT), 'length': len(module.TEXT) - module.BOUNDARY}]


def sdk_pair(name, call_id, result, *, args=None, is_error=False):
    return [{'data': {'message': {'role': 'assistant', 'content': [
        {'type': 'tool-call', 'id': call_id, 'name': name,
         'arguments': json.dumps(args if args is not None else {})}]}}},
        {'data': {'message': {'role': 'tool', 'toolCallId': call_id, 'isError': is_error,
            'content': [{'type': 'text', 'text': json.dumps(result)}]}}}]


def successful_rows(module, source_id='SRC1', parts=None, *, source_text=None, initial_slices=None, requested=None):
    parts = parts if parts is not None else slices(module)
    text = module.TEXT if source_text is None else source_text
    initial_slices = [] if initial_slices is None else initial_slices
    rows = sdk_pair('list_sources', 'sources', {'sources': [
        {'id': source_id, 'title': '两章原文工具验收', 'chars': len(text)}]}, args={})
    rows += sdk_pair('read_source', 'source', {'id': source_id, 'title': '两章原文工具验收',
        'chars': len(text), 'start': 0, 'text': text},
        args={'source_id': source_id, 'start': 0, 'length': len(text)})
    rows += sdk_pair('list_source_slices', 'existing', {'source_id': source_id, 'slices': initial_slices},
        args={'source_id': source_id})
    requested = requested if requested is not None else [
        {key: part[key] for key in ('title', 'start', 'end')} for part in parts]
    rows += sdk_pair('create_source_slices', 'create', {'source_id': source_id, 'slices': parts,
        'status': 'accepted', 'submission_status': 'committed', 'validation_status': 'valid', 'problems': []},
        args={'source_id': source_id, 'slices': requested})
    # Both calls may be issued together; completion order need not equal call order.
    pairs = [sdk_pair('read_source_slice', f'read-{i}', part | {
        'source_id': source_id, 'text': text[part['start']:part['end']]},
        args={'source_id': source_id, 'slice_id': part['id']}) for i, part in enumerate(parts)]
    return rows + [pair[0] for pair in pairs] + [pair[1] for pair in reversed(pairs)]


def message(rows, call_id, *, response=False):
    for row in rows:
        msg = row['data']['message']
        if response and msg.get('toolCallId') == call_id:
            return msg
        if not response and any(block.get('id') == call_id for block in msg.get('content', [])):
            return msg
    raise AssertionError(f'missing fixture call {call_id}')


def arguments(rows, call_id):
    return json.loads(message(rows, call_id)['content'][0]['arguments'])


def set_arguments(rows, call_id, args):
    message(rows, call_id)['content'][0]['arguments'] = json.dumps(args)


def response_value(rows, call_id):
    return json.loads(message(rows, call_id, response=True)['content'][0]['text'])


def set_response(rows, call_id, value):
    message(rows, call_id, response=True)['content'][0]['text'] = json.dumps(value)


def test_unicode_chapter_boundaries_and_sdk_received_results(acceptance):
    assert all(acceptance.assess_slices(slices(acceptance)).values())
    checks, _ = acceptance.assess_sdk(successful_rows(acceptance), 'SRC1', slices(acceptance))
    assert all(checks.values())
    assert '😀' in acceptance.TEXT and 45 <= len(acceptance.TEXT) <= 70


def test_sdk_rejected_create_is_not_host_success(acceptance):
    rows = successful_rows(acceptance)
    message(rows, 'create', response=True)['isError'] = True
    checks, _ = acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))
    assert not checks['sdk_create_committed']
    del message(rows, 'create', response=True)['isError']
    assert not acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))[0]['sdk_create_committed']


def test_staged_not_committed_and_unmatched_response_rejected(acceptance):
    rows = successful_rows(acceptance)
    result = json.loads(message(rows, 'create', response=True)['content'][0]['text'])
    result.update(status='staged', submission_status='staged')
    message(rows, 'create', response=True)['content'][0]['text'] = json.dumps(result)
    assert not acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))[0]['sdk_create_committed']
    rows = successful_rows(acceptance)
    message(rows, 'create', response=True)['toolCallId'] = 'different-call'
    assert not acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))[0]['sdk_create_committed']


def test_read_text_or_range_mismatch_and_missing_second_read_fail(acceptance):
    rows = successful_rows(acceptance)
    message(rows, 'read-1', response=True)['content'][0]['text'] = json.dumps(slices(acceptance)[1] | {'text': 'wrong'})
    assert not acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))[0]['sdk_read_each_slice_exact_text']
    assert not acceptance.assess_sdk([row for row in successful_rows(acceptance) if row['data']['message'].get('toolCallId') != 'read-1'], 'SRC1', slices(acceptance))[0]['sdk_read_each_slice_exact_text']
    parts = slices(acceptance)
    parts[0]['end'] -= 1
    assert not acceptance.assess_slices(parts)['two_chapters_exactly_covered']
    rows = successful_rows(acceptance)
    part = slices(acceptance)[1]
    message(rows, 'read-1', response=True)['content'][0]['text'] = json.dumps(part | {
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
    rows = successful_rows(acceptance, fixture['source_id'], result['slices'])
    set_response(rows, 'create', result)
    for i, part in enumerate(result['slices']):
        actual = tool_api.call('read_source_slice', {'source_id': fixture['source_id'], 'slice_id': part['id']},
            project=tmp_path / 'source.nproj')
        set_response(rows, f'read-{i}', actual)
    assert all(acceptance.assess_sdk(rows, fixture['source_id'], result['slices'])[0].values())

@pytest.mark.parametrize('case', [
    'empty_arguments', 'wrong_create_source', 'wrong_create_range', 'wrong_read_source',
    'wrong_read_slice_id', 'read_before_create', 'response_before_call',
    'duplicate_call_id', 'duplicate_response', 'wrong_response_binding',
    'non_assistant_call', 'truncated_source_read', 'missing_source_list',
    'missing_source_read', 'missing_existing_slices_list', 'wrong_source_result_id',
    'wrong_created_slice_id', 'wrong_source_list_id',
])
def test_invalid_transcript_cannot_pass_all_sdk_checks(acceptance, case):
    rows = deepcopy(successful_rows(acceptance))
    if case == 'empty_arguments':
        for row in rows:
            msg = row['data']['message']
            if msg['role'] == 'assistant':
                msg['content'][0]['arguments'] = '{}'
    elif case in {'wrong_create_source', 'wrong_create_range', 'wrong_read_source', 'wrong_read_slice_id'}:
        cid = 'create' if case.startswith('wrong_create') else 'read-1'
        args = arguments(rows, cid)
        if case in {'wrong_create_source', 'wrong_read_source'}:
            args['source_id'] = 'SRC-other'
        elif case == 'wrong_create_range':
            args['slices'][0]['end'] -= 1
        else:
            args['slice_id'] = 'SL-other'
        set_arguments(rows, cid, args)
    elif case == 'read_before_create':
        read_rows = [r for r in rows if r['data']['message'].get('toolCallId') in {'read-0', 'read-1'}
            or any(b.get('id') in {'read-0', 'read-1'} for b in r['data']['message'].get('content', []))]
        rows = read_rows + [r for r in rows if r not in read_rows]
    elif case == 'response_before_call':
        response = next(r for r in rows if r['data']['message'].get('toolCallId') == 'create')
        rows.remove(response)
        index = next(i for i, r in enumerate(rows) if r['data']['message'] is message(rows, 'create'))
        rows.insert(index, response)
    elif case == 'duplicate_call_id':
        # An earlier invalid call is hidden by the final same-ID dictionary assignment.
        duplicate = deepcopy(next(r for r in rows if r['data']['message'] is message(rows, 'create')))
        duplicate['data']['message']['content'][0]['arguments'] = '{"source_id":"other","slices":[]}'
        rows.insert(0, duplicate)
    elif case == 'duplicate_response':
        rows.append(deepcopy(next(r for r in rows if r['data']['message'].get('toolCallId') == 'create')))
    elif case == 'wrong_response_binding':
        message(rows, 'read-0', response=True)['toolCallId'] = 'read-1'
        message(rows, 'read-1', response=True)['toolCallId'] = 'read-0'
        # Both text results remain correct individually but are now attached to the other call.
    elif case == 'non_assistant_call':
        message(rows, 'create')['role'] = 'user'
    elif case == 'truncated_source_read':
        value = response_value(rows, 'source')
        value['text'] = acceptance.TEXT[:-1]
        set_response(rows, 'source', value)
    elif case.startswith('missing_'):
        cid = {'missing_source_list': 'sources', 'missing_source_read': 'source',
            'missing_existing_slices_list': 'existing'}[case]
        rows = [r for r in rows if r['data']['message'].get('toolCallId') != cid
            and not any(b.get('id') == cid for b in r['data']['message'].get('content', []))]
    elif case == 'wrong_source_result_id':
        value = response_value(rows, 'source')
        value['id'] = 'SRC-other'
        set_response(rows, 'source', value)
    elif case == 'wrong_created_slice_id':
        value = response_value(rows, 'create')
        value['slices'][0]['id'] = 'SL-other'
        set_response(rows, 'create', value)
    elif case == 'wrong_source_list_id':
        value = response_value(rows, 'sources')
        value['sources'][0]['id'] = 'SRC-other'
        set_response(rows, 'sources', value)
    else:
        raise AssertionError(case)
    checks, _ = acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))
    assert not all(checks.values()), f'{case}: invalid transcript was accepted: {checks}'

@pytest.mark.parametrize('source_read_mode', ['unique_source_defaults', 'multi_segment'])
def test_valid_unique_source_defaults_and_segmented_reads(acceptance, source_read_mode):
    rows = successful_rows(acceptance)
    # A successful unique source listing licenses omitted source_id, not absent required payloads.
    for cid in ('source', 'existing', 'create', 'read-0', 'read-1'):
        args = arguments(rows, cid)
        args.pop('source_id')
        set_arguments(rows, cid, args)
    if source_read_mode == 'unique_source_defaults':
        set_arguments(rows, 'source', {})  # default start=0, length=2000 covers this short source
    else:
        cutoff = acceptance.BOUNDARY
        args = arguments(rows, 'source')
        args['length'] = cutoff
        set_arguments(rows, 'source', args)
        value = response_value(rows, 'source')
        value['text'] = acceptance.TEXT[:cutoff]
        set_response(rows, 'source', value)
        continuation = sdk_pair('read_source', 'source-rest', {
            'id': 'SRC1', 'chars': len(acceptance.TEXT), 'start': cutoff,
            'text': acceptance.TEXT[cutoff:]}, args={'start': cutoff, 'length': len(acceptance.TEXT)})
        create_index = next(i for i, row in enumerate(rows)
            if row['data']['message'] is message(rows, 'create'))
        rows[create_index:create_index] = continuation
    assert all(acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))[0].values())


def test_generic_three_slices_nonempty_initial_and_unordered_duplicate_requests(acceptance):
    text, sid = '甲章。乙章😀。丙章结束。', 'source-independent'
    stops = [0, 3, 7, len(text)]
    parts = [{'id': f'part-{i}', 'source_id': sid, 'title': f'部分{i}',
        'start': stops[i], 'end': stops[i + 1], 'length': stops[i + 1] - stops[i]} for i in range(3)]
    initial = [parts[0]]
    request = [{key: part[key] for key in ('title', 'start', 'end')} for part in reversed(parts)]
    request.append(request[0] | {'title': '  ' + request[0]['title'] + '  '})
    rows = successful_rows(acceptance, sid, parts, source_text=text, initial_slices=initial, requested=request)
    checks, _ = acceptance.assess_sdk(rows, sid, parts, source_text=text, initial_slices=initial)
    assert all(checks.values()), checks


@pytest.mark.parametrize('case', ['unknown_argument', 'bool_read_start', 'bool_create_start',
    'wrong_title', 'unknown_slice_field', 'receipt_error', 'receipt_problems', 'missing_validation',
    'missing_read_source_id', 'conflicting_json', 'wrong_source_binding'])
def test_strict_arguments_and_unambiguous_receipt(acceptance, case):
    rows = successful_rows(acceptance)
    if case in {'unknown_argument', 'bool_read_start'}:
        args = arguments(rows, 'source')
        args['unexpected' if case == 'unknown_argument' else 'start'] = False
        set_arguments(rows, 'source', args)
    elif case in {'bool_create_start', 'wrong_title', 'unknown_slice_field'}:
        args = arguments(rows, 'create')
        args['slices'][0].update({'bool_create_start': {'start': False}, 'wrong_title': {'title': 'different'},
            'unknown_slice_field': {'surprise': 1}}[case])
        set_arguments(rows, 'create', args)
    elif case in {'receipt_error', 'receipt_problems', 'missing_validation'}:
        value = response_value(rows, 'create')
        if case == 'missing_validation':
            value.pop('validation_status')
        else:
            value['error' if case == 'receipt_error' else 'problems'] = 'contradiction'
        set_response(rows, 'create', value)
    elif case == 'missing_read_source_id':
        value = response_value(rows, 'read-1')
        value.pop('source_id')
        set_response(rows, 'read-1', value)
    elif case == 'conflicting_json':
        message(rows, 'create', response=True)['content'].append({'type': 'text', 'text': '{"status":"staged"}'})
    else:
        message(rows, 'create', response=True)['source'] = {'kind': 'tool', 'callId': 'different-call'}
    assert not all(acceptance.assess_sdk(rows, 'SRC1', slices(acceptance))[0].values())


@pytest.mark.parametrize('case', ['duplicate_id', 'empty_id', 'bool_range', 'bad_range', 'wrong_source'])
def test_invalid_database_expected_records_fail_closed(acceptance, case):
    rows = successful_rows(acceptance)
    parts = slices(acceptance)
    if case == 'duplicate_id':
        parts[1]['id'] = parts[0]['id']
    elif case == 'empty_id':
        parts[0]['id'] = ''
    elif case == 'bool_range':
        parts[0]['start'] = False
    elif case == 'bad_range':
        parts[0]['end'] = len(acceptance.TEXT) + 1
    else:
        parts[0]['source_id'] = 'wrong'
    assert not all(acceptance.assess_sdk(rows, 'SRC1', parts)[0].values())
