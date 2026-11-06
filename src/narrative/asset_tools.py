"""Visual asset tools using the public Operations boundary."""
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


    _ASSET_DOMAIN = {"type": "string", "enum": ["adaptation", "extraction"], "description": "默认adaptation；两数据域不互相写入"}
    _ASSET_IMAGE = {"type": "object", "properties": {"id": {"type": "string"}, "path": {"type": "string"},
                    "file": {"type": "string"}, "caption": {"type": "string"}}, "additionalProperties": False}
    _ASSET_PAYLOAD = {"type": "object", "required": True, "description": "无id新建，有id编辑；图片传本地path或已有image id，省略图片字段保留原图",
        "properties": {"id": {"type": "string"}, "category": {"type": "string", "enum": ["appearance", "item", "location", "material"]},
                       "node_id": {"type": "string"}, "title": {"type": "string"}, "detail": {"type": "string"},
                       "domain": _ASSET_DOMAIN, "valid_from": {"type": "string"}, "valid_to": {"type": "string"},
                       "prompt": {"type": "string"}, "images": {"type": "array", "items": _ASSET_IMAGE},
                       "reference_images": {"type": "array", "items": _ASSET_IMAGE}, "revision": {"type": "integer"}}, "additionalProperties": False}
    _adaptation_tool("list_assets", "按类别、角色与改编时间点读取视觉设定卡", {
        "category": {"type": "string", "enum": ["appearance", "item", "location", "material"], "required": True},
        "moment": {"type": "string"}, "node_id": {"type": "string"}, "domain": _ASSET_DOMAIN})
    _adaptation_tool("get_asset", "读取视觉设定、图片、参考图和提示词", {
        "asset_id": {"type": "string", "required": True}})
    _adaptation_tool("save_asset", "新增或编辑独立视觉资产；参考图复制保存，原件保留", {
        "payload": _ASSET_PAYLOAD, "expected_revision": {"type": "integer", "description": "资产独立修订号，来自list_assets/get_asset"}}, writes=True)
    _adaptation_tool("delete_asset", "删除独立视觉设定卡，保留原始附件", {
        "asset_id": {"type": "string", "required": True}, "expected_revision": {"type": "integer"}}, writes=True)
    _adaptation_tool("asset_moments", "读取按改编顺序排列的资产时间点", {"domain": _ASSET_DOMAIN})
