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

# 缓存目录名：落在工程数据根下（与 previs/ 下载目录同级），随工程一起搬走或删掉。
PREVIS_CACHE_NAME = "previs-cache"
# 已经配过缓存的目录：Qt 只认「建页面之前」的那一次设置，重复 set 只会刷一条警告日志。
_cache_stamp: Path | None = None


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


def cache_dir(workspace: Path | None) -> Path | None:
    """预演台的磁盘缓存目录：工程数据根下的 ``previs-cache/``；没有工程就返回 None。

    不给默认缓存路径（系统用户目录下的 QtWebEngine）兜底：那正是「换个工程也读同一份
    缓存」的老问题，也让卸载/删工程时留下不知道归谁的目录。
    """
    if workspace is None:
        return None
    return workspace / PREVIS_CACHE_NAME


def configure_engine_cache(workspace: Path | None) -> str:
    """把预演台 WebEngine 的磁盘缓存钉到工程目录下，返回一句可写进诊断的结论。

    为什么必须在这里做：``bridge.previsWorkspace()`` 在 ``WebEngineView`` 创建**之前**
    被调用，这是唯一「还没建过任何页面」的时机——Qt 要求 ``setCachePath()`` 早于首个
    页面，之后再调只会被忽略。

    为什么用默认 profile：保存/导出的下载接管装在默认 profile 上（``native_bridge``），
    换一个 profile 就得把下载接管一起搬走，风险比这点收益大（见下）。

    实测（Qt 6.12 / PySide6，``scripts/previs_load_bench.py`` 的 ``--named-profile`` 对照组）：
    QML 的 ``WebEngineView`` 用的是 ``QQuickWebEngineProfile.defaultProfile()``，而它
    **是 off-the-record**——``cachePath()`` 恒为空、``setCachePath()`` 被忽略，磁盘缓存
    一行都不落盘；只有另建命名 profile 才写盘（一次 10–13 MB），可首屏并没有更快
    （3.19 s → 冷 3.47 s / 热 3.37 s）：写这十几 MB 本身就在这 3 秒里抢时间，而大头是
    JS 的解析与执行——同一进程里再开一次（34 个请求全命中内存缓存，一次网络都不发）
    也才掉到 0.92 s。也就是说这份磁盘缓存换不来用户看得见的时间，却要把下载接管
    （``native_bridge`` 接在默认 profile 上）搬到新 profile 上，不值。
    所以这里**不**换 profile：能在当前 profile 上配就配（将来 Qt 改成持久 profile、
    或别处接管了下载之后自动生效），配不了就返回空串，也不去工程里留一个永远空的目录。

    为什么不清缓存：二次打开靠这份缓存省掉 chunk 的重新下载与解析，每次打开前清掉
    就等于永远当冷启动跑（现在没有任何地方调 ``clearHttpCache()``，见
    ``scripts/previs_load_bench.py`` 与全仓检索）。
    """
    target = cache_dir(workspace)
    if target is None:
        return ""
    try:
        from PySide6.QtWebEngineQuick import QQuickWebEngineProfile
    except Exception:  # noqa: BLE001 - 没有 WebEngine：预演台本来就不可用
        return ""
    profile = QQuickWebEngineProfile.defaultProfile()
    try:
        if profile.isOffTheRecord():   # 页面的默认 profile 都是这一种：改不动，也不用建空目录
            return ""
    except (OSError, RuntimeError):
        return ""
    global _cache_stamp
    if _cache_stamp == target:
        return str(target)
    try:
        target.mkdir(parents=True, exist_ok=True)
        profile.setCachePath(str(target))
        profile.setPersistentStoragePath(str(target / "storage"))
    except OSError:  # 工程目录只读：宁可没有缓存，也不要为了缓存把页面弄失败
        return ""
    _cache_stamp = target
    return str(target)


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
    """整个进程共用一份服务：界面切来切去不必反复起停。

    顺手把 WebEngine 的缓存目录钉下来：这个函数在 view 建出来之前被调用（见
    ``configure_engine_cache`` 的说明），晚一步就不再生效了。
    """
    global _default
    dist = dist_dir(workspace)
    if dist is None:
        return None
    configure_engine_cache(workspace)
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
