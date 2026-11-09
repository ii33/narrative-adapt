"""Display categories for property records.

A category answers "which aspect of the object does this sentence talk about".
It is not ``scope``: scope says whether a record is a world fact, someone's
cognition or someone's claim. A cognition record about an injury is still an
injury record, and a world record about clothing is still an appearance record.

Categories are deliberately loose and adjustable. The vocabulary is a per-node-
type list with a default for every type, stored as project config so it can be
renamed, extended or trimmed at any time. A record has exactly two states:

1. the category saved on the change (the model wrote it, or a human picked one
   with ``property_categorize``) — ``category_source`` is ``explicit``;
2. no category at all — ``未分类``, ``category_source`` ``default_unclassified``.

The program does not guess. Property names and values are never used to infer a
category: 「身高」 is not 外貌 until somebody says so, and a record that arrives
without a category is not quietly filed under one. ``未分类`` is a real, human
choice — the model is required to pass ``category`` on every property row and may
pass ``未分类`` explicitly, and a human can move a record to a proper category in
the browse page later. Merging two property names is a separate operation — a
category move never renames anything.
"""

from __future__ import annotations

import json

CONFIG_KEY = "property_categories"
UNCLASSIFIED = "未分类"
# 旧词表末项。只用于把已保存的分类配置归一化，不对记录做任何迁移。
LEGACY_FALLBACK = "其他"

# Default vocabulary per node type. The last entry is always 未分类; a saved config
# replaces the whole list for a type and 未分类 is appended back.
DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "person": [
        "基本信息", "外貌", "身份与经历", "能力与身体状态",
        "持有与使用", "关系", "心理与意图", UNCLASSIFIED,
    ],
    "item": [
        "基本信息", "外观与构造", "功能与特性", "状态",
        "归属与持有", "关联", UNCLASSIFIED,
    ],
    "group": [
        "基本信息", "构成与规模", "实力与资源", "立场与关系", "状态", UNCLASSIFIED,
    ],
    "location": [
        "基本信息", "环境与外观", "位置与范围", "势力与居住者", "状态", UNCLASSIFIED,
    ],
    # 事件属性标识基本信息、地点、参与者、过程与结果。
    "event": ["基本信息", "参与者", "过程与结果", UNCLASSIFIED],
    # 场景是这一层唯一带属性的：它说的不是自己，而是「大事件走到这里的样子」。
    "scene": ["基本信息", "大事件", "内容", UNCLASSIFIED],
    # 大事件自己不写属性——界面上那一行是从场景借来的（见 node_properties）。
    "storyline": ["主要故事", UNCLASSIFIED],
    "big_event": ["基本信息", "主要故事", "内容", UNCLASSIFIED],
    "action": ["基本信息", "参与者", "过程与结果", UNCLASSIFIED],
    "other": ["基本信息", UNCLASSIFIED],
}

# A relation stated from one side is worth reading from the other. The mapping
# only covers the categories whose opposite name is genuinely different; the
# rest keep their own name when the viewer's vocabulary has it.
_INVERSE_CATEGORY: dict[str, dict[str, str]] = {
    "person": {"归属与持有": "持有与使用", "关联": "关系"},
}


def default_vocabulary() -> dict[str, list[str]]:
    return {node_type: list(values) for node_type, values in DEFAULT_CATEGORIES.items()}


def normalize_vocabulary(config: object) -> dict[str, list[str]]:
    """Merge a saved config over the defaults, dropping anything unusable.

    A saved list replaces the default list for that type. 未分类 is always the last
    entry and cannot be removed: it is the explicit "not classified yet" option.
    A list saved before the rename still ends with the old 其他; it is normalized
    to 未分类 here so a project never carries two tails.
    """
    base = default_vocabulary()
    if not isinstance(config, dict):
        return base
    for node_type, values in config.items():
        if node_type not in base or not isinstance(values, list):
            continue
        kept: list[str] = []
        for value in values:
            if not isinstance(value, str):
                continue
            text = value.strip()
            if text and text not in (UNCLASSIFIED, LEGACY_FALLBACK) and text not in kept:
                kept.append(text)
        if kept:
            base[node_type] = kept + [UNCLASSIFIED]
    return base


def validate_vocabulary(config: object) -> dict[str, list[str]]:
    """Reject a malformed config instead of silently repairing it on save."""
    if not isinstance(config, dict) or not config:
        raise ValueError("分类配置须为按节点类型分组的对象")
    for node_type, values in config.items():
        if node_type not in DEFAULT_CATEGORIES:
            raise ValueError(f"未知节点类型：{node_type}")
        if not isinstance(values, list):
            raise ValueError(f"{node_type} 的分类须为字符串数组")
        names = [value.strip() for value in values if isinstance(value, str)]
        if len(names) != len(values):
            raise ValueError(f"{node_type} 的分类须全部是字符串")
        if any(not name for name in names):
            raise ValueError(f"{node_type} 的分类名不能为空")
        if any(len(name) > 20 for name in names):
            raise ValueError(f"{node_type} 的分类名不能超过 20 字")
        if len(names) != len(set(names)):
            raise ValueError(f"{node_type} 的分类名重复")
    return normalize_vocabulary(config)


def vocabulary_for(node_type: str | None, config: object = None) -> list[str]:
    base = normalize_vocabulary(config)
    return base.get(node_type or "", base["other"])


def resolve(
    record: dict,
    node_type: str | None,
    config: object = None,
    overrides: dict[str, str] | None = None,
) -> tuple[str, str]:
    """The category of one value record and where the decision came from.

    Two answers only: ``explicit`` when the change carries a category (written by
    the model or picked by a human), ``default_unclassified`` when it carries
    none. Nothing is inferred from the property name or the value, and a record
    without a category is never filed under a specific one.
    """
    record_id = record.get("id")
    if overrides and isinstance(record_id, str) and record_id in overrides:
        return overrides[record_id], "explicit"
    explicit = record.get("category")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip(), "explicit"
    return UNCLASSIFIED, "default_unclassified"


def aspect_of(property_id: str) -> str:
    """The aspect a property name points at.

    `cultivation` and `cultivation_stage` are two names for one subject, and a
    reader wants them on one line with one current value. The first token is the
    aspect; `state`, `status` and `statement` stay apart because they share no
    prefix. Grouping by aspect never renames anything — the merge is a decision
    the audit still asks a human to make.
    """
    name = (property_id or "").strip().lower()
    # 「恋人（冷战中）」是同一个方面的两种状态：括号里的不算新方面。
    for mark in ("（", "(", "[", "【"):
        if mark in name:
            name = name.split(mark)[0]
    return name.strip().split("_")[0] if name else ""


def inverse_category(source_category: str, viewer_type: str | None, config: object = None) -> str:
    """Where the other end of a relation shows the same record."""
    mapped = _INVERSE_CATEGORY.get(viewer_type or "", {}).get(source_category, source_category)
    return mapped if mapped in vocabulary_for(viewer_type, config) else UNCLASSIFIED


def renames(previous: object, current: object) -> dict[tuple[str, str], str]:
    """Category renames between two vocabularies, as ``(node_type, old) -> new``.

    Renaming a category would otherwise silently drop its records back into 未分类,
    because a derived category is only as good as the name it was derived from.
    A one-for-one replacement inside a type is treated as a rename; a wholesale
    rewrite is not guessed at.
    """
    before_all = normalize_vocabulary(previous)
    after_all = normalize_vocabulary(current)
    result: dict[tuple[str, str], str] = {}
    for node_type, names in after_all.items():
        before = [name for name in before_all.get(node_type, []) if name != UNCLASSIFIED]
        after = [name for name in names if name != UNCLASSIFIED]
        gone = [name for name in before if name not in after]
        added = [name for name in after if name not in before]
        if not gone or len(gone) != len(added) or len(gone) > 4:
            continue
        for old_name, new_name in zip(gone, added, strict=True):
            result[(node_type, old_name)] = new_name
    return result


def order_categories(
    categories: list[str], node_type: str | None, config: object = None
) -> list[str]:
    """Vocabulary order first, then anything unknown, with 未分类 always last."""
    vocabulary = vocabulary_for(node_type, config)
    known = [name for name in vocabulary if name != UNCLASSIFIED and name in categories]
    unknown = sorted(name for name in categories if name not in vocabulary)
    tail = [UNCLASSIFIED] if UNCLASSIFIED in categories else []
    return known + unknown + tail


def load(store) -> dict[str, list[str]]:
    """Read the project's vocabulary, falling back to the defaults."""
    raw = store.meta(CONFIG_KEY)
    if not raw:
        return default_vocabulary()
    try:
        return normalize_vocabulary(json.loads(raw))
    except json.JSONDecodeError:
        return default_vocabulary()


def save(store, config: object) -> dict[str, list[str]]:
    vocabulary = validate_vocabulary(config)
    store.set_meta(CONFIG_KEY, json.dumps(vocabulary, ensure_ascii=False))
    return vocabulary
