"""Explicit event-local property editing via the shared atomic Operations boundary."""
from __future__ import annotations

from .content_errors import ContentConflictError, ContentGuardError, ContentValidationError

MISSING = object()
OPS = {'set', 'add', 'remove', 'clear', 'unknown'}


def _invalid(message, field, *, related_fields=()):
    return ContentValidationError(message, problems=[{
        'code': 'CONTENT_VALIDATION', 'message': message, 'path': [field],
        **({'related_paths': [[name] for name in related_fields]} if related_fields else {})}])


def record_change(ops, node_id, property_id, event_id, op, *, expected_revision, reason, value=MISSING,
                  value_type=None, cardinality=None, symmetric=None, scope='world', holder_id=None,
                  context_id='canon', quote=None, source_id=None, category=None, source=None, related_nodes=None):
    snapshot = ops.graph_snapshot()
    if type(expected_revision) is not int or expected_revision != snapshot['revision']:
        return {'status': 'rejected', 'valid': False, 'revision': snapshot['revision'],
                'diagnostics': [{'severity': 'error', 'code': 'REVISION', 'message': '正式资料版本已变化，请刷新'}]}
    if not isinstance(op, str) or op not in OPS:
        raise _invalid('属性变更op须为set/add/remove/clear/unknown', 'op')
    if (value is not MISSING) != (op in {'set', 'add', 'remove'}):
        raise _invalid('set/add/remove必须带value，clear/unknown不能带value', 'value', related_fields=('op',))
    if type(expected_revision) is not int or not isinstance(reason, str) or not reason.strip():
        raise ContentConflictError('请提供当前revision及变更依据')
    if value_type is not None and (not isinstance(value_type, str) or value_type not in {'string', 'number', 'boolean', 'node', 'either', 'any'}):
        raise _invalid('value_type必须是合法类型', 'value_type')
    if cardinality is not None and (not isinstance(cardinality, str) or cardinality not in {'one', 'many'}):
        raise _invalid('cardinality必须为one/many', 'cardinality')
    if symmetric is not None and type(symmetric) is not bool:
        raise _invalid('symmetric必须为布尔值', 'symmetric')
    snapshot = ops.graph_snapshot()
    nodes = {row['id']: row for row in snapshot['nodes']}
    if node_id not in nodes or nodes.get(event_id, {}).get('node_type') not in {'event', 'action'}:
        raise ContentGuardError('主体或变更事件不存在')
    records = ops.property_records()
    definition = next((row for row in records if row.get('kind') == 'property_definition' and row['id'] == property_id), None)
    batch = []
    if definition is None:
        if value_type is None:
            raise _invalid('新属性定义必须明确value_type', 'value_type', related_fields=('property_id',))
        batch.append({'kind': 'property_definition', 'id': property_id, 'value_type': value_type,
                      'cardinality': cardinality or 'one', 'symmetric': symmetric if symmetric is not None else False})
    else:
        for field, selected in [('value_type', value_type), ('cardinality', cardinality), ('symmetric', symmetric)]:
            if selected is not None and selected != definition.get(field, False if field == 'symmetric' else 'one' if field == 'cardinality' else None):
                raise _invalid(f'{field}与已有定义不一致，须显式更正定义', field, related_fields=('property_id',))
    record = {'kind': 'property_change', 'node_id': node_id, 'property_id': property_id, 'event_id': event_id,
              'op': op, 'sequence': max((row['sequence'] for row in records if row.get('kind') == 'property_change' and row['event_id'] == event_id), default=-1)+1,
              'scope': scope, 'context_id': context_id}
    if value is not MISSING:
        record['value'] = value
    for field, item in [('holder_id', holder_id), ('category', category), ('source', source), ('related_nodes', related_nodes)]:
        if item is not None:
            record[field] = item
    if quote is not None:
        if not isinstance(quote, str) or not quote:
            raise _invalid('引文须为准确非空文本', 'quote')
        source_id = source_id or (nodes[event_id].get('disclosure') or {}).get('source_id')
        hits = ops.locate(source_id, [quote], limit_per_quote=2)[0]['matches']
        if len(hits) != 1:
            raise _invalid('变更引文须逐字唯一匹配原文', 'quote', related_fields=('source_id', 'event_id'))
        record['evidence'] = {'source_id': source_id, 'quote': quote}
        record['disclosure'] = {'source_id': source_id, 'start': hits[0]['start']}
    batch.append(record)
    return ops.apply_changes([], batch, expected_revision, reason)
