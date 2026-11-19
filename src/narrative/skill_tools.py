"""技能工具：让协作者自己维护技能，并在会话里按需加载。

分工与 scope：

- `list_skills` / `read_skill` / `create_skill` / `update_skill` / `delete_skill` 读写
  `~/.narrative/skills.json`，归 `skills.SCOPE`（`"skill"`）。它们不是
  `configuration`：技能是「助手自己的作业说明」，协作者本来就该能建立、编辑、管理；
  密钥与会话模板写入仍留在 `configuration`，那条红线不动。写操作照样标
  `writes=True`，审计与「写操作」判定和别的业务写入一致。
- `list_prompt_keys` 是只读的**提示词位清单**：有哪些内置提示词位、当前各自被哪条
  技能替换（`replaces`）。它让模型先看清「能改哪一段」，再决定要不要写一条替换技能；
  归同一个 `skill` scope——它读的就是技能与提示词位的对应关系。
- `load_skill` 是**会话行为**：它把技能的 `families`/`tools` 交给
  `tool_loading.resolve_tool_names` + `load_tools` 打开，并把 `instructions` 当
  「本次任务要遵守的说明」返回。它改变的是本次会话的工具曝光，所以归 `project`。

`load_skill` 到底做了什么，以及做不到什么：

1. 工具组是**真的**被打开（走既有按需加载机制，和 `load_tools` 同一条路径），
   不是「提醒模型自己去加载」。下一次模型请求的 tools 里就会出现这些工具的精确
   schema，同一任务继续调用。
2. 说明文本只返回给调用方那一轮的工具结果里——系统提示词不重写。技能因此不
   影响提示词缓存的结构，也不会把「一次任务的说明」变成整个会话的规则。
3. 如果当前会话的工具集在创建时已冻结（会话模板建出来的会话），打开工具这一步
   会被运行时拒绝。这时**必须失败**并说清替代做法（新建会话、换模板、或只用说明），
   不能返回成功让模型以为工具已经可用。
"""

from __future__ import annotations

from typing import Any

from . import skills
from .skills import LOAD_TOOL_NAME, MANAGE_TOOL_NAMES, SCOPE, SKILL_TOOL_NAMES, SkillError

# 工具真的被打开之后的说明：模型据此知道「不用再自己 load_tools」。
LOADED_MESSAGE = "说明已随本次结果给出，本轮起按它执行；对应工具已在本会话激活，不必再次加载。"


def _text(args: dict, key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _required_name(args: dict) -> str:
    name = _text(args, "name")
    if not name:
        raise SkillError("请给出技能名字（name）；现有技能用 list_skills 查看")
    return name


def _list_skills(_ops, _args) -> dict:
    rows = []
    for skill in skills.load_skills():
        rows.append({
            "name": skill.name,
            "summary": skill.summary,
            "enabled": skill.enabled,
            "builtin": skill.builtin,
            "families": list(skill.families),
            "tools": list(skill.tools),
            # 覆盖与工作流声明一起列出来：模型要先知道哪条技能在改提示词、哪条声明了
            # 生成工作流，才谈得上「用哪一条」。正文仍然只在 read_skill 里给。
            "replaces": skill.replaces,
            "workflow": skill.workflow,
            "updated_at": skill.updated_at,
        })
    return {"skills": rows, "count": len(rows),
            "message": "停用的技能不会被load_skill加载；内置技能只读。要正文与最终工具清单用read_skill；"
                       "要改系统提示词先看list_prompt_keys有哪些提示词位。"}


def _opens(skill) -> dict:
    """这条技能会打开哪些工具；解析不了就如实说明，不假装能打开。"""
    try:
        resolved = skills.resolve_family_names(list(skill.families), list(skill.tools))
    except ValueError as exc:
        return {"resolved_tools": [], "problem": f"当前访问模式下解析不到这些工具：{exc}"}
    return {"resolved_tools": resolved}


def _read_skill(_ops, args: dict) -> dict:
    name = _required_name(args)
    skill = skills.get_skill(name)
    if skill is None:
        raise SkillError(f"没有技能 {name}；用 list_skills 查看现有技能，或先 create_skill")
    return {"skill": skill.to_dict(), "opens": _opens(skill),
            "message": "正文是给助手遵守的作业说明；要真正打开工具请调用load_skill。"}


def _create_skill(_ops, args: dict) -> dict:
    skill = skills.create_skill(args.get("name"), args.get("summary"), args.get("instructions"),
                                args.get("families"), args.get("tools"),
                                replaces=args.get("replaces"), workflow=args.get("workflow"))
    return {"created": skill.to_dict(), "opens": _opens(skill),
            "message": f"技能「{skill.name}」已保存；在本次任务里用load_skill加载它。"
                       + _replaces_note(skill)}


def _update_skill(_ops, args: dict) -> dict:
    skill = skills.update_skill(
        _required_name(args),
        summary=args.get("summary"),
        instructions=args.get("instructions"),
        families=args.get("families"),
        tools=args.get("tools"),
        enabled=args.get("enabled"),
        replaces=args.get("replaces"),
        workflow=args.get("workflow"),
    )
    return {"updated": skill.to_dict(), "opens": _opens(skill),
            "message": f"技能「{skill.name}」已更新；只改了传进来的字段。" + _replaces_note(skill)}


def _replaces_note(skill) -> str:
    """声明了 `replaces` 时补一句：这件事会改变系统提示词的拼接结果，值得说清。"""
    if not skill.replaces:
        return ""
    return (f"它的正文现在替换提示词位「{skill.replaces}」：模板拼接与会话快照都按它现算，"
            "同一提示词位有多条技能时按最近更新生效。")


def _list_prompt_keys(_ops, _args) -> dict:
    """只读：有哪些提示词位、当前各自被哪条技能替换。"""
    from .prompt_registry import list_prompt_keys, skill_problem

    rows = list_prompt_keys()
    result = {
        "keys": rows,
        "count": len(rows),
        "message": "这些是系统提示词的固定位：replaced_by 非空表示该段当前被这条技能替换，"
                   "为空表示用的内置文本。要替换某段就用 create_skill/update_skill 的 replaces。",
    }
    problem = skill_problem()
    if problem:
        # 技能文件坏掉时列表仍给内置口径，但原因必须说出来，否则看起来像「没人覆盖」。
        result["problem"] = problem
    return result


def _delete_skill(_ops, args: dict) -> dict:
    name = _required_name(args)
    if skills.delete_skill(name, confirm=args.get("confirm") is True):
        return {"deleted": True, "name": name, "message": f"技能「{name}」已删除。"}
    raise SkillError(f"没有自定义技能 {name}；内置技能不可删除，用 list_skills 确认现有技能")


def _visibility(names: list[str]) -> list[str] | None:
    """这些工具在本会话里已经可见吗？返回 None 表示「问不出来」（没有运行时绑定）。"""
    from .tool_loading import list_available_tools

    try:
        probe = list_available_tools(names=list(names))
    except ValueError:
        return None
    rows = probe.get("tools") or []
    if len(rows) != len(names) or any(row.get("loaded") is None for row in rows):
        return None
    return [row["name"] for row in rows if row.get("loaded")]


def _refusal(skill, resolved: list[str], exc: Exception) -> str:
    """打开工具被拒时说清楚：没生效、为什么、还能怎么办。"""
    return (f"技能「{skill.name}」没能生效：本会话的工具集在创建时固定（{exc}），"
            f"因此 {len(resolved)} 个工具（" + "、".join(resolved[:8])
            + ("…" if len(resolved) > 8 else "")
            + "）没有打开。可选做法：① 让用户为本任务新建一个不由固定模板创建的会话，"
              "那里可以中途加载工具；② 换一个只给说明、不额外打开工具的技能；"
              "③ 先用 list_available_tools 看当前会话里已经有什么，缺的那几个如实告诉用户。"
              "技能正文没有生效，不要按它声称已经完成任何工作。")


def _load_skill(ops, args: dict) -> dict:
    name = _required_name(args)
    skill = skills.get_skill(name)
    if skill is None:
        raise SkillError(f"没有技能 {name}；用 list_skills 查看现有技能，或先 create_skill")
    if not skill.enabled:
        raise SkillError(f"技能 {skill.name} 已停用，不会被加载；需要时先 update_skill(enabled=true)，"
                         "或改用另一个技能")
    resolved = skills.resolve_family_names(list(skill.families), list(skill.tools))
    base: dict[str, Any] = {
        "skill": skill.name,
        "summary": skill.summary,
        "instructions": skill.instructions,
        # 技能自己声明的（families/tools）与解析后的精确工具名分开给：前者是作者的
        # 意图，后者才是本次真正要打开的东西。
        "wanted_families": list(skill.families),
        "wanted_tools": list(skill.tools),
        "resolved_tools": resolved,
    }
    if not resolved:
        # 只给说明的技能：不碰工具集，因此在冻结会话里也能用。
        return {**base, "runtime_loaded": None, "loaded_tools": [], "newly_loaded_tools": [],
                "message": "这条技能不打开额外工具，只给说明；本轮起按正文执行。"}

    from .tool_loading import load_tools

    visible = _visibility(resolved)
    if visible is not None and len(visible) == len(resolved):
        return {**base, "runtime_loaded": True, "loaded_tools": list(resolved),
                "newly_loaded_tools": [],
                "message": "技能要求的工具本会话已经可用，没有增长工具集。" + LOADED_MESSAGE}
    try:
        opened = load_tools(names=list(resolved), ops=ops)
    except (ValueError, RuntimeError) as exc:
        # 冻结会话、访问模式不足、运行时未确认加载结果都到这里：技能没生效，如实说。
        raise SkillError(_refusal(skill, resolved, exc)) from exc
    runtime_loaded = bool(opened.get("runtime_loaded"))
    result = {
        **base,
        "runtime_loaded": runtime_loaded,
        "loaded_tools": opened.get("loaded_tools") or [],
        "newly_loaded_tools": opened.get("newly_loaded_tools") or [],
        "message": (LOADED_MESSAGE if runtime_loaded
                    else "当前没有会话运行时（命令行或MCP服务）：已返回这些工具的参数合同，"
                         "说明对本次任务有效，但工具无法在这里激活。"),
    }
    if not runtime_loaded:
        # 没有运行时就没有「下一次请求」，把合同直接给出去，调用方才可能真的调通。
        result["tool_contracts"] = opened.get("tools") or []
    return result


def register(Tool, register_tool, ToolError):
    """按 tool_api 的既有范式注册。所有错误都翻成面向模型的 ToolError。"""

    def wrap(handler):
        def invoke(ops, args):
            try:
                return handler(ops, args)
            except ToolError:
                raise
            except (ValueError, OSError) as exc:
                raise ToolError(str(exc)) from exc
        return invoke

    register_tool(Tool(
        "list_skills",
        "列出全部技能：名字、一句话摘要、是否启用、是否内置，以及会打开哪些业务组/工具。只读，不返回正文。",
        {}, wrap(_list_skills), scope=SCOPE))
    register_tool(Tool(
        "read_skill",
        "读一条技能的完整正文，以及它当前能解析到的工具清单。只读；要真正打开工具用load_skill。",
        {"name": {"type": "string", "required": True, "description": "技能名字，不区分大小写"}},
        wrap(_read_skill), scope=SCOPE))
    # 只读的提示词位清单：模型先看清「能改哪一段、现在被谁改着」，再决定要不要写
    # 一条 replaces 技能。它不改任何东西，所以不标 writes。
    register_tool(Tool(
        "list_prompt_keys",
        "列出系统提示词的固定位（key、说明、当前版本），以及每段当前被哪条技能替换。只读；"
        "要替换某段就在create_skill/update_skill里填replaces。",
        {}, wrap(_list_prompt_keys), scope=SCOPE))
    register_tool(Tool(
        "create_skill",
        "新建一条技能：一段给助手遵守的作业说明，加上它要用到的业务组/工具；可选声明替换哪段"
        "系统提示词（replaces）与生成工作流（workflow）。名字唯一（不区分大小写），"
        "families/tools/replaces/workflow 必须是已知的键或形状；超长或未知会被拒绝并列出可选项。",
        {
            "name": {"type": "string", "required": True, "description": "人可读且唯一的名字，最多64字符"},
            "summary": {"type": "string", "required": True, "description": "一句话：这条技能什么时候用"},
            "instructions": {"type": "string", "required": True,
                             "description": "作业说明（markdown，最多20000字符）：先做什么、看到什么再往下走、什么不算完成"},
            "families": {"type": "array", "items": {"type": "string"},
                         "description": "要打开的业务组，例如['screenplay','writing']；可省略"},
            "tools": {"type": "array", "items": {"type": "string"},
                      "description": "额外点名的工具名，例如['read_source']；可省略"},
            "replaces": {"type": "string",
                         "description": "要替换的内置提示词位（用list_prompt_keys查），例如'extraction'；"
                                        "省略表示这条技能只附加说明，不改提示词"},
            "workflow": {"type": "object",
                         "description": "生成工作流声明：kind为sketch/style/video之一，model必填；"
                                        "可带license、workflow_file、params、expectations"},
        }, wrap(_create_skill), writes=True, scope=SCOPE))
    register_tool(Tool(
        "update_skill",
        "局部修改一条自定义技能：只改传进来的字段，其余保持不变；内置技能只读。",
        {
            "name": {"type": "string", "required": True, "description": "要修改的技能名字"},
            "summary": {"type": "string"},
            "instructions": {"type": "string"},
            "families": {"type": "array", "items": {"type": "string"},
                         "description": "传空数组表示清空，省略表示不动"},
            "tools": {"type": "array", "items": {"type": "string"},
                      "description": "传空数组表示清空，省略表示不动"},
            "enabled": {"type": "boolean", "description": "false 时停用：load_skill 会拒绝加载"},
            "replaces": {"type": "string",
                         "description": "改替换的提示词位；传空串表示改回只附加说明，省略表示不动"},
            "workflow": {"type": "object",
                         "description": "改生成工作流声明；传空对象表示清空，省略表示不动"},
        }, wrap(_update_skill), writes=True, scope=SCOPE))
    register_tool(Tool(
        "delete_skill",
        "删除一条自定义技能；须显式confirm=true。内置技能不可删除，会说明原因与替代做法。",
        {"name": {"type": "string", "required": True},
         "confirm": {"type": "boolean", "required": True, "description": "删除必须显式 true"}},
        wrap(_delete_skill), writes=True, scope=SCOPE))
    # load_skill 是会话行为：它把技能点名的工具组真的打开，并返回本次任务的说明。
    # 因此它是 project scope——它改变的是本次会话的工具曝光，不是用户配置。
    register_tool(Tool(
        LOAD_TOOL_NAME,
        "加载一条技能：打开它点名的业务组/工具（走既有按需加载，下次模型请求生效），"
        "并返回本轮要遵守的说明正文。会话工具集已冻结时会失败并给出替代做法。",
        {"name": {"type": "string", "required": True, "description": "技能名字，不区分大小写"}},
        wrap(_load_skill), scope="project"))


__all__ = ["SKILL_TOOL_NAMES", "MANAGE_TOOL_NAMES", "SCOPE", "register"]
