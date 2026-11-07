"""One isolated cross-chapter, single-event-quote QA; no product count policy."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import accept_screenplay as qa
from accept_source_tools import public_sdk_results, same_json
from accept_tool_loading import usage_breakdown

PARTS = [
    '第一章\n林岚在档案室向周宁办理借阅登记。周宁核对申请上的姓名与借阅期限，翻到签名栏，将登记表递到林岚面前。',
    '第二章\n林岚接过表，在签名栏签字后交还。周宁仍坐在原处，接着核对签名，录入系统，确认本次借阅登记完成，林岚道谢。',
]
TEXT = '\n'.join(PARTS)
ANCHORS = ['周宁核对申请上的姓名与借阅期限', '确认本次借阅登记完成']
TASK = '请读取指定原文的全部章节，按当前工程精细粒度和现行提取指导提取场景、事件及必要人物、地点，以准确原文引文支持事实，并通过submit_extraction自动入库。章节切片已建立，仅处理本原文，不提取动作节点、物品或主要故事；地点使用event.location。遇具体诊断只局部修补，不重发整稿，不重复定位校验或复核已通过数据。'


def seed(ops):
    ops.save_harness_settings({'provider': 'ark', 'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
        'model': 'deepseek-v4.1-flash-economy', 'key_env': 'ARK_API_KEY', 'reasoning_effort': 'low'})
    source = ops.import_source('连续办理过程的章节切分', TEXT)
    boundary = len(PARTS[0]) + 1
    slices = [{'title': '第一章', 'start': 0, 'end': boundary},
              {'title': '第二章', 'start': boundary, 'end': len(TEXT)}]
    created = ops.create_source_slices(source, slices)
    if not isinstance(created, dict) or created.get('status') != 'accepted':
        raise RuntimeError('Isolated chapter slice setup did not commit')
    screenplay = ops.create_screenplay('验收空容器', reason='跨章节边界隔离QA空容器')['screenplay']
    return {'source_id': source, 'screenplay_id': screenplay['id'], 'source_before': ops.source(source),
        'slices_before': ops.source_slices(source), 'screenplay_before': ops.read_screenplay(screenplay['id'])}


def assess_boundaries(graph, source_id):
    nodes = [n for n in graph['nodes'] if not n.get('retracted') and not n.get('suppressed')]
    scenes = [n for n in nodes if n.get('node_type') == 'scene']
    events = [n for n in nodes if n.get('node_type') == 'event']
    relevant = scenes + events
    exact = {}
    coverage = {}
    chapter_coverage = {}
    body_ranges = []
    offset = 0
    for part in PARTS:
        body_ranges.append((offset + part.index('\n') + 1, offset + len(part)))
        offset += len(part) + 1
    for node in relevant:
        spans = node.get('evidence_spans', [])
        exact[node['id']] = bool(spans) and all(s.get('source_id') == source_id
            and type(s.get('start')) is int and type(s.get('end')) is int
            and 0 <= s['start'] < s['end'] <= len(TEXT)
            and TEXT[s['start']:s['end']] == s.get('quote') for s in spans)
        coverage[node['id']] = [exact[node['id']] and any(
            s['start'] <= TEXT.index(anchor) and s['end'] >= TEXT.index(anchor) + len(anchor)
            for s in spans) for anchor in ANCHORS]
        # Coverage is an audit aid, not a requirement to quote both chapters.
        # A representative body quote may support a cross-chapter narrative unit.
        chapter_coverage[node['id']] = [exact[node['id']] and any(
            s['start'] < end and s['end'] > start
            and TEXT[max(start, s['start']):min(end, s['end'])].strip()
            for s in spans) for start, end in body_ranges]
    return {
        'one_continuous_scene': len(scenes) == 1,
        'one_complete_registration_event': len(events) == 1,
        'exact_scene_and_event_evidence': bool(relevant) and all(exact.values()),
        'scene_has_exact_body_evidence': len(scenes) == 1 and any(chapter_coverage[scenes[0]['id']]),
        'event_has_exact_body_evidence': len(events) == 1 and any(chapter_coverage[events[0]['id']]),
        'event_uses_one_representative_quote': len(events) == 1
            and len(events[0].get('evidence_spans', [])) == 1,
        'event_belongs_to_cross_chapter_scene': len(scenes) == len(events) == 1
            and events[0]['id'] in scenes[0].get('events', []),
        'no_default_actions': not any(n.get('node_type') == 'action' for n in nodes),
    }, {'scenes': scenes, 'events': events, 'exact_evidence': exact, 'anchor_coverage': coverage,
        'chapter_body_coverage': chapter_coverage,
        'semantic_review_required': 'One uninterrupted registration process continues in the same archive room, with the same people and no new goal across the chapter heading. Review the model event name and its single representative body quote against all source context. Neither scene nor event must quote both chapters or particular anchor sentences. Counts are fixture-specific and never product quotas.'}


def record_implementation(ops, fixture, report, directory, began):
    root = Path(__file__).resolve().parents[1]
    paths = [root / 'src/narrative' / name for name in ('prompt_defaults.py', 'assistant.py',
        'extraction.py', 'extraction_refs.py', 'extraction_draft.py', 'extraction_staging.py')]
    paths.append(Path(__file__).resolve())
    report['cross_chapter_implementation'] = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths}
    (directory / 'cross-chapter-implementation.json').write_text(
        json.dumps(report['cross_chapter_implementation'], indent=2), encoding='utf-8')


def validate(ops, fixture, screenplay, calls, report):
    graph = ops.graph_snapshot()
    checks, evidence = assess_boundaries(graph, fixture['source_id'])
    directory = Path(report['audit_directory'])
    audit = json.loads((directory / 'task.json').read_text(encoding='utf-8'))
    metadata = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(directory.glob('metadata-*.json'))]
    sessions = {m['session_id'] for m in metadata if m.get('session_id')}
    paths = [p for p in (directory.parents[1] / 'home' / 'sessions').rglob('session.v4.jsonl') if p.parent.name in sessions]
    rows = [json.loads(line) for p in paths for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
    responses = public_sdk_results(rows)
    accepted = [s for s in audit.get('steps', []) if s.get('tool') in {'submit_extraction', 'patch_extraction_submission'}
        and s.get('ok') is True and s.get('result', {}).get('status') == 'accepted'
        and s['result'].get('submission_status') == 'committed' and s['result'].get('validation_status') == 'valid']
    submitted_events = [row['item'] for step in accepted
        for row in ops.read_extraction_submission(step['result']['submission_id'], section='nodes')['entries']
        if row['item'].get('type') == 'event']
    checks['submitted_event_uses_one_quote'] = bool(submitted_events) and all(
        isinstance(row.get('quotes'), list) and len(row['quotes']) == 1 for row in submitted_events)
    checks['sdk_and_task_audit_committed_match'] = bool(accepted) and all(any(
        r['tool'] == s['tool'] and r['is_error'] is False and not r['binding_diagnostics']
        and same_json(r['args'], s['args']) and len(r['values']) == 1 and same_json(r['values'][0], s['result'])
        for r in responses) for s in accepted)
    static = ops.check_project()
    checks.update(static_no_errors=static['counts'].get('error', 0) == 0,
        source_unchanged=ops.source(fixture['source_id']) == fixture['source_before'],
        chapter_slices_unchanged=ops.source_slices(fixture['source_id']) == fixture['slices_before'],
        screenplay_unchanged=screenplay == fixture['screenplay_before'])
    report['usage_breakdown'] = usage_breakdown(metadata)
    report['cross_chapter_evidence'] = evidence | {'graph': graph, 'sdk_responses': responses,
        'accepted_steps': accepted, 'submitted_events': submitted_events, 'static_check': static}
    return checks


def main():
    qa.seed, qa.TEXT, qa.TASK = seed, TEXT, TASK
    qa.PROMPT_VERSION = 'cross-chapter-boundaries-real/2'
    qa.FIXTURE_ORIGIN = 'New isolated source and chapter index slices only. No seeded formal facts, expected counts in task, warmup, host repairs or retries.'
    return qa.main(validate=validate, required_tools=['submit_extraction'],
        prompt_factory=lambda fixture: TASK + '\nsource_id=' + fixture['source_id'],
        use_writing_target=False, allow_extraction_change=True, on_started=record_implementation)


if __name__ == '__main__':
    raise SystemExit(main())
