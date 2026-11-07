"""Explicit event-owned world locations; no inference or default unknown values."""
from __future__ import annotations

LOCATION_PROPERTIES = frozenset({'地点', '发生地点', 'location_of_event', 'event_location'})
LOCATION_SEMANTICS = frozenset({'location_of_event', 'event_location'})


def _location_value_allowed(value, value_type, nodes, context):
    if value_type in ('string', 'either') and isinstance(value, str) and value in {'无', '未知'}:
        return True
    target = nodes.get(value) if isinstance(value, str) else None
    return (value_type in ('node', 'either') and target is not None
            and target.get('node_type') == 'location'
            and not target.get('retracted') and not target.get('suppressed')
            and target.get('context_id', 'canon') == context)


def event_location_issues(objects, records, *, snapshot=None):
    from .property_core import canonical_definitions, reduce_changes

    nodes = {row['id']: row for row in objects if row.get('kind') == 'node'}
    definitions = canonical_definitions(records)
    location_ids = {key for key, definition in definitions.items()
                    if key in LOCATION_PROPERTIES or definition.get('semantic') in ('location_of_event', 'event_location')}
    issues = []
    for event in nodes.values():
        if event.get('node_type') != 'event' or event.get('retracted') or event.get('suppressed'):
            continue
        identifier = event['id']
        context = event.get('context_id', 'canon')
        rows = [r for r in records if r.get('kind') == 'property_change'
                and r.get('node_id') == identifier and r.get('property_id') in location_ids
                and r.get('scope', 'world') == 'world' and not r.get('holder_id')
                and r.get('context_id', 'canon') == context]
        def report(code, message, related, property_id=None, *, identifier=identifier, event=event):
            issues.append({'severity': 'error', 'code': code, 'object_id': identifier,
                'property_id': property_id or '地点', 'record_ids': [r['id'] for r in related if r.get('id')],
                'message': f"事件{identifier}（{event.get('name', '')}）{message}"})
        anchored = [r for r in rows if r.get('event_id') == identifier]
        wrong = [r for r in rows if r.get('event_id') != identifier]
        if wrong:
            report('EVENT_LOCATION_ANCHOR', '地点状态必须锚定本事件，不能借用其他事件的位置', wrong)
        if not anchored:
            report('EVENT_LOCATION_REQUIRED', '必须记录自己的world地点：有效地点节点、显式无或未知；缺失不等于未知', rows)
            continue
        valid = False
        for property_id in {r['property_id'] for r in anchored}:
            changes = [r for r in anchored if r['property_id'] == property_id]
            definition = definitions[property_id]
            state = reduce_changes(changes, definition, nodes, event_order={identifier: 0})
            kind = state['state']['kind']
            value_type = definition.get('value_type')
            invalid = []
            for row in changes:
                if row.get('id') not in state['active_change_ids'] or row.get('op') not in {'set', 'add', 'remove'}:
                    continue
                value = row.get('value')
                values = value if isinstance(value, list) else [value]
                if not all(_location_value_allowed(v, value_type, nodes, context) for v in values):
                    invalid.append(row)
            if invalid:
                report('EVENT_LOCATION_VALUE', '有效地点变更包含非地点或失效引用，未知状态不能掩盖错误值', invalid, property_id)
                continue
            if kind == 'unknown' and any(
                    row.get('op') == 'unknown' and row.get('id') in state['active_change_ids']
                    for row in changes):
                valid = True
                continue
            if kind in {'absent', 'unrecorded'}:
                report('EVENT_LOCATION_REQUIRED', '地点已清空或移除，须明确记录无或未知', changes, property_id)
                continue
            if kind == 'conflict':
                report('EVENT_LOCATION_CONFLICT', '地点状态冲突，不能按提交顺序猜测', changes, property_id)
                continue
            value = state['state'].get('value')
            values = value if isinstance(value, list) else [value]
            if kind != 'known' or not values or not all(_location_value_allowed(value, value_type, nodes, context) for value in values):
                report('EVENT_LOCATION_VALUE', '地点须引用有效同语境location节点，或以string/either明确记录无/未知', changes, property_id)
            else:
                valid = True
        if not valid and not any(i['object_id'] == identifier for i in issues):
            report('EVENT_LOCATION_REQUIRED', '必须有明确world地点状态', anchored)
    return issues
