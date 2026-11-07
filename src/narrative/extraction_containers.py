"""Derive extraction container evidence from validated direct children only."""
from __future__ import annotations

CONTAINERS = frozenset({'scene', 'big_event', 'storyline'})
PARENTS = {'event': 'scene', 'scene': 'big_event', 'big_event': 'storyline'}
CHILD_TYPES = {'scene': {'scene', 'event'}, 'big_event': {'scene'}, 'storyline': {'big_event'}}


def derive(data, snapshot, reader, source_id):
    """Update parsed rows, never the raw draft, using typed bound references."""
    from .container_evidence import ordered_spans, validated_spans
    local = {row['name']: row for row in data['nodes']}
    current = {row['id']: row for row in snapshot['nodes']}
    aliases = {row['existing_id']: row['name'] for row in data['nodes'] if row.get('existing_id')}
    orders = {r['source_id']: r['ordinal'] for r in snapshot.get('source_orders', [])}
    orders.setdefault(source_id, max(orders.values(), default=0) + 1)
    ordering_graph = dict(snapshot)
    ordering_graph['source_orders'] = [*snapshot.get('source_orders', [])]
    if not any(r['source_id'] == source_id for r in ordering_graph['source_orders']):
        ordering_graph['source_orders'].append({'source_id': source_id, 'ordinal': orders[source_id], 'context_id': 'canon'})
    texts = {}
    issues = data.setdefault('rejected', [])

    def canonical(ref):
        return aliases.get(ref, ref)

    def path(row):
        return ['nodes', int(row['reference'][1:]) - 1]

    def issue(row, where, message, **details):
        issues.append({'name': row.get('display_name', row['name']), 'path': where,
            'message': message, 'reason': message, **details})

    def checked_spans(node, row, where):
        targets = [node]
        if node.get('existing_id') in current:
            targets.insert(0, current[node['existing_id']])
        result = []
        for target in targets:
            spans = [{"source_id": source_id, **span} for span in target.get('evidence_spans') or []]
            for span in spans or ([target['evidence']] if target.get('evidence') else []):
                source = span.get('source_id')
                if source not in texts:
                    texts[source] = reader.source(source)['text']
            try:
                result.extend(validated_spans(dict(target) | {'evidence_spans': spans}, texts, strict=True))
            except ValueError as error:
                issue(row, where, '下级节点证据无法派生：' + str(error),
                      related_objects=[target.get('id') or target.get('reference')])
        return result

    visiting, resolved = set(), {}

    def children_for(key, node):
        children = []
        if key in local:
            row = local[key]
            old = current.get(row.get('existing_id'), {})
            children.extend((canonical(ref), path(row) + ['children']) for ref in old.get('events', []))
            children.extend((canonical(ref), path(row) + ['children', i]) for i, ref in enumerate(row.get('children', [])))
            for child in data['nodes']:
                parent_field = PARENTS.get(child['type'])
                if parent_field and canonical(child.get(parent_field)) == key:
                    children.append((canonical(child['name']), path(child) + [parent_field]))
        else:
            children.extend((canonical(ref), []) for ref in node.get('events', []))
        return list(dict.fromkeys((ref, tuple(where)) for ref, where in children if ref))

    def collect(key, owner, where):
        key = canonical(key)
        if key in resolved:
            return resolved[key]
        node = local.get(key) or current.get(key)
        if not node or node.get('retracted') or node.get('suppressed'):
            issue(owner, list(where), '下级节点不存在或未活动，不能派生上层证据', related_objects=[key])
            return []
        kind = node.get('type') if key in local else node.get('node_type')
        if kind not in CONTAINERS:
            return checked_spans(node, owner, list(where))
        if key in visiting:
            issue(owner, list(where), '下级包含关系成环，不能派生上层证据', related_objects=[key])
            return []
        visiting.add(key)
        entries = children_for(key, node)
        spans = []
        for child_key, child_path in entries:
            child = local.get(child_key) or current.get(child_key)
            child_kind = (child.get('type') if child_key in local else child.get('node_type')) if child else None
            if child_kind not in CHILD_TYPES[kind]:
                issue(owner, list(child_path or where), '下级类型不符：场景直属事件或嵌套场景，大事件直属场景，主要故事直属大事件',
                      related_objects=[child_key])
                continue
            if child.get('context_id', 'canon') != node.get('context_id', 'canon'):
                issue(owner, list(child_path or where), '下级节点语境与上层不同，不能派生证据', related_objects=[child_key])
                continue
            spans.extend(collect(child_key, owner, child_path or where))
        visiting.remove(key)
        try:
            result = ordered_spans(spans, ordering_graph, node.get('context_id', 'canon'))
        except ValueError as error:
            issue(owner, list(where), str(error))
            result = []
        resolved[key] = result
        if key in local:
            row = local[key]
            if not result:
                related = [p['path'] for p in issues if p.get('path') and p['path'][:2] != path(row)]
                issue(row, path(row) + ['children'], '上层节点没有可用的直属下级证据；请关联已验证的下级节点',
                      **({'related_paths': related} if related else {}))
            else:
                first = result[0]
                last = max((s for s in result if s['source_id'] == result[-1]['source_id']),
                           key=lambda s: s['end'])
                row['evidence_spans'] = result
                row['quotes'] = [s['quote'] for s in result]
                row['start'] = first['start']
                row['evidence_source_id'] = first['source_id']
                row['derived_container'] = True
                if kind == 'storyline':
                    row['derived_completion'] = {'source_id': last['source_id'], 'start': last['end']}
        return result

    for row in data['nodes']:
        if row['type'] in CONTAINERS:
            collect(row['name'], row, path(row) + ['children'])
    data['nodes'].sort(key=lambda row: (orders.get(row.get('evidence_source_id', source_id), float('inf')),
                                        row['start'], row['name']))
    return data
