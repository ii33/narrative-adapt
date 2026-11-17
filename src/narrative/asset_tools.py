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
    _ASSET_PAYLOAD = {"type": "object", "required": True, "description": "无id新建，有id编辑；node_id可省略建立独立创作资产，不创建提取或改编事实；图片传本地path或已有image id，省略图片字段保留原图",
        "properties": {"id": {"type": "string"}, "category": {"type": "string", "enum": ["appearance", "item", "location", "material"]},
                       "node_id": {"type": "string"}, "title": {"type": "string"}, "detail": {"type": "string"},
                       "domain": _ASSET_DOMAIN, "valid_from": {"type": "string"}, "valid_to": {"type": "string"},
                       "prompt": {"type": "string"}, "images": {"type": "array", "items": _ASSET_IMAGE},
                       "reference_images": {"type": "array", "items": _ASSET_IMAGE}, "revision": {"type": "integer"}}, "additionalProperties": False}
    _adaptation_tool("list_assets", "按类别、角色与改编时间点读取视觉设定卡", {
        "category": {"type": "string", "enum": ["appearance", "item", "location", "material"], "required": True},
        "moment": {"type": "string"}, "node_id": {"type": "string"}, "domain": _ASSET_DOMAIN})
    _adaptation_tool("search_assets", "按关键词、类别、节点、有无图片、是否已被镜头引用检索资产；不要求指定类别，用于『这个对象有没有现成资产』", {
        "keyword": {"type": "string", "description": "匹配标题与说明"},
        "category": {"type": "string", "enum": ["appearance", "item", "location", "material"]},
        "node_id": {"type": "string"}, "domain": _ASSET_DOMAIN,
        "has_images": {"type": "boolean", "description": "true只要有图的，false只要没图的"},
        "unbound": {"type": "boolean", "description": "true只列没被任何镜头引用的"}})
    _adaptation_tool("get_asset", "读取视觉设定、图片、参考图、提示词，以及被哪些场次/镜头元素引用", {
        "asset_id": {"type": "string", "required": True}})
    _adaptation_tool("update_asset", "局部修改资产：字段与图片分开改，不用重发整张卡", {
        "asset_id": {"type": "string", "required": True},
        "expected_revision": {"type": "integer", "description": "资产独立修订号，来自get_asset/search_assets"},
        "changes": {"type": "object", "description": "可改 title/node_id/category/detail/prompt/valid_from/valid_to",
                    "properties": {"title": {"type": "string"}, "node_id": {"type": "string"},
                                   "category": {"type": "string", "enum": ["appearance", "item", "location", "material"]},
                                   "detail": {"type": "string"}, "prompt": {"type": "string"},
                                   "valid_from": {"type": "string"}, "valid_to": {"type": "string"}},
                    "additionalProperties": False},
        "image_changes": {"type": "array", "description": "图片操作：add（给path新增）、adopt（设为第一张）、remove（按image_id删除）",
                          "items": {"type": "object", "properties": {"op": {"type": "string", "enum": ["add", "adopt", "remove"]},
                                                                     "image_id": {"type": "string"}, "path": {"type": "string"},
                                                                     "caption": {"type": "string"}},
                                    "required": True, "additionalProperties": False}}}, writes=True)
    _adaptation_tool("save_asset", "新增或编辑独立视觉资产；参考图复制保存，原件保留", {
        "payload": _ASSET_PAYLOAD, "expected_revision": {"type": "integer", "description": "资产独立修订号，来自list_assets/get_asset"}}, writes=True)
    _adaptation_tool("delete_asset", "删除独立视觉设定卡，保留原始附件；被引用时先列位置，force 才连绑定一起解除", {
        "asset_id": {"type": "string", "required": True}, "expected_revision": {"type": "integer"},
        "force": {"type": "boolean", "description": "true 时解除分镜里的绑定再删"}}, writes=True)
    _adaptation_tool("bind_storyboard_element", "把已有资产绑到某镜的某个元素；类别不符或已定案时拒绝", {
        "unit_id": {"type": "string", "required": True}, "shot_id": {"type": "string", "required": True},
        "index": {"type": "integer", "required": True}, "asset_id": {"type": "string", "required": True},
        "expected_revision": {"type": "integer", "description": "分镜修订号，来自read_storyboard"}}, writes=True)
    _adaptation_tool("unbind_storyboard_element", "解除某镜某个元素上的资产绑定", {
        "unit_id": {"type": "string", "required": True}, "shot_id": {"type": "string", "required": True},
        "index": {"type": "integer", "required": True}, "expected_revision": {"type": "integer"}}, writes=True)
    _adaptation_tool("generate_asset_sketch", "用图像服务按描述型素描生成资产形象图并写回资产卡；已有图时新增为候选，不覆盖", {
        "asset_id": {"type": "string"}, "node_id": {"type": "string"},
        "prompt": {"type": "string", "description": "本次绘制的补充要求"}}, writes=True)
    _adaptation_tool("asset_moments", "读取按改编顺序排列的资产时间点", {"domain": _ASSET_DOMAIN})
