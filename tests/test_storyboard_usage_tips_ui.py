"""Usage tips stay readable without a scene or ownership of an editing request."""
import pytest
from test_chat_scroll import transcript_shell
from test_screenplay_ui import render_ready
from test_static_check_ui import click, plain


@pytest.fixture(autouse=True)
def usage_tips_qml_diagnostics():
    from PySide6.QtCore import qInstallMessageHandler

    messages = []
    previous = qInstallMessageHandler(lambda kind, context, text: messages.append(text))
    try:
        yield
    finally:
        qInstallMessageHandler(previous)
    assert not [text for text in messages if "StoryImageUsageTips" in text]


def test_usage_tips_read_copy_scroll_and_small_window_without_scene(tmp_path, monkeypatch):
    from PySide6.QtCore import QMetaObject, Qt
    from PySide6.QtTest import QTest

    from narrative.operations import Operations

    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        before = ops.graph_project_info()["revision"]
        ui.window.setProperty("assistantOpen", False)
        ui.window.setProperty("currentPage", "storyboard")
        desk = ui.window.findChild(object, "storyboardPage")
        ui.wait(lambda: "screenplays" in plain(desk.property("workspace")))
        ui.wait(lambda: not desk.property("busy"))
        assert not desk.property("unitId")
        desk.setProperty("busy", True)
        request_token = desk.property("token")
        button = desk.findChild(object, "storyboardImageUsageTips")
        assert button.property("enabled")
        click(ui, button)
        dialog = desk.findChild(object, "storyboardImageUsageTipsDialog")
        ui.wait(lambda: dialog.property("opened") and not dialog.property("loading"))
        body = dialog.findChild(object, "storyboardImageUsageTipsText")
        expected = ops.read_storyboard_usage_tips()
        assert expected["summary"] in body.property("text")
        assert expected["current_workflow"] in body.property("text")
        assert all(section["title"] in body.property("text") for section in expected["sections"])
        assert body.property("readOnly") and body.property("selectByMouse")
        assert desk.property("busy") and desk.property("token") == request_token
        click(ui, dialog.findChild(object, "storyboardImageUsageTipsCopy"))
        assert ui.app.clipboard().text() == body.property("text")

        ui.window.setMinimumWidth(0)
        ui.window.setMinimumHeight(0)
        ui.window.resize(640, 420)
        render_ready(ui)
        assert dialog.property("width") <= ui.window.width() - 20
        assert dialog.property("height") <= ui.window.height() - 20
        scroll = dialog.findChild(object, "storyboardImageUsageTipsScroll")
        assert scroll.property("contentHeight") > scroll.height()
        click(ui, body)
        QTest.keyClick(ui.window, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
        render_ready(ui)
        assert scroll.property("contentItem").property("contentY") > 0
        QMetaObject.invokeMethod(dialog, "close")
        ui.wait(lambda: not dialog.property("opened"))
        assert desk.property("busy") and desk.property("token") == request_token
        assert ops.graph_project_info()["revision"] == before
        assert not ui.errors
        desk.setProperty("busy", False)
        # Both visible modules expose the same read-only entry, including no scene.
        ui.window.setProperty("currentPage", "director")
        director = ui.window.findChild(object, "directorDeskPage")
        ui.wait(lambda: director.property("visible"))
        QMetaObject.invokeMethod(director.findChild(object, "storyboardImageUsageTipsDialog"), "showTips")
        ui.wait(lambda: director.findChild(object, "storyboardImageUsageTipsDialog").property("opened"))
        QMetaObject.invokeMethod(director.findChild(object, "storyboardImageUsageTipsDialog"), "close")


def test_usage_tips_errors_closed_requests_and_project_switch_are_isolated(tmp_path, monkeypatch):
    from PySide6.QtCore import QMetaObject

    from narrative.operations import Operations

    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        expected = ops.read_storyboard_usage_tips()
        ui.window.setProperty("assistantOpen", False)
        ui.window.setProperty("currentPage", "storyboard")
        desk = ui.window.findChild(object, "storyboardPage")
        ui.wait(lambda: "screenplays" in plain(desk.property("workspace")))
        ui.wait(lambda: not desk.property("busy"))
        held = []
        original = ui.bridge._submit

        def hold(kind, work, *args, **kwargs):
            if kind == "storyboard":
                held.append(work)
            else:
                original(kind, work, *args, **kwargs)

        monkeypatch.setattr(ui.bridge, "_submit", hold)
        desk.setProperty("busy", True)
        editing_token = desk.property("token")
        dialog = desk.findChild(object, "storyboardImageUsageTipsDialog")
        click(ui, desk.findChild(object, "storyboardImageUsageTips"))
        ui.wait(lambda: bool(held))
        token = dialog.property("requestToken")
        assert dialog.property("loading")
        ui.bridge.resultReady.emit("storyboard", {"operation": "read_storyboard_usage_tips", "token": editing_token, "result": expected})
        ui.settle()
        assert dialog.property("loading")
        ui.bridge.resultReady.emit("storyboard", {"operation": "read_storyboard_usage_tips", "token": token, "error": "读取失败：隔离错误样例"})
        ui.wait(lambda: not dialog.property("loading"))
        assert "隔离错误样例" in dialog.property("errorText")
        assert desk.property("busy") and desk.property("token") == editing_token
        QMetaObject.invokeMethod(dialog, "close")
        ui.wait(lambda: not dialog.property("opened"))
        ui.bridge.resultReady.emit("storyboard", {"operation": "read_storyboard_usage_tips", "token": token, "result": expected})
        ui.settle()
        assert not dialog.property("opened") and not dialog.property("contentText")

        click(ui, desk.findChild(object, "storyboardImageUsageTips"))
        new_token = dialog.property("requestToken")
        assert new_token != token
        ui.bridge.resultReady.emit("storyboard", {"operation": "read_storyboard_usage_tips", "token": token, "result": expected})
        ui.settle()
        assert dialog.property("loading")
        ui.bridge.resultReady.emit("storyboard", {"operation": "read_storyboard_usage_tips", "token": new_token, "result": expected})
        ui.wait(lambda: bool(dialog.property("contentText")))
        assert desk.property("busy") and desk.property("token") == editing_token
        other = tmp_path / "other.nproj"
        Operations.create_graph_project(other)
        ui.bridge.openProject(str(other))
        ui.wait(lambda: not dialog.property("opened"))
        assert not dialog.property("contentText")
        ui.bridge.resultReady.emit("storyboard", {"operation": "read_storyboard_usage_tips", "token": new_token, "result": expected})
        ui.settle()
        assert not dialog.property("opened") and not dialog.property("contentText")
