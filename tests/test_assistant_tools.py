"""外部 MCP 工具（scope="mcp"）要进协作者的可见面与业务分组。

注册一律走 monkeypatch 或临时注册表：**绝不把假工具留在全局 tool_api.TOOLS 里**，
否则同进程其它用例（工程工具全集、模板工具名单、接口签名核对）会跟着变。
"""

from __future__ import annotations

import pytest

from narrative import assistant, tool_api, tool_loading
from narrative.chat_templates import TEMPLATE_FAMILIES, template_tool_names
from narrative.mcp_client import SCOPE as MCP_SCOPE
from narrative.mcp_client import TOOL_PREFIX
from narrative.tool_loading import (
    CORE_TOOL_NAMES,
    FAMILY_DESCRIPTIONS,
    STANDARD_SCOPES,
    _allowed,
    bind_tool_loader,
    initial_tool_names,
    list_available_tools,
    load_tools,
    resolve_tool_names,
    tool_family,
)

READ_ONLY = TOOL_PREFIX + "fake__echo"
WRITE = TOOL_PREFIX + "fake__write_note"


def project_names() -> list[str]:
    return [name for name, tool in tool_api.TOOLS.items() if tool.scope == "project"]


def external_tool(name: str, *, writes: bool) -> tool_api.Tool:
    """形状照 mcp_client.adapt_tools 的产物：scope="mcp"，schema 来自远端。"""
    return tool_api.Tool(name, f"外部服务器提供的工具 {name}", {"text": {"type": "string"}},
                         lambda _ops, args: {"echo": args.get("text")},
                         writes=writes, scope=MCP_SCOPE,
                         input_schema={"type": "object", "properties": {"text": {"type": "string"}},
                                       "required": ["text"]})


@pytest.fixture
def external(monkeypatch):
    """临时把两个外部工具塞进全局注册表；用例结束由 monkeypatch 摘掉。"""
    tools = {READ_ONLY: external_tool(READ_ONLY, writes=False),
             WRITE: external_tool(WRITE, writes=True)}
    for name, tool in tools.items():
        monkeypatch.setitem(tool_api.TOOLS, name, tool)
    return sorted(tools)


def test_external_tools_reach_the_collaborator_surface(external):
    """两种访问模式下，外部工具都出现在协作者拿到的工具清单里。"""
    for mode in ("standard", "full"):
        names = {row["name"] for row in assistant.tool_catalog(access_mode=mode)}
        assert set(external) <= names, mode
    # 完全访问必须是标准模式的超集：切模式不能把外部工具弄丢
    assert assistant._allowed_tool_names("standard") <= assistant._allowed_tool_names("full")
    assert set(external) <= assistant._allowed_tool_names("full")
    # 配置类工具（密钥、会话模板）依旧不对协作者开放
    assert not any(tool_api.TOOLS[name].scope == "configuration"
                   for name in assistant._allowed_tool_names("full"))
    assert set(external) <= _allowed(tool_api.TOOLS, "standard", None)


def test_only_read_only_external_tools_join_the_read_set(external):
    """只读的外部工具进只读集合；远端没声明只读的一律算写，不进。"""
    read_names = assistant.read_tool_names()
    assert READ_ONLY in read_names
    assert WRITE not in read_names
    # 写工具照样能调用，只是不算只读：只读与否不改变授权
    assert WRITE in assistant._allowed_tool_names("standard")
    # 工程业务工具里只读的那些仍在（回归）
    assert {name for name in project_names() if not tool_api.TOOLS[name].writes} <= set(read_names)
    # 只读集合里不会混进配置类、主机类工具，也不会混进任何写工具
    for name in read_names:
        assert tool_api.TOOLS[name].scope in STANDARD_SCOPES, name
        assert tool_api.TOOLS[name].writes is False, name


def test_external_tools_get_their_own_family_with_chinese_description(external):
    assert tool_family(READ_ONLY) == "mcp"
    assert FAMILY_DESCRIPTIONS["mcp"] and "MCP" in FAMILY_DESCRIPTIONS["mcp"]
    # 远端名字里带本工程业务组的关键字，也不能被认成那个组
    assert tool_family(TOOL_PREFIX + "fake__read_storyboard") == "mcp"
    groups = {row["family"]: row for row in list_available_tools()["families"]}
    assert groups["mcp"]["summary"] == FAMILY_DESCRIPTIONS["mcp"] and groups["mcp"]["count"] >= 2
    assert set(external) <= set(resolve_tool_names(families=["mcp"]))
    assert set(external) <= {row["name"] for row in list_available_tools(family="mcp")["tools"]}
    discovered = {row["name"]: row for row in list_available_tools(names=external)["tools"]}
    assert discovered[READ_ONLY]["family"] == "mcp" and discovered[READ_ONLY]["writes"] is False
    assert discovered[WRITE]["writes"] is True


def test_bound_runtime_can_load_the_mcp_family(external):
    """按业务组加载：mcp 组能被 load_tools 认出来并激活，不需要逐个报名字。"""
    allowed = project_names() + list(external)
    visible = initial_tool_names(allowed)

    def loader(requested):
        visible.extend(name for name in requested if name not in visible)
        return list(visible)

    with bind_tool_loader(loader, visible_names=lambda: visible, allowed_names=allowed):
        result = load_tools(families=["mcp"])
        assert result["runtime_loaded"] is True
        assert set(result["newly_loaded_tools"]) == set(external)
        assert {row["family"] for row in result["tools"]} == {"mcp"}


def test_no_configured_external_tools_keeps_the_old_surface(monkeypatch):
    """回归：一台外部服务器都没配置时，可见面与业务组和改动前一致。"""
    if any(tool.scope == MCP_SCOPE for tool in tool_api.TOOLS.values()):
        pytest.skip("本机已配置外部 MCP 工具（缓存已注册），无法核对「未配置」时的一致性")
    # 用只含工程工具的注册表模拟「没有外部服务器」：发现与加载都看不到 mcp 组
    registry = {name: tool_api.TOOLS[name] for name in project_names()}
    monkeypatch.setattr(tool_loading, "_registry", lambda: registry)
    families = [row["family"] for row in list_available_tools()["families"]]
    assert "mcp" not in families
    assert families == [name for name in FAMILY_DESCRIPTIONS
                        if any(tool_family(tool) == name for tool in registry)]
    with pytest.raises(ValueError, match="业务组不存在"):
        resolve_tool_names(families=["mcp"])
    assert resolve_tool_names(families=["screenplay"])
    assert _allowed(registry, "standard", None) == set(registry)
    # 标准模式没有主机/工作区工具可放行，两档的可见集合此时相同
    assert _allowed(registry, "full", None) == set(registry)
    # ASSISTANT_TOOLS 仍然是「工程业务工具全集」，没被外部工具稀释
    assert assistant.ASSISTANT_TOOLS == tuple(project_names())


def test_skill_management_is_visible_to_collaborators_but_configuration_is_not():
    """技能管理工具进协作者可见面；密钥/模板写入仍然进不来（设计红线）。"""
    from narrative.skills import LOAD_TOOL_NAME, MANAGE_TOOL_NAMES, SCOPE

    allowed = assistant._allowed_tool_names("standard")
    catalog = {row["name"] for row in assistant.tool_catalog(access_mode="standard")}
    for name in (LOAD_TOOL_NAME, *sorted(MANAGE_TOOL_NAMES)):
        assert name in allowed and name in catalog, name
        assert tool_api.TOOLS[name].scope in {"project", SCOPE}, name
    # 五个管理工具都是 skill scope；写属性只落在 create/update/delete 上
    assert {name for name in MANAGE_TOOL_NAMES} == {
        "list_skills", "read_skill", "create_skill", "update_skill", "delete_skill"}
    assert {name for name in MANAGE_TOOL_NAMES if tool_api.TOOLS[name].writes} == {
        "create_skill", "update_skill", "delete_skill"}
    assert tool_api.TOOLS[LOAD_TOOL_NAME].writes is False
    # 配置类工具（密钥、模板写入）在两档访问模式下都不可见
    for mode in ("standard", "full"):
        visible = assistant._allowed_tool_names(mode)
        assert not {name for name in visible if tool_api.TOOLS[name].scope == "configuration"}, mode
    assert not {"configure_project_api_key", "save_chat_template", "delete_chat_template"} & catalog


def test_template_family_lists_carry_skill_tools_and_resident_load_skill():
    """三个模板都带 skill 组，冻结名单里因此有管理工具与常驻的 load_skill。"""
    from narrative.skills import LOAD_TOOL_NAME, MANAGE_TOOL_NAMES

    for template_id, families in TEMPLATE_FAMILIES.items():
        assert "skill" in families, template_id
        names = template_tool_names(template_id)
        assert set(MANAGE_TOOL_NAMES) <= set(names), template_id
        assert LOAD_TOOL_NAME in names, template_id
        # 组只是组织发现：冻结名单里仍按注册表顺序，且不混进配置类工具
        assert names == [name for name in tool_api.TOOLS if name in set(names)]
        assert not any(tool_api.TOOLS[name].scope == "configuration" for name in names), template_id
    assert LOAD_TOOL_NAME in CORE_TOOL_NAMES


def test_template_family_lists_carry_external_tools():
    """模板按 family 冻结工具数组：带上 mcp 组才看得见外部工具，且不多带别的。"""
    for template_id, families in TEMPLATE_FAMILIES.items():
        assert "mcp" in families, template_id
        for name in template_tool_names(template_id):
            assert name in set(CORE_TOOL_NAMES) or tool_family(name) in set(families), (template_id, name)
