"""启动耗时基准：离屏量 Main.qml 的 load，并给出各环节的分解表。

为什么是「子进程里量一次」：Qt 编译过的 QML 会在进程内缓存，同进程连量三遍，第二、
三遍会快出好几百毫秒——那样的中位数是假的。所以这里的 ``--repeat N`` 是起 N 个独立
子进程，每进程只量一次冷启动，再对各环节取中位数。

用法::

    uv run python scripts/startup_bench.py            # 默认 --repeat 3
    uv run python scripts/startup_bench.py --repeat 5 --json
    uv run python scripts/startup_bench.py --trace    # 追加 Main.qml 内部打点
    uv run python scripts/startup_bench.py --no-bridge --repeat 1  # 与旧探针对齐

``--trace`` 会给 QGuiApplication 传 ``--startup-trace``；Main.qml 只在看到这个参数时
才打点（正常启动时那是一个布尔量的判断），打出的 ``STARTUP <名字> <毫秒>`` 由本脚本
收集成「谁在启动时就被建了」的时间线。

退出码：0 正常；2 环境缺依赖（没装 PySide6 之类）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# 行的顺序就是下面 j() 记录的先后顺序：改这里要一起改 --worker 的测量代码。
STEPS = (
    "import QtGui",
    "import QtQml",
    "import QtWebEngine",
    "webengine initialize",
    "QGuiApplication",
    "import native_app",
    "import native_bridge",
    "NativeBridge()",
    "QQmlApplicationEngine",
    "load Main.qml",
)
MARKER = "__STARTUP_BENCH_JSON__ "
# 各页自己的 objectName：启动后逐个查「在不在」，用来判断谁在启动时就被建出来了。
PAGE_OBJECT_NAMES = (
    "launcherPage",
    "applicationNavigation",
    "browsePage",
    "adaptationBrowsePage",
    "relationPage",
    "sourcePage",
    "assetsPage",
    "screenplayPage",
    "screenplayStructurePage",
    "storyboardPage",
    "shotTablePage",
    "previsPage",
    "directorDeskPage",
    "staticCheckPage",
    "assistantPanel",
    "settingsPage",
)


def _worker(args: argparse.Namespace) -> int:
    """在离屏的 Qt 里量一遍启动，把结果当成一行 JSON 打到 stdout。"""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if args.isolate:
        # 把外观 / MCP / 渠道配置指到临时目录：基准不该依赖这台机器的用户设置，
        # 否则同一个提交在别人的机器上量出来是另一个数。
        import tempfile

        sandbox = Path(tempfile.mkdtemp(prefix="narrative-bench-"))
        os.environ["NARRATIVE_PROVIDER_DB"] = str(sandbox / "providers.sqlite3")
        os.environ["NARRATIVE_APPEARANCE_FILE"] = str(sandbox / "appearance.json")
        os.environ["NARRATIVE_MCP_FILE"] = str(sandbox / "mcp-servers.json")

    steps: dict[str, float] = {}
    messages: list[str] = []
    notes: list[str] = []
    t_start = time.perf_counter()
    previous = t_start

    def mark(label: str) -> None:
        nonlocal previous
        now = time.perf_counter()
        steps[label] = round(1000.0 * (now - previous), 1)
        previous = now

    try:
        from PySide6.QtGui import QGuiApplication
    except ImportError as exc:  # pragma: no cover - 取决于机器
        print(f"未安装 PySide6：{exc}", file=sys.stderr)
        return 2
    mark("import QtGui")

    from PySide6.QtQml import QQmlApplicationEngine
    mark("import QtQml")

    webengine = None
    try:
        from PySide6.QtWebEngineQuick import QtWebEngineQuick

        webengine = QtWebEngineQuick
    except ImportError as exc:  # 没有 WebEngine 时预演页自己会退化成「不可用」
        notes.append(f"QtWebEngine 不可用：{exc}")
    mark("import QtWebEngine")

    if webengine is not None:
        webengine.initialize()
    mark("webengine initialize")

    argv = [sys.argv[0]] + (["--startup-trace"] if args.trace else [])
    # 必须留着这个引用：QGuiApplication 一旦被回收，engine.load 就会崩。
    _application = QGuiApplication.instance() or QGuiApplication(argv)
    assert _application is not None
    mark("QGuiApplication")

    if args.trace:
        # 打点走 console.log：装一个消息处理器把它收进列表，顺便让 QML 自己的
        # 警告不至于刷满终端（它们一条条记下来，最后随 JSON 一起回报）。
        from PySide6.QtCore import qInstallMessageHandler

        def handler(mode, context, message) -> None:  # noqa: ANN001 - Qt 签名
            messages.append(message)

        qInstallMessageHandler(handler)

    import narrative.native_app as native_app
    mark("import native_app")

    bridge = None
    if args.bridge:
        from narrative.native_bridge import NativeBridge
        mark("import native_bridge")
        # bridge 的构造与「先定外观」的读取：真实启动在加载 QML 之前就做完这两步，
        # 否则第一帧浅色、第二帧才跳深色。它不在用户给的那张表里，单独量出来。
        bridge = NativeBridge()
        bridge.load_appearance()
    mark("NativeBridge()")

    engine = QQmlApplicationEngine()
    if bridge is not None:
        engine.rootContext().setContextProperty("bridge", bridge)
    mark("QQmlApplicationEngine")

    from PySide6.QtCore import QUrl

    load_started = time.perf_counter()
    load_started_wall = time.time() * 1000.0
    entry = args.qml.resolve() if args.qml is not None else native_app.QML_PATH
    engine.load(QUrl.fromLocalFile(str(entry)))
    load_ms = 1000.0 * (time.perf_counter() - load_started)
    mark("load Main.qml")

    roots = engine.rootObjects()
    trace: list[dict[str, object]] = []
    for message in messages:
        if not message.startswith("STARTUP "):
            continue
        parts = message.split()
        if len(parts) < 3:
            continue
        try:
            stamp = float(parts[-1])
        except ValueError:
            continue
        trace.append({"name": " ".join(parts[1:-1]), "t_ms": round(stamp - load_started_wall, 1)})

    payload: dict[str, object] = {
        "steps": steps,
        "total_ms": round(1000.0 * (time.perf_counter() - t_start), 1),
        "load_ms": round(load_ms, 1),
        "roots": len(roots),
        "trace": trace,
        "notes": notes,
        "qml_warnings": len(messages),
    }
    if roots:
        root = roots[0]
        payload["mode"] = root.property("mode")
        payload["currentPage"] = root.property("currentPage")
        # 哪些重页面在启动这一刻就已经被建出来了：objectName 是各页自己定的，
        # 这里只查在不在，不碰它们的内部状态。
        from PySide6.QtCore import QObject

        pages = {}
        for name in PAGE_OBJECT_NAMES:
            pages[name] = root.findChild(QObject, name) is not None
        payload["pages"] = pages
    print(MARKER + json.dumps(payload, ensure_ascii=False))
    return 0


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _run_once(args: argparse.Namespace, index: int) -> dict | None:
    command = [sys.executable, str(Path(__file__).resolve()), "--worker",
               "--repeat", "1"]
    if not args.bridge:
        command.append("--no-bridge")
    if args.trace:
        command.append("--trace")
    if args.isolate:
        command.append("--isolate")
    if args.qml is not None:
        command.extend(["--qml", str(args.qml)])
    printable = " ".join(command[2:])
    print(f"[run {index + 1}/{args.repeat}] {printable}", file=sys.stderr)
    completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=args.timeout, check=False)
    if completed.returncode != 0:
        print(completed.stderr or completed.stdout, file=sys.stderr)
        return None
    for line in completed.stdout.splitlines():
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER):])
    print(completed.stderr, file=sys.stderr)
    return None


def _print_table(runs: list[dict]) -> None:
    rows: dict[str, list[float]] = {step: [] for step in STEPS}
    for run in runs:
        for step in STEPS:
            rows[step].append(float(run["steps"].get(step, 0.0)))
    per_run = len(runs)
    print()
    print(f"{'环节':24s} {'中位数':>10s} {'最小':>8s} {'最大':>8s}")
    print("-" * 56)
    for step in STEPS:
        values = rows[step]
        print(f"{step:24s} {_median(values):9.0f}ms {min(values):7.0f}ms {max(values):7.0f}ms")
    print("-" * 56)
    totals = [float(run["total_ms"]) for run in runs]
    loads = [float(run["load_ms"]) for run in runs]
    print(f"{'合计':24s} {_median(totals):9.0f}ms {min(totals):7.0f}ms {max(totals):7.0f}ms")
    print(f"{'其中 load(Main.qml)':24s} {_median(loads):9.0f}ms")
    print(f"（{per_run} 次独立进程冷启动）")

    traces = [run for run in runs if run.get("trace")]
    if traces:
        print()
        print("Main.qml 内部时间线（相对 engine.load 开始；打点按文档顺序，中位数列）:")
        order: list[str] = []
        for run in traces:
            for entry in sorted(run["trace"], key=lambda e: float(e["t_ms"])):
                if entry["name"] not in order:
                    order.append(entry["name"])
        for name in order:
            stamps: list[float] = []
            steps: list[float] = []
            for run in traces:
                timeline = sorted(run["trace"], key=lambda e: float(e["t_ms"]))
                previous = 0.0
                for entry in timeline:
                    if entry["name"] == name:
                        stamps.append(float(entry["t_ms"]))
                        steps.append(float(entry["t_ms"]) - previous)
                        break
                    previous = float(entry["t_ms"])
            if stamps:
                print(f"  {name:34s} {_median(stamps):8.0f}ms   距上一处 {_median(steps):7.0f}ms")

    created: list[str] = []
    for name in PAGE_OBJECT_NAMES:
        if any(run.get("pages", {}).get(name) for run in runs):
            created.append(name)
    print()
    print("启动时就已经建出来的大件：" + ("、".join(created) if created else "（无）"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Main.qml 启动耗时基准（离屏）")
    parser.add_argument("--repeat", type=int, default=3, help="独立进程次数，取中位数（默认 3）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出中位数与每次原始数据")
    parser.add_argument("--trace", action="store_true",
                        help="让 Main.qml 打启动点（默认关闭，正常启动零成本）")
    parser.add_argument("--no-bridge", dest="bridge", action="store_false",
                        help="不建 NativeBridge（与旧探针对齐，界面会报 bridge 未定义）")
    parser.add_argument("--isolate", action="store_true",
                        help="把外观/MCP/渠道配置指到临时目录，基准不读本机用户设置")
    parser.add_argument("--timeout", type=float, default=180.0, help="单次运行的超时秒数")
    parser.add_argument("--qml", type=Path, default=None,
                        help="换一个入口文件量（默认 Main.qml；二分排查时用变体拷贝）")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.repeat < 1:
        parser.error("--repeat 至少为 1")
    if args.worker:
        return _worker(args)

    runs = []
    for index in range(args.repeat):
        result = _run_once(args, index)
        if result is None:
            return 2
        runs.append(result)

    if args.json:
        medians = {}
        for step in STEPS:
            medians[step] = round(_median([float(r["steps"].get(step, 0.0)) for r in runs]), 1)
        medians["total_ms"] = round(_median([float(r["total_ms"]) for r in runs]), 1)
        print(json.dumps({
            "repeat": args.repeat,
            "bridge": args.bridge,
            "trace": args.trace,
            "median": medians,
            "load_ms_median": round(_median([float(r["load_ms"]) for r in runs]), 1),
            "runs": runs,
        }, ensure_ascii=False, indent=2))
        return 0

    _print_table(runs)
    for run in runs:
        for note in run.get("notes", []):
            print("注意：" + note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
