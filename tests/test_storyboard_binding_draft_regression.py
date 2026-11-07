"""Real QML binding actions must not be reverted by a cached script draft."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from test_screenplay_ui import render_ready, visuals
from test_static_check_ui import click, plain
from test_storyboard_asset_elements_ui import open_create_element, seed


def invoke(target, method, *arguments):
    from PySide6.QtCore import Q_ARG, QMetaObject

    assert QMetaObject.invokeMethod(
        target, method, *(Q_ARG("QVariant", value) for value in arguments)
    )


def page_function(name):
    """Use the real desk command functions without loading unrelated media UI."""
    from narrative.native_app import QML_PATH

    source = QML_PATH.with_name("StoryboardPage.qml").read_text(encoding="utf-8")
    start = source.index("function " + name + "(")
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


@pytest.fixture(scope="module")
def qt_app():
    import os

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_QUICK_BACKEND"] = "software"
    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Fusion"
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture
def binding_desk(tmp_path, monkeypatch, qt_app):
    from PySide6.QtCore import QCoreApplication, QEvent, QUrl
    from PySide6.QtQml import QQmlApplicationEngine

    from narrative.native_app import QML_PATH
    from narrative.native_bridge import NativeBridge
    from narrative.operations import Operations

    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    entry = tmp_path / "binding.nproj"
    ops = Operations.create_graph_project(entry)
    _screenplay, unit, board, _asset = seed(ops)
    bridge = NativeBridge()
    errors, results = [], []
    bridge.errorOccurred.connect(lambda kind, error: errors.append((kind, error)))
    bridge.resultReady.connect(lambda kind, result: results.append((kind, result)))
    monkeypatch.setattr(
        bridge, "_assistant_turn", lambda *a, **kw: pytest.fail("must not call a model")
    )

    def wait(check):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            qt_app.processEvents()
            if check():
                return
            time.sleep(.01)
        raise AssertionError(f"Binding UI did not settle: {errors=} results={results[-3:]}")

    def settle():
        for _ in range(8):
            qt_app.processEvents()
            time.sleep(.01)

    bridge.openProject(str(entry))
    wait(lambda: any(kind == "project" for kind, _ in results))
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    functions = "\n".join(page_function(name) for name in (
        "request", "save", "reload", "createMissingAssets", "createMissingAssetRow",
        "selectAllAssetShots", "toggleAssetShot",
    ))
    # The production component and NativeBridge/Operations execute unchanged.
    # Only the surrounding desk/selection is small, so QtMultimedia and the
    # unrelated assistant runtime are not needed by this binding regression.
    source = r'''
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import __QML_DIRECTORY__ as Production
ApplicationWindow {
    id:window;width:1500;height:1000;visible:true
    QtObject {
        id:testApp
        property var project:({path:__PROJECT_PATH__})
        property string mode:"workspace"
        property var theme:({ink:"#101010",muted:"#666666",warn:"#986800",bad:"#bb0000"})
        property int nextToken:0
        function nextStoryboardToken(){return ++nextToken}
    }
    Item {
        id:desk;objectName:"testBindingDesk";anchors.fill:parent
        property var app:testApp
        property var t:app.theme
        property string unitId:__UNIT_ID__
        property var board:__BOARD__
        property var shots:board.shots||[]
        property int shotIndex:1
        property var shot:shots[shotIndex]||({})
        property var scene:({unit:{blocks:[]}})
        property bool ready:board.unit_id===unitId
        property bool approved:false
        property bool busy:false
        property bool playing:false
        property bool scriptReadOnly:false
        property int deskMode:0
        property int token:0
        property string message:""
        property bool focusNew:false
        property string focusShot:""
        property int focusOffset:0
        property int localTime:0
        property bool assetAnalysisPending:false
        property bool missingAssetsPending:false
        property int missingAssetsToken:0
        property var selectedAssetShots:[]
        property var assetShotIds:selectedAssetShots
        function seconds(ms){return String(ms/1000)}
        function startOf(index){return index*1000}
        function seek(ms){shotIndex=ms>=1000?1:0}
        __FUNCTIONS__
        Connections {
            target:bridge
            function onResultReady(kind,payload){
                if(kind!=="storyboard"||payload.token!==desk.token)return
                desk.busy=false;desk.missingAssetsPending=false
                if(payload.error){desk.message=payload.error;return}
                if(payload.result?.status==="staged")return
                desk.board=payload.operation==="create_missing_storyboard_assets"?payload.result.board:payload.result
            }
        }
        Production.StoryboardScriptView {anchors.fill:parent;desk:parent}
        Button {
            objectName:"storyboardCreateMissingAssets";text:"建立缺失资产";z:20
            anchors.top:parent.top;anchors.right:parent.right
            onClicked:desk.createMissingAssets(desk.assetShotIds)
        }
    }
}
'''
    for key, value in {
        "__QML_DIRECTORY__": json.dumps(QUrl.fromLocalFile(str(QML_PATH.parent)).toString()),
        "__PROJECT_PATH__": json.dumps(str(entry)),
        "__UNIT_ID__": json.dumps(unit),
        "__BOARD__": json.dumps(board, ensure_ascii=False),
        "__FUNCTIONS__": functions,
    }.items():
        source = source.replace(key, value)
    engine.loadData(source.encode(), QUrl.fromLocalFile(str(tmp_path / "BindingHarness.qml")))
    assert engine.rootObjects()
    window = engine.rootObjects()[0]
    desk = window.findChild(object, "testBindingDesk")
    script = desk.findChild(object, "storyboardScriptView")
    shot_id = board["shots"][1]["id"]
    wait(lambda: script.property("loadedId") == shot_id)
    ui = SimpleNamespace(app=qt_app, bridge=bridge, window=window, wait=wait, settle=settle, errors=errors, engine=engine)
    settle()
    try:
        yield ui, ops, unit, desk, script, shot_id
        assert not errors
    finally:
        bridge.close()
        window.close()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qt_app.processEvents()


def set_elements(ui, script, rows):
    # JSON creates genuine JavaScript nulls, unlike Python QVariantMap None
    # entries, which Qt may expose as undefined properties.
    ui.engine.globalObject().setProperty("draftScript", ui.engine.newQObject(script))
    result = ui.engine.evaluate("draftScript.elements=" + json.dumps(rows, ensure_ascii=False))
    assert not result.isError(), result.toString()


def edit_draft(ui, script, *, add_element=True):
    script.findChild(object, "storyboardScriptDescription").setProperty("text", "未保存的画面说明")
    script.findChild(object, "storyboardScriptLanguage").setProperty("text", "未保存的镜头语言")
    script.findChild(object, "storyboardScriptSoundNotes").setProperty("text", "未保存的声音说明")
    rows = plain(script.property("elements"))
    rows[0]["usage"] = "未保存的本镜用途"
    if add_element:
        rows.append({"kind": "prop", "name": "新增草稿元素", "usage": "稍后确定", "asset_id": None})
    set_elements(ui, script, rows)
    return rows


def choose_existing(ui, script, shot_id, asset_id):
    invoke(script, "chooseElement", shot_id, 0, "prop")
    dialog = script.findChild(object, "storyboardVisualAssetPicker")
    ui.wait(lambda: dialog.property("opened") and bool(plain(dialog.property("assets"))))
    render_ready(ui)
    option = next(
        item for item in visuals(dialog.property("contentItem"))
        if item.objectName() == "storyboardElementAssetOption"
        and item.property("assetId") == asset_id
    )
    click(ui, option)


def bind_from_left(ui, ops, desk, script, shot_id, method):
    if method == "existing":
        asset = ops.save_asset({"category": "item", "title": "已有铜钥匙", "domain": "adaptation"})
        choose_existing(ui, script, shot_id, asset["id"])
    elif method == "detailed":
        _script, dialog = open_create_element(ui, desk, shot_id)
        script.findChild(object, "storyboardCreateElementAssetTitle").setProperty("text", "详细建立的钥匙")
        click(ui, script.findChild(object, "storyboardSaveElementAsset"))
        ui.wait(lambda: not dialog.property("opened"))
    elif method == "missing_row":
        assert all(desk.property(key) == value for key, value in {
            "ready": True, "busy": False, "approved": False,
            "assetAnalysisPending": False, "missingAssetsPending": False,
        }.items()), {key: desk.property(key) for key in (
            "ready", "busy", "approved", "assetAnalysisPending", "missingAssetsPending", "board", "unitId"
        )}
        old_token = desk.property("token")
        invoke(desk, "createMissingAssetRow", shot_id, 0)
        assert desk.property("token") != old_token
    else:
        invoke(desk, "selectAllAssetShots", True)
        click(ui, desk.findChild(object, "storyboardCreateMissingAssets"))


def assert_text_draft(script):
    for name, text in (
        ("storyboardScriptDescription", "未保存的画面说明"),
        ("storyboardScriptLanguage", "未保存的镜头语言"),
        ("storyboardScriptSoundNotes", "未保存的声音说明"),
    ):
        assert script.findChild(object, name).property("text") == text


@pytest.mark.parametrize("method", ["existing", "detailed", "missing_row", "missing_selected"])
@pytest.mark.parametrize("cached", [False, True])
def test_left_binding_survives_save_with_unrelated_unsaved_edits(binding_desk, method, cached):
    ui, ops, unit, desk, script, shot_id = binding_desk
    draft = edit_draft(ui, script)
    if cached:
        invoke(desk, "seek", 0)
        ui.wait(lambda: script.property("loadedId") != shot_id)
    bind_from_left(ui, ops, desk, script, shot_id, method)
    ui.wait(lambda: not desk.property("busy") and bool(plain(desk.property("board"))["shots"][1]["visual_elements"][0]["asset_id"]))
    bound = ops.read_storyboard(unit)["shots"][1]["visual_elements"][0]["asset_id"]
    ui.wait(lambda: not desk.property("busy"))
    if cached:
        invoke(desk, "seek", 1100)
    ui.wait(lambda: script.property("loadedId") == shot_id)
    ui.settle()
    rows = plain(script.property("elements"))
    assert rows[0]["asset_id"] == bound
    assert rows[0]["asset_title"] == ops.get_asset(bound)["title"]
    assert rows[0]["usage"] == draft[0]["usage"]
    assert rows[1] == draft[1]
    assert_text_draft(script)
    assert not script.property("elementsConflict")
    assert script.property("loadedRevision") == plain(desk.property("board"))["revision"]
    save = script.findChild(object, "storyboardScriptSave")
    assert save.property("enabled")
    invoke(save, "clicked")
    ui.wait(lambda: not desk.property("busy") and plain(desk.property("board"))["shots"][1]["description"] == "未保存的画面说明")
    stored = ops.read_storyboard(unit)["shots"][1]
    assert stored["visual_elements"] == [
        {"kind": "prop", "name": "钥匙", "usage": "未保存的本镜用途", "asset_id": bound},
        draft[1],
    ]
    assert stored["shot_language"] == "未保存的镜头语言"
    assert stored["sound_notes"] == "未保存的声音说明"
    assert all("_draftIndex" not in row for row in stored["visual_elements"])


@pytest.mark.parametrize("conflict", ["binding", "removed_row", "server_structure"])
def test_conflicting_element_draft_is_preserved_and_cannot_overwrite_binding(binding_desk, conflict):
    ui, ops, unit, desk, script, shot_id = binding_desk
    local_asset = ops.save_asset({"category": "item", "title": "本地草稿钥匙", "domain": "adaptation"})
    remote_asset = ops.save_asset({"category": "item", "title": "左侧选择钥匙", "domain": "adaptation"})
    draft = edit_draft(ui, script, add_element=False)
    if conflict == "binding":
        draft[0]["asset_id"] = local_asset["id"]
    elif conflict == "removed_row":
        draft = [{"kind": "prop", "name": "替代草稿元素", "usage": "手持", "asset_id": None}]
    set_elements(ui, script, draft)
    if conflict == "server_structure":
        board = ops.read_storyboard(unit)
        changed = ops.apply_storyboard_changes(unit, board["revision"], [{
            "op": "update_shot", "shot_id": shot_id, "visual_elements": [
                {"kind": "prop", "name": "钥匙", "usage": "手持", "asset_id": remote_asset["id"]},
                {"kind": "set", "name": "门口", "usage": "背景", "asset_id": None},
            ],
        }])
        invoke(desk, "reload")
        ui.wait(lambda: plain(desk.property("board"))["revision"] == changed["revision"])
    else:
        choose_existing(ui, script, shot_id, remote_asset["id"])
    ui.wait(lambda: script.property("elementsConflict"))
    saved_revision = ops.read_storyboard(unit)["revision"]
    assert plain(script.property("elements")) == draft
    assert_text_draft(script)
    assert not script.findChild(object, "storyboardScriptSave").property("enabled")
    invoke(script, "saveDraft")
    ui.settle()
    assert ops.read_storyboard(unit)["revision"] == saved_revision
    # Both the conflict and its original baseline survive leaving and returning.
    invoke(desk, "seek", 0)
    ui.wait(lambda: script.property("loadedId") != shot_id)
    invoke(desk, "seek", 1100)
    ui.wait(lambda: script.property("loadedId") == shot_id)
    assert script.property("elementsConflict")
    assert plain(script.property("elements")) == draft
    invoke(script.findChild(object, "storyboardReloadDraftElements"), "clicked")
    assert not script.property("elementsConflict")
    assert_text_draft(script)
    assert plain(script.property("elements"))[0]["asset_id"] == remote_asset["id"]
    invoke(script.findChild(object, "storyboardScriptSave"), "clicked")
    ui.wait(lambda: not desk.property("busy") and plain(desk.property("board"))["shots"][1]["description"] == "未保存的画面说明")
    assert ops.read_storyboard(unit)["shots"][1]["visual_elements"][0]["asset_id"] == remote_asset["id"]


@pytest.mark.parametrize("cached", [False, True])
def test_binding_refreshes_untouched_elements_and_keeps_text_draft(binding_desk, cached):
    ui, ops, unit, desk, script, shot_id = binding_desk
    original = plain(script.property("elements"))
    edit_draft(ui, script)
    set_elements(ui, script, original)
    if cached:
        invoke(desk, "seek", 0)
        ui.wait(lambda: script.property("loadedId") != shot_id)
    bind_from_left(ui, ops, desk, script, shot_id, "missing_row")
    ui.wait(lambda: not desk.property("busy"))
    if cached:
        invoke(desk, "seek", 1100)
    ui.wait(lambda: script.property("loadedId") == shot_id)
    ui.settle()
    stored = ops.read_storyboard(unit)["shots"][1]["visual_elements"][0]
    assert plain(script.property("elements"))[0]["asset_id"] == stored["asset_id"]
    assert plain(script.property("elements"))[0]["usage"] == original[0]["usage"]
    assert_text_draft(script)
    assert not script.property("elementsConflict")
    invoke(script.findChild(object, "storyboardScriptSave"), "clicked")
    ui.wait(lambda: not desk.property("busy"))
    assert ops.read_storyboard(unit)["shots"][1]["visual_elements"][0] == stored
