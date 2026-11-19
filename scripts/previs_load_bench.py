"""离屏基准：预演页从建 view 到「可用」的耗时。

量的是产品里真实的那条链：本地静态服务 → DirectorWorkspaceView.qml（真文件）→ 下载并
解析入口 chunk → 主题注入 → ``window.__narrativePrevisHost`` 可用 → 第一次 ``getScene()``
往返。桥用最小替身（只回 ``previsThemeScript()``，走真的 ``previs_theme``），这样量的
不是别的页面、也不是聊天后端。

为什么必须离屏：基准要能在没有桌面、没有显卡的机器上跑，且能一轮一轮重复。
``QT_QPA_PLATFORM=offscreen`` 下 QtWebEngine 走软件渲染，WebGL 可能起不来——那页面
自己的三维引擎就绪不了。这时 ``hello()`` 的原样回执会写进报告，基准只量到
``loadFinished``，并在输出里明说，不假装量到了。

用法：
    python scripts/previs_load_bench.py                  # 2 轮，取中位数（产品现在的配置）
    python scripts/previs_load_bench.py --repeat 3 --out artifacts/previs-load-bench.json
    python scripts/previs_load_bench.py --named-profile   # 对照组：换成有磁盘缓存的命名 profile
    python scripts/previs_load_bench.py --named-profile --keep-cache   # 上面那一组的第二进程（热缓存）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

VIEW_QML = SRC / "narrative" / "qml" / "DirectorWorkspaceView.qml"

# 页面里的宿主接口：它挂上并且 hello 可调，才算「页面能用」。
_HOST_PROBE = ("typeof window.__narrativePrevisHost === 'object'"
               " && window.__narrativePrevisHost !== null"
               " && typeof window.__narrativePrevisHost.hello === 'function'")

# 把 runJavaScript 折成「设属性 → 轮询结果」的 QML 小件。为什么不让 Python 直接调
# view.runJavaScript(script, 回调)：QML 里的 WebEngineView 在 Python 侧只是个 QObject，
# PySide 没法把 Python 回调塞进它的 runJavaScript（会报 Cannot copy-convert … to C++），
# 而在 QML 里调是正常的。所以脚本在 QML 里跑，结果通过一个属性回传。
_RUNNER_QML = """import QtQml

QtObject {
    id: runner
    property var target: null
    property string tag: ""
    property string request: ""
    property var results: ({})

    onRequestChanged: {
        if (request.length === 0)
            return
        var script = request
        var key = tag
        request = ""
        if (!target) {
            var failed = results
            failed[key] = "NO_TARGET"
            results = failed
            return
        }
        target.runJavaScript(script, function (value) {
            var copy = results
            copy[key] = value
            results = copy
        })
    }
}
"""


def _settle(milliseconds: int) -> None:
    """跑一下事件循环：WebEngine 的回执、QML 的信号都排在主线程事件队列上。"""
    from PySide6.QtTest import QTest

    QTest.qWait(max(1, milliseconds))


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 1) if values else 0.0


class _BridgeStub:
    """QML 侧只跟桥要一样东西：换肤脚本。真实现的「只算一次」也照搬。"""

    def __init__(self, dist: Path, dark: bool = True):
        self._dist = dist
        self._dark = dark
        self._script: str | None = None
        self.generate_ms: float | None = None

    def script(self) -> str:
        from narrative import previs_theme

        if self._script is None:
            started = time.perf_counter()
            self._script = previs_theme.inject_script(
                previs_theme.themed_css(self._dist, self._dark))
            self.generate_ms = round((time.perf_counter() - started) * 1000, 1)
        return self._script


def _make_bridge_class():
    """桥替身必须是 QObject + Slot：QML 只能调用 C++ 元对象上的方法。"""
    from PySide6.QtCore import QObject, Slot

    class BridgeStubObject(QObject):
        def __init__(self, stub: _BridgeStub):
            super().__init__()
            self._stub = stub

        @Slot(result=str)
        def previsThemeScript(self) -> str:  # noqa: N802 - 名字与 QML 对齐
            return self._stub.script()

    return BridgeStubObject


class _Runner:
    """在 QML 里执行脚本，把结果折回同步 Python 调用。"""

    def __init__(self, engine):
        from PySide6.QtCore import QUrl
        from PySide6.QtQml import QQmlComponent

        directory = Path(tempfile.mkdtemp(prefix="previs-bench-"))
        path = directory / "runner.qml"
        path.write_text(_RUNNER_QML, encoding="utf-8")
        # 组件与引擎都要留着引用、且把对象挂到引擎上：QML 建出来的 QtObject 没有父对象时，
        # 组件被回收就跟着一起没了（后面 setProperty 会报 already deleted）。
        self._component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
        if self._component.isError():
            raise RuntimeError("基准用的 QML 小件加载失败：" +
                               "; ".join(e.toString() for e in self._component.errors()))
        self.item = self._component.create()
        self.item.setParent(engine)

    def attach(self, view) -> None:
        self.item.setProperty("target", view)

    def eval(self, script: str, *, timeout_s: float = 10.0) -> tuple[object, float]:
        """跑一段脚本，返回（结果, 往返毫秒）。超时抛 TimeoutError。"""
        tag = f"t{time.perf_counter_ns()}"
        started = time.perf_counter()
        self.item.setProperty("tag", tag)
        self.item.setProperty("request", script)
        while time.perf_counter() - started < timeout_s:
            results = self.item.property("results")
            value = results.toVariant() if hasattr(results, "toVariant") else results
            if isinstance(value, dict) and tag in value:
                return value[tag], round((time.perf_counter() - started) * 1000, 1)
            _settle(10)
        raise TimeoutError("脚本回执超时：" + script[:80])


def _run_once(engine, runner: _Runner, url: str, deadline_s: float,
              requests: list[dict], run_clock: list[float], engine_profile=None):
    """建一个 view、等它可用、量第一次 getScene，然后销毁。返回一条记录。"""
    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlComponent

    record: dict = {"url": url, "progress": [], "requests_before": len(requests)}

    component = QQmlComponent(engine, QUrl.fromLocalFile(str(VIEW_QML)))
    if component.isError():
        raise RuntimeError("DirectorWorkspaceView.qml 加载失败：" +
                           "; ".join(error.toString() for error in component.errors()))

    started = time.perf_counter()
    run_clock[0] = started          # 服务的请求时间戳都相对这一刻
    view = component.create()
    if view is None or not view:
        raise RuntimeError("view 没能建出来：" +
                           "; ".join(error.toString() for error in component.errors()))
    record["create_ms"] = round((time.perf_counter() - started) * 1000, 1)
    runner.attach(view)
    if engine_profile is not None:
        # 对照组：换成 Python 建的命名 profile（持久磁盘缓存）。产品里没用它，
        # 这个开关只是让「换 profile 值不值」可复测。
        view.setProperty("profile", engine_profile)

    # QML 里的 WebEngineView 在 Python 侧只有属性可读（loading / loadProgress），信号取不到，
    # 所以这里轮询属性——时间线还是同一份，不依赖 PySide 有没有把 QML 类型包出来。
    marks: dict[str, float] = {}
    last_progress = -1

    def poll_load() -> None:
        nonlocal last_progress
        loading = bool(view.property("loading"))
        progress = view.property("loadProgress")
        progress = int(progress) if progress is not None else 0
        if progress != last_progress:
            record["progress"].append([progress, round((time.perf_counter() - started) * 1000, 1)])
            last_progress = progress
        if loading and "started" not in marks:
            marks["started"] = time.perf_counter()
        if not loading and "started" in marks and "finished" not in marks:
            marks["finished"] = time.perf_counter()

    # 与产品一致：view 先以空 URL 建出来，再给 initialUrl（Loader.onLoaded 就是这么做的）。
    view.setProperty("initialUrl", url)

    wait_until = started + deadline_s
    while "finished" not in marks and time.perf_counter() < wait_until:
        poll_load()
        _settle(10)
    if "finished" not in marks:
        record["error"] = "loadFinished 超时"
        record["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        view.deleteLater()
        _settle(30)
        return record

    finished = marks["finished"]
    record["start_ms"] = round((marks.get("started", started) - started) * 1000, 1)
    record["load_ms"] = round((finished - marks.get("started", started)) * 1000, 1)
    record["load_failed"] = bool(view.property("offline"))

    # 页面「可用」= 它自己的引导脚本能跑通。QML 在 loadFinished 之后才注入主题与引导，
    # 所以这里等的是同一条链；host 接口本身在三维引擎之前就挂上了，可能来得更早。
    host_ready_at: float | None = None
    while time.perf_counter() < wait_until:
        try:
            ready, _ms = runner.eval(_HOST_PROBE, timeout_s=5.0)
        except TimeoutError:
            ready = None
        if ready:
            host_ready_at = time.perf_counter()
            break
        _settle(20)
    record["host_wait_ms"] = round(((host_ready_at or time.perf_counter()) - finished) * 1000, 1)

    # 引导脚本的原文回执：页面起不来时它会是 'missing'，这正是要记录的证据。
    bootstrap = view.property("hostBootstrap") or ""
    try:
        boot_reply, boot_ms = runner.eval(bootstrap, timeout_s=5.0)
    except TimeoutError as exc:
        boot_reply, boot_ms = f"超时：{exc}", None
    record["bootstrap_reply"] = boot_reply
    record["bootstrap_ms"] = boot_ms
    record["ready"] = bool(host_ready_at) and boot_reply not in (None, "", "missing")

    # hello 的原文：三维引擎起不来时它会带着页面自己给的原因。它是 Promise，
    # 所以先挂 then 再轮询结果槽（JSON.stringify(Promise) 永远只会是 "{}"）。
    hello_started = time.perf_counter()
    try:
        runner.eval(
            "(function () { window.__narrativePrevisHello = 'pending';"
            " try { window.__narrativePrevisHost.hello().then("
            "  function (value) { window.__narrativePrevisHello = value },"
            "  function (error) { window.__narrativePrevisHello ="
            "     {ok: false, error: String(error && error.message || error)} });"
            " } catch (error) { window.__narrativePrevisHello ="
            "    {ok: false, error: String(error.message || error)} } return 'sent' })()",
            timeout_s=5.0)
        hello = None
        hello_deadline = time.perf_counter() + 12
        while time.perf_counter() < hello_deadline:
            raw, _ms = runner.eval(
                "JSON.stringify(window.__narrativePrevisHello)", timeout_s=5.0)
            if isinstance(raw, str) and raw not in ('"pending"', ""):
                hello = raw
                break
            _settle(30)
        record["hello_ms"] = round((time.perf_counter() - hello_started) * 1000, 1)
        record["hello"] = (hello or "（12 秒内没有兑现）")[:400]
    except TimeoutError as exc:
        record["hello"] = f"超时：{exc}"

    # 第一次 getScene 往返：与 QtWebEngineCaller 走的是同一条脚本路径（页面里的接口）。
    scene_script = ("(function () { try { return JSON.stringify("
                    "window.__narrativePrevisHost.getScene()) } "
                    "catch (error) { return 'ERR: ' + error.message } })()")
    scene_started = time.perf_counter()
    try:
        raw_scene, _ms = runner.eval(scene_script, timeout_s=10.0)
        record["getscene_ms"] = round((time.perf_counter() - scene_started) * 1000, 1)
        try:
            scene = json.loads(raw_scene or "{}")
        except (TypeError, json.JSONDecodeError):
            scene = {"__error": str(raw_scene)[:200]}
        record["getscene_ok"] = isinstance(scene, dict) and "__error" not in scene
        record["entity_count"] = len(scene.get("entities") or []) if isinstance(scene, dict) else 0
        record["getscene_key_count"] = len(scene) if isinstance(scene, dict) else 0
        if "__error" in scene:
            record["getscene_error"] = scene["__error"]
    except TimeoutError as exc:
        record["getscene_ms"] = round((time.perf_counter() - scene_started) * 1000, 1)
        record["getscene_ok"] = False
        record["getscene_error"] = f"超时：{exc}"

    record["ready_from_load_ms"] = round(((host_ready_at or finished) - finished) * 1000, 1)
    record["total_ms"] = round(((host_ready_at or finished) - started) * 1000, 1)
    record["requests"] = requests[record.pop("requests_before"):]

    # 页面自己的账：每个资源的 duration（含下载+排队，不含解析）与页面侧已过时间。
    # 有了它才能说清“3 秒花在哪个 chunk 上”，而不是凭印象。
    try:
        raw, _ms = runner.eval(
            "(function () { var rows = performance.getEntriesByType('resource')"
            " .map(function (e) { return [e.name.split('/').pop(), Math.round(e.duration)] });"
            " return JSON.stringify(rows) })()", timeout_s=5.0)
        record["page_resources"] = json.loads(raw or "[]")[:20]
    except (TimeoutError, TypeError, json.JSONDecodeError):
        record["page_resources"] = []

    runner.attach(None)
    view.deleteLater()
    _settle(200)   # 让渲染进程收尾，下一轮才是干净的
    return record


def _summarize(runs: list[dict]) -> dict:
    usable = [run for run in runs if run.get("ready")] or [
        run for run in runs if "total_ms" in run]
    fields = ("create_ms", "start_ms", "load_ms", "host_wait_ms", "total_ms",
              "getscene_ms", "hello_ms")
    summary = {field: _median([run[field] for run in usable if run.get(field) is not None])
               for field in fields}
    summary["ready_runs"] = sum(1 for run in runs if run.get("ready"))
    summary["runs"] = len(runs)
    return summary


def _print_table(runs: list[dict], summary: dict) -> None:
    header = ["轮", "建view", "起加载", "加载", "等host", "总耗时", "getScene", "就绪"]
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join(" --- " for _ in header) + "|")
    for index, run in enumerate(runs, 1):
        cells = [str(index)]
        for field in ("create_ms", "start_ms", "load_ms", "host_wait_ms", "total_ms",
                      "getscene_ms"):
            value = run.get(field)
            cells.append("-" if value is None else f"{value:.0f}")
        cells.append("是" if run.get("ready") else "否")
        print("| " + " | ".join(cells) + " |")
    cells = ["中位数"]
    for field in ("create_ms", "start_ms", "load_ms", "host_wait_ms", "total_ms", "getscene_ms"):
        cells.append(f"{summary.get(field, 0):.0f}")
    cells.append(f"{summary['ready_runs']}/{summary['runs']}")
    print("| " + " | ".join(cells) + " |")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预演页打开到可用的离屏基准")
    parser.add_argument("--repeat", type=int, default=2, help="跑几轮，取中位数（默认 2）")
    parser.add_argument("--out", type=Path,
                        default=REPO_ROOT / "artifacts" / "previs-load-bench.json",
                        help="JSON 输出路径")
    parser.add_argument("--workspace", type=Path, default=None,
                        help="工程数据根（缓存目录放它下面）；省略则用 .runtime/bench")
    parser.add_argument("--keep-cache", action="store_true",
                        help="不先清缓存目录：量的是热启动（默认清，第一轮是冷启动）")
    parser.add_argument("--deadline", type=float, default=40.0, help="单轮超时秒数（默认 40）")
    parser.add_argument("--named-profile", action="store_true",
                        help="对照组：换成 Python 建的命名 profile（有持久磁盘缓存）——产品里没用它")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat 必须 >= 1")

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # 离屏没有 GPU：不让 Chromium 卡在等显卡上下文上。软件 GL 仍可能给出 WebGL。
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                          "--disable-gpu --disable-gpu-compositing --ignore-gpu-blocklist")

    from PySide6.QtWebEngineQuick import QtWebEngineQuick

    QtWebEngineQuick.initialize()

    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine

    from narrative import previs_workspace

    QGuiApplication.instance() or QGuiApplication([])
    workspace = args.workspace or (REPO_ROOT / ".runtime" / "bench")

    dist = previs_workspace.dist_dir(workspace)
    if dist is None:
        print("找不到预演前端产物（.runtime/vendor/director-desk/dist）", file=sys.stderr)
        return 2

    cache_dir = previs_workspace.cache_dir(workspace)   # 冷/热只在 --named-profile 时才有区别

    # 请求计数：数的不是“服务快不快”，而是“页面到底拉了多少东西、卡在哪一段”。
    # 只记路径、字节数与相对时间。
    requests: list[dict] = []
    run_clock: list[float] = [time.perf_counter()]   # 每轮重置：请求时间戳相对建 view 那一刻

    class CountingHandler(previs_workspace._Handler):  # noqa: SLF001 - 基准要贴着实现量
        def do_GET(self):
            entry = {"path": self.path, "code": 0, "bytes": 0,
                     "start_ms": round((time.perf_counter() - run_clock[0]) * 1000, 1),
                     "end_ms": None}
            requests.append(entry)
            try:
                super().do_GET()
            except (ConnectionAbortedError, ConnectionResetError):
                entry["code"] = 499   # 页面已经在收尾：这条请求没意义，别让它打崩服务
            finally:
                entry["end_ms"] = round((time.perf_counter() - run_clock[0]) * 1000, 1)

        def copyfile(self, source, outputfile):
            data = source.read()
            entry = requests[-1] if requests else None
            if entry is not None:
                entry["bytes"] += len(data)
                entry["code"] = 200
            outputfile.write(data)

    original_handler = previs_workspace._Handler  # noqa: SLF001
    previs_workspace._Handler = CountingHandler  # noqa: SLF001
    server = previs_workspace.WorkspaceServer(dist)
    url = server.start()

    cache_note = previs_workspace.configure_engine_cache(workspace)
    if not cache_note:
        cache_note = "默认 profile 是 off-the-record：页面的磁盘缓存不落盘（Qt 6.12 行为）"

    engine_profile = None
    if args.named_profile:
        from PySide6.QtWebEngineQuick import QQuickWebEngineProfile

        profile_root = (cache_dir or (workspace / "previs-cache"))
        if not args.keep_cache:
            shutil.rmtree(profile_root, ignore_errors=True)
        (profile_root / "storage").mkdir(parents=True, exist_ok=True)
        engine_profile = QQuickWebEngineProfile(
            "previs-bench", None,
            cachePath=str(profile_root),
            persistentStoragePath=str(profile_root / "storage"),
        )
        cache_note = f"命名 profile（对照）：{profile_root}"

    engine = QQmlApplicationEngine()
    stub = _BridgeStub(dist)
    bridge = _make_bridge_class()(stub)          # 必须留引用：QML 上下文只持弱引用会变 null
    engine.rootContext().setContextProperty("bridge", bridge)
    runner = _Runner(engine)

    runs: list[dict] = []
    try:
        for index in range(args.repeat):
            record = _run_once(engine, runner, url, args.deadline, requests, run_clock,
                               engine_profile)
            record["round"] = index + 1
            record["cache_dir"] = cache_note
            runs.append(record)
            print(f"round {index + 1}: " + json.dumps(
                {key: record.get(key) for key in
                 ("create_ms", "start_ms", "load_ms", "host_wait_ms", "total_ms", "ready")},
                ensure_ascii=False))
    finally:
        server.stop()
        previs_workspace._Handler = original_handler  # noqa: SLF001

    summary = _summarize(runs)
    print()
    _print_table(runs, summary)

    total_bytes = sum(entry["bytes"] for entry in requests)
    cache_files = 0
    cache_bytes = 0
    if cache_dir is not None and cache_dir.is_dir():
        cached = [path for path in cache_dir.rglob("*") if path.is_file()]
        cache_files = len(cached)
        cache_bytes = sum(path.stat().st_size for path in cached)
    payload = {
        "url": url,
        "dist": str(dist),
        "dist_files": len([path for path in dist.rglob("*") if path.is_file()]),
        "cache": cache_note,
        "named_profile": bool(args.named_profile),
        "cache_files": cache_files,
        "cache_bytes": cache_bytes,
        "platform": os.environ.get("QT_QPA_PLATFORM"),
        "chromium_flags": os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS"),
        "theme_script_bytes": len(stub.script()),
        "theme_script_generate_ms": stub.generate_ms,
        "requests": {"count": len(requests), "bytes": total_bytes},
        "hello": next((run.get("hello") for run in runs if run.get("hello")), ""),
        "summary": summary,
        "runs": runs,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"服务：{url}（{payload['dist_files']} 个文件）")
    print(f"缓存：{cache_note}（落盘 {cache_files} 个文件 / {cache_bytes} 字节）")
    print(f"主题脚本：{payload['theme_script_bytes']} 字节，生成 "
          f"{payload['theme_script_generate_ms']} ms")
    print(f"页面拉取：{len(requests)} 个请求 / {total_bytes} 字节")
    print(f"hello：{payload['hello']}")
    print(f"JSON：{args.out}")
    return 0 if any(run.get("ready") for run in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
