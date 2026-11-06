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
        'screenplay_before': ops.read_screenplay(script['id'])}


def assess_slices(slices):
    ranges = [(s.get('start'), s.get('end')) for s in slices]
    return {'two_chapters_exactly_covered': ranges == [(0, BOUNDARY), (BOUNDARY, len(TEXT))]}


def public_sdk_results(rows):
    """Link public SDK responses to their actual calls, including rejected outputs."""
    calls = {}
    messages = [row.get('data', {}).get('message', {}) for row in rows]
    for message in messages:
        for block in message.get('content', []) or []:
            if not isinstance(block, dict) or block.get('type') not in {'tool-call', 'tool_use', 'tool_call'}:
                continue
            name = str(block.get('name', '')).rsplit('/', 1)[-1].rsplit('.', 1)[-1]
            args = block.get('arguments', block.get('input', {}))
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                continue
            if block.get('id') and isinstance(args, dict):
                calls[block['id']] = (name, args)
    responses = []
    for message in messages:
        call_id = message.get('toolCallId')
        if message.get('role') != 'tool' or call_id not in calls:
            continue
        name, args = calls[call_id]
        values = []
        for block in message.get('content', []) or []:
            if isinstance(block, dict) and block.get('type') == 'text':
                try:
                    values.append(json.loads(block.get('text', '')))
                except (json.JSONDecodeError, TypeError):
                    pass
        responses.append({'tool': name, 'args': args, 'is_error': message.get('isError'),
            'values': values, 'content': message.get('content')})
    return responses


def assess_sdk(rows, source_id, slices):
    responses = public_sdk_results(rows)
    successful = [r for r in responses if r['is_error'] is False]
    created = [v for r in successful if r['tool'] == 'create_source_slices' for v in r['values']
        if isinstance(v, dict) and v.get('source_id') == source_id and v.get('slices') == slices
        and v.get('status') == 'accepted' and v.get('submission_status') == 'committed']
    expected = {s['id']: s for s in slices}
    read_ids = {v.get('id') for r in successful if r['tool'] == 'read_source_slice' for v in r['values']
        if isinstance(v, dict) and v.get('id') in expected
        and v.get('source_id', source_id) == source_id
        and (v.get('start'), v.get('end')) == (expected[v['id']]['start'], expected[v['id']]['end'])
        and v.get('text') == TEXT[v['start']:v['end']]}
    return {'sdk_create_committed': bool(created),
        'sdk_read_each_slice_exact_text': len(slices) == 2 and {s['id'] for s in slices} <= read_ids}, responses


def validate(ops, fixture, screenplay, calls, report):
    directory = Path(report['audit_directory'])
    audit = json.loads((directory / 'task.json').read_text(encoding='utf-8'))
    metadata = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(directory.glob('metadata-*.json'))]
    sessions = {m['session_id'] for m in metadata if m.get('session_id')}
    paths = [p for p in (directory.parents[1] / 'home' / 'sessions').rglob('session.v4.jsonl') if p.parent.name in sessions]
    rows = [json.loads(line) for p in paths for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
    slices = ops.source_slices(fixture['source_id'])
    sdk_checks, sdk_responses = assess_sdk(rows, fixture['source_id'], slices)
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
