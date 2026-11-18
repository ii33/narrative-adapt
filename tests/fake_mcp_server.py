"""假 MCP 服务器：tests 专用，标准库实现的 newline-delimited JSON-RPC 2.0。

真服务器要装 mcp SDK、还要联网；这里只把客户端依赖的那几条协议行为钉成可测：
initialize、notifications/initialized、ping、tools/list、tools/call。
两只工具一个只读（echo）、一个写（write_note）。

异常姿态用环境变量打开，免得为每种情况抄一份脚本：
- FAKE_MCP_PAGINATE=1      tools/list 分两页给，带 nextCursor
- FAKE_MCP_ODD_NAMES=1     echo 的工具名变成 read-file.v2（测非法字符折叠）
- FAKE_MCP_CRASH_ON=<method>  收到该方法就直接退出码 3 死掉（不给任何响应）
- FAKE_MCP_SLEEP_ON=<method>  收到该方法就睡 60 秒（测客户端超时）

直接跑也行：``python tests/fake_mcp_server.py``。
"""

from __future__ import annotations

import json
import os
import sys
import time

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "fake-mcp"
SERVER_VERSION = "0.0.1"


def _flag(name: str) -> bool:
    return os.environ.get(name, "") not in {"", "0"}


def _tools() -> list[dict]:
    echo = {
        "name": "read-file.v2" if _flag("FAKE_MCP_ODD_NAMES") else "echo",
        "description": "原样回显文本（只读）",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    }
    write_note = {
        "name": "write_note",
        "description": "写一条笔记（会改状态）",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
    return [echo, write_note]


def _result(request_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: dict) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(request_id, -32602, "params 须为对象")
    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        tools = _tools()
        if _flag("FAKE_MCP_PAGINATE"):
            cursor = params.get("cursor")
            if cursor is None:
                return _result(request_id, {"tools": tools[:1], "nextCursor": "1"})
            if cursor == "1":
                return _result(request_id, {"tools": tools[1:]})
            return _result(request_id, {"tools": []})
        return _result(request_id, {"tools": tools})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "arguments 须为对象")
        tools = {tool["name"]: tool for tool in _tools()}
        if name not in tools:
            return _error(request_id, -32602, f"没有这个工具：{name}")
        if name == "write_note" and arguments.get("explode"):
            return _result(request_id, {
                "isError": True,
                "content": [{"type": "text", "text": "拒绝写入：笔记库只读"}],
            })
        text = str(arguments.get("text") or "")
        if name == "write_note":
            return _result(request_id, {"content": [
                {"type": "text", "text": "已写：" + text},
                {"type": "image", "mimeType": "image/png", "data": ""},
            ]})
        return _result(request_id, {"content": [{"type": "text", "text": text}]})
    return _error(request_id, -32601, f"不认识的方法：{method}")


def main() -> int:
    # 管道里 Python 默认按 locale 解码；中文系统（cp936）会把 UTF-8 的 JSON 解坏。
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8", errors="replace")
    crash_on = os.environ.get("FAKE_MCP_CRASH_ON", "")
    sleep_on = os.environ.get("FAKE_MCP_SLEEP_ON", "")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        if crash_on and method == crash_on:
            os._exit(3)  # 真崩：不给响应，客户端只能靠输出流关闭/超时发现
        if sleep_on and method == sleep_on:
            time.sleep(60)
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
