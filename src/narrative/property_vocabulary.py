"""Which property names each of the five event layers may carry.

五层都是「事件」，只是粒度等级（`docs/MODEL.md` 第 1 节）。受约束的是这五层，
**人物／物品／地点／群体继续自由命名**——他们的属性名是描述，事件层的属性名是结构。

判据两句话（都是用户裁定，`docs/extraction/SPEC.md` §0.1）：

* `U-12` 事件类节点（T／B／S／E／A）的属性名从这张表里取，不自己造；表外的名字
  丢弃并回一条 `info`（`U-10` 口径：多余不影响理解，缺失才影响理解）。
* `U-13` 编号是身份，名字是属性：建节点时 `node.name` 就是第一条「名称」的取值，
  之后改名写一条新的「名称」，旧名随 `set` 失效；读取取「截至阅读位置的最后一条
  名称」，没有变更就回落到 `node.name`。

这张表存在工程配置里（`store.meta` 的 `property_vocabulary`），与分类词表
（:mod:`narrative.property_categories`）同一套存储方式，可改：改名字、改归类，都只
影响之后写入的行，不改已有记录。
"""

from __future__ import annotations

import json

CONFIG_KEY = "property_vocabulary"

# 受白名单约束的五层。别的节点类型（person／item／location／group／other）不在表内，
# 属性名照旧自由命名。
LAYERS = ("storyline", "big_event", "scene", "event", "action")
LAYER_LABELS = {
    "storyline": "主要故事 T",
    "big_event": "大事件 B",
    "scene": "场景 S",
    "event": "事件 E",
    "action": "动作 A",
}

# 名字是身份的必要属性，不能从任何一层删掉（`U-13`）。
NAME_PROPERTY = "名称"
# 读取「当前名字」时认的属性名。`name` 是同一件事的英文写法（`property_labels`
# 把它显示成「名称」），历史工程里出现过，照认不误。
NAME_IDS = frozenset({NAME_PROPERTY, "name"})

# 默认表：层 → [属性名, 分类]。分类由表带出（`名称` 归「基本信息」），不再让模型
# 每次自己选一个；表外的分类写法以表为准并回一条 `info`。
DEFAULT_VOCABULARY: dict[str, list[dict[str, str]]] = {
    "storyline": [
        {"name": "下属大事件", "category": "主要故事"},
        {"name": "名称", "category": "基本信息"},
    ],
    "big_event": [
        {"name": "下属场景", "category": "内容"},
        {"name": "名称", "category": "基本信息"},
    ],
    "scene": [
        {"name": "下属事件", "category": "内容"},
        {"name": "名称", "category": "基本信息"},
    ],
    "event": [
        {"name": "地点", "category": "基本信息"},
        {"name": "参与者", "category": "参与者"},
        {"name": "锚点", "category": "基本信息"},
        {"name": "名称", "category": "基本信息"},
    ],
    "action": [
        {"name": "所属事件", "category": "基本信息"},
        {"name": "名称", "category": "基本信息"},
    ],
}

# 表里的名字是中文；同一件事的英文写法与历史写法照认。地点这一组与
# `src/narrative/event_location.py::LOCATION_PROPERTIES` 对齐——程序自己生成的地点
# 属性行可能沿用一个旧工程已有的地点定义名，不能被白名单拦掉（`U-12`）。
_ALIASES: dict[str, frozenset[str]] = {
    "名称": frozenset({"name"}),
    "地点": frozenset({"发生地点", "location_of_event", "event_location"}),
    "参与者": frozenset({"participants"}),
    "锚点": frozenset({"anchor"}),
    "下属大事件": frozenset({"events", "children"}),
    "下属场景": frozenset({"events", "children"}),
    "下属事件": frozenset({"events", "children"}),
    "所属事件": frozenset({"event"}),
}


def default_vocabulary() -> dict[str, list[dict[str, str]]]:
    return {layer: [dict(entry) for entry in entries] for layer, entries in DEFAULT_VOCABULARY.items()}


def normalize_vocabulary(config: object) -> dict[str, list[dict[str, str]]]:
    """Merge a saved config over the defaults, dropping anything unusable.

    一层给了可用的表就替换该层的默认表；给不出（坏数据、空表）就保留默认表——
    「这一层什么都不能写」不是可用的配置。`名称` 是身份属性，缺了就补回默认那一项。
    """
    base = default_vocabulary()
    if not isinstance(config, dict):
        return base
    for layer, raw in config.items():
        if layer not in base or not isinstance(raw, list):
            continue
        kept: list[dict[str, str]] = []
        for item in raw:
            entry = _normalize_entry(item)
            if entry and all(entry["name"] != row["name"] for row in kept):
                kept.append(entry)
        if not kept:
            continue
        if all(row["name"] != NAME_PROPERTY for row in kept):
            kept.insert(0, _normalize_entry(next(e for e in base[layer] if e["name"] == NAME_PROPERTY)))
        base[layer] = kept
    return base


def _normalize_entry(item: object) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    category = item.get("category")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(category, str) or not category.strip():
        return None
    return {"name": name.strip(), "category": category.strip()}


def validate_vocabulary(config: object) -> dict[str, list[dict[str, str]]]:
    """Reject a malformed config instead of silently repairing it on save."""
    if not isinstance(config, dict) or not config:
        raise ValueError("属性白名单须为按层分组的对象")
    for layer, entries in config.items():
        if layer not in LAYERS:
            raise ValueError(f"未知的层：{layer}")
        if not isinstance(entries, list):
            raise ValueError(f"{LAYER_LABELS[layer]}的白名单须为数组")
        names = []
        for item in entries:
            entry = _normalize_entry(item)
            if entry is None:
                raise ValueError(f"{LAYER_LABELS[layer]}的每一项须含 name 与 category 两个非空字符串")
            if len(entry["name"]) > 20 or len(entry["category"]) > 20:
                raise ValueError(f"{LAYER_LABELS[layer]}的属性名与分类名不能超过 20 字")
            names.append(entry["name"])
        if len(names) != len(set(names)):
            raise ValueError(f"{LAYER_LABELS[layer]}的属性名重复")
        if NAME_PROPERTY not in names:
            raise ValueError(f"{LAYER_LABELS[layer]}必须保留「{NAME_PROPERTY}」：名字是身份属性，不能删")
    missing = [layer for layer in LAYERS if layer not in config]
    if missing:
        raise ValueError("白名单必须写全五层：" + "、".join(LAYER_LABELS[layer] for layer in missing))
    return normalize_vocabulary(config)


def constrained(node_type: str | None) -> bool:
    """只有 T／B／S／E／A 受白名单约束。"""
    return (node_type or "") in LAYERS


def entries_for(node_type: str | None, config: object = None) -> list[dict[str, str]]:
    """一层允许的属性（含分类）。不受约束的类型返回空表。"""
    if not constrained(node_type):
        return []
    return list(normalize_vocabulary(config)[node_type or ""])


def allowed_names(node_type: str | None, config: object = None) -> list[str]:
    return [entry["name"] for entry in entries_for(node_type, config)]


def match(node_type: str | None, property_id: str | None, config: object = None) -> dict[str, str] | None:
    """属性名 → 表里的那一项；表外返回 `None`。

    名字与英文／历史别名都认（同一件事的两种写法）；认出来就用表里那一项的分类，
    不因为写法不同另立一条。
    """
    name = (property_id or "").strip()
    if not name or not constrained(node_type):
        return None
    lowered = name.casefold()
    for entry in entries_for(node_type, config):
        if entry["name"] == name:
            return entry
        aliases = _ALIASES.get(entry["name"], frozenset())
        if lowered in {alias.casefold() for alias in aliases}:
            return entry
    return None


def category_for(node_type: str | None, property_id: str | None, config: object = None) -> str | None:
    entry = match(node_type, property_id, config)
    return entry["category"] if entry else None


def vocabulary_snapshot(config: object = None) -> dict[str, dict]:
    """工具与界面看的形状：每层一行说明 + 允许的属性（名字与分类）。"""
    table = normalize_vocabulary(config)
    return {
        layer: {"label": LAYER_LABELS[layer],
                "properties": [{"name": entry["name"], "category": entry["category"]}
                               for entry in table[layer]]}
        for layer in LAYERS
    }


def is_name_property(property_id: object) -> bool:
    """这条变更说的是「这个名字」吗（`U-13`）。"""
    return isinstance(property_id, str) and property_id.strip() in NAME_IDS


def effective_name(node: dict, changes: list[dict]) -> str:
    """当前生效的名字：最后一条「名称」的值，没有就是节点的初始名。

    `changes` 是该节点截至某个阅读位置的「名称」变更，**已按事件顺序排好**。
    旧名不并列——`set` 取代前值，所以这里只取最后一条有效的（`U-13`）。
    """
    name = ""
    for row in changes:
        if row.get("kind", "property_change") != "property_change":
            continue
        if not is_name_property(row.get("property_id")):
            continue
        if row.get("op") not in {"set", "add"}:
            continue
        value = row.get("value")
        if isinstance(value, list):
            value = value[-1] if value else ""
        if isinstance(value, str) and value.strip():
            name = value.strip()
    return name or str(node.get("name") or node.get("id") or "")


def load(store) -> dict[str, list[dict[str, str]]]:
    """Read the project's table, falling back to the defaults."""
    raw = store.meta(CONFIG_KEY)
    if not raw:
        return default_vocabulary()
    try:
        return normalize_vocabulary(json.loads(raw))
    except json.JSONDecodeError:
        return default_vocabulary()


def save(store, config: object) -> dict[str, list[dict[str, str]]]:
    vocabulary = validate_vocabulary(config)
    store.set_meta(CONFIG_KEY, json.dumps(vocabulary, ensure_ascii=False))
    return vocabulary
