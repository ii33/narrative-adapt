"""MCP 服务器面板：真实 QML 里的列表、保存、坏配置与刷新失败。

配置与缓存一律通过 ``NARRATIVE_MCP_FILE`` 指到 tmp_path，绝不碰真实用户目录；
刷新用 tests/fake_mcp_server.py 这个标准库假服务器，不联网、不装 mcp SDK。
面板的数据全部来自 ``bridge.mcpServers()``，所以这里断言的就是界面真正显示的东西。
"""

from __future__ import annotations

import json
import socket
import sys
import time
from contextlib import contextmanager
from pathlib import Path

FAKE = Path(__file__).with_name("fake_mcp_server.py")


def _value(item, name):
    """QML 属性读成 Python 值：JS 数组/对象读回来可能是 QJSValue，统一折一下。"""
    value = item.property(name)
    return value.toVariant() if hasattr(value, "toVariant") else value


def _visuals(item):
    """按可见树找控件：ListView 的 delegate 不在 QObject 树上，findChild 找不到它。"""
    yield item
    for child in item.childItems():
        yield from _visuals(child)


def _row_button(panel, name):
    return next(item for item in _visuals(panel) if item.objectName() == name)


def _click(ui, item):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest

    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    QTest.mouseClick(ui["window"], Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, point.toPoint())
    ui["settle"]()


@contextmanager
def mcp_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("QT_QUICK_BACKEND", "software")
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    monkeypatch.setenv("NARRATIVE_APPEARANCE_FILE", str(tmp_path / "appearance.json"))
    config = tmp_path / "mcp-servers.json"
    monkeypatch.setenv("NARRATIVE_MCP_FILE", str(config))

    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtWidgets import QApplication

    from narrative import mcp_client, tool_api
    from narrative.native_app import QML_PATH
    from narrative.native_bridge import NativeBridge

    # 会话与工具表都是进程级全局：先收掉上一个用例留下的东西，否则「没有配置时面板为空」
    # 会读到别人的缓存与工具。
    mcp_client.close()
    app = QApplication.instance() or QApplication([])
    bridge = NativeBridge()
    errors: list[tuple[str, str]] = []
    notices: list[str] = []
    changed: list[int] = []
    bridge.errorOccurred.connect(lambda kind, message: errors.append((kind, message)))
    bridge.noticeRaised.connect(notices.append)
    bridge.mcpServersChanged.connect(lambda: changed.append(1))
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.load(QUrl.fromLocalFile(str(QML_PATH)))
    assert engine.rootObjects(), "Main.qml 没有加载出来"
    window = engine.rootObjects()[0]
    page = window.findChild(object, "settingsPage")
    panel = page.findChild(object, "mcpSettingsPanel")
    assert panel is not None, "设置页里没有 MCP 面板"

    def wait(check, message="MCP 面板没有稳定下来"):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            app.processEvents()
            if check():
                return
            time.sleep(0.01)
        raise AssertionError(f"{message}；errors={errors}")

    def settle():
        for _ in range(6):
            app.processEvents()
            time.sleep(0.01)

    def show_panel():
        """打开设置页的「MCP 服务器」分组：不切的话面板不可见，点击落不到控件上。"""
        window.setProperty("mode", "settings")
        page.setProperty("sectionIndex", 2)
        wait(lambda: panel.property("visible"))

    def wait_refreshed(count=1):
        wait(lambda: len(changed) >= count and not panel.property("refreshing"),
             "后台刷新没有回来")

    def open_editor():
        _click(ui, panel.findChild(object, "mcpAddServer"))
        wait(lambda: panel.findChild(object, "mcpServerEditor").property("opened"))

    def fill_editor(**fields):
        for name, text in fields.items():
            panel.setProperty(f"draft{name}", text)

    def save_editor():
        _click(ui, panel.findChild(object, "mcpServerSave"))

    def rows():
        return _value(panel, "servers") or []

    ui = {
        "app": app, "window": window, "page": page, "panel": panel, "bridge": bridge,
        "errors": errors, "notices": notices, "changed": changed, "config": config,
        "wait": wait, "settle": settle, "show_panel": show_panel, "rows": rows,
        "wait_refreshed": wait_refreshed, "open_editor": open_editor,
        "fill_editor": fill_editor, "save_editor": save_editor,
    }
    try:
        yield ui
    finally:
        bridge.close()
        mcp_client.close()
        # 工具表是进程级全局：把本用例注册的外部工具摘掉，别让同进程的其它用例看见它们。
        for name, tool in list(tool_api.TOOLS.items()):
            if getattr(tool, "scope", None) == "mcp":
                tool_api.TOOLS.pop(name, None)


def test_empty_panel_shows_nothing_and_reports_no_error(tmp_path, monkeypatch):
    with mcp_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        assert ui["rows"]() == []
        assert _value(ui["panel"], "loadError") == ""
        assert _value(ui["panel"], "configPath").endswith("mcp-servers.json")
        assert ui["errors"] == []
        # 「没有服务器」是正常状态，不该在磁盘上凭空造出一份配置。
        assert not ui["config"].exists()


def test_save_stdio_server_writes_config_and_panel_reads_it_back(tmp_path, monkeypatch):
    with mcp_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        ui["open_editor"]()
        ui["fill_editor"](
            Name="fake",
            Command=sys.executable,
            # 第三个参数带空格，靠一对双引号护住：保存要按这个切法落盘。
            Args=f'{FAKE} --mode "stdio fake"',
            Env="FAKE_MCP_TOKEN=1\n\n# 空行与注释跳过",
            Cwd=str(tmp_path),
            Timeout="15",
        )
        ui["save_editor"]()
        ui["wait_refreshed"]()

        payload = json.loads(ui["config"].read_text(encoding="utf-8"))
        saved = payload["servers"][0]
        assert saved["name"] == "fake"
        assert saved["transport"] == "stdio"
        assert saved["command"] == sys.executable
        assert saved["args"] == [str(FAKE), "--mode", "stdio fake"]
        assert saved["env"] == {"FAKE_MCP_TOKEN": "1"}
        assert saved["cwd"] == str(tmp_path)
        assert saved["timeout"] == 15
        assert saved["enabled"] is True

        # 界面读回的必须是磁盘上那份：一台服务器、两台工具、没有错误。
        rows = ui["rows"]()
        assert [row["name"] for row in rows] == ["fake"]
        assert rows[0]["transport"] == "stdio"
        assert rows[0]["last_error"] == ""
        assert [tool["name"] for tool in rows[0]["tools"]] == ["echo", "write_note"]
        assert [tool["writes"] for tool in rows[0]["tools"]] == [False, True]
        assert _value(ui["panel"], "loadError") == ""
        assert ui["errors"] == []
        assert any("已保存" in notice for notice in ui["notices"])


def test_broken_config_shows_a_readable_error_instead_of_crashing(tmp_path, monkeypatch):
    config = tmp_path / "mcp-servers.json"
    config.write_text('{"servers": [ 这不是 JSON', encoding="utf-8")
    with mcp_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        message = _value(ui["panel"], "loadError")
        assert str(config) in message
        assert "JSON" in message
        assert ui["rows"]() == []
        # 坏配置不动原文件：界面只显示错误，退出时也不能顺手把它覆盖掉。
        assert config.read_text(encoding="utf-8") == '{"servers": [ 这不是 JSON'


def test_refresh_failure_is_shown_on_that_server_row(tmp_path, monkeypatch):
    config = tmp_path / "mcp-servers.json"
    config.write_text(json.dumps({"version": 1, "servers": [{
        "name": "missing", "transport": "stdio", "command": "narrative-no-such-mcp-server",
        "args": [], "env": {}, "cwd": "", "timeout": 5,
    }]}, ensure_ascii=False), encoding="utf-8")
    with mcp_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        for index in range(1, 4):
            _click(ui, ui["panel"].findChild(object, "mcpTestServers"))
            ui["wait_refreshed"](index)

        rows = ui["rows"]()
        assert len(rows) == 1
        assert "启动失败" in rows[0]["last_error"]
        # 单台失败只落在这一行，不是整个面板的错误（其它服务器和本地工具照常）。
        assert all(kind != "mcp" for kind, _message in ui["errors"])
        assert "missing" in _value(ui["panel"], "message")


def test_editing_a_row_reopens_its_saved_values(tmp_path, monkeypatch):
    config = tmp_path / "mcp-servers.json"
    config.write_text(json.dumps({"version": 1, "servers": [{
        "name": "fake", "transport": "stdio", "command": sys.executable,
        "args": [str(FAKE), "--mode", "stdio fake"], "env": {"TOKEN": "值"},
        "cwd": str(tmp_path), "timeout": 15,
    }]}, ensure_ascii=False), encoding="utf-8")
    with mcp_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        _click(ui, _row_button(ui["panel"], "mcpServerRowEdit"))
        ui["wait"](lambda: ui["panel"].findChild(object, "mcpServerEditor").property("opened"))

        # 参数按空格拼回一行、环境变量按「每行一条」摊开：改完直接保存就是原来那份配置。
        assert _value(ui["panel"], "draftName") == "fake"
        assert _value(ui["panel"], "draftTransport") == "stdio"
        assert _value(ui["panel"], "draftArgs").split()[:3] == [str(FAKE), "--mode", "stdio"]
        assert _value(ui["panel"], "draftEnv") == "TOKEN=值"
        assert _value(ui["panel"], "draftCwd") == str(tmp_path)
        assert float(_value(ui["panel"], "draftTimeout")) == 15


def test_http_server_is_saved_and_connects_like_stdio(tmp_path, monkeypatch):
    """http 服务器现在真的会连：配置能保存，连不上时只把失败原因落在这一台。"""
    config = tmp_path / "mcp-servers.json"
    # 先占一个本机端口再放开：地址合法但没人监听，连起来必失败——不碰 DNS 与外网。
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    config.write_text(json.dumps({"version": 1, "servers": [{
        "name": "web", "transport": "http", "url": f"http://127.0.0.1:{port}/mcp", "timeout": 2,
    }]}, ensure_ascii=False), encoding="utf-8")
    with mcp_shell(tmp_path, monkeypatch) as ui:
        ui["show_panel"]()
        rows = ui["rows"]()
        assert rows[0]["transport"] == "http"
        assert rows[0]["url"] == f"http://127.0.0.1:{port}/mcp"
        # 不再有「暂不支持」这类静态错误：能不能连要连了才知道。
        assert rows[0]["error"] == ""

        _click(ui, ui["panel"].findChild(object, "mcpTestServers"))
        ui["wait_refreshed"]()
        rows = ui["rows"]()
        assert rows[0]["transport"] == "http"
        assert rows[0]["last_error"], "连不上应当有失败原因"
        assert "urllib" not in rows[0]["last_error"].lower()


def test_deleting_a_server_also_removes_its_tools(tmp_path, monkeypatch):
    config = tmp_path / "mcp-servers.json"
    config.write_text(json.dumps({"version": 1, "servers": [{
        "name": "fake", "transport": "stdio", "command": sys.executable,
        "args": [str(FAKE)], "env": {}, "cwd": "", "timeout": 15,
    }]}, ensure_ascii=False), encoding="utf-8")
    with mcp_shell(tmp_path, monkeypatch) as ui:
        from narrative import tool_api

        ui["show_panel"]()
        _click(ui, ui["panel"].findChild(object, "mcpRefreshServers"))
        ui["wait_refreshed"]()
        assert [name for name, tool in tool_api.TOOLS.items() if tool.scope == "mcp"] == [
            "mcp__fake__echo", "mcp__fake__write_note"]

        _click(ui, _row_button(ui["panel"], "mcpServerRowDelete"))
        ui["wait_refreshed"](2)
        assert ui["rows"]() == []
        assert [name for name, tool in tool_api.TOOLS.items() if tool.scope == "mcp"] == []
        assert json.loads(ui["config"].read_text(encoding="utf-8"))["servers"] == []
