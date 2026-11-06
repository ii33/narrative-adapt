"""Adaptation and asset tool catalog; handlers use public Operations."""
from __future__ import annotations


def register(Tool, register_tool, validate, ToolError):
    def _adaptation_tool(name, summary, params, *, writes=False):
        def invoke(ops, args):
            for key, value in args.items():
                validate(value, params[key], key)
            try:
                result = getattr(ops, name)(**args)
                return {"moments": result} if name in {"asset_moments", "import_adaptation_candidates"} else result
            except (ValueError, OSError) as exc:
                raise ToolError(str(exc)) from exc
        register_tool(Tool(name, summary, params, invoke, writes=writes))


    _ADAPT_REV = {"expected_revision": {"type": "integer", "description": "改编独立修订号，来自adaptation_snapshot；不是提取revision"},
                  "reason": {"type": "string", "description": "操作原因，记入审计"}}
    _ADAPT_NODE = {"node_id": {"type": "string", "required": True}}
    _ADAPT_MOVE = {"after_id": {"type": ["string", "null"], "description": "改编节点ID；省略或null放到开头（复制默认紧随原节点）"},
                   "parent_id": {"type": ["string", "null"], "description": "新父节点ID；省略或null保留原归属"}}
    _adaptation_tool("adaptation_snapshot", "读取独立改编节点、排序、属性及同步冲突", {})
    _adaptation_tool("check_adaptation", "静态检查改编结构、引用和属性有效区间", {})
    _adaptation_tool("load_project_style", "读取工程共享风格约定及独立修订；单剧本特殊要求保留在creative_brief", {})
    _adaptation_tool("save_project_style", "保存工程共享风格约定，可空；使用独立修订并记录审计，不改正文", {
        "text": {"type": "string", "required": True}, "expected_revision": {"type": "integer", "required": True},
        "reason": {"type": "string"}}, writes=True)
    _adaptation_tool("load_prompt_settings", "读取工程可编辑提示词、内置默认、有效值和设置修订", {})
    _adaptation_tool("save_prompt_override", "修改单项工程提示词；text=null恢复默认；保留设置修订检查与审计，下一任务使用新配置", {
        "prompt_id": {"type": "string", "required": True},
        "text": {"type": ["string", "null"], "required": True},
        "expected_revision": {"type": "integer", "required": True}, "reason": {"type": "string"}}, writes=True)
    _adaptation_tool("import_adaptation_candidates", "列出可多选导入的提取时间点及层级；不写入", {})
    _adaptation_tool("import_adaptation", "从提取导入指定多个时间点、必要结构与相关有效状态，保留本地改编", _ADAPT_REV | {
        "moment_ids": {"type": "array", "items": {"type": "string"}, "required": True,
                       "description": "从import_adaptation_candidates选择的非空时间点ID列表；容器包含下属，相关依赖与必要前置状态锚点由程序补齐"}}, writes=True)
    _adaptation_tool("adaptation_browse_view", "读取与数据浏览同结构的改编视图，排序与状态按改编sort_id计算", {
        "granularity": {"type": "string", "enum": ["scene", "event", "action"]},
        "cut_id": {"type": ["string", "null"]}, "node_type": {"type": "string"}, "query": {"type": "string"},
        "related_only": {"type": "boolean"}, "time_filter": {"type": "boolean"}, "include_suppressed": {"type": "boolean"}})
    for _view_name in ("adaptation_node_properties", "adaptation_node_editor", "adaptation_node_detail", "adaptation_node_assets"):
        _adaptation_tool(_view_name, "读取改编节点详情、时点状态或关联资产；不读取提取资料卡", _ADAPT_NODE | {
            "cut": {"type": ["string", "null"], "description": "改编阅读切点ID，省略为当前"}})
    _adaptation_tool("sync_adaptation", "增量同步提取到改编；保留改编编辑，记录冲突", _ADAPT_REV, writes=True)
    _adaptation_tool("edit_adaptation_node", "新增或编辑改编节点；使用独立sort_id排序，不修改提取", _ADAPT_REV | _ADAPT_MOVE | {
        "node_id": {"type": ["string", "null"], "description": "省略或null为新增；已有节点传改编ID"}, "changes": {"type": "object", "required": True, "description": "节点当前字段，如name/node_type/events/participants；point_id指定出场时点；新物品initial_holder须明确节点ID、无或未知；id/sort_id由程序分配"}}, writes=True)
    for _operation in ("copy_adaptation_node", "move_adaptation_node"):
        _adaptation_tool(_operation, "复制或移动独立改编节点，重排改编顺序", _ADAPT_REV | _ADAPT_NODE | _ADAPT_MOVE, writes=True)
    _adaptation_tool("delete_adaptation_node", "显式物理删除改编节点及包含子树并清理引用；保留剧本正文，返回失效时间段诊断", _ADAPT_NODE | {
        "expected_revision": {"type": "integer", "required": True}, "reason": {"type": "string", "required": True}}, writes=True)
    _adaptation_tool("suppress_adaptation_node", "屏蔽或恢复改编节点及整个包含子树，不影响提取", _ADAPT_REV | _ADAPT_NODE | {
        "suppressed": {"type": "boolean"}}, writes=True)
    _adaptation_tool("set_adaptation_property", "在改编事件记录属性变化，程序按事件顺序推导；record_id仅用于更正错误记录", _ADAPT_REV | _ADAPT_NODE | {
        "property_id": {"type": "string", "required": True}, "value": {},
        "event_id": {"type": "string", "required": True},
        "op": {"type": "string", "enum": ["set", "add", "remove", "clear", "unknown"], "required": True},
        "record_id": {"type": ["string", "null"]},
        "scope": {"type": "string", "enum": ["world", "cognition", "claim"]},
        "holder_id": {"type": ["string", "null"]}, "context_id": {"type": "string"}}, writes=True)
    _adaptation_tool("delete_adaptation_property_changes", "物理删除错误改编属性变更，不代表剧情失效", _ADAPT_REV | {
        "record_ids": {"type": "array", "items": {"type": "string"}, "required": True}}, writes=True)
