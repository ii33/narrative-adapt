"""MCP 客户端：把外部 MCP 服务器的工具并进本地工具表。

协作者要能用「别人的工具」：这里连上一台远端 MCP 服务器，把 ``tools/list``
变成我们自己的 ``Tool``。两种传输各有一个会话类，对外方法完全一致：

- ``McpSession``：stdio。起子进程，说 newline-delimited JSON-RPC 2.0。
- ``HttpSession``：Streamable HTTP（MCP 2025-06-18）。一个端点 URL，POST 一条消息，
  响应可能是一整条 JSON，也可能是一条 SSE 流。

``mcp_server.py`` 是反方向的另一件事：把本工程的工具递给外部模型。

不引第三方 SDK 的理由：只需要 initialize / tools.list / tools.call / ping 四件事，
标准库够用（http 传输用 ``urllib.request``，SSE 自己按行解）；少一个依赖就少一份
版本纠缠与离线安装风险。

配置是全局的（跨工程共用），所以落在用户目录，而不是某个工程里。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .version import version_number

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "narrative-collaborator"
# 与 provider_store 同一套口径：默认用户目录，环境变量可整体改到别处（测试/便携版）。
CONFIG_ENV = "NARRATIVE_MCP_FILE"
CONFIG_DIR = ".narrative"
CONFIG_NAME = "mcp-servers.json"
CACHE_NAME = "mcp-tools-cache.json"
CONFIG_VERSION = 1
DEFAULT_TIMEOUT = 20.0
CLOSE_GRACE_SECONDS = 2.0
TOOL_PREFIX = "mcp__"
SCOPE = "mcp"
TRANSPORTS = ("stdio", "http")

# -- http 传输的常量 --------------------------------------------------------
HTTP_SCHEMES = ("http", "https")
MIME_JSON = "application/json"
MIME_SSE = "text/event-stream"
# 规范要求客户端同时接受这两种：服务器可以就为这一条消息开一条 SSE 流。
ACCEPT_HEADER = f"{MIME_JSON}, {MIME_SSE}"
# 头部名大小写不敏感，这里统一用规范里的写法；urllib 会按它自己的习惯改写大小写。
SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"
# 错误消息里带上服务器原话，但不许把一大坨 HTML 整段塞进界面。
SNIPPET_LIMIT = 200

# 工具名只允许 [A-Za-z0-9_]：别的字符一律折成下划线，避免模型端按名字拼错。
_NAMESPACE = re.compile(r"[^A-Za-z0-9_]")


class McpConfigError(ValueError):
    """配置文件读不懂或写不进；消息里带路径，界面可以直接显示给用户。"""


class McpError(RuntimeError):
    """连接、进程或协议层面的失败；工具面会把它转成 ToolError，不让它穿到界面。"""


class McpRpcError(McpError):
    """远端用 JSON-RPC error 回了一个请求。"""

    def __init__(self, method: str, error: Any, *, prefix: str = ""):
        body = error if isinstance(error, dict) else {"message": error}
        self.code = body.get("code")
        self.rpc_message = str(body.get("message") or "没有说明")
        # prefix 给传输层补上下文用（http 会带上服务器名）：这条消息也会直接进日志与
        # 界面，光有一句「tools/call 返回错误」看不出是哪台服务器说的。
        super().__init__(f"{prefix}{method} 返回错误 {self.code}：{self.rpc_message}")


def normalize_url(url: Any) -> str:
    """校验并规整 http 端点地址，不合格就抛 ValueError（消息直接给人看）。

    http 服务器只有一个身份：那个 URL。写错了必须当场指出来——等到握手时才报
    「连接失败」，用户分不清是地址写错还是服务器没开。
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("缺少 url（http 传输必须给出端点地址，例如 http://127.0.0.1:8765/mcp）")
    text = url.strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme.lower() not in HTTP_SCHEMES:
        raise ValueError(f"url 须以 http:// 或 https:// 开头，收到 {text!r}")
    if not parsed.netloc:
        raise ValueError(f"url 缺少主机名，收到 {text!r}")
    return text


@dataclass(frozen=True)
class McpServer:
    """一台外部 MCP 服务器的全局配置。名字就是它的身份，工具前缀也取自它。"""

    name: str
    enabled: bool = True
    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    timeout: float = DEFAULT_TIMEOUT

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "enabled": self.enabled,
            "transport": self.transport,
            "timeout": self.timeout,
        }
        if self.transport == "http":
            data["url"] = self.url
        else:
            data.update({"command": self.command, "args": list(self.args),
                         "env": dict(self.env), "cwd": self.cwd})
        return data

    @classmethod
    def from_dict(cls, data: Any, *, index: int = 0) -> McpServer:
        """从配置里的一项建服务器；字段不对就报到具体哪一项，不猜。"""
        where = f"第 {index + 1} 项"
        if not isinstance(data, dict):
            raise McpConfigError(f"MCP 配置{where}不是对象")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise McpConfigError(f"MCP 配置{where}缺少 name")
        name = name.strip()
        transport = data.get("transport") or "stdio"
        if transport not in TRANSPORTS:
            raise McpConfigError(f"MCP 服务器 {name} 的 transport 须为 stdio 或 http，收到 {transport!r}")
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise McpConfigError(f"MCP 服务器 {name} 的 enabled 须为 true/false")
        timeout = data.get("timeout", DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise McpConfigError(f"MCP 服务器 {name} 的 timeout 须为正数秒")
        args = data.get("args") or []
        if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
            raise McpConfigError(f"MCP 服务器 {name} 的 args 须为字符串数组")
        env = data.get("env") or {}
        if not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                           for key, value in env.items()):
            raise McpConfigError(f"MCP 服务器 {name} 的 env 须为字符串键值对")
        cwd = data.get("cwd") or ""
        if not isinstance(cwd, str):
            raise McpConfigError(f"MCP 服务器 {name} 的 cwd 须为字符串")
        if transport == "http":
            # 地址不合法的条目在「读配置」这一步就拒绝：面板要显示的是哪一项错了，
            # 而不是等用户点了「测试连接」才看到一句连接失败。
            try:
                url = normalize_url(data.get("url"))
            except ValueError as exc:
                raise McpConfigError(
                    f"MCP 配置{where}（服务器 {name}）的 http url 不对：{exc}") from exc
            return cls(name=name, enabled=enabled, transport="http", url=url,
                       timeout=float(timeout))
        command = data.get("command")
        if not isinstance(command, str) or not command.strip():
            raise McpConfigError(
                f"MCP 配置{where}（服务器 {name}）用 stdio 传输但没有 command")
        return cls(name=name, enabled=enabled, transport="stdio", command=command.strip(),
                   args=tuple(args), env=dict(env), cwd=cwd, timeout=float(timeout))


# -- 配置文件 ----------------------------------------------------------------


def default_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    return Path.home() / CONFIG_DIR / CONFIG_NAME


def default_cache_path(config_path: str | Path | None = None) -> Path:
    """缓存跟着配置走：同一目录下的 mcp-tools-cache.json，方便整体搬走或删掉。"""
    base = Path(config_path) if config_path else default_path()
    return base.with_name(CACHE_NAME)


def load_servers(path: str | Path | None = None) -> list[McpServer]:
    target = Path(path) if path else default_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []  # 没配过 = 没有外部服务器，不是错误
    except OSError as exc:
        raise McpConfigError(f"读不到 MCP 配置文件 {target}：{exc}") from exc
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise McpConfigError(
            f"MCP 配置文件 {target} 不是合法 JSON（{exc}）；原文件没有改动，"
            f"改好再试，或用 save_servers 重写（重写前会备份成 {target.name}.bak）"
        ) from exc
    rows = data.get("servers") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise McpConfigError(f"MCP 配置文件 {target} 的 servers 须为数组")
    servers = [McpServer.from_dict(row, index=index) for index, row in enumerate(rows)]
    names = [server.name for server in servers]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise McpConfigError(f"MCP 配置文件 {target} 里服务器重名：{'、'.join(duplicated)}")
    return servers


def save_servers(servers: Iterable[McpServer], path: str | Path | None = None) -> Path:
    target = Path(path) if path else default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    _backup_unreadable(target)
    payload = {"version": CONFIG_VERSION, "servers": [server.to_dict() for server in servers]}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = target.with_name(target.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)  # 原子替换：半个配置文件比旧配置更糟
    except OSError as exc:
        raise McpConfigError(f"写不进 MCP 配置文件 {target}：{exc}") from exc
    return target


def _backup_unreadable(target: Path) -> Path | None:
    """读不懂的旧配置先备份成 .bak 再覆盖：手写的服务器清单不能因为一次保存就消失。"""
    if not target.is_file():
        return None
    try:
        json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        backup = target.with_name(target.name + ".bak")
        try:
            shutil.copyfile(target, backup)
        except OSError:
            return None
        return backup
    return None


# -- 工具清单缓存 ------------------------------------------------------------


def load_cache(path: str | Path | None = None) -> dict[str, list[dict]]:
    """读上次成功的工具清单；读不动、条目不对就当没有，缓存不能挡住启动。"""
    target = default_cache_path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return {}
    cache: dict[str, list[dict]] = {}
    for name, entry in rows.items():
        tools = entry.get("tools") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not isinstance(tools, list):
            continue
        cache[name] = [item for item in (_valid_cached_tool(row) for row in tools)
                       if item is not None]
    return cache


def save_cache(entries: dict[str, list[dict]], path: str | Path | None = None) -> Path:
    target = default_cache_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CONFIG_VERSION,
               "servers": {name: {"tools": tools} for name, tools in entries.items()}}
    temporary = target.with_name(target.name + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        os.replace(temporary, target)
    except OSError as exc:
        raise McpConfigError(f"写不进 MCP 工具缓存 {target}：{exc}") from exc
    return target


def _valid_cached_tool(row: Any) -> dict | None:
    """一条缓存项能不能安全地变回 Tool；不能就返回 None（整条忽略，不修补）。"""
    if not isinstance(row, dict):
        return None
    name = row.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    schema = row.get("inputSchema")
    if schema is not None and not isinstance(schema, dict):
        return None  # 坏 schema 会随 Tool.schema() 直接递给模型，宁可不要这只工具
    description = row.get("description")
    annotations = row.get("annotations")
    return {
        "name": name.strip(),
        "description": description if isinstance(description, str) else "",
        "inputSchema": dict(schema) if isinstance(schema, dict)
        else {"type": "object", "properties": {}},
        "annotations": annotations if isinstance(annotations, dict) else {},
    }


def _cache_entry(remote: dict) -> dict:
    schema = remote.get("inputSchema")
    entry: dict[str, Any] = {
        "name": str(remote.get("name") or ""),
        "description": str(remote.get("description") or ""),
        "inputSchema": dict(schema) if isinstance(schema, dict)
        else {"type": "object", "properties": {}},
    }
    annotations = remote.get("annotations")
    if isinstance(annotations, dict):
        # 只读/写判定也靠它：不缓存的话，重启后只读工具会被保守当成写工具。
        entry["annotations"] = annotations
    return entry


# -- 会话：两种传输共用同一个协议层 -----------------------------------------


class _SessionBase:
    """两种传输共用的协议层：只说「我要什么」，怎么送出去交给子类的 request/notify。

    这么分是为了让握手、翻页、ping 的语义只有一份实现：换传输不该顺带改掉
    「怎么握手」「怎么跟着 nextCursor 翻页」这些和传输无关的行为。
    真正的差异（起子进程 / POST 一个 URL）全在子类里。
    """

    server: McpServer
    timeout: float
    protocol_version: str | None
    server_info: dict[str, Any]

    def __init__(self, server: McpServer, *, timeout: float | None = None):
        self.server = server
        self.timeout = float(timeout if timeout is not None else server.timeout or DEFAULT_TIMEOUT)
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}

    # -- 子类各自实现：送一条请求出去 / 送一条通知出去 --

    def request(self, method: str, params: dict | None = None, *,
                timeout: float | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def notify(self, method: str, params: dict | None = None) -> None:
        raise NotImplementedError

    # -- 与传输无关的协议动作 --

    def initialize(self) -> dict[str, Any]:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": version_number()},
        })
        # 服务器可以回一个它自己支持的版本（MCP 允许协商），记下来但不在这里发难：
        # 真不兼容会在具体方法上报错，比在握手上凭版本号猜更准。
        self.protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        info = result.get("serverInfo")
        self.server_info = dict(info) if isinstance(info, dict) else {}
        # 钩子放在通知之前：http 的 initialized 通知本身就该带协商好的版本头。
        self._mark_initialized()
        self.notify("notifications/initialized")
        return result

    def _mark_initialized(self) -> None:
        """握手成功、发 initialized 通知之前的钩子；stdio 没有要记的东西。"""

    def list_tools(self, *, timeout: float | None = None) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            result = self.request("tools/list", {"cursor": cursor} if cursor else None,
                                  timeout=timeout)
            page = result.get("tools")
            if isinstance(page, list):
                tools.extend(item for item in page if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                # 没有下一游标，或游标绕回去了：这一页就是最后一页，别把会话卡在循环里。
                break
            seen.add(next_cursor)
            cursor = next_cursor
        return tools

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return self.request("tools/call", {"name": name, "arguments": dict(arguments or {})})

    def ping(self) -> dict:
        return self.request("ping")


# -- 一条 stdio 会话 ---------------------------------------------------------


class McpSession(_SessionBase):
    """一条到某台 MCP 服务器的 stdio 会话。

    请求串行：一条流上挂多个在途请求时，响应按 id 对号入座虽然可行，但断开时
    「谁的超时算数」会变成猜谜；串行让超时语义确定，服务器也不会被交错消息搞乱。

    读用后台线程 + Event：``readline`` 本身没有超时，Windows 上也不能靠 select，
    所以把阻塞读移出请求线程，请求只等事件，等不到就是超时。
    """

    def __init__(self, server: McpServer, *, timeout: float | None = None):
        super().__init__(server, timeout=timeout)
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._waiters: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._next_id = 0
        self._error: str | None = None

    # -- 生命周期 --

    @property
    def alive(self) -> bool:
        process = self._process
        return process is not None and self._error is None and process.poll() is None

    def matches(self, server: McpServer) -> bool:
        return self.server == server

    def start(self) -> None:
        if self.server.transport != "stdio":
            raise _wrong_transport(self.server, "stdio")
        if self._process is not None:
            return
        env = os.environ.copy()
        env.update(self.server.env)  # 配置里的 env 覆盖进程环境，和自己起子进程的习惯一致
        try:
            self._process = subprocess.Popen(
                [self.server.command, *self.server.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # 诊断信息直接丢掉：不读的 stderr 管道写满会把子进程卡死，而 stdout
                # 只许有协议消息，两边都不能用来传话。
                stderr=subprocess.DEVNULL,
                cwd=self.server.cwd or None,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # 桌面应用不该因为起后台服务器在屏幕上闪黑框。
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            raise McpError(
                f"MCP 服务器 {self.server.name} 启动失败：找不到命令 {self.server.command!r}；"
                f"请在 {default_path()} 里改 command（Windows 上给绝对路径或带扩展名的可执行文件）"
            ) from exc
        except OSError as exc:
            raise McpError(f"MCP 服务器 {self.server.name} 启动失败：{exc}") from exc
        self._reader = threading.Thread(target=self._read_loop,
                                        name=f"mcp-reader-{self.server.name}", daemon=True)
        self._reader.start()

    def close(self) -> None:
        with self._state_lock:
            if self._error is None:
                self._error = f"MCP 服务器 {self.server.name} 的会话已关闭"
            waiters = list(self._waiters.values())
        for event, _slot in waiters:
            event.set()
        self._stop_process()
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            # 等读线程收尾：进程关了它也会读到 EOF，这里只是别让它在别人回收对象时还在跑。
            reader.join(CLOSE_GRACE_SECONDS)

    def __enter__(self) -> McpSession:
        self.start()
        return self

    def __exit__(self, *_exception) -> None:
        self.close()

    # -- 协议：走到哪一步由 _SessionBase 定，这里只管怎么送出去 --

    def request(self, method: str, params: dict | None = None, *,
                timeout: float | None = None) -> dict[str, Any]:
        wait = float(timeout if timeout is not None else self.timeout)
        with self._request_lock:
            self._raise_if_dead()
            self._next_id += 1
            request_id = self._next_id
            event = threading.Event()
            slot: dict[str, Any] = {}
            with self._state_lock:
                self._waiters[request_id] = (event, slot)
            try:
                payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
                if params is not None:
                    payload["params"] = params
                self._write(payload, method)
                if not event.wait(wait):
                    self._fail(f"MCP 服务器 {self.server.name} 在 {wait:g} 秒内没有回应 {method}")
                    raise McpError(
                        f"MCP 服务器 {self.server.name} 超时：{method} 在 {wait:g} 秒内没有回应；"
                        "这条会话已关闭，下次调用会重新连"
                    )
            finally:
                with self._state_lock:
                    self._waiters.pop(request_id, None)
        message = slot.get("message")
        if not isinstance(message, dict):
            raise McpError(self._error or f"MCP 服务器 {self.server.name} 没有给出 {method} 的响应")
        error = message.get("error")
        if error is not None:
            raise McpRpcError(method, error)
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict | None = None) -> None:
        with self._request_lock:
            payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            self._write(payload, method)

    # -- 内部 --

    def _write(self, payload: dict, method: str) -> None:
        process = self._process
        stream = process.stdin if process is not None else None
        if stream is None:
            raise McpError(f"MCP 服务器 {self.server.name} 的输入流不可用，写不进 {method}")
        try:
            # 协议规定消息按 UTF-8 编码，一行一条；ensure_ascii=False 让中文原样过去。
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()
        except (OSError, ValueError) as exc:
            self._fail(f"MCP 服务器 {self.server.name} 的输入流已断开：{exc}")
            raise McpError(f"MCP 服务器 {self.server.name} 已断开，写不进 {method}") from exc

    def _read_loop(self) -> None:
        process = self._process
        stream = process.stdout if process is not None else None
        if stream is None:
            self._fail(f"MCP 服务器 {self.server.name} 没有可读的输出流")
            return
        while True:
            try:
                line = stream.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                # 服务器往 stdout 打了非协议内容（例如一句 print）：丢掉这一行，
                # 不能因为别人的调试输出就断掉整个会话。
                continue
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            if not isinstance(request_id, int):
                continue  # 服务器主动发来的请求/通知：本客户端没有向远端暴露任何能力，忽略
            with self._state_lock:
                waiter = self._waiters.get(request_id)
            if waiter is not None:
                waiter[1]["message"] = message
                waiter[0].set()
        self._fail(f"MCP 服务器 {self.server.name} 的输出流已关闭")

    def _fail(self, reason: str) -> None:
        with self._state_lock:
            if self._error is None:
                self._error = reason
            waiters = list(self._waiters.values())
        for event, _slot in waiters:
            event.set()
        self._stop_process()

    def _raise_if_dead(self) -> None:
        if self._error is not None:
            raise McpError(self._error)
        process = self._process
        if process is None:
            raise McpError(f"MCP 服务器 {self.server.name} 还没有连接")
        code = process.poll()
        if code is not None:
            raise McpError(f"MCP 服务器 {self.server.name} 的进程已退出（退出码 {code}）")

    def _stop_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        with contextlib.suppress(OSError, ValueError):
            if process.stdin is not None:
                process.stdin.close()  # 关掉输入，守规矩的服务器会自己收尾退出
        try:
            process.wait(timeout=CLOSE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # Windows 上 terminate 就是 TerminateProcess，后面的 kill 只是兜底；
            # POSIX 上两者依次收束「愿意退出」和「不肯退出」的服务器。
            process.terminate()
            try:
                process.wait(timeout=CLOSE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=CLOSE_GRACE_SECONDS)
        with contextlib.suppress(OSError, ValueError):
            if process.stdout is not None:
                process.stdout.close()


# -- 一条 Streamable HTTP 会话 -----------------------------------------------


class HttpSession(_SessionBase):
    """一条到某台 MCP 服务器的 Streamable HTTP 会话（规范 2025-06-18）。

    端点只有一个 URL：每件事都是一次 POST，响应要么是一整条 JSON，要么是一条 SSE 流。
    请求同样串行——理由和 stdio 一样（超时归属确定），外加一条：同一条 SSE 流里可能
    混着服务器自己的通知，串行时「这个 id 的答案」不会被别的请求搅在一起。

    会话失效的口径（``alive`` 从这里来）：
    - 超时与连接层失败（DNS、拒绝连接、连接中断）会让会话作废，下次调用重新握手：
      那条连接已经被放弃，再说「还活着」只是错觉。
    - 4xx/5xx 与 JSON-RPC error 是服务器给出的回答，会话仍然可用，不作废。
    - 收到 404 例外：规范说它表示会话已经没了，客户端应当重新 initialize。
    """

    def __init__(self, server: McpServer, *, timeout: float | None = None):
        super().__init__(server, timeout=timeout)
        # initialize 响应头里拿到的会话 id；之后每个请求都要回带。
        self.session_id: str | None = None
        self._handshake_done = False
        self._started = False
        self._closed = False
        self._error: str | None = None
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 0

    # -- 生命周期 --

    @property
    def alive(self) -> bool:
        return self._started and not self._closed and self._error is None

    def matches(self, server: McpServer) -> bool:
        return self.server == server

    def start(self) -> None:
        """http 没有「启动进程」这一步：只校验地址，连接留给第一次请求去建。"""
        if self.server.transport != "http":
            raise _wrong_transport(self.server, "http")
        if self._started:
            return
        try:
            normalize_url(self.server.url)
        except ValueError as exc:
            raise McpError(
                f"MCP 服务器 {self.server.name} 的 http 端点地址不对：{exc}") from exc
        self._started = True

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            if self._error is None:
                self._error = f"MCP 服务器 {self.server.name} 的会话已关闭"
            session_id = self.session_id
            self.session_id = None  # 摘掉就等于「DELETE 只发一次」
        if session_id:
            self._delete_session(session_id)

    def __enter__(self) -> HttpSession:
        self.start()
        return self

    def __exit__(self, *_exception) -> None:
        self.close()

    # -- 协议 --

    def _mark_initialized(self) -> None:
        # 规范：initialize 之「后」的请求都要带 MCP-Protocol-Version。即将发出的
        # initialized 通知已经算「之后」，所以先立旗再发。
        self._handshake_done = True

    def request(self, method: str, params: dict | None = None, *,
                timeout: float | None = None) -> dict[str, Any]:
        wait = float(timeout if timeout is not None else self.timeout)
        with self._request_lock:
            self._raise_if_dead()
            self._next_id += 1
            request_id = self._next_id
            payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            message = self._send(payload, method, wait, expect_response=True)
        if not isinstance(message, dict):
            raise McpError(f"MCP 服务器 {self.server.name} 没有给出 {method} 的响应")
        error = message.get("error")
        if error is not None:
            # 带上服务器名：这条消息可能直接出现在界面或日志里，光有方法名不知道是谁。
            raise McpRpcError(method, error, prefix=f"MCP 服务器 {self.server.name} 的 ")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict | None = None) -> None:
        with self._request_lock:
            self._raise_if_dead()
            payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            self._send(payload, method, self.timeout, expect_response=False)

    # -- 内部 --

    def _headers(self, *, with_body: bool) -> dict[str, str]:
        headers = {"Accept": ACCEPT_HEADER}
        if with_body:
            headers["Content-Type"] = MIME_JSON
        if self.session_id:
            headers[SESSION_HEADER] = self.session_id
        if self._handshake_done:
            headers[PROTOCOL_HEADER] = PROTOCOL_VERSION
        return headers

    def _send(self, payload: dict[str, Any], method: str, wait: float, *,
              expect_response: bool) -> dict | None:
        """POST 一条消息并拿回响应消息；通知返回 None。

        所有 urllib 的裸异常都在这里翻成 McpError：上层（工具面）只认我们自己的错误，
        不许 ``URLError: <urlopen error …>`` 这种话术透到界面。
        """
        request = urllib.request.Request(
            self.server.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=self._headers(with_body=True),
        )
        try:
            with urllib.request.urlopen(request, timeout=wait) as response:
                self._remember_session(response.headers)
                status = response.getcode()
                if not expect_response:
                    response.read()  # 通知只要求 202：读完就释放连接，body 不用
                    return None
                if status in (202, 204):
                    raise McpError(
                        f"MCP 服务器 {self.server.name} 只回了 HTTP {status}，"
                        f"没有 {method} 的 JSON-RPC 响应（请求必须回一条消息）")
                return self._read_message(response, payload["id"], method, wait)
        except urllib.error.HTTPError as exc:
            raise self._http_error(method, exc) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise self._timeout_error(method, wait) from None
            self._fail(f"MCP 服务器 {self.server.name} 连不上：{exc.reason}")
            raise McpError(
                f"MCP 服务器 {self.server.name} 连不上 {self.server.url}：{exc.reason}；"
                "确认地址与端口写对、服务真的在跑、没有被代理拦住") from None
        except TimeoutError:
            raise self._timeout_error(method, wait) from None
        except (OSError, ValueError) as exc:
            self._fail(f"MCP 服务器 {self.server.name} 的连接断了：{exc}")
            raise McpError(
                f"MCP 服务器 {self.server.name} 的 http 连接中断（{method}）："
                f"{type(exc).__name__}: {exc}") from None

    def _read_message(self, response: Any, request_id: int, method: str,
                      wait: float) -> dict:
        """按 Content-Type 走两条路：SSE 流式解析，或读完整个 body 当一条 JSON。"""
        content_type = str(response.headers.get("Content-Type") or "")
        media_type = content_type.split(";")[0].strip().lower()
        if media_type == MIME_SSE:
            return self._read_sse(response, request_id, method, wait)
        # 不拿声明的类型当门槛：有的服务器把 json 写成 application/json-rpc 之类，
        # 能不能解出来由下面的解析说了算，类型只进错误消息。
        return _parse_json_message(response.read(), self.server.name, method, content_type)

    def _read_sse(self, response: Any, request_id: int, method: str, wait: float) -> dict:
        """按行读 SSE：event/data 攒到空行算一条消息，认到本次请求的 id 就返回。

        不把整条流读完才返回：响应之后服务器很可能还在推心跳、日志或别的通知，
        等流结束等于把「服务器愿意闲聊多久」算进我们的超时里。
        """
        deadline = time.monotonic() + wait
        data_lines: list[str] = []
        while True:
            # 服务端可以一直发心跳把连接吊着：靠这个截止时间保证超时是「我们的」超时。
            if time.monotonic() > deadline:
                raise self._timeout_error(method, wait)
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                payload_text = "\n".join(data_lines)
                data_lines.clear()
                if payload_text:
                    message = _parse_event_data(payload_text, self.server.name, method)
                    if message.get("id") == request_id:
                        return message
                    # 别的 id、或服务器主动发的通知：本客户端没向远端暴露能力，忽略继续等。
                continue
            if line.startswith(":"):
                continue  # 注释行（心跳），SSE 规范允许且常见
            field_name, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field_name == "data":
                data_lines.append(value)
            # event/id/retry 三个字段对本客户端没有用处：消息体里的 id 才是判据。
        raise McpError(
            f"MCP 服务器 {self.server.name} 的 SSE 流在给出 {method} 的响应前就结束了")

    def _remember_session(self, headers: Any) -> None:
        """记下 initialize 响应头里的 Mcp-Session-Id：之后每个请求都得回带它。"""
        session_id = headers.get(SESSION_HEADER)
        if isinstance(session_id, str) and session_id.strip():
            self.session_id = session_id.strip()

    def _http_error(self, method: str, exc: urllib.error.HTTPError) -> McpError:
        body = b""
        with contextlib.suppress(OSError, ValueError):
            body = exc.read() or b""
        status = f"HTTP {exc.code} {exc.reason or ''}".strip()
        message = f"MCP 服务器 {self.server.name} 的 {method} 被拒绝：{status}"
        detail = _error_detail(body)
        if detail:
            message += f"；服务器说：{detail}"
        if exc.code == 404:
            # 规范：404 表示这条路或这个会话已经没了，客户端应当重新 initialize。
            self._fail(f"MCP 服务器 {self.server.name} 返回 HTTP 404（会话已失效）")
        return McpError(message)

    def _timeout_error(self, method: str, wait: float) -> McpError:
        """超时统一从这里出：会话同时作废，消息里写明下次会重连。"""
        self._fail(f"MCP 服务器 {self.server.name} 在 {wait:g} 秒内没有回应 {method}")
        return McpError(
            f"MCP 服务器 {self.server.name} 超时：{method} 在 {wait:g} 秒内没有回应"
            f"（POST {self.server.url}）；这条会话已作废，下次调用会重新握手")

    def _delete_session(self, session_id: str) -> None:
        """按规范收尾：带会话 id 发一次 DELETE。失败一律忽略——关会话不该再抛错。"""
        request = urllib.request.Request(
            self.server.url, method="DELETE",
            headers={SESSION_HEADER: session_id, PROTOCOL_HEADER: PROTOCOL_VERSION})
        # 用关会话的宽限期封顶：为了一个「顺手通知服务器」的动作让应用多等 20 秒不值得。
        wait = min(self.timeout, CLOSE_GRACE_SECONDS)
        with contextlib.suppress(OSError, ValueError):
            with urllib.request.urlopen(request, timeout=wait) as response:
                response.read()

    def _fail(self, reason: str) -> None:
        with self._state_lock:
            if self._error is None:
                self._error = reason

    def _raise_if_dead(self) -> None:
        if self._error is not None:
            raise McpError(self._error)
        if not self._started:
            raise McpError(f"MCP 服务器 {self.server.name} 还没有连接")


def _wrong_transport(server: McpServer, expected: str) -> McpError:
    """会话类只处理自己那种传输：走错了说明接线错了，别硬撑。"""
    return McpError(
        f"MCP 服务器 {server.name} 的 transport 是 {server.transport!r}，"
        f"这条会话只处理 {expected}；请按 transport 选对应的会话类")


def _snippet(data: bytes | str) -> str:
    """把服务器原话截成一句：界面要看得懂，但不该被一整页 HTML 灌满。"""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    text = " ".join(text.split())
    return text[:SNIPPET_LIMIT] + "…" if len(text) > SNIPPET_LIMIT else text


def _error_detail(body: bytes) -> str:
    """从错误响应的 body 里取出服务器自己的说明；取不到就给原始片段。"""
    try:
        payload = json.loads(body.decode("utf-8")) if body.strip() else None
    except (UnicodeDecodeError, ValueError):
        return _snippet(body)
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            code = error.get("code")
            return f"{error['message']}（JSON-RPC {code}）" if code is not None \
                else str(error["message"])
        detail = payload.get("message")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    return _snippet(body)


def _parse_json_message(body: bytes, server_name: str, method: str, content_type: str) -> dict:
    """把一整条 JSON 响应解成消息；解不出来就报「服务器没按协议说话」。"""
    text = body.decode("utf-8", "replace")
    if not text.strip():
        raise McpError(
            f"MCP 服务器 {server_name} 回给 {method} 的响应是空的"
            f"（Content-Type: {content_type or '未声明'}）")
    try:
        message = json.loads(text)
    except ValueError as exc:
        raise McpError(
            f"MCP 服务器 {server_name} 回给 {method} 的不是 JSON"
            f"（Content-Type: {content_type or '未声明'}）：{_snippet(text)}") from exc
    if not isinstance(message, dict):
        raise McpError(
            f"MCP 服务器 {server_name} 回给 {method} 的不是 JSON-RPC 对象：{_snippet(text)}")
    return message


def _parse_event_data(data: str, server_name: str, method: str) -> dict:
    """SSE 里一条 data 就是一条 JSON-RPC 消息；不是就当场说清楚，别拖到超时。"""
    try:
        message = json.loads(data)
    except ValueError as exc:
        raise McpError(
            f"MCP 服务器 {server_name} 在 {method} 的 SSE 流里发来不是 JSON 的数据："
            f"{_snippet(data)}") from exc
    if not isinstance(message, dict):
        raise McpError(
            f"MCP 服务器 {server_name} 在 {method} 的 SSE 流里发来的不是 JSON-RPC 对象："
            f"{_snippet(data)}")
    return message


# -- 会话登记 ---------------------------------------------------------------

# 两种传输的会话放在同一张表里：上层（adapt_tools / call_tool / refresh）不该关心
# 一台服务器是子进程还是一个 URL，它们要的只是「对外方法一致的那个对象」。
_SESSIONS: dict[str, McpSession | HttpSession] = {}
_SESSION_LOCK = threading.Lock()
# 我们自己注册过的工具名 -> 服务器名。刷新要原地换合同、清理消失的工具，靠它认出「自己的」。
_OWNED: dict[str, str] = {}
_LAST_ERRORS: dict[str, str] = {}


def open_sessions() -> dict[str, McpSession | HttpSession]:
    """当前还活着的会话；界面做诊断或退出前检查用。"""
    with _SESSION_LOCK:
        return {name: session for name, session in _SESSIONS.items() if session.alive}


def last_errors() -> dict[str, str]:
    """上次 refresh 里每台服务器各自的失败原因；成功的服务器不会出现在这里。"""
    return dict(_LAST_ERRORS)


def close() -> None:
    """收束所有会话（stdio 走 terminate → kill 兜底，http 发 DELETE）：应用退出、配置改动、测试前后都该调。"""
    with _SESSION_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session in sessions:
        session.close()


def _new_session(server: McpServer) -> McpSession | HttpSession:
    """按 transport 选会话实现——这是整个模块里唯一需要知道传输差别的地方。"""
    if server.transport == "http":
        return HttpSession(server)
    return McpSession(server)


def _session(server: McpServer) -> McpSession | HttpSession:
    # 建连整个过程放在锁里：同一台服务器绝不能因为并发调用起出两个子进程 / 两条会话。
    # 代价是最坏情况下一次建连（默认 20 秒超时）会挡住别的服务器的调用。
    with _SESSION_LOCK:
        current = _SESSIONS.get(server.name)
        if current is not None and current.matches(server) and current.alive:
            return current
        if current is not None:
            current.close()
            _SESSIONS.pop(server.name, None)
        session = _new_session(server)
        session.start()
        try:
            session.initialize()
        except McpError:
            session.close()
            raise
        _SESSIONS[server.name] = session
        return session


# -- 工具适配 ---------------------------------------------------------------


def sanitize(text: str) -> str:
    """名字里的非法字符折成下划线；全被折光就给个占位名，别造出空名字。

    首尾的下划线一并去掉：留着它们会和名字里的 ``__`` 分隔符搅在一起，
    让人分不清 ``mcp__a__b`` 里哪一个是分隔。
    """
    cleaned = _NAMESPACE.sub("_", str(text or "")).strip("_")
    return cleaned or "unnamed"


def tool_name(server_name: str, remote_name: str, used: set[str] | None = None) -> str:
    """``mcp__<server>__<tool>``：前缀是硬边界，模型和人都能一眼看出这是外部工具。

    传 ``used`` 时顺手保证唯一（``_2``、``_3`` 递增）：两台服务器名折叠后相同、
    或远端自己就有 ``a.b`` 与 ``a-b`` 这种名字时，不能互相顶掉。
    """
    base = f"{TOOL_PREFIX}{sanitize(server_name)}__{sanitize(remote_name)}"
    if used is None:
        return base
    name = base
    index = 2
    while name in used:
        name = f"{base}_{index}"
        index += 1
    return name


def _params_from_schema(schema: dict) -> dict[str, dict]:
    """按远端 schema 的 properties/required 镜像一份 params。

    ``Tool.schema()`` 走 input_schema 原样返回（模型的合同不变）；params 只喂给
    ``tool_api.call`` 的「多余参数/缺必需参数」校验，所以两边口径必须一致。
    """
    properties = schema.get("properties")
    required = schema.get("required")
    required_names = {str(item) for item in required} if isinstance(required, list) else set()
    params: dict[str, dict] = {}
    if isinstance(properties, dict):
        for key, spec in properties.items():
            if not isinstance(key, str):
                continue
            mirror = dict(spec) if isinstance(spec, dict) else {}
            if key in required_names:
                mirror["required"] = True
            params[key] = mirror
    return params


def _summary(server: McpServer, remote: dict, remote_name: str) -> str:
    description = remote.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    return f"{server.name} 提供的外部工具 {remote_name}"


def _handler(server: McpServer, remote_name: str):
    def invoke(_ops, args):
        return call_tool(server, remote_name, args)

    return invoke


def adapt_tools(Tool, server: McpServer, remote_tools, *,
                used: set[str] | None = None) -> list:
    """把远端的 tools/list 结果翻成我们的 Tool 对象（名字、写标记、schema 一起定）。"""
    reserved = used if used is not None else set()
    adapted: list = []
    for remote in remote_tools or []:
        if not isinstance(remote, dict):
            continue
        remote_name = str(remote.get("name") or "").strip()
        if not remote_name:
            continue
        name = tool_name(server.name, remote_name, reserved)
        reserved.add(name)
        raw_schema = remote.get("inputSchema")
        schema = dict(raw_schema) if isinstance(raw_schema, dict) else {}
        schema.setdefault("type", "object")
        # 别的调用方（本工程的 MCP 服务端会给 schema 塞 project 参数）会直接写
        # schema["properties"]，所以这里保证键在，schema 的其余内容原样不动。
        schema.setdefault("properties", {})
        annotations = remote.get("annotations")
        annotations = annotations if isinstance(annotations, dict) else {}
        adapted.append(Tool(
            name,
            _summary(server, remote, remote_name),
            _params_from_schema(schema),
            _handler(server, remote_name),
            # 远端没明说 readOnlyHint=true 就按写处理：宁可多要一次确认，
            # 也不能让一次有副作用的远端调用从只读通道溜过去。
            writes=annotations.get("readOnlyHint") is not True,
            scope=SCOPE,
            input_schema=schema,
        ))
    return adapted


def _tool_error(message: str) -> Exception:
    """延迟取 ToolError：tool_api 在导入末尾就会回调本模块，模块顶部不能反向 import。"""
    from .tool_api import ToolError

    return ToolError(message)


def _content_text(result: dict) -> tuple[str, list[str]]:
    """远端 content 折成一段文本；图片、资源这类留一句摘要，不静默丢掉。"""
    parts = result.get("content")
    texts: list[str] = []
    others: list[str] = []
    for item in parts if isinstance(parts, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            texts.append(str(item.get("text") or ""))
        elif kind == "image":
            others.append(f"[图片 {item.get('mimeType') or '未知类型'}]")
        elif kind == "audio":
            others.append(f"[音频 {item.get('mimeType') or '未知类型'}]")
        elif kind == "resource":
            resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
            others.append(f"[资源 {resource.get('uri') or '无地址'}]")
        else:
            others.append(f"[{kind or '未知'} 内容]")
    return "\n".join(text for text in texts if text), others


def call_tool(server: McpServer, remote_tool: str, arguments: dict | None = None) -> dict:
    """把一次调用转给远端，并把远端的各种「不好」都翻成 ToolError。

    工具面是模型和界面能看见的最后一道：这里不许让 OSError、超时、进程崩溃
    这类裸异常穿出去。
    """
    args = dict(arguments) if isinstance(arguments, dict) else {}
    try:
        result = _session(server).call_tool(remote_tool, args)
    except McpRpcError as exc:
        raise _tool_error(f"MCP 服务器 {server.name} 的 {remote_tool} 返回错误："
                          f"{exc.rpc_message}（JSON-RPC {exc.code}）") from None
    except McpError as exc:
        raise _tool_error(f"MCP 服务器 {server.name} 的 {remote_tool} 没能执行：{exc}") from None
    except (OSError, ValueError) as exc:
        raise _tool_error(f"MCP 服务器 {server.name} 的 {remote_tool} 调用失败：{exc}") from None
    except Exception as exc:  # 兜底：意外异常也只回一句错误文本
        raise _tool_error(f"MCP 服务器 {server.name} 的 {remote_tool} 调用失败："
                          f"{type(exc).__name__}: {exc}") from None
    if result.get("isError") is True:
        text, _others = _content_text(result)
        raise _tool_error(f"MCP 服务器 {server.name} 的 {remote_tool} 执行失败："
                          f"{text or '远端没有给出说明'}")
    text, others = _content_text(result)
    payload: dict[str, Any] = {"server": server.name, "tool": remote_tool, "content": text}
    if others:
        payload["other_content"] = others
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        payload["structured_content"] = structured
    return payload


# -- 注册 -------------------------------------------------------------------


def _registry_default() -> dict[str, Any]:
    from . import tool_api

    return tool_api.TOOLS


def _reserved_names(registry: dict[str, Any]) -> set[str]:
    """本次可用的名字：本地工具名 + 注册表已有的名字，减去上次我们自己注册的。

    减去很关键：刷新要原地换掉旧合同，不能让 ``mcp__fake__echo`` 每刷一次变成
    ``mcp__fake__echo_2``。
    """
    from . import tool_api

    reserved = set(registry)
    reserved.update(name for name, tool in tool_api.TOOLS.items()
                    if getattr(tool, "scope", None) != SCOPE)
    reserved -= {name for name in reserved if _OWNED.get(name)}
    return reserved


def _register_or_replace(tool, register_tool, registry: dict[str, Any]) -> None:
    existing = registry.get(tool.name)
    if existing is not None:
        if getattr(existing, "scope", None) != SCOPE:
            # mcp__ 前缀是我们保留的，正常不会撞；真撞了也不能覆盖别人的工具。
            raise McpError(f"工具名 {tool.name} 已被本地工具占用，不能注册同名外部工具")
        # _register 拒绝重名；刷新要换成本次发现的合同，所以先摘掉自己的旧条目。
        registry.pop(tool.name, None)
    register_tool(tool)


def _install_server_tools(Tool, register_tool, registry: dict[str, Any], server: McpServer,
                          remote_tools, reserved: set[str]) -> list[str]:
    adapted = adapt_tools(Tool, server, remote_tools, used=reserved)
    kept = {tool.name for tool in adapted}
    stale = [name for name, owner in list(_OWNED.items())
             if owner == server.name and name not in kept]
    for name in stale:  # 远端删掉/改名的工具要跟着消失，不然模型会调到已经不存在的工具
        registry.pop(name, None)
        _OWNED.pop(name, None)
    for tool in adapted:
        _register_or_replace(tool, register_tool, registry)
        _OWNED[tool.name] = server.name
    return [tool.name for tool in adapted]


def _record_failure(report: dict[str, Any], name: str, message: str) -> None:
    report["servers"][name] = {"ok": False, "tools": [], "error": message}
    report["errors"][name] = message


def register_cached(Tool, register_tool=None, *, tools: dict | None = None) -> dict:
    """只用上次成功的缓存注册工具，不连服务器。

    启动路径要快：模型先看得见「上次有哪些外部工具」，真正的连接刷新由调用方稍后触发。
    """
    from .tool_api import _register

    register = register_tool or _register
    registry = _registry_default() if tools is None else tools
    cache = load_cache()
    reserved = _reserved_names(registry)
    registered: list[str] = []
    for server in load_servers():
        if not server.enabled:
            continue
        entries = cache.get(server.name) or []
        if entries:
            registered.extend(
                _install_server_tools(Tool, register, registry, server, entries, reserved))
    return {"registered": registered}


def refresh(Tool, register_tool=None, *, tools: dict | None = None) -> dict:
    """连所有启用的服务器，发现工具并注册。

    单台服务器失败只进 ``errors`` / ``last_errors``：其它服务器和本地工具照常。
    配置本身读不懂（McpConfigError）会抛出去——那不是某台服务器的问题。
    """
    global _LAST_ERRORS

    from .tool_api import _register

    register = register_tool or _register
    registry = _registry_default() if tools is None else tools
    servers = load_servers()
    cache = load_cache()
    configured = {server.name for server in servers}
    for name in list(cache):
        if name not in configured:
            cache.pop(name, None)  # 服务器已从配置里删掉：缓存跟着过期，别再注册它的工具
    reserved = _reserved_names(registry)
    report: dict[str, Any] = {"registered": [], "servers": {}, "errors": {}}
    for server in servers:
        if not server.enabled:
            report["servers"][server.name] = {"ok": False, "tools": [], "error": "已停用"}
            continue
        try:
            remote_tools = _session(server).list_tools()
        except McpError as exc:
            _record_failure(report, server.name, str(exc))
            continue
        except Exception as exc:  # 一台服务器的意外不许带走整次刷新
            _record_failure(report, server.name, f"{type(exc).__name__}: {exc}")
            continue
        try:
            names = _install_server_tools(Tool, register, registry, server, remote_tools, reserved)
        except (McpError, ValueError) as exc:
            _record_failure(report, server.name, str(exc))
            continue
        cache[server.name] = [_cache_entry(item) for item in remote_tools]
        report["servers"][server.name] = {"ok": True, "tools": names, "error": None}
        report["registered"].extend(names)
    if report["servers"]:
        try:
            save_cache(cache)
        except McpConfigError as exc:
            report["cache_error"] = str(exc)  # 缓存写不进只是下次启动少点便利，不算刷新失败
    _LAST_ERRORS = dict(report["errors"])
    return report


def refresh_async(Tool, register_tool=None, *, tools: dict | None = None,
                  on_done: Callable[[dict], None] | None = None) -> threading.Thread:
    """在后台线程里刷新：界面启动后用缓存先顶上，再调它去连真服务器。

    建连要等超时，绝不能占住界面线程。工具表本身不是线程安全的，所以
    ``on_done`` 也在后台线程里执行，界面侧请把报告交给主线程再处理。
    """
    def run() -> None:
        try:
            report = refresh(Tool, register_tool, tools=tools)
        except Exception as exc:  # 后台线程里的异常没人能接住，收进报告里
            report = {"registered": [], "servers": {}, "errors": {},
                      "error": f"{type(exc).__name__}: {exc}"}
        if on_done is not None:
            on_done(report)

    thread = threading.Thread(target=run, name="mcp-refresh", daemon=True)
    thread.start()
    return thread
