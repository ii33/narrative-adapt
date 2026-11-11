"""工具数组是 prompt 的一部分：每条请求都要留下数量与顺序指纹，供缓存排查使用。"""
import http.client
import json

from test_gateway_turn_cancel import COMPLETE, _upstream

from narrative.harness import HarnessSettings
from narrative.model_gateway import ModelGateway, _tools_signature


def _registry(names):
    return [{"type": "function",
             "function": {"name": name, "description": name,
                          "parameters": {"type": "object", "properties": {}}}}
            for name in names]


def _ask(gateway, tools):
    connection = http.client.HTTPConnection("127.0.0.1", gateway._server.server_port, timeout=4)
    try:
        connection.request("POST", "/v1/chat/completions", json.dumps({
            "model": "offline", "messages": [{"role": "user", "content": "看看工程"}],
            "stream": True, "tools": tools}),
            {"Authorization": f"Bearer {gateway.token}", "Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200
        response.read()
    finally:
        connection.close()


def _signatures(gateway):
    rows = gateway.request_snapshot()
    return [(row["tools_count"], row["tools_hash"]) for row in rows]


def test_same_tool_set_keeps_the_same_signature():
    """同一会话连续两次请求、工具集不变：指纹必须一致，缓存前缀才可能命中。"""
    with _upstream(COMPLETE) as (port, _, release, requests):
        release.set()
        settings = HarnessSettings(provider="custom", base_url=f"http://127.0.0.1:{port}/v1", model="offline")
        gateway = ModelGateway(settings, "offline-key")
        gateway.tool_allowlist_provider = lambda: ("project_info", "load_tools")
        tools = _registry(["read_screenplay", "project_info", "load_tools"])
        try:
            with gateway.session("same-chat"):
                _ask(gateway, tools)
                _ask(gateway, tools)
            first, second = _signatures(gateway)
            assert first == second
            assert first[0] == 2
            assert len(requests) == 2
        finally:
            gateway.close()


def test_changed_tool_set_changes_the_signature():
    """中途加载了新工具：指纹必须变——这正是会让其后所有 token 失效的那件事。"""
    with _upstream(COMPLETE) as (port, _, release, requests):
        release.set()
        settings = HarnessSettings(provider="custom", base_url=f"http://127.0.0.1:{port}/v1", model="offline")
        gateway = ModelGateway(settings, "offline-key")
        visible = ["project_info", "load_tools"]
        gateway.tool_allowlist_provider = lambda: tuple(visible)
        tools = _registry(["read_screenplay", "project_info", "load_tools"])
        try:
            with gateway.session("same-chat"):
                _ask(gateway, tools)
                visible.append("read_screenplay")
                _ask(gateway, tools)
            before, after = _signatures(gateway)
            assert before[0] == 2 and after[0] == 3
            assert before[1] != after[1]
            assert len(requests) == 2
        finally:
            gateway.close()


def test_signature_matches_what_was_actually_sent():
    """tools_count 与实际发出的数组长度一致，hash 与按发送顺序拼名字的结果一致。"""
    with _upstream(COMPLETE) as (port, _, release, requests):
        release.set()
        settings = HarnessSettings(provider="custom", base_url=f"http://127.0.0.1:{port}/v1", model="offline")
        gateway = ModelGateway(settings, "offline-key")
        gateway.tool_allowlist_provider = lambda: ("project_info", "read_screenplay")
        tools = _registry(["read_screenplay", "project_info", "load_tools"])
        try:
            with gateway.session("same-chat"):
                _ask(gateway, tools)
            count, digest = _signatures(gateway)[0]
            sent = [tool["function"]["name"] for tool in requests[0]["tools"]]
            assert sent == ["project_info", "read_screenplay"]
            assert count == len(sent)
            assert (count, digest) == _tools_signature(requests[0]["tools"])
        finally:
            gateway.close()


def test_signature_is_none_when_request_has_no_tools():
    """没有 tools 字段时记 None，而不是伪造一个空指纹。"""
    assert _tools_signature(None) == (None, None)
    assert _tools_signature([]) == (0, _tools_signature([])[1])
