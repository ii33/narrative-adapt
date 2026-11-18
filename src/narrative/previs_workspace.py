"""三维预演工作区：把 DirectorDesk 的构建产物以本地只读 HTTP 服务提供给 QtWebEngine。

为什么不是 file://：它的产物是 ES module，`file://` 下会被 CORS 拦掉；
QtWebEngine 对本地文件放开这些限制需要全局改安全策略，不如起一个只服务这一份
目录的本地服务——端口随机、只绑 127.0.0.1、不写文件。

产物来源按顺序查找，任一处存在即可：
    NARRATIVE_PREVIS_DIST（环境变量，绝对路径）
    <工程目录>/previs-web/          随工程打包的产物
    .runtime/vendor/director-desk/dist   开发期的构建输出
"""

from __future__ import annotations

import functools
import http.server
import os
import socketserver
import threading
from pathlib import Path

ENV_DIST = "NARRATIVE_PREVIS_DIST"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _candidates(workspace: Path | None) -> list[Path]:
    found: list[Path] = []
    override = os.environ.get(ENV_DIST, "").strip()
    if override:
        found.append(Path(override))
    if workspace is not None:
        found.append(workspace / "previs-web")
    found.append(_REPO_ROOT / ".runtime" / "vendor" / "director-desk" / "dist")
    return found


def dist_dir(workspace: Path | None = None) -> Path | None:
    """找到可用的构建产物目录；找不到返回 None。"""
    for candidate in _candidates(workspace):
        if (candidate / "index.html").is_file():
            return candidate
    return None


def availability(workspace: Path | None = None) -> tuple[bool, str]:
    """能不能开预演台，以及为什么不能。"""
    dist = dist_dir(workspace)
    if dist is None:
        return False, "未找到三维预演的前端产物（previs-web 或 director-desk/dist）"
    try:
        import PySide6.QtWebEngineQuick  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - 缺模块就是缺模块，界面按不可用处理
        return False, f"当前环境没有 QtWebEngine：{type(exc).__name__}"
    return True, str(dist)


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs) -> None:  # noqa: D401 - 静默默认日志
        return


class WorkspaceServer:
    """只服务一个目录的本地静态服务，端口随机，进程内启停。"""

    def __init__(self, directory: Path):
        self.directory = directory
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("预演台服务还没启动")
        return f"http://127.0.0.1:{self._server.server_address[1]}/index.html"

    def start(self) -> str:
        if self._server is not None:
            return self.url
        handler = functools.partial(_Handler, directory=str(self.directory))
        socketserver.TCPServer.allow_reuse_address = True
        server = socketserver.TCPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="previs-web")
        thread.start()
        self._server, self._thread = server, thread
        return self.url

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None


_default: WorkspaceServer | None = None


def shared_server(workspace: Path | None = None) -> WorkspaceServer | None:
    """整个进程共用一份服务：界面切来切去不必反复起停。"""
    global _default
    dist = dist_dir(workspace)
    if dist is None:
        return None
    if _default is None or _default.directory != dist:
        if _default is not None:
            _default.stop()
        _default = WorkspaceServer(dist)
        _default.start()
    else:
        _default.start()
    return _default


def stop_shared_server() -> None:
    global _default
    if _default is not None:
        _default.stop()
        _default = None
