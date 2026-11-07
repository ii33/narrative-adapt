"""One isolated event-location extraction; coordinator runs 600s/100k once."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import accept_screenplay as qa
from accept_source_tools import public_sdk_results, same_json
from accept_tool_loading import usage_breakdown

TEXT = '林岚在档案室宣布暂停发布名单，要求同事核查授权，众人开始联系相关人员。\n次日，周宁收到匿名消息，得知自己的名字被冒用，决定另行调查。叙述没有交代周宁此时身在何处。'
KNOWN_QUOTE = '林岚在档案室宣布暂停发布名单'
UNKNOWN_QUOTE = '周宁收到匿名消息'
TASK = '请读取指定原文，提取有独立叙事意义的场景、事件以及所需人物和地点；每个事件记录world地点状态，明确地点用location节点引用，未交代地点明确未知，不臆造位置。仅梳理这份原文，不提取物品、动作节点或主要故事。通过submit_extraction暂存、校验并自动入库；若有具体诊断，只局部修补问题项。'


def record_implementation(ops, fixture, report, directory, began):
    root = Path(__file__).resolve().parents[1]
    names = ['event_location.py', 'graph_service.py', 'project_changes.py', 'property_audit.py',
        'adaptation_operations.py', 'screenplay_timeline.py', 'extraction_draft.py', 'prompt_defaults.py', 'assistant.py']
    paths = [root / 'src/narrative' / name for name in names] + [Path(__file__).resolve()]
    report['event_location_implementation'] = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths}
    (directory / 'event-location-implementation.json').write_text(
        json.dumps(report['event_location_implementation'], indent=2), encoding='utf-8')


def seed(ops):
    ops.save_harness_settings({'provider': 'ark', 'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
        'model': 'deepseek-v4.1-flash-economy', 'key_env': 'ARK_API_KEY', 'reasoning_effort': 'low'})
    source = ops.import_source('事件地点明确与未知', TEXT)
    script = ops.create_screenplay('验收空容器', reason='事件地点独立QA种子')['screenplay']
    return {'source_id': source, 'screenplay_id': script['id'], 'source_before': ops.source(source),
        'screenplay_before': ops.read_screenplay(script['id'])}


def assess_location_rows(graph, records, derived):
    from narrative.event_location import LOCATION_PROPERTIES, LOCATION_SEMANTICS
    nodes = {n['id']: n for n in graph['nodes']}
    definitions = {r['id']: r for r in records if r.get('kind') == 'property_definition'}
    location_ids = {key for key, d in definitions.items() if d.get('semantic') in LOCATION_SEMANTICS or key in LOCATION_PROPERTIES}
    raw = {r['id']: r for r in records if r.get('kind') == 'property_change'}
    states = {}
    for event in nodes.values():
        if event.get('node_type') != 'event' or event.get('retracted') or event.get('suppressed'):
            continue
        rows = [r for r in derived if r.get('node_id') == event['id'] and r.get('property_id') in location_ids
            and r.get('scope', 'world') == 'world' and not r.get('holder_id')
            and r.get('context_id', 'canon') == event.get('context_id', 'canon')]
        state = 'missing'
        if len(rows) == 1:
            row = rows[0]
            contributions = [raw.get(identifier) for identifier in row.get('active_change_ids', [])]
            anchored = bool(contributions) and all(r and r.get('event_id') == event['id'] for r in contributions)
            value = row.get('value')
            if anchored and row.get('state_kind') == 'unknown':
                state = 'unknown'
            elif anchored and row.get('state_kind') == 'known':
                value_type = definitions[row['property_id']].get('value_type')
                if value_type in ('string', 'either') and value in ('无', '未知'):
                    state = 'none' if value == '无' else 'unknown'
                elif value_type in ('node', 'either'):
                    values = value if isinstance(value, list) else [value]
                    if values and all(isinstance(v, str) and v in nodes and nodes[v].get('node_type') == 'location'
                            and not nodes[v].get('retracted') and not nodes[v].get('suppressed')
                            and nodes[v].get('context_id', 'canon') == event.get('context_id', 'canon') for v in values):
                        state = 'location'
        states[event['id']] = state
    return {'all_active_events_have_own_world_location': bool(states) and all(s != 'missing' for s in states.values())}, states


def validate(ops, fixture, screenplay, calls, report):
    from narrative.event_location import event_location_issues
    from narrative.temporal_state import projected_changes
    graph, records = ops.graph_snapshot(), ops.property_records()
    derived = projected_changes(graph, records)
    checks, states = assess_location_rows(graph, records, derived)
    location_issues = event_location_issues(graph['nodes'], records, snapshot=graph)
    checks['core_event_location_no_errors'] = not location_issues
    events = [n for n in graph['nodes'] if n.get('node_type') == 'event' and not n.get('retracted')]

    def covering(quote):
        start = TEXT.index(quote)
        return [n['id'] for n in events if any(s.get('source_id') == fixture['source_id'] and
            s['start'] <= start and s['end'] >= start + len(quote) and TEXT[s['start']:s['end']] == s['quote']
            for s in n.get('evidence_spans', []))]

    known, unknown = covering(KNOWN_QUOTE), covering(UNKNOWN_QUOTE)
    checks['independent_known_and_unknown_events'] = any(a != b and states.get(a) == 'location' and states.get(b) == 'unknown'
        for a in known for b in unknown)
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
        screenplay_unchanged=screenplay == fixture['screenplay_before'],
        no_default_actions=not any(n.get('node_type') == 'action' for n in graph['nodes']))
    report['usage_breakdown'] = usage_breakdown(metadata)
    report['event_location_evidence'] = {'graph': graph, 'records': records, 'derived': derived, 'states': states,
        'sdk_responses': responses, 'accepted_steps': accepted, 'static_check': static, 'location_issues': location_issues,
        'review': 'Review unmodified raw draft and exact evidence for the located announcement and unlocated message; no model output is repaired by the host.'}
    return checks


def main():
    qa.seed, qa.TEXT, qa.TASK = seed, TEXT, TASK
    qa.PROMPT_VERSION = 'event-locations-real/1'
    qa.FIXTURE_ORIGIN = 'New isolated short source. Model authors all nodes and own-event location changes; no seeded facts, warmup or retries.'
    return qa.main(validate=validate, required_tools=['read_source', 'submit_extraction'],
        prompt_factory=lambda fixture: TASK + '\nsource_id=' + fixture['source_id'],
        use_writing_target=False, allow_extraction_change=True, on_started=record_implementation)


if __name__ == '__main__':
    raise SystemExit(main())
