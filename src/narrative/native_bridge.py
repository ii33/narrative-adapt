"""Asynchronous Qt Quick adapter over public Operations graph capabilities.

No database connection, model runner or canvas geometry is shared with the GUI thread.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Property, QObject, Signal, Slot

from . import (
    appearance,
    graph_profile_ops,
    mcp_client,
    project_catalog,
    project_package,
    project_removal,
    prompt_registry,
    providers,
    runtime_security,
    skills,
    theme,
    version,
)
from .browse_view import moment_tree
from .graph_operations import GraphOperations
from .harness import HarnessRunner, harness_env
from .native_models import CanvasState, node_detail, search_result
from .node_material import holds_at
from .prompt_defaults import DEFAULT_PROMPTS
from .prompt_settings import effective_prompt
from .provider_store import GLOBAL_SCOPE, ProviderStore

_NODE_TYPES = {"person", "group", "item", "location", "event", "action", "scene", "big_event", "storyline", "other"}
SCREENPLAY_WRITING_PROMPT_VERSION = "screenplay-writing-ui/9"

SCREENPLAY_SCENE_TABLE_RULES = DEFAULT_PROMPTS["scene_table"]["text"]


# -- 全局 MCP 服务器：界面交上来的那一行怎么变成配置 --------------------------
# 解析规则只写一份，QML 那边不重复实现：参数怎么切、环境变量怎么写，永远以这里为准。


def _mcp_split_args(text: str) -> list[str]:
    """把界面上那一行参数按空白切开，允许用一对双引号护住带空格的一段。

    为什么不用 shlex：Windows 上 POSIX 模式会把 ``C:\\a\\b`` 里的反斜杠当转义吃掉，
    而参数里最常见的恰恰是路径。所以只认双引号这一条规则，并且明确不做引号嵌套、
    单引号和反斜杠转义——碰到没配对的引号就报错，不替用户猜一个用法不同的切法。
    """
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    started = False  # 区分空串 ""（一个空参数）与「这里根本没有参数」
    for char in text:
        if char == '"':
            quoted = not quoted
            started = True
            continue
        if char.isspace() and not quoted:
            if started:
                tokens.append("".join(current))
                current, started = [], False
            continue
        current.append(char)
        started = True
    if quoted:
        raise ValueError("参数里的双引号没有配对；这里只支持用一对双引号护住带空格的一段，"
                         "不支持引号嵌套，也不支持单引号")
    if started:
        tokens.append("".join(current))
    return tokens


def _mcp_env_map(value: object) -> dict[str, str]:
    """环境变量：界面上是「每行一条 K=V」的文本，配置里是对象，两种都收。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    lines = value.splitlines() if isinstance(value, str) else value
    if not isinstance(lines, (list, tuple)):
        raise ValueError("环境变量须为 K=V 文本或字符串键值对")
    env: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        text = str(line).strip()
        if not text or text.startswith("#"):
            continue  # 空行与注释跳过：手写这一段时留空行是常态，不该算错
        key, sep, item = text.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"环境变量第 {number} 行不是 K=V：{text}")
        env[key.strip()] = item.strip()
    return env


def _mcp_timeout(value: object) -> float:
    """超时：界面给的是文本，配置里是秒数；留空就用内核默认值。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return mcp_client.DEFAULT_TIMEOUT
    if isinstance(value, bool):
        raise ValueError("超时须为正数秒")
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"超时须为正数秒，收到 {value!r}") from exc


def _mcp_server_from_row(row: object, index: int) -> mcp_client.McpServer:
    """把界面交来的一行折成 McpServer；字段校验一律交给 McpServer.from_dict。"""
    if not isinstance(row, dict):
        raise ValueError(f"第 {index + 1} 台服务器不是对象")
    transport = str(row.get("transport") or "stdio").strip()
    enabled = row.get("enabled")
    data: dict[str, Any] = {
        "name": row.get("name"),
        "enabled": True if enabled is None else enabled,
        "transport": transport,
        "timeout": _mcp_timeout(row.get("timeout")),
    }
    if transport == "http":
        data["url"] = row.get("url")
    else:
        data["command"] = row.get("command")
        # 文本形式（界面上这一行刚被编辑过）在这里折成数组/对象；列表里没被编辑过的行
        # 直接带着数组/对象过来，原样通过，不重算。
        args = row.get("args")
        data["args"] = _mcp_split_args(args) if isinstance(args, str) else args
        data["env"] = _mcp_env_map(row.get("env"))
        data["cwd"] = row.get("cwd") or ""
    return mcp_client.McpServer.from_dict(data, index=index)


def _mcp_cached_tools(rows: list[dict]) -> list[dict]:
    """缓存里的工具条目 -> 面板要的形状；只读判定与 mcp_client.adapt_tools 同口径。"""
    tools: list[dict] = []
    for row in rows:
        annotations = row.get("annotations")
        tools.append({
            "name": str(row.get("name") or ""),
            "description": str(row.get("description") or ""),
            # 远端没明说 readOnlyHint=true 就按写处理：宁可多要一次确认，
            # 也不能把有副作用的工具在界面上标成只读。
            "writes": not (isinstance(annotations, dict)
                           and annotations.get("readOnlyHint") is True),
        })
    return tools


def _mcp_server_row(server: mcp_client.McpServer, cached: list[dict],
                    last_errors: dict[str, str]) -> dict[str, Any]:
    """一台服务器在界面上的样子：配置原文 + 缓存里已知的工具 + 静态问题 + 上次失败原因。

    ``error`` 与 ``last_error`` 分开：前者不连服务器就能判定（例如配置缺字段），
    后者来自上一次刷新。两者都要显示——用户改完命令还没刷新时，看到的是上一次的失败原因。
    stdio 与 http 两种传输都真能连了，这里不再对 http 造静态错误。
    """
    return {
        "name": server.name,
        "enabled": server.enabled,
        "transport": server.transport,
        "command": server.command,
        "args": list(server.args),
        "env": dict(server.env),
        "cwd": server.cwd,
        "url": server.url,
        "timeout": server.timeout,
        "tools": _mcp_cached_tools(cached),
        "error": "",
        "last_error": last_errors.get(server.name, ""),
    }


# -- 技能：一条「作业说明 + 要打开的工具组」 ----------------------------------
# 与 MCP 那组同一分工：界面只负责列出、编辑、触发保存；校验与写盘都在 skills 内核里，
# 这里只做界面形状的转换。区别在调度——一次技能保存是毫秒级的原子写，不需要后台线程，
# 所以不像 mcpServers 那样「保存 → 后台刷新 → 信号」，而是直接写完就发 skillsChanged。


def _skill_names(value: object, label: str) -> list[str]:
    """界面多选框给的是 JS 数组，读回来可能是 QJSValue：这里只做形状归一。

    名字合不合法（未知业务组、未知工具、是不是业务组写进了 tools）一律交给内核的
    ``_clean_families`` / ``_clean_tools``：校验口径只写一份，界面不做第二套白名单。
    """
    if value is None:
        return []
    if hasattr(value, "toVariant"):
        value = value.toVariant()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} 须为名称数组")
    rows = [str(item).strip() for item in value if str(item).strip()]
    return list(dict.fromkeys(rows))


def _skill_text(value: object, label: str) -> str | None:
    """界面传回来的文本字段（可能是 QJSValue 里的 undefined）：空串原样保留。

    空串在 ``skills`` 内核里是「显式清空」的意思（`replaces` 传空串=改回纯附加说明），
    所以这里不能把空串折成 None——None 表示「没传，不动这个字段」。
    """
    if value is None:
        return None
    if hasattr(value, "toVariant"):
        value = value.toVariant()
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} 须为文本")
    return value


def _skill_mapping(value: object, label: str) -> dict | None:
    """界面传回来的对象（``workflow``）：QJSValue 折成 dict，形状校验交给内核。

    界面不做第二套白名单：`_clean_workflow` 是唯一口径，写错了在哪里报都一个说法。
    空对象原样保留（``{}`` 在 ``update_skill`` 里是「清空工作流声明」）。
    """
    if value is None:
        return None
    if hasattr(value, "toVariant"):
        value = value.toVariant()
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{label} 须为 JSON 对象，例如 {{\"kind\": \"sketch\", \"model\": \"...\"}}")
    return dict(value)


def _skill_opens(families: list[str], tools: list[str]) -> dict:
    """这条技能会打开哪些工具；解析不了就如实说明，不假装能打开。

    走的是 ``skills.resolve_family_names`` → ``tool_loading.resolve_tool_names``，
    与模型调用 ``load_skill`` 时用的是同一个解析入口，所以界面上显示的工具数就是
    会话里真正会打开的那一批（仍然受当前访问模式约束）。
    """
    try:
        return {"resolved_tools": skills.resolve_family_names(list(families), list(tools)),
                "problem": ""}
    except ValueError as exc:
        return {"resolved_tools": [], "problem": f"当前访问模式下解析不到这些工具：{exc}"}


def _skill_row(skill: skills.Skill) -> dict:
    """一条技能在界面上的样子：配置原文 + 「会打开什么」。

    ``builtin`` 原样带出来：界面据此禁用编辑/删除，不自己按名字猜哪几条只读。
    """
    row = skill.to_dict()
    row["opens"] = _skill_opens(list(skill.families), list(skill.tools))
    return row


class NativeBridge(QObject):
    """QML context object named ``bridge``; all Operations I/O runs off the GUI thread."""

    # The string form is what QML needs: a bare ``dict`` registers a Python type
    # that QML cannot see, so every Connections handler silently fails.
    blockTextDoubleClicked = Signal(str)
    resultReady = Signal(str, "QVariantMap")  # type: ignore[arg-type]
    errorOccurred = Signal(str, str)
    busyChanged = Signal(bool)
    # 一次后台操作花了多久（毫秒）。只在明显偏慢时发，用来定位「界面卡」到底卡在哪一步。
    slowOperation = Signal(str, int)
    _workerDone = Signal(int, object, object)

    themeModeChanged = Signal()
    darkModeChanged = Signal()
    layoutModeChanged = Signal()
    versionChanged = Signal()
    # 非致命提示（「事已经按你说的办好了，只是有个附带动作没成」）。界面当提示显示，
    # 不当错误：最近工程列表这类便利记录写不进去，不该把一个做完的动作染成失败。
    noticeRaised = Signal(str)
    # 外部 MCP 服务器：保存或刷新之后后台线程已经改过工具表，界面要重读 mcpServers()。
    # 从后台线程发射是安全的（Qt 把调用排进界面线程的队列），和 _workerDone 同一做法。
    mcpServersChanged = Signal()
    # 技能：保存或删除之后配置文件已经变了，界面要重读 skills()。技能写盘是毫秒级的
    # 原子写，所以这里由保存动作自己同步发射，不像 MCP 那样等后台刷新线程。
    skillsChanged = Signal()
    # 进程完整性不变，所以 integrityChanged 从不发射；它只是让 Property 有 notify。
    integrityChanged = Signal()

    @Property(str, notify=themeModeChanged)
    def themeMode(self) -> str:
        """当前外观模式：system / light / dark。"""
        return self._appearance_mode

    @Property(str, notify=layoutModeChanged)
    def layoutMode(self) -> str:
        """当前主框构：compact（默认）或 islands。"""
        return self._appearance_layout

    @Property(bool, notify=darkModeChanged)
    def darkMode(self) -> bool:
        """实际是否使用深色；模式为 system 时由系统偏好决定。"""
        return self._appearance_dark

    @Property("QVariantMap", notify=darkModeChanged)
    def theme(self) -> dict:
        """QML 用的设计令牌（颜色是十六进制字符串），与 Qt 控件调色板同源。"""
        return theme.tokens(self._appearance_dark)

    # 构建身份在进程内不变，所以 versionChanged 从不发射；它只是让 Property
    # 与上面几个只读属性一样有 notify。版本串的真源是 narrative/version.py。
    @Property(str, notify=versionChanged)
    def appVersion(self) -> str:
        """带构建身份的完整版本串，例如 ``v0.3.0-dev.timeline+20261112.ac8d5af``。"""
        return version.display_version()

    @Property(str, notify=versionChanged)
    def appVersionTag(self) -> str:
        """不带构建身份的版本标签，例如 ``v0.3.0-dev.timeline``。"""
        return version.tag()

    # 低完整性进程写不了用户目录（最近工程、模型设置都在那儿）。这不是致命错误，
    # 但必须常驻可见：状态栏按这条串决定要不要一直挂着那条警告。正常启动是空串。
    @Property(str, notify=integrityChanged)
    def integrityWarning(self) -> str:
        return runtime_security.current_integrity_notice()

    # 一个操作最多等这么久。超过就放弃它并把「处理中」清掉：
    # 结果要是永远不回来，界面就会一直卡在「处理中」，什么都点不动。
    STALE_AFTER_SECONDS = 10.0

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.resultReady.connect(self._invalidate_conversation_graph)
        from .native_text_events import ReadOnlyBlockTextEvents
        self._block_text_events = ReadOnlyBlockTextEvents(self)
        self._block_text_events.doubleClicked.connect(self.blockTextDoubleClicked.emit)
        from .native_content import FormSubmissions
        self._content_forms = FormSubmissions()
        self._project_path: Path | None = None
        self._canvas = CanvasState()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="narrative-native")
        self._requests: dict[
            int, tuple[str, int, Callable[[dict], dict] | None, int | None, float, str]
        ] = {}
        self._next_request = 0
        self._static_scan_generation = 0
        self._static_target_generation = 0
        self._screenplay_query_generation = 0
        self._epoch = 0
        self._query_epoch = 0
        self._recent_generation = 0
        # 清单写不进去时，也得让它在内存里消失：否则刷新列表又把它捡回来。
        self._recent_removed: set[str] = set()
        self._query_spec: dict = {}
        self._query_canvas: dict | None = None
        self._unified_active = False
        self._query_folded: set[str] = set()
        self._query_visible: set[str] = set()
        self._detail_node = ""
        self._reading_cut: dict = {}
        self._state_cut: str = ""
        self._closed = False
        # 常驻协作者：一个工程一条会话，模型看得见自己前面的工具调用。
        self._assistant_runtime = None
        self._assistant_runner = None
        self._assistant_tool_host = None
        self._assistant_steps: list[dict] = []
        self._assistant_task_lock = threading.Lock()
        self._assistant_jobs: dict[int, dict] = {}
        self._assistant_activity_sequence = 0
        self._assistant_chat = ""
        self._assistant_resumed: set[str] = set()
        self._assistant_lock = threading.Lock()
        # 页面 → 会话模板的临时覆盖（Router 自己只管内存里的一次性意图）。
        self._chat_template_router = None

        # 外观偏好在界面起来前读一次；未读取前按浅色渲染，不碰磁盘。
        self._appearance_mode = appearance.DEFAULT_MODE
        self._appearance_layout = appearance.DEFAULT_LAYOUT
        self._appearance_dark = False
        self._appearance_watching = False
        # 外部 MCP 服务器正在后台刷新：重复点「测试连接」不该并行起两个刷新线程。
        self._mcp_refreshing = False
        self._workerDone.connect(self._on_worker_done)

    def close(self) -> None:
        """Cancel owned tasks and close this project runtime; ignore late UI callbacks."""
        self._closed = True
        self._close_assistant()
        # 外部 MCP 服务器是我们自己起的子进程：进程退出前必须收掉，否则每台都会留下
        # 一个没人管的孤儿进程。收尾失败也不能拦住关窗，所以只吞掉异常。
        try:
            mcp_client.close()
        except Exception:  # noqa: BLE001 - 退出路径上不允许再抛
            pass
        self._pool.shutdown(wait=False, cancel_futures=True)

    def load_appearance(self) -> None:
        """启动时读一次外观偏好并应用；跟随系统时还要跟着系统深浅色变化。"""
        self._appearance_mode = appearance.load()
        self._appearance_layout = appearance.load_layout()
        self.layoutModeChanged.emit()
        self._watch_system_scheme()
        self._apply_appearance()

    @Slot(str)
    def setLayoutMode(self, layout: str) -> None:
        """切换主框构（紧凑／岛屿）：立刻生效，并记住选择。保存失败不拦着切换。"""
        if layout not in appearance.LAYOUTS:
            self.errorOccurred.emit("appearance", f"未知布局：{layout}")
            return
        failure = ""
        try:
            appearance.save_layout(layout)
        except (OSError, ValueError) as exc:
            failure = str(exc)
        self._appearance_layout = layout
        self.layoutModeChanged.emit()
        if failure:
            self.errorOccurred.emit("appearance", f"布局选择没有保存：{failure}")

    @Slot(str)
    def setThemeMode(self, mode: str) -> None:
        """切换外观模式：立刻生效，并记住选择。保存失败不拦着切换。"""
        if mode not in appearance.MODES:
            self.errorOccurred.emit("appearance", f"未知外观模式：{mode}")
            return
        failure = ""
        try:
            appearance.save(mode)
        except (OSError, ValueError) as exc:
            failure = str(exc)
        self._appearance_mode = mode
        self._watch_system_scheme()
        self._apply_appearance()
        if failure:
            self.errorOccurred.emit("appearance", f"外观偏好没有保存：{failure}")

    def _watch_system_scheme(self) -> None:
        if self._appearance_watching:
            return
        from PySide6.QtGui import QGuiApplication

        instance = QGuiApplication.instance()
        hints = instance.styleHints() if instance is not None else None
        if hints is None:
            return
        hints.colorSchemeChanged.connect(self._on_system_scheme_changed)
        self._appearance_watching = True

    def _on_system_scheme_changed(self, *_scheme) -> None:
        # 手动选了浅色或深色时，系统变化不该覆盖用户的选择；关闭后也不再改调色板。
        if self._appearance_mode == "system" and not self._closed:
            self._apply_appearance()

    def _apply_appearance(self) -> None:
        from PySide6.QtGui import QGuiApplication

        instance = QGuiApplication.instance()
        if instance is not None and self._appearance_mode == "system":
            # 先把自己的钉值交回系统：否则切回「跟随系统」时读到的是上次强制的那一档。
            theme.reset_color_scheme(instance)
        system_dark = theme.system_dark(instance) if instance is not None else False
        dark = appearance.is_dark(self._appearance_mode, system_dark)
        if instance is not None:
            # 跟随系统时必须把窗口框架交回 Unknown：钉成具体一档会把系统偏好盖住，
            # 之后既读不到系统深色，也收不到系统切换的通知。
            theme.apply_palette(instance, dark, follow_system=self._appearance_mode == "system")
        changed = dark != self._appearance_dark
        self._appearance_dark = dark
        self.themeModeChanged.emit()
        if changed:
            self.darkModeChanged.emit()

    def _submit(
        self,
        kind: str,
        work: Callable[[], dict],
        apply: Callable[[dict], dict] | None = None,
        *,
        epoch: int | None = None,
        query_epoch: int | None = None,
        content_form: str = "",
    ) -> None:
        if self._closed:
            return
        self._drop_stale()
        self._next_request += 1
        request_id = self._next_request
        started = time.perf_counter()
        self._requests[request_id] = (
            kind, self._epoch if epoch is None else epoch, apply, query_epoch, started, content_form,
        )
        if len(self._requests) == 1:
            self.busyChanged.emit(True)
        future = self._pool.submit(work)

        def completed(done) -> None:
            try:
                payload, error = done.result(), None
            except Exception as exc:  # surfaced to the UI, never silently swallowed
                payload, error = None, str(exc)
            # 先把结果送出去，再报耗时：报告失败绝不能连累结果本身。
            # 这条路径上没有别的兜底——丢了它，界面就永远停在「处理中」。
            try:
                if not self._closed:
                    self._workerDone.emit(request_id, payload, error)
            except Exception:  # QObject destroyed during teardown
                pass
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if elapsed_ms >= 150:
                try:
                    self.slowOperation.emit(kind, elapsed_ms)
                except Exception:
                    pass

        future.add_done_callback(completed)

    @Slot(str, "QVariantMap")
    def _invalidate_conversation_graph(self, kind, payload):
        if (kind == "conversation_progress" and payload.get("graph_reset_required")
                and self._project_path is not None
                and self.recentProjectKey(payload.get("project_path", "")) == self.recentProjectKey(str(self._project_path))):
            self._canvas.clear(preserve_positions=True)

    @Slot(int, object, object)
    def _on_worker_done(self, request_id: int, payload: object, error: object) -> None:
        with self._assistant_lock:
            assistant_job = self._assistant_jobs.pop(request_id, None)
        request = self._requests.pop(request_id, None)
        if not self._requests:
            self.busyChanged.emit(False)
        if request is None or self._closed:
            return
        kind, epoch, apply, query_epoch, started, content_form = request
        if epoch != self._epoch or (query_epoch is not None and query_epoch != self._query_epoch):
            return  # A different project was selected while the read was pending.
        if kind == "graph_view" and not self._unified_active:
            return
        if error is not None:
            if assistant_job and assistant_job.get("writing_target"):
                self.resultReady.emit("screenplay_writing_failed", {
                    "target": assistant_job["writing_target"], "task_id": assistant_job["task_id"],
                    "project_path": assistant_job["project_path"], "error": str(error),
                })
            conflict = self._content_forms.take_conflict(content_form)
            if conflict:
                self.resultReady.emit("content_submission_conflict", {**conflict, "form_id": content_form,
                    "project_path": str(self._project_path or "")})
            self.errorOccurred.emit(kind, str(error))
            return
        try:
            result = payload if isinstance(payload, dict) else {}
            conflict = self._content_forms.take_conflict(content_form)
            if conflict:
                self.resultReady.emit("content_submission_conflict", {**conflict, "form_id": content_form,
                    "project_path": str(self._project_path or "")})
            if kind in {"chat", "chats"}:
                result = result | self._assistant_activity_snapshot()
            if kind == "recent_projects" and result.get("generation") != self._recent_generation:
                return
            if kind == "detail" and result.get("node", {}).get("id") != self._detail_node:
                return
            from .native_content import receipt
            submission = receipt(result)
            if submission and submission.get("status") == "staged" and kind not in {"content_submission_read", "content_submissions"}:
                self.resultReady.emit("content_submission", {**submission, "form_id": content_form,
                    "original_kind": kind, "project_path": str(self._project_path or "")})
                return
            if apply is not None:
                result = apply(result)
            if submission and kind not in {"content_submission_read", "content_submissions"}:
                result = {**result, "content_form_id": content_form}
            # 界面自己处理这条结果花了多久：卡在布局/重建列表时，这个数会很大。
            handler_started = time.perf_counter()
            self.resultReady.emit(kind, result)
            handler_ms = int((time.perf_counter() - handler_started) * 1000)
            if handler_ms >= 100:
                self.slowOperation.emit(f"{kind}:界面", handler_ms)
            total_ms = int((time.perf_counter() - started) * 1000)
            if total_ms >= 300:
                self.slowOperation.emit(f"{kind}:合计", total_ms)
        except Exception as exc:
            self.errorOccurred.emit(kind, str(exc))

    def _drop_stale(self) -> None:
        """放弃迟迟不回来的操作，别让界面永远停在「处理中」。

        没有用定时器：那会在窗口对象销毁之后还触发，直接崩。改成每次提交前
        顺手清一次——「点了没反应」的那一刻，正是需要它的时候。
        """
        now = time.perf_counter()
        stale = [
            (request_id, request)
            for request_id, request in self._requests.items()
            # Conversation tasks run until completion or explicit stop; short
            # graph-refresh reads must never discard their terminal result.
            if request[0] not in {"assistant", "static_check"}
            and now - request[4] > self.STALE_AFTER_SECONDS
        ]
        if not stale:
            return
        for request_id, _request in stale:
            self._requests.pop(request_id, None)
        if not self._requests:
            self.busyChanged.emit(False)
        kinds = "、".join(sorted({request[0] for _rid, request in stale}))
        self.errorOccurred.emit(
            "timeout", f"{kinds} 超过 {int(self.STALE_AFTER_SECONDS)} 秒没有返回，已放弃这一步"
        )

    def pendingOperations(self) -> list[str]:
        """还没回来的操作（QML 与诊断用）：卡住时它说明卡在哪一步。"""
        return [request[0] for request in self._requests.values()]

    @Slot(result="QVariantList")
    def pendingKinds(self) -> list[str]:
        return self.pendingOperations()

    def _project(self) -> Path:
        if self._project_path is None:
            raise ValueError("请先打开图项目")
        return self._project_path

    def _project_task(
        self,
        kind: str,
        operation: Callable[[GraphOperations], dict],
        apply: Callable[[dict], dict] | None = None,
    ) -> None:
        try:
            path = self._project()
        except ValueError as exc:
            self.errorOccurred.emit(kind, str(exc))
            return

        content_form = "" if kind in {"assistant", "screenplay_writing", "source_task", "event_actions_task"} else self._content_forms.current()
        def work() -> dict:
            ops = project_catalog.open_graph_project(path)
            return operation(self._content_forms.proxy(ops, content_form))

        self._submit(kind, work, apply, content_form=content_form,
                     query_epoch=self._query_epoch if kind in {"graph_view", "canvas", "detail", "search", "expand", "timeline", "hierarchy", "reading_hierarchy", "event_compare", "property_context", "property_state", "property_world", "property_history",
                                   "node_properties"} else None)

    @Slot(result=str)
    def newContentFormSession(self) -> str:
        return self._content_forms.token()

    @Slot(str, bool, int)
    def activateContentForm(self, form_id: str, active: bool, priority: int) -> None:
        self._content_forms.activate(form_id, active, priority)

    @Slot(str, str, str)
    def listContentSubmissions(self, operation: str, target_json: str, form_id: str) -> None:
        target = json.loads(target_json or "{}")
        def work(ops):
            result = ops.list_content_submissions(operation=operation or None, target=target or None)
            if operation == "apply_storyboard_changes":
                for row in result["submissions"]:
                    detail = ops.read_content_submission(row["submission_id"], paths=[["changes", 0]])
                    action = (detail.get("values") or [{}])[0].get("value") or {}
                    row["action_kind"] = action.get("op")
                    row["action_fields"] = list(action)
            return result | {"form_id": form_id,
                "project_path": str(project_package.layout(ops.project_info().path).entry)}
        self._project_task("content_submissions", work)

    @Slot(str, str)
    def readContentSubmission(self, submission_id: str, form_id: str) -> None:
        def work(ops):
            result = ops.read_content_submission(submission_id, paths=[[]])
            return result | {"form_id": form_id, "project_path": str(project_package.layout(ops.project_info().path).entry)}
        def apply(result):
            arguments = next((value["value"] for value in result.get("values", []) if not value["path"]), {})
            self._content_forms.restore(form_id, result, arguments)
            return result
        self._project_task("content_submission_read", work, apply)

    @staticmethod
    def _exact_integers(settings: dict) -> dict:
        """QML numbers arrive as doubles; strict int fields need exact integers back."""
        normalized = dict(settings)
        for key in (
            "max_tokens", "context_window", "output_reserve", "compaction_reserve",
            "compaction_max_tokens", "timeout_seconds",
        ):
            value = normalized.get(key)
            if isinstance(value, float) and value.is_integer():
                normalized[key] = int(value)
        return normalized

    def _path_for(self, name_or_path: str) -> Path:
        """Public opening accepts only the private entry information file."""
        text = name_or_path.strip()
        if not text:
            raise ValueError("请选择工程入口文件")
        supplied = Path(text)
        if not supplied.is_absolute():
            raise ValueError("请给出工程入口文件的完整路径（.nproj 文件）")
        resolved = supplied.expanduser()
        if resolved.suffix.lower() != project_package.PACKAGE_SUFFIX or resolved.is_dir():
            raise ValueError("请选择 .nproj 工程入口文件，不是文件夹或数据库")
        if not resolved.is_file():
            raise ValueError("找不到这个 .nproj 工程入口文件")
        return resolved.resolve()

    @staticmethod
    def _project_view(ops: GraphOperations) -> dict:
        info = ops.graph_project_info()
        info = info | {"path": str(project_package.layout(ops.project_info().path).entry)}
        return {
            "project": info,
            "sources": ops.sources(),
            "provider": {
                key: ops.provider_profile().get(key) for key in (
                    "provider", "model", "max_tokens", "context_window", "output_reserve",
                    "compaction_reserve", "compaction_max_tokens",
                )
            },
        }

    @Slot()
    def loadGlobalSettings(self) -> None:
        """Application-wide harness defaults; available before a project is open."""

        def work() -> dict:
            store = ProviderStore()
            return {
                "profile": graph_profile_ops.global_settings(store),
                "catalog": providers.catalog(),
                "path": str(store.path),
            }

        self._submit("global_settings", work)

    @Slot("QVariantMap")
    def saveGlobalSettings(self, settings: dict) -> None:
        normalized = self._exact_integers(settings)

        def work() -> dict:
            store = ProviderStore()
            profile = graph_profile_ops.save_global_settings(store, normalized)
            return {"profile": profile, "catalog": providers.catalog(), "path": str(store.path)}

        self._submit("global_settings", work)

    # -- 外部 MCP 服务器（全局设置，跨工程共用） ------------------------------
    # 界面只负责「列出、编辑、触发刷新」；读配置、起子进程、注册工具都在 mcp_client 里，
    # 这里不重复实现那份逻辑，只做界面形状的转换与后台调度。

    @Slot(result="QVariantMap")
    def mcpServers(self) -> dict:
        """MCP 服务器清单 + 上次发现的工具；只读配置与缓存，不现场建连。

        ``tools`` 取缓存是为了「点开面板立刻有东西看」：真连接要等握手和超时，
        不能让一条列表把界面拖住；要最新结果按「测试连接」或「刷新工具」。
        """
        from . import tool_api

        snapshot: dict[str, Any] = {
            "ok": True,
            "path": str(mcp_client.default_path()),
            "cache_path": str(mcp_client.default_cache_path()),
            "error": "",
            "servers": [],
            "startup_error": tool_api.MCP_STARTUP_ERROR or "",
        }
        try:
            servers = mcp_client.load_servers()
        except mcp_client.McpConfigError as exc:
            # 坏配置要报成错误而不是空列表：说「没有服务器」会把用户引到错方向
            # （他会去满头找设置，而真正要改的是那个 JSON 文件）。
            snapshot["ok"] = False
            snapshot["error"] = str(exc)
            return snapshot
        cache = mcp_client.load_cache()
        last_errors = mcp_client.last_errors()
        snapshot["servers"] = [_mcp_server_row(server, cache.get(server.name) or [], last_errors)
                               for server in servers]
        return snapshot

    @Slot("QVariantList")
    def saveMcpServers(self, rows: object) -> None:
        """保存整份 MCP 服务器清单（界面传全量），成功后后台刷新工具。

        写文件是毫秒级的小操作，留在界面线程无妨；建连与 ``tools/list`` 要等超时，
        所以刷新一律扔到后台，完成后再发 ``mcpServersChanged`` 让面板重读。
        """
        names: list[str] = []
        try:
            servers = [_mcp_server_from_row(row, index)
                       for index, row in enumerate(list(rows or []))]
            names = [server.name for server in servers]
            duplicated = sorted({name for name in names if names.count(name) > 1})
            if duplicated:
                # 重名不能存：load_servers 会因为重名整份配置读不出来，
                # 那比在这里报错更难恢复（界面上连列表都没有了）。
                raise mcp_client.McpConfigError(f"服务器名称重复：{'、'.join(duplicated)}")
            previous = {server.name for server in mcp_client.load_servers()}
            target = mcp_client.save_servers(servers)
        except (mcp_client.McpConfigError, ValueError) as exc:
            self.errorOccurred.emit("mcp", str(exc))
            return
        removed = previous - set(names)
        if removed:
            self._drop_mcp_tools_of(removed)
        self.noticeRaised.emit(f"MCP 配置已保存：{target}，正在后台刷新工具…")
        self._refresh_mcp_servers(close_sessions=True)

    @Slot()
    def refreshMcpServers(self) -> None:
        """后台重连所有启用的服务器并重发现工具；结果通过 mcpServersChanged 回报。"""
        self._refresh_mcp_servers()

    @staticmethod
    def _drop_mcp_tools_of(names: set[str]) -> None:
        """配置里删掉（或改名）的服务器，它的工具要立刻从工具表撤掉。

        mcp_client.refresh 只清理「还在配置里」的服务器留下的过期工具，被删掉的服务器
        注册过的工具名会一直挂在工具表上：模型还能调用它，而一调用又会把这台已经
        不存在的服务器重新拉起来。归属判断用内核自己维护的那张表——重名后缀让工具名
        无法反推出属于谁，按前缀猜会误伤名字有包含关系的服务器。
        """
        from . import tool_api

        owned = getattr(mcp_client, "_OWNED", None)
        if not isinstance(owned, dict):
            return
        for tool_name, owner in list(owned.items()):
            if owner in names:
                tool_api.TOOLS.pop(tool_name, None)

    def _refresh_mcp_servers(self, *, close_sessions: bool = False) -> None:
        """在后台线程里刷新外部工具，结束时发 ``mcpServersChanged``。

        没有直接用 ``mcp_client.refresh_async``，是因为「重配前先 close」必须排在刷新
        之前：close 要等每台服务器优雅退出（最坏几秒），同样不能占界面线程，所以两件事
        一起放进同一个后台线程里按顺序做。
        """
        if self._mcp_refreshing:
            return  # 面板已经按状态禁用了按钮，这里再兜一层：不并行起两个刷新线程
        self._mcp_refreshing = True

        def run() -> None:
            from . import tool_api

            try:
                if close_sessions:
                    mcp_client.close()
                try:
                    # 每台服务器的成败已经落在 last_errors() 与工具缓存里，面板重读
                    # mcpServers() 就能看到，报告本身不用再往界面上塞一份。
                    mcp_client.refresh(tool_api.Tool, tool_api._register)
                except Exception as exc:  # noqa: BLE001 - 后台线程里的异常没人接得住
                    self.errorOccurred.emit("mcp", f"MCP 工具刷新失败：{exc}")
            finally:
                self._mcp_refreshing = False
                self.mcpServersChanged.emit()

        threading.Thread(target=run, name="mcp-settings-refresh", daemon=True).start()

    # -- 技能（全局设置，跨工程共用） ----------------------------------------
    # 技能不建连、不起子进程：读一份 JSON、写一份 JSON 都是毫秒级，所以这几个槽位都在
    # 界面线程里同步做完，完成后直接发 skillsChanged 让面板重读。MCP 那组需要后台线程
    # 是因为握手与超时会拖住界面，技能没有这个问题。

    @Slot(result="QVariantMap")
    def skills(self) -> dict:
        """技能清单（内置在前、用户在后）+ 编辑器可选的业务组与工具。

        读不懂的用户文件返回 ``ok=false`` 与可读错误，而不是空列表：说「没有技能」会把
        用户引到错方向（他会去重建技能，而真正要改的是那个 JSON 文件）。内置技能来自
        代码，读用户文件失败时列表照样为空——但错误已经写在 ``error`` 里，界面先显示它。
        """
        from . import tool_api

        # 内置提示词位先算好（只读、不落盘）：它不受「用户技能文件读不读得出来」影响，
        # 面板的「内置提示词」区必须照常列出 17 条，才能让人看到哪条被谁覆盖了。
        # list_prompt_keys 自己容错（坏文件时 replaced_by 全为 None），不在这里抛。
        prompt_keys = prompt_registry.list_prompt_keys()
        snapshot: dict[str, Any] = {
            "ok": True,
            "path": str(skills.default_path()),
            "error": "",
            "startup_error": tool_api.SKILL_STARTUP_ERROR or "",
            # 可选项是注册表的静态事实，和读文件成败无关：编辑器要能选「还没有任何技能
            # 点名过」的业务组与工具，所以先给出来再试读文件。
            "families": sorted(skills.known_families()),
            "tools": sorted(skills.known_tools()),
            "skills": [],
            # 内置提示词位（key/label/description/version/replaced_by/source）：
            # 「复制成我的技能」的入口就从这份清单来。
            "prompt_keys": prompt_keys,
            # 技能文件坏掉时提示词位全部回落到内置；原因在这里说一次，面板显示一行。
            "prompt_problem": prompt_registry.skill_problem() or "",
        }
        try:
            rows = skills.load_skills()
        except skills.SkillError as exc:
            snapshot["ok"] = False
            snapshot["error"] = str(exc)
            return snapshot
        snapshot["skills"] = [_skill_row(skill) for skill in rows]
        return snapshot

    @Slot("QVariantList", "QVariantList", result="QVariantMap")
    def skillOpens(self, families: object, tools: object) -> dict:
        """编辑器里的「会打开哪些工具」实时预览：不必先保存就能看到结果。"""
        try:
            return _skill_opens(_skill_names(families, "families"), _skill_names(tools, "tools"))
        except ValueError as exc:
            return {"resolved_tools": [], "problem": str(exc)}

    @Slot(str, result="QVariantMap")
    def duplicatePromptAsSkill(self, key: str) -> dict:
        """把某条内置提示词**当前生效**的全文预填成一条新技能草稿（不落盘）。

        面板的「复制成我的技能」用它：返回的 ``draft`` 直接灌进编辑器，用户改完点保存
        才走 ``saveSkill``。默认 ``replaces`` 就指向这个键——复制出来的技能如果不顶替
        任何提示词，就只是一段没人会加载的说明，这个动作等于白做。

        预填的是**当前生效**全文（可能已经是被某条技能改写过的版本），不是 `DEFAULT_PROMPTS`
        里那份：用户看到的要是他此刻真正在用的文本，否则「改一处措辞」就变成了「回到出厂」。
        """
        name = str(key or "").strip()
        try:
            resolved = prompt_registry.resolve_prompt(name)
        except ValueError as exc:
            # 未知键：内核的报错已经列出全部可选键，直接摆到面板上。
            self.errorOccurred.emit("skill", str(exc))
            return {"ok": False, "error": str(exc)}
        entry = DEFAULT_PROMPTS.get(name) or {}
        label = str(entry.get("label") or name)
        text = resolved.get("text") or ""
        draft = {
            # original 为空 = 新建；名字是身份，复制出来的草稿用「标签·我的改法」，
            # 撞名时内核会在保存那一步说清楚（可以在这之前改成别的名字）。
            "original": "",
            "name": f"{label}·我的改法"[:64],
            "summary": f"顶替内置提示词「{label}」，按我的写法生效。"[:200],
            "instructions": text,
            "families": [],
            "tools": [],
            "enabled": True,
            "replaces": name,
        }
        result: dict[str, Any] = {
            "ok": True,
            "key": name,
            "label": label,
            "draft": draft,
            # 来源：builtin 表示抄的是内置原文，skill 表示当前生效的文本已经来自某条技能。
            "source": resolved.get("source"),
            "overridden_by": resolved.get("overridden_by"),
        }
        if resolved.get("ignored"):
            # 同键还有别的技能：解析只认最近更新的那条，被忽略的也要说出来。
            result["ignored"] = resolved["ignored"]
        if resolved.get("problem"):
            result["problem"] = resolved["problem"]
        if not text:
            # 生效文本为空（覆盖它的技能正文是空的）时复制不出正文：说清楚，别让编辑器
            # 打开一片空白却不解释。
            result["problem"] = "这条提示词当前生效的文本为空（顶替它的技能正文为空）；编辑器里需要自己写一段。"
        return result

    @Slot("QVariantMap")
    def saveSkill(self, row: dict) -> None:
        """新建或修改一条技能。``original`` 为已保存的名字时走修改，否则走新建。

        名字是身份：内核没有改名接口（``update_skill`` 按名字定位），所以界面在编辑
        已有技能时把名称框置灰；这里也不再假装支持改名——改名要另存一条。
        """
        data = dict(row or {})
        original = str(data.get("original") or "").strip()
        try:
            enabled = data.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                raise skills.SkillError("enabled 须为布尔值（界面上就是那个启用开关的状态）")
            families = _skill_names(data.get("families"), "families")
            tools = _skill_names(data.get("tools"), "tools")
            # replaces / workflow 只在界面**明确带了这个键**时才传给内核：
            # 传 None 在内核里表示「不动这个字段」（行内启用开关回传整行时用的就是这个语义，
            # 不能因为没带 workflow 就把已有声明清空）；带空串/空对象才是显式清空。
            replaces = (_skill_text(data["replaces"], "replaces")
                        if "replaces" in data else None)
            workflow = (_skill_mapping(data["workflow"], "workflow")
                        if "workflow" in data else None)
            if original:
                skill = skills.update_skill(
                    original,
                    summary=data.get("summary"),
                    instructions=data.get("instructions"),
                    families=families,
                    tools=tools,
                    enabled=enabled,
                    replaces=replaces,
                    workflow=workflow,
                )
            else:
                skill = skills.create_skill(data.get("name"), data.get("summary"),
                                            data.get("instructions"), families, tools,
                                            replaces=replaces, workflow=workflow)
                # 内核新建一律从「启用」开始；编辑器里没勾启用时在同一份提交里补一步
                # 停用，仍然交给内核写盘——界面不自己拼一个 Skill 直接覆盖整表。
                if enabled is False:
                    skill = skills.update_skill(skill.name, enabled=False)
        except ValueError as exc:
            # SkillError 是 ValueError 的子类：校验失败与读写失败都从这里出去，
            # 消息本来就是写给调用者看的（说清哪里不对、下一步怎么做）。
            self.errorOccurred.emit("skill", str(exc))
            return
        self.noticeRaised.emit(f"技能「{skill.name}」已保存：{skills.default_path()}")
        self.skillsChanged.emit()

    @Slot(str)
    def deleteSkill(self, name: str) -> None:
        """删除一条用户技能；内置技能由内核拒绝，错误原样交给界面显示。"""
        try:
            removed = skills.delete_skill(name, confirm=True)
        except ValueError as exc:
            self.errorOccurred.emit("skill", str(exc))
            return
        if not removed:
            # 不存在不当成功：界面那一行可能已经被别处改过（手改文件或另一个窗口），
            # 静默「删成功」会让人以为删掉的是另一样东西。
            self.errorOccurred.emit(
                "skill", f"没有自定义技能 {name}：内置技能不可删除，列表可能已经变了，请重新载入")
            return
        self.noticeRaised.emit(f"技能「{name}」已删除：{skills.default_path()}")
        self.skillsChanged.emit()

    @Slot()
    def closeProject(self) -> None:
        """Return to the launcher: drop the open project and its cached reads."""
        self._epoch += 1
        self._reading_cut = {}
        self._state_cut = ""
        self._project_path = None
        self._canvas.clear()
        self._query_canvas = None
        self._close_assistant()
        self.resultReady.emit("project_closed", {})
        # The launcher stays mounted while a project is open. Refresh its
        # initial snapshot after changing epoch, so it includes this visit.
        self.loadRecentProjects()

    @Slot(str, result=str)
    def urlToPath(self, url: str) -> str:
        """QML 的 FolderDialog 给的是 file:///…，界面要显示成路径。"""
        from PySide6.QtCore import QUrl

        return QUrl(url).toLocalFile() or url

    @Slot(result=str)
    def homeDirectory(self) -> str:
        """新建工程时的默认位置。没有「工程目录」，所以退到用户目录。"""
        return str(Path.home())

    @Slot(str, str)
    def createProjectAt(self, parent_url: str, name: str) -> None:
        """在选定的位置建一个工程包。没有「默认工程目录」这回事。"""
        if not re.fullmatch(r"[\w-]{1,64}", name):
            self.errorOccurred.emit("project", "项目名只能含字母、数字、中文、下划线或短横线")
            return
        from PySide6.QtCore import QUrl

        folder = Path(QUrl(parent_url).toLocalFile() or parent_url)
        if not folder.is_dir():
            self.errorOccurred.emit("project", "请先选一个存在的文件夹")
            return
        path = folder / f"{name}{project_package.PACKAGE_SUFFIX}"
        # 只认入口文件会漏掉「从最近列表移除了、但文件还在磁盘上」的半残状态：那时
        # 入口文件仍在，用户看到「同名项目已经存在」却不知道是哪条路、怎么办。
        leftovers = [item for item in (path, folder / f"{name}_data", folder / f"{name}_extraction",
                                       folder / f"{name}_chats") if item.exists()]
        if leftovers:
            self.errorOccurred.emit(
                "project",
                "该位置已有同名工程：" + "、".join(str(item) for item in leftovers)
                + "。移出最近列表不会删文件；换一个代号，或先删除上面这些文件/目录再试。",
            )
            return
        self._epoch += 1
        self._reading_cut = {}
        self._state_cut = ""
        self._project_path = None
        self._canvas.clear()
        self._query_canvas = None
        epoch = self._epoch

        def work() -> dict:
            ops = project_catalog.create_graph_project(path)
            return self._project_view(ops)

        def apply(result: dict) -> dict:
            self._project_path = path
            self._canvas.clear()
            # 换了工程就是换了话题：旧的会话不该把上一个工程的内容带进来。
            self._close_assistant()
            self._remember_project(path)
            return result

        self._submit("project", work, apply, epoch=epoch)

    @Slot(str)
    def openProject(self, name_or_path: str) -> None:
        try:
            path = self._path_for(name_or_path)
        except ValueError as exc:
            self.errorOccurred.emit("project", str(exc))
            return
        self._epoch += 1
        self._reading_cut = {}
        self._state_cut = ""
        self._project_path = None
        self._canvas.clear()
        self._query_canvas = None
        epoch = self._epoch

        def work() -> dict:
            ops = project_catalog.open_graph_project(path)
            return self._project_view(ops)

        def apply(result: dict) -> dict:
            self._project_path = path
            self._canvas.clear()
            # 换了工程就是换了话题：旧的会话不该把上一个工程的内容带进来。
            self._close_assistant()
            self._remember_project(path)
            return result

        self._submit("project", work, apply, epoch=epoch)

    # -- 最近打开 --------------------------------------------------------
    #
    # 工程可以放在任何地方，「打开工程…」打开的往往不在 projects 目录里。
    # 不记住的话，下次启动列表里就没有它了。

    RECENT_LIMIT = 8

    def _recent_paths(self, *, strict: bool = False) -> list[str]:
        try:
            raw = ProviderStore().get(GLOBAL_SCOPE, "recent_projects")
        except Exception:  # 存储坏了不该连累启动
            if strict:
                raise
            return []
        if not raw:
            return []
        try:
            rows = json.loads(raw)
        except ValueError:
            if strict:
                raise
            return []
        if strict and not isinstance(rows, list):
            raise ValueError("最近工程列表格式无效")
        if not isinstance(rows, list):
            return []
        return [row for row in rows
                if isinstance(row, str) and self.recentProjectKey(row) not in self._recent_removed]

    def _warn_recent_write(self, action: str, exc: Exception) -> None:
        """清单写不进去只发一条提示，绝不让动作失败。

        最近工程列表是「下次启动的便利记录」，不是业务数据：写不进去不影响这个工程和
        它的数据。常见原因只有一个——当前进程写不了用户目录（低完整性、受限终端或
        沙箱启动），所以提示里必须写清「怎么办」。
        """
        self.noticeRaised.emit(
            f"{action}：{exc}。"
            "最近工程列表只是下次启动的便利记录，不影响工程本身和它的数据；"
            "写不进去通常是这个进程没有用户目录（C:\\Users\\<用户名>\\.narrative）的写权限，"
            "常见原因是从受限终端、沙箱或代理里启动。关闭后从桌面/资源管理器双击 "
            "start-desktop.cmd 重新启动即可。"
        )

    def _remember_project(self, path: Path) -> None:
        text = str(path)
        rows = [row for row in self._recent_paths() if row != text]
        rows.insert(0, text)
        try:
            ProviderStore().put(
                GLOBAL_SCOPE,
                "recent_projects",
                json.dumps(rows[: self.RECENT_LIMIT], ensure_ascii=False),
            )
        except Exception as exc:  # 记不住不影响打开
            self._warn_recent_write("已打开工程，但没能写进最近工程列表", exc)
        else:
            # 写成功了，就别再拿内存里的「已移除」压着它：重新打开就是重新记住。
            self._recent_removed.discard(self.recentProjectKey(text))

    @Slot(str)
    def openProjectUrl(self, url: str) -> None:
        """QML 的 FileDialog 给的是 file:///… URL，转成本地路径再打开。"""
        from PySide6.QtCore import QUrl

        local = QUrl(url).toLocalFile()
        self.openProject(local or url)

    @Slot(str, result=str)
    def recentProjectKey(self, path: str) -> str:
        from PySide6.QtCore import QUrl

        return os.path.normcase(str(Path(QUrl(path).toLocalFile() or path).resolve()))

    @Slot()
    def loadRecentProjects(self) -> None:
        """最近打开过的工程，带「还在不在」——搬走或删掉的不该点进去才报错。"""

        self._recent_generation += 1
        generation = self._recent_generation

        def work() -> dict:
            items = []
            for text in self._recent_paths():
                path = Path(self.urlToPath(text))
                name, title, exists, copyable = path.stem, path.stem, False, False
                if path.is_file() and path.suffix.lower() == project_package.PACKAGE_SUFFIX:
                    try:
                        found = project_package.layout(path)
                        exists = found.db.is_file()
                        from .operations import Operations
                        copyable = Operations.inspect_graph_project(path)["status"] == "ready"
                        title = project_package.project(found)["project"]["title"] or name
                    except (OSError, ValueError, KeyError):
                        pass
                items.append(
                    {"path": text, "name": name, "title": title, "exists": exists, "copyable": copyable}
                )
            return {"items": items, "generation": generation}

        self._submit("recent_projects", work)

    @Slot(str)
    def suggestProjectCopy(self, source: str) -> None:
        from PySide6.QtCore import QUrl

        from .operations import Operations

        def work() -> dict:
            path = Operations.next_project_copy_path(self.urlToPath(source))
            return {"source": source, "destination": QUrl.fromLocalFile(str(path)).toString()}

        self._submit("project_copy_destination", work)

    @Slot(str, str)
    def duplicateProject(self, source: str, destination: str) -> None:
        from .operations import Operations

        def work() -> dict:
            target = Path(self.urlToPath(destination))
            copied = Operations.duplicate_graph_project(self.urlToPath(source), target)
            self._remember_project(target)
            return {"path": str(target), "report": getattr(copied, "_copy_report", {})}

        def apply(result: dict) -> dict:
            self.loadRecentProjects()
            return result

        self._submit("project_duplicated", work, apply)

    @Slot(str, bool)
    def removeProject(self, path: str, delete_files: bool) -> None:
        """Forget the entry; ``delete_files`` removes what its descriptor owns.

        删什么由 descriptor 说了算（见 ``project_removal``），不靠目录名猜——只有
        真有危险（别人也在用、在入口目录之外、会连入口目录本身一起删）的项才保留，
        并且每项都带人话原因进 payload。

        清单写失败不放弃动作：先按用户意图做完（内存里移除、关工程、删自有文件、刷新
        列表），最后只发一条能看懂的提示。最近工程列表是下次启动的便利记录，不是业务
        数据——它写不进去，不该让「移出工程」整段失败。
        """
        from PySide6.QtCore import QUrl

        target = Path(QUrl(path).toLocalFile() or path).resolve()
        key = self.recentProjectKey(path)
        try:
            rows = [row for row in self._recent_paths(strict=True)
                    if self.recentProjectKey(row) != key]
        except Exception:
            # 读坏了也要能移除：读不出来的条目本来就不在列表里。
            rows = [row for row in self._recent_paths() if self.recentProjectKey(row) != key]
        write_error: Exception | None = None
        try:
            ProviderStore().put(
                GLOBAL_SCOPE, "recent_projects", json.dumps(rows, ensure_ascii=False)
            )
        except Exception as exc:
            write_error = exc
            # 写不进去就在内存里也当它已移除，否则下面的刷新会把它捡回列表。
            self._recent_removed.add(key)
        self._recent_generation += 1
        if self._project_path is not None and self.recentProjectKey(str(self._project_path)) == key:
            self.closeProject()
        removed: list[str] = []
        retained: list[tuple[Path, str]] = []
        deletion_error = ""
        if delete_files and target.exists():
            try:
                other_entries = set(target.parent.glob("*.nproj")) | {
                    Path(self.urlToPath(row)) for row in rows
                }
                # 要删什么由 descriptor 决定，由目录名猜是另一种形式的静默不删。
                plan = project_removal.plan_removal(target, other_entries=other_entries)
                outcome = project_removal.apply_plan(plan)
                removed = [str(path) for path in outcome.removed]
                retained = plan.retained
                if outcome.errors:
                    # 删不掉的项如实报错，不许当作已经删了。
                    deletion_error = "；".join(outcome.errors)
                    self.errorOccurred.emit("project", deletion_error)
            except (OSError, ValueError) as exc:
                deletion_error = str(exc)
                self.errorOccurred.emit("project", deletion_error)
        self.resultReady.emit("project_removed", {
            "path": str(target), "deleted": removed, "kept": not removed,
            "retained_data": bool(retained),
            "retained_paths": [{"path": str(path), "reason": reason}
                               for path, reason in retained],
            "key": key, "deletion_error": deletion_error,
        })
        self.loadRecentProjects()
        if write_error is not None:
            # 动作已经做完了，只是清单没写进去：一条提示，且要在刷新列表之后发。
            self._warn_recent_write("已移出工程，但没能写进最近工程列表", write_error)

    @Slot(str)
    def forgetRecentProject(self, path: str) -> None:
        self.removeProject(path, False)

    @Slot()
    def loadGraph(self) -> None:
        self._unified_active = False
        def work(ops) -> dict:
            return {"view": ops.graph_query(limit=2000, disclosure_cut=self._reading_cut or None), "labels": ops.graph_display_map()["items"]}

        def apply(result: dict) -> dict:
            self._query_canvas = None
            return self._canvas.seed(result["view"], result["labels"], prefer_events=any(e.get("semantic") == "temporal" for e in result["view"].get("edges", [])))

        self._project_task("canvas", work, apply)

    @Slot("QVariantMap")
    def loadGraphView(self, spec: dict) -> None:
        """One shared query controls every reading projection and cancels stale reads."""
        try:
            self._project()
        except ValueError as exc:
            self.errorOccurred.emit("graph_view", str(exc))
            return
        self._query_epoch += 1
        self._unified_active = True
        requested = dict(spec)
        self._reading_cut = dict(requested.get("disclosure_cut") or {})
        self._query_spec = requested
        self._query_canvas = None
        self._query_folded.clear()
        self._query_visible.clear()
        self._canvas.clear(preserve_positions=True)

        def work(ops) -> dict:
            view = ops.graph_view(requested | {"view": "table"})
            return {"view": view, "labels": ops.graph_display_map()["items"]}

        def apply(result: dict) -> dict:
            view = result["view"]
            self._query_canvas = view
            self._query_visible = {node["id"] for node in view["nodes"]}
            return {"canvas": view, "labels": result["labels"]}

        self._project_task("graph_view", work, apply)

    @Slot()
    @Slot(str, bool)
    def loadRelations(self, cut: str = "", related_only: bool = False) -> None:
        """人物关系网：人是点、关系是有方向的边，给关系图用。

        `cut` 是阅读位置——只画那一步之前已经知道的关系；`related_only` 再收一层，
        只留跟这一步有关的人。
        """
        from .tool_api import _relation_map

        spec = {"cut": cut, "related_only": related_only}
        self._project_task("relations", lambda ops: _relation_map(ops, spec))

    @Slot("QVariantMap")
    def loadBrowse(self, spec: dict) -> None:
        """One question for the browsing page: nodes, counts, steps and the cut.

        Filtering, counting, the step list and the related-node rule all happen
        in the projection; the page only renders what comes back. The cut is
        resolved there too, so the list and the property pane cannot disagree.
        """
        try:
            self._project()
        except ValueError as exc:
            self.errorOccurred.emit("browse", str(exc))
            return
        self._query_epoch += 1
        epoch = self._query_epoch
        requested = dict(spec)

        def work() -> dict:
            ops = project_catalog.open_graph_project(self._project())
            return ops.browse_view(
                granularity=str(requested.get("granularity") or "scene"),
                cut_id=str(requested.get("cut_id") or ""),
                node_type=str(requested.get("node_type") or ""),
                query=str(requested.get("query") or ""),
                related_only=bool(requested.get("related_only")),
                time_filter=bool(requested.get("time_filter")),
            )

        def apply(result: dict) -> dict:
            cut = result.get("cut") or {}
            # The property pane cuts at the same anchor event the list uses.
            self._state_cut = str(requested.get("cut_id") or cut.get("event_id") or "")
            return result

        self._submit("browse", work, apply, query_epoch=epoch)

    @Slot(str)
    def loadNodeProperties(self, node_id: str) -> None:
        """Every property of one node, read-only; used by the browsing detail pane."""

        def work(ops) -> dict:
            return ops.property_node_summary(node_id, cut=self._state_cut or None)

        self._project_task("node_properties", work)

    @Slot()
    def loadPropertyCategories(self) -> None:
        """The display vocabulary per node type, one list for each type."""

        def work(ops) -> dict:
            return {"vocabulary": ops.property_categories()}

        self._project_task("property_categories", work)

    @Slot(str)
    def savePropertyCategories(self, config_json: str) -> None:
        """Replace the vocabulary. Existing records keep their own category."""
        try:
            config = json.loads(config_json)
        except json.JSONDecodeError as exc:
            self.errorOccurred.emit("property_categories", f"分类配置 JSON 无效：{exc.msg}")
            return

        def work(ops) -> dict:
            result = ops.set_property_categories(config)
            return {
                "vocabulary": result["vocabulary"],
                "saved": True,
                "moved": len(result.get("moved") or []),
            }

        self._project_task("property_categories", work)

    @Slot(str, str)
    def categorizeProperties(self, targets_json: str, category: str) -> None:
        """Move disclosures to a category. Appends a record; nothing is rewritten."""
        try:
            targets = json.loads(targets_json)
        except json.JSONDecodeError as exc:
            self.errorOccurred.emit("property_categorize", f"记录 ID 列表无效：{exc.msg}")
            return
        if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
            self.errorOccurred.emit("property_categorize", "请提供记录 ID 数组")
            return

        def work(ops) -> dict:
            return ops.property_categorize([str(item) for item in targets], category)

        self._project_task("property_categorize", work)

    @Slot(str, str)
    def mergeProperties(self, source_id: str, target_id: str) -> None:
        """Physically merge definitions and their references."""
        self._project_task("property_merge", lambda ops: ops.merge_properties(
            source_id, target_id, "用户合并属性", ops.graph_project_info()["revision"],
        ) | {"target_id": target_id})

    @Slot(str, str)
    def renameProperty(self, from_id: str, to_id: str) -> None:
        """Rename the current definition and update references."""
        self._project_task("property_rename", lambda ops: ops.rename_property(
            from_id, to_id, "用户重命名属性", ops.graph_project_info()["revision"],
        ) | {"to_id": to_id})

    @Slot("QVariantMap")
    def addPropertyRecord(self, payload: dict) -> None:
        fields = {key: payload[key] for key in ("value", "value_type", "cardinality", "symmetric", "scope", "holder_id",
                  "context_id", "quote", "source_id", "category", "source", "related_nodes") if key in payload and (key not in {"quote", "source_id"} or payload[key])}
        self._project_task("property_add", lambda ops: ops.record_property_change(
            payload.get("node_id"), payload.get("property_id"), payload.get("event_id"), payload.get("op"),
            expected_revision=int(payload["expected_revision"]), reason=payload.get("reason") or "人工记录属性变更", **fields))

    @Slot(str, "QVariantMap")
    def correctPropertyRecord(self, record_id: str, payload: dict) -> None:
        changes = {key: payload[key] for key in ("op", "value", "event_id", "category", "scope", "holder_id", "context_id",
                   "source", "related_nodes", "source_id", "quote") if key in payload and (key not in {"quote", "source_id"} or payload[key])}
        self._project_task("property_correct", lambda ops: {"status": "accepted", **ops.correct_property(
            record_id, changes, payload.get("reason") or "更正属性变更录入错误", expected_revision=int(payload["expected_revision"]))})

    @Slot(str, int)
    def deletePropertyRecords(self, targets_json: str, expected_revision: int) -> None:
        try:
            targets = json.loads(targets_json)
            if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
                raise ValueError("请提供变更 ID 数组")
        except ValueError as exc:
            self.errorOccurred.emit("property_delete", str(exc))
            return
        self._project_task("property_delete", lambda ops: ops.delete_property_records(
            targets, "用户删除错误变更", expected_revision=expected_revision) | {"deleted": targets})

    @Slot(str, int)
    def previewRollback(self, source_id: str, start: int) -> None:
        self._project_task("rollback_preview", lambda ops: ops.rollback_preview(
            source_id, start, ops.graph_project_info()["revision"],
        ))

    @Slot(str, int, int, str)
    def rollbackTo(self, source_id: str, start: int, expected_revision: int, reason: str) -> None:
        def apply(result: dict) -> dict:
            if result.get("status") == "accepted":
                self._canvas.clear()
                self._query_canvas = None
                result = result | {"graph_reset_required": True}
            return result
        self._project_task("rollback", lambda ops: ops.rollback_to(
            source_id, start, expected_revision, reason,
        ), apply)

    @Slot(str)
    def loadNodeEditor(self, node_id: str) -> None:
        """Everything the node editor needs, scoped to the reading position.

        At a cut the editor shows what the reader had met by then and is read-only;
        at 最新 it shows the whole history and can edit.
        """
        cut = self._state_cut or None
        self._project_task("node_editor", lambda ops: ops.node_editor(node_id, cut=cut))

    # -- 节点资料（一张张带生效/失效时间的资料卡）-----------------------

    @Slot()
    def loadAdaptation(self) -> None:
        self._project_task("adaptation", lambda ops: ops.adaptation_snapshot())

    @Slot(str)
    def loadScreenplayWorkspace(self, screenplay_id: str) -> None:
        self._screenplay_query_generation += 1
        generation = self._screenplay_query_generation
        def work(ops):
            storage = ops.inspect_screenplay_storage()
            if storage["status"] == "incomplete":
                return {"storage": storage, "screenplays": [], "adaptation": ops.adaptation_snapshot(),
                        "timeline": {"nodes": [], "revision": 0}, "document": {}}
            items = ops.list_screenplays()
            selected = screenplay_id if any(item["id"] == screenplay_id for item in items) else (items[0]["id"] if items else "")
            return {"storage": storage, "screenplays": items, "adaptation": ops.adaptation_snapshot(),
                    "timeline": ops.read_screenplay_timeline(selected) if selected else {"nodes": [], "revision": 0},
                    "document": ops.read_screenplay(selected) if selected else {}}
        self._project_task("screenplay_workspace", work, lambda result: result if
                           generation == self._screenplay_query_generation else {"superseded": True})

    @Slot(QObject)
    def observeBlockTextDoubleClicks(self, item: QObject) -> None:
        if item.objectName() == "scriptBlockDisplayText" and item.property("readOnly") is True:
            self._block_text_events.observe(item)

    @Slot(str, "QVariantMap")
    def screenplayAction(self, operation: str, arguments: dict) -> None:
        allowed = {
            "preview_adaptation_span", "create_adaptation_span", "update_adaptation_span", "read_adaptation_span",
            "create_screenplay", "read_screenplay", "apply_screenplay_structure",
            "create_script_segment", "update_script_segment", "read_script_segment",
            "read_scene_table_context", "submit_scene_table", "apply_scene_table_changes", "adopt_scene_table",
            "read_script_unit_context", "submit_script_unit", "apply_script_block_changes",
            "mark_writing_basis_current", "check_screenplay", "export_screenplay", "compile_screenplay",
            "load_compile_settings", "save_compile_settings",
            "load_creative_agreement", "save_creative_agreement",
            "prepare_script_writing_targets",
            "read_screenplay_timeline", "import_screenplay_timeline_candidates", "import_screenplay_timeline",
            "append_screenplay_timeline",
            "edit_screenplay_timeline_node", "move_screenplay_timeline_node",
            "copy_screenplay_timeline_node", "delete_screenplay_timeline_node",
            "inspect_screenplay_storage", "reset_screenplay_storage",
        }
        if operation not in allowed:
            self.errorOccurred.emit("screenplay", "未知剧本操作")
            return
        args: dict[str, Any] = json.loads(json.dumps(dict(arguments), ensure_ascii=False))
        request_token = args.pop("_view_request_token", None)
        def normalize(value) -> Any:
            if isinstance(value, dict):
                    return {key: int(item) if (key.startswith("expected_") or key == "position") and item is not None
                        and isinstance(item, (int, float)) and item == int(item) else normalize(item)
                        for key, item in value.items()}
            if isinstance(value, list):
                return [normalize(item) for item in value]
            return value
        args = dict(normalize(args))
        self._project_task("screenplay", lambda ops: {
            "operation": operation, "request_screenplay_id": args.get("screenplay_id"),
            "request_selection_ids": args.get("selection_ids"),
            "request_token": request_token,
            "result": getattr(ops, operation)(**args)})

    @Slot(str, result=str)
    def storyboardImageUrl(self, path: str) -> str:
        from PySide6.QtCore import QUrl
        return QUrl.fromLocalFile(path).toString() if path else ""

    @Slot("QVariantMap", str, str, result=str)
    def storyboardPanelPreviewUrl(self, board: dict, shot_id: str, panel_id: str) -> str:
        """Render the already-read board snapshot without changing project state."""
        import base64
        import io

        from .storyboard_panels import render_panel
        try:
            shot = next(row for row in board.get("shots", []) if row["id"] == shot_id)
            panel = next(row for row in shot.get("panels", []) if row["id"] == panel_id)
            output = io.BytesIO()
            render_panel(board, shot, panel, annotated=False).save(output, format="PNG")
            return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
        except (ValueError, OSError, KeyError, StopIteration):
            return ""

    @Slot(str, "QVariantMap", int)
    def storyboardAction(self, operation: str, arguments: dict, token: int) -> None:
        """Keep the response scoped to the scene and request that opened it."""
        from .node_material import _as_path

        def normalize(value, key=""):
            if isinstance(value, dict):
                return {k: normalize(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [normalize(item) for item in value]
            if key in {"sketch_path", "style_path", "output_path", "audio_path", "video_path", "path"} and isinstance(value, str) and value:
                return str(_as_path(value))
            if key in {"revision", "expected_revision", "offset_ms", "duration_ms", "index", "time_ms", "action_start_ms", "fps", "source_in_ms", "in_ms", "out_ms", "start_ms", "end_ms"}:
                if isinstance(value, float) and value.is_integer():
                    return int(value)
            return value

        def work(ops):
            try:
                actions = {
                    "read_storyboard", "read_storyboard_usage_tips", "check_storyboard", "preview_storyboard", "preview_storyboard_changes", "apply_storyboard_changes",
                    "read_storyboard_assets_context", "submit_storyboard_assets",
                    "create_missing_storyboard_assets",
                    # 资产需求与框图是分镜的相邻层：镜头表、导演台与助手都要能直接读写，
                    # 所以走分镜这一条界面通道，而不是另开白名单。
                    "read_asset_needs", "declare_asset_needs", "read_storyboard_layout",
                    "load_storyboard_camera_presets", "save_storyboard_camera_preset",
                    "read_storyboard_video_settings", "save_storyboard_video_settings", "read_storyboard_video_receipts",
                    "set_storyboard_animation", "render_storyboard_animatic", "attach_storyboard_video", "detach_storyboard_video",
                    "prepare_storyboard_video", "submit_storyboard_video", "refresh_storyboard_video",
                    "generate_storyboard_sketch", "export_storyboard_style_package", "render_storyboard_style",
                    "export_storyboard_script", "export_storyboard_panel", "generate_storyboard_panel", "generate_storyboard_panels",
                }
                if operation not in actions:
                    raise ValueError("未知分镜操作")
                result = getattr(ops, operation)(**normalize(dict(arguments)))
                return {"token": token, "operation": operation, "result": result}
            except (ValueError, OSError, KeyError) as exc:
                return {"token": token, "operation": operation, "error": str(exc)}
            except Exception as exc:
                if operation not in {"check_storyboard", "read_storyboard_usage_tips"}:
                    raise
                # A background diagnostic must never release an unrelated write.
                return {"token": token, "operation": operation, "error": str(exc), "error_type": type(exc).__name__}
        if operation == "read_storyboard_usage_tips":
            def failed(exc):
                return {"token": token, "operation": operation, "error": str(exc), "error_type": type(exc).__name__}

            try:
                path = self._project()
            except Exception as exc:
                self.resultReady.emit("storyboard", failed(exc))
                return

            def read_help():
                try:
                    return work(project_catalog.open_graph_project(path))
                except Exception as exc:
                    return failed(exc)

            try:
                self._submit("storyboard", read_help)
            except Exception as exc:
                self.resultReady.emit("storyboard", failed(exc))
            return
        self._project_task("storyboard", work)

    # ---- 镜头表（逐镜制作驾驶舱） ------------------------------------------------
    # 这一层只做「把已经读得到的数据摆成一行一镜」：字段全部在 Python 侧从
    # read_storyboard / read_asset_needs / read_storyboard_video_receipts 派生，
    # QML 不解析业务结构，只负责画。
    #
    # 为什么是同步槽位：表格与详情都是「点一下就该看见」的只读快照，直接返回
    # {ok, error, ...} 比再走一趟 resultReady 信道更直接；失败也仍是
    # errorOccurred("shot_table", …) + ok:false，绝不把异常抛进 QML 事件循环。
    #
    # 四个产物的口径（只认存储字段，不猜）：
    #   layout（框图）＝该镜有结构化框图记录（shot['layouts']，见 storyboard_layout：
    #                   一帧的机位、主体归一化 bbox、景深、这一帧还缺哪些资产）；
    #   sketch（素描）＝关键帧绑了由图像服务生成的草图（带 sketch_source 回执）；
    #   style（风格）＝关键帧有 style_path；
    #   video（视频）＝镜头已采用视频（selected_video）。
    # 四者互不约束：没有框图也要能看素描状态，所以这里只报事实，不判「解锁」。
    SHOT_TABLE_KIND = "shot_table"

    def _shot_views(self, unit_id: str) -> dict:
        """读一次场次：分镜板 + 缺口视图 + 视频回执 + 场次标题。三个槽位共用同一口径。"""
        ops = project_catalog.open_graph_project(self._project())
        board = ops.read_storyboard(unit_id)
        # 缺口视图是派生结果（asset_needs.gap_view）：需求未落实与引用悬空分开算。
        gaps = ops.read_asset_needs(unit_id)
        receipts = ops.read_storyboard_video_receipts(unit_id).get("receipts") or []
        try:
            unit = ops.read_script_unit_context(unit_id).get("unit") or {}
        except Exception:  # noqa: BLE001 - 标题只是表头，读不到不该拖垮整张表
            unit = {}
        plan = unit.get("plan") or {}
        return {"board": board, "gaps": gaps, "receipts": receipts,
                "title": plan.get("title") or "", "scene_number": unit.get("scene_number") or ""}

    @staticmethod
    def _shot_layouts(shot: dict) -> dict:
        """该镜的框图，按镜内时刻索引。

        两个来源合一：存储里的 shot['layouts']（键是时刻字符串），以及 read_storyboard
        贴在 timeline.keyframes[i].layout 上的读视图——后者只覆盖出现在计划里的时刻，
        所以存储那一份也要看，免得「有计划外的框」被漏报。
        """
        found: dict[int, dict] = {}
        stored = shot.get("layouts")
        if isinstance(stored, dict):
            for key, row in stored.items():
                if isinstance(row, dict):
                    try:
                        found[int(key)] = row
                    except (TypeError, ValueError):
                        continue
        for row in (shot.get("timeline") or {}).get("keyframes") or []:
            if isinstance(row.get("layout"), dict):
                found.setdefault(row.get("offset_ms", 0), row["layout"])
        return found

    @classmethod
    def _shot_products(cls, shot: dict) -> dict:
        frames = shot.get("keyframes") or []
        return {"layout": bool(cls._shot_layouts(shot)),
                "sketch": any(frame.get("sketch_source") for frame in frames),
                "style": any(frame.get("style_path") for frame in frames),
                "video": bool(shot.get("selected_video"))}

    @staticmethod
    def _shot_receipts(shot: dict, video_receipts: list[dict]) -> list[dict]:
        """回执：素描生成记录（关键帧里的 sketch_source）＋该镜的视频服务回执。"""
        rows = []
        for frame in shot.get("keyframes") or []:
            source = frame.get("sketch_source") or {}
            if not source:
                continue
            rows.append({"kind": "sketch", "id": frame.get("id", ""),
                         "offset_ms": frame.get("offset_ms", 0),
                         "provider": source.get("provider", "") or "",
                         "job_id": source.get("job_id", "") or ""})
        for record in video_receipts:
            if record.get("shot_id") != shot.get("id"):
                continue
            rows.append({"kind": "video", "id": record.get("id", ""),
                         "generation_id": record.get("generation_id", "") or "",
                         "offset_ms": 0, "provider": "", "job_id": ""})
        return rows

    @classmethod
    def _shot_gaps(cls, gaps: dict, shot_id: str, *, scope: str = "shot",
                   shot: dict | None = None) -> list[dict]:
        """本镜的缺口清单：镜级需求未落实 + 本镜的引用悬空 + 框图里这一帧还缺的资产。

        场级需求只在本场有引用时才可能落实，混进每一镜的数里会让人以为「每镜都缺」，
        所以单独走 unit_gaps；镜级缺口只认 scope=shot 的两类，外加框图自带的 unmet_needs
        （那是**帧级**需求：某一帧要的人物/物品还没绑卡，同样是没落实）。
        """
        rows = []
        for need in gaps.get("needs") or []:
            if need.get("scope") != scope or need.get("bound_asset_id"):
                continue
            if scope == "shot" and need.get("shot_id") != shot_id:
                continue
            rows.append({"gap": "unresolved", "scope": scope, "shot_id": need.get("shot_id"),
                         "kind": need.get("kind", ""), "name": need.get("name", ""),
                         "variant": need.get("variant", ""), "note": need.get("note", ""),
                         "message": "需求未落实：还没有任何引用把它绑到资产卡上"})
        if scope != "shot":
            return rows
        for record in gaps.get("dangling") or []:
            if record.get("shot_id") != shot_id:
                continue
            rows.append({"gap": "dangling", "scope": "shot", "shot_id": shot_id, "kind": "",
                         "name": record.get("name", ""), "asset_id": record.get("asset_id", ""),
                         "message": record.get("message", "")})
        if shot is not None:
            for offset, layout in sorted(cls._shot_layouts(shot).items()):
                for need in layout.get("unmet_needs") or []:
                    rows.append({"gap": "frame", "scope": "frame", "shot_id": shot_id,
                                 "offset_ms": offset, "kind": need.get("kind", ""),
                                 "name": need.get("name", ""), "variant": need.get("variant", ""),
                                 "note": need.get("note", ""),
                                 "message": f"这一帧（{offset}ms）还缺这张资产卡"})
        return rows

    @classmethod
    def _shot_row(cls, shot: dict, index: int, gaps: list[dict], receipts: list[dict]) -> dict:
        timeline = shot.get("timeline") or {}
        return {"shot_id": shot.get("id", ""), "index": index,
                "title": shot.get("title", ""), "duration_ms": shot.get("duration_ms", 0),
                "summary": shot.get("description", "") or shot.get("shot_language", ""),
                "keyframe_count": len(shot.get("keyframes") or []),
                "segment_count": len(timeline.get("segments") or []),
                "gap_count": len(gaps), "has": cls._shot_products(shot),
                "receipts": len(receipts), "handoff": shot.get("handoff") or None}

    # ---- 标尺上的可拖点：能不能拖、为什么不能，全在 Python 判 ---------------------
    # 标尺要改的是「时刻」，而「时刻的主人」不止一个：静帧、命名时刻、关键帧计划。
    # 谁做主由 storyboard_operations 的既有规则决定（绑了命名时刻的帧改时刻必须走
    # update_marker，直接 move_keyframe 会被拒），所以这里把「拖动会走哪几条路」一并
    # 算给界面：QML 只提交与重画，不重新推断规则——同一口径两处实现迟早不一致。
    #
    # 声音节拍与机位点本轮只显示不可拖：它们各有自己的编辑入口（声音编辑器、机位
    # 关键帧、预演绑定），从标尺上顺手改会绕开那些入口自己的校验。既然不可拖，就把
    # 原因写出来，免得点了没反应、看着像坏了。
    SOUND_CUE_READONLY = "声音节拍由声音编辑器管（镜内时刻与命名时刻的绑定都在那里改），本轮不从标尺拖"
    CAMERA_KEY_READONLY = "机位点由机位关键帧与预演绑定管，本轮不从标尺拖"
    DRAG_APPROVED = "本场已定案：请先解除定案，再改这一镜的时间轴"

    @staticmethod
    def _shot_aspect_ratio(shot: dict) -> str:
        """画幅只认镜头字段（与 storyboard_operations._shot_aspect 同一来源）。

        老工程没有这个字段：返回空串，界面显示「画幅未写」，而不是替它猜一个 16:9。
        """
        for field in ("aspect_ratio", "frame_aspect"):
            value = shot.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _shot_segment_limit(board: dict) -> int:
        """单段上限：场级可配，配坏了只落回默认值——详情面板不是校验器。

        校验器（validate_timeline）会拒掉非法配置；这里要是照抄那套抛错，一个坏配置
        就能让整张镜头表打不开，而详情本来只是「读出来给人看」。
        """
        from .storyboard_operations import DEFAULT_MAX_SEGMENT_MS

        limit = board.get("timeline_max_segment_ms", DEFAULT_MAX_SEGMENT_MS)
        return limit if type(limit) is int and 1 <= limit <= 600000 else DEFAULT_MAX_SEGMENT_MS

    @classmethod
    def _shot_keyframe_points(cls, shot: dict, layouts: dict, *, approved: bool) -> list[dict]:
        """标尺上的关键帧点：时刻、命名、这一刻有没有框图、能不能拖、拖动走哪些路。

        三条路都来自既有规则：
        * 绑了命名时刻的静帧（marker_id）只能经 update_marker 改时刻；
        * 没绑标记的静帧走 move_keyframe；
        * 显式计划行还要一起走 update_timeline_keyframe——只挪静帧的话，标尺是按计划
          画的，那一刻的竖线不会动，用户会以为没保存。
        没有显式计划（计划是从静帧推导的）时就没有计划行可改，只挪静帧。
        """
        duration = shot.get("duration_ms") or 0
        frames: dict[int, dict] = {}
        for frame in shot.get("keyframes") or []:
            frames.setdefault(frame.get("offset_ms", 0), frame)
        plan_explicit = not (shot.get("timeline_derived") or {}).get("keyframes", True)
        rows = []
        for row in (shot.get("timeline") or {}).get("keyframes") or []:
            offset = row.get("offset_ms", 0)
            frame = frames.get(offset) or {}
            marker_id = frame.get("marker_id") or ""
            moves = []
            if frame:
                moves.append("update_marker" if marker_id else "move_keyframe")
            if plan_explicit:
                moves.append("update_timeline_keyframe")
            # 末帧边界：静帧必须落进 [0, duration)（与 timing_conflicts 同一条线），
            # 只有计划行时可以落在时长上（validate_timeline 允许 offset == duration）。
            ceiling = duration - 1 if frame else duration
            reason = cls.DRAG_APPROVED if approved else ""
            rows.append({"offset_ms": offset,
                         "moment_name": row.get("moment_name", ""),
                         "description": row.get("description", ""),
                         "frame_id": frame.get("id", ""),
                         "marker_id": marker_id,
                         "has_layout": offset in layouts,
                         # 站位是"输入"，框图是"结果"：界面要能分开显示，才知道这一帧
                         # 的框图是从预演抓的、从站位推的，还是手工给的。
                         "subjects": row.get("subjects") or [],
                         "draggable": not reason, "reason": reason, "moves": moves,
                         "min_offset_ms": 0, "max_offset_ms": max(0, ceiling)})
        return rows

    @classmethod
    def _shot_segment_rows(cls, board: dict, shot: dict, *, approved: bool
                           ) -> tuple[list[list[int]], list[dict], bool, list[dict]]:
        """段（原始 [start,end] 表）、段的可显示行、段是不是显式存的、每条内部边界的可拖范围。

        段端点必须落在已有关键帧上、且每段不超过单段上限（validate_timeline 的既有
        约束）。所以「把边界挪到别处」只能是「把边界上的那个关键帧挪到别处，并把分段
        表钉成显式」——这里给出每一条边界能落到哪个区间，写的时候两件事一起提交。
        """
        timeline = shot.get("timeline") or {}
        segments = [[int(seg[0]), int(seg[1])] for seg in timeline.get("segments") or []]
        explicit = not (shot.get("timeline_derived") or {}).get("segments", True)
        limit = cls._shot_segment_limit(board)
        anchors = {row.get("offset_ms", 0) for row in timeline.get("keyframes") or []}
        anchors |= {frame.get("offset_ms") for frame in shot.get("keyframes") or []}
        # 显式分段要求每个端点都落在关键帧上（validate_timeline 的既有约束）。推导出来的
        # 分段常把镜头首尾（0 与时长）当端点，而它们未必是关键帧——这时把这张表写成显式
        # 会被拒，所以拖动只能挪走边界上的关键帧，分段继续按关键帧推导。
        storable = bool(segments) and all(seg[0] in anchors for seg in segments) and segments[-1][1] in anchors
        rows = [{"index": index, "start_ms": seg[0], "end_ms": seg[1], "explicit": explicit}
                for index, seg in enumerate(segments)]
        edges = []
        # 只给内部边界：整镜的首尾边界拖开会在镜头开头或结尾留下没人管的一段，
        # 那不是「分段更自由」，而是把时间轴弄出一个谁也读不到的洞。
        for index in range(1, len(segments)):
            left, right, at = segments[index - 1][0], segments[index][1], segments[index][0]
            # 边界不必吸附到现有关键帧：那一刻的关键帧会跟着一起挪，挪完它自己就是端点。
            low = max(left + 1, right - limit)
            high = min(right - 1, left + limit)
            if approved:
                reason = cls.DRAG_APPROVED
            elif low > high:
                reason = f"相邻两段已顶到单段上限{limit}ms：边界往哪边挪都会超限"
            else:
                reason = ""
            becomes_explicit = not explicit and storable
            edges.append({"index": index, "at_ms": at, "min_ms": low, "max_ms": high,
                          "draggable": not reason, "reason": reason,
                          "becomes_explicit": becomes_explicit,
                          "hint": ("拖动会把分段从推导变成显式：之后不再随关键帧自动推导"
                                   if becomes_explicit else
                                   ("拖动会挪走边界上的关键帧，分段仍按关键帧推导" if not explicit else ""))})
        return segments, rows, explicit, edges

    @classmethod
    def _shot_cue_rows(cls, shot: dict) -> list[dict]:
        """声音节拍：时刻、来源（命名时刻／自己的时刻／没时刻）与标签。

        binding 说出「这一刻是谁定的」：绑了命名时刻的节拍会随标记走，改时刻得去声音
        编辑器；没时刻的节拍在尺上画不出来，所以 offset_ms 是 None 而不是 0——
        0 会被画成一个真的落在镜头开头的声音。
        """
        cues = []
        sounds = {row.get("id"): row for row in shot.get("sounds") or []}
        for cue in (shot.get("timeline") or {}).get("sound_cues") or []:
            sound = sounds.get(cue.get("sound_id")) or {}
            if sound.get("marker_id"):
                binding = "marker"
            elif sound.get("time_ms") is not None:
                binding = "own"
            else:
                binding = "none"
            cues.append({"sound_id": cue.get("sound_id", ""), "offset_ms": cue.get("offset_ms"),
                         "binding": binding, "kind": cue.get("kind") or "",
                         "label": cue.get("label") or sound.get("label") or "",
                         "draggable": False, "reason": cls.SOUND_CUE_READONLY})
        return cues

    @classmethod
    def _shot_camera_rows(cls, shot: dict) -> list[dict]:
        return [{"keyframe_id": row.get("keyframe_id", ""), "offset_ms": row.get("offset_ms", 0),
                 "draggable": False, "reason": cls.CAMERA_KEY_READONLY}
                for row in (shot.get("timeline") or {}).get("camera_keys") or []]

    @Slot(str, result="QVariantMap")
    def shotTable(self, unit_id: str) -> dict:
        """一镜一行的表：顺序即镜头顺序，index 从 1 起（人读的镜号）。"""
        try:
            view = self._shot_views(unit_id)
        except Exception as exc:  # noqa: BLE001 - 界面槽位不抛异常，只报 ok:false
            return self._shot_failed(exc)
        board, gaps = view["board"], view["gaps"]
        rows = []
        for index, shot in enumerate(board.get("shots") or [], 1):
            receipts = self._shot_receipts(shot, view["receipts"])
            shot_gaps = self._shot_gaps(gaps, shot.get("id", ""), shot=shot)
            rows.append(self._shot_row(shot, index, shot_gaps, receipts))
        return {"ok": True, "error": "", "unit_id": board.get("unit_id", unit_id),
                "revision": board.get("revision", 0), "title": view["title"],
                "scene_number": view["scene_number"],
                "approved": board.get("approved") is True,
                "total_duration_ms": sum(row["duration_ms"] for row in rows),
                "unit_gap_count": len(self._shot_gaps(gaps, "", scope="unit")),
                "counts": gaps.get("counts") or {},
                "rows": rows}

    @Slot(str, str, result="QVariantMap")
    def shotDetail(self, unit_id: str, shot_id: str) -> dict:
        """选中镜的全部只读细节：时间轴、关键帧计划、需求与绑定、缺口、回执。

        另给标尺拖动要用的三组派生字段：keyframes（可拖点与它们的 moves/上下界）、
        segments/segment_edges（段与内部边界）、sound_cues/camera_keys（只显示不可拖，
        带原因）。能不能拖、为什么不能都在这里判好，界面不重新推断规则。
        """
        try:
            view = self._shot_views(unit_id)
        except Exception as exc:  # noqa: BLE001
            return self._shot_failed(exc)
        board, gaps = view["board"], view["gaps"]
        index = next((number for number, row in enumerate(board.get("shots") or [], 1)
                      if row.get("id") == shot_id), 0)
        if not index:
            message = "该镜头不属于本场，可能已被删除或已换成别的场次；请刷新镜头表"
            self.errorOccurred.emit(self.SHOT_TABLE_KIND, message)
            return {"ok": False, "error": message, "unit_id": board.get("unit_id", unit_id),
                    "revision": board.get("revision", 0), "shot": {}, "timeline": {},
                    "timeline_derived": {}, "handoff": None,
                    "asset_needs": {"unit": [], "shot": []}, "gaps": [], "unit_gaps": [],
                    "receipts": [], "layouts": [], "has": {}}
        shot = next(row for row in board.get("shots") or [] if row.get("id") == shot_id)
        elements = shot.get("asset_elements") or shot.get("visual_elements") or []
        needs = gaps.get("needs") or []
        layouts = self._shot_layouts(shot)
        approved = board.get("approved") is True
        segments, segment_rows, segments_explicit, edges = self._shot_segment_rows(
            board, shot, approved=approved)
        return {"ok": True, "error": "", "unit_id": board.get("unit_id", unit_id),
                "revision": board.get("revision", 0), "title": view["title"],
                "approved": board.get("approved") is True,
                "shot": {"id": shot.get("id", ""), "index": index,
                         "title": shot.get("title", ""),
                         "summary": shot.get("description", "") or shot.get("shot_language", ""),
                         "description": shot.get("description", ""),
                         "shot_language": shot.get("shot_language", ""),
                         "sound_notes": shot.get("sound_notes", ""),
                         "duration_ms": shot.get("duration_ms", 0),
                         "action_start_ms": shot.get("action_start_ms", 0),
                         "source_block_ids": list(shot.get("source_block_ids") or []),
                         "keyframe_count": len(shot.get("keyframes") or []),
                         "visual_elements": [{"kind": row.get("kind", ""), "name": row.get("name", ""),
                                              "variant": row.get("variant", ""),
                                              "usage": row.get("usage", ""),
                                              "asset_id": row.get("asset_id") or "",
                                              "label": row.get("label", "") or row.get("name", ""),
                                              "status": row.get("status", ""),
                                              "message": row.get("message", "")}
                                             for row in elements if isinstance(row, dict)],
                         "asset_elements_status": shot.get("asset_elements_status", "")},
                "timeline": shot.get("timeline") or {},
                "timeline_derived": shot.get("timeline_derived") or {},
                "handoff": shot.get("handoff") or None,
                "asset_needs": {"unit": [row for row in needs if row.get("scope") == "unit"],
                                "shot": [row for row in needs if row.get("shot_id") == shot_id]},
                "gaps": self._shot_gaps(gaps, shot_id, shot=shot),
                "unit_gaps": self._shot_gaps(gaps, "", scope="unit"),
                # 框图按时刻列出来：详情面板据此画「这一帧该有什么」，也是近似预演的输入。
                "layouts": [{"offset_ms": offset, "hash": row.get("hash", ""), "layout": row}
                            for offset, row in sorted(self._shot_layouts(shot).items())],
                "receipts": self._shot_receipts(shot, view["receipts"]),
                "has": self._shot_products(shot),
                # 标尺上要拖的东西：点、段、段边界、节拍。能不能拖与为什么不能都在这里判好，
                # 界面只负责画与提交（一次拖动只提交一次，见页面里的 commitKeyframeDrag）。
                "aspect_ratio": self._shot_aspect_ratio(shot),
                "keyframes": self._shot_keyframe_points(shot, layouts, approved=approved),
                "segments": segment_rows,
                "segments_explicit": segments_explicit,
                "segment_edges": edges,
                "max_segment_ms": self._shot_segment_limit(board),
                "sound_cues": self._shot_cue_rows(shot),
                "camera_keys": self._shot_camera_rows(shot)}

    @Slot(str, str, int, result="QVariantMap")
    def shotLayout(self, unit_id: str, shot_id: str, offset_ms: int) -> dict:
        """该时刻的框图（结构化构图记录），供详情面板显示「有没有框图」。

        框图是 storyboard_layout 那一层数据（机位、主体归一化 bbox、景深、这一帧还缺
        哪些资产），不是草图图片；草图另走 sketch_url，两者一起返回，界面不必再读一次。

        「这一帧还没出框图」是正常状态而不是错误：只返回 ok:false + reason 说明为什么，
        不报 errorOccurred——把它当错误报，用户会以为界面坏了。
        """
        base = {"shot_id": shot_id, "offset_ms": offset_ms, "frame_offset_ms": None,
                "frame_id": "", "hash": "", "layout": {}, "camera": {}, "objects": [],
                "unmet_needs": [], "sketch_path": "", "sketch_url": "",
                "style_path": "", "style_url": ""}
        try:
            ops = project_catalog.open_graph_project(self._project())
            shot = next((row for row in ops.read_storyboard(unit_id).get("shots") or []
                         if row.get("id") == shot_id), None)
        except Exception as exc:  # noqa: BLE001 - 读不到工程/场次才真的算错
            message = self._shot_failed(exc)["error"]
            return base | {"ok": False, "reason": message}
        if shot is None:
            return base | {"ok": False, "reason": "该镜头不属于本场，可能已被删除或已换成别的场次"}
        layouts = self._shot_layouts(shot)
        frames = shot.get("keyframes") or []
        plan = (shot.get("timeline") or {}).get("keyframes") or []
        if not frames and not plan and not layouts:
            return base | {"ok": False, "reason": "本镜还没有任何关键帧，先在分镜稿里排框"}
        # 框图的时刻必须落在关键帧上：显式框位（keyframes）与关键帧计划（timeline）都算。
        # 先认精确时刻，再退到「该时刻正在播的那一帧」，与预演里取当前帧的口径一致。
        offsets = sorted({row.get("offset_ms", 0) for row in frames}
                         | {row.get("offset_ms", 0) for row in plan} | set(layouts))
        chosen = offset_ms if offset_ms in offsets else next(
            (value for value in reversed(offsets) if value <= offset_ms), None)
        if chosen is None:
            return base | {"ok": False,
                           "reason": f"本镜第一帧在 {offsets[0]}ms，比 {offset_ms}ms 还晚，此刻没有帧"}
        frame = next((row for row in frames if row.get("offset_ms") == chosen), None) or {}
        sketch = frame.get("sketch_path") or ""
        style = frame.get("style_path") or ""
        found = {"frame_offset_ms": chosen, "frame_id": frame.get("id", ""),
                 "sketch_path": sketch, "sketch_url": self.storyboardImageUrl(sketch),
                 "style_path": style, "style_url": self.storyboardImageUrl(style)}
        layout = layouts.get(chosen)
        if layout is None:
            return base | found | {"ok": False,
                                   "reason": "这一帧还没有框图：先写下这一帧的站位、机位与景深"}
        return base | found | {"ok": True, "reason": "", "hash": layout.get("hash", ""),
                               "layout": layout, "camera": layout.get("camera") or {},
                               "objects": layout.get("objects") or [],
                               "unmet_needs": layout.get("unmet_needs") or []}

    def _shot_failed(self, exc: Exception) -> dict:
        message = str(exc) or exc.__class__.__name__
        self.errorOccurred.emit(self.SHOT_TABLE_KIND, message)
        return {"ok": False, "error": message, "unit_id": "", "revision": 0, "title": "",
                "scene_number": "", "approved": False, "total_duration_ms": 0,
                "unit_gap_count": 0, "counts": {}, "rows": []}

    # ---- 镜头表：写（拖时刻、拖段边界、产图、预演） -------------------------------
    # 为什么这几个写槽位同步返回、不走 _project_task：一次拖动或一次按钮点击只提交一次，
    # 界面要立刻知道「写没写成、为什么没写成」再决定刷新还是回弹；写成异步回调，
    # 松手后的标尺会先弹回原处再跳过去。写的是本地 sqlite 里的一块 JSON，耗时毫秒级
    # （慢的是产图里的网络请求，见 runGenerationTask 的说明）。
    #
    # 这些操作不进 storyboardAction 的界面白名单：那条通道是分镜页既有的异步契约，
    # 返回 {token, result|error}；把「ok/warnings/output_path」硬塞进去，会让两种
    # 失败语义混在一起。它们各自有专用槽位，失败一律 ok:false + 中文原因。
    def _shot_write_failed(self, exc: Exception, **extra) -> dict:
        """写槽位的统一失败出口：ok:false + 可直接显示的中文原因，并报 errorOccurred。"""
        message = str(exc) or exc.__class__.__name__
        self.errorOccurred.emit(self.SHOT_TABLE_KIND, message)
        return {"ok": False, "error": message, **extra}

    @Slot(str, str, int, int, result="QVariantMap")
    def moveShotKeyframe(self, unit_id: str, shot_id: str, offset_ms: int, new_offset_ms: int) -> dict:
        """把标尺上某一刻的关键帧挪到新时刻；一次拖动只写一次。

        走哪条路由既有规则决定（见 _shot_keyframe_points 的 moves）：绑了命名时刻的帧
        换算成标记时刻写 update_marker，静帧写 move_keyframe，显式计划行写
        update_timeline_keyframe。越界、撞点、已定案都只回 ok:false + 中文原因，
        界面刷新后仍看到原来的时刻——不能出现「拖了但没写进去，画面却动了」。
        """
        try:
            ops = project_catalog.open_graph_project(self._project())
            board = ops.read_storyboard(unit_id)
            shot = next(row for row in board.get("shots") or [] if row.get("id") == shot_id)
        except StopIteration:
            return self._shot_write_failed(ValueError("该镜头不属于本场，可能已被删除或换了场次；请刷新镜头表"))
        except Exception as exc:  # noqa: BLE001 - 写槽位照例不把异常抛进 QML 事件循环
            return self._shot_write_failed(exc)
        approved = board.get("approved") is True
        point = next((row for row in self._shot_keyframe_points(shot, self._shot_layouts(shot), approved=approved)
                      if row["offset_ms"] == offset_ms), None)
        if point is None:
            return self._shot_write_failed(ValueError(
                f"标尺上没有{offset_ms}ms这一刻的关键帧；请刷新镜头表后再拖"))
        if not point["draggable"]:
            return self._shot_write_failed(ValueError(point["reason"]))
        duration = shot.get("duration_ms") or 0
        if not 0 <= new_offset_ms <= point["max_offset_ms"]:
            tail = (f"（这一镜时长{duration}ms，静帧不能落在时长上）" if point["frame_id"] and point["max_offset_ms"] < duration
                    else f"（这一镜时长{duration}ms）")
            return self._shot_write_failed(ValueError(
                f"镜内时刻须在0至{point['max_offset_ms']}ms之间{tail}"))
        if new_offset_ms == offset_ms:
            return {"ok": True, "error": "", "notice": "", "via": [], "unit_id": unit_id,
                    "shot_id": shot_id, "offset_ms": offset_ms, "revision": board.get("revision", 0)}
        taken = {row.get("offset_ms") for row in (shot.get("timeline") or {}).get("keyframes") or []}
        taken |= {frame.get("offset_ms") for frame in shot.get("keyframes") or []}
        if new_offset_ms in taken:
            return self._shot_write_failed(ValueError(
                f"{new_offset_ms}ms 上已经有另一条关键帧：两个点不能并成一个，请先挪开或删掉那一个"))
        changes, marker_id = [], point["marker_id"]
        if point["frame_id"]:
            if marker_id:
                # 既有规则：绑了命名时刻的帧，时刻的主人是那个标记；帧的镜内时刻由
                # resolve_markers 按「标记时刻 − 动作起点」重算，所以这里换算成标记时刻。
                action_start = shot.get("action_start_ms") or 0
                changes.append({"op": "update_marker", "marker_id": marker_id,
                                "time_ms": action_start + new_offset_ms})
            else:
                changes.append({"op": "move_keyframe", "shot_id": shot_id,
                                "frame_id": point["frame_id"], "offset_ms": new_offset_ms})
        if "update_timeline_keyframe" in point["moves"]:
            changes.append({"op": "update_timeline_keyframe", "shot_id": shot_id,
                            "offset_ms": offset_ms, "new_offset_ms": new_offset_ms})
        try:
            written = ops.apply_storyboard_changes(unit_id, board.get("revision", 0), changes)
        except Exception as exc:  # noqa: BLE001
            return self._shot_write_failed(exc)
        marker_name = next((row.get("name", "") for row in written.get("markers") or []
                            if row.get("id") == marker_id), "") if marker_id else ""
        via = "命名时刻" + (f"「{marker_name}」" if marker_name else "") if marker_id else "关键帧"
        return {"ok": True, "error": "",
                "notice": f"已把{offset_ms}ms的帧挪到{new_offset_ms}ms（经{via}）",
                "via": point["moves"], "unit_id": unit_id, "shot_id": shot_id,
                "offset_ms": new_offset_ms, "frame_id": point["frame_id"], "marker_id": marker_id,
                "revision": written.get("revision", 0)}

    @Slot(str, str, int, int, result="QVariantMap")
    def moveShotSegmentEdge(self, unit_id: str, shot_id: str, index: int, new_offset_ms: int) -> dict:
        """把第 index 条段边界挪到新时刻：连带挪边界上的关键帧，并把分段写成显式。

        为什么连带挪关键帧：段端点必须落在已有关键帧上、每段不超过单段上限
        （validate_timeline 的既有约束），所以「边界挪到别处」只能是「那一刻的关键帧
        挪到别处，顺手把分段表钉下来」。推导分段一拖就变成显式表——这正是页面上那句
        提示要说的事（按下时显示一行字，不弹对话框）。
        """
        try:
            ops = project_catalog.open_graph_project(self._project())
            board = ops.read_storyboard(unit_id)
            shot = next(row for row in board.get("shots") or [] if row.get("id") == shot_id)
        except StopIteration:
            return self._shot_write_failed(ValueError("该镜头不属于本场，可能已被删除或换了场次；请刷新镜头表"))
        except Exception as exc:  # noqa: BLE001
            return self._shot_write_failed(exc)
        approved = board.get("approved") is True
        segments, _, explicit, edges = self._shot_segment_rows(board, shot, approved=approved)
        edge = next((row for row in edges if row["index"] == index), None)
        if edge is None:
            return self._shot_write_failed(ValueError("标尺上没有这条段边界（整镜首尾不可拖）；请刷新镜头表后再拖"))
        if not edge["draggable"]:
            return self._shot_write_failed(ValueError(edge["reason"]))
        if not edge["min_ms"] <= new_offset_ms <= edge["max_ms"]:
            return self._shot_write_failed(ValueError(
                f"这条段边界须落在{edge['min_ms']}至{edge['max_ms']}ms之间"
                f"（两侧段长不能超{self._shot_segment_limit(board)}ms，也不能越过相邻边界）"))
        point = next((row for row in self._shot_keyframe_points(
            shot, self._shot_layouts(shot), approved=approved) if row["offset_ms"] == edge["at_ms"]), None)
        if point is None or not point["moves"]:
            return self._shot_write_failed(ValueError(
                f"这条段边界（{edge['at_ms']}ms）上没有可移动的关键帧；请刷新镜头表后再拖"))
        taken = {row.get("offset_ms") for row in (shot.get("timeline") or {}).get("keyframes") or []}
        taken |= {frame.get("offset_ms") for frame in shot.get("keyframes") or []}
        if new_offset_ms in taken:
            return self._shot_write_failed(ValueError(
                f"{new_offset_ms}ms 上已经有另一条关键帧：两个点不能并成一个，请先挪开或删掉那一个"))
        changes = []
        marker_id = point["marker_id"]
        if point["frame_id"]:
            if marker_id:
                action_start = shot.get("action_start_ms") or 0
                changes.append({"op": "update_marker", "marker_id": marker_id,
                                "time_ms": action_start + new_offset_ms})
            else:
                changes.append({"op": "move_keyframe", "shot_id": shot_id,
                                "frame_id": point["frame_id"], "offset_ms": new_offset_ms})
        if "update_timeline_keyframe" in point["moves"]:
            changes.append({"op": "update_timeline_keyframe", "shot_id": shot_id,
                            "offset_ms": edge["at_ms"], "new_offset_ms": new_offset_ms})
        written_segments = []
        if explicit or edge["becomes_explicit"]:
            # 显式分段必须跟着改：只挪关键帧会被「端点必须落在关键帧上」拒掉。
            # 推导分段只有在整张表都能落到关键帧上时才写成显式，否则继续推导。
            written_segments = [list(seg) for seg in segments]
            written_segments[index - 1][1] = new_offset_ms
            written_segments[index][0] = new_offset_ms
            changes.append({"op": "set_timeline_segments", "shot_id": shot_id,
                            "segments": written_segments})
        try:
            written = ops.apply_storyboard_changes(unit_id, board.get("revision", 0), changes)
        except Exception as exc:  # noqa: BLE001
            return self._shot_write_failed(exc)
        notice = f"段边界已挪到{new_offset_ms}ms（边界上的关键帧一起挪了）"
        if edge["becomes_explicit"]:
            notice += "；分段从此变成显式，不再随关键帧自动推导"
        return {"ok": True, "error": "", "notice": notice, "index": index, "at_ms": new_offset_ms,
                "became_explicit": edge["becomes_explicit"], "via": point["moves"],
                "segments": written_segments,
                "unit_id": unit_id, "shot_id": shot_id, "revision": written.get("revision", 0)}

    @Slot(str, str, int, str, str, result="QVariantMap")
    def runGenerationTask(self, unit_id: str, shot_id: str, offset_ms: int, kind: str, prompt: str) -> dict:
        """按技能声明的生成工作流真跑一次生成，并把结果绑回这一帧。

        warnings 原样带回界面：技能里那些这轮没生效的项（workflow.params 没有透传
        通道、工作流文件不存在、视频不读 ComfyUI 工作流）是「配了却没效果」的唯一
        解释，界面必须逐条显示，不能只报一句成功。

        失败一律 ok:false：执行器保证失败不绑定、旧图保留，界面因此不得按「已出图」
        改写产物状态——只要 ok 不是 true，output_path 就当作没有。
        """
        base = {"ok": False, "error": "", "warnings": [], "output_path": "", "bound_to": "",
                "skill": {}, "provider": {}, "unit_id": unit_id, "shot_id": shot_id,
                "offset_ms": offset_ms, "kind": kind, "revision": 0}
        try:
            ops = project_catalog.open_graph_project(self._project())
            board = ops.read_storyboard(unit_id)
            result = ops.run_generation_task(unit_id, board.get("revision", 0), kind,
                                             shot_id, offset_ms, prompt or "")
        except Exception as exc:  # noqa: BLE001 - 技能缺失/输入不合法/服务失败都从这里出去
            message = str(exc) or exc.__class__.__name__
            self.errorOccurred.emit(self.SHOT_TABLE_KIND, message)
            return base | {"error": message}
        return base | {"ok": True, "error": "",
                       "warnings": list(result.get("warnings") or []),
                       "output_path": result.get("output_path") or "",
                       "bound_to": result.get("bound_to", "") or "",
                       "skill": result.get("skill") or {},
                       "provider": result.get("provider") or {},
                       "revision": result.get("storyboard_revision", 0),
                       "started_at": result.get("started_at", ""),
                       "finished_at": result.get("finished_at", "")}

    @Slot(str, result="QVariantMap")
    def readPrevis(self, unit_id: str) -> dict:
        """本场有没有预演、有哪些机位、哪几镜已经绑好。

        「本场还没有预演工程」不算错误（与 shotLayout 同一口径）：它只说明还没把
        .director 放进来，报成错误会让用户以为界面坏了；真读不出来（文件坏、多文件
        没指定场景）才走 errorOccurred。
        """
        base = {"ok": False, "error": "", "hint": "", "available": False, "unit_id": unit_id,
                "scene_id": "", "name": "", "duration_s": 0, "fps": 0, "aspect": "",
                "cameras": [], "bindings": [], "files": [], "path": ""}
        try:
            ops = project_catalog.open_graph_project(self._project())
        except Exception as exc:  # noqa: BLE001
            # base 里 error 是空串：先摊 base 再盖失败的 error，否则原因会被空串盖掉。
            return base | self._shot_write_failed(exc)
        try:
            info = ops.read_previs(unit_id)
        except Exception as exc:  # noqa: BLE001
            message = str(exc) or exc.__class__.__name__
            if message.startswith("本场还没有预演工程"):
                return base | {"error": message,
                               "hint": "把 DirectorDesk 导出的 .director 放进本场预演目录，再来绑机位"}
            return base | self._shot_write_failed(exc)
        return base | {"ok": True, "available": True, "error": "",
                       "scene_id": info.get("scene_id", "") or "",
                       "name": info.get("name", "") or "",
                       "duration_s": info.get("duration_s", 0) or 0,
                       "fps": info.get("fps", 0) or 0,
                       "aspect": info.get("aspect", "") or "",
                       "path": info.get("path", "") or "",
                       "files": info.get("files") or [],
                       "cameras": [{"camera_id": row.get("camera_id", ""),
                                    "name": row.get("name", ""),
                                    "focal": row.get("focal", 0)}
                                   for row in info.get("cameras") or []],
                       "bindings": info.get("bindings") or []}

    @Slot(str, str, int, bool, result="QVariantMap")
    def capturePrevisLayout(self, unit_id: str, shot_id: str, offset_ms: int,
                           overwrite: bool = False) -> dict:
        """按这一镜绑定的预演机位，把镜内 offset_ms 投影成框图写进去。

        「还没绑机位」「预演目录里没有工程」「这一帧已有手工框图」都不是程序故障，
        而是需要用户做一件具体事的状态：所以除执行器原样的中文原因，还给一句 hint
        （去预演页绑定／先放 .director／确认覆盖），界面照着说，用户照着做。
        """
        base = {"ok": False, "error": "", "hint": "", "unit_id": unit_id, "shot_id": shot_id,
                "offset_ms": offset_ms, "revision": 0, "camera_id": "", "scene_id": "",
                "time_ms": 0, "framing": "", "hash": "", "projected": 0}
        try:
            ops = project_catalog.open_graph_project(self._project())
            result = ops.capture_previs_layout(unit_id, shot_id, offset_ms, overwrite)
        except Exception as exc:  # noqa: BLE001
            message = str(exc) or exc.__class__.__name__
            self.errorOccurred.emit(self.SHOT_TABLE_KIND, message)
            return base | {"error": message, "hint": self._previs_hint(message)}
        layout = result.get("layout") or {}
        return base | {"ok": True, "error": "",
                       "revision": result.get("revision", 0),
                       "camera_id": result.get("camera_id", "") or "",
                       "scene_id": result.get("scene_id", "") or "",
                       "time_ms": result.get("time_ms", 0) or 0,
                       "framing": result.get("framing", "") or "",
                       "hash": layout.get("hash", "") or "",
                       "projected": len(result.get("projected") or []),
                       "notice": f"已按预演机位写下{offset_ms}ms的框图"
                                 f"（{result.get('framing') or '未判景别'}，"
                                 f"{len(result.get('projected') or [])}个主体）"}

    @staticmethod
    def _previs_hint(message: str) -> str:
        """把执行器的原因翻成「下一步做什么」；认不出的原因不硬编指引。"""
        table = (("还没有绑定预演机位", "去预演页把这一镜绑到某台机位（选中机位后绑定这一镜），回来再出框图"),
                 ("本场还没有预演工程", "先把 DirectorDesk 导出的 .director 放进本场预演目录，再去预演页绑机位"),
                 ("已有手工框图", "这一帧的手工框图不会被机器盖掉：确认要换成预演投影时再点一次「覆盖」"),
                 ("没有关键帧", "先在分镜稿里给这一刻建关键帧，再回来出框图"))
        return next((hint for key, hint in table if key in message), "")

    @Slot(str, str, "QVariant", "QVariant", "QVariant", result="QVariantMap")
    def setShotPrevis(self, unit_id: str, shot_id: str, camera_id, scene_id, time_offset_ms) -> dict:
        """把这一镜绑到预演里的某台机位；camera_id 传 null/空串即解除绑定。

        只写 shot.previs，不动框图与时间轴；机位是否真在预演场景里由执行器核对，
        免得存进一个点开就报错的映射（这条校验不在这里重复实现）。
        """
        camera = camera_id if isinstance(camera_id, str) and camera_id.strip() else None
        scene = scene_id if isinstance(scene_id, str) and scene_id.strip() else None
        try:
            offset = None if time_offset_ms is None else int(time_offset_ms)
        except (TypeError, ValueError):
            return self._shot_write_failed(ValueError("time_offset_ms 须为大于等于0的整数毫秒"))
        base = {"ok": False, "error": "", "unit_id": unit_id, "shot_id": shot_id,
                "camera_id": camera or "", "scene_id": scene or "", "revision": 0, "previs": {}}
        try:
            ops = project_catalog.open_graph_project(self._project())
            board = ops.read_storyboard(unit_id)
            written = ops.set_shot_previs(unit_id, board.get("revision", 0), shot_id,
                                          camera_id=camera, scene_id=scene,
                                          time_offset_ms=offset)
        except Exception as exc:  # noqa: BLE001
            return base | self._shot_write_failed(exc)
        previs = written.get("previs") or {}
        return base | {"ok": True, "error": "", "revision": written.get("revision", 0),
                       "previs": previs,
                       "notice": (f"已把这一镜绑到预演机位{previs.get('camera_id', '')}"
                                  f"（镜内0ms＝预演{previs.get('time_offset_ms', 0)}ms）"
                                  if previs else "已解除这一镜的预演机位绑定")}

    @Slot("QVariantMap", result=str)
    def storyboardSelectionLabel(self, selection: dict) -> str:
        """把「镜头表定位预演」的选择记录摊成一行回执。

        预演页不接场景参数（它自己从预演工程里列场景），所以这行文字是唯一的定位
        回执：没有它，用户点了「预演」看到的还是上一次的场景，会以为定位没生效。
        缺 unit_id/shot_id 就返回空串——没有定位时不留一条空条。
        """
        data = selection if isinstance(selection, dict) else {}
        unit, shot = data.get("unit_id") or "", data.get("shot_id") or ""
        if not unit or not shot:
            return ""
        scene = data.get("unit_label") or unit
        index = data.get("shot_index") or 0
        label = f"第 {index} 镜" if index else shot
        title = f"（{data['shot_title']}）" if data.get("shot_title") else ""
        return f"当前定位：{scene} / {label}{title} @ {data.get('offset_ms') or 0}ms"

    # ---- 三维预演台（DirectorDesk 构建产物） -------------------------------------
    # 产物是 ES module，只能走本地 http；这里给出地址并接管它的“保存/导出”下载。
    @Slot(result="QVariantMap")
    def previsWorkspace(self) -> dict:
        from . import previs_workspace

        workspace = self._project_path.parent if self._project_path is not None else None
        ok, detail = previs_workspace.availability(workspace)
        if not ok:
            return {"available": False, "reason": detail, "url": ""}
        try:
            server = previs_workspace.shared_server(workspace)
        except OSError as exc:
            return {"available": False, "reason": f"预演台服务启动失败：{exc}", "url": ""}
        if server is None:
            return {"available": False, "reason": detail, "url": ""}
        self._install_previs_downloads()
        return {"available": True, "reason": "", "url": server.url,
                "download_dir": str(self._previs_download_dir())}

    def _previs_system_dark(self) -> bool:
        return self._appearance_dark

    @Slot(result=str)
    def previsThemeScript(self) -> str:
        """预演台的换肤脚本：把它的中性灰映射到我们的深色令牌。

        只改颜色不改结构；3D 视口里的画面不染色（用户要靠它判断光线）。
        """
        from . import previs_theme, previs_workspace

        workspace = self._project_path.parent if self._project_path is not None else None
        dist = previs_workspace.dist_dir(workspace)
        if dist is None:
            return ""
        dark = self._appearance_dark
        cached = getattr(self, "_previs_theme_cache", None)
        stamp = (dist, dark)
        if cached is None or self._previs_theme_stamp != stamp:
            self._previs_theme_cache = previs_theme.inject_script(previs_theme.themed_css(dist, dark))
            self._previs_theme_stamp = stamp
        return self._previs_theme_cache

    def _previs_download_dir(self) -> Path:
        base = self._project_path.parent if self._project_path is not None else Path.cwd() / "data"
        target = base / "previs"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _install_previs_downloads(self) -> None:
        """把它的浏览器下载落到工程目录：不伪造桌面桥，也不弹系统保存对话框。"""
        if getattr(self, "_previs_downloads_ready", False):
            return
        try:
            from PySide6.QtWebEngineCore import QWebEngineProfile
        except Exception:  # noqa: BLE001 - 没有 WebEngine 就没有这一步
            return
        profile = QWebEngineProfile.defaultProfile()

        def accept(item) -> None:
            directory = self._previs_download_dir()
            item.setDownloadDirectory(str(directory))
            name = item.suggestedFileName() or "download.bin"
            item.setDownloadFileName(name)
            item.accept()
            self.noticeRaised.emit(f"预演台已保存：{directory / name}")

        profile.downloadRequested.connect(accept)
        self._previs_downloads_ready = True

    # ---- 预演页工具条：可读性开关与铺布景 ---------------------------------------
    # 这两件事原先只能绕到镜头表或助手去做。它们不该各长一条工具通道（工具是给模型
    # 用的，会进审计），所以直接走同一份页面接口；界面只负责画开关与回执。

    # 可读性四项与页面 setReadability 的键一一对应，顺序也按“人先看什么”排。
    PREVIS_READABILITY_FLAGS = ("persons", "roomOutlines", "kindColors", "labels")

    def _previs_readability(self) -> dict:
        """四项可读性开关的宿主侧记录（缺省全开）。

        为什么宿主自己记一份：接口 v1 只有 ``setReadability``，没有取值方法——页面
        不回读，工具条就没有「现在是什么状态」可画。所以记最后一次成功下发的值；
        页面回执里带了状态就以页面为准（页面自己动过也不算错）。
        """
        cached = getattr(self, "_previs_readability_flags", None)
        if not isinstance(cached, dict):
            cached = {key: True for key in self.PREVIS_READABILITY_FLAGS}
            self._previs_readability_flags = cached
        return cached

    def _previs_page_call(self, method: str, payload: dict) -> dict:
        """调一次预演页的方法体，失败一律变成可读原因（不抛到界面）。

        为什么不走工具层：工具条点一下开关只是「顺手改一下页面」，不该为此铺一条
        写审计的工具通道。为什么直接摸调用器：``PrevisHost`` 只给契约方法各包了一层，
        ``setReadability`` 还没有包装，而``previs_host.py`` 不归这一路改——假宿主（测试
        与将来换实现）暴露 ``call``，真宿主用 ``_call``；两种都认，免得为一个开关去改
        别人的文件。
        """
        from . import previs_host

        host = previs_host.installed()
        if host is None:
            raise previs_host.PrevisPageNotReady(
                "预演页还没就绪：先在界面里打开本场预演台，页面加载完成后这个开关才起作用")
        direct = getattr(host, "call", None)
        try:
            reply = direct(method, payload) if callable(direct) else host._call(method, payload)
        except previs_host.PrevisHostError:
            raise  # 超时/未就绪已经区分好了，不再套一层
        except Exception as exc:  # noqa: BLE001 - 调用器什么异常都可能抛
            raise previs_host.PrevisHostError(f"调用预演页 {method} 失败：{exc}") from exc
        return reply if isinstance(reply, dict) else {}

    @Slot(result="QVariantMap")
    def previsReadability(self) -> dict:
        """工具条画四个开关用的当前状态（纯读宿主记录，不碰页面）。

        页面未就绪时不报错：开关的默认值（全开）本身就是可画的状态，界面的禁用
        由 ``root.ready`` 决定，不需要这里再造一份失败语义。
        """
        flags = self._previs_readability()
        return {**{key: bool(flags.get(key, True)) for key in self.PREVIS_READABILITY_FLAGS},
                "ok": True, "error": ""}

    @Slot("QVariantMap", result="QVariantMap")
    def setPrevisReadability(self, flags) -> dict:
        """把四项可读性开关下发给预演页，页面自己重绘。

        只接受这四项里的键：多给的键忽略（不认识的开关发过去，页面只会静默丢掉，
        界面却以为改成功了）。失败返回 ``ok:false`` + 中文原因，绝不抛——工具条的
        开关点坏了只是「没改成」，就地显示一句就够，抛异常会打断 QML 的点击处理，
        用户连提示都看不到。
        """
        from . import previs_host

        current = self._previs_readability()
        wanted = {key: bool(current.get(key, True)) for key in self.PREVIS_READABILITY_FLAGS}
        requested = flags if isinstance(flags, dict) else {}
        touched = False
        for key in self.PREVIS_READABILITY_FLAGS:
            if key in requested:
                wanted[key] = bool(requested[key])
                touched = True
        if not touched:
            return {"ok": False, "applied": dict(wanted),
                    "error": "没有要改的开关：可读性只有 persons / roomOutlines / "
                             "kindColors / labels 四项，传参里一个都没有"}
        try:
            reply = self._previs_page_call("setReadability", dict(wanted))
        except previs_host.PrevisHostError as exc:
            return {"ok": False, "applied": dict(current), "error": str(exc)}
        if reply.get("ok") is False:
            return {"ok": False, "applied": dict(current),
                    "error": str(reply.get("error") or "预演页没有接受这次可读性设置")}
        # 页面回执里带了状态（state / readability）就以页面为准：它可能顺手归一化过。
        page_state = reply.get("state")
        if not isinstance(page_state, dict):
            page_state = reply.get("readability")
        if isinstance(page_state, dict):
            wanted = {key: bool(page_state.get(key, wanted[key]))
                      for key in self.PREVIS_READABILITY_FLAGS}
        self._previs_readability_flags = dict(wanted)
        return {"ok": True, "applied": dict(wanted), "error": ""}

    @Slot(result="QVariantMap")
    def previsSceneSet(self) -> dict:
        """工具条上「这页是哪一场」的摘要：场景名、盒体数、机位数、房间、页面修订号。

        为什么读页面而不是读 ``.director``：工具条要显示的是**预演页上正在编的这一场**，
        人工拖过的盒子还没导出过文件，读文件会给出对不上的旧快照。为什么用 ``get_scene``
        而不是 ``snapshot``：它只读、不落盘——工具条会随时问一句，不该为看一眼场景
        就往工程目录写快照文件。
        """
        from . import previs_host

        base = {"ok": False, "error": "", "scene_id": "", "name": "",
                "entity_count": 0, "camera_count": 0, "room": {}, "revision": 0}
        host = previs_host.installed()
        if host is None:
            return base | {"error": "预演页还没就绪：先在界面里打开本场预演台，"
                                    "页面加载完成后这里会显示当前场景"}
        try:
            raw = host.get_scene()
            scene = previs_host.scene_from_page("", raw)
        except Exception as exc:  # noqa: BLE001 - 页面/调用器什么异常都可能抛
            return base | {"error": str(exc) or exc.__class__.__name__}
        return base | {"ok": True, "error": "",
                       "scene_id": scene.get("scene_id", "") or "",
                       "name": scene.get("name", "") or "",
                       # 盒体与机位分开数：页面里机位也是实体，混着数会让人以为多放了箱子。
                       "entity_count": len(scene.get("entities") or []),
                       "camera_count": len(scene.get("cameras") or []),
                       "room": scene.get("room") or {},
                       "revision": int(scene.get("page_revision") or 0)}

    @Slot(str, result="QVariantMap")
    def applySceneSet(self, unit_id: str) -> dict:
        """把这一场绑定的布景（地点资产）铺进预演页。

        只做「取绑定 → 调铺场工具」的接线：布景怎么算、怎么批量写都归工具层
        （``read_unit_set`` / ``apply_location_set``），界面里不该长第二套布景逻辑。
        默认不删多余实体（工具默认 ``overwrite=False``）：预演页上人手工摆的东西，
        工具条一下点击就删掉不合适。
        失败只回 ``ok:false`` + 中文原因（就地显示，不弹窗），也不发 errorOccurred：
        这不是程序故障，而是「这一场还没绑布景」这类需要用户做一件具体事的状态。
        """
        base = {"ok": False, "error": "", "unit_id": unit_id, "asset_id": "",
                "blocks": [], "cameras": [], "revision": 0}
        unit = (unit_id or "").strip()
        if not unit:
            return base | {"error": "还没有当前场：先在镜头表里选一镜点「预演」，"
                                    "或从分镜稿打开本场预演页"}
        try:
            ops = project_catalog.open_graph_project(self._project())
        except Exception as exc:  # noqa: BLE001
            return base | {"error": str(exc) or exc.__class__.__name__}
        read_binding = getattr(ops, "read_unit_set", None)
        apply_set = getattr(ops, "apply_location_set", None)
        if not callable(read_binding) or not callable(apply_set):
            # 正常安装里这两个方法就在 Operations 上（场 ↔ 地点资产的绑定与铺场）。
            # 取不到只说明跑的程序版本与界面不一致，不在这里自己实现布景逻辑。
            return base | {"error": "这个程序里没有布景铺放能力（read_unit_set / apply_location_set）："
                                    "界面与工程库的版本可能不一致，请用同一版程序重新打开工程"}
        try:
            binding = read_binding(unit) or {}
        except Exception as exc:  # noqa: BLE001 - 未绑布景/资产丢了都是可读的 ValueError
            return base | {"error": str(exc) or exc.__class__.__name__}
        status = str(binding.get("status") or "")
        if status in {"unbound", "no_set", "missing_asset"}:
            # 工具已经给了可显示的整句（含下一步点哪），原样带出去，不再另写措辞。
            return base | {"error": str(binding.get("message") or "这一场还没有绑定布景"),
                           "asset_id": str(binding.get("asset_id") or "")}
        try:
            result = apply_set(unit) or {}
        except Exception as exc:  # noqa: BLE001
            return base | {"error": str(exc) or exc.__class__.__name__}
        blocks = result.get("blocks") or []
        cameras = result.get("cameras") or []
        return base | {"ok": True, "error": "",
                       "asset_id": str(result.get("asset_id") or binding.get("asset_id") or ""),
                       "blocks": blocks, "cameras": cameras,
                       "revision": result.get("revision", 0) or 0}

    @Slot(str, str, str)
    def saveScreenplayExport(self, screenplay_id: str, export_format: str, path: str) -> None:
        def work(ops):
            from .node_material import _as_path
            destination = _as_path(path)
            return ops.compile_screenplay(screenplay_id, format=export_format, path=str(destination))
        self._project_task("screenplay_export_saved", work)

    @Slot("QVariantMap")
    def loadAdaptationBrowse(self, spec: dict) -> None:
        self._project_task("adaptation_browse", lambda ops: ops.adaptation_browse_view(**dict(spec)))

    @Slot(str, str)
    def loadAdaptationDetail(self, node_id: str, cut: str) -> None:
        self._project_task("adaptation_detail", lambda ops: ops.adaptation_node_detail(node_id, cut=cut or None))

    @Slot(str, str)
    def loadAdaptationProperties(self, node_id: str, cut: str) -> None:
        self._project_task("adaptation_node_properties", lambda ops: ops.adaptation_node_properties(node_id, cut=cut or None))

    @Slot(str, str)
    def loadAdaptationEditor(self, node_id: str, cut: str) -> None:
        self._project_task("adaptation_node_editor", lambda ops: ops.adaptation_node_editor(node_id, cut=cut or None))

    @Slot(str, str)
    def loadAdaptationMaterial(self, node_id: str, cut: str) -> None:
        self._project_task("adaptation_material", lambda ops: ops.adaptation_node_assets(node_id, cut=cut or None))

    @Slot()
    def loadAdaptationImportCandidates(self) -> None:
        # 带上改编修订号：导入是改编侧的写操作，界面若沿用提取图的修订号会直接被拒
        # （『改编修订已变化，请刷新后重试』），用户看到的就是『点了没反应』。
        self._project_task("adaptation_import_candidates",
                           lambda ops: {"moments": ops.import_adaptation_candidates(),
                                        "revision": ops.adaptation_snapshot().get("revision")})

    @Slot("QVariantList", int)
    def importAdaptation(self, moment_ids: list, revision: int) -> None:
        self._project_task("adaptation", lambda ops: ops.import_adaptation(
            moment_ids, expected_revision=revision, reason="从提取导入"))

    @Slot(str, "QVariantMap")
    def changeAdaptation(self, action: str, arguments: dict) -> None:
        methods = {
            "sync": "sync_adaptation", "edit": "edit_adaptation_node",
            "copy": "copy_adaptation_node", "move": "move_adaptation_node",
            "suppress": "suppress_adaptation_node", "property": "set_adaptation_property",
            "delete_property_changes": "delete_adaptation_property_changes",
        }
        if action not in methods:
            self.errorOccurred.emit("adaptation", "未知改编操作")
            return
        args = dict(arguments)
        if args.get("expected_revision") is not None:
            args["expected_revision"] = int(args["expected_revision"])
        self._project_task("adaptation", lambda ops: getattr(ops, methods[action])(**args))

    @Slot()
    def checkAdaptation(self) -> None:
        self._project_task("adaptation_check", lambda ops: ops.check_adaptation())

    @Slot(str, str, str)
    @Slot(str, str, str, str)
    def loadAssets(self, category: str, moment: str, node_id: str, domain: str = "adaptation") -> None:
        def work(ops):
            result = ops.list_assets(category, moment=moment, node_id=node_id, domain=domain)
            snapshot = ops.adaptation_snapshot() if domain == "adaptation" else ops.graph_snapshot()
            return result | {"moments": ops.asset_moments(domain=domain),
                             "nodes": snapshot.get("nodes", [])}
        self._project_task("assets", work)

    @Slot("QVariantMap", int)
    def saveAsset(self, payload: dict, revision: int) -> None:
        self._project_task("asset_saved", lambda ops: {
            "asset": ops.save_asset(dict(payload), expected_revision=revision)})

    @Slot("QVariantMap", int, int)
    def saveStoryboardElementAsset(self, payload: dict, revision: int, token: int) -> None:
        project_path = str(self._project_path or "")
        frozen = json.loads(json.dumps(dict(payload), ensure_ascii=False))

        def work(ops):
            result = {"token": token, "project_path": project_path}
            try:
                return result | {"asset": ops.save_asset(frozen, expected_revision=revision)}
            except Exception as exc:
                return result | {"error": str(exc), "error_type": type(exc).__name__}

        self._project_task("storyboard_element_asset", work)

    @Slot(str)
    def loadAsset(self, asset_id: str) -> None:
        self._project_task("asset_detail", lambda ops: {"asset": ops.get_asset(asset_id)})

    @Slot(str, int)
    def deleteAsset(self, asset_id: str, revision: int) -> None:
        self._project_task("asset_saved", lambda ops: ops.delete_asset(
            asset_id, expected_revision=revision))

    def _material_moments(self, ops) -> dict:
        """生效/失效下拉用的分级阅读位置：一列一层（大事件 → 场景 → 事件）。

        层级表在 `browse_view.MOMENT_LEVELS`，父子关系看谁把谁列在 `events` 里。
        带上当前阅读位置和属性取值：大事件的「走到哪了」
        由已经读过的场景那句话攒出来——同一份数据，读者停在哪就显示到哪。
        """
        return moment_tree(
            ops.graph_snapshot(),
            cut_event_id=self._state_cut,
            records=ops.property_records(),
        )

    def _material_payload(self, ops, node_id: str) -> dict:
        return ops.material_view(node_id, cut=self._state_cut) | {
            "moments": self._material_moments(ops)
        }

    @staticmethod
    def _images_from_json(text: str) -> list[dict]:
        """编辑器传上来的图片清单：已有的带 id，新加的带 path。"""
        raw = (text or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"图片清单不是合法 JSON：{exc.msg}") from exc
        if not isinstance(parsed, list):
            raise ValueError("图片清单应该是一个数组")
        return [item for item in parsed if isinstance(item, dict)]

    def _material_task(self, node_id: str, change) -> None:
        """改一次资料，然后把这一刻的整页发回去：界面只认一种 payload。"""

        def work(ops) -> dict:
            changed = change(ops)
            if isinstance(changed, dict) and changed.get("status") == "staged":
                return changed
            payload = self._material_payload(ops, node_id)
            if isinstance(changed, dict) and changed.get("submission_id"):
                for key in ("status", "submission_id", "submission_revision", "operation", "target", "base_revisions", "validation_status", "problems"):
                    if key in changed:
                        payload[key] = changed[key]
            # 刚改的那张如果不在当前阅读位置上，界面要说一声——否则像是没保存。
            if isinstance(changed, dict) and changed.get("id"):
                payload["changed_id"] = changed["id"]
                payload["changed_visible"] = holds_at(changed, self._state_cut, snapshot=ops.graph_snapshot())
            # 删资料/删图时磁盘上可能还占着（Windows 不放正被显示的文件）。删不掉的路径
            # 必须一起发回去：界面要能说"下次打开会再试"，而不是让人以为已经删干净。
            if isinstance(changed, dict) and changed.get("leftovers"):
                payload["leftovers"] = list(changed["leftovers"])
            return payload

        self._project_task("material", work)

    @Slot(str)
    def loadMaterial(self, node_id: str) -> None:
        """这个节点在当前阅读位置上有效的资料卡。"""

        def work(ops) -> dict:
            return self._material_payload(ops, node_id)

        self._project_task("material", work)

    @Slot(str, str, str, str, str, str)
    def addMaterialCard(
        self,
        node_id: str,
        title: str,
        detail: str,
        valid_from: str,
        valid_to: str,
        images_json: str,
    ) -> None:
        images = self._images_from_json(images_json)
        self._material_task(
            node_id,
            lambda ops: ops.material_add_card(
                node_id, title, detail, valid_from, valid_to, images
            ),
        )

    @Slot(str, str, str, str, str, str, str)
    def updateMaterialCard(
        self,
        node_id: str,
        card_id: str,
        title: str,
        detail: str,
        valid_from: str,
        valid_to: str,
        images_json: str,
    ) -> None:
        images = self._images_from_json(images_json)
        self._material_task(
            node_id,
            lambda ops: ops.material_update_card(
                node_id,
                card_id,
                title=title,
                detail=detail,
                valid_from=valid_from,
                valid_to=valid_to,
                images=images,
            ),
        )

    @Slot(str, str)
    def removeMaterialCard(self, node_id: str, card_id: str) -> None:
        self._material_task(node_id, lambda ops: ops.material_remove_card(node_id, card_id))

    @Slot(str, str, str, str)
    def addMaterialImage(self, node_id: str, card_id: str, path: str, caption: str) -> None:
        self._material_task(
            node_id, lambda ops: ops.material_add_image(node_id, card_id, Path(path), caption)
        )

    @Slot(str, str, str, str)
    def updateMaterialImageCaption(
        self, node_id: str, card_id: str, image_id: str, caption: str
    ) -> None:
        self._material_task(
            node_id,
            lambda ops: ops.material_update_image_caption(node_id, card_id, image_id, caption),
        )

    @Slot(str, str, str)
    def removeMaterialImage(self, node_id: str, card_id: str, image_id: str) -> None:
        self._material_task(
            node_id, lambda ops: ops.material_remove_image(node_id, card_id, image_id)
        )

    @Slot(str, "QVariantList")
    def searchNodes(self, query: str, types: list) -> None:
        requested = [str(item) for item in types]
        if not set(requested) <= _NODE_TYPES:
            self.errorOccurred.emit("search", "节点类型筛选无效")
            return

        def work(ops) -> dict:
            view = ops.graph_query(query=query, limit=2000, disclosure_cut=self._reading_cut or None)
            return search_result(view, ops.graph_display_map()["items"], requested)
    @Slot(str)
    def nodeDetail(self, node_id: str) -> None:
        self._detail_node = node_id
        cut = dict(self._reading_cut)
        def work(ops) -> dict:
            from .graph_event_order import _storyline_spans

            detail = node_detail(
                ops.graph_query(node_id=node_id, limit=2000, disclosure_cut=cut or None), node_id,
                ops.graph_display_map()["items"], ops.sources(),
            )
            # 大事件是一段：把它的起止事件附上，页面上才看得出它串了哪几场。
            snapshot = ops.graph_snapshot()
            spans = {row["id"]: row for row in _storyline_spans(
                snapshot, "canon", cut or None
            )}
            span = spans.get(node_id)
            detail["span"] = span
            return detail

        self._project_task("detail", work)

    # -- 常驻协作者 ------------------------------------------------------

    def _close_assistant(self) -> None:
        with self._assistant_lock:
            for job in self._assistant_jobs.values():
                job["stop_reason"] = "project_cancelled"
                job["cancel"].set()
            self._assistant_jobs.clear()
            runtime = self._assistant_runtime
            self._assistant_runtime = None
            self._assistant_runner = None
            self._assistant_tool_host = None
            self._assistant_steps = []
            self._assistant_resumed = set()
            self._assistant_chat = ""
        if runtime is not None:
            runtime.close("project_closed")

    def _attach_conversation_runtime(self, ops, settings, api_key, expected_path):
        from .conversation_runtime import get_runtime

        runtime = get_runtime(ops, settings, api_key, runner_factory=HarnessRunner)
        with self._assistant_lock:
            cancelled = self._closed or self._project_path != expected_path
            if not cancelled:
                self._assistant_runtime = runtime
                self._assistant_runner = runtime.runner
                self._assistant_tool_host = runtime.host
                self._assistant_resumed = runtime._resumed
        if cancelled:
            runtime.close("project_closed")
            raise ValueError("工程已切换，任务已停止")
        return runtime

    def _clear_closed_conversation(self, runtime) -> None:
        with self._assistant_lock:
            if self._assistant_runtime is runtime and runtime.closed:
                self._assistant_runtime = None
                self._assistant_runner = None
                self._assistant_tool_host = None
                self._assistant_resumed = set()
                self._assistant_steps = []

    def _assistant_activity_snapshot(self):
        with self._assistant_lock:
            self._assistant_activity_sequence += 1
            return {'active_tasks': {job['chat_id']: job['task_id'] for job in self._assistant_jobs.values()
                                     if job.get('chat_id') and not job.get('finished')},
                    'activity_sequence': self._assistant_activity_sequence,
                    'project_path': str(self._project_path or '')}

    @Slot()
    def loadAssistantActivity(self):
        if not self._closed:
            self.resultReady.emit('assistant_activity', self._assistant_activity_snapshot())

    def _conversation_event(self, ops, expected_path, event: dict, *, busy=True):
        if self._closed or self._project_path != expected_path:
            return
        if event.get("event") == "usage_update":
            statistics = event.get("usage_statistics")
            if isinstance(statistics, dict):
                self.resultReady.emit("assistant_usage", {
                    **statistics,
                    "project_path": str(project_package.layout(expected_path).entry),
                })
            return
        store = self._chats(ops)
        chat_id = event.get("chat_id")
        if not chat_id:
            chat = store.active(create=False)
            chat_id = chat["id"] if chat else ""
        if not chat_id:
            return
        self.resultReady.emit("conversation_progress", {
            **event, **self._assistant_activity_snapshot(), "chat_id": chat_id, "chat": store.load(chat_id), "items": store.list(),
            "current": self._assistant_chat, "project_path": str(project_package.layout(expected_path).entry),
            "busy": busy,
        })

    def _chats(self, ops):
        from .chat_store import ChatStore

        return ChatStore(Path(ops.project_info().path))

    @Slot(str, str)
    def loadUsageStatistics(self, chat_id: str, task_id: str = "") -> None:
        if self._closed or self._project_path is None or not chat_id:
            return
        def work(ops) -> dict:
            return {
                **ops.read_usage_statistics(chat_id=chat_id or None, task_id=task_id or None),
                "project_path": str(project_package.layout(ops.project_info().path).entry),
            }

        self._project_task("assistant_usage", work)

    @Slot(str)
    def loadChatAccess(self, chat_id: str) -> None:
        if self._closed or self._project_path is None:
            return
        def work(ops):
            identity = {"operation": "read", "chat_id": chat_id or None, "project_path": str(project_package.layout(ops.project_info().path).entry)}
            try:
                return ops.read_assistant_access(chat_id or None) | identity
            except (ValueError, OSError) as exc:
                return identity | {"error": str(exc)}
        self._project_task("chat_access", work)

    @Slot(str, str)
    def setChatAccessMode(self, chat_id: str, access_mode: str) -> None:
        if self._closed or self._project_path is None:
            return
        def work(ops):
            identity = {"operation": "set", "chat_id": chat_id, "project_path": str(project_package.layout(ops.project_info().path).entry)}
            try:
                return ops.set_assistant_access(chat_id, access_mode) | identity
            except (ValueError, OSError) as exc:
                return identity | {"error": str(exc)}
        self._project_task("chat_access", work)

    @Slot()
    def loadChats(self) -> None:
        """会话清单：最近说过的排在前面，外加「现在是哪一个」。"""

        def work(ops) -> dict:
            store = self._chats(ops)
            chat = store.active(create=False)
            self._assistant_chat = chat["id"] if chat else ""
            return {"items": self._chat_rows(store), "current": self._assistant_chat}

        self._project_task("chats", work)

    @Slot(str)
    def openChat(self, chat_id: str) -> None:
        def work(ops) -> dict:
            store = self._chats(ops)
            chat = store.set_active(chat_id)
            self._assistant_chat = chat["id"]
            return {"chat": chat, "items": self._chat_rows(store)}

        self._project_task("chat", work)

    @Slot()
    def newChat(self) -> None:
        def work(ops) -> dict:
            store = self._chats(ops)
            chat = store.create()
            self._assistant_chat = chat["id"]
            return {"chat": chat, "items": self._chat_rows(store)}

        self._project_task("chat", work)

    # -- 会话模板（UI 接线）------------------------------------------------
    # 这里**只是接线**：模板清单、快照、路由、临时覆盖的实现全在 chat_templates.py，
    # 会话落盘在 chat_store.py。桥里不存第二套状态（Router 只有一个实例，就是
    # chat_templates.Router）；名字若与将来的工具 API 冲突，以 Python 侧为准改这里一处。

    def _chat_rows(self, store) -> list[dict]:
        """会话清单带上模板标记：界面一眼看出它是提取/剧本/分镜/自定义。"""
        rows = store.list()
        for row in rows:
            frozen = store.template(row.get("id") or "") or {}
            row["template_id"] = str(frozen.get("id") or "")
            row["template_name"] = str(frozen.get("name") or "")
        return rows

    def _chat_router(self):
        if self._chat_template_router is None:
            from .chat_templates import Router

            self._chat_template_router = Router()
        return self._chat_template_router

    @Slot()
    def listChatTemplates(self) -> None:
        """可用模板：内置三个 + 工程级/应用级自定义，带工具数与提示词摘要。"""

        def work(ops) -> dict:
            from .chat_templates import list_all

            return {"templates": list_all(ops), "project_path": str(self._project_path or "")}

        self._project_task("chat_templates", work)

    @Slot(str)
    def newChatWithTemplate(self, template_id: str) -> None:
        """新建会话；template_id 为空表示不绑定模板。"""

        def work(ops) -> dict:
            from .chat_templates import snapshot

            store = self._chats(ops)
            chat = store.create(template=snapshot(template_id, ops)) if template_id else store.create()
            self._assistant_chat = chat["id"]
            return {"chat": chat, "items": self._chat_rows(store), "template_id": template_id}

        self._project_task("chat", work)

    @Slot(str, result=str)
    def chatRouteTemplate(self, page_id: str) -> str:
        """这一页当前的临时覆盖（不含自动路由）；空串表示没有覆盖。"""
        return self._chat_router().override_for(page_id) or ""

    @Slot(str, result=str)
    def chatDefaultTemplate(self, page_id: str) -> str:
        """这一页的默认路由（还没开会话时新会话用它）。"""
        from .chat_templates import route_for_page

        return route_for_page(page_id) or ""

    @Slot(str, str)
    def overrideChatTemplate(self, page_id: str, template_id: str) -> None:
        """临时覆盖本页的去向；template_id 为空表示回到自动路由。"""
        router = self._chat_router()
        if template_id:
            router.override(page_id, template_id)
        else:
            router.clear(page_id)

    def _chat_for_route(self, page_id: str) -> str:
        """按去向挑会话：临时覆盖消费即失效；没有会话时才按页面默认开一个。

        规则（用户裁决，别顺手改成「每次发送都按页面路由」）：**用户已打开的会话优先**。
        页面路由只是「默认发给对应会话」；用户刚在下拉里显式挑了一个会话，是比页面更强的
        意图。强制路由会造成两种坏结果：翻页悄悄换掉他挑的会话；消息落在他没看的会话里。
        所以这里只有三种情况走路由：有临时覆盖（那一次）、或当前还没有任何会话。
        """
        from .chat_templates import route_for_page, snapshot

        router = self._chat_router()
        override_id = router.override_for(page_id)
        if override_id:
            router.clear(page_id)
            template_id = override_id
        elif self._assistant_chat:
            # 已经在某个会话里就发在那个会话——切页不会把用户选好的会话抢走。
            template_id = ""
        else:
            template_id = route_for_page(page_id) or ""
        if not template_id:
            return ""
        ops = project_catalog.open_graph_project(self._project())
        store = self._chats(ops)
        for row in store.list():
            frozen = store.template(row.get("id") or "") or {}
            if frozen.get("id") == template_id:
                return row["id"]
        return store.create(template=snapshot(template_id, ops))["id"]

    @Slot(str, str)
    def renameChat(self, chat_id: str, title: str) -> None:
        def work(ops) -> dict:
            store = self._chats(ops)
            chat = store.rename(chat_id, title)
            return {"chat": chat, "items": self._chat_rows(store)}

        self._project_task("chat", work)

    @Slot(str)
    def deleteChat(self, chat_id: str) -> None:
        def work(ops) -> dict:
            store = self._chats(ops)
            store.delete(chat_id)
            active = store.active(create=False)
            self._assistant_chat = active["id"] if active else ""
            items = self._chat_rows(store)
            chat = active or {"id": "", "session": "", "title": "新会话", "messages": []}
            return {"chat": chat, "items": items, "removed": chat_id}

        self._project_task("chat", work)

    def _capture_assistant_access(self, requested_chat):
        ops = project_catalog.open_graph_project(self._project())
        chat = self._chats(ops).select(requested_chat, activate=False)
        if chat is None:
            raise ValueError("无法选择工程会话")
        return chat["id"], ops.read_assistant_access(chat["id"])["access_mode"]

    @Slot(str, str)
    def assistantTurn(self, text: str, image_path: str = "") -> None:
        self._assistant_turn(text, image_path, self._assistant_chat)

    @staticmethod
    def _attachment_paths(attachments_json):
        paths = json.loads(attachments_json or '[]')
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError('附件须为本机文件路径数组')
        return list(dict.fromkeys(paths))

    @Slot(str, str)
    def assistantTurnWithAttachments(self, text: str, attachments_json: str = '[]') -> None:
        try:
            paths = self._attachment_paths(attachments_json)
        except (ValueError, TypeError) as error:
            self.errorOccurred.emit('assistant', str(error))
            return
        self._assistant_turn(text, '', self._assistant_chat, attachment_paths=paths)

    @Slot(str, str, str)
    def assistantTurnOnPage(self, text: str, attachments_json: str, page_id: str) -> None:
        """按当前页的去向发一句话：有路由（含临时覆盖）就用那个模板的会话。"""
        try:
            paths = self._attachment_paths(attachments_json)
        except (ValueError, TypeError) as error:
            self.errorOccurred.emit('assistant', str(error))
            return
        try:
            chat_id = self._chat_for_route(page_id)
        except (ValueError, OSError) as error:
            self.errorOccurred.emit('assistant', str(error))
            return
        self._assistant_turn(text, '', chat_id or self._assistant_chat, attachment_paths=paths)

    @Slot(str, str, str, str)
    def assistantSteer(self, text: str, attachments_json: str, chat_id: str, task_id: str) -> None:
        try:
            path = self._project()
            paths = self._attachment_paths(attachments_json)
            message = (text or '').strip()
            if not message and not paths:
                raise ValueError('请提供插话文字或附件')
            with self._assistant_lock:
                job = next((item for item in self._assistant_jobs.values()
                            if item.get('chat_id') == chat_id and item.get('task_id') == task_id and not item['cancel'].is_set() and not item.get('finished')), None)
                if job is None or job.get('runtime') is None or not job.get('binding_nonce'):
                    raise ValueError('目标任务已结束或尚未就绪，插话未发送；不会另开任务')
                runtime, nonce = job['runtime'], job['binding_nonce']
        except (ValueError, TypeError) as error:
            self.resultReady.emit('assistant_steer', {'chat_id': chat_id, 'task_id': task_id, 'status': 'not_delivered',
                                                    'reason': str(error), 'project_path': str(self._project_path or '')})
            return
        message = message or '补充本条附件。'
        message_id = f'steer-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}'
        def work(ops):
            from .assistant import build_attachment_blocks
            from .conversation_steering import pack_instruction
            store = self._chats(ops)
            attachments = ops.import_chat_attachments(chat_id, paths, task_id=task_id)['attachments'] if paths else []
            store.append(chat_id, 'user', message, message_id=message_id, task_id=task_id,
                         kind='user_steering', steering_status='sending', attachments=attachments)
            audit_directory = project_package.layout(path).extraction / 'assistant' / 'tasks' / task_id / 'steering'
            audit_directory.mkdir(parents=True, exist_ok=True)
            state_lock = threading.RLock()
            def receipt(event):
                status = event.get('status', 'not_delivered')
                with state_lock:
                    row = store.annotate_steering(chat_id, message_id, status, steering_reason=event.get('reason', ''))
                    record = {'message_id': message_id, 'chat_id': chat_id, 'task_id': task_id, 'text': message,
                              'attachments': attachments, 'status': row['steering_status'], 'reason': row.get('steering_reason', '')}
                    temporary = audit_directory / (message_id+'.tmp')
                    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
                    temporary.replace(audit_directory / (message_id+'.json'))
                self._conversation_event(ops, path, {'event': 'steering_status', **record})
                if not self._closed and self._project_path == path:
                    self.resultReady.emit('assistant_steer', {**record, 'project_path': str(path)})
            try:
                if self._closed or self._project_path != path or job['cancel'].is_set():
                    raise ValueError('目标任务已结束或工程已切换，附件已入档但插话未送达')
                blocks = pack_instruction(build_attachment_blocks(message, attachments))
                result = runtime.steer(chat_id, task_id, nonce, blocks, message_id=message_id, on_status=receipt)
                receipt(result | {'status': result.get('status', 'queued')})
                return {**result, 'chat_id': chat_id, 'task_id': task_id, 'message_id': message_id,
                        'attachments': attachments, 'project_path': str(path)}
            except Exception as error:
                receipt({'status': 'not_delivered', 'reason': str(error)})
                return {'chat_id': chat_id, 'task_id': task_id, 'message_id': message_id, 'status': 'not_delivered',
                        'reason': str(error), 'attachments': attachments, 'project_path': str(path)}
        self._project_task('assistant_steer', work)

    @Slot(str, "QVariantMap")
    def screenplayWritingTurn(self, text: str, target: dict) -> None:
        frozen_target = json.loads(json.dumps(dict(target), ensure_ascii=False))
        frozen_path = str(self._project_path or "")

        def failed(message):
            self.resultReady.emit("screenplay_writing_failed", {
                "target": frozen_target, "task_id": "", "project_path": frozen_path, "error": str(message),
            })

        try:
            requested_chat, access_mode = self._capture_assistant_access(self._assistant_chat)
        except (ValueError, OSError) as exc:
            failed(exc)
            self.errorOccurred.emit("screenplay_writing", str(exc))
            return

        def apply(task):
            if "writing_prepare_error" in task:
                failed(task["writing_prepare_error"])
                self.errorOccurred.emit("screenplay_writing", task["writing_prepare_error"])
                return {"target": frozen_target, "started": False, "error": task["writing_prepare_error"]}
            task_id = self._assistant_turn(text, "", requested_chat, writing_task=task, access_mode_override=access_mode)
            return {"target": task.get("target", frozen_target), "started": bool(task_id), "task_id": task_id or ""}

        def prepare(ops):
            try:
                return ops.prepare_script_writing_task(frozen_target, prompt=text)
            except Exception as exc:
                return {"writing_prepare_error": str(exc)}

        self._project_task("screenplay_writing", prepare, apply)

    def _assistant_turn(self, text: str, image_path: str, requested_chat: str | None,
                        *, writing_task: dict | None = None, access_mode_override: str | None = None, attachment_paths: list[str] | None = None) -> str | None:
        """Run an assistant turn with the tools in assistant.ASSISTANT_TOOLS.

        Native tools create source slices, plan exact pattern ranges, and
        precheck and automatically submit extraction batches through Operations.
        Chats persist on disk.
        """
        from .assistant import ASSISTANT_VERSION, _call, _public, run_turn, tool_catalog
        from .conversation_session import (
            ConversationCancelled,
            open_conversation_session,
            publish_event,
            use_chat,
        )

        paths = list(attachment_paths or [])
        def fail_start(error):
            if writing_task is not None:
                self.resultReady.emit("screenplay_writing_failed", {
                    "target": writing_task.get("target", {}), "task_id": "",
                    "project_path": str(self._project_path or ""), "error": str(error),
                })
            self.errorOccurred.emit("assistant", str(error))

        if str(image_path or "").strip():
            paths.append(str(image_path).strip())
        message = (text or "").strip() or ("请查看本条附件。" if paths else "")
        if not message:
            fail_start("先说一句想问什么")
            return
        try:
            path = self._project()
        except ValueError as exc:
            fail_start(exc)
            return
        image = str(image_path or "").strip()
        try:
            if access_mode_override is None:
                requested_chat, access_mode_override = self._capture_assistant_access(requested_chat)
            if access_mode_override not in {"standard", "full"}:
                raise ValueError("访问模式须为standard或full")
        except (ValueError, OSError) as exc:
            fail_start(exc)
            return

        from .task_ids import next_task_id

        # 任务身份是工程内序数 task-12：能读、能复述，不是时间戳加哈希。
        job = {"chat_id": requested_chat, "task_id": next_task_id(path), "runtime": None,
               "cancel": threading.Event(), "stop_reason": "user_stopped"}
        if writing_task is not None:
            job.update(writing_target=writing_task.get("target", {}), project_path=str(path))

        def check_cancelled(metadata=None):
            if job.get("control_error"):
                raise RuntimeError("停止控制通道失败，运行时已关闭：" + job["control_error"])
            if job["cancel"].is_set():
                raise ConversationCancelled(metadata, reason=job["stop_reason"])

        def work(ops) -> dict:
            def observer(event):
                if not job["cancel"].is_set():
                    self._conversation_event(ops, path, event | {"access_mode": job.get("access_mode", "standard")})
            with self._assistant_task_lock, use_chat(ops, requested_chat, on_event=observer) as selected:
                if self._closed or self._project_path != path:
                    raise ValueError("工程已切换，协作者任务已停止")
                started = time.monotonic()
                starting_revision = ops.graph_project_info()["revision"]
                store = self._chats(ops)
                chat_id = selected["id"]
                access_mode = access_mode_override
                job["access_mode"] = access_mode
                if not self._assistant_chat or self._assistant_chat == requested_chat:
                    self._assistant_chat = chat_id
                attachments = ops.import_chat_attachments(chat_id, paths, task_id=job["task_id"])["attachments"] if paths else []
                store.append(chat_id, "user", message, attachments=attachments, task_id=job["task_id"])
                task_id = job["task_id"]
                with self._assistant_lock:
                    job["chat_id"] = chat_id
                publish_event(ops, {"event": "assistant_started", "chat_id": chat_id, "task_id": task_id, "access_mode": access_mode})
                extraction_granularity = ops.load_project_settings()["extraction_granularity"]
                settings, api_key, workdir = harness_env(ops)
                runtime = self._attach_conversation_runtime(ops, settings, api_key, path)
                with self._assistant_lock:
                    job.update(chat_id=chat_id, runtime=runtime)
                entry = project_package.layout(path).entry
                task_id = job["task_id"]
                with self._assistant_lock:
                    job["task_id"] = task_id
                audit_dir = workdir / "assistant" / "tasks" / task_id
                audit_dir.mkdir(parents=True)
                audit = {"version": ASSISTANT_VERSION, "task_id": task_id,
                         "chat_id": chat_id, "status": "running", "calls": [], "access_mode": access_mode, "attachments": attachments,
                         "extraction_granularity": extraction_granularity,
                         "config": settings.model_dump(exclude={"key_env"}),
                         "response_idle_timeout_seconds": settings.timeout_seconds,
                         "runtime_restart": runtime.restart_reason, "prompt_config": runtime._prompt_config,
                         "project_style": writing_task["project_style"] if writing_task is not None else ops.load_project_style()}
                if writing_task is not None:
                    audit["writing_task"] = writing_task
                    audit["writing_prompt_version"] = SCREENPLAY_WRITING_PROMPT_VERSION
                    audit["writing_task_metadata"] = {
                        "task_id": task_id, "manifest_ref": str(audit_dir / "writing-task.json"),
                    }
                native_steps: list[dict] = []
                self._assistant_steps = native_steps
                calls = 0
                outcome: dict | None = None
                model_message_ids: list[str] = []
                message_lock = threading.RLock()

                def model_message(event):
                    from .assistant_audit import sanitize_host_audit
                    event = sanitize_host_audit(event)
                    message_id = str(event.get("message_id") or "")
                    if not message_id:
                        return
                    with message_lock:
                        if message_id in model_message_ids:
                            return
                        store.append(
                            chat_id, "assistant", event.get("text") or "",
                            kind="model_output", sdk_turn=True, task_id=task_id,
                            message_id=message_id, full_output=event.get("full_output"),
                            display_kind=event.get("display_kind", "answer"),
                            content=event.get("content"), seq=event.get("seq"),
                        )
                        model_message_ids.append(message_id)
                    if not job["cancel"].is_set():
                        self._conversation_event(ops, path, {
                            "event": "model_message", "chat_id": chat_id,
                            "task_id": task_id, "message_id": message_id,
                        })

                session_runner = None
                host = runtime.host
                if host is None:
                    raise ValueError("会话工具服务未就绪")
                task_secrets = [api_key, host.token]

                def redact(value: str) -> str:
                    for secret in task_secrets:
                        if secret:
                            value = value.replace(secret, "[REDACTED]")
                    return value

                def save(name, value):
                    from .assistant_audit import sanitize_host_audit
                    value = sanitize_host_audit(value)
                    (audit_dir / name).write_text(
                        redact(json.dumps(value, ensure_ascii=False, indent=2)), encoding="utf-8",
                    )

                native_call_lock = threading.Lock()
                submitted_unit_ids: list[str] = []

                def native_call(name, args):
                    with native_call_lock:
                        check_cancelled()
                        effective_name, effective_args = name, args
                        from .tool_fingerprint_refs import decode_tool_parameters
                        try:
                            if name == "patch_content_submission":
                                decoded = decode_tool_parameters(ops, args)
                                if not isinstance(decoded.get("submission_id"), str) or not isinstance(decoded.get("patches"), list):
                                    raise ValueError("请提供暂存编号和字段修补数组")
                                candidate = ops.preview_content_patch(decoded.get("submission_id"), decoded.get("expected_revision"), decoded.get("patches"))
                                effective_name, effective_args = candidate["operation"], candidate["arguments"]
                            if writing_task is not None and access_mode == "standard":
                                from .screenplay_writing import validate_script_writing_tool_call
                                validate_script_writing_tool_call(writing_task, effective_name, decode_tool_parameters(ops, effective_args), ops=ops)
                        except ValueError as exc:
                            from .model_text import scrub

                            message = redact(scrub(str(exc)))
                            native_steps.append({"tool": name, "args": args, "ok": False,
                                                 "error": message, "diagnostic": "writing_target_scope"})
                            raise ValueError(message) from exc
                        from .tool_execution_context import bind_tool_execution_metadata
                        task_metadata = (audit.get("writing_task_metadata") if access_mode == "standard" else None) or {
                            "task_id": task_id, "manifest_ref": str(audit_dir / "task.json")}
                        with bind_tool_execution_metadata(task_metadata):
                            step = _call(entry, name, args, access_mode=access_mode)
                        from .assistant_audit import sanitize_host_audit
                        step = sanitize_host_audit(step)
                        native_steps.append(step)
                        self.resultReady.emit("conversation_progress", {
                            "event": "tool_complete", "chat_id": chat_id, "task_id": task_id, "access_mode": access_mode,
                            "project_path": str(entry), "current": self._assistant_chat, "busy": True,
                            "step_index": len(native_steps) - 1, "step": _public([step])[0],
                        })
                        if not step["ok"]:
                            raise ValueError(step["error"])
                        committed = isinstance(step["result"], dict) and step["result"].get("status") in {"accepted", "unchanged"}
                        if committed and effective_name in {"apply_storyboard_changes", "submit_storyboard_assets", "create_missing_storyboard_assets", "generate_storyboard_sketch", "generate_storyboard_panel", "generate_storyboard_panels", "render_storyboard_style",
                                    "set_storyboard_animation", "attach_storyboard_video", "detach_storyboard_video",
                                    "prepare_storyboard_video", "submit_storyboard_video", "refresh_storyboard_video"}:
                            self._conversation_event(ops, path, {
                                "event": "storyboard_submission", "chat_id": chat_id, "task_id": task_id,
                                "unit_id": effective_args.get("unit_id"),
                                "board": step["result"].get("board", step["result"]),
                            })
                        if committed and writing_task is not None and effective_name in {"submit_script_unit", "apply_script_block_changes"}:
                            unit_id = str(effective_args.get("unit_id") or "")
                            if unit_id and unit_id not in submitted_unit_ids:
                                submitted_unit_ids.append(unit_id)
                            self._conversation_event(ops, path, {
                                "event": "screenplay_submission", "chat_id": chat_id, "task_id": job["task_id"],
                                "writing_target": writing_task.get("target"), "writing_status": "running",
                                "submitted_unit_ids": list(submitted_unit_ids),
                            })
                        return step["result"]

                save("task.json", audit)
                if writing_task is not None:
                    save("writing-task.json", {"version": SCREENPLAY_WRITING_PROMPT_VERSION,
                         "task": writing_task, "metadata": audit["writing_task_metadata"]})

                try:
                    check_cancelled()
                    task_catalog = tool_catalog(access_mode=access_mode)
                    audit["task_tool_names"] = [tool["name"] for tool in task_catalog]
                    session_runner, session = open_conversation_session(
                        ops, settings, api_key, HarnessRunner,
                        task_tools=task_catalog, tool_handler=native_call,
                        chat_id=chat_id, prompt_config=audit["prompt_config"], task_id=task_id,
                        access_mode=access_mode, cancel_event=job["cancel"],
                        output_presentation="thought" if writing_task is not None else "answer",
                    )
                    audit["session_id"] = session
                    with self._assistant_lock:
                        job["binding_nonce"] = session_runner.binding_nonce

                    def ask(instruction) -> str:
                        nonlocal calls
                        check_cancelled()
                        calls += 1
                        save(f"prompt-{calls}.json", {"instruction": instruction})
                        try:
                            reply = session_runner.agent_turn(instruction, session, on_message=model_message)
                        except Exception as exc:
                            failed_metadata = getattr(exc, "metadata", None) or session_runner.usage_metadata()
                            save(f"metadata-{calls}.json", failed_metadata)
                            save(f"partial-raw-{calls}.json", {"responses": session_runner.partial_raw()})
                            audit["calls"].append(failed_metadata)
                            save("task.json", audit)
                            check_cancelled(failed_metadata)
                            raise
                        (audit_dir / f"raw-{calls}.txt").write_text(redact(reply.raw), encoding="utf-8")
                        save(f"metadata-{calls}.json", reply.metadata)
                        for event in reply.metadata.get("model_messages") or []:
                            model_message(event)
                        audit["calls"].append(reply.metadata)
                        save("task.json", audit)
                        check_cancelled(reply.metadata)
                        return reply.raw

                    def on_format_error(event):
                        check_cancelled(event.get("metadata"))
                        diagnostic_path = event.get("diagnostic_path") or str(audit_dir / f"metadata-{calls}.json")
                        audit.setdefault("format_repairs", []).append({
                            "attempt": event.get("attempt"), "code": event.get("code"),
                            "diagnostic_path": diagnostic_path,
                            "metadata_path": str(audit_dir / f"metadata-{calls}.json"),
                        })
                        save("task.json", audit)
                        store.append(
                            chat_id, "system", "工具参数格式错误，已反馈同一任务修正。",
                            kind="format_correction", task_id=task_id,
                            diagnostic_path=diagnostic_path, audit_directory=str(audit_dir),
                            error_code=event.get("code"), attempt=event.get("attempt"),
                        )
                        self._conversation_event(ops, path, {
                            "event": "format_correction", "chat_id": chat_id, "task_id": task_id,
                        })

                    result = run_turn(
                        ask, message if writing_task is None else message + "\n\n" + str(writing_task.get("instruction") or "")
                        + ("\n当前页面创作焦点及输入清单（完全访问模式不限制写入范围）：" if access_mode == "full" else "\n固定写作目标及输入清单：") + json.dumps(writing_task, ensure_ascii=False)
                        + "\n任务与依据档案由程序内部关联；请只提供业务内容、对象ID和并发校验参数。"
                        + "\n写作提示版本：" + SCREENPLAY_WRITING_PROMPT_VERSION,
                        project=entry, image="" if attachments else image, attachments=attachments, steps=native_steps,
                        on_format_error=on_format_error,
                        extraction_granularity=extraction_granularity, prompt_config=audit["prompt_config"],
                        project_style=audit["project_style"], task_id=task_id, access_mode=access_mode,
                    )
                    check_cancelled()
                    if model_message_ids:
                        saved = store.annotate_message(
                            chat_id, model_message_ids[-1],
                            steps=result.get("steps") or [], task_version=ASSISTANT_VERSION,
                        )
                    else:
                        saved = store.append(
                            chat_id, "assistant", result.get("answer") or "",
                            steps=result.get("steps") or [], task_id=task_id,
                            task_version=ASSISTANT_VERSION, sdk_turn=True,
                        )
                    audit["status"] = "completed"
                    revision = ops.graph_project_info()["revision"]
                    result.update(revision=revision, graph_reset_required=revision != starting_revision)
                    outcome = result | {"chat_id": chat_id, "title": saved["title"], "chat": saved,
                                        "items": store.list(), "task_id": task_id,
                                        "audit_directory": str(audit_dir)}
                    return outcome
                except Exception as exc:
                    if job["cancel"].is_set() and not job.get("control_error") and not isinstance(exc, ConversationCancelled):
                        exc = ConversationCancelled(getattr(exc, "metadata", None), reason=job["stop_reason"])
                    cancelled = isinstance(exc, ConversationCancelled)
                    audit.update(status="cancelled" if cancelled else "incomplete",
                                 error_type=type(exc).__name__, error=redact(str(exc)))
                    if not cancelled or session_runner is not None and not session_runner.turn_settled or not runtime.healthy():
                        if session_runner is not None:
                            session_runner.cancel()
                        else:
                            runtime.close("task_failed")
                    self._clear_closed_conversation(runtime)
                    # An already running local operation may finish atomically.
                    # Wait for it before reporting the actual retained revision.
                    with native_call_lock:
                        pass
                    failure_metadata = getattr(exc, "metadata", None) or {}
                    diagnostic_path = getattr(exc, "diagnostic_path", "") or str(audit_dir / "task.json")
                    from .model_text import scrub
                    from .task_ids import task_reference

                    # 给模型与面板的话只说哪一类事、哪个可读编号；完整路径留在审计里。
                    reason = redact(scrub(str(exc)))
                    if getattr(exc, "code", "") == "tool_input_invalid_json" and "提取" in message:
                        reason = "模型工具参数 JSON 格式错误，本次提取未完成"
                    revision = ops.graph_project_info()["revision"]
                    state = "实际已提交内容见程序入库汇总。"
                    if not cancelled:
                        state += "本次任务未全部完成。"
                    audit.update(finish_reason=failure_metadata.get("finish_reason"),
                                 sdk_error_diagnostics=failure_metadata.get("sdk_error_diagnostics") or [],
                                 diagnostic_path=diagnostic_path, starting_revision=starting_revision,
                                 final_revision=revision)
                    store.append(chat_id, "system", reason + "。" + state,
                                 kind="assistant_cancelled" if cancelled else "assistant_incomplete", task_id=task_id,
                                 diagnostic_path=diagnostic_path, audit_directory=str(audit_dir),
                                 steps=_public(native_steps),
                                 error_code=getattr(exc, "code", type(exc).__name__),
                                 finish_reason=failure_metadata.get("finish_reason"),
                                 revision=revision, starting_revision=starting_revision)
                    if cancelled:
                        outcome = {"status": "cancelled", "chat_id": chat_id, "task_id": task_id,
                                   "chat": store.load(chat_id), "items": store.list(), "revision": revision,
                                   "graph_reset_required": revision != starting_revision,
                                   "audit_directory": str(audit_dir)}
                        return outcome
                    raise ValueError(reason + "。" + state + "诊断见" + task_reference(task_id)
                                     + "的本地任务记录（不随消息发送）。") from exc
                finally:
                    host_cleanup = None
                    if session_runner is not None:
                        host_cleanup = session_runner.close(completed=audit["status"] == "completed")
                    if not isinstance(host_cleanup, dict):
                        from .assistant_host import read_host_task_cleanup
                        host_cleanup = read_host_task_cleanup(path, task_id)
                    audit['host_cleanup'] = host_cleanup
                    unfinished = [row for row in host_cleanup.get('cleanup_receipts', [])
                                  if row.get('reason') == 'task_completed_before_command_finished']
                    if unfinished and audit['status'] == 'completed':
                        audit['model_finish_reason'] = (audit['calls'][-1].get('finish_reason') if audit['calls'] else None) or 'completed'
                        audit['status'] = 'incomplete'
                        if outcome is not None:
                            outcome.update(status='incomplete', model_finish_reason=audit['model_finish_reason'], host_cleanup=host_cleanup)
                    from .task_write_summary import build_submission_summary
                    with native_call_lock:
                        commit_summary = build_submission_summary(
                            native_steps, task_id=task_id,
                            status="failed" if audit["status"] == "incomplete" else audit["status"],
                            host_cleanup=host_cleanup,
                        )
                    audit.update(commit_summary=commit_summary,
                                 commit_summary_text=commit_summary["text"])
                    store.append(chat_id, "system", commit_summary["text"],
                                 kind="task_summary", task_id=task_id,
                                 commit_summary=commit_summary)
                    if outcome is not None:
                        outcome.update(commit_summary=commit_summary, host_cleanup=host_cleanup,
                                       commit_summary_text=commit_summary["text"],
                                       chat=store.load(chat_id), items=store.list())
                    audit.update(steps=native_steps, public_steps=_public(native_steps),
                                 model_calls=calls, elapsed_seconds=time.monotonic() - started)
                    if writing_task is not None:
                        audit["submitted_unit_ids"] = list(submitted_unit_ids)
                    save("task.json", audit)
                    with self._assistant_lock:
                        job["finished"] = True
                    self._conversation_event(ops, path, {
                        "chat_id": chat_id, "event": "task_complete", "task_id": task_id, "access_mode": access_mode,
                        "revision": ops.graph_project_info()["revision"],
                        "graph_reset_required": ops.graph_project_info()["revision"] != starting_revision,
                        **({"writing_target": writing_task.get("target"), "writing_status": audit["status"],
                            "submitted_unit_ids": list(submitted_unit_ids)} if writing_task is not None else {}),
                    }, busy=False)

        def apply(result: dict) -> dict:
            if result.get("graph_reset_required"):
                self._canvas.clear(preserve_positions=True)
            return result

        with self._assistant_lock:
            request_id = self._next_request + 1
            self._assistant_jobs[request_id] = job
        self._project_task("assistant", work, apply)
        return job["task_id"]

    @Slot(str, str)
    def assistantStop(self, chat_id: str, task_id: str) -> None:
        """Stop only the identified running task, never another chat or queued job."""
        if self._closed:
            return
        with self._assistant_lock:
            job = next((item for item in self._assistant_jobs.values()
                        if item['chat_id'] == chat_id and item['task_id'] == task_id and not item.get('finished')), None)
            current = next((item for item in self._assistant_jobs.values() if item['chat_id'] == chat_id and not item.get('finished')), None)
            duplicate = bool(job and job['cancel'].is_set())
            if job is not None:
                job['stop_reason'] = 'user_stopped'
                job['cancel'].set()
                runtime, nonce = job.get('runtime'), job.get('binding_nonce')
            else:
                runtime, nonce = None, None
        identity = {'chat_id': chat_id, 'task_id': task_id, **self._assistant_activity_snapshot()}
        if job is None:
            self.resultReady.emit('assistant_stop', identity | {'status': 'not_running', 'reason': '目标任务已结束或当前进程没有该任务'})
            if current is None:
                self.resultReady.emit('conversation_progress', identity | {'event': 'task_complete', 'busy': False, 'status': 'not_running'})
            else:
                self.resultReady.emit('conversation_progress', identity | {'event': 'stop_ignored', 'busy': True,
                                      'task_id': current['task_id'], 'access_mode': current.get('access_mode', 'standard')})
            return
        self.resultReady.emit('assistant_stop', identity | {'status': 'stopping'})
        if duplicate:
            return
        if runtime is not None and nonce:
            runtime.signal_stop(nonce)
            def interrupt():
                try:
                    runtime.interrupt(nonce)
                except Exception as exc:
                    job['control_error'] = str(exc)
                    runtime.close('interrupt_failed')
            self._pool.submit(interrupt)

    @Slot()
    def assistantReset(self) -> None:
        """换个话题：开一个新会话，旧的那个还在清单里。"""
        self.newChat()

    @Slot(str, "QVariantMap")
    def reviseNode(self, node_id: str, changes: dict) -> None:
        """Save an explicit user correction after deterministic prechecks."""
        allowed = {
            "node_type", "name", "aliases", "identity_note",
            "disclosure",
        }
        payload = {
            key: value for key, value in (changes or {}).items() if key in allowed
        }
        if not payload:
            self.errorOccurred.emit("node_edit", "没有可应用的修改")
            return

        def work(ops) -> dict:
            snapshot = ops.graph_snapshot()
            node = next(
                (item for item in snapshot["nodes"] if item["id"] == node_id), None
            )
            if node is None:
                raise ValueError("节点不存在")
            updated = dict(node) | payload
            revision = ops.graph_project_info()["revision"]
            result = ops.apply_changes([updated], [], revision, f"节点修正：{node['name']}")
            if result["status"] in {"accepted", "unchanged"}:
                self._canvas.clear(preserve_positions=True)
            return result | {"graph_reset_required": result["status"] == "accepted",
                             "node_id": node_id, "changes": payload}

        def apply(result: dict) -> dict:
            if result.get("status") == "accepted":
                self._canvas.clear(preserve_positions=True)
            return result

        self._project_task("node_edit", work, apply)

    @Slot()
    def runStaticCheck(self) -> None:
        self._static_scan_generation += 1
        scan_id = self._static_scan_generation
        try:
            path = self._project()
        except ValueError as exc:
            self.errorOccurred.emit("static_check", str(exc))
            return
        self.resultReady.emit("static_check_started", {"scan_id": scan_id, "project_path": str(path)})

        def work(ops) -> dict:
            try:
                report = ops.check_project()
            except Exception as exc:
                return {"scan_id": scan_id, "failed": True, "error": str(exc), "project_path": str(path)}
            current = ops.graph_project_info()["revision"]
            return report | {"scan_id": scan_id, "current_revision": current,
                             "is_stale": current != report["revision"], "project_path": str(path)}

        self._project_task("static_check", work)

    @Slot()
    def loadStaticCheckRevision(self) -> None:
        self._project_task("static_check_revision", lambda ops: {"revision": ops.graph_project_info()["revision"]})

    @Slot("QVariantMap", str, str)
    def resolveStaticIssue(self, issue: dict, kind: str, record_id: str = "") -> None:
        self._static_target_generation += 1
        generation = self._static_target_generation

        def work(ops) -> dict:
            snapshot = ops.graph_snapshot()
            nodes = {row["id"]: row for row in snapshot["nodes"]}
            record = None
            requested_record = record_id or issue.get("record_id")
            if requested_record:
                record = next((row for row in ops.property_records()
                               if row["id"] == requested_record), None)
            if kind not in {"node", "record", "source"}:
                raise ValueError("未知的检查定位类型")
            if kind == "record":
                node_id = (record or {}).get("node_id")
                target_id = (record or {}).get("target_id")
                if not node_id and target_id:
                    target = next((row for row in ops.property_records()
                                   if row["id"] == target_id), {})
                    node_id = target.get("node_id")
            else:
                node_id = issue.get("node_id") or (record or {}).get("node_id")
            node = nodes.get(node_id, {})
            if kind == "record" or (kind == "source" and record_id):
                evidence = (record or {}).get("evidence") or {}
                disclosure = (record or {}).get("disclosure") or {}
                source_id = evidence.get("source_id") or disclosure.get("source_id")
                start, end = evidence.get("start", disclosure.get("start")), evidence.get("end")
                quote = evidence.get("quote") or ""
            else:
                evidence = (record or {}).get("evidence") or node.get("evidence") or {}
                disclosure = (record or {}).get("disclosure") or node.get("disclosure") or {}
                source_id = issue.get("source_id") or evidence.get("source_id") or disclosure.get("source_id")
                start, end = issue.get("start"), issue.get("end")
                quote = issue.get("quote") or evidence.get("quote") or ""
            if kind == "record" and record is None:
                raise ValueError("该属性记录已不存在，不能定位旧检查结果")
            if kind == "node" and not node:
                raise ValueError("该节点已不存在，不能定位旧检查结果")
            if source_id:
                try:
                    text = ops.source(source_id)["text"]
                except (ValueError, KeyError):
                    if kind == "source":
                        raise ValueError("该问题引用的原文来源不存在，不能定位") from None
                    source_id = ""
                    start, end = None, None
                else:
                    if type(start) is not int:
                        if quote:
                            from .source_location import locate_text
                            hits = locate_text(text, [quote], 2)[0]["matches"]
                            if len(hits) != 1:
                                if kind == "source":
                                    raise ValueError("引文无法唯一定位，请重新检查或在原文中核对")
                            else:
                                start, end = hits[0]["start"], hits[0]["end"]
                        if type(start) is not int:
                            original_start = disclosure.get("start")
                            start = original_start if type(original_start) is int else 0
                    if type(end) is not int:
                        end = start + len(quote) if quote else min(len(text), start + 80)
                    valid_range = 0 <= start <= end <= len(text)
                    if quote and (not valid_range or text[start:end] != quote):
                        from .source_location import locate_text
                        hits = locate_text(text, [quote], 2)[0]["matches"]
                        if len(hits) == 1:
                            start, end = hits[0]["start"], hits[0]["end"]
                            valid_range = True
                        else:
                            if kind == "source":
                                raise ValueError("披露坐标不是该引文位置，且引文无法唯一定位")
                            source_id = ""
                            start, end = None, None
                    elif not valid_range:
                        if kind == "source":
                            raise ValueError("检查结果的原文坐标已无效，请重新运行检查")
                        source_id = ""
                        start, end = None, None
            elif kind == "source":
                raise ValueError("该问题没有可定位的原文来源")
            if kind == "record" and node_id not in nodes:
                node_id = ""
            return {"kind": kind, "node_id": node_id or "", "record": record or {},
                    "record_id": requested_record or "", "source_id": source_id or "",
                    "start": start, "end": end, "quote": quote, "generation": generation}

        def apply(result: dict) -> dict:
            return result | {"superseded": generation != self._static_target_generation}

        self._project_task("static_check_target", work, apply)

    @Slot(str, str, bool)
    def runSourceTask(self, source_id: str, slice_id: str, review: bool = False) -> None:
        """Send the selected source range to the current unified conversation."""
        try:
            requested_chat, access_mode = self._capture_assistant_access(self._assistant_chat)
        except (ValueError, OSError) as exc:
            self.errorOccurred.emit("source_task", str(exc))
            return

        def work(ops) -> dict:
            source = ops.source(source_id)
            start, end = 0, len(source["text"])
            title = source.get("title") or source_id
            if slice_id:
                part = ops.source_slice(source_id, slice_id)
                start, end = part["start"], part["end"]
                title = part.get("title") or title
            action = "复核" if review else "提取"
            purpose = effective_prompt(ops, "review" if review else "source_extraction")
            instruction = (f"{action}原文：{title}。\n来源 source_id={source_id}，"
                           f"范围 start={start}, end={end}（Unicode字符坐标，左闭右开）"
                           + (f"，slice_id={slice_id}" if slice_id else "（全文）") + "。\n"
                           + purpose)
            return {"instruction": instruction, "source_id": source_id, "slice_id": slice_id,
                    "start": start, "end": end, "review": review, "chat_id": requested_chat}

        def apply(result: dict) -> dict:
            self._assistant_turn(result["instruction"], "", requested_chat, access_mode_override=access_mode)
            return result

        self._project_task("source_task", work, apply)

    @Slot()
    def loadSources(self) -> None:
        self._project_task("sources", lambda ops: {"items": ops.sources()})

    @Slot(str)
    def sourceText(self, source_id: str) -> None:
        self._project_task("source", lambda ops: {"source_id": source_id, "source": ops.source(source_id)})

    @Slot(str, str)
    def importSource(self, title: str, text: str) -> None:
        def work(ops) -> dict:
            source_id = ops.import_source(title, text)
            return {"source_id": source_id, "sources": ops.sources(), "revision": ops.graph_project_info()["revision"]}

        self._project_task("source_imported", work)

    @Slot(str, str)
    def importSourceFile(self, title: str, file_path: str) -> None:
        def work(ops) -> dict:
            source_id = ops.import_source_file(self.urlToPath(file_path), title=title)
            return {"source_id": source_id, "sources": ops.sources(), "revision": ops.graph_project_info()["revision"]}

        self._project_task("source_imported", work)

    @Slot(str)
    def loadSourceSlices(self, source_id: str) -> None:
        self._project_task(
            "source_slices",
            lambda ops: {"source_id": source_id, "items": ops.source_slices(source_id)},
        )

    @Slot(str, str)
    def sourceSlice(self, source_id: str, slice_id: str) -> None:
        self._project_task(
            "source_slice",
            lambda ops: {"source_id": source_id, "slice": ops.source_slice(source_id, slice_id)},
        )

    @Slot()
    @Slot(int)
    def loadProjectSettings(self, request_token: int = 0) -> None:
        self._project_task("project_settings", lambda ops: ops.load_project_settings() | {
            "catalog": ops.provider_catalog(), "request_token": request_token,
            "project_path": str(project_package.layout(ops.project_info().path).entry)})

    @Slot("QVariantMap")
    @Slot("QVariantMap", int)
    def saveProjectSettings(self, settings: dict, request_token: int = 0) -> None:
        self.saveProjectSettingsWithKey(settings, "", False, request_token)

    @Slot("QVariantMap", str, bool)
    @Slot("QVariantMap", str, bool, int)
    def saveProjectSettingsWithKey(self, settings: dict, api_key: str, clear_api_key: bool,
                                   request_token: int = 0) -> None:
        payload = dict(settings)
        payload["profile"] = self._exact_integers(dict(payload.get("profile") or {}))
        def work(ops):
            identity = {"request_token": request_token,
                        "project_path": str(project_package.layout(ops.project_info().path).entry)}
            try:
                return ops.save_project_settings(payload, api_key=api_key or None, clear_api_key=clear_api_key) | {
                    **identity, "catalog": ops.provider_catalog(), "saved": True}
            except Exception as error:
                # Secret-bearing failures must never cross signals or UI logs.
                return identity | {"error": "设置未保存，请检查配置及本机密钥存储。", "error_type": type(error).__name__}
        self._project_task("project_settings", work)

    @Slot("QVariantMap", int)
    def loadProjectKeyStatus(self, profile: dict, request_token: int) -> None:
        payload = self._exact_integers(dict(profile))
        def work(ops):
            identity = {"request_token": request_token,
                        "project_path": str(project_package.layout(ops.project_info().path).entry)}
            try:
                return identity | {"api_key_status": ops.provider_api_key_status(payload)}
            except (ValueError, OSError):
                return identity | {"error": "当前配置的密钥状态暂不可用。"}
        self._project_task("project_key_status", work)

    @Slot("QVariantMap", str, bool, int)
    def checkProjectApiKey(self, profile: dict, api_key: str, clear_api_key: bool,
                           request_token: int) -> None:
        try:
            path = self._project()
        except ValueError:
            return
        epoch = self._epoch
        payload = self._exact_integers(dict(profile))
        def work():
            identity = {"project_path": str(path), "request_token": request_token}
            try:
                ops = project_catalog.open_graph_project(path)
                result = ops.check_model_credential(payload, api_key=api_key or None,
                                                    ignore_saved_key=clear_api_key)
            except Exception:
                result = {"valid": None, "code": "check_failed", "message": "暂时无法完成有效性检查。"}
            if not self._closed and self._project_path == path and self._epoch == epoch:
                try:
                    self.resultReady.emit("project_key_check", result | identity)
                except RuntimeError:
                    pass  # The window was destroyed while the request completed.
        self._pool.submit(work)

    @Slot()
    def loadProjectStyle(self) -> None:
        self._project_task("project_style", lambda ops: ops.load_project_style() | {
            "project_path": str(project_package.layout(ops.project_info().path).entry)})

    @Slot(str, int)
    def saveProjectStyle(self, text: str, revision: int) -> None:
        self._project_task("project_style", lambda ops: ops.save_project_style(
            text, expected_revision=revision, reason="编辑项目风格约定") | {
                "project_path": str(project_package.layout(ops.project_info().path).entry), "saved": True})

    @Slot()
    def loadPromptSettings(self) -> None:
        self._project_task("prompt_settings", lambda ops: ops.load_prompt_settings() | {
            "project_path": str(project_package.layout(ops.project_info().path).entry)})

    @Slot(str, "QVariant", int)
    def savePromptOverride(self, prompt_id: str, text: object, revision: int) -> None:
        if text is not None and not isinstance(text, str):
            self.errorOccurred.emit("prompt_settings", "提示词须为文本")
            return
        self._project_task("prompt_settings", lambda ops: ops.save_prompt_override(
            prompt_id, text, expected_revision=revision, reason="编辑工程提示词") | {
                "project_path": str(project_package.layout(ops.project_info().path).entry),
                "saved": True, "prompt_id": prompt_id, "restored": text is None})

    @Slot(str)
    def renameProject(self, title: str) -> None:
        normalized = title.strip()
        if not normalized:
            self.errorOccurred.emit("project_renamed", "工程名称不能为空")
            return

        def rename(ops: GraphOperations) -> dict:
            ops.set_graph_title(normalized)
            project = ops.graph_project_info()
            project["path"] = str(project_package.layout(ops.project_info().path).entry)
            return {"project": project}

        self._project_task("project_renamed", rename)

    @Slot(str)
    def extractEventActions(self, node_id: str) -> None:
        """An explicit user request, dispatched into the selected unified chat."""
        try:
            requested_chat, access_mode = self._capture_assistant_access(self._assistant_chat)
        except (ValueError, OSError) as exc:
            self.errorOccurred.emit("event_actions_task", str(exc))
            return
        def work(ops) -> dict:
            node = next((row for row in ops.graph_snapshot()["nodes"] if row["id"] == node_id), None)
            if node is None or node.get("node_type") != "event":
                raise ValueError("动作提取只能针对已存在的event事件")
            instruction = (effective_prompt(ops, "action_extraction") + "\n目标事件："
                           + json.dumps({"event_id": node_id, "name": node.get("name")}, ensure_ascii=False))
            return {"instruction": instruction, "event_id": node_id, "chat_id": requested_chat}
        def apply(result: dict) -> dict:
            self._assistant_turn(result["instruction"], "", requested_chat, access_mode_override=access_mode)
            return result
        self._project_task("event_actions_task", work, apply)

    @Slot(str)
    @Slot(str, int)
    def loadProviderProfileFor(self, provider: str, request_token: int = 0) -> None:
        """Read this project's saved provider profile before editing or activating it."""
        def work(ops):
            profile = ops.harness_settings(provider.strip() or None)
            return {"profile": profile, "catalog": ops.provider_catalog(),
                    "api_key_status": ops.provider_api_key_status(profile),
                    "request_token": request_token,
                    "project_path": str(project_package.layout(ops.project_info().path).entry)}
        self._project_task("provider", work)
