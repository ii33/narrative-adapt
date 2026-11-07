"""One isolated real check of child-derived container evidence; never retries."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import accept_screenplay as qa
from accept_source_tools import public_sdk_results, same_json
from accept_tool_loading import usage_breakdown

STORY = '林岚在档案室向周宁申请借阅。周宁核验登记后交付资料，林岚签收并道谢，借阅办理至此结束。'
TAIL = '\n附记：本行仅说明排版，不属于办理故事。'
TEXT = STORY + TAIL
CONTAINERS = {'scene', 'big_event', 'storyline'}
CHILD_TYPES = {'scene': 'event', 'big_event': 'scene', 'storyline': 'big_event'}
TASK = '这份原文的办理正文是一篇已经完结的微型故事，请读取并完整提取适用的主要故事、大事件、场景、事件以及必要人物和地点，通过submit_extraction自动入库。只处理办理正文，不把附记纳入故事，不提取物品或动作。事件按现行指导使用代表性引文；事件以上层级只给名称和下级归属，不给quotes或closing_quote，由程序派生证据与位置。主要故事仅因正文确已完结而创建，不因原文范围末尾闭合。遇具体诊断只局部修补，不重发整稿，不重复定位预检。'


def seed(ops):
    ops.save_harness_settings({'provider': 'ark', 'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3',
        'model': 'deepseek-v4.1-flash-economy', 'key_env': 'ARK_API_KEY', 'reasoning_effort': 'low'})
    source = ops.import_source('已完结微型办理故事及无关附记', TEXT)
    script = ops.create_screenplay('验收空容器', reason='容器证据隔离QA空容器')['screenplay']
    return {'source_id': source, 'screenplay_id': script['id'], 'source_before': ops.source(source),
        'screenplay_before': ops.read_screenplay(script['id'])}


def span_key(span):
    return span.get('source_id'), span.get('start'), span.get('end'), span.get('quote')


def assess_container_graph(graph, source_id):
    nodes = {n['id']: n for n in graph['nodes'] if not n.get('retracted') and not n.get('suppressed')}
    containers = [n for n in nodes.values() if n.get('node_type') in CONTAINERS]
    events = [n for n in nodes.values() if n.get('node_type') == 'event']
    relevant = containers + events
    exact = all(n.get('evidence_spans') and all(
        s.get('source_id') == source_id and type(s.get('start')) is int and type(s.get('end')) is int
        and 0 <= s['start'] < s['end'] <= len(STORY)
        and TEXT[s['start']:s['end']] == s.get('quote') for s in n['evidence_spans']) for n in relevant)
    details = []
    for node in containers:
        children = [nodes.get(identifier) for identifier in node.get('events', [])]
        valid_children = bool(children) and all(c and c.get('node_type') == CHILD_TYPES[node['node_type']]
                                              for c in children)
        spans = [s for c in children if c for s in c.get('evidence_spans', [])]
        expected = {span_key(s) for s in spans}
        actual = {span_key(s) for s in node.get('evidence_spans', [])}
        first = min((s['start'] for s in spans), default=None)
        last = max((s['end'] for s in spans), default=None)
        details.append({'id': node['id'], 'name': node['name'], 'type': node['node_type'],
            'valid_children': valid_children, 'evidence_equals_children': bool(expected) and actual == expected,
            'disclosure_from_children': node.get('disclosure') == {'source_id': source_id, 'start': first},
            'completion_from_children': node['node_type'] != 'storyline'
                or node.get('completion') == {'source_id': source_id, 'start': last},
            'completion_not_source_end': node['node_type'] != 'storyline'
                or type(node.get('completion', {}).get('start')) is int
                and node['completion']['start'] < len(TEXT)})
    return {
        'all_container_levels_created': CONTAINERS <= {n.get('node_type') for n in containers},
        'events_have_one_representative_evidence': bool(events) and all(len(n.get('evidence_spans', [])) == 1 for n in events),
        'exact_body_evidence_excludes_tail': bool(relevant) and exact,
        'containers_have_typed_children': bool(details) and all(d['valid_children'] for d in details),
        'container_evidence_is_child_derived': bool(details) and all(d['evidence_equals_children'] for d in details),
        'container_disclosure_is_child_derived': bool(details) and all(d['disclosure_from_children'] for d in details),
        'story_completion_is_child_derived': bool(details) and all(d['completion_from_children'] for d in details),
        'story_completion_not_source_end': bool(details) and all(d['completion_not_source_end'] for d in details),
        'no_default_actions_or_items': not any(n.get('node_type') in {'action', 'item'} for n in nodes.values()),
    }, details


def assess_drafts(drafts):
    nodes = [n for draft in drafts for n in draft.get('nodes', [])]
    upper = [n for n in nodes if n.get('type') in CONTAINERS]
    events = [n for n in nodes if n.get('type') == 'event']
    return {
        'model_did_not_author_container_quotes': bool(upper) and all('quotes' not in n and 'closing_quote' not in n for n in upper),
        'submitted_event_uses_one_quote': bool(events) and all(isinstance(n.get('quotes'), list) and len(n['quotes']) == 1 for n in events),
    }


def record_implementation(ops, fixture, report, directory, began):
    root = Path(__file__).resolve().parents[1]
    names = ['prompt_defaults.py', 'assistant.py', 'extraction.py', 'extraction_refs.py',
             'extraction_draft.py', 'extraction_staging.py', 'extraction_containers.py',
             'container_evidence.py', 'batch_write.py']
    paths = [root / 'src/narrative' / name for name in names] + [Path(__file__).resolve()]
    report['container_implementation'] = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths if p.is_file()}
    (directory / 'container-implementation.json').write_text(
        json.dumps(report['container_implementation'], indent=2), encoding='utf-8')


def validate(ops, fixture, screenplay, calls, report):
    graph = ops.graph_snapshot()
    checks, details = assess_container_graph(graph, fixture['source_id'])
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
    identifiers = list(dict.fromkeys(s['result']['submission_id'] for s in accepted))
    raw_drafts = [{'nodes': [r['item'] for r in ops.read_extraction_submission(identifier, section='nodes')['entries']]}
                  for identifier in identifiers]
    checks.update(assess_drafts(raw_drafts))
    initial_drafts = [s.get('args', {}).get('result', {}) for s in audit.get('steps', []) if s.get('tool') == 'submit_extraction']
    checks['initial_model_calls_did_not_author_container_quotes'] = assess_drafts(initial_drafts)['model_did_not_author_container_quotes']
    checks['sdk_and_task_audit_committed_match'] = bool(accepted) and all(any(
        r['tool'] == s['tool'] and r['is_error'] is False and not r['binding_diagnostics']
        and same_json(r['args'], s['args']) and len(r['values']) == 1 and same_json(r['values'][0], s['result'])
        for r in responses) for s in accepted)
    static = ops.check_project()
    checks.update(static_no_errors=static['counts'].get('error', 0) == 0,
        source_unchanged=ops.source(fixture['source_id']) == fixture['source_before'],
        screenplay_unchanged=screenplay == fixture['screenplay_before'],
        no_pending_extraction_submissions=not ops.list_extraction_submissions()['submissions'])
    report['usage_breakdown'] = usage_breakdown(metadata)
    report['container_evidence'] = {'graph': graph, 'derived_details': details, 'raw_drafts': raw_drafts,
        'initial_model_drafts': initial_drafts, 'sdk_responses': responses, 'accepted_steps': accepted,
        'static_check': static, 'semantic_review_required': 'Review the completed short registration story and model hierarchy; no container quote or completion is authored by the model. Tail text is outside this story.'}
    return checks


def main():
    qa.seed, qa.TEXT, qa.TASK = seed, TEXT, TASK
    qa.PROMPT_VERSION = 'container-evidence-real/1'
    qa.FIXTURE_ORIGIN = 'New isolated short story plus unrelated tail. No seeded formal facts, warmup, host repairs or retries.'
    return qa.main(validate=validate, required_tools=['submit_extraction'],
        prompt_factory=lambda fixture: TASK + '\nsource_id=' + fixture['source_id'],
        use_writing_target=False, allow_extraction_change=True, on_started=record_implementation)


if __name__ == '__main__':
    raise SystemExit(main())
