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

import json
import os
import re
from copy import deepcopy
from pathlib import Path

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


def snapshot(template_id: str, ops=None) -> dict:
    """新建会话时抄进会话元数据的冻结快照：提示词全文 + 工具名单 + 来源。"""
    frozen = deepcopy(resolve(template_id, ops))
    for key in ("builtin", "scope"):
        frozen.pop(key, None)
    frozen.setdefault("tools", [])
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


# ---------------------------------------------------------------- 自定义模板存储
# 三层解析：project 覆盖 app，app 覆盖内置；内置是代码常量，只读、不可删、不可被永久覆盖。
APP_FILE_ENV = "NARRATIVE_CHAT_TEMPLATES_FILE"
PROJECT_META_KEY = "chat_templates"
ID_PATTERN = re.compile(r"\A[a-z][a-z0-9-]{0,47}\Z")


def app_path() -> Path:
    override = os.environ.get(APP_FILE_ENV)
    if override:
        return Path(override)
    return Path.home() / ".narrative" / "chat-templates.json"


def _read_json(target: Path) -> dict:
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json(payload: dict, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def load_custom(scope: str, ops=None) -> dict[str, dict]:
    """一层自定义模板（app 级文件 / 工程 meta）；坏数据当空表，不让一个坏文件挡住列表。"""
    if scope == "app":
        rows = _read_json(app_path()).get("templates")
    elif scope == "project":
        if ops is None:
            return {}
        raw = ops.project.store.meta(PROJECT_META_KEY)
        try:
            rows = json.loads(raw)["templates"] if raw else {}
        except (ValueError, KeyError, TypeError):
            rows = {}
    else:
        raise ValueError("模板作用域须为app或project")
    return {str(key): dict(value) for key, value in (rows or {}).items() if isinstance(value, dict)}


def save_custom(scope: str, template_id: str, payload: dict, ops=None) -> dict:
    if scope not in {"app", "project"}:
        raise ValueError("模板作用域须为app或project")
    rows = load_custom(scope, ops)
    rows[template_id] = payload
    if scope == "app":
        current = _read_json(app_path())
        current["templates"] = rows
        _write_json(current, app_path())
    else:
        if ops is None:
            raise ValueError("工程级模板需要当前工程")
        ops.project.store.set_meta(PROJECT_META_KEY, json.dumps({"templates": rows}, ensure_ascii=False))
    return payload


def delete_custom(scope: str, template_id: str, ops=None) -> bool:
    if template_id in TEMPLATE_META:
        raise ValueError(f"内置模板不可删除：{template_id}")
    rows = load_custom(scope, ops)
    if template_id not in rows:
        return False
    rows.pop(template_id)
    if scope == "app":
        current = _read_json(app_path())
        current["templates"] = rows
        _write_json(current, app_path())
    else:
        if ops is None:
            raise ValueError("工程级模板需要当前工程")
        ops.project.store.set_meta(PROJECT_META_KEY, json.dumps({"templates": rows}, ensure_ascii=False))
    return True


def system_prompt_for(definition: dict) -> str:
    """两种形态：引用 prompt_defaults 的 key 列表（推荐）或自由文本。"""
    keys = definition.get("prompt_keys")
    if keys:
        from .prompt_defaults import DEFAULT_PROMPTS

        missing = [key for key in keys if key not in DEFAULT_PROMPTS]
        if missing:
            raise ValueError("引用了不存在的提示词条目：" + "、".join(missing))
        parts = [DEFAULT_PROMPTS[key]["text"].strip() for key in keys]
        return "\n\n".join(part for part in parts if part)
    text = definition.get("system_prompt")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("模板须给出prompt_keys或非空system_prompt")
    return text.strip()


def validate(definition: dict) -> dict:
    """保存前校验：id 规则、工具都在注册表、提示词引用有效。任一不满足即拒绝并列出。"""
    from .tool_api import TOOLS

    template_id = str(definition.get("id") or "").strip()
    if not ID_PATTERN.fullmatch(template_id):
        raise ValueError("模板id须为小写字母开头、只含小写字母/数字/短横线，长度不超过48")
    if template_id in TEMPLATE_META:
        raise ValueError(f"内置模板不可覆盖：{template_id}；请另存为自定义模板")
    tools = definition.get("tools")
    if not isinstance(tools, list) or not tools or any(not isinstance(name, str) for name in tools):
        raise ValueError("模板tools须为非空工具名数组")
    unknown = sorted(set(tools) - set(TOOLS))
    if unknown:
        raise ValueError("模板引用了不存在的工具：" + "、".join(unknown))
    name = str(definition.get("name") or "").strip()
    if not name:
        raise ValueError("模板须有名称")
    prompt = system_prompt_for(definition)
    payload = {"id": template_id, "name": name, "description": str(definition.get("description") or "").strip(),
               "tools": [name_ for name_ in TOOLS if name_ in set(tools)], "system_prompt": prompt}
    if definition.get("prompt_keys"):
        payload["prompt_keys"] = [key for key in definition["prompt_keys"]]
    return payload


def resolve(template_id: str, ops=None) -> dict:
    """按 project → app → 内置 的顺序解析一个模板。"""
    if template_id in TEMPLATE_META:
        return template(template_id)
    for scope in ("project", "app"):
        rows = load_custom(scope, ops)
        if template_id in rows:
            stored = rows[template_id]
            return {**deepcopy(stored), "id": template_id, "builtin": False, "scope": scope,
                    "system_prompt": stored.get("system_prompt") or system_prompt_for(stored)}
    raise ValueError(f"未知会话模板：{template_id}")


def list_all(ops=None) -> list[dict]:
    """项目级 + 应用级 + 内置；同 id 只列解析后生效的那一条。"""
    seen = dict.fromkeys(TEMPLATE_META)
    rows = []
    for scope in ("project", "app"):
        for template_id, stored in load_custom(scope, ops).items():
            if template_id in seen:
                continue
            seen[template_id] = True
            rows.append({"id": template_id, "name": stored.get("name") or template_id,
                         "description": stored.get("description") or "", "scope": scope, "builtin": False,
                         "tool_count": len(stored.get("tools") or []),
                         "prompt_summary": (stored.get("system_prompt") or "")[:80]})
    for template_id in TEMPLATE_META:
        rows.append({"id": template_id, "name": TEMPLATE_META[template_id][0], "scope": "builtin", "builtin": True,
                     "description": TEMPLATE_META[template_id][1], "tool_count": len(template_tool_names(template_id)),
                     "prompt_summary": compose_system_prompt(template_id)[:80]})
    return rows


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
