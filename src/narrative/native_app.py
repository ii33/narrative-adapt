"""Native Qt Quick entry point for graph projects.

Qt chooses its normal RHI backend; this module records what the visible window
actually initialized. An offscreen or software test is never counted as proof
of hardware acceleration.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

QML_PATH = Path(__file__).with_name("qml") / "Main.qml"



def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="叙事图原生工作台（Qt Quick）")
    parser.add_argument("--project", help="启动后打开这个工程入口文件（.nproj）")
    parser.add_argument("--render-log", type=Path, help="记录真实 Qt Quick 渲染后端及 Qt RHI 适配器日志")
    parser.add_argument("--qa-snapshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--qa-exit-after-seconds", type=float, help=argparse.SUPPRESS)
    return parser


def _render_log_path(supplied: Path | None) -> Path:
    if supplied is not None:
        return supplied.resolve()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return (Path("data") / "native-logs" / f"qt-{stamp}-{os.getpid()}.log").resolve()


def _show_startup_error(app: Any, message: str) -> None:
    """Keep a visible error for windowless launches without loading Qt Widgets."""
    from PySide6.QtQml import QQmlApplicationEngine

    error_engine = QQmlApplicationEngine()
    error_engine.rootContext().setContextProperty("startupError", message)
    error_engine.loadData(b"""
import QtQuick
import QtQuick.Window
Window {
    width: 640; height: 240; visible: true
    title: "Startup error"
    color: "#f4f7fb"
    Text {
        anchors.fill: parent; anchors.margins: 24
        text: startupError; color: "#16233a"
        wrapMode: Text.WrapAnywhere
    }
}
""")
    if error_engine.rootObjects():
        app.exec()
    else:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.qa_exit_after_seconds is not None and args.qa_exit_after_seconds <= 0:
        raise ValueError("QA 窗口时长须为正数")

    # 聊天要拉起 dsh 运行时（控制台程序）：不关掉控制台，每问一句就闪一个终端。
    from .quiet_children import hide_consoles

    hide_consoles()

    # Do not force a backend. Qt Quick selects its RHI API for the platform.
    # 逐帧的 scenegraph / RHI 日志只在 --render-log 时打开：它每帧写若干行到磁盘，
    # 正常使用时纯粹是开销。
    if args.render_log is not None:
        os.environ.setdefault("QSG_INFO", "1")
        os.environ.setdefault("QT_LOGGING_RULES", "qt.scenegraph.general=true;qt.rhi.*=true")
    try:
        from PySide6.QtCore import QTimer, QUrl, qInstallMessageHandler
        from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
        from PySide6.QtQuickControls2 import QQuickStyle
    except ImportError as exc:
        if isinstance(exc, ModuleNotFoundError) and exc.name == "PySide6":
            message = "未安装 PySide6；请在项目目录运行 uv sync --locked --extra desktop。"
        else:
            message = "PySide6 导入失败；请检查下面的原始错误和系统运行库。"
        print(f"{message}\n{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    from .native_bridge import NativeBridge

    app = cast(QGuiApplication, QGuiApplication.instance() or QGuiApplication([sys.argv[0]]))
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        # The offscreen Qt plugin may expose no system fonts on Windows.
        cjk_font = Path("C:/Windows/Fonts/NotoSansSC-VF.ttf")
        if cjk_font.is_file():
            font_id = QFontDatabase.addApplicationFont(str(cjk_font))
            if font_id >= 0:
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families:
                    app.setFont(QFont(families[0]))
    # Fusion 风格：Qt Quick Controls 需要单独切风格，只在 QML 里 import Fusion 没用，
    # 默认风格会让控件完全没有主题。深浅色不在这里钉死——交给下面的
    # bridge.load_appearance()，它按用户偏好（或系统偏好）套上同一份令牌；
    # 在这里先强制一次浅色会把系统的深色偏好盖住，之后「跟随系统」就永远读到浅色。
    QQuickStyle.setStyle("Fusion")
    app.setApplicationName("叙事图工作台")
    render_log = _render_log_path(args.render_log)
    render_log.parent.mkdir(parents=True, exist_ok=True)
    log_lock = threading.Lock()

    def record(message: str) -> None:
        # 写日志永远不能连累调用方：一个满盘或权限问题不该让界面停摆。
        try:
            with log_lock, render_log.open("a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now(UTC).isoformat()} {message}\n")
        except OSError:
            pass

    def qt_message(mode, context, message) -> None:
        category = getattr(context, "category", "qt")
        record(f"qt:{category}:{getattr(mode, 'name', str(mode))}: {message}")

    qInstallMessageHandler(qt_message)
    record(f"startup pid={os.getpid()} qml={QML_PATH}")
    record(f"requested_render_loop={os.environ.get('QSG_RENDER_LOOP', '<Qt platform default>')}")
    record(f"requested_backend={os.environ.get('QSG_RHI_BACKEND', '<Qt platform default>')}")
    bridge = NativeBridge()
    # 外观要在加载 QML 之前定下来：否则第一帧先按浅色画一遍，再跳成深色。
    bridge.load_appearance()
    record(f"appearance mode={bridge.themeMode} dark={bridge.darkMode}")
    # 慢操作写进日志：界面卡的时候，能看出是数据慢、界面慢，还是整体都慢。
    bridge.slowOperation.connect(lambda kind, ms: record(f"slow {kind} {ms}ms"))
    app.aboutToQuit.connect(bridge.close)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.load(QUrl.fromLocalFile(str(QML_PATH)))
    roots = engine.rootObjects()
    if not roots or not isinstance(roots[0], QQuickWindow):
        record("startup_error=QML root is not a QQuickWindow")
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen" or args.qa_exit_after_seconds is not None:
            print(f"原生界面加载失败；见渲染日志：{render_log}", file=sys.stderr)
        else:
            _show_startup_error(app, f"原生界面加载失败。\n渲染日志：{render_log}")
        bridge.close()
        return 1
    window = roots[0]
    window.show()
    # 从脚本（Start-Process / 快捷方式）启动时窗口不一定被激活：那样它只画第一帧，
    # 之后一直停在那儿——看着像「卡死」，其实程序在跑。主动激活 + 提到前面。
    window.raise_()
    window.requestActivate()
    record(f"window_shown activated={window.isActive()}")
    if args.project:
        bridge.openProject(args.project)

    # 看门狗：有操作超过 2 秒没回来就记一行。界面停在「处理中」时，
    # 这行直接说明卡在哪个操作上；同一批只记一次，不刷日志。
    watchdog = QTimer()
    watchdog.setInterval(2000)
    reported: set[tuple[str, ...]] = set()

    def watch_pending() -> None:
        pending = tuple(sorted(bridge.pendingOperations()))
        if pending and pending not in reported:
            reported.add(pending)
            record("stuck " + ",".join(pending))
        elif not pending:
            reported.clear()

    watchdog.timeout.connect(watch_pending)
    watchdog.start()

    probe = QTimer()
    probe.setInterval(100)
    probe_attempts = 0

    def capture_window() -> None:
        if args.qa_snapshot is None:
            return
        target = args.qa_snapshot.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        # grabToImage is asynchronous and captures the actual Qt Quick scene;
        # QQuickWindow.grabWindow() can block with a D3D render thread.
        grab = cast(Any, window.contentItem().grabToImage())
        if grab is None:
            record(f"qa_snapshot={target} saved=false reason=grab_unavailable")
            return

        def save_grab() -> None:
            record(f"qa_snapshot={target} saved={grab.saveToFile(str(target))}")

        grab.ready.connect(save_grab)

    def report_graphics() -> None:
        nonlocal probe_attempts
        probe_attempts += 1
        if not window.isSceneGraphInitialized():
            if probe_attempts >= 150:
                record("actual_graphics_api=unknown scene_graph_initialized=false hardware_acceleration=unverified")
                probe.stop()
            return
        api = window.rendererInterface().graphicsApi()
        rhi_based = QSGRendererInterface.isApiRhiBased(api)
        record(f"actual_graphics_api={api.name} scene_graph_initialized=true rhi_based={rhi_based}")
        record("hardware_adapter=inspect_qt_rhi_adapter_log; D3D11 alone does not prove a physical GPU")
        probe.stop()
        QTimer.singleShot(1000, capture_window)

    probe.timeout.connect(report_graphics)
    probe.start()
    if args.qa_exit_after_seconds is not None:
        QTimer.singleShot(int(args.qa_exit_after_seconds * 1000), app.quit)
    code = app.exec()
    record(f"exit_code={code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
