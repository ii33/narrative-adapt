"""One isolated source-tool task; run with --seconds 600 --token-budget 100000.

No service request is made by importing this module. Only the coordinator runs QA.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import accept_screenplay as qa
from accept_tool_loading import usage_breakdown

TEXT = '第一章 来客\n😀林舟推开书店的门，向店主问好。\n\n第二章 归途\n傍晚，林舟带着旧书沿河回家。'
TASK = '请列出工程原文并读取内容，查看现有切片。本次验证按坐标提交接口：通过create_source_slices按两章边界一次批量创建两个连续切片，完整覆盖原文；再逐一读取新切片核对文本。不修改原文，不提取节点、属性或改编剧本。只通过工程工具完成。'
BOUNDARY = TEXT.index('第二章')
MODEL = 'deepseek-v4.1-flash-economy'


def record_implementation(ops, fixture, report, directory, began):
    root = Path(__file__).resolve().parents[1]
    paths = [root / 'src/narrative/tool_api.py', root / 'src/narrative/source_tools.py', Path(__file__).resolve()]
    report['source_tool_implementation'] = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths}
    (directory / 'source-tool-implementation.json').write_text(
        json.dumps(report['source_tool_implementation'], indent=2), encoding='utf-8')


def seed(ops):
    from narrative.providers import preset
    profile = preset('ark')
    ops.save_harness_settings({'provider': 'ark', 'model': MODEL,
        'base_url': profile['base_url'], 'key_env': profile['key_env'],
        'reasoning_effort': 'low', 'token_parameter': profile['token_parameter']})
    source_id = ops.import_source('两章原文工具验收', TEXT)
    script = ops.create_screenplay('验收空容器', reason='独立QA只读空容器')['screenplay']
    return {'source_id': source_id, 'screenplay_id': script['id'],
        'source_before': ops.source(source_id), 'properties_before': ops.property_records(),
        'initial_slices': ops.source_slices(source_id),
        'screenplay_before': ops.read_screenplay(script['id'])}


def assess_slices(slices):
    ranges = [(s.get('start'), s.get('end')) for s in slices]
    return {'two_chapters_exactly_covered': ranges == [(0, BOUNDARY), (BOUNDARY, len(TEXT))]}


def same_json(left, right):
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(right, sort_keys=True, ensure_ascii=False)


def public_sdk_results(rows):
    """Preserve ordered public call/result bindings; ambiguity fails closed."""
    calls, results, diagnostics = {}, [], []
    for index, row in enumerate(rows):
        message = row.get('data', {}).get('message', {})
        for block in message.get('content', []) or []:
            if not isinstance(block, dict) or block.get('type') not in {'tool-call', 'tool_use', 'tool_call'}:
                continue
            call_id = block.get('id')
            if message.get('role') != 'assistant' or not isinstance(call_id, str) or not call_id:
                diagnostics.append('tool call lacks assistant ownership or call ID')
                continue
            if call_id in calls:
                diagnostics.append('duplicate tool call ID')
                continue
            args = block.get('arguments', block.get('input', {}))
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                args = None
            if not isinstance(args, dict):
                diagnostics.append('tool arguments are not a JSON object')
            calls[call_id] = {'tool': str(block.get('name', '')).rsplit('/', 1)[-1].rsplit('.', 1)[-1],
                'args': args, 'call_index': index, 'responded': False}
        if message.get('role') != 'tool':
            continue
        call_id = message.get('toolCallId')
        source = message.get('source')
        if isinstance(source, dict) and source.get('callId', call_id) != call_id:
            diagnostics.append('response source call ID disagrees with toolCallId')
        call = calls.get(call_id) if isinstance(call_id, str) else None
        if call is None:
            diagnostics.append('response has no preceding assistant call')
            continue
        if call['responded']:
            diagnostics.append('duplicate tool response')
            continue
        call['responded'] = True
        values = []
        for block in message.get('content', []) or []:
            if isinstance(block, dict) and block.get('type') == 'text':
                try:
                    value = json.loads(block.get('text', ''))
                    if not any(same_json(value, existing) for existing in values):
                        values.append(value)
                except (json.JSONDecodeError, TypeError):
                    pass
        if len(values) > 1:
            diagnostics.append('contradictory JSON values in one tool response')
        results.append({key: call[key] for key in ('tool', 'args', 'call_index')} | {
            'call_id': call_id, 'result_index': index, 'is_error': message.get('isError'),
            'values': values, 'content': message.get('content')})
    if any(not call['responded'] for call in calls.values()):
        diagnostics.append('tool call has no response')
    for result in results:
        result['binding_diagnostics'] = list(dict.fromkeys(diagnostics))
    if diagnostics and not results:
        results.append({'tool': '', 'args': {}, 'is_error': None, 'values': [],
            'binding_diagnostics': list(dict.fromkeys(diagnostics))})
    return results


def assess_sdk(rows, source_id, slices, *, source_text=None, initial_slices=None):
    source_text = TEXT if source_text is None else source_text
    initial_slices = [] if initial_slices is None else initial_slices
    responses = public_sdk_results(rows)
    bindings_ok = bool(responses) and not any(r['binding_diagnostics'] for r in responses)
    argument_keys = {'list_sources': set(), 'read_source': {'source_id', 'start', 'length'},
        'list_source_slices': {'source_id'}, 'create_source_slices': {'source_id', 'slices'},
        'read_source_slice': {'source_id', 'slice_id'}}

    def integer(number):
        return isinstance(number, int) and not isinstance(number, bool)

    def valid_arguments(row):
        args = row['args']
        if not isinstance(args, dict):
            return False
        if row['tool'] not in argument_keys:
            return True
        return (not set(args) - argument_keys[row['tool']] and
            all(key not in args or integer(args[key]) for key in ('start', 'length')) and
            ('source_id' not in args or isinstance(args['source_id'], str)))

    arguments_ok = all(valid_arguments(r) for r in responses)
    successful = [r for r in responses if bindings_ok and valid_arguments(r) and r['is_error'] is False
        and len(r['values']) == 1]

    def value(row):
        return row['values'][0]

    listings = [r for r in successful if r['tool'] == 'list_sources' and isinstance(value(r), dict)
        and isinstance(value(r).get('sources'), list)]

    def has_source(row):
        supplied = row['args'].get('source_id')
        if supplied not in (None, ''):
            return supplied == source_id
        preceding = [item for item in listings if item['result_index'] < row['call_index']]
        if not preceding:
            return False
        sources = value(max(preceding, key=lambda item: item['result_index']))['sources']
        return len(sources) == 1 and isinstance(sources[0], dict) and sources[0].get('id') == source_id

    source_reads = []
    for row in successful:
        data, args = value(row), row['args']
        if row['tool'] != 'read_source' or not has_source(row) or not isinstance(data, dict):
            continue
        start, length = args.get('start') or 0, args.get('length') or 2000
        if not integer(start) or not integer(length):
            continue
        start, length = max(0, start), max(1, length)
        text = source_text[start:start + length]
        if (data.get('id') == source_id and integer(data.get('chars')) and data['chars'] == len(source_text)
                and integer(data.get('start')) and data['start'] == start and data.get('text') == text):
            source_reads.append((row, start, start + len(text)))

    existing = [r for r in successful if r['tool'] == 'list_source_slices' and has_source(r)
        and isinstance(value(r), dict) and value(r).get('source_id') == source_id
        and same_json(value(r).get('slices'), initial_slices)]

    def covered_before(index):
        end = 0
        for _, start, stop in sorted((r for r in source_reads if r[0]['result_index'] < index), key=lambda item: item[1]):
            if start > end:
                return False
            end = max(end, stop)
        return end == len(source_text)

    def identity(item):
        if (not isinstance(item, dict) or not isinstance(item.get('title'), str) or not item['title'].strip()
                or not integer(item.get('start')) or not integer(item.get('end'))
                or not 0 <= item['start'] < item['end'] <= len(source_text)):
            return None
        return item['title'].strip(), item['start'], item['end']

    def exact_requested_slices(requested):
        if not isinstance(requested, list) or not requested:
            return False
        if any(not isinstance(item, dict) or set(item) != {'title', 'start', 'end'} for item in requested):
            return False
        requested_keys = [identity(item) for item in requested]
        initial_keys = [identity(item) for item in initial_slices]
        final_keys = [identity(item) for item in slices]
        if (any(key is None for key in requested_keys + initial_keys + final_keys)
                or len(set(final_keys)) != len(final_keys)):
            return False
        if set(final_keys) != set(initial_keys) | set(requested_keys):
            return False
        final_by_key = dict(zip(final_keys, slices, strict=True))
        # Existing generated IDs/metadata are immutable navigation records.
        return all(same_json(final_by_key[key], original) for key, original in zip(initial_keys, initial_slices, strict=True))

    creates = [r for r in successful if r['tool'] == 'create_source_slices' and has_source(r)
        and exact_requested_slices(r['args'].get('slices')) and isinstance(value(r), dict)
        and value(r).get('source_id') == source_id and same_json(value(r).get('slices'), slices)
        and value(r).get('status') == 'accepted' and value(r).get('submission_status') == 'committed'
        and value(r).get('validation_status') == 'valid' and not value(r).get('error')
        and value(r).get('problems', []) == []]
    workflow_creates = [r for r in creates if covered_before(r['call_index'])
        and any(item['result_index'] < r['call_index'] for item in existing)
        and any(item['result_index'] < r['call_index'] and any(
            isinstance(src, dict) and src.get('id') == source_id for src in value(item)['sources']) for item in listings)]
    expected_ok = bool(slices) and all(isinstance(s, dict) and isinstance(s.get('id'), str) and s['id']
        and isinstance(s.get('title'), str) and s['title'].strip()
        and integer(s.get('start')) and integer(s.get('end')) and 0 <= s['start'] < s['end'] <= len(source_text)
        and s.get('source_id', source_id) == source_id for s in slices)
    expected = {s['id']: s for s in slices} if expected_ok else {}
    expected_ok = expected_ok and len(expected) == len(slices)
    read_ids = set()
    for row in successful:
        data = value(row)
        requested_id = row['args'].get('slice_id')
        if (row['tool'] != 'read_source_slice' or not has_source(row) or not isinstance(requested_id, str) or requested_id not in expected
                or not isinstance(data, dict) or data.get('id') != requested_id
                or not any(create['result_index'] < row['call_index'] for create in workflow_creates)):
            continue
        actual = expected[requested_id]
        if (data.get('source_id') == source_id and integer(data.get('start')) and integer(data.get('end')) and
                (data.get('start'), data.get('end')) == (actual['start'], actual['end']) and
                data.get('text') == source_text[actual['start']:actual['end']]):
            read_ids.add(requested_id)
    return {'sdk_unambiguous_bindings': bindings_ok,
        'sdk_arguments_valid': arguments_ok, 'sdk_expected_slices_valid': bool(expected_ok),
        'sdk_create_committed': bool(workflow_creates) and bool(expected_ok),
        'sdk_read_each_slice_exact_text': bool(expected_ok) and set(expected) <= read_ids}, responses


def validate(ops, fixture, screenplay, calls, report):
    directory = Path(report['audit_directory'])
    audit = json.loads((directory / 'task.json').read_text(encoding='utf-8'))
    metadata = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(directory.glob('metadata-*.json'))]
    sessions = {m['session_id'] for m in metadata if m.get('session_id')}
    paths = [p for p in (directory.parents[1] / 'home' / 'sessions').rglob('session.v4.jsonl') if p.parent.name in sessions]
    rows = [json.loads(line) for p in paths for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
    slices = ops.source_slices(fixture['source_id'])
    sdk_checks, sdk_responses = assess_sdk(rows, fixture['source_id'], slices,
        source_text=fixture['source_before']['text'], initial_slices=fixture['initial_slices'])
    create_steps = [s for s in audit.get('steps', []) if s.get('tool') == 'create_source_slices']
    report['usage_breakdown'] = usage_breakdown(metadata)
    report['source_tool_evidence'] = {'slices': slices, 'sdk_responses': sdk_responses,
        'create_steps': create_steps, 'session_files': [str(p) for p in paths],
        'review': 'The model authors slice titles and coordinates; no manual repair or host-generated tool results.'}
    return assess_slices(slices) | sdk_checks | {
        'single_create_attempt': len(create_steps) == 1,
        'same_sdk_session': len(sessions) == 1,
        'source_unchanged': ops.source(fixture['source_id']) == fixture['source_before'],
        'properties_unchanged': ops.property_records() == fixture['properties_before'],
        'screenplay_unchanged': screenplay == fixture['screenplay_before']}


def main():
    qa.seed, qa.TEXT, qa.TASK = seed, TEXT, TASK
    qa.PROMPT_VERSION = 'source-tools-registration-real/1'
    qa.FIXTURE_ORIGIN = 'New isolated two-chapter Unicode source; one real task, no warmup, retries or manual result repair.'
    return qa.main(validate=validate, required_tools=['list_sources', 'read_source', 'list_source_slices',
        'create_source_slices', 'read_source_slice'], use_writing_target=False, prompt_factory=lambda _: TASK,
        on_started=record_implementation)


if __name__ == '__main__':
    raise SystemExit(main())
