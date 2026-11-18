"""假 MCP Streamable HTTP 服务器：tests 专用，标准库 http.server 实现。

真服务器要装 mcp SDK、还要联网；这里只把客户端依赖的那几条协议行为钉成可测：

- ``initialize`` 回一条结果，并在响应头里给 ``Mcp-Session-Id``；
- ``notifications/initialized`` 只回 202，不带 body（规范如此，客户端不该等响应）；
- ``tools/list`` 可要求分页（跟 ``nextCursor`` 一页页给）；
- ``tools/call`` 三只工具：只读 ``echo``、写 ``write_note``、``explode`` 走 ``isError``；
- 两种响应形态：``application/json``（一整条响应）与 ``text/event-stream``（SSE）。

SSE 形态故意不是「一条流只装一条消息」：先发一条无关的通知事件、再发本次响应，
响应之后还可以挂一段尾巴——客户端要是把整条流读完才返回，用例里的耗时立刻就会超。
这样「认准 id 就返回」这条实现要求有可观测的证据，而不只是代码里的注释。

错误姿态用构造参数打开（不是环境变量：这些用例是同进程起服务器，直接传参最清楚）：
``error_modes={"tools/call": "http-500"}``。可选值见 ``ERROR_MODES``。

直接跑也行：``python tests/fake_http_mcp_server.py``（随机端口，打印 URL）。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "fake-http-mcp"
SERVER_VERSION = "0.0.1"
SESSION_ID = "fake-http-session-1"
ENDPOINT_PATH = "/mcp"
# 每页工具数：分页用例靠它确认「客户端确实带着 nextCursor 又请求了一次」。
PAGE_SIZE = 2
# 服务器能给客户端制造的麻烦，按 JSON-RPC 方法名挂上去。
ERROR_MODES = (
    "http-400",      # 直接拒绝这次请求（4xx）
    "http-500",      # 服务器炸了（5xx）
    "not-json",      # 200 + application/json，但 body 不是 JSON
    "sse-error",     # 200 + SSE，流里回一条 JSON-RPC error
    "slow",          # 迟迟不回，只回一条心跳，用来测超时
)


def _flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default) not in {"", "0"}


def _tools() -> list[dict]:
    echo = {
        "name": "echo",
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
    explode = {
        "name": "explode",
        "description": "永远失败（用 isError 报错）",
        "inputSchema": {"type": "object", "properties": {}},
    }
    return [echo, write_note, explode]


def _result(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeHttpMcp/0.0.1"

    # -- 入口 --

    @property
    def state(self) -> FakeHttpMcpServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: Any) -> None:
        """静音访问日志：测试输出里不该混进每个请求的 stderr。"""

    def do_DELETE(self) -> None:  # noqa: N802 - http.server 的约定命名
        self.state.record("DELETE", self.path, self.headers, None)
        # 规范允许服务器不接受 DELETE（回 405）；这里接受，好让用例确认客户端真的发了。
        self._send_empty(200)

    def do_POST(self) -> None:  # noqa: N802 - http.server 的约定命名
        raw = self._read_body()
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_json(400, _error(None, -32700, "请求体不是 JSON"))
            return
        if not isinstance(message, dict):
            self._send_json(400, _error(None, -32600, "请求体须为 JSON-RPC 对象"))
            return
        self.state.record("POST", self.path, self.headers, message)
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            # 会话从这里开始：响应头给 id，客户端之后每个请求都要回带。
            message = _result(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
            # 会话头必须两种形态都给：SSE 形态下客户端也得从响应头（不是 body）里认出它。
            headers = {"Mcp-Session-Id": self.state.session_id}
            if self.state.mode == "sse":
                self._send_sse([message], headers=headers)
            else:
                self._send_json(200, message, headers=headers)
            return

        # 除 initialize 之外的请求都必须带会话 id：缺了就是我们「服务端重启丢了会话」的样子。
        session_id = self.headers.get("Mcp-Session-Id")
        if self.state.require_session:
            if not session_id:
                self._send_json(400, _error(request_id, -32000, "缺少 Mcp-Session-Id"))
                return
            if session_id != self.state.session_id:
                self._send_json(404, _error(request_id, -32001, "会话已失效"))
                return

        mode = self.state.error_modes.get(str(method))
        if mode is not None and self._fail_mode(mode, request_id, method):
            return

        if method == "notifications/initialized":
            self._send_empty(202)  # 通知不需要 body：202 就够
            return
        if method == "ping":
            self._respond(request_id, _result(request_id, {}))
            return
        if method == "tools/list":
            params = message.get("params") or {}
            self._respond(request_id, _result(request_id, self.state.tools_page(params)))
            return
        if method == "tools/call":
            self._respond(request_id, self.state.call_tool(message.get("params") or {},
                                                          request_id))
            return
        self._respond(request_id, _error(request_id, -32601, f"不认识的方法：{method}"))

    # -- 坏姿态 --

    def _fail_mode(self, mode: str, request_id: Any, method: Any) -> bool:
        """按用例配置制造一种失败；返回 True 表示已经自己回过响应了。"""
        if mode == "http-400":
            self._send_json(400, _error(request_id, -32000, "假服务器拒绝这次请求"))
            return True
        if mode == "http-500":
            self._send_json(500, _error(request_id, -32603, "假服务器内部错误"))
            return True
        if mode == "not-json":
            body = "<html>这不是 JSON-RPC</html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return True
        if mode == "sse-error":
            self._send_sse([_error(request_id, -32002, f"假服务器在 SSE 里回了 {method} 的错误")])
            return True
        if mode == "slow":
            # 只推心跳、不给响应：客户端的超时必须由自己的 timeout 决定。
            self._send_sse([], tail_delay=self.state.slow_seconds, keepalive_only=True)
            return True
        return False

    # -- 响应 --

    def _respond(self, request_id: Any, message: dict, *, headers: dict[str, str] | None = None
                 ) -> None:
        """按配置的形态回一条消息：整条 JSON，或一条 SSE。"""
        if self.state.mode == "sse":
            self._send_sse([message], tail_delay=self.state.sse_tail_delay)
        else:
            self._send_json(200, message, headers=headers)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _send_json(self, status: int, payload: Any, *,
                   headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.wfile.flush()

    def _send_sse(self, messages: list[dict], *, tail_delay: float = 0.0,
                  keepalive_only: bool = False,
                  headers: dict[str, str] | None = None) -> None:
        """写一条 SSE 流：先一条无关通知（逼客户端按 id 挑），再给真响应，最后可选尾巴。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        # 不切 chunked：Connection: close + 无 Content-Length 的响应对 urllib 同样是流式的，
        # 而且少一层分块编码，读坏的可能是自己写错而不是分块没解开。
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self._write_event({"jsonrpc": "2.0", "method": "notifications/message",
                           "params": {"level": "info", "data": "假服务器的招呼"}})
        for message in messages:
            self._write_event(message)
        if keepalive_only:
            # 只推注释行（心跳）：服务端还在，但永远不给答案。
            deadline = time.monotonic() + tail_delay
            while not self.state.stopping and time.monotonic() < deadline:
                self.state.stop_event.wait(min(0.2, max(0.0, deadline - time.monotonic())))
                with contextlib.suppress(OSError):
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        elif tail_delay > 0:
            # 尾巴：响应已经发出去了，流还没结束。读完整条流的客户端会在这里卡住。
            self.state.stop_event.wait(tail_delay)
            self._write_event({"jsonrpc": "2.0", "method": "notifications/message",
                               "params": {"level": "info", "data": "尾巴"}})

    def _write_event(self, payload: Any) -> None:
        body = (f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n")
        # 连接被客户端提前关掉时，写会抛 OSError；假服务器不该因此打印一屏回溯。
        with contextlib.suppress(OSError):
            self.wfile.write(body.encode("utf-8"))
            self.wfile.flush()


class FakeHttpMcpServer(ThreadingHTTPServer):
    """随机端口上的假 Streamable HTTP MCP 服务器。

    用例这样用::

        with FakeHttpMcpServer(mode="sse") as fake:
            server = mcp_client.McpServer.from_dict(
                {"name": "web", "transport": "http", "url": fake.url, "timeout": 5})
    """

    daemon_threads = True  # 用例结束不等残留连接：假服务器自己先关
    allow_reuse_address = True

    def __init__(self, *, mode: str = "json", paginate: bool = False,
                 require_session: bool = True, session_id: str = SESSION_ID,
                 error_modes: dict[str, str] | None = None, slow_seconds: float = 5.0,
                 sse_tail_delay: float = 0.0) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)  # 0 = 让系统给一个空闲端口
        self.mode = mode
        self.paginate = paginate
        self.require_session = require_session
        self.session_id = session_id
        self.error_modes = dict(error_modes or {})
        self.slow_seconds = slow_seconds
        self.sse_tail_delay = sse_tail_delay
        self.requests: list[dict[str, Any]] = []
        self._log_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.stopping = False
        self._thread: threading.Thread | None = None

    # -- 生命周期 --

    def start(self) -> FakeHttpMcpServer:
        self._thread = threading.Thread(target=self.serve_forever,
                                        name="fake-http-mcp", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """可以重复调：用例里提前停一次、上下文退出时再停一次是常态。"""
        if self.stopping:
            return
        self.stopping = True
        self.stop_event.set()
        self.shutdown()
        self.server_close()

    def __enter__(self) -> FakeHttpMcpServer:
        return self.start()

    def __exit__(self, *_exception) -> None:
        self.stop()

    # -- 给用例看的 --

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}{ENDPOINT_PATH}"

    def record(self, verb: str, path: str, headers: Any, message: dict | None) -> None:
        with self._log_lock:
            self.requests.append({
                "method": verb,
                "path": path,
                # 头部名大小写不敏感：客户端走 urllib，发出去的大小写由它决定。
                "headers": {str(key).lower(): str(value) for key, value in headers.items()},
                "body": message,
                "rpc_method": message.get("method") if isinstance(message, dict) else None,
            })

    def requests_for(self, rpc_method: str) -> list[dict[str, Any]]:
        return [row for row in self.requests if row["rpc_method"] == rpc_method]

    def delete_count(self) -> int:
        return sum(1 for row in self.requests if row["method"] == "DELETE")

    # -- 协议内容 --

    def tools_page(self, params: dict) -> dict:
        tools = _tools()
        if not self.paginate:
            return {"tools": tools}
        cursor = params.get("cursor")
        offset = 0
        if isinstance(cursor, str) and cursor:
            try:
                offset = int(cursor)
            except ValueError:
                offset = len(tools)
        page = tools[offset:offset + PAGE_SIZE]
        result: dict[str, Any] = {"tools": page}
        if offset + PAGE_SIZE < len(tools):
            result["nextCursor"] = str(offset + PAGE_SIZE)
        return result

    def call_tool(self, params: dict, request_id: Any) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "arguments 须为对象")
        text = str(arguments.get("text") or "")
        if name == "echo":
            return _result(request_id, {"content": [{"type": "text", "text": text}]})
        if name == "write_note":
            return _result(request_id, {"content": [
                {"type": "text", "text": "已写：" + text},
                {"type": "image", "mimeType": "image/png", "data": ""},
            ]})
        if name == "explode":
            return _result(request_id, {
                "isError": True,
                "content": [{"type": "text", "text": "炸了：假服务器拒绝执行"}],
            })
        return _error(request_id, -32602, f"没有这个工具：{name}")


def main() -> int:
    fake = FakeHttpMcpServer(
        mode=os.environ.get("FAKE_HTTP_MODE", "json"),
        paginate=_flag("FAKE_HTTP_PAGINATE"),
        sse_tail_delay=float(os.environ.get("FAKE_HTTP_SSE_TAIL", "0") or 0),
    )
    fake.start()
    print(f"假 MCP HTTP 服务器：{fake.url}（Session id {fake.session_id}，Ctrl-C 结束）",
          flush=True)
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("收尾", flush=True)
    finally:
        fake.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
