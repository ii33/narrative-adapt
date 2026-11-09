"""Revision-local typed references; @indices never become persisted aliases."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from copy import deepcopy

# 事件参与者只能是"参与事件的主体"：人和群体。地点有事件自身的地点属性，
# 物品有持有者，二者混进参与者等于把同一条事实写两遍。
PARTICIPANT_TYPES = {'person', 'group'}
KIND_LABELS = {'person': '人物', 'group': '群体', 'item': '物品', 'location': '地点'}
MOMENT_TYPES = {'event', 'action'}
ID_PATTERN = re.compile(r'[PGILEASTBN][0-9]+(?:\.[0-9]+)*\Z')


class References:
    def __init__(self, nodes: list[dict], current: list[dict]):
        self.local = {row['reference']: row for row in nodes}
        self.current = {row['id']: row for row in current}
        self.aliases = {}
        identities = {}
        for ref, row in self.local.items():
            key = json.dumps([row.get('type'), row.get('name'), (row.get('quotes') or [''])[0],
                              row.get('context_id', 'canon'),
                              ref if row.get('type') in {'scene', 'big_event', 'storyline'} else None],
                             ensure_ascii=False, sort_keys=True)
            canonical = identities.setdefault(key, ref)
            self.aliases[ref] = canonical
        self.names = defaultdict(list)
        for ref, row in self.local.items():
            self.names[row['name']].append((ref, row, True))
        for ref, row in self.current.items():
            self.names[row.get('name')].append((ref, row, False))

    def resolve(self, value, *, allowed=None, context=None, optional=False, literal=False,
                allowed_note=None):
        if value is None or value == '':
            if optional:
                return ''
            raise ValueError('必须提供节点引用')
        if not isinstance(value, str):
            raise ValueError('节点引用必须是正式ID或@索引字符串')
        if literal and value in {'无', '未知'}:
            return value
        if value.startswith('@'):
            if not re.fullmatch(r'@[1-9][0-9]*', value) or value not in self.local:
                raise ValueError(f'局部引用{value}无效，索引按原始nodes完整数组从1起，拒绝项不重编号')
            canonical = self.aliases[value]
            candidates = [(canonical, self.local[canonical], True)]
        elif value in self.current:
            candidates = [(value, self.current[value], False)]
        elif ID_PATTERN.fullmatch(value):
            raise ValueError(f'正式节点ID不存在：{value}；若这是新节点名称请用@索引')
        else:
            candidates = self.names.get(value, [])
            if not candidates and literal:
                return value
        valid = []
        rejected_kinds = []
        for ref, row, local in candidates:
            kind = row.get('type') if local else row.get('node_type')
            if row.get('retracted'):
                continue
            if allowed is not None and kind not in allowed:
                rejected_kinds.append(kind)
                continue
            if context is not None and row.get('context_id', 'canon') != context:
                continue
            canonical = self.aliases.get(ref, ref)
            identity = row.get('existing_id') if local else ref
            valid.append((identity or canonical, canonical, local))
        if not value.startswith('@') and value not in self.current:
            local_matches = [item for item in valid if item[2]]
            if local_matches:
                valid = local_matches
        unique = {}
        for identity, ref, _local in valid:
            unique.setdefault(identity, ref)
        if len(unique) != 1:
            # 引用确实指向一个节点、只是类型不对时，说清是哪种类型不对，
            # 而不是让它看起来像"引用不存在"。
            if allowed_note and rejected_kinds and not valid:
                kinds = '、'.join(dict.fromkeys(KIND_LABELS.get(kind, str(kind)) for kind in rejected_kinds))
                raise ValueError(f'引用{value}是{kinds}节点：{allowed_note}')
            raise ValueError(f'引用{value}不存在、歧义或类型/语境不符；请使用正式ID或明确@索引')
        return next(iter(unique.values()))


def bind(data: dict, snapshot: dict, records: list[dict]) -> dict:
    """Resolve only reference-typed fields, preserving the caller's original JSON."""
    result = deepcopy(data)
    resolver = References(result['nodes'], snapshot['nodes'])
    issues = result.setdefault('rejected', [])
    def ref(value, owner, path, **options):
        try:
            return resolver.resolve(value, **options)
        except ValueError as exc:
            causes = []
            if isinstance(value, str) and re.fullmatch(r'@[1-9][0-9]*', value):
                target = ['nodes', int(value[1:]) - 1]
                causes = [issue for issue in issues if issue.get('path', [])[:2] == target]
            message = str(exc)
            if causes:
                message += '；引用目标未通过校验：' + '；'.join(dict.fromkeys(str(c.get('message') or c.get('reason')) for c in causes))
            issues.append({'name': owner, 'reference': value, 'path': path, 'reason': message,
                           'message': message, **({'related_paths': [c['path'] for c in causes]} if causes else {})})
            return ''
    for row in result['nodes']:
        name = row['name']
        path = ['nodes', int(row['reference'][1:]) - 1]
        row['display_name'] = name
        row['name'] = resolver.aliases[row['reference']]
        context = row.get('context_id', 'canon')
        for field, kind in [('scene', 'scene'), ('event', 'event'), ('big_event', 'big_event'), ('storyline', 'storyline')]:
            row[field] = ref(row.get(field), name, path + [field], allowed={kind}, context=context, optional=True)
        child_kind = {'storyline':'big_event','big_event':'scene','scene':'event','event':'action'}.get(row['type'])
        row['children'] = [ref(value, name, path + ['children', index], allowed=({"scene", "event"} if row["type"] == "scene" else {child_kind}) if child_kind else set(), context=context)
                           for index, value in enumerate(row.get('children', []))]
        row['participants'] = [ref(value, name, path + ['participants', index], allowed=PARTICIPANT_TYPES,
                                   allowed_note='事件参与者只能是人物或群体；地点请用事件自身的地点属性，物品请用持有者属性')
                               for index, value in enumerate(row.get('participants', []))]
        row['participants_not_in_text'] = []
    types = {row['id']: row.get('value_type') for row in records if row.get('kind') == 'property_definition'}
    for property_index, row in enumerate(result.get('properties', [])):
        owner = str(row.get('node')) + '.' + str(row.get('property'))
        path = ['properties', row.get('_draft_index', property_index)]
        row['node'] = ref(row.get('node'), owner, path + ['node'])
        value_type = row.get('value_type') or 'string'
        types[row['property']] = value_type
        if value_type in {'node', 'either'} and 'value' in row:
            value = row.get('value')
            row['value'] = ([ref(item, owner, path + ['value', index], literal=value_type == 'either') for index, item in enumerate(value)]
                            if isinstance(value, list) else ref(value, owner, path + ['value'], literal=value_type == 'either'))
        row['holder'] = ref(row.get('holder'), owner, path + ['holder'], allowed={'person'},
                            optional=(row.get('scope') or 'world') == 'world')
        row['at_event'] = ref(row.get('at_event'), owner, path + ['at_event'], allowed=MOMENT_TYPES)
        row['related_nodes'] = [ref(value, owner, path + ['related_nodes', index]) for index, value in enumerate(row.get('related_nodes', []))]
    result['reference_types'] = {**{ref: row['type'] for ref, row in resolver.local.items()},
                                 **{ref: row['node_type'] for ref, row in resolver.current.items()}}
    result['property_reference_types'] = types
    return result
