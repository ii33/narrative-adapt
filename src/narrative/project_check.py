"""Read-only static checks of one revision, without staging a write."""
from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime

from . import property_categories, property_core
from .property_audit import audit_properties

# 静态检查的每条问题都要能"点得动"：target.kind 说明它该跳到哪一类对象上。
# 前缀优先于码表，所以 EVENT_LOCATION_*、STORYLINE_* 这类族不需要逐个登记。
_TARGET_KINDS_BY_PREFIX = (
    ("EVENT_LOCATION_", "event_location"),
    ("STORYLINE_", "storyline_range"),
    ("BIG_EVENT_", "storyline_range"),
)
_TARGET_KINDS_BY_CODE = {
    "PARTICIPANTS": "participants",
    # 定义级：问题针对属性定义本身，没有节点或原文坐标。
    "definition_unused": "property_definition",
    "definition_missing": "property_definition",
    "category_unknown": "property_definition",
    # 引用级：问题在于某个引用指向不存在、歧义或不合法的对象。
    "REF": "reference",
    "NODE_REF": "reference",
    "EVENT_REF": "reference",
    "TARGET": "reference",
    "COMPOSITE": "reference",
    "EVENT_CHAIN": "reference",
    "CONTAINMENT_CYCLE": "reference",
    "EVENT_SCENE": "reference",
    "ACTION_EVENT": "reference",
    "ACTION_LEAF": "reference",
    "TIME_REF": "reference",
    "self_reference": "reference",
    "name_variant": "reference",
    "duplicate_value": "reference",
    "repeated_value": "reference",
    "relation_not_a_person": "reference",
    "relation_pair_name": "reference",
    "relation_untyped_reference": "reference",
    "relation_same_both_ways": "reference",
}


def _target_kind(code: str, record: dict, node_id, property_id, records: dict, source_id=None) -> str:
    # 判据只有一条：有定位对象才给具体 kind。项目级汇总（例如「27 条属性归入其他，
    # 分布在 14 个节点」）身上没有任何 property_id/node_id/记录/来源，就不该伪装成
    # 某一个定义或节点——否则界面会以为可以跳，实际无处可跳。
    if not (node_id or property_id or record.get("id") or source_id):
        return "project"
    for prefix, kind in _TARGET_KINDS_BY_PREFIX:
        if code.startswith(prefix):
            return kind
    kind = _TARGET_KINDS_BY_CODE.get(code)
    if kind:
        return kind
    if record.get("kind") in {"property_change", "property_definition"}:
        return record["kind"]
    if node_id:
        return "node"
    if property_id and records.get(property_id, {}).get("kind") == "property_definition":
        return "property_definition"
    return "project"


def _locate(issue: dict, objects: dict, records: dict, sources: dict) -> dict:
    result = dict(issue)
    identifier = issue.get("object_id")
    record_ids = list(issue.get("record_ids") or [])
    if identifier in records and identifier not in record_ids:
        record_ids.insert(0, identifier)
    record = records.get(record_ids[0], {}) if record_ids else {}
    node_id = issue.get("node_id") or record.get("node_id")
    if not node_id and objects.get(identifier, {}).get("kind") == "node":
        node_id = identifier
    node = objects.get(node_id, {})
    obj = objects.get(identifier, node)
    evidence = record.get("evidence") or node.get("evidence") or {}
    disclosure = record.get("disclosure") or node.get("disclosure") or {}
    source_id = evidence.get("source_id") or disclosure.get("source_id")
    start = evidence.get("start", disclosure.get("start"))
    quote = evidence.get("quote")
    end = evidence.get("end")
    if end is None and isinstance(start, int) and isinstance(quote, str):
        end = start + len(quote)
    result.update(object_id=identifier or node_id, node_id=node_id, node_name=node.get("name"),
                  object_name=obj.get("name") or obj.get("label") or sources.get(source_id, {}).get("title"),
                  property_id=issue.get("property_id") or record.get("property_id")
                              or (record.get("id") if record.get("kind") == "property_definition" else None),
                  record_ids=record_ids, record_id=record_ids[0] if record_ids else None,
                  source_id=source_id, start=start, end=end, quote=quote)
    result["target"] = {"kind": _target_kind(str(issue.get("code") or ""), record, node_id,
                                             result["property_id"], records, source_id),
                        "property_id": result["property_id"],
                        "node_id": node_id, "record_id": result["record_id"],
                        "source_id": source_id, "start": start, "end": end, "quote": quote}
    if 'path' not in result:
        if result['record_id']:
            result['path'] = ['property_records', result['record_id']]
        elif identifier in objects:
            result['path'] = ['objects', identifier]
        elif node_id:
            result['path'] = ['objects', node_id]
        elif source_id:
            result['path'] = ['sources', source_id]
        else:
            result['path'] = []
        if result.get('field') and isinstance(result['field'], str) and result['path']:
            result['path'].append(result['field'])
    from .diagnostic_locations import normalize_problem
    return normalize_problem(result, operation='check_project')


def check(ops) -> dict:
    # Capture objects, event changes and immutable sources in one read transaction.
    graph = ops._store.snapshot(validation_data=True)
    revision = graph["revision"]
    validation = graph.pop("_validation")
    graph["sources"] = [row | {"text": validation["sources"][row["id"]]["text"]} for row in graph["sources"]]
    raw = validation["records"]
    records = raw
    indexed = {row["id"]: row for row in records}
    issues = ops._service.validate_snapshot(graph, records)
    from .temporal_state import change_issues, projected_changes
    issues.extend(change_issues(graph, records))
    current = records
    historical = []
    enriched = graph
    issues.extend(property_core.validate_records(current, enriched, historical))
    audit_rows = [row for row in records if row.get("kind") == "property_definition"] + projected_changes(graph, records)
    active_nodes = [row for row in graph["nodes"] if not row.get("retracted")]
    active_ids = {row["id"] for row in active_nodes}
    audit_rows = [row for row in audit_rows if not row.get("node_id") or row["node_id"] in active_ids]
    category_config = validation["meta"].get(property_categories.CONFIG_KEY)
    try:
        categories = property_categories.normalize_vocabulary(json.loads(category_config)) if category_config else None
    except json.JSONDecodeError:
        categories = None
    issues.extend(audit_properties(audit_rows, active_nodes, categories=categories, usage_records=records,
                                   snapshot=enriched)["issues"])
    objects = {row["id"]: row for row in graph["objects"]}
    sources = {row["id"]: row for row in graph["sources"]}
    located = [_locate(issue, objects, indexed, sources) for issue in issues]
    unique = {}
    for issue in located:
        key = (issue["code"], issue.get("object_id"), issue.get("node_id"),
               tuple(issue["record_ids"]), issue["message"])
        unique.setdefault(key, issue)
    output = sorted(unique.values(), key=lambda row: (
        {"error": 0, "warning": 1, "info": 2}.get(row.get("severity"), 3),
        str(row.get("node_id") or ""), row["code"], json.dumps(row["record_ids"])))
    counts = {severity: 0 for severity in ("error", "warning", "info")}
    counts.update(Counter(row["severity"] for row in output))
    return {"revision": revision, "checked_revision": revision,
            "current_revision": ops.graph_project_info()["revision"], "issues": output, "counts": counts,
            "checked_at": datetime.now(UTC).isoformat(),
            "checked_objects": len(active_nodes), "checked_records": len(current),
            "historical_records": len(historical)}
