"""MCP 客户端的 Streamable HTTP 传输（MCP 2025-06-18）。

一律用 tests/fake_http_mcp_server.py 这个标准库假服务器，起在 127.0.0.1 的随机端口，
不联网、不要 mcp SDK；配置路径与缓存通过 NARRATIVE_MCP_FILE 指到 tmp_path，
绝不碰真实用户目录。注册一律走本文件自己的注册表，不往全局 tool_api.TOOLS 里塞东西。

重点钉住四件事：
1. 握手拿到 Mcp-Session-Id 之后，后续每个请求都要回带它和 MCP-Protocol-Version；
2. 两种响应形态（一整条 JSON / SSE 流）走的是同一条上层路径，行为一致；
3. 服务器各种「不好」都翻成 McpError/McpRpcError，最后由工具面变成 ToolError；
4. http 与 stdio 并存时，一台坏掉不影响另一台。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fake_http_mcp_server import FakeHttpMcpServer

from narrative import mcp_client, tool_api

FAKE_STDIO = Path(__file__).with_name("fake_mcp_server.py")
SERVER_NAME = "web"


def _new_registry():
    """独立注册表 + 模拟 tool_api._register 的重名拒绝语义。"""
    tools: dict[str, tool_api.Tool] = {}

    def register(tool):
        if tool.name in tools:
            raise ValueError(f"Duplicate tool registration: {tool.name}")
        tools[tool.name] = tool

    return tools, register


def _http_server(fake: FakeHttpMcpServer, name: str = SERVER_NAME,
                 **changes) -> mcp_client.McpServer:
    data = {"name": name, "transport": "http", "url": fake.url, "timeout": 5}
    data.update(changes)
    return mcp_client.McpServer.from_dict(data)


def _stdio_server(name: str = "local", **changes) -> mcp_client.McpServer:
    data = {"name": name, "transport": "stdio", "command": sys.executable,
            "args": [str(FAKE_STDIO)], "timeout": 10}
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


# -- 握手与会话头 -----------------------------------------------------------


def test_initialize_keeps_session_id_and_protocol_version_headers(config_file):
    with FakeHttpMcpServer() as fake:
        server = _http_server(fake)
        session = mcp_client.HttpSession(server)
        assert session.alive is False  # 还没 start
        session.start()
        try:
            result = session.initialize()
            assert result["protocolVersion"] == mcp_client.PROTOCOL_VERSION
            assert session.protocol_version == mcp_client.PROTOCOL_VERSION
            assert session.server_info["name"] == "fake-http-mcp"
            assert session.session_id == fake.session_id
            assert session.alive is True
            assert session.ping() == {}
            assert [tool["name"] for tool in session.list_tools()] == [
                "echo", "write_note", "explode"]
        finally:
            session.close()
        assert session.alive is False

        # 握手那一次：还没有会话 id，版本也还没协商，两个头都不该带
        handshake = fake.requests_for("initialize")[0]
        assert "mcp-session-id" not in handshake["headers"]
        assert "mcp-protocol-version" not in handshake["headers"]
        assert handshake["headers"]["content-type"] == "application/json"
        assert handshake["headers"]["accept"] == "application/json, text/event-stream"

        # initialize 之后：通知与每个请求都要回带会话 id 与协商版本
        for rpc_method in ("notifications/initialized", "ping", "tools/list"):
            row = fake.requests_for(rpc_method)[0]
            assert row["headers"]["mcp-session-id"] == fake.session_id, rpc_method
            assert row["headers"]["mcp-protocol-version"] == mcp_client.PROTOCOL_VERSION, rpc_method
            assert row["path"] == "/mcp"


def test_notification_expects_only_202_and_no_body(config_file):
    """initialized 通知不带 id；服务器回 202 空 body，客户端不该等一条响应。"""
    with FakeHttpMcpServer() as fake:
        server = _http_server(fake)
        with mcp_client.HttpSession(server) as session:
            session.initialize()
        notification = fake.requests_for("notifications/initialized")[0]["body"]
        assert "id" not in notification
        assert notification["jsonrpc"] == "2.0"


# -- 两种响应形态 -----------------------------------------------------------


@pytest.mark.parametrize("mode", ["json", "sse"])
def test_list_and_call_work_in_both_response_shapes(mode, registry, config_file):
    tools, register = registry
    with FakeHttpMcpServer(mode=mode) as fake:
        server = _http_server(fake)
        mcp_client.save_servers([server])

        report = mcp_client.refresh(tool_api.Tool, register, tools=tools)
        assert report["errors"] == {}
        assert set(tools) == {"mcp__web__echo", "mcp__web__write_note", "mcp__web__explode"}

        echo = tools["mcp__web__echo"]
        assert echo.writes is False  # 远端 readOnlyHint=true
        assert tools["mcp__web__write_note"].writes is True
        result = echo.handler(None, {"text": "你好，世界"})
        assert result == {"server": "web", "tool": "echo", "content": "你好，世界"}
        note = tools["mcp__web__write_note"].handler(None, {"text": "第一章"})
        assert note["other_content"] == ["[图片 image/png]"]


def test_sse_response_returns_before_the_stream_ends(registry, config_file):
    """SSE 里先有一条无关通知，响应后面还挂着尾巴：客户端该认准 id 立刻返回。"""
    tools, register = registry
    # 尾巴比用例的超时短得多，但比「读完整条流」的代价长得多：读到底的实现在这里露馅。
    with FakeHttpMcpServer(mode="sse", sse_tail_delay=3.0) as fake:
        server = _http_server(fake, timeout=10)
        mcp_client.save_servers([server])
        started = time.monotonic()
        mcp_client.refresh(tool_api.Tool, register, tools=tools)
        elapsed = time.monotonic() - started
        assert set(tools) == {"mcp__web__echo", "mcp__web__write_note", "mcp__web__explode"}
        assert elapsed < 2.0, f"没在拿到响应时返回，等了 {elapsed:.2f} 秒"


def test_paginated_tools_list_follows_cursor(registry, config_file):
    tools, register = registry
    with FakeHttpMcpServer(mode="sse", paginate=True) as fake:
        server = _http_server(fake)
        mcp_client.save_servers([server])

        report = mcp_client.refresh(tool_api.Tool, register, tools=tools)
        assert report["errors"] == {}
        assert set(tools) == {"mcp__web__echo", "mcp__web__write_note", "mcp__web__explode"}
        # 两页：第一页没有游标，第二页带着服务器给的 nextCursor
        pages = [row["body"].get("params") or {} for row in fake.requests_for("tools/list")]
        assert pages == [{}, {"cursor": "2"}]


# -- 错误映射 ---------------------------------------------------------------


def test_is_error_becomes_tool_error(registry, config_file):
    with FakeHttpMcpServer() as fake:
        server = _http_server(fake)
        with pytest.raises(tool_api.ToolError) as excinfo:
            mcp_client.call_tool(server, "explode", {})
        assert "web" in str(excinfo.value) and "炸了" in str(excinfo.value)


def test_missing_session_id_is_reported_with_status(config_file):
    """服务端丢了会话（400）：消息里要有服务器名和状态码，不能是 urllib 的裸异常。"""
    with FakeHttpMcpServer() as fake:
        server = _http_server(fake)
        with mcp_client.HttpSession(server) as session:
            session.initialize()
            session.session_id = None  # 模拟「本地丢了会话 id」
            with pytest.raises(mcp_client.McpError) as excinfo:
                session.list_tools()
        message = str(excinfo.value)
        assert "web" in message and "400" in message
        assert "缺少 Mcp-Session-Id" in message  # 服务器自己的说明也带上了
        assert not isinstance(excinfo.value, mcp_client.McpRpcError)


def test_http_500_becomes_clear_tool_error(registry, config_file):
    tools, register = registry
    with FakeHttpMcpServer(error_modes={"tools/call": "http-500"}) as fake:
        server = _http_server(fake)
        mcp_client.save_servers([server])
        report = mcp_client.refresh(tool_api.Tool, register, tools=tools)
        assert report["errors"] == {}  # tools/list 没事，只有调用会 500

        with pytest.raises(tool_api.ToolError) as excinfo:
            mcp_client.call_tool(server, "echo", {"text": "x"})
        message = str(excinfo.value)
        assert "web" in message and "500" in message and "假服务器内部错误" in message


def test_non_json_body_is_reported_clearly(registry, config_file):
    tools, register = registry
    with FakeHttpMcpServer(error_modes={"tools/list": "not-json"}) as fake:
        server = _http_server(fake)
        mcp_client.save_servers([server])
        report = mcp_client.refresh(tool_api.Tool, register, tools=tools)

        message = report["errors"]["web"]
        assert "不是 JSON" in message and "web" in message
        assert "这不是 JSON-RPC" in message  # 顺手给出服务器回的片段
        assert tools == {}


def test_sse_rpc_error_becomes_tool_error(registry, config_file):
    tools, register = registry
    with FakeHttpMcpServer(mode="sse", error_modes={"tools/call": "sse-error"}) as fake:
        server = _http_server(fake)
        mcp_client.save_servers([server])
        assert mcp_client.refresh(tool_api.Tool, register, tools=tools)["errors"] == {}

        # 会话层：JSON-RPC error 就是 McpRpcError，消息里带服务器名与远端错误码
        with mcp_client.HttpSession(server) as session:
            session.initialize()
            with pytest.raises(mcp_client.McpRpcError) as rpc:
                session.call_tool("echo", {"text": "x"})
        assert "web" in str(rpc.value) and "-32002" in str(rpc.value)

        # 工具面：翻成 ToolError，远端原话留着
        with pytest.raises(tool_api.ToolError) as excinfo:
            mcp_client.call_tool(server, "echo", {"text": "x"})
        message = str(excinfo.value)
        assert "web" in message and "SSE" in message


def test_timeout_closes_the_session(config_file):
    with FakeHttpMcpServer(slow_seconds=3.0, error_modes={"tools/call": "slow"}) as fake:
        server = _http_server(fake, timeout=0.5)
        started = time.monotonic()
        with pytest.raises(tool_api.ToolError) as excinfo:
            mcp_client.call_tool(server, "echo", {"text": "x"})
        assert time.monotonic() - started < 3.0  # 心跳吊着也不能拖过自己的超时
        message = str(excinfo.value)
        assert "超时" in message and "web" in message
        assert mcp_client.open_sessions() == {}  # 作废的会话不留在登记表里


def test_unknown_tool_reports_rpc_error(config_file):
    with FakeHttpMcpServer() as fake:
        server = _http_server(fake)
        with pytest.raises(tool_api.ToolError) as excinfo:
            mcp_client.call_tool(server, "no_such_tool", {})
        assert "web" in str(excinfo.value) and "no_such_tool" in str(excinfo.value)


# -- 收尾 -------------------------------------------------------------------


def test_close_sends_delete_and_clears_registry(registry, config_file):
    tools, register = registry
    with FakeHttpMcpServer() as fake:
        server = _http_server(fake)
        mcp_client.save_servers([server])
        mcp_client.refresh(tool_api.Tool, register, tools=tools)
        assert list(mcp_client.open_sessions()) == ["web"]

        mcp_client.close()
        assert mcp_client.open_sessions() == {}
        mcp_client.close()  # 再关一次不该发第二条 DELETE
        assert fake.delete_count() == 1

        delete = [row for row in fake.requests if row["method"] == "DELETE"][0]
        assert delete["path"] == "/mcp"
        assert delete["headers"]["mcp-session-id"] == fake.session_id


# -- 与 stdio 并存 ----------------------------------------------------------


def test_http_and_stdio_refresh_do_not_disturb_each_other(registry, config_file):
    tools, register = registry
    local_before = set(tool_api.TOOLS)
    with FakeHttpMcpServer() as fake:
        mcp_client.save_servers([_stdio_server("local"), _http_server(fake, name="web")])

        report = mcp_client.refresh(tool_api.Tool, register, tools=tools)
        assert report["errors"] == {}
        assert set(tools) == {
            "mcp__local__echo", "mcp__local__write_note",
            "mcp__web__echo", "mcp__web__write_note", "mcp__web__explode"}
        # 两台各走自己的路径：stdio 的调用结果和 http 的调用结果都正常
        assert tools["mcp__local__echo"].handler(None, {"text": "本地"})["content"] == "本地"
        assert tools["mcp__web__echo"].handler(None, {"text": "远端"})["content"] == "远端"

        # 把 http 那台整个关掉，再刷一次：stdio 那台照旧，坏的那台只写在自己的错误里
        fake.stop()
        again = mcp_client.refresh(tool_api.Tool, register, tools=tools)
        assert "web" in again["errors"] and "local" not in again["errors"]
        assert again["servers"]["local"]["ok"] is True
        assert {"mcp__local__echo", "mcp__local__write_note"} <= set(tools)
        assert list(mcp_client.open_sessions()) == ["local"]
        assert set(tool_api.TOOLS) == local_before  # 本地工具一个没动


# -- 配置校验 ---------------------------------------------------------------


def test_http_entry_requires_a_usable_url(config_file):
    with pytest.raises(mcp_client.McpConfigError) as excinfo:
        mcp_client.McpServer.from_dict({"name": "web", "transport": "http"})
    message = str(excinfo.value)
    assert "web" in message and "url" in message and "第 1 项" in message

    for bad_url in ("ftp://example.invalid/mcp", "example.invalid/mcp", "https:///mcp"):
        with pytest.raises(mcp_client.McpConfigError) as excinfo:
            mcp_client.McpServer.from_dict(
                {"name": "web", "transport": "http", "url": bad_url}, index=2)
        message = str(excinfo.value)
        assert "web" in message and "url" in message and "第 3 项" in message


def test_stdio_entry_requires_a_command(config_file):
    with pytest.raises(mcp_client.McpConfigError) as excinfo:
        mcp_client.McpServer.from_dict({"name": "half", "transport": "stdio"}, index=4)
    message = str(excinfo.value)
    assert "half" in message and "command" in message and "第 5 项" in message


def test_bad_url_never_reaches_urllib(config_file):
    """手写的 Server 对象（绕过 from_dict）也要在 start 时就拒绝，别让 urllib 抛裸异常。"""
    server = mcp_client.McpServer(name="odd", transport="http", url="ftp://example.invalid/mcp")

    with pytest.raises(mcp_client.McpError) as excinfo:
        mcp_client.HttpSession(server).start()
    assert "odd" in str(excinfo.value) and "url" in str(excinfo.value)

    with pytest.raises(tool_api.ToolError) as excinfo:
        mcp_client.call_tool(server, "echo", {"text": "x"})
    assert "odd" in str(excinfo.value) and "url" in str(excinfo.value)

    # 走配置文件那条路拒绝得更早：读配置就报错，指名第几项与哪台服务器
    mcp_client.save_servers([server])
    with pytest.raises(mcp_client.McpConfigError) as excinfo:
        mcp_client.load_servers()
    assert "odd" in str(excinfo.value) and "url" in str(excinfo.value)


# -- 缓存 -------------------------------------------------------------------


def test_cached_http_tools_register_without_connecting(registry, config_file):
    tools, register = registry
    with FakeHttpMcpServer(mode="sse") as fake:
        mcp_client.save_servers([_http_server(fake)])
        assert mcp_client.refresh(tool_api.Tool, register, tools=tools)["errors"] == {}
        cache_file = mcp_client.default_cache_path()
        cached = json.loads(cache_file.read_text(encoding="utf-8"))["servers"]["web"]["tools"]
        assert {tool["name"] for tool in cached} == {"echo", "write_note", "explode"}

        # 服务器已经停了：只用缓存也要能列出工具，且不去连它（请求数不变）
        fake.stop()
        mcp_client.close()
        requests_before = len(fake.requests)
        cached_tools, cached_register = _new_registry()
        mcp_client.register_cached(tool_api.Tool, cached_register, tools=cached_tools)

        assert set(cached_tools) == {
            "mcp__web__echo", "mcp__web__write_note", "mcp__web__explode"}
        assert cached_tools["mcp__web__echo"].writes is False
        assert cached_tools["mcp__web__write_note"].writes is True
        assert cached_tools["mcp__web__echo"].schema()["properties"]["text"] == {
            "type": "string", "description": "要回显的文本"}
        assert len(fake.requests) == requests_before
        assert mcp_client.open_sessions() == {}
