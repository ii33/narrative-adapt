"""Editorial scene standards, with only deterministic source-reference checks."""
from __future__ import annotations

import copy

from .content_errors import ContentValidationError


def scene_table_policy(resolved, prompt_config=None):
    from .prompt_defaults import DEFAULT_PROMPTS
    text = prompt_config["texts"]["scene_policy"] if prompt_config is not None else DEFAULT_PROMPTS["scene_policy"]["text"]
    source = resolved.get("input") or {}
    return {"version": prompt_config["versions"]["scene_policy"] if prompt_config is not None else DEFAULT_PROMPTS["scene_policy"]["version"], "decision_owner": "model",
            "rules": [line.strip() for line in text.splitlines() if line.strip()],
            "source_event_ids": list(source.get("source_event_ids", []))}


def prepare_generated_scene_table(drafts, resolved, *, paths=None, active_scene_count=None):
    """The model decides scene count/boundaries; the program checks references."""
    rows = copy.deepcopy(drafts)
    events = (resolved.get("input") or {}).get("source_event_ids", [])
    problems = []
    for index, row in enumerate(rows):
        if not row["enabled"]:
            continue
        plan = row["plan"]
        path = paths[index] if paths is not None else ['units', index, 'plan']
        row['_source_reference_origin'] = 'explicit'
        if not plan["summary"].strip():
            problems.append({'code': 'SCENE_SUMMARY_EMPTY', 'path': [*path, 'summary'],
                             'message': '拆场提交每个启用场至少需要非空概要'})
        seen = set()
        for ref_index, identifier in enumerate(plan['source_event_ids']):
            message = ('场表不能引用屏蔽、删除或范围外事件' if identifier not in events else
                       '同一场的来源事件编号不能重复' if identifier in seen else None)
            if message:
                problems.append({'code': 'SCENE_SOURCE_UNAVAILABLE', 'path': [*path, 'source_event_ids', ref_index],
                                 'message': message})
            seen.add(identifier)
    if problems:
        raise ContentValidationError('；'.join(p['message'] for p in problems), problems=problems)
    return rows


def scene_source_references(unit, timeline, resolved):
    """Resolve stored local event IDs for every reader; never persist display names."""
    allowed = set((resolved.get('input') or {}).get('source_event_ids', []))
    nodes = {node['id']: node for node in timeline['nodes']}
    references = []
    labels = {'missing': '已删除或不存在', 'suppressed': '已屏蔽',
              'out_of_scope': '范围外', 'invalid_type': '非事件节点'}
    for identifier in unit['plan'].get('source_event_ids', []):
        node = nodes.get(identifier)
        status = ('missing' if not node or node.get('retracted') else
                  'suppressed' if node.get('suppressed') else
                  'invalid_type' if node.get('node_type') != 'event' else
                  'out_of_scope' if identifier not in allowed else 'available')
        references.append({'id': identifier, 'name': node.get('name', '') if node else '',
                           'source_node_id': node.get('source_node_id') if node else None, 'status': status})
    text = ('依据（剧本改编时间线）：' + '；'.join(
        ref['id'] + (' · ' + ref['name'] if ref['name'] else '')
        + ('（' + labels[ref['status']] + '）' if ref['status'] != 'available' else '')
        for ref in references)) if references else ''
    return {'source_references': {'timeline_id': timeline.get('timeline_id') or timeline.get('id'),
                                 'timeline_revision': timeline['revision'],
                                 'upstream_timeline_id': timeline.get('source_timeline_id'),
                                 'upstream_revision': timeline.get('source_revision'),
                                 'derived': unit.get('source_reference_origin') == 'single_scene', 'nodes': references},
            'source_reference_text': text}


def scene_source_diagnostics(units, resolved):
    allowed = set((resolved.get("input") or {}).get("source_event_ids", []))
    issues = []
    for unit in units:
        if not unit["enabled"]:
            continue
        invalid = [identifier for identifier in unit["plan"].get("source_event_ids", []) if identifier not in allowed]
        if invalid:
            issues.append({"severity": "error", "code": "SCENE_SOURCE_UNAVAILABLE", "unit_id": unit["id"],
                "source_event_ids": invalid, "message": "场表来源包含屏蔽、删除或范围外事件，须重新核对概要；旧正文保留"})
    return issues


def scene_event_usage(units, resolved):
    """Observed provenance only: missing attribution is unknown, not unused."""
    events = list((resolved.get('input') or {}).get('source_event_ids', []))
    allowed = set(events)
    used, unknown, unbound = set(), set(), []
    active = [u for u in units if u.get('enabled', True) and u.get('kind') == 'scene']
    for unit in active:
        recorded = (unit.get('plan', {}).get('source_event_ids', [])
                    if unit.get('source_reference_origin') != 'single_scene' else [])
        refs = set(recorded) & allowed
        body = bool(unit.get('blocks')) or unit.get('body_presence') == 'present'
        if body:
            used.update(refs)
        if not recorded or any(identifier not in allowed for identifier in recorded):
            unbound.append(unit['id'])
            unknown.update(allowed)
        elif not body:
            unknown.update(refs)
    if not active:
        unknown.update(allowed)
    unknown.difference_update(used)
    known = resolved.get('status') == 'valid' and bool(active) and not unknown and not unbound
    return {'timeline_id': (resolved.get('span') or {}).get('timeline_id'), 'event_ids': events,
            'used_event_ids': [identifier for identifier in events if identifier in used],
            'unused_event_ids': [identifier for identifier in events if identifier not in used | unknown],
            'unknown_event_ids': [identifier for identifier in events if identifier in unknown],
            'unbound_unit_ids': unbound, 'scope_status': resolved.get('status'),
            'known': known, 'complete': known}


def screenplay_event_usage(timeline, segments):
    """Aggregate independently scoped segment coverage without semantic guessing."""
    events = [n['id'] for n in timeline['nodes'] if n.get('node_type') == 'event'
              and not n.get('suppressed') and not n.get('retracted')]
    allowed = set(events)
    active = [s for s in segments if s.get('enabled', True)]
    used, unknown, unbound = set(), set(), []
    invalid_scope = False
    for segment in active:
        coverage = segment.get('event_usage', {})
        used.update(set(coverage.get('used_event_ids', [])) & allowed)
        unknown.update(set(coverage.get('unknown_event_ids', [])) & allowed)
        unbound.extend(coverage.get('unbound_unit_ids', []))
        invalid_scope |= coverage.get('scope_status') != 'valid'
    if invalid_scope or not active:
        unknown.update(allowed)
    unknown.difference_update(used)
    known = bool(active) and not invalid_scope and not unknown and not unbound
    return {'timeline_id': timeline.get('timeline_id') or timeline.get('id'), 'event_ids': events,
            'used_event_ids': [identifier for identifier in events if identifier in used],
            'unused_event_ids': [identifier for identifier in events if identifier not in used | unknown],
            'unknown_event_ids': [identifier for identifier in events if identifier in unknown],
            'unbound_unit_ids': list(dict.fromkeys(unbound)), 'known': known, 'complete': known}
