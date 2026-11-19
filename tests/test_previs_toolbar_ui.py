"""预演页工具条上的两个直接入口：可读性开关与铺布景。

这两件事都不在页面里算数：可读性只是把四项布尔下发给预演页，铺布景只是
「取本场绑定 → 调工具层」。所以这里两头都验——桥槽位的失败语义与透传
（假宿主，不联网、不起 WebEngine），以及工具条上真的有这两个按钮、该禁用时禁用、
点了会按顺序走到桥槽位（成功一句回执、失败一句原因，都不弹窗）。

不起真预演台的原因：本用例验的是工具条接线，不是三维视口。离屏跑 WebEngine 既慢又
依赖构建产物，所以把「预演台可用」固定成不可用——那正是「页面未就绪」这条分支，
工具条在该状态下该说什么、该禁用什么，才是这里要盯的。
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from screenplay_scene_fixture import seed
from test_chat_scroll import transcript_shell
from test_static_check_ui import click

from narrative import previs_host, previs_workspace, project_catalog
from narrative.native_bridge import NativeBridge
from narrative.operations import Operations

pytestmark = pytest.mark.ui

# 页面 getScene 的形状（只列本用例用到的键）：两个盒体 + 一台机位，机位也在 entities 里。
PAGE_SCENE = {
    "sceneId": "scene-1", "name": "咖啡馆", "duration_ms": 12000, "fps": 24,
    "aspect": "16:9", "revision": 7,
    "room": {"width": 6.0, "depth": 8.0, "height": 3.0},
    "entities": [
        {"id": "box-1", "kind": "prop", "name": "桌子", "position": [0, 0.5, 0],
         "rotation": [0, 0, 0], "scale": [1, 1, 1], "height": 0.75},
        {"id": "actor-1", "kind": "actor", "name": "安德烈", "position": [1, 0, 1]},
        {"id": "cam-1", "kind": "camera", "name": "机位A", "position": [0, 1.6, -3],
         "camera": {"focal": 35, "target": [1, 1, 1]}},
    ],
    "cuts": [{"time_ms": 0, "cameraId": "cam-1"}],
}


class FakeCaller:
    """假页面调用器：只记下发过的方法与参数，可指定回执或抛错。

    setReadability 还没有 Python 侧包装（``previs_host`` 不归这一路改），桥走的是
    调用器的 ``call``——替身暴露的也是 call，与将来真宿主补齐后的名字一致。
    """

    def __init__(self, reply=None, error=""):
        self.calls = []
        self.reply = {"ok": True} if reply is None else reply
        self.error = error

    def call(self, method, payload):
        self.calls.append((method, payload))
        if self.error:
            raise previs_host.PrevisHostError(self.error)
        return self.reply


class FakeReader:
    """假页面读取器：``get_scene`` 回一份页面形状的场景（工具条的摘要就吃这一份）。"""

    def __init__(self, scene=None):
        self.scene = PAGE_SCENE if scene is None else scene

    def get_scene(self):
        return self.scene


class FakeOps:
    """假操作层：只做 read_unit_set → apply_location_set 这两步，并记下调用顺序。"""

    def __init__(self, blocks=None, cameras=None, error=""):
        self.calls = []
        self.error = error
        self.blocks = [{"id": "box-1"}, {"id": "box-2"}] if blocks is None else blocks
        self.cameras = [{"camera_id": "cam-1"}] if cameras is None else cameras

    def read_unit_set(self, unit_id):
        self.calls.append(("read_unit_set", unit_id))
        return {"unit_id": unit_id, "status": "bound", "asset_id": "asset-set-1",
                "bound_revision": 3, "message": ""}

    def apply_location_set(self, unit_id):
        self.calls.append(("apply_location_set", unit_id))
        if self.error:
            raise ValueError(self.error)
        return {"unit_id": unit_id, "asset_id": "asset-set-1", "revision": 4,
                "blocks": self.blocks, "cameras": self.cameras}


@contextmanager
def bare_bridge(tmp_path, monkeypatch):
    """只起一个空桥：这几个槽位与页面无关，也不必（没法）起 QML/WebEngine。"""
    widgets = pytest.importorskip("PySide6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    app = widgets.QApplication.instance() or widgets.QApplication([])
    bridge = NativeBridge()
    try:
        yield bridge, app
    finally:
        bridge.close()


def open_page(ui):
    """切到预演页。预演台在测试里固定成不可用（见模块说明），页面因此停在「未就绪」。"""
    ui.window.setProperty("currentPage", "previs")
    page = ui.window.findChild(object, "previsPage")
    assert page is not None, "Main.qml 里没有接入预演页（objectName 应为 previsPage）"
    ui.settle()
    return page


def test_slots_report_a_missing_page_instead_of_raising(tmp_path, monkeypatch):
    with bare_bridge(tmp_path, monkeypatch) as (bridge, _app):
        # 页面没就绪：四项开关按宿主默认（全开）画出来，不报错——这是可显示的状态。
        flags = bridge.previsReadability()
        assert flags == {"persons": True, "roomOutlines": True, "kindColors": True,
                         "labels": True, "ok": True, "error": ""}

        # 改不成：ok:false + 可读原因（页面还没接管），状态原地不动，不抛到界面。
        refused = bridge.setPrevisReadability({"persons": False})
        assert refused["ok"] is False
        assert "预演页还没就绪" in refused["error"]
        assert refused["applied"] == {"persons": True, "roomOutlines": True,
                                      "kindColors": True, "labels": True}

        # 这页是哪一场：页面没就绪就问不到，给原因而不是空对象。
        scene = bridge.previsSceneSet()
        assert scene["ok"] is False and scene["error"]
        assert (scene["entity_count"], scene["camera_count"], scene["revision"]) == (0, 0, 0)

        # 没有当前场：不去碰工程，也不去碰工具层。
        empty = bridge.applySceneSet("")
        assert empty["ok"] is False and "当前场" in empty["error"]
        assert empty["blocks"] == [] and empty["cameras"] == [] and empty["asset_id"] == ""


def test_set_readability_forwards_the_four_flags_to_the_page(tmp_path, monkeypatch):
    caller = FakeCaller()
    monkeypatch.setattr(previs_host, "installed", lambda: caller)
    with bare_bridge(tmp_path, monkeypatch) as (bridge, _app):
        result = bridge.setPrevisReadability({"persons": False, "roomOutlines": False})
        assert result["ok"] is True and result["error"] == ""
        # 一次调用、四个键全给：页面不做默认值推测，宿主这边先把缺的补齐。
        assert caller.calls == [("setReadability", {"persons": False, "roomOutlines": False,
                                                    "kindColors": True, "labels": True})]
        assert result["applied"] == caller.calls[0][1]
        # 宿主记着它：工具条下次打开面板画的就是这个状态。
        assert bridge.previsReadability()["persons"] is False

        # 页面回执里带状态就以页面为准（页面可能自己归一化过）。
        caller.reply = {"ok": True, "state": {"labels": False}}
        second = bridge.setPrevisReadability({"labels": True})
        assert second["applied"] == {"persons": False, "roomOutlines": False,
                                     "kindColors": True, "labels": False}

        # 四个键以外的东西：既不改也不发请求（发过去页面只会静默丢掉）。
        sent = len(caller.calls)
        ignored = bridge.setPrevisReadability({"unknown": True})
        assert ignored["ok"] is False and "四项" in ignored["error"]
        assert len(caller.calls) == sent

        # 页面报错：ok:false + 原文，状态不前进（开关不会假装改成了）。
        caller.error = "预演页 setReadability 超时"
        failed = bridge.setPrevisReadability({"persons": True})
        assert failed["ok"] is False and "超时" in failed["error"]
        assert failed["applied"]["persons"] is False


def test_scene_set_summary_splits_boxes_from_cameras(tmp_path, monkeypatch):
    monkeypatch.setattr(previs_host, "installed", lambda: FakeReader())
    with bare_bridge(tmp_path, monkeypatch) as (bridge, _app):
        scene = bridge.previsSceneSet()
        assert scene["ok"] is True and scene["error"] == ""
        assert scene["scene_id"] == "scene-1" and scene["name"] == "咖啡馆"
        # 机位在页面里也是实体，但摘要要分开数：混着数会让人以为多放了箱子。
        assert (scene["entity_count"], scene["camera_count"]) == (2, 1)
        assert scene["room"] == {"width": 6.0, "depth": 8.0, "height": 3.0}
        assert scene["revision"] == 7


def test_toolbar_exposes_readability_and_set_buttons(tmp_path, monkeypatch):
    from PySide6.QtCore import QPointF

    # 预演台固定成不可用：工具条因此停在「页面未就绪」，正是要验的那条分支。
    monkeypatch.setattr(previs_workspace, "availability",
                        lambda workspace=None: (False, "测试里不启动预演台"))
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops = Operations.open(ui.bridge._project_path)
        _screenplay_id, segment = seed(ops)
        unit_id = segment["units"][0]["id"]
        page = open_page(ui)

        bar = page.findChild(object, "previsBar")
        assert bar is not None
        # 旧按钮一个都没少，而且文字没变：场景下拉 + 保存 + 导出。
        assert page.findChild(object, "previsScene") is not None
        assert page.findChild(object, "previsSave").property("text") == "保存"
        assert page.findChild(object, "previsExport").property("text") == "导出"
        # 窗口控件还在同一行右边：新按钮都在左侧区域，不抢它们的位置。
        toggle = page.findChild(object, "assistantToggle")
        assert toggle is not None

        # 两个新入口都在，页面未就绪时都点不动（可读性没人接、铺布景没有当前场）。
        readability = page.findChild(object, "previsReadability")
        apply_set = page.findChild(object, "previsApplySet")
        assert readability.property("text") == "可读性"
        assert apply_set.property("text") == "铺布景"
        assert apply_set.mapToItem(page, QPointF(apply_set.width(), 0)).x() <= \
            toggle.mapToItem(page, QPointF(0, 0)).x()
        assert page.property("ready") is False
        assert readability.property("enabled") is False
        assert apply_set.property("enabled") is False
        assert "当前场" in page.property("applySetDisabledReason")
        # 这页是哪一场 + 铺布景回执各占一行文字（工具条右侧不动，文字走状态行）。
        summary = page.findChild(object, "previsSceneSummary")
        assert page.findChild(object, "previsStatusBar") is not None
        assert summary.property("text") == "预演页未就绪"

        # 面板里四个开关 + 全开/全关；开关状态跟着页面的 readability 重画。
        assert page.findChild(object, "previsReadabilityPopup") is not None
        switches = {name: page.findChild(object, name) for name in
                    ("previsReadabilityPersons", "previsReadabilityRoomOutlines",
                     "previsReadabilityKindColors", "previsReadabilityLabels")}
        assert all(item is not None and item.property("checked") is True
                   for item in switches.values())
        assert page.findChild(object, "previsReadabilityAll").property("text") == "全开"
        assert page.findChild(object, "previsReadabilityNone").property("text") == "全关"
        page.setProperty("readability", {"persons": False, "roomOutlines": True,
                                         "kindColors": True, "labels": True})
        ui.settle()
        assert switches["previsReadabilityPersons"].property("checked") is False

        # 真工具（read_unit_set / apply_location_set）：这一场还没绑布景，
        # 桥就地给一句可读原因——不抛、不弹窗，也不写任何东西。
        # 放在 monkeypatch 之前：这一句要的就是真工具，不是假的操作层。
        real = ui.bridge.applySceneSet(unit_id)
        assert real["ok"] is False and "布景" in real["error"]
        assert real["blocks"] == [] and real["cameras"] == []

        # 没有当前场：按钮禁用，点下去也不该发出任何请求。
        idle = FakeOps()
        monkeypatch.setattr(project_catalog, "open_graph_project", lambda *a, **k: idle)
        click(ui, apply_set)
        assert idle.calls == []
        note = page.findChild(object, "previsToolbarNote")

        # 有了当前场（镜头表「定位预演」记下的那一份）：按钮可用。
        ui.window.setProperty("storyboardSelection", {"unit_id": unit_id, "shot_id": "shot-1",
                                                      "unit_label": "第1场", "shot_index": 1})
        ui.wait(lambda: str(page.property("currentUnitId")) == unit_id)
        assert apply_set.property("enabled") is True

        # 布景工具还没落地（另一路在做）：点下去只给一句可读原因，不抛、不弹窗。
        class OpsWithoutSetTools:
            pass

        monkeypatch.setattr(project_catalog, "open_graph_project",
                            lambda *a, **k: OpsWithoutSetTools())
        click(ui, apply_set)
        assert "read_unit_set" in note.property("text")
        assert note.property("visible") is True

        # 工具就绪：顺序固定为「取绑定 → 铺场」，成功一句回执写进工具条。
        ready = FakeOps()
        monkeypatch.setattr(project_catalog, "open_graph_project", lambda *a, **k: ready)
        click(ui, apply_set)
        assert ready.calls == [("read_unit_set", unit_id), ("apply_location_set", unit_id)]
        assert note.property("text") == "已铺 2 个盒体、1 台机位"
        assert note.property("visible") is True

        # 铺不成（这一场还没有预演工程…）：原因就地显示，仍然不弹窗。
        ready.error = "本场还没有预演工程"
        click(ui, apply_set)
        assert note.property("text") == "本场还没有预演工程"
        assert not ui.errors
