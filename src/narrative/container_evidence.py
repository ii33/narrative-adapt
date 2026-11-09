"""Program-owned evidence for narrative containers, from their current children."""
from __future__ import annotations

from copy import deepcopy

CONTAINER_TYPES = {'scene', 'big_event', 'storyline'}
CHILD_TYPES = {'scene': {'scene', 'event'}, 'big_event': {'scene'}, 'storyline': {'big_event'}}


def validated_spans(node, sources, *, strict=False):
    result = []
    spans = list(node.get('evidence_spans') or [])
    if not spans and node.get('evidence'):
        evidence = dict(node['evidence'])
        disclosure = node.get('disclosure') or {}
        if 'start' not in evidence and evidence.get('source_id') == disclosure.get('source_id'):
            evidence['start'] = disclosure.get('start')
        spans.append(evidence)
    for index, span in enumerate(spans):
        if not isinstance(span, dict):
            if strict:
                raise ValueError(f'下级证据[{index}]必须是对象')
            continue
        source = sources.get(span.get('source_id'))
        text = source.get('text') if isinstance(source, dict) else source
        start, quote = span.get('start'), span.get('quote')
        if not isinstance(text, str) or type(start) is not int or not isinstance(quote, str) or not quote:
            if strict:
                raise ValueError(f'下级证据[{index}]缺少有效来源、Unicode起点或非空引文')
            continue
        end = span.get('end', start + len(quote))
        if type(end) is int and 0 <= start < end <= len(text) and text[start:end] == quote:
            result.append({'source_id': span['source_id'], 'start': start, 'end': end, 'quote': quote})
        elif strict:
            raise ValueError(f'下级证据[{index}]范围无效或引文与原文不一致')
    return result


def descendant_events(nodes, container_id, *, context_id=None):
    """Leaf event/action ids under one container, by these same direct-child rules.

    Returns ``None`` when the hierarchy cannot be determined (missing child,
    other context, wrong child type, cycle): a caller who only wants to compare
    levels must then decline rather than guess.
    """
    root = nodes.get(container_id) if isinstance(container_id, str) else None
    if not root or root.get('node_type') not in CONTAINER_TYPES:
        return []
    context = root.get('context_id', 'canon') if context_id is None else context_id
    found, visiting = [], set()

    def walk(identifier):
        node = nodes.get(identifier)
        if not node or node.get('retracted') or node.get('suppressed'):
            raise ValueError('下级节点不存在或未活动')
        kind = node.get('node_type')
        if kind not in CONTAINER_TYPES:
            if kind in {'event', 'action'}:
                found.append(identifier)
            return
        if identifier in visiting:
            raise ValueError('下级包含关系成环')
        visiting.add(identifier)
        for child_id in node.get('events') or []:
            child = nodes.get(child_id)
            if child is None:
                raise ValueError('下级节点未入库')
            if child.get('context_id', 'canon') != context or child.get('node_type') not in CHILD_TYPES[kind]:
                raise ValueError('下级节点语境或类型与上层不符')
            walk(child_id)
        visiting.remove(identifier)

    try:
        walk(container_id)
    except ValueError:
        return None
    return found


def ordered_spans(spans, graph, context_id='canon'):
    from .temporal_state import source_order
    order = source_order(graph)
    order.update({r['source_id']: r['ordinal'] for r in graph.get('source_orders', [])
                  if r.get('context_id', 'canon') == context_id})
    unique = {(s['source_id'], s['start'], s['end'], s['quote']): dict(s) for s in spans}
    if any(s['source_id'] not in order for s in spans):
        raise ValueError('下级证据来源未进入正式原文阅读顺序')
    return sorted(unique.values(), key=lambda s: (order[s['source_id']],
                                                s['start'], s['end'], s['source_id']))


def refresh_container_evidence(graph, sources, previous_graph=None):
    """Refresh changed containers and ancestors; leave unrelated objects untouched.

    The returned graph is a private copy. This runs inside Operations preparation,
    before static validation and the atomic commit, never on project opening.
    """
    result = deepcopy(graph)
    nodes = {n['id']: n for n in result.get('nodes', [])}
    before = {n['id']: n for n in (previous_graph or {}).get('nodes', [])}
    changed = {key for key in nodes.keys() | before.keys() if nodes.get(key) != before.get(key)}
    if previous_graph is None or graph.get('source_orders') != previous_graph.get('source_orders'):
        changed.update(nodes)
    parents = {}
    for node in [*before.values(), *nodes.values()]:
        if node.get('node_type') in CONTAINER_TYPES:
            for child in node.get('events', []):
                parents.setdefault(child, set()).add(node['id'])
    pending = list(changed)
    while pending:
        for parent in parents.get(pending.pop(), ()):
            if parent not in changed:
                changed.add(parent)
                pending.append(parent)
    active, done = set(), {}

    def evidence(identifier):
        if identifier in done:
            return done[identifier]
        node = nodes.get(identifier)
        if not node or node.get('retracted') or node.get('suppressed') or identifier in active:
            return []  # Existing structural checks report invalid references/cycles.
        if node.get('node_type') not in CONTAINER_TYPES:
            return validated_spans(node, sources)
        active.add(identifier)
        spans = []
        for child_id in node.get('events', []):
            child = nodes.get(child_id)
            if (child and child.get('context_id', 'canon') == node.get('context_id', 'canon')
                    and child.get('node_type') in CHILD_TYPES[node['node_type']]):
                spans.extend(evidence(child_id))
        spans = ordered_spans(spans, result, node.get('context_id', 'canon'))
        active.remove(identifier)
        done[identifier] = spans
        if identifier in changed:
            node['evidence_spans'] = spans
            if spans:
                first = spans[0]
                node['evidence'] = {key: first[key] for key in ('source_id', 'quote')}
                node['disclosure'] = {key: first[key] for key in ('source_id', 'start')}
                if node['node_type'] == 'storyline':
                    last = max((s for s in spans if s['source_id'] == spans[-1]['source_id']),
                               key=lambda s: s['end'])
                    node['completion'] = {'source_id': last['source_id'], 'start': last['end']}
            else:
                node.pop('evidence', None)
                node.pop('disclosure', None)
                if node['node_type'] == 'storyline':
                    node.pop('completion', None)
        return spans

    for identifier in changed:
        if identifier in nodes and nodes[identifier].get('node_type') in CONTAINER_TYPES:
            evidence(identifier)
    result['objects'] = [nodes.get(o['id'], o) if o.get('kind') == 'node' else o
                         for o in result.get('objects', [])]
    return result
