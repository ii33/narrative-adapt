"""One bounded real repair task for exact diagnostic paths; never retries."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import accept_screenplay as qa
from accept_source_tools import public_sdk_results, same_json
from accept_tool_loading import usage_breakdown

TEXT = '林岚在档案室向周宁宣布暂停发布名单，周宁同意暂缓发布。'
TASK = '请读取指定原文和提取暂存的检查问题，依据原始字段位置在一次patch_extraction_submission中局部修复已明确的问题，使其自动入库。保留其余内容，不重发整稿、不新增事实，不重复定位或复核已通过的数据。完成后自然结束。'
EXPECTED_PATHS = {('nodes', 0, 'quotes'), ('properties', 0, 'value')}


def original_draft():
    return {'nodes': [
        {'type': 'person', 'name': '林岚'},
        {'type': 'person', 'name': '周宁', 'quotes': ['周宁同意暂缓发布']},
        {'type': 'location', 'name': '档案室', 'quotes': ['档案室']},
        {'type': 'scene', 'name': '档案室宣布暂缓发布', 'quotes': [TEXT]},
        {'type': 'event', 'name': '宣布暂停发布名单并获同意', 'quotes': [TEXT],
         'scene': '@4', 'participants': ['@1', '@2'], 'location': '@3'},
    ], 'properties': [
        {'node': '@5', 'property': '发布状态'  , 'op': 'set', 'value_type': 'string',
         'scope': 'world', 'at_event': '@5', 'quotes': ['宣布暂停发布名单']},
    ]}


def assess_initial_diagnostics(problems):
    errors = [p for p in problems if p.get('severity', 'error') == 'error']
    paths = {tuple(p['path']) for p in errors if isinstance(p.get('path'), list)}
    return {'both_original_error_fields_located': EXPECTED_PATHS <= paths,
            'all_errors_have_path_and_reason': bool(errors) and all(
                isinstance(p.get('path'), list) and bool(p['path'])
                and isinstance(p.get('message'), str) and bool(p['message'].strip()) for p in errors)}


def seed(ops):
    ops.save_harness_settings({'provider': 'ark', 'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
        'model': 'deepseek-v4.1-flash-economy', 'key_env': 'ARK_API_KEY', 'reasoning_effort': 'low'})
    source = ops.import_source('准确诊断位置与局部修补', TEXT)
    screenplay = ops.create_screenplay('验收空容器', reason='隔离诊断QA空容器')['screenplay']
    draft = original_draft()
    before = deepcopy(draft)
    staged = ops.submit_extraction(source, draft)
    node_rows = ops.read_extraction_submission(staged['submission_id'], section='nodes')['entries']
    property_rows = ops.read_extraction_submission(staged['submission_id'], section='properties')['entries']
    seed_checks = assess_initial_diagnostics(staged['problems']) | {
        'initial_submission_staged': staged.get('status') == 'staged',
        'invalid_seed_not_committed': not ops.graph_snapshot()['nodes'],
        'caller_draft_unchanged': draft == before,
        'staged_draft_unchanged': [r['item'] for r in node_rows] == draft['nodes']
            and [r['item'] for r in property_rows] == draft['properties'],
    }
    if not all(seed_checks.values()):
        raise RuntimeError('Diagnostic acceptance seed is not ready: ' + json.dumps(seed_checks))
    return {'source_id': source, 'screenplay_id': screenplay['id'], 'submission_id': staged['submission_id'],
        'source_before': ops.source(source), 'screenplay_before': ops.read_screenplay(screenplay['id']),
        'original_draft': before, 'initial_submission': staged, 'seed_checks': seed_checks}


def assess_repair(steps, fixture, current):
    patches = [s for s in steps if s.get('tool') == 'patch_extraction_submission']
    accepted = [s for s in patches if s.get('ok') is True and s.get('result', {}).get('status') == 'accepted'
        and s['result'].get('submission_status') == 'committed' and s['result'].get('validation_status') == 'valid']
    unchanged = deepcopy(current)
    unchanged['nodes'][0].pop('quotes', None)
    unchanged['properties'][0].pop('value', None)
    changed = {tuple((p.get('section'), p.get('index'), p.get('field')))
        for s in patches for p in s.get('args', {}).get('patches', [])}
    return {
        'single_patch_automatically_committed': len(patches) == 1 and len(accepted) == 1,
        'repair_target_preserved': all(s.get('args', {}).get('submission_id') == fixture['submission_id'] for s in patches),
        'only_reported_fields_changed': changed == EXPECTED_PATHS and unchanged == fixture['original_draft'],
        'no_full_resubmission': not any(s.get('tool') == 'submit_extraction' for s in steps),
        'no_tool_errors': all(s.get('ok') is True for s in steps),
    }, accepted


def record_implementation(ops, fixture, report, directory, began):
    root = Path(__file__).resolve().parents[1]
    paths = [root / 'src/narrative' / name for name in ('extraction.py', 'extraction_refs.py',
        'extraction_draft.py', 'extraction_staging.py', 'content_operations.py')]
    paths.append(Path(__file__).resolve())
    report['diagnostic_implementation'] = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths}
    (directory / 'diagnostic-implementation.json').write_text(
        json.dumps(report['diagnostic_implementation'], indent=2), encoding='utf-8')


def validate(ops, fixture, screenplay, calls, report):
    directory = Path(report['audit_directory'])
    audit = json.loads((directory / 'task.json').read_text(encoding='utf-8'))
    metadata = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(directory.glob('metadata-*.json'))]
    sessions = {m['session_id'] for m in metadata if m.get('session_id')}
    paths = [p for p in (directory.parents[1] / 'home' / 'sessions').rglob('session.v4.jsonl') if p.parent.name in sessions]
    rows = [json.loads(line) for p in paths for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
    responses = public_sdk_results(rows)
    identifier = fixture['submission_id']
    current = {section: [r['item'] for r in ops.read_extraction_submission(identifier, section=section)['entries']]
        for section in ('nodes', 'properties')}
    steps = audit.get('steps', [])
    checks, accepted = assess_repair(steps, fixture, current)
    checks.update(fixture['seed_checks'])
    checks['sdk_and_task_audit_committed_match'] = bool(accepted) and all(any(
        r['tool'] == s['tool'] and r['is_error'] is False and not r['binding_diagnostics']
        and same_json(r['args'], s['args']) and len(r['values']) == 1 and same_json(r['values'][0], s['result'])
        for r in responses) for s in accepted)
    graph, static = ops.graph_snapshot(), ops.check_project()
    checks.update(static_no_errors=static['counts'].get('error', 0) == 0,
        formal_event_committed=any(n.get('node_type') == 'event' for n in graph['nodes']),
        source_unchanged=ops.source(fixture['source_id']) == fixture['source_before'],
        screenplay_unchanged=screenplay == fixture['screenplay_before'])
    report['usage_breakdown'] = usage_breakdown(metadata)
    report['diagnostic_evidence'] = {'current_draft': current, 'original_draft': fixture['original_draft'],
        'initial_submission': fixture['initial_submission'], 'sdk_responses': responses,
        'accepted_steps': accepted, 'graph': graph, 'static_check': static}
    return checks


def main():
    qa.seed, qa.TEXT, qa.TASK = seed, TEXT, TASK
    qa.PROMPT_VERSION = 'diagnostic-locations-real/1'
    qa.FIXTURE_ORIGIN = 'New isolated invalid staged draft; no seeded formal facts. Real model repairs only diagnosed fields in one task, no retries or host repairs.'
    return qa.main(validate=validate, required_tools=['read_source', 'read_extraction_submission', 'patch_extraction_submission'],
        prompt_factory=lambda fixture: TASK + '\nsource_id=' + fixture['source_id'] + '\nsubmission_id=' + fixture['submission_id'],
        use_writing_target=False, allow_extraction_change=True, on_started=record_implementation)


if __name__ == '__main__':
    raise SystemExit(main())
