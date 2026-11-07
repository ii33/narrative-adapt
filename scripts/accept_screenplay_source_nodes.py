"""One isolated real screenplay provenance task; no intervention or retry."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import accept_screenplay as qa
from accept_source_tools import public_sdk_results, same_json
from accept_tool_loading import usage_breakdown

FIRST = '清晨，林岚在档案室核对登记，确认自己的借阅申请已经获准。'
SECOND = '当天夜里，林岚在车站遇见周宁，向他报平安，两人约定次日会合。'
TEXT = FIRST + '\n' + SECOND
TASK = '''请在指定剧本段落中实际生成两场场表，按原文先后排列，每场plan.source_event_ids只填写该场实际依据的本剧本独立改编时间线事件编号，不重复拼写节点名称或来源修订；名称和追溯说明由程序生成。
先读取read_scene_table_context，用真实修订和指纹提交场表，并记录采用标记。然后读取首场read_script_unit_context，仅为首场提交一小段正文（一个简短动作块即可，其他场不必写正文），使用真实上下文和正文修订。不要为了展示来源而在动作或对白中手写来源注记。不要修改原文、提取、改编或剧本时间线，不添加范围外情节；遇具体内容诊断只局部修补，不重发整稿。完成后自然结束。'''


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
    screenplay = ops.create_screenplay('来源标注验收', reason='隔离来源标注剧本')['screenplay']
    timeline = ops.import_screenplay_timeline(screenplay['id'], event_ids, 0,
        imported['revision'], '仅导入两个已选事件')
    span = ops.create_adaptation_span(screenplay['id'], '两个独立时空事件', selection_ids=event_ids,
        expected_timeline_revision=timeline['revision'], reason='隔离来源标注范围')['span']
    segment = ops.create_script_segment(screenplay['id'], span['id'], '档案室与车站',
        screenplay['structure_revision'], reason='隔离来源标注段落')
    return {'source_id': source, 'screenplay_id': screenplay['id'], 'segment_id': segment['id'],
        'span_id': span['id'], 'timeline_id': timeline['timeline_id'], 'timeline_revision': timeline['revision'],
        'event_ids': event_ids, 'event_names': {n['id']: n['name'] for n in events},
        'source_before': ops.source(source), 'timeline_before': ops.read_screenplay_timeline(screenplay['id'])}


def assess_sources(document, fixture, elements, outputs):
    units = [u for s in document['segments'] for u in s['units'] if u.get('enabled', True) and u.get('kind') == 'scene']
    expected = fixture['event_ids']
    details = []
    for index, unit in enumerate(units):
        ids = unit.get('plan', {}).get('source_event_ids', [])
        references = unit.get('source_references', {})
        rows = references.get('nodes', [])
        text = unit.get('source_reference_text', '')
        details.append({'unit_id': unit['id'], 'ids': ids, 'text': text,
            'correct_event_for_scene': index < len(expected) and ids == [expected[index]],
            'current_local_timeline': references.get('timeline_id') == fixture['timeline_id']
                and references.get('timeline_revision') == fixture['timeline_revision'],
            'program_resolved_nodes': len(rows) == len(ids) == 1 and all(
                row.get('id') == identifier and row.get('name') == fixture['event_names'].get(identifier)
                and row.get('status') == 'available' and row.get('source_node_id') == identifier
                for row, identifier in zip(rows, ids, strict=True)),
            'id_and_name_displayed': bool(ids) and all(identifier in text
                and fixture['event_names'].get(identifier, '\0') in text for identifier in ids),
            'no_annotation_body_blocks': all(b.get('type') != 'source_reference'
                and '依据（剧本改编时间线）' not in str(b.get('payload', {}).get('text', ''))
                for b in unit.get('blocks', [])),
        })
    notes = [e for e in elements if e.get('kind') == 'source_reference']
    after_headings = [elements[index + 1] for index, element in enumerate(elements[:-1])
                      if element.get('kind') == 'scene']
    expected_notes = {d['unit_id']: d['text'] for d in details}
    fountain_notes = {text.strip() for text in re.findall(r'\[\[(.*?)\]\]', outputs.get('fountain', ''), re.S)}
    return {
        'two_distinct_scenes_mapped_in_reading_order': len(units) == len(expected) == 2
            and all(d['correct_event_for_scene'] for d in details),
        'references_resolve_current_script_timeline': bool(details) and all(d['current_local_timeline'] for d in details),
        'references_are_program_resolved_id_name_and_upstream': bool(details) and all(d['program_resolved_nodes'] for d in details),
        'view_shows_id_and_node_name': bool(details) and all(d['id_and_name_displayed'] for d in details),
        'first_scene_has_real_body': bool(units) and bool(units[0].get('blocks')),
        'source_notes_are_not_body_blocks': bool(details) and all(d['no_annotation_body_blocks'] for d in details),
        'one_compiled_note_after_each_heading': len(notes) == len(after_headings) == len(units) == 2
            and all(n.get('kind') == 'source_reference' and n.get('text') == expected_notes.get(n.get('unit_id'))
                    for n in after_headings),
        'export_and_compile_show_each_source_note': bool(details) and all(d['text'] and all(
            d['text'] in output for output in outputs.values()) for d in details),
        'fountain_notes_use_note_syntax': bool(details) and all(d['text'] in fountain_notes for d in details),
    }, details


def record_implementation(ops, fixture, report, directory, began):
    root = Path(__file__).resolve().parents[1]
    names = ['screenplay_writing.py', 'screenplay_scene_policy.py', 'screenplay_tools.py',
             'screenplay_compile.py', 'screenplay_operations.py', 'screenplay_document_output.py']
    paths = [root / 'src/narrative' / name for name in names] + [Path(__file__).resolve()]
    report['source_nodes_implementation'] = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths if p.is_file()}
    (directory / 'source-nodes-implementation.json').write_text(
        json.dumps(report['source_nodes_implementation'], indent=2), encoding='utf-8')


def validate(ops, fixture, screenplay, calls, report):
    compiled = {format: ops.compile_screenplay(fixture['screenplay_id'], format) for format in ('text', 'html', 'fountain')}
    outputs = {format: result['text'] for format, result in compiled.items()}
    outputs['legacy_text'] = ops.export_screenplay(fixture['screenplay_id'], 'text')['text']
    outputs['qt_preview'] = compiled['html']['qt_preview_html']
    checks, details = assess_sources(screenplay, fixture, compiled['text']['elements'], outputs)
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
    checks.update(source_unchanged=ops.source(fixture['source_id']) == fixture['source_before'],
        local_timeline_unchanged=ops.read_screenplay_timeline(fixture['screenplay_id']) == fixture['timeline_before'],
        no_content_pending=not ops.list_content_submissions()['submissions'],
        screenplay_no_errors=not any(i.get('severity') == 'error' for i in ops.check_screenplay(fixture['screenplay_id'])['issues']))
    report['usage_breakdown'] = usage_breakdown(metadata)
    report['source_nodes_evidence'] = {'document': screenplay, 'details': details, 'outputs': outputs,
        'elements': compiled['text']['elements'], 'accepted_steps': accepted, 'sdk_responses': responses,
        'semantic_review_required': 'Review that morning archive approval maps to the first event, nighttime station meeting maps to the second, and source notes are program output rather than performed screenplay text.'}
    return checks


def main():
    qa.seed, qa.TEXT, qa.TASK = seed, TEXT, TASK
    qa.PROMPT_VERSION = 'screenplay-source-nodes-real/1'
    qa.FIXTURE_ORIGIN = 'New isolated two-event fixture. Real model authors scene mappings and a minimal first-scene body; no host corrections, warmup or retries.'
    return qa.main(validate=validate,
        required_tools=['read_scene_table_context', 'submit_scene_table', 'read_script_unit_context', 'submit_script_unit'],
        prompt_factory=lambda f: TASK + '\nscreenplay_id=' + f['screenplay_id'] + '\nsegment_id=' + f['segment_id'])


if __name__ == '__main__':
    raise SystemExit(main())
