"""One surface for programs and models: read the graph, and edit it.

Every capability here is a plain function over a project file — no Qt, no window,
no GUI state. The CLI and the MCP server are two doors onto this same list, so an
LLM calling a tool and a person typing a command cannot disagree about what
"the state of this scene" means.

Reading is side-effect free. Event facts are appended for story changes;
editorial corrections change the current record through Operations. Validated
writes are atomic and audited; pending extraction drafts are separate from facts.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import source_tools
from .adaptation_tools import register as _register_adaptation_tools
from .asset_tools import register as _register_asset_tools
from .assistant_attachment_tools import register as _register_attachment_tools
from .assistant_host_tools import register as _register_host_tools
from .extraction_draft import submission_input_schema, submission_parameters
from .project_catalog import list_projects, open_graph_project
from .screenplay_tools import register as _register_screenplay_tools


class ToolError(ValueError):
    """A call the tool cannot honour; the message is meant for the caller."""


@dataclass(frozen=True)
class Tool:
    name: str
    summary: str
    params: dict[str, dict]
    handler: Callable[[Any, dict], dict]
    writes: bool = False
    required: tuple[str, ...] = field(default=())
    scope: str = "project"
    input_schema: dict | None = None

    def schema(self) -> dict:
        if self.input_schema is not None:
            from copy import deepcopy
            return deepcopy(self.input_schema)
        properties = {
            key: {k: v for k, v in spec.items() if k != "required"}
            for key, spec in self.params.items()
        }
        required = [key for key, spec in self.params.items() if spec.get("required")]
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }


def _labelled(node: dict) -> dict:
    """节点 + 它的显示编号（`P3`/`E1.2`）。界面显示什么，工具就返回什么。"""
    from .order_ids import label_id

    if not isinstance(node, dict) or not node.get("id"):
        return node
    return node | {"label_id": label_id(str(node["id"]), node.get("node_type"))}


def _need(params: dict, *keys: str) -> None:
    missing = [key for key in keys if key not in params or params[key] is None]
    if missing:
        raise ToolError("缺少参数：" + "、".join(missing))


def _cut(params: dict) -> str | None:
    cut = params.get("cut")
    return str(cut) if cut not in (None, "") else None


def _node_types(snapshot: dict) -> Counter:
    return Counter(
        node.get("node_type") or "other" for node in snapshot.get("nodes", [])
    )


def _properties(ops, node_id: str, cut: str | None) -> list[dict]:
    """The node's properties as the reader knows them at this position."""
    summary = ops.property_node_summary(node_id, cut=cut)
    return [
        {
            "property_id": item["property_id"],
            "label": item["label"],
            "category": item["category"],
            "value": item["value"],
            "value_node": item.get("value_node"),
            "scope": item["scope"],
            "holder": item.get("holder_name"),
            "disclosed_at": item.get("disclosed_at"),
            "records": [
                {
                    "id": row["id"],
                    "value": row.get("value"),
                    "op": row.get("op"), "sequence": row.get("sequence"),
                    "event_id": row["event_id"],
                    "event_name": row.get("event_name"),
                }
                for row in item["records"]
            ],
        }
        for item in summary["properties"]
    ]


# -- reading ---------------------------------------------------------------


def _list_projects(_ops, params: dict) -> dict:
    directory = params.get("dir") or "data/graph-projects"
    return {"directory": str(Path(directory).resolve()), "projects": list_projects(directory)}


def _duplicate_project(_ops, params: dict) -> dict:
    from .operations import Operations
    _need(params, "source", "destination")
    for key in ("source", "destination"):
        _validate_adaptation_argument(params[key], {"type": "string"}, key)
    try:
        copied = Operations.duplicate_graph_project(params["source"], params["destination"])
    except (ValueError, OSError) as exc:
        raise ToolError(str(exc)) from exc
    info = copied.graph_project_info()
    return {"status": "copied", "path": str(Path(params["destination"]).resolve()),
            "title": info["title"], "revision": info["revision"]}


def _project_info(ops, _params: dict) -> dict:
    info = ops.graph_project_info()
    snapshot = ops.graph_snapshot()
    return {
        "project": {key: info[key] for key in ("title", "name", "revision", "schema_version") if key in info},
        "nodes": dict(_node_types(snapshot)),
        "sources": len(ops.sources()),
    }


def _list_nodes(ops, params: dict) -> dict:
    view = ops.browse_view(
        granularity=params.get("granularity") or "scene",
        cut_id=str(params.get("cut") or ""),
        node_type=params.get("node_type") or "",
        query=params.get("query") or "",
        related_only=bool(params.get("related_only")),
        time_filter=True,
    )
    limit = int(params.get("limit") or 200)
    return {
        "cut": view.get("cut"),
        "counts": view.get("counts"),
        "total": view.get("total"),
        "project_total": view.get("project_total"),
        "nodes": view.get("nodes", [])[:limit],
    }


def _get_node(ops, params: dict) -> dict:
    _need(params, "node_id")
    node_id = str(params["node_id"])
    cut = _cut(params)
    snapshot = ops.graph_snapshot()
    node = next((item for item in snapshot["nodes"] if item["id"] == node_id), None)
    if node is None:
        raise ToolError(f"节点不存在：{node_id}")
    return {"node": node, "cut": cut, "properties": _properties(ops, node_id, cut)}


def _node_properties(ops, params: dict) -> dict:
    _need(params, "node_id")
    cut = _cut(params)
    requested = str(params["node_id"])
    return {"node_id": requested, "cut": cut, "properties": _properties(ops, requested, cut)}


def _related_ids(ops, step_id: str) -> list[str]:
    from .browse_view import related_nodes

    snapshot = ops.graph_snapshot()
    return sorted(related_nodes(snapshot, step_id))


def _scene_events(snapshot: dict, scene: dict, seen: set[str] | None = None) -> list[str]:
    """A scene's events, deepest last — the reading order inside one scene."""
    from .order_ids import order_key

    nodes = {node["id"]: node for node in snapshot["nodes"]}
    seen = seen or set()
    out: list[str] = []
    for child_id in scene.get("events") or []:
        if child_id in seen:
            continue
        seen.add(child_id)
        child = nodes.get(child_id)
        if child is None:
            continue
        if child.get("node_type") == "event":
            out.append(child_id)
        out.extend(_scene_events(snapshot, child, seen))
    return sorted(dict.fromkeys(out), key=lambda item: order_key(item) or ())


def _state_of(ops, ids: list[str], cut: str, limit: int = 20) -> list[dict]:
    snapshot = ops.graph_snapshot()
    by_id = {node["id"]: node for node in snapshot["nodes"]}
    out: list[dict] = []
    for node_id in ids:
        node = by_id.get(node_id)
        if node is None or node.get("node_type") in {"event", "action", "scene", "big_event", "storyline"}:
            continue
        properties = _properties(ops, node_id, cut)
        if not properties:
            continue
        out.append({
            "id": node_id,
            "label_id": _labelled(node).get("label_id"),
            "name": node.get("name"),
            "node_type": node.get("node_type"),
            "properties": properties,
        })
        if len(out) >= limit:
            break
    return out


def _list_moments(ops, _params: dict) -> dict:
    """阅读位置的层级：一列一层（场景 → 事件），给 cut 挑合法值。"""
    from .browse_view import moment_tree

    return moment_tree(ops.graph_snapshot())


def _material_view(ops, params: dict) -> dict:
    _need(params, "node_id")
    return ops.material_view(str(params["node_id"]), cut=_cut(params) or "")


def _content_receipt(value):
    return {key: value[key] for key in ('status', 'submission_id', 'submission_revision', 'submission_status',
        'validation_status', 'operation', 'target', 'base_revisions', 'problems', 'committed_changes') if key in value}


def _add_material_card(ops, params: dict) -> dict:
    _need(params, "node_id", "title")
    card = ops.material_add_card(
        str(params["node_id"]),
        str(params["title"]),
        str(params.get("detail") or ""),
        str(params.get("valid_from") or ""),
        str(params.get("valid_to") or ""),
    )
    if card.get('status') == 'staged':
        return card
    return {"status": "accepted", "card": card} | _material_view(ops, params) | _content_receipt(card)


def _update_material_card(ops, params: dict) -> dict:
    _need(params, "node_id", "card_id")
    card = ops.material_update_card(
        str(params["node_id"]),
        str(params["card_id"]),
        title=None if params.get("title") is None else str(params["title"]),
        detail=None if params.get("detail") is None else str(params["detail"]),
        valid_from=None if params.get("valid_from") is None else str(params["valid_from"]),
        valid_to=None if params.get("valid_to") is None else str(params["valid_to"]),
    )
    if card.get('status') == 'staged':
        return card
    return {"status": "accepted", "card": card} | _material_view(ops, params) | _content_receipt(card)


def _delete_material_card(ops, params: dict) -> dict:
    _need(params, "node_id", "card_id")
    result = ops.material_remove_card(str(params["node_id"]), str(params["card_id"]))
    return {"status": "accepted", **result} | _material_view(ops, params)


def _add_material_image(ops, params: dict) -> dict:
    _need(params, "node_id", "card_id", "path")
    record = ops.material_add_image(
        str(params["node_id"]),
        str(params["card_id"]),
        Path(str(params["path"])),
        str(params.get("caption") or ""),
    )
    return {"status": "accepted", "image": record} | _material_view(ops, params)


def _delete_material_image(ops, params: dict) -> dict:
    _need(params, "node_id", "card_id", "image_id")
    result = ops.material_remove_image(
        str(params["node_id"]), str(params["card_id"]), str(params["image_id"])
    )
    return {"status": "accepted", **result} | _material_view(ops, params)


def _scene_state(ops, params: dict) -> dict:
    _need(params, "scene_id")
    scene_id = str(params["scene_id"])
    snapshot = ops.graph_snapshot()
    scene = next((item for item in snapshot["nodes"] if item["id"] == scene_id), None)
    if scene is None:
        raise ToolError(f"场景不存在：{scene_id}")
    view = ops.browse_view(granularity="scene", cut_id=scene_id, time_filter=True)
    # browse_view 的 cut 是一整条步骤描述；对外只给事件编号，别的工具都按它取状态。
    # 没有章节标记的小工程算不出投影切点，就退到「它最后一个成员事件」。
    step = view.get("cut") or {}
    cut = step.get("event_id") if isinstance(step, dict) else step
    if not cut:
        members = _scene_events(snapshot, scene)
        cut = members[-1] if members else scene_id
    cut = str(cut)
    return {
        "scene": _labelled(scene),
        "cut": cut,
        "step": step,
        "nodes": view.get("nodes", []),
        "state": _state_of(ops, _related_ids(ops, scene_id), cut),
    }


def _event_state(ops, params: dict) -> dict:
    _need(params, "event_id")
    event_id = str(params["event_id"])
    snapshot = ops.graph_snapshot()
    event = next((item for item in snapshot["nodes"] if item["id"] == event_id), None)
    if event is None:
        raise ToolError(f"事件不存在：{event_id}")
    return {
        "event": _labelled(event),
        "cut": event_id,
        "related": _related_ids(ops, event_id),
        "state": _state_of(ops, _related_ids(ops, event_id), event_id),
    }


def _statistics(ops, _params: dict) -> dict:
    snapshot = ops.graph_snapshot()
    records = ops.property_records()
    kinds = Counter(record.get("kind") for record in records)
    definitions = {record["id"] for record in records if record.get("kind") == "property_definition"}
    values = [record for record in records if record.get("kind") == "property_change"]
    info = ops.graph_project_info()
    return {
        "revision": info["revision"],
        # 编号从 v7 起按类型各一套：读到别的版本就知道编号怎么读。
        "schema_version": info.get("schema_version"),
        "nodes": dict(_node_types(snapshot)),
        "node_total": len(snapshot.get("nodes", [])),
        "sources": [
            {"id": source["id"], "title": source.get("title"), "chars": source.get("length")}
            for source in ops.sources()
        ],
        "properties": {
            "definitions": len(definitions),
            "change_records": len(values),
            "changes_by_op": dict(Counter(row.get("op") for row in values)),
            "records_by_kind": dict(kinds),
        },
    }


def _search(ops, params: dict) -> dict:
    _need(params, "query")
    view = ops.graph_query(query=str(params["query"]), limit=int(params.get("limit") or 50))
    if isinstance(view, dict) and isinstance(view.get("nodes"), list):
        view = view | {"nodes": [_labelled(node) for node in view["nodes"]]}
    return {"query": params["query"], "result": view}


def _property_categories(ops, _params: dict) -> dict:
    return {"vocabulary": ops.property_categories()}


def _source_id(ops, params: dict) -> str:
    return source_tools.source_id(ops, params, ToolError)


def _event_merge(ops, params: dict, *, create: bool = False) -> dict:
    _need(params, "source_event_id", "target_event_id", "expected_revision", "evidence", "reason")
    try:
        method = ops.merge_events if create else ops.preview_event_merge
        return method(**params)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _audit(ops, params: dict) -> dict:
    node_ids = params.get("node_ids")
    return ops.property_audit(set(node_ids) if node_ids else None)


# -- writing ---------------------------------------------------------------


def _revision(ops) -> int:
    return int(ops.graph_project_info()["revision"])


def _record_property_change(ops, params: dict) -> dict:
    _need(params, "node_id", "property_id", "event_id", "op", "expected_revision", "reason")
    fields = {k: v for k, v in params.items() if k not in (
        "node_id", "property_id", "event_id", "op", "expected_revision", "reason")}
    return ops.record_property_change(params["node_id"], params["property_id"],
        params["event_id"], params["op"], expected_revision=params["expected_revision"],
        reason=params["reason"], **fields)


def _correct_property(ops, params: dict) -> dict:
    _need(params, "record_id", "changes", "expected_revision", "reason")
    return ops.correct_property(params["record_id"], params["changes"], params["reason"],
                                expected_revision=params["expected_revision"])


def _revise_node(ops, params: dict) -> dict:
    _need(params, "node_id")
    allowed = ("name", "node_type", "aliases", "identity_note")
    changes = {key: params[key] for key in allowed if params.get(key) not in (None, "")}
    if not changes:
        raise ToolError("没有可应用的修改（name / node_type / aliases / identity_note）")
    snapshot = ops.graph_snapshot()
    node = next((item for item in snapshot["nodes"] if item["id"] == params["node_id"]), None)
    if node is None:
        raise ToolError(f"节点不存在：{params['node_id']}")
    updated = dict(node) | changes
    revision = _revision(ops)
    return ops.apply_changes([updated], [], revision, f"工具修订：{node.get('name')}") | {
        "node_id": params["node_id"], "changes": changes,
    }


def _relation_map(ops, params: dict) -> dict:
    """人物关系网：人是点，关系是有方向的边。

    属性名是关系性质，值指向对方；同向、同性质/状态/作用域的多次披露汇总为一条边，
    原记录与证据保留在 records 中。不同性质仍分开，所以两个人之间
    可以有两支箭头——「A 父亲 B」和「B 儿子 A」——也可以只有一支（「A 崇拜 B」）。
    关系性质带括号时（「恋人（冷战中）」），括号里的是状态，单独给出来。
    """
    from .browse_view import cut_for, moment_tree, related_nodes
    from .property_core import canonical_definitions
    from .property_store import relation_targets
    from .temporal_state import projected_changes

    snapshot = ops.graph_snapshot()
    nodes = {item["id"]: item for item in snapshot["nodes"]}
    events = {item["id"]: item.get("name", item["id"]) for item in snapshot["nodes"]}
    cut = str(params.get("cut") or "")
    anchor = cut_for(snapshot, cut) if cut else None
    persons = [
        {"id": item["id"], "name": item.get("name", item["id"]),
         "label_id": item.get("label_id", item["id"])}
        for item in snapshot["nodes"] if item.get("node_type") == "person"
    ]
    edges: list[dict] = []
    relation_edges: dict[tuple, dict] = {}
    records = ops.property_records()
    cut_position = anchor
    definitions = canonical_definitions(records)
    for record in projected_changes(snapshot, records, anchor):
        if record.get("state_kind") != "known":
            continue
        property_id = str(record.get("property_id") or "")
        canonical = property_id
        targets = relation_targets(record | {"property_id": canonical}, nodes, definitions)
        if not targets:
            continue
        for right in targets:
            left = str(record.get("node_id") or "")
            if left == right:
                continue  # 自己指向自己不是关系（审核会报 self_reference）
            if nodes.get(left, {}).get("node_type") != "person":
                continue
            if nodes.get(right, {}).get("node_type") != "person":
                continue
            raw = canonical
            kind, state = raw, ""
            for mark in ("（", "("):
                if mark in raw:
                    head, _, tail = raw.partition(mark)
                    kind, state = head.strip(), tail.rstrip("）)").strip()
                    break
            scope = str(record.get("scope") or "world")
            holder = str(record.get("holder_id") or "")
            context = str(record.get("context_id") or "canon")
            key = (left, right, kind, state, scope, holder, context)
            existing = relation_edges.get(key)
            if existing is not None:
                existing["record_ids"].append(record.get("id"))
                existing["records"].extend(record.get("provenance") or [dict(record)])
                if record.get("event_id") not in existing["event_ids"]:
                    existing["event_ids"].append(record.get("event_id"))
                continue
            edge = {
                "record_id": record.get("id"),
                "record_ids": [record.get("id")],
                "records": record.get("provenance") or [dict(record)],
                "from": left, "from_name": nodes[left].get("name", left),
                "to": right, "to_name": nodes[right].get("name", right),
                "kind": kind, "state": state,
                "symmetric": definitions.get(canonical, {}).get("symmetric") is True,
                "scope": scope, "holder_id": holder, "context_id": context,
                "event_id": record.get("event_id"),
                "event_ids": [record.get("event_id")],
                "event_name": events.get(record.get("event_id") or "", ""),
            }
            relation_edges[key] = edge
            edges.append(edge)
    edges.sort(key=lambda item: (item["from_name"], item["kind"], item["to_name"]))

    # 只看相关：只留跟这一步有关的人（这一步出场的人、以及关系指向他们的人）。
    related_only = bool(params.get("related_only"))
    related_ids: set[str] = set()
    if related_only and cut:
        related_ids = {
            item for item in related_nodes(snapshot, cut, ops.property_records())
            if nodes.get(item, {}).get("node_type") == "person"
        }
        # 两头都得是这一步里的人：只要一头相关，枢纽人物的所有关系都会被拖进来，
        # 图上又出现这一场根本没出场的人。
        edges = [
            e for e in edges
            if e["from"] in related_ids and e["to"] in related_ids
        ]

    # 没写阅读位置、也没开筛选时，全书的人都画（孤点说明「书里没写他和谁有关」）；
    # 一旦按位置或相关收窄，就只留连线两端——筛选后还摆一屏孤点，等于没筛。
    if related_only and cut:
        # 只看相关时，「相关的人」本身就是答案：一个人出场的场景就画一个人。
        persons = [item for item in persons if item["id"] in related_ids]
    elif cut_position is not None:
        linked = {e["from"] for e in edges} | {e["to"] for e in edges}
        persons = [item for item in persons if item["id"] in linked]
    return {
        "revision": snapshot["revision"],
        "persons": persons,
        "edges": edges,
        "cut": cut,
        "cut_name": (anchor or {}).get("step_name", ""),
        # 时间点选择器用的那棵树（与数据浏览同一份）
        "moments": moment_tree(snapshot),
    }


def _attach_children(ops, params: dict) -> dict:
    """按明确父子编号归入直属容器，完整结构由 Operations 静态校验。"""
    _need(params, "parent_id", "child_ids")
    child_ids = params["child_ids"]
    if not isinstance(child_ids, list) or not child_ids or any(not isinstance(item, str) for item in child_ids):
        raise ToolError("child_ids须为非空节点编号数组")
    nodes = {item["id"]: item for item in ops.graph_snapshot()["nodes"]}
    parent = nodes.get(params["parent_id"])
    if parent is None:
        raise ToolError("父节点不存在")
    child_type = {"storyline": "big_event", "big_event": "scene", "scene": "event", "event": "action"}.get(str(parent.get("node_type") or ""))
    if child_type is None:
        raise ToolError("parent_id须指向主要故事、大事件、场景或事件")
    if any(nodes.get(item, {}).get("node_type") not in ({"scene", "event"} if parent["node_type"] == "scene" else {child_type}) for item in child_ids):
        raise ToolError(f"直属子节点类型须为{child_type}")
    existing = [] if params.get("replace") else list(parent.get("events") or [])
    merged = list(dict.fromkeys(existing + child_ids))
    return ops.apply_changes([dict(parent) | {"events": merged}], [], _revision(ops), "归入直属容器")


def _merge(ops, params: dict) -> dict:
    _need(params, "source_id", "target_id", "reason", "expected_revision")
    return {"status": "accepted", **ops.merge_properties(
        params["source_id"], params["target_id"], params["reason"], params["expected_revision"],
    )}


def _rename(ops, params: dict) -> dict:
    _need(params, "from_id", "to_id", "reason", "expected_revision")
    return {"status": "accepted", **ops.rename_property(
        params["from_id"], params["to_id"], params["reason"], params["expected_revision"],
    )}


def _categorize(ops, params: dict) -> dict:
    _need(params, "ids", "category")
    ids = params["ids"] if isinstance(params["ids"], list) else [params["ids"]]
    return {
        "status": "accepted",
        **ops.property_categorize([str(item) for item in ids], params["category"]),
    }


def _preview_extraction(ops, params: dict) -> dict:
    _need(params, "result")
    return ops.preview_extraction(_source_id(ops, params), params["result"],
                                  params.get("start", 0), params.get("end"))


def _submit_extraction(ops, params: dict) -> dict:
    _need(params, "result")
    return ops.submit_extraction(_source_id(ops, params), params["result"],
                                 params.get("start", 0), params.get("end"))


def _read_extraction_submission(ops, params):
    _need(params, 'submission_id')
    return ops.read_extraction_submission(params['submission_id'], section=params.get('section'), indices=params.get('indices'))


def _list_extraction_submissions(ops, params):
    return ops.list_extraction_submissions(source_id=params.get('source_id'), limit=params.get('limit', 20), after=params.get('after', 0))


def _patch_extraction_submission(ops, params):
    _need(params, 'submission_id', 'expected_revision', 'patches', 'reason')
    return ops.patch_extraction_submission(params['submission_id'], params['expected_revision'], params['patches'], params['reason'])


def _list_content_submissions(ops, params):
    return ops.list_content_submissions(**params)


def _read_content_submission(ops, params):
    return ops.read_content_submission(**params)


def _patch_content_submission(ops, params):
    return ops.patch_content_submission(**params)


TOOLS: dict[str, Tool] = {}


def _register(tool: Tool) -> None:
    if tool.name in TOOLS:
        raise ValueError(f"Duplicate tool registration: {tool.name}")
    TOOLS[tool.name] = tool


_register(Tool(
    "list_projects",
    "列出某个目录下的工程（只读，不打开）",
    {"dir": {"type": "string", "description": "工程目录，默认 data/graph-projects"}},
    _list_projects,
    scope="workspace",
))
_register(Tool(
    "duplicate_project",
    "创建完整、独立的工程副本；目标为新的.nproj入口，不覆盖已有工程，源工程不变",
    {"source": {"type": "string", "required": True, "description": "源.nproj入口路径"},
     "destination": {"type": "string", "required": True, "description": "新副本.nproj入口路径"}},
    _duplicate_project, writes=True, scope="workspace",
))
_register(Tool(
    "check_project",
    "只读静态检查当前有效图与属性，返回固定修订号和可定位诊断；不创建候选或写入",
    {},
    lambda ops, params: ops.check_project(),
))
_register(Tool(
    "project_info",
    "工程标题、修订号、节点数与原文数",
    {},
    _project_info,
))
_register(Tool(
    "list_nodes",
    "按阅读位置列出节点；可按类型、关键字筛选，返回分类计数",
    {
        "granularity": {"type": "string", "enum": ["scene", "event"], "description": "步进单位"},
        "cut": {"type": "string", "description": "阅读位置（事件编号）；省略为最新"},
        "node_type": {"type": "string", "description": "person/group/item/location/event/action/scene"},
        "query": {"type": "string", "description": "名称或说明里的关键字"},
        "related_only": {"type": "boolean", "description": "只看这一步相关的节点"},
        "limit": {"type": "integer", "description": "最多返回多少条，默认 200"},
    },
    _list_nodes,
))
_register(Tool(
    "get_node",
    "一个节点的全部字段与它在某阅读位置上的属性",
    {
        "node_id": {"type": "string", "required": True},
        "cut": {"type": "string", "description": "阅读位置（事件编号）；省略为最新"},
    },
    _get_node,
))
_register(Tool(
    "node_properties",
    "一个节点在某阅读位置上的属性（失效的不返回）",
    {
        "node_id": {"type": "string", "required": True},
        "cut": {"type": "string"},
    },
    _node_properties,
))
_register(Tool(
    "scene_state",
    "一个场景：它包含的节点，以及这一场结束时各方是什么状态",
    {"scene_id": {"type": "string", "required": True}},
    _scene_state,
))
_register(Tool(
    "event_state",
    "一个事件：相关节点，以及读到这一步时各方是什么状态",
    {"event_id": {"type": "string", "required": True}},
    _event_state,
))
_register(Tool(
    "list_moments",
    "阅读位置的层级：场景 → 事件，一列一层；cut 只能用这里的编号",
    {},
    _list_moments,
))
_register(Tool(
    "node_material",
    "一个节点的资料卡：标题、详情、带说明的图片，各自的有效区间",
    {
        "node_id": {"type": "string", "required": True},
        "cut": {"type": "string", "description": "阅读位置（事件编号）；省略为最新"},
    },
    _material_view,
))
_register(Tool(
    "statistics",
    "统计：节点分类计数、原文、属性记录种类与失效记录数",
    {},
    _statistics,
))
_register(Tool(
    "search",
    "按关键字检索节点与属性记录",
    {
        "query": {"type": "string", "required": True},
        "limit": {"type": "integer", "description": "默认 50"},
    },
    _search,
))
source_tools.register_list_sources(Tool, _register, error_type=ToolError, need=_need, content_receipt=_content_receipt)


_register(Tool(
    "property_categories",
    "每个节点类型的大类表：显示用的分组，不是事实",
    {},
    _property_categories,
))
source_tools.register_reading_tools(Tool, _register, error_type=ToolError, need=_need, content_receipt=_content_receipt)


_EVENT_MERGE_PARAMS = {
    "source_event_id": {"type": "string", "required": True, "description": "要归入已有事件的重复事件ID"},
    "target_event_id": {"type": "string", "required": True, "description": "保留的更早披露事件ID"},
    "expected_revision": {"type": "integer", "required": True},
    "reason": {"type": "string", "required": True, "description": "依据原文说明两ID为何确为同一次发生"},
    "evidence": {"type": "array", "required": True, "minItems": 1,
                 "items": {"type": "object", "required": ["source_id", "start", "end", "quote"],
                    "additionalProperties": False, "properties": {
                        "source_id": {"type": "string"}, "start": {"type": "integer", "minimum": 0},
                        "end": {"type": "integer", "minimum": 1}, "quote": {"type": "string"}}}},
}
_register(Tool("preview_event_merge", "核对原文后预检两个事件ID的显式合并；不按同名自动合并，不写入",
               _EVENT_MERGE_PARAMS, lambda ops, params: _event_merge(ops, params)))
_register(Tool("merge_events", "凭覆盖两事件的逐字证据合并重复事件；合并证据与当前引用，物理删除重复事件，不保留重定向",
               _EVENT_MERGE_PARAMS, lambda ops, params: _event_merge(ops, params, create=True), writes=True))

_register(Tool(
    "audit_properties",
    "检查属性证据与结构：重复值、缺定义、缺证据、锚点缺失等诊断",
    {"node_ids": {"type": "array", "items": {"type": "string"}, "description": "只审这些节点"}},
    _audit,
))

_register(Tool(
    "record_property_change",
    "在指定事件记录属性建立、变更或失效，程序按事件顺序推导状态；不填写有效期",
    {
        "node_id": {"type": "string", "required": True, "description": "属性主体；身份、关系或作用角色中为参照方A"},
        "property_id": {"type": "string", "required": True, "description": "属性名；关系描述对方相对node的身份、关系或作用中的角色：node=A,property=父亲,value=B表示B是A的父亲"},
        "value": {"type": ["string", "number", "boolean", "array"], "items": {"type": ["string", "number", "boolean"]},
                  "description": "one为单值；many为数组。节点引用为被指向的对方，普通字面值不改写"},
        "value_type": {"type": "string", "enum": ["string", "number", "boolean", "node", "either", "any"], "description": "新定义必填，已知定义省略沿用；node/either为节点引用，string/any不能因P样字符串视为关系"},
        "cardinality": {"type": "string", "enum": ["one", "many"], "description": "录入规则，不约束读取形状：one表示同一事件点只允许一个当前值（改就set覆盖，两个不同值报冲突），人物关系按对方分别维护可并存；many表示同一事件点允许多项并存，各元素符合value_type。读取一律返回当前有效的全部值列表。已知定义省略沿用，显式冲突拒绝"},
        "symmetric": {"type": "boolean", "description": "新关系定义是否对称，默认false；已有定义省略沿用，冲突拒绝"},
        "event_id": {"type": "string", "required": True, "description": "披露事件编号"},
        "op": {"type": "string", "enum": ["set", "add", "remove", "clear", "unknown"], "required": True, "description": "set/add/remove须value；clear/unknown禁止value"},
        "expected_revision": {"type": "integer", "required": True},
        "reason": {"type": "string", "required": True},
        "context_id": {"type": "string"},
        "category": {"type": "string", "description": "显示大类，默认按属性名推断"},
        "source": {"type": "string", "description": "narration/speech/thought/observation"},
        "scope": {"type": "string", "enum": ["world", "cognition", "claim"], "description": "客观world、人物所知所信cognition、言说主张claim；非world须给认知者/说话人holder_id，不按对白形式机械分类"},
        "holder_id": {"type": "string", "description": "认知者或说话人ID；node_id仍是被认知/谈论的主体，可与holder_id不同"},
        "quote": {"type": "string", "description": "原文引文（与 source_id 一起给）"},
        "source_id": {"type": "string"},
    },
    _record_property_change,
    writes=True,
))
_register(Tool(
    "correct_property", "更正错误的事件属性记录，保留原sequence；新剧情变化请追加record_property_change",
    {"record_id": {"type": "string", "required": True},
     "changes": {"type": "object", "required": True},
     "expected_revision": {"type": "integer", "required": True},
     "reason": {"type": "string", "required": True}}, _correct_property, writes=True,
))
_register(Tool(
    "delete_property_records", "物理删除错误属性记录；不表示剧情变化",
    {"record_ids": {"type": "array", "items": {"type": "string"}, "required": True},
     "expected_revision": {"type": "integer", "required": True},
     "reason": {"type": "string", "required": True}},
    lambda ops, p: ops.delete_property_records(p["record_ids"], p["reason"],
        expected_revision=p["expected_revision"]), writes=True,
))
_register(Tool(
    "rollback_preview", "只读预览按原文时间点删除后缀数据的范围",
    {"source_id": {"type": "string", "required": True},
     "start": {"type": "integer", "required": True},
     "expected_revision": {"type": "integer", "required": True}},
    lambda ops, p: ops.rollback_preview(p["source_id"], p["start"], p["expected_revision"]),
))
_register(Tool(
    "rollback_to", "按原文时间点物理删除后缀数据并清理引用；先用rollback_preview核对范围",
    {"source_id": {"type": "string", "required": True},
     "start": {"type": "integer", "required": True},
     "expected_revision": {"type": "integer", "required": True},
     "reason": {"type": "string", "required": True}},
    lambda ops, p: ops.rollback_to(p["source_id"], p["start"], p["expected_revision"], p["reason"]), writes=True,
))
_register(Tool(
    "add_material_card",
    "给节点加一张资料卡：标题、详情、生效/失效各挑一个场景或事件",
    {
        "node_id": {"type": "string", "required": True},
        "title": {"type": "string", "required": True},
        "detail": {"type": "string"},
        "valid_from": {"type": "string", "description": "生效时间点（场景或事件编号）"},
        "valid_to": {"type": "string", "description": "失效时间点；空为一直有效"},
        "cut": {"type": "string", "description": "回读时用的阅读位置"},
    },
    _add_material_card,
    writes=True,
))
_register(Tool(
    "update_material_card",
    "改一张资料卡：只改给了的字段",
    {
        "node_id": {"type": "string", "required": True},
        "card_id": {"type": "string", "required": True},
        "title": {"type": "string"},
        "detail": {"type": "string"},
        "valid_from": {"type": "string"},
        "valid_to": {"type": "string"},
        "cut": {"type": "string"},
    },
    _update_material_card,
    writes=True,
))
_register(Tool(
    "delete_material_card",
    "删一张资料卡；它的图片一起走",
    {
        "node_id": {"type": "string", "required": True},
        "card_id": {"type": "string", "required": True},
        "cut": {"type": "string"},
    },
    _delete_material_card,
    writes=True,
))
_register(Tool(
    "add_material_image",
    "给一张资料卡加图：拷进工程，可带一句说明",
    {
        "node_id": {"type": "string", "required": True},
        "card_id": {"type": "string", "required": True},
        "path": {"type": "string", "required": True, "description": "图片文件路径"},
        "caption": {"type": "string"},
        "cut": {"type": "string"},
    },
    _add_material_image,
    writes=True,
))
_register(Tool(
    "delete_material_image",
    "删掉一张资料卡里的某张图",
    {
        "node_id": {"type": "string", "required": True},
        "card_id": {"type": "string", "required": True},
        "image_id": {"type": "string", "required": True},
        "cut": {"type": "string"},
    },
    _delete_material_image,
    writes=True,
))
_register(Tool(
    "revise_node",
    "修订一个节点的名称、类型、别名或说明（立刻生效）",
    {
        "node_id": {"type": "string", "required": True},
        "name": {"type": "string"},
        "node_type": {"type": "string", "enum": sorted(["person", "group", "item", "location", "event", "action", "scene", "big_event", "storyline", "other"])},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "identity_note": {"type": "string"},
    },
    _revise_node,
    writes=True,
))
_register(Tool(
    "relation_map",
    "人物关系网：保留方向；定义明确symmetric的关系显示双向，A父亲B与B儿子A分别保留",
    {"cut": {"type": "string", "description": "阅读位置节点ID；省略表示最新"},
     "related_only": {"type": "boolean", "description": "仅显示与阅读位置相关的人物"}},
    _relation_map,
))
_register(Tool(
    "attach_children",
    "把已有节点归入直属容器：主要故事→大事件→场景→事件→动作；静态校验后直接提交",
    {
        "parent_id": {"type": "string", "required": True},
        "child_ids": {"type": "array", "items": {"type": "string"}, "required": True},
        "replace": {"type": "boolean", "description": "true 时只留这些子节点，false 时追加（默认）"},
    },
    _attach_children,
    writes=True,
))
_register(Tool(
    "merge_properties", "仅合并已查证同义且schema一致的属性定义及引用；相同主体或对象不是同义依据，不同关系性质须保留",
    {"source_id": {"type": "string", "required": True},
     "target_id": {"type": "string", "required": True},
     "reason": {"type": "string", "required": True},
     "expected_revision": {"type": "integer", "required": True}},
    _merge, writes=True,
))
_register(Tool(
    "rename_property", "物理重命名属性定义并更新引用",
    {"from_id": {"type": "string", "required": True},
     "to_id": {"type": "string", "required": True},
     "reason": {"type": "string", "required": True},
     "expected_revision": {"type": "integer", "required": True}},
    _rename, writes=True,
))
_register(Tool(
    "categorize_records",
    "把记录移到某个显示大类",
    {
        "ids": {"type": "array", "items": {"type": "string"}, "required": True},
        "category": {"type": "string", "required": True},
    },
    _categorize,
    writes=True,
))
source_tools.register_import_source(Tool, _register, error_type=ToolError, need=_need, content_receipt=_content_receipt)


for _draft_name, _draft_handler, _draft_writes in (
    ("preview_extraction", _preview_extraction, False),
    ("submit_extraction", _submit_extraction, True),
):
    _register(Tool(
        _draft_name,
        ("可选单独试查一个事实批次的原文证据、引用、结构与失效锚点，只返回诊断；不是提交前置" if not _draft_writes else
         "直接提交一个语境与引用完整的已核实事实批次，支持同任务增量多次提交；source_id/start/end放外层，result仅含草稿字段。后台自动定位逐字引文、静态校验；合法自动原子入库，问题批持久暂存并返回submission_id/submission_revision及问题项，后续只patch修补而不重发整批，无需先locate_source或preview"),
        submission_parameters(),
        _draft_handler, writes=_draft_writes, input_schema=submission_input_schema(),
    ))


_SUBMISSION_SECTIONS = ['nodes', 'properties', 'uncertainties', 'exclusions']
_register(Tool('list_extraction_submissions', '仅需找回未完成暂存时列出pending摘要，支持原文过滤与分页，不返回整稿，不是例行提取前置。', {
    'source_id': {'type': 'string', 'description': '可选，仅列该原文；省略列本工程全部未解决暂存'},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'description': '每页条数，默认20'},
    'after': {'type': 'integer', 'minimum': 0, 'description': '上一页next_after整数游标；首请求0'},
}, _list_extraction_submissions))
_PATCH_BASE = {'section': {'type': 'string', 'enum': _SUBMISSION_SECTIONS}}
_EXTRACTION_PATCH_ITEM = {'oneOf': [
    {'type': 'object', 'additionalProperties': False, 'required': ['op', 'path'],
     'properties': {'op': {'type': 'string', 'const': 'remove'},
                    'path': {'type': 'array', 'minItems': 1, 'maxItems': 1,
                             'items': {'type': 'string', 'not': {'enum': _SUBMISSION_SECTIONS}},
                             'description': '仅删除result内未知顶层字段，例如["result_note"]；不能删除正式草稿字段或修改外层目标。'}}},
    {'type': 'object', 'additionalProperties': False, 'required': ['op', 'section', 'index', 'field', 'value'],
     'properties': _PATCH_BASE | {'op': {'type': 'string', 'const': 'set'}, 'index': {'type': 'integer', 'minimum': 0},
                                 'field': {'type': 'string'}, 'value': {}}},
    {'type': 'object', 'additionalProperties': False, 'required': ['op', 'section', 'index', 'field'],
     'properties': _PATCH_BASE | {'op': {'type': 'string', 'const': 'remove'}, 'index': {'type': 'integer', 'minimum': 0},
                                 'field': {'type': 'string'}}},
    {'type': 'object', 'additionalProperties': False, 'required': ['op', 'section', 'value'],
     'properties': _PATCH_BASE | {'op': {'type': 'string', 'const': 'append'}, 'value': {'type': 'object'}}},
]}
_register(Tool('read_extraction_submission', '仅缺少局部内容时读取暂存状态/问题/指定行，非patch前置；反馈已足够则直接patch，默认不回传整稿。', {
    'submission_id': {'type': 'string', 'required': True, 'description': 'submit_extraction返回的暂存编号'},
    'section': {'type': 'string', 'enum': _SUBMISSION_SECTIONS, 'description': '默认只返回状态和问题；指定才读取该部分'},
    'indices': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0}, 'description': 'section内0基行号；node reference仍为原1基@n'},
}, _read_extraction_submission))
_register(Tool('patch_extraction_submission', '反馈足够时直接一次patch数组修多个问题，无需先read；只改字段或追加项，保留索引和@n引用，自动重检入库，不重发整批。', {
    'submission_id': {'type': 'string', 'required': True},
    'expected_revision': {'type': 'integer', 'required': True, 'minimum': 1, 'description': '刚读取的submission_revision，不是正式工程revision'},
    'patches': {'type': 'array', 'required': True, 'minItems': 1, 'items': _EXTRACTION_PATCH_ITEM,
                'description': 'set/remove改index行的field；append追加对象；remove配单元素path仅删除result未知顶层字段。禁止删整行、重排或splice；index0基，不修改外层source_id/start/end。'},
    'reason': {'type': 'string', 'required': True, 'description': '局部修补依据'},
}, _patch_extraction_submission, writes=True))

_register(Tool('delete_extraction_submission', '物理删除指定未入库提取暂存，保留正式事实及原任务历史；即时原子命令，必须明确暂存修订和原因。已入库暂存不可删除。', {
    'submission_id': {'type': 'string', 'required': True},
    'expected_revision': {'type': 'integer', 'required': True, 'minimum': 1,
                          'description': '待删除暂存的submission_revision，不是工程revision'},
    'reason': {'type': 'string', 'required': True},
}, lambda ops, params: ops.delete_extraction_submission(**params), writes=True))

_CONTENT_PATH = {'type': 'array', 'minItems': 1, 'items': {'type': ['string', 'integer']},
                 'description': '从原Operations绑定arguments根开始的字段路径；数组索引从0起，不重排既有条目'}
_CONTENT_PATCH = {'type': 'object', 'additionalProperties': False,
    'properties': {'op': {'type': 'string', 'enum': ['set', 'remove', 'append']}, 'path': _CONTENT_PATH, 'value': {}},
    'required': ['op', 'path'], 'oneOf': [
        {'properties': {'op': {'enum': ['set', 'append']}}, 'required': ['value']},
        {'properties': {'op': {'const': 'remove'}}, 'not': {'required': ['value']}}]}

_register(Tool('list_content_submissions', '仅需恢复未完成业务内容时，分页列出暂存摘要；不回传正文整稿，不是每次保存的前置步骤。', {
    'operation': {'type': ['string', 'null']}, 'target': {'type': ['string', 'object', 'null']},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'after': {'type': 'integer', 'minimum': 0},
}, _list_content_submissions))
_register(Tool('read_content_submission', '查看统一业务内容暂存的状态与问题；默认仅摘要，paths只读取确实需要的局部字段。', {
    'submission_id': {'type': 'string', 'required': True},
    'paths': {'type': ['array', 'null'], 'items': _CONTENT_PATH | {'minItems': 0,
        'description': '显式读取的字段路径；空路径[]仅在确需完整表单恢复时读取全部绑定参数，默认不返回整稿'}},
}, _read_content_submission))
_register(Tool('patch_content_submission', '反馈足够则一次局部patch修多个问题，无需先read或重发整稿；重检通过即自动原子入库。不能修改operation、目标、权限、修订与依据，也不能变成删除/移动等命令。', {
    'submission_id': {'type': 'string', 'required': True},
    'expected_revision': {'type': 'integer', 'minimum': 1, 'required': True, 'description': '暂存submission_revision，不是业务数据修订号'},
    'patches': {'type': 'array', 'minItems': 1, 'items': _CONTENT_PATCH, 'required': True},
    'reason': {'type': 'string', 'minLength': 1, 'required': True},
}, _patch_content_submission, writes=True))


_CHANGE_PARAMS = {
    "graph_objects": {"type": "array", "items": {"type": "object"}, "required": True,
                      "description": "完整新增或更新对象；kind可为node/context/timeline/source_order，结构见get_schema；节点events表达嵌套归属"},
    "property_records": {"type": "array", "items": {"type": "object"}, "required": True,
                         "description": "追加或更正合法property_definition/property_change；新变更无需id，由程序分配PC编号；event_id/op/sequence记录事件事实，程序按事件顺序推导状态，无有效期字段；结构见get_schema"},
    "expected_revision": {"type": "integer", "required": True,
                          "description": "刚读取的工程修订号，过期即拒绝，不覆盖他人修改"},
}


def _project_changes(ops, params, *, write=False):
    _need(params, "expected_revision")
    try:
        args = {key: params.get(key, []) for key in ("graph_objects", "property_records")}
        if write:
            return ops.apply_changes(**args, expected_revision=params["expected_revision"], reason=params.get("reason", ""))
        return ops.preview_changes(**args, expected_revision=params["expected_revision"])
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


_register(Tool("get_schema", "读取当前图对象和属性记录的结构合同、字段及图JSON schema，不写入", {},
               lambda ops, params: ops.get_schema()))
_register(Tool("read_project_state", "读取当前完整图结构与原始属性事件变更记录，用于查漏补缺；图的投影审计字段不可原样写回", {
    },
    lambda ops, params: _read_project_state(ops, params)))
_register(Tool("preview_changes", "对通用工程修改做静态预检：证据、引用、结构及修订号；不写入", _CHANGE_PARAMS,
               lambda ops, params: _project_changes(ops, params)))
_register(Tool("apply_changes", "静态校验后原子写入通用图对象与属性记录，保留变更审计，不等待人工接受", _CHANGE_PARAMS | {
    "reason": {"type": "string", "required": True, "description": "这批修改的依据与目的，记入审计"}},
    lambda ops, params: _project_changes(ops, params, write=True), writes=True))


source_tools.finalize_source_parameters(TOOLS)
for _name in ("preview_extraction", "submit_extraction"):
    TOOLS[_name].params["source_id"] = {"type": "string", "description": "真实SRC编号；工程恰好一份原文时可省略，多份必须指定"}


def _read_project_state(ops, _params: dict) -> dict:
    state = ops.read_project_state()
    graph = {key: value for key, value in state["graph"].items()
             if key in {"revision", "schema_version", "objects", "sources"}}
    graph["sources"] = [{key: source[key] for key in ("id", "title", "length", "imported_revision")
                         if key in source} for source in graph.get("sources", [])]
    return state | {"graph": graph}


# Adaptation and visual assets share the same public Operations boundary.
def _validate_adaptation_argument(value, spec, path):
    """Enforce the published transport schema before invoking Operations."""
    kind = spec.get("type")
    types = kind if isinstance(kind, list) else [kind]
    valid = {"string": isinstance(value, str), "integer": type(value) is int,
             "boolean": type(value) is bool, "object": isinstance(value, dict),
             "array": isinstance(value, list), "null": value is None,
             "number": type(value) in (int, float)}
    if kind and not any(valid.get(item, False) for item in types):
        raise ToolError(f"{path} 类型须为 {kind}")
    if "enum" in spec and value not in spec["enum"]:
        raise ToolError(f"{path} 须为 {spec['enum']}")
    if isinstance(value, dict) and "properties" in spec:
        fields = spec["properties"]
        if spec.get("additionalProperties") is False and set(value) - set(fields):
            raise ToolError(f"{path} 有不认识的字段：{sorted(set(value) - set(fields))}")
        for key, child in value.items():
            if key in fields:
                _validate_adaptation_argument(child, fields[key], f"{path}.{key}")
    if isinstance(value, list) and "items" in spec:
        for index, child in enumerate(value):
            _validate_adaptation_argument(child, spec["items"], f"{path}[{index}]")


_register_adaptation_tools(Tool, _register, _validate_adaptation_argument, ToolError)
_register_asset_tools(Tool, _register, _validate_adaptation_argument, ToolError)

def _extraction_settings(ops, params, *, write=False):
    settings = ops.load_project_settings()
    if write:
        granularity = params["granularity"]
        if not isinstance(granularity, str) or granularity not in {"standard", "fine"}:
            raise ToolError("提取粒度须为standard或fine")
        settings = ops.save_project_settings(settings | {"extraction_granularity": granularity})
    return {"extraction_granularity": settings["extraction_granularity"]}


_register(Tool("get_extraction_settings", "读取工程提取粒度，默认fine精细；不返回供应商密钥", {},
               lambda ops, args: _extraction_settings(ops, args)))
_register(Tool("set_extraction_granularity", "设置工程提取粒度，保留当前模型配置和已提取数据", {
    "granularity": {"type": "string", "enum": ["standard", "fine"], "required": True}},
    lambda ops, args: _extraction_settings(ops, args, write=True), writes=True))


_register_screenplay_tools(Tool, _register, _validate_adaptation_argument)
_register_host_tools(Tool, _register, _validate_adaptation_argument)
_register_attachment_tools(Tool, _register, _validate_adaptation_argument)


def _assistant_access_configuration(name, params, writes=False, summary=None):
    def invoke(ops, args):
        for key, value in args.items():
            _validate_adaptation_argument(value, params[key], key)
        try:
            return getattr(ops, name)(**args)
        except (ValueError, OSError) as error:
            raise ToolError(str(error)) from error
    _register(Tool(name, summary or ('读取聊天访问模式' if not writes else '设置聊天下一任务的访问模式；不改变活动任务，模型不可自提升'),
                   params, invoke, writes=writes, scope='configuration'))


_assistant_access_configuration('read_assistant_access', {'chat_id': {'type': ['string', 'null']}})
_assistant_access_configuration('set_assistant_access', {
    'chat_id': {'type': 'string', 'required': True},
    'access_mode': {'type': 'string', 'enum': ['standard', 'full'], 'required': True},
    'reason': {'type': 'string'}}, writes=True)

_assistant_access_configuration('import_chat_attachments', {
    'chat_id': {'type': 'string', 'required': True},
    'paths': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'required': True},
    'task_id': {'type': ['string', 'null']},
}, writes=True, summary='显式导入用户选择的聊天附件，复制原件不覆盖；此操作不向助手模型开放任意外部文件导入')


def _configure_project_api_key(ops, args):
    for name, value in args.items():
        _validate_adaptation_argument(value, TOOLS['configure_project_api_key'].params[name], name)
    secret = args.get('api_key') or ''
    try:
        current = ops.load_project_settings()
        saved = ops.save_project_settings({key: current[key] for key in ('profile', 'extraction_granularity')},
                                         api_key=secret, clear_api_key=args.get('clear_api_key', False))
        return saved['api_key_status']
    except (ValueError, OSError) as error:
        message = str(error)
        for value in (secret, secret.strip()):
            if value:
                message = message.replace(value, '[REDACTED]')
        raise ToolError(message) from None


_register(Tool('read_project_api_key_status', '读取本工程当前模型服务的密钥可用状态与来源，不返回密钥或密文', {},
               lambda ops, args: ops.provider_api_key_status(), scope='configuration'))
_register(Tool('configure_project_api_key', '显式设置或清除本工程当前服务地址的本机加密API Key；不向助手模型开放，不回显密钥', {
    'api_key': {'type': 'string', 'writeOnly': True}, 'clear_api_key': {'type': 'boolean'},
}, _configure_project_api_key, writes=True, scope='configuration'))


def _check_project_api_key(ops, args):
    for name, value in args.items():
        _validate_adaptation_argument(value, TOOLS['check_project_api_key'].params[name], name)
    return ops.check_model_credential(api_key=args.get('api_key'), ignore_saved_key=args.get('ignore_saved_key', False))


_register(Tool('check_project_api_key', '向当前服务和模型发送一次最小认证检查；不保存配置，不进入聊天，不重试，返回状态及用量而不回显密钥', {
    'api_key': {'type': 'string', 'writeOnly': True}, 'ignore_saved_key': {'type': 'boolean'},
}, _check_project_api_key, writes=True, scope='configuration'))


def _list_chat_templates(ops, args):
    from .chat_templates import list_all

    return {'templates': list_all(ops)}


def _save_chat_template(ops, args):
    from .chat_templates import save_custom, validate

    scope = str(args.get('scope') or '').strip()
    if scope not in {'app', 'project'}:
        raise ToolError('模板作用域须为app或project')
    payload = validate({key: value for key, value in args.items() if key != 'scope'})
    save_custom(scope, payload['id'], payload, ops)
    return {'template': payload, 'scope': scope}


def _delete_chat_template(ops, args):
    from .chat_templates import delete_custom

    scope = str(args.get('scope') or '').strip()
    if args.get('confirm') is not True:
        raise ToolError('删除自定义模板须显式传confirm=true；内置模板不可删除')
    if delete_custom(scope, str(args.get('id') or '').strip(), ops):
        return {'deleted': True, 'id': args['id'], 'scope': scope}
    raise ToolError('该作用域下没有这个自定义模板')


_register(Tool('list_chat_templates', '列出会话模板：内置与自定义、作用域、工具数与系统提示词摘要。不返回完整提示词。', {},
               _list_chat_templates, scope='configuration'))
_register(Tool('save_chat_template', '保存会话模板（新建或更新）：scope=app写应用级配置文件、scope=project写进本工程，随工程走。'
               '工具名须都在注册表里；提示词用prompt_keys引用既有条目（推荐）或给出自由文本；内置模板不可覆盖。', {
    'id': {'type': 'string', 'required': True, 'description': '小写字母开头，只含小写字母/数字/短横线'},
    'name': {'type': 'string', 'required': True},
    'description': {'type': 'string'},
    'scope': {'type': 'string', 'enum': ['app', 'project'], 'required': True},
    'tools': {'type': 'array', 'items': {'type': 'string'}, 'required': True},
    'prompt_keys': {'type': 'array', 'items': {'type': 'string'}, 'description': '按顺序拼接的提示词条目名，推荐'},
    'system_prompt': {'type': 'string', 'description': '未用prompt_keys时的自由文本'},
}, _save_chat_template, writes=True, scope='configuration'))
_register(Tool('delete_chat_template', '删除自定义会话模板；内置模板不可删除，删除须显式confirm=true。', {
    'id': {'type': 'string', 'required': True},
    'scope': {'type': 'string', 'enum': ['app', 'project']},
    'confirm': {'type': 'boolean', 'required': True},
}, _delete_chat_template, writes=True, scope='configuration'))


def _register_loading_tools():
    from .tool_loading import register
    register(Tool, _register)


_register_loading_tools()


def describe() -> list[dict]:
    """The tool list in the shape MCP asks for."""
    return [
        {"name": tool.name, "description": tool.summary, "inputSchema": tool.schema()}
        for tool in TOOLS.values()
    ]


def call(name: str, params: dict | None = None, *, project: str | Path | None = None) -> dict:
    """Run one tool. ``project`` is a project file, not a directory."""
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(f"没有这个工具：{name}")
    if params is not None and not isinstance(params, dict):
        raise ToolError("参数须为 JSON 对象")
    args = dict(params or {})
    unknown = set(args) - set(tool.params)
    if unknown:
        raise ToolError("不认识的参数：" + "、".join(sorted(unknown)))
    for key, spec in tool.params.items():
        if spec.get("required") and (key not in args or args[key] is None):
            raise ToolError(f"缺少参数：{key}")
    if tool.scope == "workspace":
        return tool.handler(None, args)
    if project in (None, ""):
        raise ToolError("请提供 --project（.nproj 工程入口文件）")
    path = Path(project)
    if not path.exists():
        raise ToolError(f"工程不存在：{path}")
    # Public tools accept the private entry file; database paths remain internal.
    ops = open_graph_project(path)
    try:
        from .tool_fingerprint_refs import decode_tool_parameters, encode_tool_result
        try:
            resolved = decode_tool_parameters(ops, args)
            from .model_tool_schema import PROVENANCE_TOOLS
            from .tool_execution_context import current_tool_execution_metadata
            metadata = current_tool_execution_metadata() if name in PROVENANCE_TOOLS else {}
            for key, value in metadata.items():
                if key in tool.params:
                    resolved[key] = value
            result = tool.handler(ops, resolved)
            return encode_tool_result(ops, result)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
    finally:
        close = getattr(ops, "close", None)
        if callable(close):
            close()


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
