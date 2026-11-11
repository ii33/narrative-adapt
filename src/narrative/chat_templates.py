"""内置固定会话模板：每个模板有各自的系统提示词与工具集合。

**系统提示词不新写一行**：全部由 `prompt_defaults` 的既有条目按下面的组合拼成
（同一份文本被提示词负责人升级时，模板自动跟着走）：

- `extract`     = `system` + 提取通用要求（`extraction`）+ 精细粒度细则（`granularity_fine`）
                  + 只说话不落库的对话动作说明（`conversation_actions`）
- `screenplay`  = `system` + 场表与正文写作（`writing`）+ 拆场策略（`scene_policy`）
                  + 引用策略（`reference_policy`）
- `storyboard`  = `system` + 分镜（`storyboard`）+ 素描风格（`sketch_style`）
                  + 分镜素材（`storyboard_assets`）+ 引用策略（`reference_policy`）

**工具集合按 `tool_loading` 的业务 family 推导**（不做逐名手写清单，避免与注册表漂移）：
每个模板都是「常驻读取/发现工具（CORE_TOOL_NAMES）+ 该会话必需的 family」，
再与当前访问模式允许的名字求交。单会话量级 39／53／47，不是全量 172。

**创建即冻结**：`snapshot()` 把系统提示词全文与工具名单抄进会话元数据。会话进行中
改模板不影响已建会话——这同时保证了「一次会话从头到尾 tools 数组不变」，模型侧的
提示词缓存不会因为中途 `load_tools` 增长而整体失效。
"""

from __future__ import annotations

from copy import deepcopy

# 页面 → 模板。依据：原文页做提取、剧本页写剧本、分镜页做分镜。
# 「原文页」在前端有多个入口（原文/浏览），都给到提取模板，宁可多一个别名也不留空路由。
PAGE_TEMPLATE_ROUTES = {
    "source": "extract",
    "sources": "extract",
    "browse": "extract",
    "extract": "extract",
    "screenplay": "screenplay",
    "storyboard": "storyboard",
}

# 每个模板取哪些业务组；CORE 常驻。
TEMPLATE_FAMILIES = {
    "extract": ("extraction", "properties", "sources"),
    "screenplay": ("screenplay", "writing"),
    "storyboard": ("storyboard",),
}

# 模板组合用的 prompt_defaults 条目（顺序即拼接顺序；`system` 恒在首位）。
TEMPLATE_PROMPT_KEYS = {
    "extract": ("system", "extraction", "granularity_fine", "conversation_actions"),
    "screenplay": ("system", "writing", "scene_policy", "reference_policy"),
    "storyboard": ("system", "storyboard", "sketch_style", "storyboard_assets", "reference_policy"),
}

TEMPLATE_META = {
    "extract": ("提取", "读原文、提取事实、修补提取草稿；不写剧本。"),
    "screenplay": ("剧本", "把已入库的剧情改编成剧本：场表、正文与写作上下文。"),
    "storyboard": ("分镜", "把剧本场次拆成分镜：调度、摄影机、画格与素材。"),
}


def compose_system_prompt(template_id: str) -> str:
    """把既有提示词条目按模板顺序拼成系统提示词；不新增任何文案。"""
    from .prompt_defaults import DEFAULT_PROMPTS

    keys = TEMPLATE_PROMPT_KEYS.get(template_id)
    if not keys:
        raise ValueError(f"未知会话模板：{template_id}")
    parts = [DEFAULT_PROMPTS[key]["text"].strip() for key in keys]
    return "\n\n".join(part for part in parts if part)


def template_tool_names(template_id: str) -> list[str]:
    """模板的工具名单：常驻工具 + 该模板的 family，顺序沿用注册表顺序。"""
    from .tool_api import TOOLS
    from .tool_loading import CORE_TOOL_NAMES, tool_family

    families = TEMPLATE_FAMILIES.get(template_id)
    if families is None:
        raise ValueError(f"未知会话模板：{template_id}")
    wanted = set(CORE_TOOL_NAMES) | {name for name in TOOLS if tool_family(name) in set(families)}
    return [name for name in TOOLS if name in wanted]


def prompt_versions(template_id: str) -> dict[str, str]:
    from .prompt_defaults import DEFAULT_PROMPTS

    return {key: DEFAULT_PROMPTS[key].get("version", "") for key in TEMPLATE_PROMPT_KEYS[template_id]}


def template(template_id: str) -> dict:
    """一个内置模板的完整定义（每次现算，供新建会话时快照）。"""
    if template_id not in TEMPLATE_META:
        raise ValueError(f"未知会话模板：{template_id}")
    name, description = TEMPLATE_META[template_id]
    return {"id": template_id, "name": name, "description": description,
            "builtin": True, "system_prompt": compose_system_prompt(template_id),
            "tools": template_tool_names(template_id), "prompt_versions": prompt_versions(template_id)}


def list_templates() -> list[dict]:
    return [template(template_id) for template_id in TEMPLATE_META]


def snapshot(template_id: str) -> dict:
    """新建会话时抄进会话元数据的冻结快照：提示词全文 + 工具名单 + 版本。"""
    frozen = deepcopy(template(template_id))
    frozen.pop("builtin", None)
    return frozen


def visible_tool_names(frozen: dict, allowed_names) -> list[str]:
    """冻结名单与当前访问模式允许集合的交集；顺序沿用冻结顺序，逐轮不变。"""
    allowed = set(allowed_names)
    return [name for name in (frozen or {}).get("tools") or [] if name in allowed]


def route_for_page(page_id: str | None, overrides: dict[str, str] | None = None) -> str | None:
    """页面默认路由；带临时覆盖时先用覆盖值。都不命中返回 None（调用方用当前会话）。"""
    if overrides and page_id in overrides:
        value = overrides.get(page_id)
        return value if value in TEMPLATE_META else None
    return PAGE_TEMPLATE_ROUTES.get(str(page_id or ""))


class Router:
    """临时覆盖的存活期是「下一次发送」，切页即失效。

    只用内存状态：覆盖是当下意图，不写进工程数据，重启即清空。
    """

    def __init__(self) -> None:
        self._overrides: dict[str, str] = {}

    def override(self, page_id: str, template_id: str) -> None:
        if template_id not in TEMPLATE_META:
            raise ValueError(f"未知会话模板：{template_id}")
        self._overrides[str(page_id)] = template_id

    def clear(self, page_id: str | None = None) -> None:
        if page_id is None:
            self._overrides.clear()
        else:
            self._overrides.pop(str(page_id), None)

    def route(self, page_id: str | None) -> str | None:
        return route_for_page(page_id, self._overrides)

    def consume(self, page_id: str | None) -> str | None:
        """取这次发送该用的模板，并让它失效（切页后回到自动路由）。"""
        template_id = self.route(page_id)
        if page_id is not None:
            self._overrides.pop(str(page_id), None)
        return template_id
