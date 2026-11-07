"""One isolated optional-source generation task; no host repair or retry."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import accept_screenplay as qa
from accept_source_tools import public_sdk_results, same_json
from accept_tool_loading import usage_breakdown

FIRST = '清晨，林岚在档案室核对登记，确认自己的借阅申请已经获准。'
SECOND = '当天夜里，林岚在车站遇见周宁，向他报平安，两人约定次日会合。'
TEXT = FIRST + '\n' + SECOND
TASK = '''请只为指定段落生成一场极简场表和一个简短动作正文块，内容仅限段落中的清晨档案室事件，不能写夜间车站事件。场表只填必需概要和标题即可，不填场号或其他非必要字段。
本次明确省略plan.source_event_ids，验证它是可选信息；不要在正文中手写来源注记或未标注提示。先读read_scene_table_context再提交场表，然后read_script_unit_context并提交本场正文，使用实际修订与指纹。不要修改原文、提取、改编和本剧本时间线，不添加范围外情节，不为验收补事实；正常内容错误仅按诊断局部修补，不重发整稿。完成正文后自然结束。'''


def seed(ops):
    ops.save_harness_settings({'provider': 'ark', 'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
        'model': 'deepseek-v4.1-flash-economy', 'key_env': 'ARK_API_KEY', 'reasoning_effort': 'low'})
    source = ops.import_source('清晨档案室与夜间车站', TEXT)
    result = ops.submit_extraction(source, {'nodes': [
        {'type': 'person', 'name': '林岚', 'quotes': [FIRST]},
        {'type': 'person', 'name': '周宁', 'quotes': [SECOND]},
        {'type': 'location', 'name': '档案室', 'quotes': ['档案室']},
        {'type': 'location', 'name': '车站', 'quotes': ['车站']},
        {'type': 'scene', 'name': '清晨档案室确认借阅'},
        {'type': 'scene', 'name': '夜间车站相约会合'},
        {'type': 'event', 'name': '林岚在档案室确认借阅获准', 'scene': '@5',
         'participants': ['@1'], 'location': '@3', 'quotes': [FIRST]},
        {'type': 'event', 'name': '林岚在车站向周宁报平安并约定会合', 'scene': '@6',
         'participants': ['@1', '@2'], 'location': '@4', 'quotes': [SECOND]},
    ], 'properties': []})
    if result.get('status') != 'accepted':
        raise RuntimeError('Current-contract isolated input did not commit: ' + json.dumps(result, ensure_ascii=False))
    events = sorted((n for n in ops.graph_snapshot()['nodes'] if n['node_type'] == 'event'),
                    key=lambda n: n['disclosure']['start'])
    event_ids = [n['id'] for n in events]
    imported = ops.import_adaptation(event_ids, expected_revision=0, reason='隔离来源标注输入')
    screenplay = ops.create_screenplay('可选来源与未用事件验收', reason='隔离可选来源剧本')['screenplay']
    timeline = ops.import_screenplay_timeline(screenplay['id'], event_ids, 0,
        imported['revision'], '仅导入两个已选事件')
    span = ops.create_adaptation_span(screenplay['id'], '仅首个事件', selection_ids=event_ids[:1],
        expected_timeline_revision=timeline['revision'], reason='隔离来源标注范围')['span']
    segment = ops.create_script_segment(screenplay['id'], span['id'], '清晨档案室',
        screenplay['structure_revision'], reason='隔离来源标注段落')
    return {'source_id': source, 'screenplay_id': screenplay['id'], 'segment_id': segment['id'],
        'span_id': span['id'], 'timeline_id': timeline['timeline_id'], 'timeline_revision': timeline['revision'],
        'event_ids': event_ids, 'event_names': {n['id']: n['name'] for n in events},
        'source_before': ops.source(source), 'timeline_before': ops.read_screenplay_timeline(screenplay['id'])}


def assess_usage(document, fixture, elements, outputs):
    units = [u for s in document['segments'] for u in s['units']
             if u.get('enabled', True) and u.get('kind') == 'scene']
    usage = document.get('event_usage', {})
    first, second = fixture['event_ids']
    return {
        'one_scene_has_real_body': len(units) == 1 and bool(units[0].get('blocks')),
        'optional_sources_remain_empty': bool(units) and all(not u.get('plan', {}).get('source_event_ids') for u in units),
        'completed_persistent_generation_status': usage.get('status') == 'completed',
        'coverage_uses_current_local_timeline': usage.get('timeline_id') == fixture['timeline_id'],
        'unbound_scope_is_unknown_not_used': usage.get('unknown_event_ids') == [first]
            and usage.get('used_event_ids') == [] and first not in usage.get('unused_event_ids', []),
        'unselected_timeline_event_is_unused': usage.get('unused_event_ids') == [second]
            and second not in usage.get('unknown_event_ids', []),
        'partial_unknown_does_not_erase_known_unused': usage.get('known') is False
            and usage.get('unused_event_ids') == [second],
        'no_missing_source_notes_or_body_annotations': all(not u.get('source_reference_text') for u in units)
            and not any(e.get('kind') == 'source_reference' for e in elements)
            and all('尚未标注' not in output and '依据（剧本改编时间线）' not in output for output in outputs.values()),
    }


def raw_calls_leave_sources_unbound(initial_calls):
    return bool(initial_calls) and all(isinstance(units, list) and bool(units) and all(
        isinstance(unit, dict) and isinstance(unit.get('plan'), dict)
        and not unit['plan'].get('source_event_ids') for unit in units) for units in initial_calls)


def record_implementation(ops, fixture, report, directory, began):
    root = Path(__file__).resolve().parents[1]
    names = ['screenplay_writing.py', 'screenplay_scene_policy.py', 'screenplay_tools.py',
             'screenplay_operations.py', 'screenplay_generation_status.py', 'prompt_defaults.py',
             'qml/ScreenplayPage.qml', 'qml/ScriptRangePicker.qml']
    paths = [root / 'src/narrative' / name for name in names] + [Path(__file__).resolve()]
    report['optional_usage_implementation'] = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths if p.is_file()}
    (directory / 'optional-usage-implementation.json').write_text(
        json.dumps(report['optional_usage_implementation'], indent=2), encoding='utf-8')


def validate(ops, fixture, screenplay, calls, report):
    compiled = {format: ops.compile_screenplay(fixture['screenplay_id'], format) for format in ('text', 'html', 'fountain')}
    outputs = {format: result['text'] for format, result in compiled.items()}
    outputs['legacy_text'] = ops.export_screenplay(fixture['screenplay_id'], 'text')['text']
    checks = assess_usage(screenplay, fixture, compiled['text']['elements'], outputs)
    directory = Path(report['audit_directory'])
    audit = json.loads((directory / 'task.json').read_text(encoding='utf-8'))
    metadata = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(directory.glob('metadata-*.json'))]
    sessions = {m['session_id'] for m in metadata if m.get('session_id')}
    paths = [p for p in (directory.parents[1] / 'home' / 'sessions').rglob('session.v4.jsonl') if p.parent.name in sessions]
    rows = [json.loads(line) for p in paths for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
    responses = public_sdk_results(rows)
    accepted = [s for s in audit.get('steps', []) if s.get('tool') in {
        'submit_scene_table', 'submit_script_unit', 'patch_content_submission'}
        and s.get('ok') is True and s.get('result', {}).get('status') in {'accepted', 'unchanged'}]
    committed_operations = {s.get('result', {}).get('operation') or s['tool'] for s in accepted}
    checks['model_scene_table_and_body_committed'] = {'submit_scene_table', 'submit_script_unit'} <= committed_operations
    checks['sdk_and_task_audit_committed_match'] = bool(accepted) and all(any(
        r['tool'] == s['tool'] and r['is_error'] is False and not r['binding_diagnostics']
        and same_json(r['args'], s['args']) and len(r['values']) == 1 and same_json(r['values'][0], s['result'])
        for r in responses) for s in accepted)
    initial = [s.get('args', {}).get('units') for s in audit.get('steps', []) if s.get('tool') == 'submit_scene_table']
    checks['initial_model_calls_leave_sources_unbound'] = raw_calls_leave_sources_unbound(initial)
    checks.update(source_unchanged=ops.source(fixture['source_id']) == fixture['source_before'],
        local_timeline_unchanged=ops.read_screenplay_timeline(fixture['screenplay_id']) == fixture['timeline_before'],
        no_content_pending=not ops.list_content_submissions()['submissions'],
        screenplay_no_errors=not any(i.get('severity') == 'error' for i in ops.check_screenplay(fixture['screenplay_id'])['issues']))
    report['usage_breakdown'] = usage_breakdown(metadata)
    report['optional_usage_evidence'] = {'document': screenplay, 'outputs': outputs,
        'elements': compiled['text']['elements'], 'accepted_steps': accepted, 'sdk_responses': responses,
        'initial_scene_table_calls': initial,
        'semantic_review_required': 'Read body: only morning archive event is written. Missing explicit source attribution stays unknown for E1; E2 is unused because no enabled segment covers it. No source binding was inserted by host.'}
    return checks


def main():
    qa.seed, qa.TEXT, qa.TASK = seed, TEXT, TASK
    qa.PROMPT_VERSION = 'optional-screenplay-usage-real/1'
    qa.FIXTURE_ORIGIN = 'New isolated two-event timeline, one-event segment. Model writes a scene and tiny body without source bindings; no host corrections or retries.'
    return qa.main(validate=validate,
        required_tools=['read_scene_table_context', 'submit_scene_table', 'read_script_unit_context', 'submit_script_unit'],
        prompt_factory=lambda f: TASK + '\nscreenplay_id=' + f['screenplay_id'] + '\nsegment_id=' + f['segment_id'],
        on_started=record_implementation)


if __name__ == '__main__':
    raise SystemExit(main())
