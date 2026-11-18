"""MCP 客户端内核：全局配置、stdio 会话、工具适配与清单缓存。

一律用 tests/fake_mcp_server.py 这个标准库假服务器，不联网、不要 mcp SDK；
配置路径通过 NARRATIVE_MCP_FILE 指到 tmp_path，绝不碰真实用户目录。
注册一律走本文件的独立注册表，不往全局 tool_api.TOOLS 里塞外部工具。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from narrative import mcp_client, tool_api

FAKE = Path(__file__).with_name("fake_mcp_server.py")
LOCAL_ECHO = "mcp__fake__echo"
LOCAL_WRITE = "mcp__fake__write_note"


def _new_registry():
    """独立注册表 + 模拟 tool_api._register 的重名拒绝语义。"""
    tools: dict[str, tool_api.Tool] = {}

    def register(tool):
        if tool.name in tools:
            raise ValueError(f"Duplicate tool registration: {tool.name}")
        tools[tool.name] = tool

    return tools, register


def _server(name: str = "fake", **changes) -> mcp_client.McpServer:
    data = {"name": name, "transport": "stdio", "command": sys.executable,
            "args": [str(FAKE)], "timeout": 10}
    data.update(changes)
    return mcp_client.McpServer.from_dict(data)


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    """每个用例一份配置与缓存；会话不留到下一个用例。"""
    path = tmp_path / "mcp-servers.json"
    monkeypatch.setenv(mcp_client.CONFIG_ENV, str(path))
    mcp_client.close()
    yield path
    mcp_client.close()


@pytest.fixture
def registry():
    return _new_registry()


# -- 配置 -------------------------------------------------------------------


def test_missing_config_reads_as_empty_list(config_file):
    assert mcp_client.load_servers() == []
    assert not config_file.exists()


def test_config_roundtrip_keeps_stdio_and_http_fields(config_file):
    servers = [
        _server("本地工具", args=["server.py", "--flag"], env={"TOKEN": "值"},
                cwd=str(config_file.parent)),
        mcp_client.McpServer.from_dict(
            {"name": "remote-http", "transport": "http", "url": "https://example.invalid/mcp"}),
    ]
    mcp_client.save_servers(servers)

    loaded = mcp_client.load_servers()
    assert [server.name for server in loaded] == ["本地工具", "remote-http"]
    assert loaded[0].transport == "stdio"
    assert loaded[0].command == sys.executable
    assert loaded[0].args == ("server.py", "--flag")
    assert loaded[0].env == {"TOKEN": "值"}
    assert loaded[0].cwd == str(config_file.parent)
    assert loaded[0].timeout == 10  # 显式给的超时原样留住
    assert all(server.enabled for server in loaded)
    # 省略 timeout 时用默认值
    assert mcp_client.McpServer.from_dict({"name": "x", "command": "cmd"}).timeout == \
        mcp_client.DEFAULT_TIMEOUT
    assert loaded[1].transport == "http"
    assert loaded[1].url == "https://example.invalid/mcp"
    # 配置文件是人也能改的：落盘是 {"version", "servers": [...]}
    payload = json.loads(config_file.read_text(encoding="utf-8"))
    assert [row["name"] for row in payload["servers"]] == ["本地工具", "remote-http"]


def test_corrupt_config_reports_path_and_keeps_original(config_file):
    broken = '{"servers": [ 这不是 JSON'
    config_file.write_text(broken, encoding="utf-8")

    with pytest.raises(mcp_client.McpConfigError) as excinfo:
        mcp_client.load_servers()
    assert str(config_file) in str(excinfo.value)
    assert config_file.read_text(encoding="utf-8") == broken  # 读失败不改原文件

    # 保存时先把读不懂的原文件备份成 .bak，再原子写新配置：坏配置不能静默消失
    mcp_client.save_servers([_server()])
    backup = config_file.with_name(config_file.name + ".bak")
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8") == broken
    assert [server.name for server in mcp_client.load_servers()] == ["fake"]


def test_invalid_entry_names_the_offending_server(config_file):
    config_file.write_text(json.dumps({"servers": [
        {"name": "half", "transport": "stdio"},
    ]}, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(mcp_client.McpConfigError) as excinfo:
        mcp_client.load_servers()
    assert "half" in str(excinfo.value) and "command" in str(excinfo.value)


# -- 会话全链路 -------------------------------------------------------------


def test_handshake_list_paginate_and_call(registry):
    """initialize + tools/list（两页）+ tools/call + ping，走真子进程。"""
    server = _server(env={"FAKE_MCP_PAGINATE": "1"})
    mcp_client.save_servers([server])
    with mcp_client.McpSession(server) as session:
        result = session.initialize()
        assert result["protocolVersion"] == mcp_client.PROTOCOL_VERSION
        assert session.server_info["name"] == "fake-mcp"
        assert session.ping() == {}
        assert [tool["name"] for tool in session.list_tools()] == ["echo", "write_note"]

    tools, register = registry
    report = mcp_client.refresh(tool_api.Tool, register, tools=tools)
    assert report["errors"] == {}
    assert set(tools) == {LOCAL_ECHO, LOCAL_WRITE}
    assert mcp_client.open_sessions()  # 会话留着复用，不是每次调用重起进程


def test_adapted_tool_keeps_remote_schema_and_writes_flag(registry):
    tools, register = registry
    mcp_client.save_servers([_server()])
    mcp_client.refresh(tool_api.Tool, register, tools=tools)

    echo = tools[LOCAL_ECHO]
    write = tools[LOCAL_WRITE]
    assert echo.scope == "mcp"
    assert echo.writes is False  # 远端 readOnlyHint=true
    assert write.writes is True  # 远端没声明只读 → 保守当写
    # schema 原样来自远端 inputSchema，模型看到的合同不变
    assert echo.schema() == {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "要回显的文本"}},
        "required": ["text"],
    }
    # 参数原样转发；中文按 UTF-8 往返
    result = echo.handler(None, {"text": "你好，世界"})
    assert result["server"] == "fake"
    assert result["tool"] == "echo"
    assert result["content"] == "你好，世界"
    # 非文本 content 留摘要，不静默丢
    note = write.handler(None, {"text": "第一章"})
    assert note["content"] == "已写：第一章"
    assert note["other_content"] == ["[图片 image/png]"]


def test_tool_names_are_prefixed_sanitized_and_unique(registry):
    assert mcp_client.tool_name("my server.b", "read-file") == "mcp__my_server_b__read_file"
    # 折叠后同名时递增，互相不顶掉
    used = {"mcp__fake__echo"}
    assert mcp_client.tool_name("fake", "echo", used) == "mcp__fake__echo_2"

    tools, register = registry
    mcp_client.save_servers([_server(env={"FAKE_MCP_ODD_NAMES": "1"})])
    mcp_client.refresh(tool_api.Tool, register, tools=tools)
    odd = "mcp__fake__read_file_v2"
    assert odd in tools
    assert all(name.startswith(mcp_client.TOOL_PREFIX) for name in tools)
    # 前缀保证与本地工具不撞名
    assert not (set(tools) & set(tool_api.TOOLS))


def test_remote_errors_become_tool_errors(config_file):
    server = _server()
    mcp_client.save_servers([server])

    with pytest.raises(tool_api.ToolError) as excinfo:
        mcp_client.call_tool(server, "write_note", {"text": "x", "explode": True})
    assert "fake" in str(excinfo.value) and "拒绝写入" in str(excinfo.value)

    # JSON-RPC error 也一样：消息里带服务器名和远端说明
    with pytest.raises(tool_api.ToolError) as excinfo:
        mcp_client.call_tool(server, "no_such_tool", {})
    assert "fake" in str(excinfo.value) and "no_such_tool" in str(excinfo.value)


def test_missing_command_reports_clear_error(registry):
    tools, register = registry
    mcp_client.save_servers([_server(command="narrative-no-such-mcp-server")])

    report = mcp_client.refresh(tool_api.Tool, register, tools=tools)
    message = report["errors"]["fake"]
    assert "fake" in message and "启动失败" in message
    assert tools == {}

    with pytest.raises(tool_api.ToolError) as excinfo:
        mcp_client.call_tool(mcp_client.load_servers()[0], "echo", {"text": "x"})
    assert "启动失败" in str(excinfo.value)


def test_crashed_server_never_raises_bare_exceptions(config_file):
    server = _server(env={"FAKE_MCP_CRASH_ON": "tools/call"})
    mcp_client.save_servers([server])

    for _attempt in range(2):  # 第一次是当场崩溃，第二次是会话已死必须重连
        with pytest.raises(tool_api.ToolError) as excinfo:
            mcp_client.call_tool(server, "echo", {"text": "x"})
        assert "fake" in str(excinfo.value)


def test_timeout_closes_session_and_reports(config_file):
    server = _server(timeout=0.5, env={"FAKE_MCP_SLEEP_ON": "tools/call"})

    with pytest.raises(tool_api.ToolError) as excinfo:
        mcp_client.call_tool(server, "echo", {"text": "x"})
    assert "超时" in str(excinfo.value) and "fake" in str(excinfo.value)
    assert mcp_client.open_sessions() == {}  # 超时的会话被关掉，不留僵尸进程


def test_http_transport_is_saved_but_refused_on_connect(registry):
    tools, register = registry
    web = mcp_client.McpServer.from_dict(
        {"name": "web", "transport": "http", "url": "https://example.invalid/mcp"})
    mcp_client.save_servers([web])
    assert mcp_client.load_servers()[0].transport == "http"

    report = mcp_client.refresh(tool_api.Tool, register, tools=tools)
    message = report["errors"]["web"]
    assert "stdio" in message and "web" in message
    assert tools == {}


# -- 缓存 -------------------------------------------------------------------


def test_cache_is_written_and_registers_without_server(registry):
    tools, register = registry
    mcp_client.save_servers([_server()])
    report = mcp_client.refresh(tool_api.Tool, register, tools=tools)
    assert report["errors"] == {}

    cache_file = mcp_client.default_cache_path()
    assert cache_file.is_file()
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    cached = payload["servers"]["fake"]["tools"]
    assert {tool["name"] for tool in cached} == {"echo", "write_note"}
    assert {tool["name"]: tool for tool in cached}["echo"]["inputSchema"]["required"] == ["text"]

    # 换一条起不来的命令：不连服务器，仅靠缓存也要把工具列出来
    mcp_client.close()
    mcp_client.save_servers([_server(command="narrative-no-such-mcp-server")])
    cached_tools, cached_register = _new_registry()
    mcp_client.register_cached(tool_api.Tool, cached_register, tools=cached_tools)
    assert set(cached_tools) == {LOCAL_ECHO, LOCAL_WRITE}
    assert cached_tools[LOCAL_ECHO].writes is False
    assert cached_tools[LOCAL_WRITE].writes is True
    # 缓存的 schema 能安全反序列化回 Tool
    assert cached_tools[LOCAL_ECHO].schema()["properties"]["text"] == {
        "type": "string", "description": "要回显的文本"}


def test_cache_ignores_broken_entries_and_unknown_servers(registry):
    tools, register = registry
    mcp_client.save_servers([_server()])
    cache_file = mcp_client.default_cache_path()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"version": 1, "servers": {
        "fake": {"tools": [
            {"name": "echo", "description": "只读",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
             "annotations": {"readOnlyHint": True}},
            {"name": "", "description": "空名字"},
            "不是对象",
            {"name": "bad", "inputSchema": "不是对象"},
        ]},
        "gone": {"tools": [{"name": "x"}]},
    }}, ensure_ascii=False), encoding="utf-8")

    mcp_client.register_cached(tool_api.Tool, register, tools=tools)
    assert set(tools) == {LOCAL_ECHO}  # 非法项逐条忽略；配置里没有的服务器整条过期
    assert tools[LOCAL_ECHO].writes is False


# -- 刷新容错 ---------------------------------------------------------------


def test_refresh_survives_one_broken_server(registry):
    tools, register = registry
    local_before = set(tool_api.TOOLS)
    mcp_client.save_servers([_server("good"),
                             _server("bad", command="narrative-no-such-mcp-server")])

    report = mcp_client.refresh(tool_api.Tool, register, tools=tools)
    assert "bad" in report["errors"] and "good" not in report["errors"]
    assert set(tools) == {"mcp__good__echo", "mcp__good__write_note"}
    assert mcp_client.last_errors() == {"bad": report["errors"]["bad"]}
    assert set(tool_api.TOOLS) == local_before  # 本地工具一个没动

    # 重复刷新：同名覆盖，不重复注册、不抛重名错误
    again = mcp_client.refresh(tool_api.Tool, register, tools=tools)
    assert set(tools) == {"mcp__good__echo", "mcp__good__write_note"}
    assert again["servers"]["good"]["ok"] is True
    assert again["servers"]["bad"]["ok"] is False


def test_disabled_server_is_neither_connected_nor_cached(registry):
    tools, register = registry
    mcp_client.save_servers([_server("off", enabled=False)])

    report = mcp_client.refresh(tool_api.Tool, register, tools=tools)
    assert report["servers"]["off"]["error"] == "已停用"
    assert tools == {}
    assert mcp_client.last_errors() == {}  # 停用不是错误


def test_refresh_async_registers_without_blocking(registry):
    tools, register = registry
    mcp_client.save_servers([_server()])
    reports: list[dict] = []

    thread = mcp_client.refresh_async(tool_api.Tool, register, tools=tools,
                                      on_done=reports.append)
    thread.join(30)
    assert not thread.is_alive()
    assert set(tools) == {LOCAL_ECHO, LOCAL_WRITE}
    assert reports and reports[0]["errors"] == {}


def test_startup_hook_contains_config_failures(config_file):
    """启动钩子只许留痕，不许把异常抛到应用启动路径上。"""
    config_file.write_text("这不是 JSON", encoding="utf-8")

    tool_api._register_mcp_tools()  # 不抛
    assert tool_api.MCP_STARTUP_ERROR and "McpConfigError" in tool_api.MCP_STARTUP_ERROR
    tool_api.MCP_STARTUP_ERROR = None
