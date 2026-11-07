"""One bounded real-model repair of an unknown draft root key; no retries."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import accept_screenplay as qa
from accept_source_tools import public_sdk_results, same_json
from accept_tool_loading import usage_breakdown

TEXT = '林岚在档案室整理记录。'
TASK = '请读取指定原文和提取暂存的问题，按诊断原始字段路径用一次patch_extraction_submission删除多余顶层字段，使合法内容自动入库。不要改其他内容，不新增事实，不重发整稿，不删除暂存；完成后自然结束。'
PATCH = {'op': 'remove', 'path': ['result_note']}


def original_draft():
    return {'nodes': [{'type': 'person', 'name': '林岚', 'quotes': [TEXT]}],
            'properties': [], 'result_note': '模型附加说明，不属于草稿合同'}


def seed(ops):
    ops.save_harness_settings({'provider': 'ark', 'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
        'model': 'deepseek-v4.1-flash-economy', 'key_env': 'ARK_API_KEY', 'reasoning_effort': 'low'})
    source = ops.import_source('删除多余草稿字段', TEXT)
    screenplay = ops.create_screenplay('验收空容器', reason='顶层修补隔离QA空容器')['screenplay']
    raw = original_draft()
    before = deepcopy(raw)
    staged = ops.submit_extraction(source, raw)
    errors = [p for p in staged.get('problems', []) if p.get('severity', 'error') == 'error']
    checks = {'initial_submission_staged': staged.get('status') == 'staged',
        'root_error_located': bool(errors) and all(p.get('path') == ['result_note'] and p.get('message') for p in errors),
        'original_draft_unchanged': raw == before, 'invalid_seed_not_committed': not ops.graph_snapshot()['nodes']}
    if not all(checks.values()):
        raise RuntimeError('Root repair seed is not ready: ' + json.dumps(checks))
    return {'source_id': source, 'screenplay_id': screenplay['id'], 'submission_id': staged['submission_id'],
        'submission_revision': staged['submission_revision'], 'original_draft': before,
        'initial_submission': staged, 'seed_checks': checks, 'source_before': ops.source(source),
        'screenplay_before': ops.read_screenplay(screenplay['id'])}


def assess_repair(steps, fixture, current):
    patches = [s for s in steps if s.get('tool') == 'patch_extraction_submission']
    accepted = [s for s in patches if s.get('ok') is True and s.get('result', {}).get('status') == 'accepted'
        and s['result'].get('submission_status') == 'committed' and s['result'].get('validation_status') == 'valid']
    expected = deepcopy(fixture['original_draft'])
    expected.pop('result_note')
    allowed = {'project_info', 'read_source', 'list_sources', 'list_available_tools', 'load_tools', 'get_schema',
        'read_extraction_submission', 'patch_extraction_submission', 'read_task_write_summary', 'read_usage_statistics'}
    first_patch = next((i for i, s in enumerate(steps) if s.get('tool') == 'patch_extraction_submission'), None)
    return {
        'single_patch_automatically_committed': len(patches) == len(accepted) == 1,
        'only_unknown_root_field_removed': bool(patches) and all(s.get('args', {}).get('patches') == [PATCH] for s in patches)
            and current == expected,
        'original_target_and_revision_preserved': bool(patches) and all(
            s.get('args', {}).get('submission_id') == fixture['submission_id']
            and s['args'].get('expected_revision') == fixture['submission_revision'] for s in patches),
        'no_full_resubmission_or_other_writes': all(s.get('tool') in allowed for s in steps),
        'read_diagnostics_before_patch': first_patch is not None and any(
            s.get('tool') == 'read_extraction_submission' and s.get('ok') is True
            and s.get('args', {}).get('submission_id') == fixture['submission_id'] for s in steps[:first_patch]),
        'no_tool_errors': bool(steps) and all(s.get('ok') is True for s in steps),
    }, accepted


def record_implementation(ops, fixture, report, directory, began):
    root = Path(__file__).resolve().parents[1]
    paths = [root / 'src/narrative' / name for name in ('extraction_staging.py', 'extraction_draft.py',
        'tool_api.py', 'graph_operations.py')] + [Path(__file__).resolve()]
    report['root_repair_implementation'] = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths}
    (directory / 'root-repair-implementation.json').write_text(
        json.dumps(report['root_repair_implementation'], indent=2), encoding='utf-8')


def validate(ops, fixture, screenplay, calls, report):
    directory = Path(report['audit_directory'])
    audit = json.loads((directory / 'task.json').read_text(encoding='utf-8'))
    metadata = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(directory.glob('metadata-*.json'))]
    sessions = {m['session_id'] for m in metadata if m.get('session_id')}
    paths = [p for p in (directory.parents[1] / 'home' / 'sessions').rglob('session.v4.jsonl') if p.parent.name in sessions]
    rows = [json.loads(line) for p in paths for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
    responses = public_sdk_results(rows)
    current = {section: [r['item'] for r in ops.read_extraction_submission(fixture['submission_id'], section=section)['entries']]
        for section in ('nodes', 'properties')}
    checks, accepted = assess_repair(audit.get('steps', []), fixture, current)
    checks.update(fixture['seed_checks'])
    checks['sdk_and_task_audit_committed_match'] = bool(accepted) and all(any(
        r['tool'] == s['tool'] and r['is_error'] is False and not r['binding_diagnostics']
        and same_json(r['args'], s['args']) and len(r['values']) == 1 and same_json(r['values'][0], s['result'])
        for r in responses) for s in accepted)
    graph, static = ops.graph_snapshot(), ops.check_project()
    checks.update(static_no_errors=static['counts'].get('error', 0) == 0,
        formal_person_committed=len(graph['nodes']) == 1 and graph['nodes'][0]['node_type'] == 'person'
            and graph['nodes'][0]['name'] == '林岚',
        source_unchanged=ops.source(fixture['source_id']) == fixture['source_before'],
        screenplay_unchanged=screenplay == fixture['screenplay_before'],
        no_pending_submissions=not ops.list_extraction_submissions()['submissions'])
    report['usage_breakdown'] = usage_breakdown(metadata)
    report['root_repair_evidence'] = {'original_draft': fixture['original_draft'], 'current_draft': current,
        'initial_submission': fixture['initial_submission'], 'accepted_steps': accepted,
        'sdk_responses': responses, 'graph': graph, 'static_check': static}
    return checks


def main():
    qa.seed, qa.TEXT, qa.TASK = seed, TEXT, TASK
    qa.PROMPT_VERSION = 'extraction-root-repair-real/1'
    qa.FIXTURE_ORIGIN = 'New isolated invalid draft. Model removes only an unknown root key; no host repairs, retries or seeded formal facts.'
    return qa.main(validate=validate, required_tools=['read_extraction_submission', 'patch_extraction_submission'],
        prompt_factory=lambda f: TASK + '\nsource_id=' + f['source_id'] + '\nsubmission_id=' + f['submission_id'],
        use_writing_target=False, allow_extraction_change=True, on_started=record_implementation)


if __name__ == '__main__':
    raise SystemExit(main())
