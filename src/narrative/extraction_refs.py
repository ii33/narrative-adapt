"""Draft references resolve by semantic identity: formal ID or node name, never ``@n``.

草稿里的引用字段写**有语义的标识**，不写按数组位置编号的定位：位置是提交的排版，
不是身份——丢一行、换个顺序就整体错位，模型也记不住（`I-29`，见
``docs/extraction/SPEC.md``）。

解析顺序：正式 ID（指向工程已有节点）→ 本批唯一名称 → 名称本身跨类型时按字段要求
的类型筛（`allowed`）。`@n` 一律拒收，报错必须给出候选与可用写法，让模型一轮改对。

本模块内部仍用草稿行的原始槽位 ``@n`` 做**行标签**（`path` 定位、去重归并都要它），
那与 patch 的整数 index 同性质：说明「第几行」，不是引用。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from copy import deepcopy

# 事件参与者只能是"参与事件的主体"：人和群体。地点有事件自身的地点属性，
# 物品有持有者，二者混进参与者等于把同一条事实写两遍。
PARTICIPANT_TYPES = {'person', 'group'}
KIND_LABELS = {'person': '人物', 'group': '群体', 'item': '物品', 'location': '地点',
               'scene': '场景', 'big_event': '大事件', 'storyline': '主要故事',
               'event': '事件', 'action': '动作'}
MOMENT_TYPES = {'event', 'action'}
ID_PATTERN = re.compile(r'[PGILEASTBN][0-9]+(?:\.[0-9]+)*\Z')
AT_INDEX = re.compile(r'@[0-9]+\Z')


def _labels(kinds, *, unique: bool = True) -> str:
    """候选的类型标签。

    歧义报错要保留重复（``场景、场景`` 正是「同名同类」的证据），所以 ``unique=False``；
    讲「这里需要哪种类型」时去掉重复。
    """
    labels = [KIND_LABELS.get(str(kind), str(kind)) for kind in kinds]
    return '、'.join(dict.fromkeys(labels) if unique else labels)


def _kinds(candidates) -> list:
    return [row.get('type') if local else row.get('node_type') for _ref, row, local in candidates]


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
        """Resolve one draft reference to the identity it names.

        认得三种写法，按这个顺序：**正式 ID**（本工程已有节点，任何时候都成立）、
        **本批唯一名称**、**名称 + 字段类型**（同名跨类型时用 ``allowed`` 筛）。
        ``@n`` 不再接受：它是数组位置，不是身份；报错要写清候选与实际可用的写法。
        """
        if value is None or value == '':
            if optional:
                return ''
            raise ValueError('必须提供节点引用：写本工程正式 ID 或节点名称')
        if not isinstance(value, str):
            raise ValueError('节点引用必须是字符串：本工程正式 ID 或节点名称')
        if literal and value in {'无', '未知'}:
            return value
        if value.startswith('@'):
            raise ValueError(f'引用「{value}」无效：不使用 @ 索引，请写名称或正式 ID')
        if value in self.current:
            candidates = [(value, self.current[value], False)]
        elif ID_PATTERN.fullmatch(value):
            raise ValueError(f'正式节点ID不存在：{value}；若这是本批新建的节点，请写它的名称')
        else:
            candidates = list(self.names.get(value, []))
            if not candidates and literal:
                return value
            if not candidates:
                raise ValueError(_missing_reference_message(value, self))
        valid = []
        valid_kinds = []
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
            valid_kinds.append(kind)
        if value not in self.current:
            # 名称形式先认本批：同一批里刚声明的节点就是模型正在说的那个，
            # 工程里的同名节点只在没写进本批时才作为候选。
            local_matches = [item for item in valid if item[2]]
            if local_matches:
                valid = local_matches
        unique = {}
        for identity, ref, _local in valid:
            unique.setdefault(identity, ref)
        if len(unique) == 1:
            return next(iter(unique.values()))
        if valid:
            where = '本批' if valid[0][2] else '工程'
            raise ValueError(f'{where}有 {len(unique)} 个「{value}」：'
                             f'{_labels(valid_kinds, unique=False)}；请改名区分或使用正式 ID')
        # 引用确实指向节点、只是类型或语境不对时，说清是哪种类型不对，而不是让它
        # 看起来像「引用不存在」；候选与可用的写法一并列出，模型不用再猜是哪一个。
        if rejected_kinds:
            parts = [f'引用「{value}」的候选项是{_labels(_kinds(candidates), unique=False)}']
            if allowed:
                parts.append(f'这里需要{_labels(allowed)}')
            if allowed_note:
                parts.append(allowed_note)
            parts.append('请写名称或正式 ID')
            raise ValueError('；'.join(parts))
        raise ValueError(f'引用「{value}」指向的节点已撤回或语境不符；请写名称或正式 ID')


def _missing_reference_message(value: str, refs) -> str:
    """名字找不到时，把最像的几个名字一并给出——让模型一次改对，而不是再试一轮。

    实跑里最常见的失败就是这类：模型写了一个本批没有的名字（常常是自己拼的复合名，
    例如「仓库·租仓存货」），只回「找不到」它会换个猜法再来一次，每次都是一整轮请求。
    """
    from difflib import get_close_matches

    pool = list(refs.names) + list(refs.current)
    close = get_close_matches(value, pool, n=3, cutoff=0.4)
    hint = f'最接近的本批/工程名称：{"、".join(close)}' if close else '本批与工程里都没有形近的名称'
    return f'找不到引用「{value}」；{hint}；请写名称或正式 ID'


def bind(data: dict, snapshot: dict, records: list[dict]) -> dict:
    """Resolve only reference-typed fields, preserving the caller's original JSON."""
    result = deepcopy(data)
    resolver = References(result['nodes'], snapshot['nodes'])
    issues = result.setdefault('rejected', [])
    def ref(value, owner, path, **options):
        try:
            return resolver.resolve(value, **options)
        except ValueError as exc:
            message = str(exc)
            issues.append({'name': owner, 'reference': value, 'path': path, 'reason': message,
                           'message': message})
            return ''
    for row in result['nodes']:
        name = row['name']
        # `reference` 是草稿行的原始槽位（`@第几行`），只用来定位这一行：报错 path、
        # 去重归并、暂存索引都用它。它不是引用写法，也不进模型可见字段——模型看见的
        # 是 `display_name`。
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
