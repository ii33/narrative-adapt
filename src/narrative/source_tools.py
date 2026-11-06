"""Original-source tool contracts and forwarding; business remains in Operations."""
from __future__ import annotations


def source_id(ops, params: dict, error_type) -> str:
    supplied = params.get("source_id")
    if supplied not in (None, ""):
        if not isinstance(supplied, str):
            raise error_type("source_id须为真实原文编号")
        return supplied
    sources = ops.sources()
    if len(sources) != 1:
        raise error_type("仅一份原文时可省略source_id；当前须明确提供真实SRC编号")
    return sources[0]["id"]


class _SourceHandlers:
    def __init__(self, error_type, need, content_receipt):
        self.error_type = error_type
        self.need = need
        self.content_receipt = content_receipt

    def _source_id(self, ops, params):
        return source_id(ops, params, self.error_type)

    def _import_source(self, ops, params: dict) -> dict:
        self.need(params, "title", "text")
        source_id = ops.import_source(str(params["title"]), str(params["text"]))
        return {"status": "accepted", "source_id": source_id}

    def _list_sources(self, ops, _params: dict) -> dict:
        return {"sources": [
            {"id": source["id"], "title": source.get("title"), "chars": source.get("length")}
            for source in ops.sources()
        ]}

    def _read_source(self, ops, params: dict) -> dict:
        source = ops.source(self._source_id(ops, params))
        text = source.get("text", "")
        start = max(0, int(params.get("start") or 0))
        length = max(1, int(params.get("length") or 2000))
        return {
            "id": source["id"],
            "title": source.get("title"),
            "chars": len(text),
            "start": start,
            "text": text[start:start + length],
        }

    def _locate_source(self, ops, params: dict) -> dict:
        """One source read and bounded quote batches; no hash or evidence objects."""
        self.need(params, "quotes")
        source_id = self._source_id(ops, params)
        return {"source_id": source_id,
                "results": ops.locate(source_id, params["quotes"], limit_per_quote=params.get("limit_per_quote", 20))}

    def _list_source_slices(self, ops, params: dict) -> dict:
        source_id = self._source_id(ops, params)
        try:
            return {"source_id": source_id, "slices": ops.source_slices(source_id)}
        except (ValueError, KeyError) as exc:
            raise self.error_type(str(exc)) from exc

    def _read_source_slice(self, ops, params: dict) -> dict:
        self.need(params, "slice_id")
        try:
            return ops.source_slice(self._source_id(ops, params), str(params["slice_id"]))
        except (ValueError, KeyError) as exc:
            raise self.error_type(str(exc)) from exc

    def _create_source_slices(self, ops, params: dict) -> dict:
        self.need(params, "slices")
        slices = params["slices"]
        source_id = self._source_id(ops, params)
        try:
            created = ops.create_source_slices(source_id, slices)
        except (ValueError, KeyError) as exc:
            raise self.error_type(str(exc)) from exc
        if isinstance(created, dict):
            if created.get('status') == 'staged':
                return created
            return {'source_id': source_id, 'slices': created.get('result', []), **self.content_receipt(created)}
        return {"status": "created", "source_id": source_id, "slices": created}

    def _pattern_slices(self, ops, params: dict, *, create: bool = False) -> dict:
        from .pattern_slices import compact

        self.need(params, "pattern")
        args = {key: params[key] for key in ("pattern", "start_index", "limit") if key in params}
        args["source_id"] = self._source_id(ops, params)
        try:
            if create:
                result = ops.create_pattern_slices(**args)
            else:
                result = ops.preview_pattern_slices(**args)
            return compact(result)
        except (ValueError, KeyError) as exc:
            raise self.error_type(str(exc)) from exc


def register_list_sources(Tool, register_tool, *, error_type, need, content_receipt):
    handlers = _SourceHandlers(error_type, need, content_receipt)
    register_tool(Tool(
        "list_sources",
        "列出工程里的原文",
        {},
        handlers._list_sources,
    ))


def register_reading_tools(Tool, register_tool, *, error_type, need, content_receipt):
    handlers = _SourceHandlers(error_type, need, content_receipt)
    register_tool(Tool(
        "read_source",
        "读取一段原文（按字符位置切片）",
        {
            "source_id": {"type": "string", "required": True},
            "start": {"type": "integer", "description": "起始字符位置，默认 0"},
            "length": {"type": "integer", "description": "读取多少字，默认 2000"},
        },
        handlers._read_source,
    ))

    register_tool(Tool(
        "locate_source",
        "可选批量查询引文坐标、歧义或纠错；submit_extraction已自动逐字定位和预检，正常提交无需先调用本工具。多匹配截断明确标记，不创建证据",
        {"source_id": {"type": "string"},
         "quotes": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 1000, "required": True},
         "limit_per_quote": {"type": "integer", "minimum": 1, "maximum": 1000,
                             "description": "每条最多返回坐标，默认20；截断时match_count为下界、count_is_exact=false，仍报告歧义"}},
        handlers._locate_source,
    ))

    register_tool(Tool(
        "list_source_slices",
        "列出一份原文已保存的切片，按原文位置排列；不创建切片",
        {"source_id": {"type": "string", "required": True}},
        handlers._list_source_slices,
    ))
    register_tool(Tool(
        "read_source_slice",
        "读取一份原文中的单个切片，返回完整内容和原文位置",
        {
            "source_id": {"type": "string", "required": True},
            "slice_id": {"type": "string", "required": True},
        },
        handlers._read_source_slice,
    ))
    register_tool(Tool(
        "create_source_slices",
        "为无规律内容按语义提供手动ranges批量创建切片；有规律标题优先使用pattern切片工具，不改原文",
        {
            "source_id": {"type": "string", "required": True},
            "slices": {
                "type": "array", "required": True, "minItems": 1,
                "description": "整批校验后创建；start/end 为原文中从 0 开始的 Python Unicode codepoint 位置，end 不包含在切片内，不是 UTF-8 字节或 UTF-16 单元",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["title", "start", "end"],
                    "properties": {
                        "title": {"type": "string", "minLength": 1},
                        "start": {"type": "integer", "minimum": 0},
                        "end": {"type": "integer", "minimum": 1},
                    },
                },
            },
        },
        handlers._create_source_slices,
        writes=True,
    ))

    _PATTERN_PARAMS = {
        "source_id": {"type": "string", "required": True},
        "pattern": {"type": "string", "required": True,
                    "description": "自行从原文样本确定的标题正则；MULTILINE，一次扫描全篇，匹配非空单行标题；2秒超时"},
        "start_index": {"type": "integer", "minimum": 0, "description": "从0开始的匹配标题索引，不是字符位置；默认0"},
        "limit": {"type": "integer", "minimum": 1, "description": "选取章节数量；省略为剩余全部。末选章以其下一标题为终点"},
    }
    register_tool(Tool(
        "preview_pattern_slices",
        "小样确认标题模式后，一次程序扫描并计算相邻章节范围；只读。返回完整计数及最多6个明确标记的首尾样例，章前内容明确排除",
        _PATTERN_PARAMS,
        lambda ops, params: handlers._pattern_slices(ops, params),
    ))
    register_tool(Tool(
        "create_pattern_slices",
        "按标题模式和章节索引范围一次自动扫描、校验并创建切片，无需预览或抄写哈希与坐标",
        _PATTERN_PARAMS,
        lambda ops, params: handlers._pattern_slices(ops, params, create=True),
        writes=True,
    ))


def register_import_source(Tool, register_tool, *, error_type, need, content_receipt):
    handlers = _SourceHandlers(error_type, need, content_receipt)
    register_tool(Tool(
        "import_source",
        "导入一段原文：标题 + 正文；返回统一对话读写工具使用的原文编号",
        {
            "title": {"type": "string", "required": True},
            "text": {"type": "string", "required": True},
        },
        handlers._import_source,
        writes=True,
    ))


def finalize_source_parameters(tools):
    for name in ('read_source', 'locate_source', 'list_source_slices', 'read_source_slice',
                 'create_source_slices', 'preview_pattern_slices', 'create_pattern_slices'):
        tools[name].params['source_id'] = {'type': 'string', 'description': '真实SRC编号；工程恰好一份原文时可省略，多份必须指定'}
