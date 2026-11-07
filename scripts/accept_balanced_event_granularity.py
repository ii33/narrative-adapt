"""One isolated balanced-granularity QA; no count policy is added to the product."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import accept_screenplay as qa
from accept_source_tools import public_sdk_results, same_json
from accept_tool_loading import usage_breakdown

UNITS = [
    '社区会议室里，林岚和周宁商量周末开放活动。周宁认为场地尚未获准使用，要求取消安排。林岚取出回函，摊在桌上，指给他看批准日期；周宁读完回函，承认手续齐全，撤回取消要求。',
    '仍在这场会议中，两人接着核对活动经费。周宁提出全部经费用于宣传，林岚说明这样将无法租用必要设备。双方对照报价逐项商议，划去两项广告支出，最终签下设备优先的预算方案。',
    '会议尚未结束，林岚准备按已谈妥的安排发布通知。周宁却承认自己早已私下通知参与者活动取消，并拿出已经发出的群消息。林岚查看时间，要求他当众说明事实；周宁接通群通话，承认此前取消通知未经商议，向参与者道歉并撤回通知。',
]
TEXT = '\n'.join(UNITS)
PIVOTS = ['撤回取消要求', '签下设备优先的预算方案', '向参与者道歉并撤回通知']
TASK = '请读取指定原文，按当前工程精细粒度梳理场景、事件以及必要人物、地点，并通过submit_extraction自动入库。仅处理这份原文，不提取动作节点、物品或主要故事；地点使用event.location。遇到具体内容诊断只局部修补，不重复定位校验或复核已通过数据。'


def seed(ops):
    ops.save_harness_settings({'provider': 'ark', 'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
        'model': 'deepseek-v4.1-flash-economy', 'key_env': 'ARK_API_KEY', 'reasoning_effort': 'low'})
    source = ops.import_source('同一会议中的连续情节单元', TEXT)
    script = ops.create_screenplay('验收空容器', reason='隔离粒度QA空容器')['screenplay']
    return {'source_id': source, 'screenplay_id': script['id'], 'source_before': ops.source(source),
        'screenplay_before': ops.read_screenplay(script['id'])}


def assess_events(graph, source_id):
    nodes = [n for n in graph['nodes'] if not n.get('retracted') and not n.get('suppressed')]
    events = [n for n in nodes if n.get('node_type') == 'event']
    ranges = [(TEXT.index(unit), TEXT.index(unit) + len(unit)) for unit in UNITS]
    bindings, pivot_bindings, invalid = {}, [], []
    for event in events:
        spans = event.get('evidence_spans', [])
        valid = bool(spans) and all(s.get('source_id') == source_id
            and isinstance(s.get('start'), int) and isinstance(s.get('end'), int)
            and 0 <= s['start'] < s['end'] <= len(TEXT)
            and TEXT[s['start']:s['end']] == s.get('quote') for s in spans)
        if not valid:
            invalid.append(event['id'])
            bindings[event['id']] = []
            continue
        bindings[event['id']] = [i for i, (start, end) in enumerate(ranges)
            if any(s['start'] < end and s['end'] > start for s in spans)]
    for quote in PIVOTS:
        start, end = TEXT.index(quote), TEXT.index(quote) + len(quote)
        pivot_bindings.append([n['id'] for n in events if n['id'] not in invalid and any(
            s['start'] <= start and s['end'] >= end for s in n['evidence_spans'])])
    per_unit = [[eid for eid, units in bindings.items() if i in units] for i in range(len(UNITS))]
    return {
        'exact_event_evidence': bool(events) and not invalid,
        'distinct_narrative_units_not_merged': all(len(ids) == 1 for ids in pivot_bindings)
            and len({ids[0] for ids in pivot_bindings if len(ids) == 1}) == len(UNITS)
            and all(len(units) == 1 for units in bindings.values()),
        'process_details_not_fragmented': all(len(ids) == 1 for ids in per_unit),
        'continuous_meeting_not_split_into_scenes': len([n for n in nodes if n.get('node_type') == 'scene']) == 1,
        'no_default_actions': not any(n.get('node_type') == 'action' for n in nodes),
    }, {'events': events, 'unit_event_ids': per_unit, 'pivot_event_ids': pivot_bindings,
        'event_unit_bindings': bindings, 'invalid_evidence_event_ids': invalid,
        'review_required': 'Read names and exact evidence: authorization dispute, budget negotiation, and concealed cancellation disclosure are distinct units inside one continuous meeting. Their gestures and immediate resolutions stay within each unit. These fixture-specific semantic boundaries are not product quotas.'}


def record_implementation(ops, fixture, report, directory, began):
    root = Path(__file__).resolve().parents[1]
    paths = [root / 'src/narrative' / name for name in ('prompt_defaults.py', 'assistant.py', 'extraction_draft.py')]
    paths.append(Path(__file__).resolve())
    report['implementation'] = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    (directory / 'implementation.json').write_text(json.dumps(report['implementation'], indent=2), encoding='utf-8')


def validate(ops, fixture, screenplay, calls, report):
    graph, records = ops.graph_snapshot(), ops.property_records()
    checks, evidence = assess_events(graph, fixture['source_id'])
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
    checks['sdk_and_task_audit_committed_match'] = bool(accepted) and all(any(
        r['tool'] == s['tool'] and r['is_error'] is False and not r['binding_diagnostics']
        and same_json(r['args'], s['args']) and len(r['values']) == 1 and same_json(r['values'][0], s['result'])
        for r in responses) for s in accepted)
    static = ops.check_project()
    checks.update(static_no_errors=static['counts'].get('error', 0) == 0,
        source_unchanged=ops.source(fixture['source_id']) == fixture['source_before'],
        screenplay_unchanged=screenplay == fixture['screenplay_before'])
    report['usage_breakdown'] = usage_breakdown(metadata)
    report['granularity_evidence'] = evidence | {'graph': graph, 'records': records,
        'sdk_responses': responses, 'accepted_steps': accepted, 'static_check': static}
    return checks


def main():
    qa.seed, qa.TEXT, qa.TASK = seed, TEXT, TASK
    qa.PROMPT_VERSION = 'balanced-event-granularity-real/1'
    qa.FIXTURE_ORIGIN = 'New isolated continuous-scene sample. No expected event counts or boundaries in model task; no seeded facts, warmup, host repairs or retries.'
    return qa.main(validate=validate, required_tools=['read_source', 'submit_extraction'],
        prompt_factory=lambda fixture: TASK + '\nsource_id=' + fixture['source_id'],
        use_writing_target=False, allow_extraction_change=True, on_started=record_implementation)


if __name__ == '__main__':
    raise SystemExit(main())
