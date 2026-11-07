"""One isolated real-model draft deletion, with no retries or formal writes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import accept_screenplay as qa
from accept_source_tools import public_sdk_results, same_json
from accept_tool_loading import usage_breakdown

TEXT = '林岚在档案室整理借阅记录。'
TASK = '请读取指定的未通过提取暂存，确认其当前修订后，用delete_extraction_submission物理删除该暂存。仅删除这份草稿，不修补、不重新提交，不创建任何正式事实，不修改原文或其他工程数据。完成后自然结束。'


def seed(ops):
    ops.save_harness_settings({'provider': 'ark', 'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
        'model': 'deepseek-v4.1-flash-economy', 'key_env': 'ARK_API_KEY', 'reasoning_effort': 'low'})
    source = ops.import_source('未通过草稿删除', TEXT)
    screenplay = ops.create_screenplay('验收空容器', reason='删除暂存隔离QA空容器')['screenplay']
    raw = {'nodes': [{'type': 'person', 'name': '林岚'}], 'properties': []}
    staged = ops.submit_extraction(source, raw)
    if staged.get('status') != 'staged' or ops.graph_snapshot()['nodes']:
        raise RuntimeError('Expected an invalid staged draft without formal facts')
    return {'source_id': source, 'screenplay_id': screenplay['id'],
        'submission_id': staged['submission_id'], 'submission_revision': staged['submission_revision'],
        'initial_submission': staged, 'raw_draft': raw, 'source_before': ops.source(source),
        'graph_before': ops.graph_snapshot(), 'records_before': ops.property_records(),
        'screenplay_before': ops.read_screenplay(screenplay['id'])}


def assess_delete(steps, fixture):
    deletes = [s for s in steps if s.get('tool') == 'delete_extraction_submission']
    accepted = [s for s in deletes if s.get('ok') is True
        and s.get('result', {}).get('status') == 'accepted'
        and s['result'].get('deleted_submission_id') == fixture['submission_id']]
    reads = [s for s in steps if s.get('tool') == 'read_extraction_submission'
        and s.get('args', {}).get('submission_id') == fixture['submission_id']
        and s.get('ok') is True]
    first_delete = next((i for i, s in enumerate(steps) if s.get('tool') == 'delete_extraction_submission'), None)
    read_first = first_delete is not None and any(s in reads for s in steps[:first_delete])
    allowed = {'project_info', 'list_available_tools', 'load_tools', 'get_schema', 'list_sources',
               'read_source', 'read_extraction_submission', 'list_extraction_submissions',
               'delete_extraction_submission', 'statistics', 'read_project_state',
               'read_task_write_summary', 'read_usage_statistics'}
    return {
        'single_delete_succeeded': len(deletes) == len(accepted) == 1,
        'read_current_draft_before_delete': bool(reads) and read_first,
        'delete_target_and_revision_preserved': bool(deletes) and all(
            s.get('args', {}).get('submission_id') == fixture['submission_id']
            and s['args'].get('expected_revision') == fixture['submission_revision']
            and bool(str(s['args'].get('reason', '')).strip()) for s in deletes),
        'no_repair_resubmission_or_other_writes': all(s.get('tool') in allowed for s in steps),
        'no_tool_errors': bool(steps) and all(s.get('ok') is True for s in steps),
    }, accepted


def submission_missing(ops, identifier):
    try:
        ops.read_extraction_submission(identifier)
    except ValueError as error:
        return str(error) == '提取暂存不存在'
    return False


def record_implementation(ops, fixture, report, directory, began):
    root = Path(__file__).resolve().parents[1]
    paths = [root / 'src/narrative' / name for name in ('extraction_staging.py',
        'graph_operations.py', 'tool_api.py', 'tool_loading.py')]
    paths.append(Path(__file__).resolve())
    report['draft_delete_implementation'] = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths}
    (directory / 'draft-delete-implementation.json').write_text(
        json.dumps(report['draft_delete_implementation'], indent=2), encoding='utf-8')


def validate(ops, fixture, screenplay, calls, report):
    directory = Path(report['audit_directory'])
    audit = json.loads((directory / 'task.json').read_text(encoding='utf-8'))
    metadata = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(directory.glob('metadata-*.json'))]
    sessions = {m['session_id'] for m in metadata if m.get('session_id')}
    paths = [p for p in (directory.parents[1] / 'home' / 'sessions').rglob('session.v4.jsonl') if p.parent.name in sessions]
    rows = [json.loads(line) for p in paths for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
    responses = public_sdk_results(rows)
    checks, accepted = assess_delete(audit.get('steps', []), fixture)
    checks['sdk_and_task_audit_delete_match'] = bool(accepted) and all(any(
        r['tool'] == s['tool'] and r['is_error'] is False and not r['binding_diagnostics']
        and same_json(r['args'], s['args']) and len(r['values']) == 1 and same_json(r['values'][0], s['result'])
        for r in responses) for s in accepted)
    graph, records = ops.graph_snapshot(), ops.property_records()
    pending = ops.list_extraction_submissions()['submissions']
    static = ops.check_project()
    checks.update(deleted_draft_unreadable=submission_missing(ops, fixture['submission_id']),
        no_pending_drafts=not pending, graph_and_revision_unchanged=graph == fixture['graph_before'],
        formal_properties_unchanged=records == fixture['records_before'],
        source_unchanged=ops.source(fixture['source_id']) == fixture['source_before'],
        screenplay_unchanged=screenplay == fixture['screenplay_before'],
        static_no_errors=static['counts'].get('error', 0) == 0)
    report['usage_breakdown'] = usage_breakdown(metadata)
    report['draft_delete_evidence'] = {'initial_submission': fixture['initial_submission'],
        'raw_draft': fixture['raw_draft'], 'sdk_responses': responses, 'accepted_steps': accepted,
        'graph': graph, 'records': records, 'pending': pending, 'static_check': static}
    return checks


def main():
    qa.seed, qa.TEXT, qa.TASK = seed, TEXT, TASK
    qa.PROMPT_VERSION = 'extraction-draft-delete-real/1'
    qa.FIXTURE_ORIGIN = 'New isolated invalid person draft; no formal facts. One real task reads and deletes only the selected draft, no repair, resubmission, warmup or retries.'
    return qa.main(validate=validate, required_tools=['read_extraction_submission', 'delete_extraction_submission'],
        prompt_factory=lambda fixture: TASK + '\nsubmission_id=' + fixture['submission_id'],
        use_writing_target=False, on_started=record_implementation)


if __name__ == '__main__':
    raise SystemExit(main())
