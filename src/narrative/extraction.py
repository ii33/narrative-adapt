"""Validate conversation-produced facts and build current graph records."""

from __future__ import annotations

import json
import unicodedata
from typing import NamedTuple, cast

from .order_ids import allocate, prefix_for
from .property_core import container_property_duplicate
from .property_labels import BIG_EVENT_PROGRESS, STORYLINE_PROGRESS
from .source_location import ExactQuoteLookup

NODE_TYPES = ("person", "group", "item", "location", "scene", "event", "action", "big_event", "storyline")











VALUE_TYPES = ("string", "number", "boolean", "node", "any", "either")
# 参与者是"参与事件的主体"，只有人和群体。地点有事件自身的地点属性、物品有持有者，
# 二者都由各自的属性族记录，混进参与者会让同一条事实在两个地方各写一遍。
ALLOWED_PARTICIPANTS = ("person", "group")
PARTICIPANT_TYPE_LABELS = {"item": "物品", "location": "地点", "scene": "场景", "event": "事件",
                           "action": "动作", "big_event": "大事件", "storyline": "主要故事"}


def _diagnostic(issues: list[dict], path: list, name: str, reason: str, **details):
    issues.append({'name': name, 'path': path, 'reason': reason, 'message': reason, **details})


def _batch_participant_kind(value: str, rows: list, declared: dict[str, str]) -> str:
    """Best-effort type of a participant reference *inside this draft*.

    Only the batch is visible here: ``@N`` points at the original nodes array and
    a name is usable when this batch declares it. Anything else (a formal ID or a
    name that only exists in the graph) is left to reference resolution, which
    sees the stored graph.
    """
    if value.startswith("@") and value[1:].isdigit():
        index = int(value[1:])
        if 1 <= index <= len(rows) and isinstance(rows[index - 1], dict):
            return str(rows[index - 1].get("type") or "").strip()
        return ""
    return declared.get(value, "")


def _parse_properties(rows, text: str, names: set[str], rejected: list[dict], locations: dict,
                      lookup=None, spans: dict[int, tuple[int, int]] | None = None, *,
                      dropped=None, source=None) -> list[dict]:
    """Verify property observations the same way as nodes: exact quote, known owner.

    ``quotes`` 是可选的：给了就按节点同样的定位规则（先逐字，只差排版时归一化校正，
    `U-11`），不给就不产生证据。
    但属性不该自带引文（出处就是 ``at_event``），模型多写的一律丢掉并记进
    ``dropped``，不整批拒收（`U-10`）。程序自己生成的行（事件地点快捷字段）例外：
    它的引文来自已经核对过的事件定位依据，标记 ``_draft_generated``。这些行还可能带
    ``spans``——那是已经核对过的精确坐标，不再回到原文里搜一遍。
    """
    if not isinstance(rows, list):
        return []
    properties: list[dict] = []
    for draft_index, row in enumerate(rows):
        path = ['properties', draft_index]
        if not isinstance(row, dict):
            _diagnostic(rejected, path, '', '属性行必须是对象')
            continue
        node_name = str(row.get("node") or "").strip()
        property_id = str(row.get("property") or "").strip()
        value_type = str(row.get("value_type") or "string").strip()
        if not node_name or not property_id:
            for field, value in [('node', node_name), ('property', property_id)]:
                if not value:
                    _diagnostic(rejected, path + [field], property_id, f'{field}必须是非空字符串')
            continue
        if any(row.get(key) is not None and not isinstance(row[key], str) for key in ('node', 'holder', 'at_event')):
            _diagnostic(rejected, path + [next(key for key in ('node', 'holder', 'at_event') if row.get(key) is not None and not isinstance(row[key], str))], property_id, '节点引用必须为字符串')
            continue
        if (not isinstance(row.get('related_nodes', []), list)
                or any(not isinstance(value, str) for value in row.get('related_nodes', []))):
            _diagnostic(rejected, path + ['related_nodes'], property_id, 'related_nodes须为节点引用字符串数组')
            continue
        if value_type not in VALUE_TYPES:
            _diagnostic(rejected, path + ['value_type'], f'{node_name}.{property_id}', f'值类型无效：{value_type}')
            continue
        if "cardinality" in row and row["cardinality"] not in ("one", "many"):
            _diagnostic(rejected, path + ['cardinality'], f'{node_name}.{property_id}', 'cardinality必须为one或many')
            continue
        if "symmetric" in row and type(row["symmetric"]) is not bool:
            _diagnostic(rejected, path + ['symmetric'], f'{node_name}.{property_id}', 'symmetric必须是布尔值')
            continue
        quote_entries = [(index, str(item)) for index, item in enumerate(row.get('quotes') or []) if str(item).strip()]
        if quote_entries and not row.get('_draft_generated'):
            # 属性带 quotes 是"多余的参数"：属性以 at_event 指向的事件为出处，逐字证据
            # 记在事件上（`U-09`）。丢掉引文继续走，只记一条 info（`U-10`）。
            quote_entries = []
            if dropped is not None:
                dropped.add('properties', 'quotes', draft_index)
        quotes = [quote for _, quote in quote_entries]
        good = []
        span = (spans or {}).get(draft_index)
        for quote_index, quote in quote_entries:
            hit = _locate(text, quote, locations, lookup, source)
            if hit.status in {"exact", "normalized"}:
                good.append({"quote": hit.quote, "start": hit.start})
                continue
            if row.get("_draft_generated"):
                # 程序生成的行（事件地点快捷字段）证据来自事件本身：引文定位不到时
                # 事件那边已经回了一条 info，这里不再重复报错，也不拒收——这一行
                # 的取值（地点）仍然有用，缺的只是坐标。
                continue
            _diagnostic(rejected, path + ['quotes', quote_index], f'{node_name}.{property_id}', '引文不在指定原文范围中', quote=quote[:60])
        if quotes and not good and not row.get("_draft_generated"):
            _diagnostic(rejected, path + ['quotes'], f'{node_name}.{property_id}', '没有可用的原文引文')
            continue
        if not quotes and span is not None:
            # 程序生成的精确坐标：截取到的就是原文子串，不需要再搜。
            good = [{"quote": text[span[0]:span[1]], "start": span[0]}]
        good.sort(key=lambda item: item["start"])
        op = row.get('op')
        if not isinstance(op, str) or op not in {'set', 'add', 'remove', 'clear', 'unknown'}:
            _diagnostic(rejected, path + ['op'], property_id, '必须明确事件变更op')
            continue
        if ('value' in row) != (op in {'set', 'add', 'remove'}):
            _diagnostic(rejected, path + ['value'], property_id, 'set/add/remove须有value，clear/unknown禁止value')
            continue
        value = row.get('value')
        if op in {'set', 'add', 'remove'} and value_type == 'boolean' and not isinstance(value, list) and type(value) is not bool:
            _diagnostic(rejected, path + ['value'], property_id, '布尔属性必须为true或false')
            continue
        properties.append({
            "_draft_index": draft_index,
            "node": node_name,
            "property": property_id,
            **({"value": value} if op in {"set", "add", "remove"} else {}), "op": op,
            "value_type": value_type,
            **({"cardinality": row["cardinality"]} if "cardinality" in row else {}),
            **({"symmetric": row["symmetric"]} if "symmetric" in row else {}),
            "start": good[0]["start"] if good else None,
            "quotes": [item["quote"] for item in good],
            "scope": str(row.get("scope") or "world").strip(),
            "holder": str(row.get("holder") or "").strip(),
            "at_event": str(row.get("at_event") or "").strip(),
            "source": str(row.get("source") or "").strip(),
            # A display grouping, not a fact. Empty is allowed and means 其他.
            "category": str(row.get("category") or "").strip(),
            "related_nodes": [
                str(item).strip()
                for item in (row.get("related_nodes") or [])
                if str(item).strip()
            ],
        })
    # 没有引文的属性行没有原文坐标可排（evidence 由 at_event 指向事件承担），
    # 稳定排序保序：同批里它们按提交顺序排在所有有坐标的行之后。
    properties.sort(key=lambda item: item["start"] if item["start"] is not None else float("inf"))
    return properties


WRAPPING_QUOTES = "\"'\u2018\u2019\u201c\u201d\u300c\u300d\u300e\u300f"


# 宽容定位只抹**排版差别**，文字一个都不抹（`U-11`）：
# * 全半角与兼容写法按 NFKC 统一（`，`→`,`, `Ａ`→`A`, `１２`→`12`）；
# * 空白与不可见字符（控制符、格式符、零宽、行／段分隔）不参与比对；
# * 标点不参与比对：连续几处折成零处，换一个标点、少一个标点都不算改内容。
# 汉字／字母／数字一个都不动，所以差一个实词（`狠狠地`→`狠地`）仍然定位不到——
# 这就是「宽容」的边界：放宽排版，不放弃逐字校验（写进证据的永远是原文截取的文本）。
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zs", "Zl", "Zp"})
_PUNCTUATION_CATEGORY = "P"


def _fold_character(char: str) -> str:
    """一个字符在比对里的样子：NFKC 展开后去掉空白、不可见字符与标点。"""
    folded = []
    for piece in unicodedata.normalize("NFKC", char):
        category = unicodedata.category(piece)
        if category in _INVISIBLE_CATEGORIES or category.startswith(_PUNCTUATION_CATEGORY):
            continue
        folded.append(piece)
    return "".join(folded)


def _fold_text(text: str) -> str:
    return "".join(_fold_character(char) for char in text)


def _normalized_text(text: str) -> tuple[str, list[int]]:
    """比对骨架与「骨架下标 → 原文下标」映射：命中后一律回原文取逐字文本。"""
    pieces: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        folded = _fold_character(char)
        pieces.append(folded)
        origin.extend([index] * len(folded))
    return "".join(pieces), origin


class _NormalizedSource:
    """一次提交的比对骨架，按需构建：逐字全部命中时根本不建（长章节不白算）。"""

    def __init__(self, text: str):
        self.text = text
        self._built: tuple[str, list[int]] | None = None

    def get(self) -> tuple[str, list[int]]:
        if self._built is None:
            self._built = _normalized_text(self.text)
        return self._built


class _QuoteHit(NamedTuple):
    """一次引文定位的结果。

    * ``exact``：原文逐字命中（含只剥首尾引号的写法）；
    * ``normalized``：逐字不中、归一化后在本批范围**唯一**命中——``start``／``quote``
      已是原文的真实坐标与截取到的逐字文本，不是模型给的那一串；
    * ``ambiguous``：多处命中，``candidates`` 是候选起点（提交内相对坐标），不任取首处；
    * ``missing``：逐字与归一化都不命中（引文里的文字确实对不上原文）。
    """

    status: str
    start: int
    quote: str
    candidates: tuple[int, ...] = ()


def _quote_spellings(quote: str) -> list[str]:
    """先原样、再去掉首尾引号：模型常把整句再套一层引号，多出来的是排版不是内容。"""
    trimmed = quote.strip().strip(WRAPPING_QUOTES).strip()
    if not trimmed or trimmed == quote:
        return [quote]
    return [quote, trimmed]


def _normalized_positions(source: _NormalizedSource, quote: str) -> list[tuple[int, int]]:
    """归一化后的全部命中，回到原文坐标；同一段原文只算一处。"""
    skeleton, origin = source.get()
    needle = _fold_text(quote)
    if not needle:
        return []  # 只有标点／空白的引文没有内容可比，按找不到处理
    found: list[tuple[int, int]] = []
    cursor = 0
    while True:
        at = skeleton.find(needle, cursor)
        if at < 0:
            break
        span = (origin[at], origin[at + len(needle) - 1] + 1)
        if span not in found:
            found.append(span)
        cursor = at + 1
    return found


def _search_quote(text: str, quote: str, lookup=None, source=None, *,
                  unique_exact: bool = False) -> _QuoteHit:
    """宽容定位一次：先逐字，再归一化（`U-11`）。

    ``unique_exact`` 表示「逐字命中也要唯一」——事件用它（同一句在本批出现多处时
    坐标本身就是歧义，不能任取首处当身份依据）；属性行沿用旧口径（取首处）。
    归一化命中**一律**要求唯一：模型给的引文本来就不是原文坐标，没法替它在多处之间选。
    """
    lookup = lookup if lookup is not None else ExactQuoteLookup(text)
    source = source if source is not None else _NormalizedSource(text)
    for spelling in _quote_spellings(quote):
        positions = lookup.positions(spelling, 2 if unique_exact else 1)
        if positions:
            if len(positions) > 1:
                return _QuoteHit("ambiguous", -1, spelling, tuple(positions))
            return _QuoteHit("exact", positions[0], spelling)
    found = _normalized_positions(source, quote)
    if len(found) > 1:
        return _QuoteHit("ambiguous", -1, quote, tuple(start for start, _ in found[:5]))
    if found:
        start, end = found[0]
        return _QuoteHit("normalized", start, text[start:end])
    return _QuoteHit("missing", -1, quote)


def _locate(text: str, quote: str, locations: dict, lookup=None, source=None, *,
            unique_exact: bool = False) -> _QuoteHit:
    key = (quote, unique_exact)
    hit = locations.get(key)
    if hit is None:
        hit = locations[key] = _search_quote(text, quote, lookup, source, unique_exact=unique_exact)
    return hit


def _find_quote(text: str, quote: str, lookup=None) -> tuple[int, str]:
    """找到引文在原文里的位置，返回 (下标, 实际用的引文)。

    先按原样找；找不到就只去掉**首尾的引号**再试一次（模型常把整句再套一层引号）；
    再不行按归一化骨架找**唯一**命中，命中时返回原文截取到的逐字文本与真实下标
    （`U-11`：全半角、空白、不可见字符、标点的差别不算改内容）。多处命中与找不到
    都返回 ``(-1, quote)``——宁可不认，也不指向一个猜出来的位置。
    """
    hit = _search_quote(text, quote, lookup)
    if hit.status in {"exact", "normalized"}:
        return hit.start, hit.quote
    return -1, quote


# 引文定位的提醒按「同一件事」折叠成一条 `info`（与 `U-10` 的丢弃记账同一口径）：
# 一处异常一行会把几十行一样的提示糊满，折叠后 `count` 记处数、`indexes` 指出条目。
_QUOTE_NOTE_INDEX_LIMIT = 12
_QUOTE_NOTE_MESSAGES = {
    "normalized": ("有 {count} 处事件引文与原文只有排版差异（全半角／空白／不可见字符／标点），"
                   "程序已按归一化在本批范围唯一命中，证据改用原文截取的逐字文本与真实坐标"),
    "dropped": ("有 {count} 处事件引文在提交范围里定位不到原文（归一化后也不命中），已丢弃这些引文；"
                "事件照常入库，位置近似取本批范围起点"),
}


class _QuoteNotes:
    """引文定位的两种提醒：近似命中、找不到被丢弃。都只是 `info`，不影响 `valid`。"""

    def __init__(self):
        self._groups: dict[str, dict] = {}
        self._order: list[str] = []

    def add(self, kind: str, index: int):
        group = self._groups.get(kind)
        if group is None:
            group = self._groups[kind] = {"count": 0, "indexes": []}
            self._order.append(kind)
        group["count"] += 1
        if index not in group["indexes"]:
            group["indexes"].append(index)

    def diagnostics(self) -> list[dict]:
        rows = []
        for kind in self._order:
            group = self._groups[kind]
            rows.append({
                "severity": "info", "code": "EXTRACTION_INFO",
                "path": ["nodes"], "field": "quotes", "note": kind,
                "count": group["count"],
                "indexes": group["indexes"][:_QUOTE_NOTE_INDEX_LIMIT],
                "message": _QUOTE_NOTE_MESSAGES[kind].format(count=group["count"])
                           + "；涉及条目见 indexes，其余内容照常入库",
            })
        return rows



def _participant_domain_error(participants: list, rows: list, declared: dict[str, str]):
    """First participant that the draft itself proves is not a person/group.

    Returns ``(index, reference, kind)`` or ``None``. Only the batch is inspected;
    refereces that resolve against the stored graph are judged by reference
    resolution, which can see their type (:mod:`narrative.extraction_refs`).
    """
    for index, value in enumerate(participants):
        reference = str(value).strip()
        kind = _batch_participant_kind(reference, rows, declared)
        if kind and kind not in ALLOWED_PARTICIPANTS:
            return index, reference, kind
    return None


def parse_nodes(payload: dict, text: str, *, node_spans: dict[int, tuple[int, int]] | None = None,
                property_spans: dict[int, tuple[int, int]] | None = None, dropped=None) -> dict:
    """Verify every given quote and pin each node to its first appearance.

    证据是可选的：节点或属性给了引文，每条都要能**定位**到原文；不给就不产生
    证据，也不因此拒收——实体与属性的出处是它参与的事件（``participants``、
    事件处的属性变更、``at_event``）。**事件例外**：它必须给定位依据，``anchor``
    或 ``quotes`` 二选一，``node_spans`` 就是已经核对过的 anchor 坐标。

    定位是程序的事，宽容只放宽**排版**（`U-11`）：逐字不中时按归一化骨架在本批范围
    找唯一命中，命中就用原文截取到的逐字文本与真实坐标（``_search_quote``）。逐字要求
    因此是**对存储结果**的硬要求，不是对模型输入的措辞要求。归一化后多处命中仍拒收
    （给候选起点）；归一化后找不到就丢弃这条引文、事件照常入库，位置取本批范围起点，
    两件事都折叠成一条 `info`（`quote_notices`），不整批拒收。

    非事件节点带的 ``quotes`` 是多余的参数：丢掉引文继续走，只把丢弃记进 ``dropped``
    （`U-10`），不整批拒收——它本来就不该有引文，写多了不代表这一行看不懂。
    """
    rows = payload.get("nodes")
    if not isinstance(rows, list):
        raise ValueError("模型回复缺少 nodes 数组")
    raw_rows = rows
    # 本批声明过的名字 → 类型。同名出现多次说明名称本身证明不了身份，记空串，
    # 由引用解析按歧义处理，不在这里猜。
    declared_types: dict[str, str] = {}
    for draft_row in raw_rows:
        if not isinstance(draft_row, dict):
            continue
        draft_name = str(draft_row.get("name") or "").strip()
        draft_type = str(draft_row.get("type") or "").strip()
        if not draft_name or draft_type not in NODE_TYPES:
            continue
        declared_types[draft_name] = draft_type if draft_name not in declared_types else ""

    # 模型偶尔把属性行写进 `nodes` 里（长章节更容易）：一行有 node/property 没有 type，
    # 那就是属性，不是节点。按**形状**认，不按数组位置认——否则这些行会被当成
    # 「类型无效」丢掉，整章的属性、关系、大事件进展一起消失，还不报错。
    strays: list[dict] = []
    kept_rows: list = []
    for original_index, row in enumerate(rows, 1):
        if (
            isinstance(row, dict)
            and not str(row.get("type") or "").strip()
            and str(row.get("node") or "").strip()
            and str(row.get("property") or "").strip()
        ):
            strays.append(row)
        else:
            kept_rows.append((original_index, row))
    rows = kept_rows

    nodes: list[dict] = []
    locations: dict[tuple[str, bool], _QuoteHit] = {}
    lookup = ExactQuoteLookup(text)
    source = _NormalizedSource(text)
    notes = _QuoteNotes()
    rejected: list[dict] = []
    for original_index, row in rows:
        path = ['nodes', original_index - 1]
        if not isinstance(row, dict):
            _diagnostic(rejected, path, '', '节点行必须是对象')
            continue
        name = str(row.get("name") or "").strip()
        node_type = str(row.get("type") or "").strip()
        if node_type not in NODE_TYPES:
            _diagnostic(rejected, path + ['type'], name, f'类型无效：{node_type}')
            continue
        if (any(row.get(field) is not None and not isinstance(row[field], str) for field in ('scene', 'event', 'big_event', 'storyline'))
                or not isinstance(row.get('participants', []), list)
                or any(not isinstance(value, str) for value in row.get('participants', []))
                or not isinstance(row.get('children', []), list)
                or any(not isinstance(value, str) for value in row.get('children', []))):
            for field in ('scene', 'event', 'big_event', 'storyline', 'participants', 'children'):
                value = row.get(field)
                invalid = (value is not None and not isinstance(value, str)) if field not in ('participants', 'children') else (not isinstance(value if value is not None else [], list) or any(not isinstance(item, str) for item in (value or [])))
                if invalid:
                    _diagnostic(rejected, path + [field], name, '父级、参与者和子项须为正式ID或@索引字符串；数组字段须为字符串数组')
            continue
        if node_type == 'event' and 'participants' not in row:
            _diagnostic(rejected, path + ['participants'], name,
                        '事件必须给出参与者：人物引用或本批 @索引；确无参与者或原文未交代时给空数组')
            continue
        if node_type == 'event':
            offender = _participant_domain_error(row.get('participants') or [], raw_rows, declared_types)
            if offender is not None:
                index, reference, kind = offender
                _diagnostic(rejected, path + ['participants', index], name,
                            f'事件参与者只能是人物或群体：{reference} 是本批的'
                            f'{PARTICIPANT_TYPE_LABELS.get(kind, kind)}节点；地点请用事件自身的地点属性，'
                            f'物品请用持有者属性')
                continue
        is_container = node_type in {'scene', 'big_event', 'storyline'}
        quote_entries = [(index, str(item)) for index, item in enumerate(row.get('quotes') or []) if str(item).strip()]
        quotes = [quote for _, quote in quote_entries]
        if node_type != 'event' and quotes:
            # 同样只丢弃不拒收（`U-10`）：非事件节点以它参与的事件为出处，容器由下级
            # 证据派生，都不提供自身引文；但多写一条引文不代表这一行看不懂。
            quote_entries = []
            quotes = []
            if dropped is not None:
                dropped.add('nodes', 'quotes', original_index - 1)
        if is_container:
            quote_entries = []  # Container evidence comes only from verified children.
        good: list[dict] = []
        span = (node_spans or {}).get(original_index - 1)
        if is_container:
            pass  # 容器证据只从已验证的下级派生，模型给的位置锚点/引文都不算自身证据
        elif span is not None:
            # 位置锚点已核对过范围：截到的就是原文子串，也不必再判「同句多处」——
            # 坐标本身就是消歧。
            good = [{"quote": text[span[0]:span[1]], "start": span[0]}]
        else:
            for quote_index, quote in quote_entries:
                hit = _locate(text, quote, locations, lookup, source, unique_exact=node_type == "event")
                if hit.status == "ambiguous":
                    _diagnostic(rejected, path + ['quotes', quote_index], name,
                                '事件引文在提交范围出现多处，不能任取首处作为精确身份依据；请缩小提交范围或提供更具体的逐字引文',
                                quote=hit.quote, positions_sample=list(hit.candidates),
                                position_basis='submission_relative',
                                # 候选起点已给出（`positions_sample`）；也告诉模型另一条路：
                                # 直接用 `anchor` 写坐标就是消歧，不必再猜哪一处。
                                related_paths=[path + ['anchor']])
                    continue
                if hit.status == "missing":
                    # 逐字与归一化都不命中：丢弃这条引文，事件照常入库（`U-11`）。
                    # 位置在下面按本批范围起点近似，提醒在 `notes` 里折叠成一条 info。
                    notes.add("dropped", original_index - 1)
                    continue
                if hit.status == "normalized":
                    # 只有排版差异：用原文截取到的逐字文本与真实坐标，不发回重来。
                    notes.add("normalized", original_index - 1)
                good.append({"quote": hit.quote, "start": hit.start})
        if not name:
            _diagnostic(rejected, path + ['name'], name, 'name必须是非空字符串')
            continue
        position = good[0]["start"] if good else None
        if node_type == 'event' and not good:
            if quotes:
                # 宽容定位也找不到：引文丢掉、事件入库，位置取本批范围起点（`U-11`）。
                # 逐字要求针对**存储结果**（这里已经没有引文可存），不是要模型重写这一段。
                position = 0
            else:
                _diagnostic(rejected, path + ['anchor'], name,
                            '事件必须给定位依据：anchor{start,end}（推荐）或非空quotes逐字引文，二者至少一个')
                continue
        if row.get("existing_id") is not None and not isinstance(row["existing_id"], str):
            _diagnostic(rejected, path + ['existing_id'], name, 'existing_id须为节点ID字符串')
            continue
        if row.get("new_occurrence") is not None and type(row["new_occurrence"]) is not bool:
            _diagnostic(rejected, path + ['new_occurrence'], name, 'new_occurrence须为布尔值')
            continue
        good.sort(key=lambda item: item["start"])
        participants = [
            str(item).strip() for item in (row.get("participants") or []) if str(item).strip()
        ]
        nodes.append({
            "name": name,
            "reference": f"@{original_index}",
            "type": node_type,
            # 没有定位依据的节点不冒称一个原文坐标：start 为 None，交由批次起点定序。
            # 引文被丢弃的事件有一个近似位置（本批范围起点），那是「位置」不是「证据」。
            "start": position,
            "quotes": [item["quote"] for item in good],
            "evidence_spans": [{"start": item["start"], "end": item["start"] + len(item["quote"]),
                                "quote": item["quote"]} for item in good],
            "existing_id": str(row.get("existing_id") or "").strip(),
            "new_occurrence": row.get("new_occurrence") is True,
            "context_id": str(row.get("context_id") or "canon"),
            "aliases": [
                str(item).strip() for item in (row.get("aliases") or []) if str(item).strip()
            ],
            "participants": participants,
            "children": list(row.get("children") or []),
            "participants_not_in_text": [item for item in participants if item not in text],
            "scene": str(row.get("scene") or "").strip(),
            "event": str(row.get("event") or "").strip(),
            # 场景归属的大事件（可选）：大事件跨场景、甚至跨章。
            "storyline": str(row.get("storyline") or "").strip(),
            "big_event": str(row.get("big_event") or "").strip(),
        })

    # Reading order inside each type, then grouped for display. 无坐标的节点按名称
    # 排在最前（start 为 None），同一个批次内顺序确定，不随提交顺序漂移。
    nodes.sort(key=lambda item: (item["start"] is not None, item["start"] or 0, item["name"]))

    grouped: dict[str, list[dict]] = {node_type: [] for node_type in NODE_TYPES}
    for node in nodes:
        grouped[node["type"]].append(node)
    declared = payload.get("properties")
    declared = declared if isinstance(declared, list) else []
    if strays:
        declared = declared + strays
    properties = _parse_properties(declared, text, {item["name"] for item in nodes}, rejected, locations, lookup,
                                  dropped=dropped, source=source)
    scenes = {item["name"] for item in grouped["scene"]}
    for event in grouped["event"]:
        if event["scene"] and event["scene"] not in scenes:
            event["scene_not_found"] = True
    containers = {kind: {item["name"] for item in grouped[kind]} for kind in ("storyline", "big_event")}
    for child_type, parent_type in (("scene", "big_event"), ("big_event", "storyline")):
        for child in grouped[child_type]:
            if child[parent_type] and child[parent_type] not in containers[parent_type]:
                child[parent_type + "_not_found"] = True

    return {
        "nodes": nodes,
        "properties": properties,
        "grouped": grouped,
        "counts": {node_type: len(items) for node_type, items in grouped.items()},
        "rejected": rejected,
        # 引文定位的 info 提醒（近似命中／丢弃）：由调用方并进诊断，不影响 valid（`U-11`）。
        "quote_notices": notes.diagnostics(),
    }












def _checkpoint_at(start: int, checkpoints: list[tuple[int, int, str]]) -> str | None:
    """The event whose reading segment contains this position.

    Prefer the event whose quote covers the position; otherwise the nearest one
    *before* it, because the information appears in that event's segment; before
    the chapter's first event it belongs to the opening segment. Text after the
    last event hangs on that last event rather than being dropped.

    The result is a reading anchor. It says nothing about when the value holds in
    the story — a value learned in a flashback is disclosed here, not there.
    """
    if not checkpoints:
        return None
    for begin, end, event_id in checkpoints:
        if begin <= start <= end:
            return event_id
    preceding = [item for item in checkpoints if item[0] <= start]
    if preceding:
        return max(preceding, key=lambda item: item[0])[2]
    return min(checkpoints, key=lambda item: item[0])[2]


def _anchor_for(reference: str, event_by_name: dict[str, str]) -> tuple[str | None, str]:
    anchor = event_by_name.get(reference)
    return anchor, "explicit" if anchor else ""



def _node_spans(item: dict, source_id: str) -> list[dict]:
    return [{"source_id": source_id, **span} for span in item.get("evidence_spans") or []]


def _mention_key(item: dict) -> tuple:
    """A node mention's identity key: type + name + its first verified quote.

    只有事件一定带引文；实体与容器不填（容器另有派生证据），所以这里必须容忍
    空的引文列表——没有引文时键退化为「类型 + 名称」，由 batch 内的引用解析负责。
    """
    return (item["type"], item["name"], item["quotes"][0] if item["quotes"] else "")


def _span_key(span: dict) -> tuple:
    return (span.get("source_id"), span.get("start"), span.get("end"), span.get("quote"))


def _participants(item: dict, by_name: dict, ids: dict, existing: dict) -> list[str]:
    resolved = []
    for name in item.get("participants") or []:
        target = None
        candidate = by_name.get(name)
        if candidate and candidate["type"] in ALLOWED_PARTICIPANTS:
            target = ids.get((candidate["type"], name))
        if target is None:
            target = next((existing[(kind, name)] for kind in ALLOWED_PARTICIPANTS
                           if (kind, name) in existing), None)
        if target and target not in resolved:
            resolved.append(target)
    return resolved


def _event_reuse_candidates(item: dict, snapshot: dict, source_id: str,
                            by_name: dict, ids: dict, existing: dict) -> tuple[list[dict], list[dict]]:
    named = [node for node in snapshot["nodes"] if node["node_type"] == item["type"]
             and node.get("context_id", "canon") == item.get("context_id", "canon")
             and (item.get("display_name", item["name"]) == node["name"]
                  or item.get("display_name", item["name"]) in (node.get("aliases") or []))]
    parent_field = "event" if item["type"] == "action" else "scene"
    scene_name = item.get(parent_field)
    scene_id = item.get("resolved_" + parent_field + "_id")
    scene = next((node for node in snapshot["nodes"] if node["id"] == scene_id), {})
    if item["type"] == "action":
        named = [node for node in named if node["id"] in (scene.get("events") or [])]
    participants = set(_participants(item, by_name, ids, existing))
    # 事件必须给 participants：空数组表示「确无参与者」，同样要参与比对；
    # action 仍按旧规则只在显式给出时比对。
    compare_participants = item["type"] == "event" or bool(item.get("participants"))
    spans = {_span_key(span) for span in _node_spans(item, source_id)}
    proven = []
    for candidate in named:
        if scene_name and candidate["id"] not in (scene.get("events") or []):
            continue
        if compare_participants and set(candidate.get("participants") or []) != participants:
            continue
        if (candidate.get("disclosure") or {}).get("source_id") != source_id:
            continue
        recorded = list(candidate.get("evidence_spans") or [])
        # Match the recorded primary evidence exactly. Do not read history
        # or invent other evidence that the node did not retain.
        primary = candidate.get("evidence") or {}
        start = (candidate.get("disclosure") or {}).get("start")
        if isinstance(start, int) and isinstance(primary.get("quote"), str):
            recorded.append({"source_id": primary.get("source_id"), "start": start,
                             "end": start + len(primary["quote"]), "quote": primary["quote"]})
        first_quote = item["quotes"][0] if item["quotes"] else ""
        exact_primary = bool(
            first_quote and primary.get("quote") == first_quote
            and primary.get("source_id") == source_id and start == item["start"]
        )
        # Strict primary-quote identity stays valid when a correction omits the
        # scene — and, for actions, the participants: omitted structure is not a
        # claim that the node had none. Additional evidence proves identity only
        # with the full scene/participant checks.
        # ``participants`` no longer needs an "was it given?" test here:
        # parse_nodes fills the list on every row (events must give it, actions
        # omitted get []), so "omitted" and "explicitly empty" are the same
        # statement by the time a candidate is judged.
        complete_structure = bool(scene_id
                                  and set(candidate.get("participants") or []) == participants)
        if exact_primary or (complete_structure and spans.intersection(_span_key(span) for span in recorded)):
            proven.append(candidate)
    return named, proven


def observation_key(row: dict) -> str:
    """Full fact/provenance identity; readable row IDs do not encode semantics."""
    boundary = row.get("disclosure") or {}
    evidence = row.get("evidence") or {}
    fields = {
        "node": row.get("node_id"), "property": row.get("property_id"), "value": row.get("value"),
        "scope": row.get("scope") or "world", "holder": row.get("holder_id"),
        "event": row.get("event_id"), "op": row.get("op"), "sequence": row.get("sequence"),
        "source_id": boundary.get("source_id"), "start": boundary.get("start"),
        "quote": evidence.get("quote"), "evidence_source": evidence.get("source_id"),
    }
    return json.dumps(fields, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _entity_event_anchors(objects, nodes_by_id, changes, definitions, action_parents,
                          orders, fallback_ordinal) -> None:
    """实体的阅读位置 = 它最早挂靠的那个事件的位置。

    事件是唯一自带原文位置的对象：实体（人／群体／地点／物品／动作）不填引文，
    它的出处就是它参与的事件。这里只补「自己没有任何证据、也不是事件或容器」的
    节点——取最早挂靠的事件（先看是哪种挂靠，再看事件链正式顺序与 Unicode 起点），
    把那个事件的 ``evidence``／``disclosure`` 抄过来，形状与模型亲自给引文时一致，
    所以阅读位置查询、浏览排序与「定位原文」都不需要另一套语义。找不到落点的
    实体保持没有位置，由静态检查的 ``entity_unanchored`` 报出来。
    """
    from .event_location import LOCATION_PROPERTIES, LOCATION_SEMANTICS

    def point(event_id):
        disclosure = (nodes_by_id.get(event_id) or {}).get("disclosure") or {}
        start = disclosure.get("start")
        if not isinstance(start, int):
            return None
        return (orders.get(disclosure.get("source_id"), fallback_ordinal), start)

    def anchored_at(identifier):
        """动作不填引文：它和它的事件同点，所以挂靠位置要落到那个事件上。"""
        return action_parents.get(identifier, identifier)

    location_properties = set(LOCATION_PROPERTIES) | {
        key for key, row in definitions.items() if row.get("semantic") in LOCATION_SEMANTICS}
    holder_properties = {key for key, row in definitions.items() if row.get("semantic") == "item_holder"} or {"持有者"}

    participants: dict[str, list[str]] = {}
    for identifier, node in nodes_by_id.items():
        if node.get("node_type") not in {"event", "action"} or node.get("retracted"):
            continue
        for member in node.get("participants") or []:
            if isinstance(member, str):
                participants.setdefault(member, []).append(anchored_at(identifier))

    location_at: dict[str, list[str]] = {}
    possession_at: dict[str, list[str]] = {}
    referenced_at: dict[str, list[str]] = {}
    for row in changes:
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or row.get("scope", "world") != "world" or row.get("holder_id"):
            continue
        if row.get("op") not in {"set", "add"}:
            continue
        event_id = anchored_at(event_id)
        value = row.get("value")
        targets = [item for item in (value if isinstance(value, list) else [value]) if isinstance(item, str)]
        targets += [item for item in row.get("related_nodes") or [] if isinstance(item, str)]
        for target in targets:
            referenced_at.setdefault(target, []).append(event_id)
            if row.get("node_id") == event_id and row.get("property_id") in location_properties:
                location_at.setdefault(target, []).append(event_id)
            if row.get("property_id") in holder_properties:
                possession_at.setdefault(target, []).append(event_id)
        if isinstance(row.get("node_id"), str):
            # 属性主体自己也是「在事件处被引用」：物品的持有状态、人物的身份都在事件处写。
            referenced_at.setdefault(row["node_id"], []).append(event_id)

    def earliest(event_ids):
        ranked = [entry for entry in ((point(event_id), event_id) for event_id in event_ids)
                  if entry[0] is not None]
        return min(ranked)[1] if ranked else None

    for obj in objects:
        if obj.get("kind") != "node" or obj.get("node_type") in {"event", "scene", "big_event", "storyline"}:
            continue
        if obj.get("evidence"):
            continue
        identifier, kind = obj["id"], obj["node_type"]
        if kind in {"person", "group"}:
            candidates = participants.get(identifier, [])
        elif kind == "location":
            candidates = location_at.get(identifier) or referenced_at.get(identifier, [])
        elif kind == "item":
            candidates = possession_at.get(identifier) or referenced_at.get(identifier, [])
        elif kind == "action":
            candidates = [action_parents[identifier]] if identifier in action_parents else []
        else:
            continue
        context = obj.get("context_id", "canon")
        candidates = [event_id for event_id in candidates
                      if (nodes_by_id.get(event_id) or {}).get("context_id", "canon") == context]
        event_id = earliest(candidates)
        if event_id is None:
            continue
        source = nodes_by_id.get(event_id) or {}
        evidence, disclosure = source.get("evidence") or {}, source.get("disclosure") or {}
        quote, start = evidence.get("quote"), disclosure.get("start")
        if not isinstance(quote, str) or not quote or not isinstance(start, int):
            continue
        obj["evidence"] = {"source_id": evidence.get("source_id"), "quote": quote}
        obj["disclosure"] = {"source_id": disclosure.get("source_id"), "start": start}
        obj["evidence_spans"] = [{"source_id": evidence.get("source_id"), "start": start,
                                  "end": start + len(quote), "quote": quote}]


def build_extraction(ops, data: dict) -> dict:
    """Turn one extraction result into graph objects + property records.

    Two orders are kept apart. The disclosure chain is derived from source
    positions and never stored. ``predecessors``/``successors`` hold only the
    relations the source states, so a missing edge means "not established yet",
    not "comes next in the text". Property values are anchored to the disclosure
    checkpoint where the reader learns them.
    """
    snapshot = ops.graph_snapshot()
    skipped: list[dict] = []
    # Notices that never block the batch: the write is dropped, everything else
    # is still stored. They travel back with the preflight result.
    diagnostics: list[dict] = []
    snapshot_nodes = {node["id"]: node for node in snapshot["nodes"]}
    # Ids are disclosure positions, so a new mention needs to know where it sits
    # relative to the nodes that already exist.
    orders = {
        row["source_id"]: row["ordinal"] for row in snapshot.get("source_orders", []) or []
    }
    current_ordinal = orders.get(data["source_id"])
    if current_ordinal is None:
        current_ordinal = max((int(value) for value in orders.values()), default=-1) + 1
    # 每种节点各有自己的编号序列：人从 P1 开始，物品从 I1 开始，互不相关。
    # 所以位置表和占用表都按类型分开——两个类型共用一本流水号就不是两套编号了。
    positions: dict[str, list[tuple[tuple[int, int], str]]] = {}
    taken: dict[str, set[str]] = {}
    for node in snapshot["nodes"]:
        node_type = str(node.get("node_type") or "")
        taken.setdefault(node_type, set()).add(node["id"])
        disclosure = node.get("disclosure")
        if isinstance(disclosure, dict) and isinstance(disclosure.get("start"), int):
            positions.setdefault(node_type, []).append((
                (int(orders.get(disclosure.get("source_id"), 99)), disclosure["start"]),
                node["id"],
            ))

    existing: dict[tuple[str, str], str] = {}
    name_counts: dict[tuple[str, str], int] = {}
    for node in snapshot["nodes"]:
        key = (node["node_type"], node["name"])
        existing.setdefault(key, node["id"])
        name_counts[key] = name_counts.get(key, 0) + 1

    existing_by_name = dict(existing)
    for node in snapshot['nodes']:
        existing[(node['node_type'], node['id'])] = node['id']
    # A shared name is not an identity. Entries collapse only when the type, the
    # name *and* the sentence all match — that is the model repeating itself,
    # not two different deeds that happen to be called the same thing.
    unique: dict[tuple[str, str, str], dict] = {}
    for item in data["nodes"]:
        key = _mention_key(item)
        if key in unique:
            merged = unique[key]
            identity_fields = ("existing_id", "new_occurrence", "context_id", "scene", "event")
            if (any(item.get(field) != merged.get(field) for field in identity_fields)
                    or set(item.get("participants") or []) != set(merged.get("participants") or [])):
                skipped.append({"name": item["name"], "reason": "同一首引文的节点声明了不同身份或参与结构，请先消除歧义"})
            for extra in item["quotes"]:
                if extra not in merged["quotes"]:
                    merged["quotes"].append(extra)
            span_keys = {_span_key(span) for span in merged.get("evidence_spans") or []}
            for span in item.get("evidence_spans") or []:
                if _span_key(span) not in span_keys:
                    merged.setdefault("evidence_spans", []).append(span)
                    span_keys.add(_span_key(span))
            continue
        unique[key] = dict(item)
    nodes = []
    for item in unique.values():
        explicit = item.get("existing_id")
        target = snapshot_nodes.get(explicit) if explicit else None
        if explicit and (target is None
                         or target.get("node_type") != item["type"]
                         or target.get("context_id", "canon") != item.get("context_id", "canon")
                         or item.get("new_occurrence")):
            skipped.append({"name": item["name"], "existing_id": explicit,
                            "reason": "existing_id须指向本工程存在且类型、语境一致的节点，不能同时声明new_occurrence"})
            continue
        nodes.append(item)
    by_name: dict[str, dict] = {}
    for item in nodes:
        by_name.setdefault(item["name"], item)
    ids: dict[tuple[str, str], str] = {}
    # Two mentions can share a name; the sentence is what keeps them apart, so
    # each mention gets its own id while references still resolve by name.
    mention_ids: dict[tuple[str, str, str], str] = {}
    objects: list[dict] = []

    event_items = [item for item in nodes if item["type"] == "event"]
    event_items.sort(key=lambda item: (item["start"] is not None, item["start"] or 0))
    action_items = sorted((item for item in nodes if item["type"] == "action"),
                          key=lambda item: (item["start"] is not None, item["start"] or 0))

    # Resolve every id first: an event may reference a node that comes later.
    # Entities keep their identity across chapters; events do not, because two
    # scenes may legitimately narrate two different deeds under one name.
    # One extraction record is identified by what it says *and* where it says it:
    # the same sentence can legitimately appear twice in a book.
    known_by_quote = {
        (
            item["node_type"], item["name"],
            (item.get("evidence") or {}).get("quote"),
            (item.get("disclosure") or {}).get("source_id"),
            (item.get("disclosure") or {}).get("start"), item.get("context_id", "canon"),
        ): item["id"]
        for item in snapshot["nodes"]
    }
    allocation_order = [item for item in nodes if item["type"] not in {"event", "action"}] + event_items + action_items
    for item in allocation_order:
        key = (item["type"], item["name"])
        quote = item["quotes"][0] if item["quotes"] else ""
        mention = _mention_key(item)
        record_key = (item["type"], item.get("display_name", item["name"]), quote, item.get("evidence_source_id", data["source_id"]), item["start"], item.get("context_id", "canon"))
        if item.get("existing_id"):
            mention_ids[mention] = item["existing_id"]
            ids.setdefault(key, mention_ids[mention])
            continue
        if item["type"] in {"event", "action"}:
            from .containment_refs import resolve_parent

            parent_kind = "event" if item["type"] == "action" else "scene"
            item["resolved_" + parent_kind + "_id"] = resolve_parent(
                item.get(parent_kind) or "", parent_kind, item.get("context_id", "canon"),
                snapshot, nodes, mention_ids, skipped, item["name"],
            )
            named, proven = _event_reuse_candidates(item, snapshot, data["source_id"], by_name, ids, existing)
            if proven and item.get("new_occurrence"):
                skipped.append({"name": item["name"], "existing_ids": [node["id"] for node in proven],
                                "reason": "new_occurrence与同一位置的事件证据冲突；请查证后引用existing_id或提供另一次发生的证据"})
            elif len(proven) == 1:
                mention_ids[mention] = proven[0]["id"]
                ids.setdefault(key, mention_ids[mention])
                continue
            if len(proven) > 1 or (named and not item.get("new_occurrence")):
                skipped.append({"name": item["name"], "existing_ids": [node["id"] for node in named],
                                "reason": "同名事件身份不能唯一确认；查证后用existing_id引用已有事件，或用new_occurrence声明原文中的另一次发生"})
        if item["type"] not in {"event", "action"} and record_key in known_by_quote:
            mention_ids[mention] = known_by_quote[record_key]
            ids.setdefault(key, mention_ids[mention])
            continue
        # Reuse an entity only when the project holds exactly one node of that
        # name and type. Once there are several, the name proves nothing, so the
        # new mention stays separate until someone confirms the identity.
        reused = (
            existing_by_name.get((item["type"], item.get("display_name", item["name"])))
            if item["type"] not in {"event", "action"}
            and name_counts.get((item["type"], item.get("display_name", item["name"])), 0) == 1
            else None
        )
        if reused:
            mention_ids[mention] = reused
            ids.setdefault(key, reused)
            continue
        position = (orders.get(item.get("evidence_source_id", data["source_id"]), current_ordinal),
                    item["start"] if isinstance(item["start"], int) else 0)
        node_type = str(item["type"])
        node_id = allocate(
            positions.setdefault(node_type, []),
            position,
            taken.setdefault(node_type, set()),
            prefix_for(node_type),
        )
        taken[node_type].add(node_id)
        positions[node_type].append((position, node_id))
        mention_ids[mention] = node_id
        ids.setdefault(key, node_id)

    from .containment_refs import resolve_parent

    # Typed parent IDs are the common identity for old and new containers.
    scene_children: dict[str, list[str]] = {}
    for event in event_items:
        if "resolved_scene_id" not in event:
            event["resolved_scene_id"] = resolve_parent(
                event.get("scene") or "", "scene", event.get("context_id", "canon"),
                snapshot, nodes, mention_ids, skipped, event["name"],
            )
        parent = event.get("resolved_scene_id")
        if parent:
            scene_children.setdefault(parent, []).append(mention_ids[_mention_key(event)])
    action_children: dict[str, list[str]] = {}
    for action in action_items:
        parent = action.get("resolved_event_id")
        if not parent:
            parent = resolve_parent(action.get("event") or "", "event", action.get("context_id", "canon"),
                                    snapshot, nodes, mention_ids, skipped, action["name"])
        if parent:
            action_children.setdefault(parent, []).append(mention_ids[_mention_key(action)])
    big_event_children: dict[str, list[str]] = {}
    storyline_children: dict[str, list[str]] = {}
    for child_type, parent_type, children in (("scene", "big_event", big_event_children),
                                               ("big_event", "storyline", storyline_children)):
        for child in nodes:
            if child["type"] != child_type:
                continue
            parent = resolve_parent(child.get(parent_type) or "", parent_type, child.get("context_id", "canon"),
                                    snapshot, nodes, mention_ids, skipped, child["name"])
            if parent:
                children.setdefault(parent, []).append(mention_ids[_mention_key(child)])
    children_by_type = {"scene": scene_children, "big_event": big_event_children,
                        "storyline": storyline_children, "event": action_children}
    direct_kind = {"storyline":"big_event", "big_event":"scene", "scene":"event", "event":"action"}
    for item in nodes:
        parent_kind = item["type"]
        for reference in item.get("children") or []:
            if parent_kind not in direct_kind:
                skipped.append({"name": item["name"], "reason": "只有五层容器节点可声明children"})
                continue
            child_kind = direct_kind[parent_kind]
            if parent_kind == "scene" and snapshot_nodes.get(reference, {}).get("node_type") == "scene":
                child_kind = "scene"
            if parent_kind == "scene" and any(row.get("name") == reference and row.get("type") == "scene" for row in nodes):
                child_kind = "scene"
            child_id = resolve_parent(reference, child_kind, item.get("context_id", "canon"),
                                      snapshot, nodes, mention_ids, skipped, item["name"])
            if child_id:
                parent_id = mention_ids[_mention_key(item)]
                children_by_type[parent_kind].setdefault(parent_id, []).append(child_id)
    for children_map in children_by_type.values():
        for parent_id, children in children_map.items():
            children = list(dict.fromkeys(children))
            children_map[parent_id] = children
            parent = snapshot_nodes.get(parent_id)
            if parent is None:
                continue
            merged_children = list(dict.fromkeys([*(parent.get("events") or []), *children]))
            if merged_children != list(parent.get("events") or []):
                objects.append(dict(parent) | {"events": merged_children})
            children_map[parent_id] = cast(list[str], merged_children)

    existing_ids = {item["id"] for item in snapshot["nodes"]}
    for item in nodes:
        key = (item["type"], item["name"])
        node_id = mention_ids[_mention_key(item)]
        if node_id in existing_ids:
            old = snapshot_nodes[node_id]
            staged_index = next((index for index, obj in enumerate(objects) if obj["id"] == node_id), None)
            current = objects[staged_index] if staged_index is not None else old
            updated = dict(current)
            spans = list(current.get("evidence_spans") or [])
            keys = {_span_key(span) for span in spans}
            for span in _node_spans(item, data["source_id"]):
                if _span_key(span) not in keys:
                    spans.append(span)
                    keys.add(_span_key(span))
            if spans:
                updated["evidence_spans"] = spans
            if item.get("derived_container"):
                updated["evidence_spans"] = _node_spans(item, data["source_id"])
                updated["disclosure"] = {"source_id": item["evidence_source_id"], "start": item["start"]}
                updated["evidence"] = {"source_id": item["evidence_source_id"], "quote": item["quotes"][0]}
                if item.get("derived_completion"):
                    updated["completion"] = dict(item["derived_completion"])
            children = children_by_type.get(item["type"], {}).get(node_id, [])
            if children:
                updated["events"] = list(dict.fromkeys([*(current.get("events") or []), *children]))
            if updated != current:
                if staged_index is None:
                    objects.append(updated)
                else:
                    objects[staged_index] = updated
            continue
        payload = {
            "kind": "node",
            "id": node_id,
            "node_type": item["type"],
            "name": item.get("display_name", item["name"]),
            "aliases": item.get("aliases") or [],
            "context_id": item.get("context_id", "canon"),
            "identity_note": "原文提取",
        }
        # 只有事件自带原文位置；实体与容器的 evidence/disclosure 由程序派生
        # （容器从下级证据，实体从最早挂靠的事件），不在这里凭空造一个。
        if item["quotes"]:
            payload["disclosure"] = {"source_id": item.get("evidence_source_id", data["source_id"]), "start": item["start"]}
            payload["evidence"] = {"source_id": item.get("evidence_source_id", data["source_id"]), "quote": item["quotes"][0]}
            payload["evidence_spans"] = _node_spans(item, data["source_id"])
        elif item["type"] == "event" and isinstance(item["start"], int):
            # 引文被丢弃的事件（`U-11`）仍要在时间轴上有个位置：披露点取本批范围起点。
            # 只给位置、不造引文——没有逐字证据就不冒称有，静态检查按「没有证据」看它。
            payload["disclosure"] = {"source_id": item.get("evidence_source_id", data["source_id"]), "start": item["start"]}
        if item.get("derived_completion"):
            payload["completion"] = dict(item["derived_completion"])
        if item["type"] in children_by_type:
            payload["events"] = children_by_type[item["type"]].get(node_id, [])
        if item["type"] in {"event", "action"}:
            payload["participants"] = _participants(item, by_name, ids, existing)
        if item["type"] == "event" and action_children.get(node_id):
            payload["events"] = action_children[node_id]
        objects.append(payload)

    # A source needs a reading position before disclosure queries can order it.
    registered_sources = {
        item["source_id"] for item in snapshot.get("source_orders", []) or []
    }
    if data["source_id"] not in registered_sources:
        ordinal = max(
            (int(item.get("ordinal", 0)) for item in snapshot.get("source_orders", []) or []),
            default=0,
        ) + 1
        objects.append({
            "kind": "source_order",
            "id": f"order_{data['source_id'][:12]}",
            "source_id": data["source_id"],
            "ordinal": ordinal,
            "context_id": "canon",
        })

    # Disclosure checkpoints: the event a reader has just passed when a value
    # becomes visible. This is a reading position, not a moment of change — the
    # same field must never mean both.
    event_by_name: dict[str, str] = {}
    checkpoint_order: list[tuple[int, int, str]] = []
    event_starts: dict[str, int] = {}
    for event in event_items + action_items:
        quote = event["quotes"][0] if event["quotes"] else ""
        event_id = mention_ids[_mention_key(event)]
        event_by_name.setdefault(event["name"], event_id)
        start = event["start"]
        if not isinstance(start, int):
            # 事件一定自带位置；不填引文的动作取直属事件的位置——动作在时间轴上
            # 没有自己的点，它就是那个事件的一次实施。
            parent = str(event.get("resolved_event_id") or "")
            start = event_starts.get(parent)
            if start is None:
                point = (snapshot_nodes.get(parent) or {}).get("disclosure") or {}
                start = point.get("start") if isinstance(point.get("start"), int) else 0
        event_starts[event_id] = start
        checkpoint_order.append((start, start + len(quote), event_id))

    # Properties are one sequence of (value, checkpoint). A value is visible from
    # the checkpoint the reader reaches it at; before that it is simply unknown.
    # It does not follow that the checkpoint caused the value, or that the value
    # began to hold in the story world at that moment.
    accepted_records = ops.property_records()
    known_values = {observation_key(row): row for row in accepted_records if row.get("kind") == "property_change"}
    reserved_ids = {row["id"] for row in accepted_records} | {prop["property"] for prop in data.get("properties", [])}
    records: list[dict] = []
    property_origins: dict[str, list[int]] = {}

    for node in snapshot['nodes']:
        if node['node_type'] in {'event', 'action'} and not node.get('retracted'):
            event_by_name[node['id']] = node['id']
    for (kind, _ref), identifier in ids.items():
        if kind in {'event', 'action'}:
            event_by_name[identifier] = identifier
    definition_records = {row["id"]: row for row in accepted_records
                          if row.get("kind") == "property_definition"}
    symmetric_flags = {}
    for prop in data.get("properties", []):
        if "symmetric" not in prop:
            continue
        name = prop["property"]
        if name in symmetric_flags and symmetric_flags[name] != prop["symmetric"]:
            skipped.append({"name": name, "reason": "同一属性定义的symmetric声明冲突"})
        symmetric_flags[name] = prop["symmetric"]
    cardinalities = {}
    for prop in data.get("properties", []):
        if "cardinality" not in prop:
            continue
        name, proposed = prop["property"], prop["cardinality"]
        if name in cardinalities and cardinalities[name] != proposed:
            skipped.append({"name": name, "reason": "同一属性定义的cardinality声明冲突"})
        cardinalities[name] = proposed
        if name in definition_records and definition_records[name].get("cardinality", "one") != proposed:
            skipped.append({"name": name, "reason": "cardinality与已有定义冲突；请显式修改属性定义"})
    definition_updates = set()
    definitions: dict[str, str] = {
        str(item["id"]): str(item.get("value_type") or "string")
        for item in accepted_records
        if item["kind"] == "property_definition"
    }
    # 同一批里同一个方面可能既有 node 又有 either：either 只是允许「无 / 未知」这类普通值，
    # 两者是同一类。整批先定下类型，免得先出现的 node 把后面的 either 顶掉。
    batch_types: dict[str, str] = {}
    for prop in data.get("properties", []):
        name = str(prop["property"])
        if batch_types.get(name) != "either":
            batch_types[name] = prop["value_type"]

    current_nodes = snapshot_nodes | {row["id"]:row for row in objects if row.get("kind") == "node"}
    progress_owners = {STORYLINE_PROGRESS:"big_event", BIG_EVENT_PROGRESS:"scene"}
    def last_leaf(identifier):
        seen, pending, leaves = set(), [identifier], []
        while pending:
            here = pending.pop()
            if here in seen:
                continue
            seen.add(here)
            node = current_nodes.get(here, {})
            if node.get("events"):
                pending.extend(node["events"])
            elif node.get("node_type") in {"event", "action"}:
                leaves.append(here)
        def position(identifier):
            point = current_nodes[identifier].get("disclosure") or {}
            return orders.get(point.get("source_id"), current_ordinal), point.get("start", -1)
        return max(leaves,key=position) if leaves else None

    # 属性的顺序序号：带引文的行仍用它定位到的原文起点；不带引文的行按
    # 「at_event 的事件起点 + 同事件内的提交顺序」追加（并避开已被占用的序号）。
    # 同一条事实在同一事件点重复出现是同一件事：它们共用同一个序号，因此仍然按
    # observation_key 折叠成一条记录，而不是因为序号不同每条都存一遍。
    quoted_sequences: dict[str, set[int]] = {}
    used_sequences: dict[str, set[int]] = {}
    assigned_sequences: dict[str, dict[str, int]] = {}
    for prop in data.get("properties", []):
        if isinstance(prop.get("start"), int) and prop.get("quotes"):
            quoted_sequences.setdefault(event_by_name.get(prop.get("at_event") or "") or "", set()).add(prop["start"])

    for prop in data.get("properties", []):
        node_id = ids.get((prop["node_type"], prop["node"])) if "node_type" in prop else None
        if node_id is None:
            for kind in NODE_TYPES:
                node_id = ids.get((kind, prop["node"]))
                if node_id:
                    break
        if node_id is None:
            for kind in NODE_TYPES:
                node_id = existing.get((kind, prop["node"]))
                if node_id:
                    break
        expected_owner = progress_owners.get(str(prop.get("property") or ""))
        if expected_owner and current_nodes.get(node_id or "", {}).get("node_type") != expected_owner:
            skipped.append({"name":prop["property"], "reason":f"该进展属性须直接记录在{expected_owner}，不自动迁移旧层级"})
            continue
        if node_id is None:
            continue
        property_id = str(prop["property"])
        proposed_type = prop["value_type"]
        known_type = definitions.get(property_id)
        if known_type is None:
            known_type = str(batch_types.get(property_id) or proposed_type)
            definitions[property_id] = known_type
            records.append({
                "kind": "property_definition", "id": property_id,
                "value_type": known_type, "cardinality": cardinalities.get(property_id, "one"),
                "symmetric": symmetric_flags.get(property_id, False),
            })
        elif known_type != proposed_type and {known_type, proposed_type} != {"node", "either"}:
            continue
        if (property_id in definition_records and property_id in symmetric_flags
                and property_id not in definition_updates
                and definition_records[property_id].get("symmetric", False) != symmetric_flags[property_id]):
            records.append(definition_records[property_id] | {"symmetric": symmetric_flags[property_id]})
            definition_updates.add(property_id)
        value_type = known_type
        value = prop.get("value")
        if "value" in prop and value_type in {"node", "either"}:
            resolved_values = []
            values = value if isinstance(value, list) else [value]
            unresolved = False
            for item in values:
                target = None
                if isinstance(item, str):
                    for kind in NODE_TYPES:
                        target = ids.get((kind, item)) or existing.get((kind, item))
                        if target:
                            break
                if target is None and value_type == "node":
                    unresolved = True
                    break
                resolved_values.append(target if target is not None else item)
            if unresolved:
                skipped.append({"name": property_id, "reason": "节点引用未能解析"})
                continue
            value = resolved_values if isinstance(value, list) else resolved_values[0]
        anchor = event_by_name.get(prop.get("at_event") or "")
        if prop.get('at_event') and anchor is None:
            skipped.append({'name': property_id, 'reference': prop['at_event'], 'reason': '显式披露锚点未能解析，不使用临近事件代替'})
            continue
        if not anchor:
            # Nothing in this chapter covers the position. Anchoring to a made-up
            # start would make the value visible before the reader learns it, so
            # the value is reported instead of silently dropped.
            skipped.append({
                "name": f"{prop['node']}.{property_id}",
                "reason": "没有可锚定的披露事件：本章事件未覆盖该位置",
            })
            continue
        quote = prop["quotes"][0] if prop["quotes"] else ""
        # 属性不填引文：它的出处就是 at_event 那个事件。顺序规则因此只能是确定的，
        # 不能变成"谁先提交谁在前"——取事件的原文起点，同一事件内按提交顺序
        # 依次增量，并避开本事件里已被引文坐标占用的序号。
        sequence = prop["start"]
        if not isinstance(sequence, int):
            identity = json.dumps([anchor, node_id, property_id, prop.get("value"), prop["op"],
                                   prop.get("scope") or "world", prop.get("holder") or ""],
                                  ensure_ascii=False, sort_keys=True)
            assigned = assigned_sequences.setdefault(anchor, {})
            sequence = assigned.get(identity)
            if sequence is None:
                sequence = event_starts.get(anchor, 0)
                while (sequence in quoted_sequences.get(anchor, set())
                       or sequence in used_sequences.setdefault(anchor, set())):
                    sequence += 1
                assigned[identity] = sequence
        used_sequences.setdefault(anchor, set()).add(sequence)
        record = {
            "kind": "property_change",
            "node_id": node_id, "property_id": property_id,
            "op": prop['op'], "event_id": anchor, "sequence": sequence,
            **({'value': value} if 'value' in prop else {}),
        }
        if quote:
            record["disclosure"] = {"source_id": data["source_id"], "start": prop["start"]}
            record["evidence"] = {"source_id": data["source_id"], "quote": quote}
        else:
            # 属性不填引文：它的披露位置与证据就是 at_event 那个事件的（形状与逐字引文一致）。
            event = current_nodes.get(anchor) or {}
            point, words = event.get("disclosure") or {}, event.get("evidence") or {}
            if isinstance(point.get("start"), int) and isinstance(words.get("quote"), str) and words["quote"]:
                record["disclosure"] = {"source_id": point.get("source_id"), "start": point["start"]}
                record["evidence"] = {"source_id": words.get("source_id"), "quote": words["quote"]}
        # Who chose the anchor, not what it proves: "model" means the model named
        # the event; it still only locates the disclosure, not the story change.
        if prop.get("source"):
            record["source"] = str(prop["source"])
        # The category the model chose. 草稿层已要求必填（含显式「未分类」）：
        # 程序不猜分类，人可以在浏览页之后把记录移到大类。
        if prop.get("category"):
            record["category"] = prop["category"]
        # Who else the sentence involves. Resolved to ids; unknown names drop out
        # rather than becoming dangling references.
        related: list[str] = []
        for name in prop.get("related_nodes") or []:
            target = None
            for kind in NODE_TYPES:
                target = ids.get((kind, name)) or existing.get((kind, name))
                if target:
                    break
            if target and target not in related:
                related.append(target)
        if related:
            record["related_nodes"] = related
        scope = prop.get("scope") or "world"
        if scope != "world":
            holder = None
            for kind in ("person", "group"):
                holder = ids.get((kind, prop.get("holder", "")))
                if holder:
                    break
            if holder is None:
                for kind in ("person", "group"):
                    holder = existing.get((kind, prop.get("holder", "")))
                    if holder:
                        break
            if holder is None:
                continue
            record["scope"] = scope
            record["holder_id"] = holder
        key = observation_key(record)
        previous = known_values.get(key)
        if previous is not None:
            if "_draft_index" in prop:
                property_origins.setdefault(previous["id"], []).append(prop["_draft_index"])
            # Classification edits preserve the identity of the original event change.
            annotations = {field: record[field] for field in ("category", "source")
                           if record.get(field) and previous.get(field) != record[field]}
            combined_related = sorted(set(previous.get("related_nodes") or []) | set(record.get("related_nodes") or []))
            if combined_related != sorted(previous.get("related_nodes") or []):
                annotations["related_nodes"] = combined_related
            if annotations:
                updated = previous | annotations
                if previous["id"] in {row["id"] for row in records}:
                    records = [updated if row["id"] == previous["id"] else row for row in records]
                else:
                    records.append(updated)
                known_values[key] = updated
            continue
        record["id"] = ops.new_property_id()
        while record["id"] in reserved_ids:
            record["id"] = ops.new_property_id()
        reserved_ids.add(record["id"])
        if "_draft_index" in prop:
            property_origins.setdefault(record["id"], []).append(prop["_draft_index"])
        known_values[key] = record
        records.append(record)

    # A container states what its events state. When a descendant event already
    # carries the same property, the event record keeps the fact and the
    # container write is dropped with a notice: the container value is derived
    # from the events (last effective value), so writing it again would let the
    # reduction count one fact twice at two levels.
    comparable = [row for row in accepted_records if row.get("kind") == "property_change"] + [
        row for row in records if row.get("kind") == "property_change"]
    kept: list[dict] = []
    for row in records:
        duplicate = container_property_duplicate(row, comparable, current_nodes)
        if duplicate is None:
            kept.append(row)
            continue
        indexes = property_origins.get(row.get("id")) or []
        diagnostics.append({
            "severity": "info", "code": "CONTAINER_PROPERTY_DEDUP", "object_id": row.get("node_id"),
            "path": ["properties", indexes[0]] if indexes else [],
            "message": "该属性由下属事件自动归纳（取最后有效值），此处写入已忽略",
            "node_id": row.get("node_id"), "property_id": row.get("property_id"),
            "duplicate_of": duplicate.get("id"), "event_id": duplicate.get("event_id"),
        })
    records = kept

    _entity_event_anchors(
        objects, current_nodes,
        [row for row in accepted_records if row.get("kind") == "property_change"] + comparable,
        definition_records,
        {action_id: event_id for event_id, members in action_children.items() for action_id in members},
        orders, current_ordinal)

    revision = ops.graph_project_info()["revision"]
    node_ids = {item['reference']: ids[(item['type'], item['name'])] for item in data['nodes']
                if 'reference' in item and (item['type'], item['name']) in ids}
    if not objects and not records:
        return {
            "objects": [], "property_records": [], "diagnostics": diagnostics, "errors": [],
            "revision": revision, "empty": True, "node_count": 0,
            "property_count": 0, "scene_links": 0,
            "skipped": skipped, "node_ids": node_ids, "property_origins": property_origins,
        }
    errors: list[dict] = []
    return {
        "node_ids": node_ids,
        "property_origins": property_origins,
        "objects": objects,
        "property_records": records,
        "diagnostics": diagnostics,
        "errors": errors,
        "revision": revision,
        "empty": False,
        "skipped": skipped,
        "node_count": len([item for item in objects if item["kind"] == "node"]),
        "property_count": len([
            item for item in records if item["kind"] == "property_change"
        ]),
        "scene_links": sum(len(items) for items in scene_children.values()),
    }
