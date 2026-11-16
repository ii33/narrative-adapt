"""Deterministic draft validation and automatic apply tools for the conversation agent."""

from __future__ import annotations

import re

DRAFT_VERSION = "conversation-draft/21"
# How many info notices preview repeats verbatim; the rest is only counted.
INFO_PREVIEW_LIMIT = 5

RESULT_SCHEMA = {
    "type": "object", "required": ["nodes", "properties"],
    "description": "节点引用写有语义的标识：优先本工程已有正式ID（P1、E2），否则写本批节点的名称；同名跨类型时按字段要求的类型区分，不写@位置索引。",
    "properties": {
        "nodes": {"type": "array", "items": {"type": "object", "required": ["name", "type"],
            "allOf": [{"if": {"properties": {"type": {"const": "event"}}},
                       "then": {"required": ["participants"],
                                "anyOf": [{"required": ["anchor"]}, {"required": ["quotes"]}]}}],
            "properties": {
                "name": {"type": "string"},
                "type": {"type": "string", "enum": ["person", "group", "item", "location", "storyline", "big_event", "scene", "event", "action"],
                    "description": "event是一个事件里还分得出「怎么做的」若干步的粒度等级；拆不开、只有一步的属于动作层（action）。同场可有多个相邻事件，场景只含一个事件也成立，不按章节或实施步骤机械拆分。action仅在明确请求动作提取时使用。"},
                "quotes": {"type": "array", "items": {"type": "string"},
                    "description": "仅event可填：原文证据。程序负责定位——先逐字，只有排版差异（全半角／空白／标点）时按归一化校正，存进证据的永远是原文截取文本；归一化后也找不到就丢弃这条引文、事件照常入库并回一条info。场景／大事件／主要故事由下级派生，其它节点与属性以参与的事件为出处，都不填quotes（多写的不入库，只回一条info说明已丢弃）"},
                "anchor": {"type": "object", "required": ["start", "end"], "additionalProperties": False,
                    "description": "仅event：原文Unicode位置锚点{start,end}（左闭右开），程序按它截取逐字原文作为本事件证据，推荐用它代替回抄引文；与quotes同时给出时以anchor为准",
                    "properties": {"start": {"type": "integer", "description": "原文Unicode起点，含"},
                                   "end": {"type": "integer", "description": "原文Unicode终点，不含"}}},
                "location": {"type": "string", "description": "仅event：明确地点的location节点名称或正式ID，或无/未知；程序复用本事件的定位依据（anchor 截取的文本，或 quotes）生成自身world地点变更，不与显式地点properties双写；省略不自动补未知"},
                "aliases": {"type": "array", "items": {"type": "string"}},
                "existing_id": {"type": "string", "description": "查证后引用本工程已有节点；不新建同一事件"},
                "new_occurrence": {"type": "boolean", "description": "同名但原文明确是另一次发生时声明"},
                "context_id": {"type": "string"},
                "participants": {"type": "array", "items": {"type": "string"},
                    "description": "事件的参与者必填：人物或群体的名称（或本工程正式ID），含次要、无名人物；地点用事件自身的地点属性、物品用持有者属性，不写进参与者；确无参与者或原文未交代时给空数组"},
                "children": {"type": "array", "items": {"type": "string"}, "description": "容器直接子项的名称或正式ID，可归入已有子项而不重列原文；按T→B→S→E→A校验"},
                "scene": {"type": "string", "description": "同语境场景的名称或正式ID"},
                "event": {"type": "string", "description": "action直属事件的名称或正式ID，仅按需动作提取使用"},
                "big_event": {"type": "string", "description": "scene所属阶段性大事件的名称或正式ID（B编号）"},
                "storyline": {"type": "string", "description": "big_event所属主要故事的名称或正式ID（T编号）；未总结篇章可省略"},
            }, "additionalProperties": False}},
        "properties": {"type": "array", "items": {"type": "object", "required": ["node", "property", "category", "at_event", "op"],
            "properties": {
                "node": {"type": "string", "description": "属性主体的名称或正式ID；身份、关系或作用角色中为参照方A"}, "property": {"type": "string", "description": "关系描述对方相对node的身份、关系或作用中的角色；node=A,property=父亲,value=B表示B是A的父亲；事件地点使用属性名‘地点’并引用地点节点"},
                "op": {"type": "string", "enum": ["set", "add", "remove", "clear", "unknown"], "description": "在at_event处建立/设置、增添、移除、明确清空或变为未知；不填写有效期"},
                "value": {"description": "node/either节点引用表示被指向的对方的名称或正式ID；普通string/any值保持字面内容；每个event必须有自身world地点，value引用location节点或显式‘无’‘未知’，也可op=unknown不携value"}, "value_type": {"type": "string", "enum": ["string", "number", "boolean", "node", "either", "any"], "description": "节点引用用node/either；事件地点建议either以支持location引用及‘无’‘未知’，已有node定义可引用location；string/any即使内容像P编号也是字面值"},
                "cardinality": {"type": "string", "enum": ["one", "many"], "description": "新定义默认one；多值状态显式many且value为数组。已有定义省略沿用，显式冲突拒绝，须显式修改定义"},
                "symmetric": {"type": "boolean", "description": "该关系性质对称时显式true；单向false，不按名称猜测"},
                "category": {"type": "string", "description": "必填：填该节点类型分类表里的一个大类；原文看不出归类时显式填「未分类」，程序不会按属性名或取值猜分类"}, "scope": {"type": "string", "enum": ["world", "cognition", "claim"], "description": "world为原文支持的客观事实；事件地点为world；cognition为某人物所知所信/主观判断，claim为人物言说主张，二者须给holder；对白可有客观事实依据，不机械全记claim"},
                "holder": {"type": ["string", "null"], "description": "cognition的认知者或claim的说话人名称（或正式ID）；node仍是被认知/被谈论的主体，可与holder不同；例如甲以为乙是教师：node乙、scope=cognition、holder甲；world不填"}, "source": {"type": "string"},
                "related_nodes": {"type": "array", "items": {"type": "string"}},
                "at_event": {"type": "string", "description": "必须给本次建立/变更/失效所在事件的名称或正式ID；程序按事件顺序推状态，sequence由已定位引文或事件位置与提交顺序生成"},
            }, "additionalProperties": False}},
        "uncertainties": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "exclusions": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    }, "additionalProperties": False,
}

_PROPERTY_DRAFT_SCHEMA = RESULT_SCHEMA['properties']['properties']['items']
_PROPERTY_DRAFT_SCHEMA['oneOf'] = [
    {'properties': {'op': {'enum': ['set', 'add', 'remove']}}, 'required': ['value']},
    {'properties': {'op': {'enum': ['clear', 'unknown']}}, 'not': {'required': ['value']}},
]


def submission_input_schema() -> dict:
    """The exact public draft-call schema; no storage-layer fields or tool import."""
    from copy import deepcopy
    return {"type": "object", "additionalProperties": False, "required": ["source_id", "result"],
        "properties": {
            "source_id": {"type": "string", "description": "当前工程原文ID，放在外层，不能写进result"},
            "result": deepcopy(RESULT_SCHEMA),
            "start": {"type": "integer", "description": "本批实际原文范围的全局Unicode起点，放外层；默认0"},
            "end": {"type": "integer", "description": "本批实际原文范围的全局Unicode终点，不包含；放外层，默认原文末尾"}}}


def submission_parameters() -> dict:
    """Tool binding metadata derives from that same authoritative JSON schema."""
    schema = submission_input_schema()
    return {key: {**value, "required": key in schema["required"]}
            for key, value in schema["properties"].items()}


def _source_range(ops, source_id: str, start: int, end: int | None):
    source = ops.source(source_id)
    text = source["text"]
    if isinstance(start, bool) or not isinstance(start, int):
        raise ValueError("start必须是整数Unicode字符位置")
    if end is not None and (isinstance(end, bool) or not isinstance(end, int)):
        raise ValueError("end必须是整数Unicode字符位置")
    end = len(text) if end is None else end
    if not 0 <= start <= end <= len(text):
        raise ValueError("提取范围超出原文；start/end必须是原文Unicode字符位置")
    return source, text[start:end], start, end


class ExtractionDraftValidationError(ValueError):
    """Content failure carrying all independent original-input diagnostics."""
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__('；'.join(item['message'] for item in diagnostics))


# 草稿里的**无效参数**只丢弃并提醒（`U-10`）：多余字段、摆在没意义的字段上的东西、
# 未知字段都不改变模型想表达的意思，丢掉以后这一行仍然可读。所以校验分两步——先剥
# （`_prune_draft`）并记账，再按剥完的对象严格校验（`_draft_input_diagnostics`，
# `additionalProperties: false` 留在这第二步兜底）。只有「看不懂」的输入才整批拒收：
# 缺必填、类型不对、引用解析不到、事件没有定位依据。
#
# 记账口径同 `extraction_staging._compact`：同一 (section, field) 折叠成一条 info，
# `count` 是实际处数，`indexes` 是涉及条目（最多列 12 个），不铺几十行一样的提示。
DROPPED_INDEX_LIMIT = 12
_NODE_DRAFT_FIELDS = set(RESULT_SCHEMA['properties']['nodes']['items']['properties'])
_PROPERTY_DRAFT_FIELDS = set(_PROPERTY_DRAFT_SCHEMA['properties'])
_DROPPED_REASONS = {
    ('nodes', 'quotes'): '只有事件需要定位依据，其它节点与场景／大事件／主要故事以参与的事件或下级证据为出处',
    ('properties', 'quotes'): '属性的出处就是 at_event 指向的那个事件，逐字证据记在事件上',
    ('nodes', 'anchor'): '位置锚点只给事件用，其它节点的位置来自它挂靠的事件',
}
_DROPPED_LABELS = {'nodes': '节点行', 'properties': '属性行'}
_DROPPED_UNKNOWN = '草稿只收合同里写明的字段，不认的字段一律丢弃'


class _DroppedParameters:
    """丢弃记账：同类 ``(section, field)`` 只留一行，``count`` 记处数、``indexes`` 记条目。"""

    def __init__(self):
        self._groups: dict[tuple[str, str], dict] = {}
        self._order: list[tuple[str, str]] = []

    def add(self, section: str, field: str, index: int | None = None):
        key = (section, field)
        group = self._groups.get(key)
        if group is None:
            group = self._groups[key] = {'count': 0, 'indexes': []}
            self._order.append(key)
        group['count'] += 1
        if index is not None and index not in group['indexes']:
            group['indexes'].append(index)

    def diagnostics(self) -> list[dict]:
        rows = []
        for section, field in self._order:
            group = self._groups[(section, field)]
            reason = _DROPPED_REASONS.get((section, field), _DROPPED_UNKNOWN)
            label = _DROPPED_LABELS.get(section, '草稿顶层')
            # 顶层的未知字段没有 section 可指，path 直接写那个字段；行内的写到它的数组上，
            # 具体是哪几行看 indexes。
            path = [field] if section == 'result' else [section]
            rows.append({'severity': 'info', 'code': 'EXTRACTION_INFO', 'path': path,
                'field': field, 'count': group['count'],
                'indexes': group['indexes'][:DROPPED_INDEX_LIMIT],
                'message': f'{label}的{field}已丢弃：{reason}；共 {group["count"]} 处，其余内容照常入库'})
        return rows


def _is_property_shaped(row: dict) -> bool:
    """属性行写进 nodes 的兜底形状：没有 type，但有 node 与 property。

    判据与 `parse_nodes` 认这种行时一致——按形状认，不按数组位置认，所以剥字段时
    也不能把它当节点行，否则 ``node``／``property`` 会被当成未知字段丢掉。
    """
    return (not str(row.get('type') or '').strip()
            and str(row.get('node') or '').strip() and str(row.get('property') or '').strip())


def _prune_node_row(row: dict, dropped: _DroppedParameters, index: int) -> dict:
    kept = {}
    is_event = str(row.get('type') or '').strip() == 'event'
    for key, value in row.items():
        if key not in _NODE_DRAFT_FIELDS:
            dropped.add('nodes', str(key), index)
        elif key in ('quotes', 'anchor', 'location') and not is_event:
            # 事件以外的节点没有自身位置：引文与锚点都归它挂靠的事件（`U-09`），
            # 地点也只在事件上（场景不承担地点身份）——多写的丢掉并提示，不作废整批。
            dropped.add('nodes', key, index)
        else:
            kept[key] = value
    return kept


def _prune_property_row(row: dict, dropped: _DroppedParameters, index: int) -> dict:
    kept = {}
    for key, value in row.items():
        if key in _PROPERTY_DRAFT_FIELDS:
            kept[key] = value
        else:
            dropped.add('properties', str(key), index)
    return kept


def _prune_draft(result, dropped: _DroppedParameters):
    """第一步：剥掉草稿里的无效参数并记账，返回剥完的副本（不改调用方的对象）。

    只剥**字段**，不删行：一行被剥空以后仍留在原位，由第二步按「缺必填」拒收——
    例如只给了 quotes 的人物，模型本来想写的是什么看不出来，不能静默丢成空节点。
    """
    if not isinstance(result, dict):
        return result
    pruned = {}
    for key, value in result.items():
        if key in RESULT_SCHEMA['properties']:
            pruned[key] = value
        else:
            dropped.add('result', str(key))
    for section in ('nodes', 'properties'):
        rows = pruned.get(section)
        if not isinstance(rows, list):
            continue
        cleaned = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                cleaned.append(row)  # 不是对象：第二步按类型错拒收，这里不动它
            elif section == 'properties' or _is_property_shaped(row):
                cleaned.append(_prune_property_row(row, dropped, index))
            else:
                cleaned.append(_prune_node_row(row, dropped, index))
        pruned[section] = cleaned
    return pruned


def _draft_input_diagnostics(result):
    """第二步：按**已经剥完**的草稿做严格校验，只留「看不懂」的问题。

    行内的未知字段、非事件与属性带的 ``quotes``／``anchor`` 已经由 `_prune_draft`
    剥掉并记成 info（`U-10`），这里的同名检查是兜底：`additionalProperties: false`
    的语义仍然成立，真有漏网的未知字段照样报错。
    """
    issues = []
    def issue(path, message):
        issues.append({'severity': 'error', 'code': 'DRAFT_SCHEMA', 'path': path, 'message': message})
    if not isinstance(result, dict):
        issue([], 'result必须是对象')
        return issues
    for section in ('nodes', 'properties'):
        if not isinstance(result.get(section), list):
            issue([section], f'{section}缺失或不是数组；即使没有内容也须显式传空数组')
    for key in set(result) - set(RESULT_SCHEMA['properties']):
        issue([key], f'未知草稿字段：{key}')
    for section in ('uncertainties', 'exclusions'):
        rows = result.get(section, [])
        if not isinstance(rows, list):
            issue([section], f'{section}必须是对象数组')
        else:
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    issue([section, index], f'{section}条目必须是对象')
    allowed_node_fields = set(RESULT_SCHEMA['properties']['nodes']['items']['properties'])
    for index, row in enumerate(result.get('nodes') if isinstance(result.get('nodes'), list) else []):
        path = ['nodes', index]
        if not isinstance(row, dict):
            issue(path, '节点行必须是对象')
            continue
        for key in set(row) - allowed_node_fields:
            issue(path + [key], f'未知节点字段：{key}')
        if 'location' in row and (row.get('type') != 'event' or not isinstance(row['location'], str) or not row['location'].strip()):
            issue(path + ['location'], f'nodes[{index}].location仅event可填非空地点引用、无或未知')
        expected_parent = {'scene': 'big_event', 'big_event': 'storyline', 'event': 'scene', 'action': 'event'}.get(str(row.get('type') or ''))
        for key in ('storyline', 'big_event', 'scene', 'event'):
            if key in row and key != expected_parent:
                issue(path + [key], f'父级字段{key}不适用于节点类型{row.get("type")}')
        if 'quotes' in row and row.get('type') != 'event':
            issue(path + ['quotes'],
                  f'nodes[{index}].quotes仅event可填：其它节点以它参与的事件为出处，'
                  '场景／大事件／主要故事由下级派生，都不提供自身引文')
        elif 'quotes' in row and not isinstance(row['quotes'], list):
            issue(path + ['quotes'], 'quotes必须是字符串数组')
        elif isinstance(row.get('quotes'), list):
            for quote_index, quote in enumerate(row['quotes']):
                if not isinstance(quote, str) or not quote.strip():
                    issue(path + ['quotes', quote_index], '引文必须是非空原文字符串')
        if 'anchor' in row:
            anchor = row['anchor']
            if row.get('type') != 'event':
                issue(path + ['anchor'], f'nodes[{index}].anchor仅event可填：原文位置锚点只给事件定位')
            elif not isinstance(anchor, dict) or set(anchor) != {'start', 'end'}:
                issue(path + ['anchor'], 'anchor必须只含start与end两个整数Unicode偏移')
            elif (type(anchor['start']) is not int or type(anchor['end']) is not int
                    or not 0 <= anchor['start'] < anchor['end']):
                issue(path + ['anchor'], 'anchor必须满足 0 <= start < end（原文Unicode偏移，左闭右开）')
        if row.get('type') == 'event':
            located = 'anchor' in row or any(
                isinstance(quote, str) and quote.strip() for quote in row.get('quotes') or [])
            if not located:
                issue(path + ['anchor'],
                      '事件必须给定位依据：anchor{start,end}（推荐，程序按偏移截取逐字原文）'
                      '或非空quotes逐字引文，二者至少一个')
    for index, row in enumerate(result.get('properties') if isinstance(result.get('properties'), list) else []):
        path = ['properties', index]
        if not isinstance(row, dict):
            issue(path, '属性行必须是对象')
            continue
        for key in set(row) - set(_PROPERTY_DRAFT_SCHEMA['properties']) - {'quotes'}:
            issue(path + [key], f'未知属性字段：{key}；只使用事件变更合同，不接受有效期或模型sequence')
        if 'quotes' in row:
            issue(path + ['quotes'],
                  '属性不接受quotes：属性的出处就是at_event指向的那个事件，逐字证据记在事件上')
        for field in ('node', 'property'):
            if not isinstance(row.get(field), str) or not row.get(field, '').strip():
                issue(path + [field], f'{field}必须是非空字符串')
        if ('value' in row) != (isinstance(row.get('op'), str) and row['op'] in {'set', 'add', 'remove'}):
            issue(path + ['value'], 'set/add/remove须有value，clear/unknown不能有value')
        if not isinstance(row.get('at_event'), str) or not row.get('at_event', '').strip():
            issue(path + ['at_event'], '必须明确at_event事件引用')
        if not isinstance(row.get('op'), str) or row['op'] not in {'set', 'add', 'remove', 'clear', 'unknown'}:
            issue(path + ['op'], '必须明确有效op：set/add/remove/clear/unknown')
        if 'quotes' in row:
            issue(path + ['quotes'],
                  '属性不接受quotes：属性的出处就是at_event指向的那个事件，逐字证据记在事件上')
        if not isinstance(row.get('category'), str) or not row['category'].strip():
            issue(path + ['category'], '必须明确category：填该节点类型分类表里的一个大类，或显式填「未分类」；程序不猜分类')
    return issues


def _validate_anchors(rows, text: str, start: int, end: int) -> dict[int, tuple[int, int]]:
    """检查每个事件的原文位置锚点，返回提交范围内的相对 (start, end)。

    ``anchor`` 是原文全局 Unicode 偏移（左闭右开）：越界、反向或不在本批范围内
    都直接拒绝，并把路径指到这个字段——不猜、不夹到边界。形状由
    :func:`_draft_input_diagnostics` 先查过，这里只查与原文/批次位置有关的部分。
    """
    spans: dict[int, tuple[int, int]] = {}
    problems = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get('anchor'), dict):
            continue
        anchor = row['anchor']
        start_at, end_at = anchor.get('start'), anchor.get('end')
        if type(start_at) is not int or type(end_at) is not int or not 0 <= start_at < end_at:
            continue
        where = ['nodes', index, 'anchor']
        if end_at > len(text):
            problems.append({'severity': 'error', 'code': 'DRAFT_SCHEMA', 'path': where,
                'message': f'anchor超出原文末尾：原文共{len(text)}个Unicode字符，end不得超过它'})
        elif not (start <= start_at and end_at <= end):
            problems.append({'severity': 'error', 'code': 'DRAFT_SCHEMA', 'path': where,
                'message': f'anchor不在本次提交范围内：本批范围是[{start}, {end})，越界锚点不许用'})
        else:
            spans[index] = (start_at - start, end_at - start)
    if problems:
        raise ExtractionDraftValidationError(problems)
    return spans


def _prepare(ops, source_id: str, result: dict, start: int, end: int | None):
    from .extraction import build_extraction, parse_nodes

    snapshot = ops.capture_changes()
    reader = snapshot.reader
    source, selected, start, end = _source_range(reader, source_id, start, end)
    # 第一步剥无效参数、第二步按剥完的对象严格校验（`U-10`）。剥掉的项即使这一批
    # 因为别的原因被拒也要报出来：模型得知道哪部分已经被程序忽略、不用再改。
    dropped = _DroppedParameters()
    result = _prune_draft(result, dropped)
    input_issues = _draft_input_diagnostics(result)
    if input_issues:
        raise ExtractionDraftValidationError(dropped.diagnostics() + input_issues)
    allowed_node_fields = set(RESULT_SCHEMA["properties"]["nodes"]["items"]["properties"])
    for index, row in enumerate(result["nodes"]):
        if not isinstance(row, dict):
            raise ValueError(f"nodes[{index}]必须是对象")
        if 'location' in row and (row.get('type') != 'event' or not isinstance(row['location'], str) or not row['location'].strip()):
            raise ValueError(f'nodes[{index}].location仅event可填非空地点引用、无或未知')
        expected_parent = {"scene": "big_event", "big_event": "storyline", "event": "scene", "action": "event"}.get(str(row.get("type") or ""))
        invalid_parent = {key for key in ("storyline", "big_event", "scene", "event") if key in row and key != expected_parent}
        if invalid_parent:
            raise ValueError(f"nodes[{index}]父级字段不适用于该类型：" + "、".join(sorted(invalid_parent)))
        unknown_node = set(row) - allowed_node_fields
        if unknown_node:
            raise ValueError(f"nodes[{index}]未知字段：" + "、".join(sorted(unknown_node)))
    fields = set(_PROPERTY_DRAFT_SCHEMA['properties'])
    for index, row in enumerate(result['properties']):
        if not isinstance(row, dict) or set(row)-fields:
            raise ValueError(f'properties[{index}]字段无效；只使用事件变更合同，不接受有效期或模型sequence')
        if not isinstance(row.get('at_event'), str) or not row['at_event'].strip() or not isinstance(row.get('op'), str) or row.get('op') not in {'set', 'add', 'remove', 'clear', 'unknown'}:
            raise ValueError(f'properties[{index}]必须明确at_event与有效op')
        if ('value' in row) != (row['op'] in {'set', 'add', 'remove'}):
            raise ValueError(f'properties[{index}]set/add/remove须有value，clear/unknown不能有value')
    unknown = set(result) - set(RESULT_SCHEMA["properties"])
    if unknown:
        raise ValueError("未知草稿字段：" + "、".join(sorted(unknown)))
    from copy import deepcopy

    from .extraction_refs import bind
    anchor_spans = _validate_anchors(result['nodes'], source['text'], start, end)
    original = deepcopy(result)
    # 位置锚点在草稿层就换成逐字原文：下游（证据、披露起点、容器派生、审计）形状不变，
    # 模型也不必回抄原文。同一行同时给了 anchor 与 quotes 时以 anchor 为准，
    # 但要说出来——静默丢弃输入是最难查的那种行为。
    anchor_notices = []
    for index, (span_start, span_end) in anchor_spans.items():
        row = original['nodes'][index]
        if row.get('quotes'):
            anchor_notices.append({'severity': 'info', 'code': 'EXTRACTION_INFO',
                'path': ['nodes', index, 'quotes'],
                'message': '同时给了anchor与quotes：已按anchor截取的原文作为本事件证据，quotes未使用'})
        row['quotes'] = [source['text'][start + span_start:start + span_end]]
    current_graph = reader.graph_snapshot()
    current_records = reader.property_records()
    definitions = {row['id']: row for row in current_records if row.get('kind') == 'property_definition'}
    from .event_location import LOCATION_CATEGORY, LOCATION_PROPERTIES, LOCATION_SEMANTICS
    shortcut_indices = []
    explicit_count = len(original['properties'])
    location_definitions = [row for key, row in definitions.items()
        if key in LOCATION_PROPERTIES or row.get('semantic') in tuple(LOCATION_SEMANTICS)]
    if len(location_definitions) > 1 and any('location' in row for row in original['nodes']):
        raise ExtractionDraftValidationError([{'severity': 'error', 'code': 'DRAFT_SCHEMA',
            'path': ['nodes', index, 'location'],
            'message': '存在多个地点定义，请用显式properties指定，不能猜测location快捷字段的目标定义',
            'related_objects': [{'property_id': definition['id']} for definition in location_definitions]}
            for index, row in enumerate(original['nodes']) if 'location' in row])
    location_definition = location_definitions[0] if location_definitions else None
    property_id = location_definition['id'] if location_definition else '地点'
    for index, row in enumerate(original['nodes']):
        if 'location' not in row:
            continue
        location = row['location'].strip()
        event_name = str(row.get('name') or '').strip() if isinstance(row, dict) else ''
        if not event_name:
            # 没有名称的事件本身就会被拒收（`name必须是非空字符串`）：这里不再造一条
            # 引用不到主体的地点记录，免得多报一条看不懂的错。
            continue
        generated = {'node': event_name, 'property': property_id, 'at_event': event_name,
            'scope': 'world', 'op': 'unknown' if location == '未知' else 'set',
            'quotes': list(row.get('quotes') or []),
            # 程序自己生成的行：它的引文来自已经核对过的事件定位依据，不是模型多写的，
            # 所以不能被「属性不带引文」的丢弃规则吃掉。
            '_draft_generated': True,
            # 程序生成的行也要满足「分类必填」：地点的语义分类就是「基本信息」，
            # 与模型自己写地点属性时一致；不能用「未分类」敷衍。
            'category': LOCATION_CATEGORY,
            'value_type': location_definition['value_type'] if location_definition else 'either'}
        if index in anchor_spans:
            # 本事件用位置锚点时，这条程序生成的地点变更沿用同一段原文的精确坐标，
            # 不回到引文里再搜一次（同一句在批内出现两次时会搜到错的那一处）。
            generated['_anchor_span'] = anchor_spans[index]
        if location != '未知':
            generated['value'] = ([location] if location_definition and location_definition.get('cardinality') == 'many' else location)
            if location in {'无', '未知'}:
                # 「无」是字面值，不是地点节点名称：已有定义即使是 node 类型也不能当引用去解析。
                generated['value_type'] = 'either'
        if location_definition:
            generated['cardinality'] = location_definition.get('cardinality', 'one')
        original['properties'].append(generated)
        shortcut_indices.append(index)
    # Resolve omitted type information from explicit definitions anywhere in this batch.
    # A later clear/unknown must not turn an earlier node reference into a string.
    for row in original['properties']:
        if isinstance(row, dict) and row.get('property') not in definitions and row.get('value_type'):
            definitions[row['property']] = row
    for row in original['properties']:
        if isinstance(row, dict) and 'value_type' not in row and row.get('property') in definitions:
            row['value_type'] = definitions[row['property']]['value_type']
        if isinstance(row, dict) and 'cardinality' not in row and row.get('property') in definitions:
            row['cardinality'] = definitions[row['property']].get('cardinality', 'one')
    data = parse_nodes(original, selected, node_spans=anchor_spans, dropped=dropped,
                       property_spans={position: row['_anchor_span']
                                       for position, row in enumerate(original['properties'])
                                       if isinstance(row, dict) and '_anchor_span' in row})
    # 丢弃记账在解析之后再取：`parse_nodes`／`_parse_properties` 直接遇到的多余引文
    # 也记进同一个记账器，最后仍然按 (section, field) 折叠成一条 info（`U-10`）。
    # 引文定位的两种提醒（近似命中、找不到被丢弃）由 `parse_nodes` 自己折叠好带回来，
    # 同样是 info、同样不影响 valid（`U-11`）。
    notices = dropped.diagnostics() + list(data.get("quote_notices") or []) + anchor_notices
    for key in ("uncertainties", "exclusions"):
        rows = result.get(key) or []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"{key}必须是对象数组")
        data[key] = rows
    data = bind(data, current_graph, current_records)
    for index in shortcut_indices:
        reference = f'@{index+1}'
        # `reference` 只是这一行的槽位标签（内部定位用）；报错要说人话，用事件名称。
        node_row = next((row for row in data['nodes'] if row['reference'] == reference), {})
        owner = node_row.get('name', reference)
        display = node_row.get('display_name') or owner
        generated_index = explicit_count + shortcut_indices.index(index)
        generated = next((row for row in data['properties'] if row.get('_draft_index') == generated_index), None)
        if generated is None:
            message = f'nodes[{index}].location无法定位事件地点证据，请核对本事件的anchor或quotes'
            data['rejected'].append({'name': display, 'path': ['nodes', index, 'location'], 'message': message, 'reason': message,
                                     'related_paths': [['nodes', index, 'anchor' if index in anchor_spans else 'quotes']]})
            continue
        if generated.get('op') != 'unknown':
            values = generated.get('value')
            values = values if isinstance(values, list) else [values]
            if any(value not in ('无', '未知') and data['reference_types'].get(value) != 'location' for value in values):
                path = ['nodes', index, 'location']
                causes = [item for item in data['rejected'] if item.get('path') == ['properties', generated_index, 'value']]
                message = f'nodes[{index}].location必须引用通过校验的location节点（写地点名称或正式 ID），或填写无/未知'
                if causes:
                    message += '；' + '；'.join(item.get('message') or item.get('reason', '') for item in causes)
                data['rejected'].append({'name': display, 'path': path, 'message': message, 'reason': message,
                    'related_paths': [related for item in causes for related in item.get('related_paths', [])]})
        explicit_locations = [row for row in data['properties'] if row.get('_draft_index', explicit_count) < explicit_count
            and row.get('node') == owner and row.get('at_event') == owner
            and row.get('scope', 'world') == 'world'
            and (row.get('property') in LOCATION_PROPERTIES or definitions.get(row.get('property'), {}).get('semantic') in tuple(LOCATION_SEMANTICS))]
        if explicit_locations:
            message = f'nodes[{index}].location与本事件显式地点properties双写；保留一种明确表达后修补，不自动选择'
            data['rejected'].append({'name': display, 'path': ['nodes', index, 'location'], 'message': message, 'reason': message,
                'related_paths': [['properties', row['_draft_index']] for row in explicit_locations]})
    shortcut_paths = {explicit_count + order: ['nodes', index, 'location'] for order, index in enumerate(shortcut_indices)}
    for item in data.get('rejected', []):
        # A rejected generated row is reported where the model filled it in.
        path = item.get('path', [])
        if len(path) >= 2 and path[0] == 'properties' and path[1] in shortcut_paths:
            item['path'] = shortcut_paths[path[1]]
    issues = content_diagnostics(data, selected, source["title"])
    for kind in ("nodes", "properties"):
        for row in data[kind]:
            if row["start"] is None:
                # 没有定位依据的行没有原文坐标（证据是它参与的事件），
                # 不把 0 平移成批首再冒称一个位置。
                continue
            row["start"] += start
            if kind == "nodes":
                for span in row.get("evidence_spans") or []:
                    span["start"] += start
                    span["end"] += start
    from .extraction_containers import derive
    data = derive(data, current_graph, reader, source_id)
    if data.get("rejected"):
        diagnostics = list(notices) + [
            {"severity": "error", "code": "EXTRACTION_CONTENT", **item,
             "object_id": item.get("name"), "message": item.get("message") or item.get("reason")}
            for item in data["rejected"]]
        outcome = {"valid": False, "revision": snapshot.revision, "diagnostics": diagnostics}
        return data, {}, None, outcome
    data.update(source_id=source_id, chapter=source["title"],
                slice_text=selected, slice_start=start, slice_end=end)
    built = build_extraction(reader, data)
    # A notice about a dropped container write names the record that won. When
    # that winner is a location shortcut row, the model never wrote it as a
    # property: say which node field it actually filled in.
    origins = built.get("property_origins") or {}
    for item in built.get("diagnostics") or []:
        duplicate = item.get("duplicate_of")
        for index in (origins.get(duplicate) or []) if isinstance(duplicate, str) else []:
            if index in shortcut_paths:
                item["duplicate_of_origin"] = "location"
                item["duplicate_of_path"] = list(shortcut_paths[index])
                break
    issues += list(built.get("skipped") or [])
    prepared = None
    diagnostics = list(notices) + [{"severity": "error", "code": "EXTRACTION_CONTENT",
                    "object_id": item.get("name"), "message": item.get("reason") or str(item),
                    **{key: item[key] for key in ('path', 'related_paths', 'quote', 'positions_sample', 'position_basis') if key in item}} for item in issues]
    # Notices the build raised about rows it deliberately did not store. They
    # never turn the batch invalid; they are reported so the model can see the
    # decision instead of silently disagreeing with the stored result.
    diagnostics += [{**item, "severity": item.get("severity", "info"),
                     "code": item.get("code", "EXTRACTION_INFO"),
                     "message": item.get("message") or item.get("reason")}
                    for item in built.get("diagnostics") or []]
    if not issues and not built.get("empty"):
        prepared = ops.prepare_changes(built["objects"], built["property_records"], snapshot.revision, snapshot=snapshot)
        diagnostics += prepared.diagnostics
    valid = not any(row.get("severity") == "error" for row in diagnostics)
    outcome = {"valid": valid, "revision": snapshot.revision,
               "node_count": built.get("node_count", 0), "property_count": built.get("property_count", 0),
               "uncertainty_count": len(data["uncertainties"]), "diagnostics": diagnostics}
    return data, built, prepared, outcome


def _compact(outcome: dict) -> dict:
    """Compact preflight view: actionable problems plus a bounded info summary.

    Info rows never move ``valid``: they report a decision the build made, such
    as a write it dropped. ``preview`` shows the first few so the same notice is
    visible without re-reading the draft; the rest is only counted.
    """
    result = {key: value for key, value in outcome.items() if key != "diagnostics"}
    diagnostics = outcome.get("diagnostics") or []
    from .diagnostic_locations import compact_problems, normalize_problems
    actionable = [row for row in diagnostics if row.get("severity") in {"error", "warning"}]
    if actionable:
        # 预检也要折叠：26 条属性各缺 op/value 就是 52 行同一句话。
        result['diagnostics'] = compact_problems(
            normalize_problems(actionable, operation='submit_extraction'))
    notices = [row for row in diagnostics if row.get("severity") == "info"]
    if notices:
        result["info_count"] = len(notices)
        result["info"] = normalize_problems(notices[:INFO_PREVIEW_LIMIT], operation='submit_extraction')
        if len(notices) > INFO_PREVIEW_LIMIT:
            result["info_omitted"] = len(notices) - INFO_PREVIEW_LIMIT
    return result


def preview(ops, source_id: str, result: dict, start: int = 0, end: int | None = None) -> dict:
    try:
        return _compact(_prepare(ops, source_id, result, start, end)[3])
    except ExtractionDraftValidationError as error:
        return _compact({'valid': False, 'revision': ops.graph_project_info()['revision'],
                         'diagnostics': error.diagnostics})


def submit(ops, source_id: str, result: dict, start: int = 0, end: int | None = None) -> dict:
    from .extraction_staging import submit as stage_submit
    return stage_submit(ops, source_id, result, start, end)


def _organization_headings(text: str, title: str = "") -> list[dict]:
    """Identify whole heading lines; quoted mentions inside prose do not count."""
    headings = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        label = content.strip().lstrip("\ufeff")
        numbered = re.fullmatch(
            r"第[0-9０-９零〇一二三四五六七八九十百千万两]+[章卷部集节回篇]"
            r"(?:\s+[^。！？!?“”‘’「」\r\n]+)?", label,
        )
        if label and (label == title.strip() or numbered or re.match(r"^#{1,6}\s+\S", label)):
            headings.append({"text": label, "start": cursor, "end": cursor + len(content)})
        cursor += len(line)
    return headings

def _only_heading_evidence(quotes: list[str], text: str, headings: list[dict]) -> bool:
    if not quotes or not headings:
        return False
    for quote in quotes:
        offset = 0
        found = False
        while (start := text.find(quote, offset)) >= 0:
            found = True
            end = start + len(quote)
            if not any(row["start"] <= start and end <= row["end"] for row in headings):
                return False
            offset = start + max(1, len(quote))
        if not found:
            return False
    return True

def content_diagnostics(data: dict, text: str, title: str = "") -> list[dict]:
    """Check structural content boundaries without building records or writing."""
    rejected = []
    node_types = dict(data.get("reference_types") or {})
    node_types.update({node.get("name"): node.get("type") for node in data.get("nodes") or []})
    for prop in data.get("properties") or []:
        base = re.split(r"[（(]", str(prop.get("property") or ""), maxsplit=1)[0].strip()
        value = prop.get("value")
        if (base == "关系"
                and node_types.get(prop.get("node")) == "person"
                and isinstance(value, str) and node_types.get(value) == "person"):
            rejected.append({
                "name": f"{prop.get('node')}.{prop.get('property')}",
                "node": prop.get("node"), "property": prop.get("property"), "value": prop.get("value"),
                "path": ['properties', prop['_draft_index'], 'property'] if '_draft_index' in prop else [],
                "reason": "人物关系的 property 需要具体关系性质，不能把分类名“关系”当作性质；"
                          "请依据原文判断，无法确定则说明存疑而非编造",
            })
    if text:
        headings = _organization_headings(text, title)
        for node in data.get("nodes") or []:
            if node.get("type") == "big_event" and _only_heading_evidence(
                node.get("quotes") or [], text, headings,
            ):
                rejected.append({
                    "name": node.get("display_name", node.get("name")), "type": "big_event",
                    "path": ['nodes', int(node['reference'][1:]) - 1, 'children'] if node.get('reference', '').startswith('@') else [],
                    "reason": "大事件下级证据只来自卷章组织标题，缺少正文发生过程证据；请核对下级节点及其证据",
                })
    return rejected
