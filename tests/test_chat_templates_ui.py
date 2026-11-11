"""会话模板的界面接线：新建会话选模板、会话标记、「发往」提示与临时覆盖、草稿插入。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


def plain(value):
    return value.toVariant() if hasattr(value, "toVariant") else value


def vfind(root, name):
    """按 objectName 在可视树里找：Popup 里由 Repeater 生成的控件不在 window 的 QObject 链上。"""
    stack = [root]
    while stack:
        item = stack.pop(0)
        try:
            if item.objectName() == name:
                return item
        except RuntimeError:
            continue
        stack.extend(item.childItems())
    return None


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("QT_QUICK_BACKEND", "software")
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    monkeypatch.setenv("NARRATIVE_APPEARANCE_FILE", str(tmp_path / "appearance.json"))
    # 自定义模板写到临时文件：别让开发机上的 ~/.narrative 影响断言。
    monkeypatch.setenv("NARRATIVE_CHAT_TEMPLATES_FILE", str(tmp_path / "chat-templates.json"))

    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtWidgets import QApplication

    from narrative.native_app import QML_PATH
    from narrative.native_bridge import NativeBridge
    from narrative.operations import Operations

    app = QApplication.instance() or QApplication([])
    entry = tmp_path / "chat-templates.nproj"
    Operations.create_graph_project(entry)
    bridge = NativeBridge()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.load(QUrl.fromLocalFile(str(QML_PATH)))
    assert engine.rootObjects()
    window = engine.rootObjects()[0]

    def settle(rounds=12):
        for _ in range(rounds):
            app.processEvents()
            time.sleep(0.01)

    def wait(check, what="界面没到位"):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            app.processEvents()
            if check():
                return
            time.sleep(0.01)
        raise AssertionError(what)

    bridge.openProject(str(entry))
    wait(lambda: window.property("project"), "工程没打开")
    window.setProperty("assistantOpen", True)
    panel = window.findChild(object, "assistantPanel")
    assert panel is not None
    # 打开工程时 Main 会先 enterWorkspace 到 browse，等它落定再切原文页。
    wait(lambda: window.property("currentPage") == "browse", "工作区没进去")
    window.setProperty("currentPage", "source")
    wait(lambda: plain(panel.property("pageId")) == "source", "没停在原文页")
    wait(lambda: len(plain(panel.property("templates"))) >= 3, "模板清单没加载")
    wait(lambda: plain(panel.property("routeDefaultId")) == "extract", "原文页没路由到提取")
    try:
        yield type("Desk", (), {
            "app": app, "window": window, "panel": panel, "bridge": bridge,
            "project": Path(entry), "settle": staticmethod(settle), "wait": staticmethod(wait),
        })()
    finally:
        bridge.close()
        engine.deleteLater()
        app.processEvents()


def click(ui, item):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest

    assert item is not None, "控件不存在"
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    QTest.mouseClick(ui.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point.toPoint())
    ui.settle(20)


def call(panel, name, *args):
    """调面板上的 QML 函数（QML 里 var 参数在元对象上是 QVariant）。"""
    from PySide6.QtCore import Q_ARG, QMetaObject

    QMetaObject.invokeMethod(panel, name, *[Q_ARG("QVariant", value) for value in args])
    time.sleep(0.05)


def vclick(ui, name):
    """等控件真正上屏（宽高都 > 0）再点：弹出层里的行是打开后才实例化的。"""
    item = None

    def found():
        nonlocal item
        item = vfind(ui.window.contentItem(), name)
        return item is not None and item.width() > 0 and item.height() > 0

    ui.wait(found, "找不到可点的控件：" + name)
    click(ui, item)
    return item


def test_new_chat_picker_lists_the_three_builtin_templates(ui):
    click(ui, ui.window.findChild(object, "assistantNew"))
    picker = ui.window.findChild(object, "assistantNewChatPicker")
    assert picker.property("visible") is True
    labels = []
    stack = [picker.property("contentItem")]
    while stack:
        item = stack.pop(0)
        try:
            text = item.property("text")
        except RuntimeError:
            continue
        if text:
            labels.append(str(text))
        stack.extend(item.childItems())
    for name in ("提取", "剧本", "分镜"):
        assert any(("· " + name + " · 工具 ") in text for text in labels), (name, labels)
    # 每项都带一句描述；内置三个的顺序由后端清单保证。
    assert any("读原文、提取事实" in text for text in labels), labels
    templates = plain(ui.panel.property("templates"))
    assert [row["id"] for row in templates if row["builtin"]] == ["extract", "screenplay", "storyboard"]
    assert all(row["tool_count"] > 0 for row in templates if row["builtin"])


def test_new_chat_from_chosen_template_carries_the_template_marker(ui):
    click(ui, ui.window.findChild(object, "assistantNew"))
    ui.panel.setProperty("templateChoice", "screenplay")
    click(ui, ui.window.findChild(object, "assistantNewChatConfirm"))
    ui.wait(lambda: plain(ui.panel.property("chatId")) != "", "没建出新会话")
    ui.wait(lambda: plain(ui.panel.property("chatTemplateName")) == "剧本", "会话没带模板标记")
    marker = ui.window.findChild(object, "assistantChatTemplate")
    assert marker.property("text") == "剧本" and marker.property("visible") is True
    sessions = plain(ui.panel.property("sessions"))
    assert sessions[0]["template_name"] == "剧本"
    combo = ui.window.findChild(object, "assistantSessions")
    assert str(plain(combo.property("model"))[0]).endswith("· 剧本")
    from narrative.chat_store import ChatStore

    frozen = ChatStore(ui.project).template(sessions[0]["id"])
    assert frozen["id"] == "screenplay" and len(frozen["tools"]) > 0


def test_source_page_shows_where_the_next_message_goes(ui):
    label = ui.window.findChild(object, "assistantRoute")
    assert label.property("text") == "发往：提取会话"
    ui.window.setProperty("currentPage", "storyboard")
    ui.wait(lambda: ui.panel.property("routeText") == "发往：分镜会话", "切到分镜页没跟着改")
    ui.window.setProperty("currentPage", "source")
    ui.wait(lambda: ui.panel.property("routeText") == "发往：提取会话", "切回原文页没复原")


def test_temporary_override_drives_one_send_then_falls_back_to_auto(ui, monkeypatch):
    # 覆盖之前：原文页还没有会话，默认去向就是提取。
    assert ui.bridge.chatRouteTemplate("source") == ""
    assert ui.bridge.chatDefaultTemplate("source") == "extract"
    # 「改」入口打得开，弹层里列出模板与已开会话。
    click(ui, ui.window.findChild(object, "assistantRouteChange"))
    picker = ui.window.findChild(object, "assistantRoutePicker")
    assert picker.property("visible") is True
    assert vfind(ui.window.contentItem(), "assistantRouteChoice_storyboard") is not None

    # 覆盖那一步直接调后端而不是点弹层里的行：离屏环境下 Popup 的模态层会吃掉这些
    # Repeater 生成的行的点击（console.log 实测行内 onClicked 从未触发），改成 ListView
    # 也未必解决。弹层本身由截图确认，这里验的是它绑定的同一条调用与后续效果。
    ui.bridge.overrideChatTemplate("source", "storyboard")
    assert ui.bridge.chatRouteTemplate("source") == "storyboard"
    ui.window.setProperty("currentPage", "browse")
    ui.window.setProperty("currentPage", "source")
    ui.wait(lambda: ui.panel.property("routeText") == "发往：分镜会话（临时，发送后恢复）",
            "覆盖没反映到提示上")

    # 发送这一步只验路由：把真正跑模型的那一半换成记录调用（不联网、不超时）。
    sent = []
    monkeypatch.setattr(type(ui.bridge), "_assistant_turn",
                        lambda self, text, image, chat, **kw: sent.append((text, chat)))
    ui.bridge.assistantTurnOnPage("把这场拆成分镜", "[]", "source")
    # 消费即失效：发送后覆盖没了，但会话本身留着，提示回到「会话自己的模板」。
    ui.wait(lambda: ui.bridge.chatRouteTemplate("source") == "", "发送后覆盖没被消费")
    ui.window.setProperty("currentPage", "browse")
    ui.window.setProperty("currentPage", "source")
    # 覆盖没了、面板还没切到新会话（这一条直接调了后端），提示就落回原文页的自动路由。
    ui.wait(lambda: ui.panel.property("routeText") == "发往：提取会话", "发送后没回到自动")
    from narrative.chat_store import ChatStore

    store = ChatStore(ui.project)
    chat_id = sent[0][1]
    frozen = store.template(chat_id) or {}
    assert frozen.get("id") == "storyboard", frozen


def test_template_draft_is_inserted_without_overwriting_what_was_typed(ui):
    prompt = ui.window.findChild(object, "assistantPrompt")
    expected = "请新建一个会话模板：名称（），工具（），用途（）"
    click(ui, ui.window.findChild(object, "assistantNew"))
    vclick(ui, "assistantTemplateDraftInNew")
    ui.wait(lambda: ui.panel.property("draft") == expected, "空输入框没插入草稿")
    assert prompt.property("text") == expected

    # 已经有内容时插在下面，而不是替换掉。
    ui.panel.setProperty("draft", "先写一段已有内容")
    ui.settle()
    click(ui, ui.window.findChild(object, "assistantNew"))
    vclick(ui, "assistantTemplateDraftInNew")
    ui.wait(lambda: plain(ui.panel.property("draft")).count("请新建一个会话模板") == 1, "草稿没插进已有内容")
    assert plain(ui.panel.property("draft")) == "先写一段已有内容" + chr(10) + expected


def test_opened_chat_wins_over_page_route(ui, monkeypatch):
    """用户显式打开的会话优先：切页/发送都不会把他挑的会话换成本页模板的会话。

    这条规则来自用户裁决：页面路由只是「默认发给对应会话」，用户刚在下拉里挑的会话
    是更强的意图。别顺手改成「每次发送都按页面路由」——那会让消息落到他没看的会话里。
    """
    from narrative.chat_store import ChatStore

    click(ui, ui.window.findChild(object, "assistantNew"))
    ui.panel.setProperty("templateChoice", "screenplay")
    click(ui, ui.window.findChild(object, "assistantNewChatConfirm"))
    ui.wait(lambda: plain(ui.panel.property("chatTemplateName")) == "剧本", "没切到剧本会话")
    opened = plain(ui.panel.property("chatId"))

    # 原文页的默认路由是提取，但当前会话是用户刚建的剧本会话。
    assert ui.bridge.chatDefaultTemplate("source") == "extract"
    assert plain(ui.panel.property("routeText")) == "发往：剧本会话"

    sent = []
    monkeypatch.setattr(type(ui.bridge), "_assistant_turn",
                        lambda self, text, image, chat, **kw: sent.append((text, chat)))
    ui.panel.setProperty("draft", "这场戏的调子再暗一点")
    ui.settle()
    send = ui.window.findChild(object, "assistantSend")
    assert send.property("enabled") is True
    click(ui, send)

    ui.wait(lambda: len(sent) == 1, "没发出去")
    assert sent[0][1] == opened, (sent, opened)
    # 没有偷偷开一个「提取」会话。
    store = ChatStore(ui.project)
    assert store.list()[0]["id"] == opened
    assert len(store.list()) == 1
    assert plain(ui.panel.property("routeText")) == "发往：剧本会话"
