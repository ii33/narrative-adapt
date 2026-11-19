"""镜头表（逐镜制作驾驶舱）：桥槽位派生的表格、状态标记、缺口、筛选与详情。

判定全在 Python（read_storyboard / read_asset_needs / 视频回执），界面只负责画。
所以这里两头都验：既点真实界面（行、筛选、详情），也直接核对桥槽位的返回形状——
两边一起才说明「画出来的就是算出来的」。
"""
from __future__ import annotations

import pytest
from PIL import Image
from screenplay_scene_fixture import seed
from test_chat_scroll import transcript_shell
from test_static_check_ui import click, plain, visual_find

from narrative.operations import Operations

pytestmark = pytest.mark.ui

# 三镜的时长加起来 6000ms：总时长与镜号顺序都靠它断言。
SHOTS = [
    {"op": "create_shot", "title": "敲门", "duration_ms": 2000, "description": "安德烈敲窗三下"},
    {"op": "create_shot", "title": "车窗降下", "duration_ms": 1500, "description": "车窗降下，车内传出录音"},
    {"op": "create_shot", "title": "录音", "duration_ms": 2500, "description": "录音仍在播放"},
]

# 框图是结构化记录（机位＋主体归一化 bbox＋景深＋这一帧还缺什么），不是图片。
LAYOUT = {"camera": {"framing": "medium", "lens_mm": 35},
          "objects": [{"asset_id": "asset-1", "bbox": [0.1, 0.2, 0.5, 0.9],
                       "depth": 3.0, "kind": "person"}],
          "unmet_needs": [],
          "source": "manual"}


def prepare(ui):
    """打开工程之后造一场三镜：场次来自 screenplay_scene_fixture，镜头走真实写入。"""
    ops = Operations.open(ui.bridge._project_path)
    screenplay_id, segment = seed(ops)
    unit_id = segment["units"][0]["id"]
    board = ops.apply_storyboard_changes(unit_id, 0, SHOTS)
    assert [shot["title"] for shot in board["shots"]] == ["敲门", "车窗降下", "录音"]
    return ops, screenplay_id, unit_id, board


def open_page(ui):
    """切到镜头表页并等它把表格读出来（场次清单要先走一次 screenplay_workspace）。"""
    ui.window.setProperty("currentPage", "shot_table")
    page = ui.window.findChild(object, "shotTablePage")
    assert page is not None, "Main.qml 里没有接入镜头表页"
    ui.wait(lambda: page.property("unitId") and len(plain(page.property("rows"))) == 3)
    return page


def rows_of(page):
    return plain(page.property("rows"))


def row_delegates(page):
    """行是 ListView 的视觉子节点：按镜号取，不用 findChild（delegate 不在 QObject 树上）。"""
    view = page.findChild(object, "shotTableList")
    view.forceLayout()
    return [item for item in view.property("contentItem").childItems()
            if item.objectName() == "shotTableRow"]


def click_row(ui, page, shot_id):
    row = next(item for item in row_delegates(page) if item.property("shotId") == shot_id)
    click(ui, row)


def test_table_has_one_row_per_shot_in_shot_order(tmp_path, monkeypatch):
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        page = open_page(ui)
        ui.settle()

        rows = rows_of(page)
        # 顺序就是镜头顺序，镜号从 1 起（人读的镜号，不是存储下标）。
        assert [row["index"] for row in rows] == [1, 2, 3]
        assert [row["shot_id"] for row in rows] == [shot["id"] for shot in board["shots"]]
        assert [row["title"] for row in rows] == ["敲门", "车窗降下", "录音"]
        assert [row["duration_ms"] for row in rows] == [2000, 1500, 2500]
        assert rows[0]["summary"] == "安德烈敲窗三下"
        assert rows[2]["summary"] == "录音仍在播放"
        # 一镜一行真的画出来了，不只是数据对。
        assert page.findChild(object, "shotTableList").property("count") == 3
        assert len(row_delegates(page)) == 3
        # 顶栏的总时长是各镜之和
        assert page.findChild(object, "shotTableTotal").property("text") == "总时长 6.00 s"
        # 还没排框：四态全空、缺口与回执都是 0——「什么都没有」也要如实显示，不能隐藏。
        for row in rows:
            assert row["has"] == {"layout": False, "sketch": False, "style": False, "video": False}
            assert row["gap_count"] == 0 and row["receipts"] == 0
            assert row["keyframe_count"] == 0
        assert not ui.errors


def test_layout_mark_turns_on_and_the_filter_leaves_that_shot_out(tmp_path, monkeypatch):
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        page = open_page(ui)

        # 给第二镜排一帧关键帧（框图必须挂在已存在的关键帧上），再写下这一帧的框图。
        second = board["shots"][1]["id"]
        ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "set_keyframe", "shot_id": second, "offset_ms": 0,
             "sketch_path": str(_frame(tmp_path))},
        ])
        board = ops.read_storyboard(unit_id)
        second = board["shots"][1]["id"]
        ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "set_keyframe_layout", "shot_id": second, "offset_ms": 0, "layout": LAYOUT},
        ])
        page.reload()
        ui.wait(lambda: plain(page.property("rows"))[1]["has"]["layout"] is True)

        rows = rows_of(page)
        assert rows[1]["has"]["layout"] is True and rows[1]["keyframe_count"] == 1
        # 框图是结构化记录，不是画出来的那一张：两档要分开报，不能一起变绿。
        assert rows[1]["has"]["sketch"] is False
        assert rows[0]["has"]["layout"] is False

        # 筛选「未出框图」：有框图的那一镜被排除在外，行数从 3 变 2。
        page.setProperty("filterIndex", 1)
        ui.settle()
        filtered = plain(page.property("filteredRows"))
        assert [row["shot_id"] for row in filtered] == [board["shots"][0]["id"], board["shots"][2]["id"]]
        assert page.findChild(object, "shotTableList").property("count") == 2

        # 回到「全部」：三行都回来（筛选只改看哪几行，不改数据）。
        page.setProperty("filterIndex", 0)
        ui.settle()
        assert page.findChild(object, "shotTableList").property("count") == 3
        assert not ui.errors


def test_declared_unmet_need_becomes_a_gap_on_that_shot_only(tmp_path, monkeypatch):
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        page = open_page(ui)
        third = board["shots"][2]["id"]
        ops.declare_asset_needs(unit_id, board["revision"], shot_needs=[
            {"shot_id": third, "needs": [{"kind": "prop", "name": "金属头盔", "note": "录音里的那个人"}]},
        ])
        page.reload()
        ui.wait(lambda: plain(page.property("rows"))[2]["gap_count"] == 1)

        rows = rows_of(page)
        # 需求没落实＝本镜一条缺口；同一场的另外两镜不该跟着涨。
        assert rows[2]["gap_count"] == 1
        assert rows[0]["gap_count"] == 0 and rows[1]["gap_count"] == 0
        # 场级缺口是另一档：混进每一镜会让人以为「每镜都缺」。
        assert plain(page.property("table"))["unit_gap_count"] == 0
        # 缺口详情要能说出缺口是哪一种、叫什么（详情只讲选中的那一镜）。
        click_row(ui, page, third)
        ui.wait(lambda: page.property("selectedShotId") == third)
        detail = plain(page.property("detail"))
        assert detail["ok"] and [gap["gap"] for gap in detail["gaps"]] == ["unresolved"]
        assert detail["gaps"][0]["name"] == "金属头盔"
        assert visual_find(page, "shotDetailGapRow") is not None

        # 筛选「缺口未落实」只剩这一镜。
        page.setProperty("filterIndex", 3)
        ui.settle()
        assert [row["shot_id"] for row in plain(page.property("filteredRows"))] == [third]
        assert not ui.errors


def test_selecting_a_row_shows_that_shots_timeline_and_keyframe_plan(tmp_path, monkeypatch):
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        first = board["shots"][0]["id"]
        ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "set_keyframe", "shot_id": first, "offset_ms": 0,
             "sketch_path": str(_frame(tmp_path))},
            {"op": "add_timeline_keyframe", "shot_id": first, "offset_ms": 1000,
             "moment_name": "抬手", "description": "抬手敲门"},
        ])
        board = ops.read_storyboard(unit_id)
        ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "set_keyframe_layout", "shot_id": first, "offset_ms": 0, "layout": LAYOUT},
        ])
        page = open_page(ui)

        click_row(ui, page, board["shots"][2]["id"])
        ui.wait(lambda: page.property("selectedShotId") == board["shots"][2]["id"])
        title = visual_find(page, "shotDetailTitle")
        ui.wait(lambda: title.property("text").startswith("3 · "))

        click_row(ui, page, first)
        ui.wait(lambda: page.property("selectedShotId") == first)
        detail = plain(page.property("detail"))
        assert detail["ok"] and detail["shot"]["id"] == first
        # 关键帧计划是「时刻＋命名＋描述」，界面上得真的出现这三样。
        plan = visual_find(page, "shotDetailKeyframeRow")
        ui.wait(lambda: "抬手" in plan.property("text"))
        assert "1.00 s" in plan.property("text") and "抬手敲门" in plan.property("text")
        assert [(row["offset_ms"], row["moment_name"], row["description"])
                for row in detail["timeline"]["keyframes"]] == [(1000, "抬手", "抬手敲门")]
        # 框图清单按时刻给出：0 时刻那一张就是下面 shotLayout 拿到的同一份记录。
        assert [row["offset_ms"] for row in detail["layouts"]] == [0]
        frame = plain(page.property("frame"))
        assert detail["layouts"][0]["hash"] == frame["hash"]
        # 时间轴标尺、框图记录与草图预览都按选中镜来：这一镜零时刻两者都有。
        assert frame["ok"] is True and frame["frame_offset_ms"] == 0
        assert frame["hash"] and frame["objects"][0]["asset_id"] == "asset-1"
        assert frame["sketch_url"] and frame["camera"]["framing"] == "medium"
        assert visual_find(page, "shotDetailRuler") is not None
        assert not ui.errors


def test_frame_level_unmet_needs_are_counted_as_gaps(tmp_path, monkeypatch):
    """框图自带的 unmet_needs 是「这一帧还缺哪张卡」，也属于没落实，得进缺口数。"""
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        first = board["shots"][0]["id"]
        ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "set_keyframe", "shot_id": first, "offset_ms": 0,
             "sketch_path": str(_frame(tmp_path))},
        ])
        board = ops.read_storyboard(unit_id)
        layout = dict(LAYOUT, unmet_needs=[{"kind": "prop", "name": "铜钥匙"}])
        ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "set_keyframe_layout", "shot_id": first, "offset_ms": 0, "layout": layout},
        ])
        page = open_page(ui)
        page.reload()
        ui.wait(lambda: plain(page.property("rows"))[0]["gap_count"] == 1)

        assert [gap["gap"] for gap in plain(page.property("detail"))["gaps"]] == ["frame"]
        assert visual_find(page, "shotDetailLayoutNeedRow") is not None
        # 别的镜没有被牵连：帧级需求只算在它自己那一镜。
        assert plain(page.property("rows"))[1]["gap_count"] == 0
        assert not ui.errors


def test_bridge_slots_report_a_missing_scene_without_crashing(tmp_path, monkeypatch):
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        page = open_page(ui)
        first = board["shots"][0]["id"]

        # 拿不到场：ok:false + error，并且报在 shot_table 这一路上。
        missing = ui.bridge.shotTable("unit-does-not-exist")
        assert missing["ok"] is False and missing["error"]
        assert missing["rows"] == []
        assert any(kind == "shot_table" for kind, _ in ui.errors)

        # 镜头不属于本场：同样是 ok:false，而不是抛异常炸掉事件循环。
        stray = ui.bridge.shotDetail(unit_id, "shot-does-not-exist")
        assert stray["ok"] is False and "不属于本场" in stray["error"]

        # 这一镜还没排框：拿不到框图是正常状态，只说明为什么，不算错误。
        layout = ui.bridge.shotLayout(unit_id, first, 0)
        assert layout["ok"] is False and "关键帧" in layout["reason"]
        assert layout["shot_id"] == first

        # 页面还活着，而且还能正常刷新（失败没有把状态弄坏）。
        page.reload()
        ui.wait(lambda: len(plain(page.property("rows"))) == 3)


def test_shot_layout_returns_the_structured_frame_record(tmp_path, monkeypatch):
    """shotLayout 给的是框图（机位＋主体框＋景深），草图只作为附带的预览地址。"""
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        first = board["shots"][0]["id"]
        ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "set_keyframe", "shot_id": first, "offset_ms": 0,
             "sketch_path": str(_frame(tmp_path))},
        ])
        board = ops.read_storyboard(unit_id)
        ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "set_keyframe_layout", "shot_id": first, "offset_ms": 0, "layout": LAYOUT},
        ])

        frame = ui.bridge.shotLayout(unit_id, first, 0)
        assert frame["ok"] is True and frame["frame_offset_ms"] == 0
        assert frame["hash"] == frame["layout"]["hash"] and frame["hash"]
        assert frame["camera"]["framing"] == "medium" and frame["camera"]["lens_mm"] == 35.0
        assert frame["objects"] == [{"asset_id": "asset-1", "bbox": [0.1, 0.2, 0.5, 0.9],
                                     "depth": 3.0, "kind": "person"}]
        assert frame["layout"]["source"] == "manual"
        assert frame["sketch_url"]  # 草图是预览，不参与框图的判定

        # 镜内任意时刻问到的都是「此刻正在播的那一帧」：这一镜只有 0 时刻有框图，
        # 所以问到别的时刻也会退回到它，而不是报错。
        playing = ui.bridge.shotLayout(unit_id, first, board["shots"][0]["duration_ms"] - 1)
        assert playing["ok"] is True and playing["frame_offset_ms"] == 0

        # 该帧存在但没写框图：ok:false 且说明为什么，仍然带着草图信息。
        ops.apply_storyboard_changes(unit_id, ops.read_storyboard(unit_id)["revision"], [
            {"op": "add_timeline_keyframe", "shot_id": first, "offset_ms": 500,
             "moment_name": "抬眼", "description": "抬眼看向车窗"},
        ])
        bare = ui.bridge.shotLayout(unit_id, first, 500)
        assert bare["ok"] is False and "框图" in bare["reason"]
        assert bare["frame_offset_ms"] == 500

        # 换掉框图，哈希必须跟着变（依据稳定性靠它判）。
        ops.apply_storyboard_changes(unit_id, ops.read_storyboard(unit_id)["revision"], [
            {"op": "set_keyframe_layout", "shot_id": first, "offset_ms": 0,
             "layout": dict(LAYOUT, camera={"framing": "close", "lens_mm": 50})},
        ])
        changed = ui.bridge.shotLayout(unit_id, first, 0)
        assert changed["hash"] != frame["hash"] and changed["camera"]["framing"] == "close"
        assert not ui.errors


def test_asset_need_tools_are_registered_on_the_storyboard_channel(tmp_path, monkeypatch):
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        first = board["shots"][0]["id"]

        def reply(token):
            ui.wait(lambda: any(payload.get("token") == token
                                for kind, payload in ui.results if kind == "storyboard"))
            return next(payload for kind, payload in ui.results
                        if kind == "storyboard" and payload.get("token") == token)

        # read_asset_needs：登记进白名单后界面这条路能读到缺口视图。
        ui.bridge.storyboardAction("read_asset_needs", {"unit_id": unit_id}, 4242)
        payload = reply(4242)
        assert "error" not in payload
        assert payload["result"]["counts"]["unresolved"] == 0

        # declare_asset_needs：同样走这条路（以前会被判成「未知分镜操作」）。
        revision = ops.read_storyboard(unit_id)["revision"]
        ui.bridge.storyboardAction("declare_asset_needs", {
            "unit_id": unit_id, "revision": revision,
            "shot_needs": [{"shot_id": first, "needs": [{"kind": "prop", "name": "金属头盔"}]}],
        }, 4243)
        payload = reply(4243)
        assert "error" not in payload
        assert payload["result"]["asset_needs_gap"]["counts"]["unresolved"] == 1

        # read_storyboard_layout：框图清单也能从界面这条路读。
        ui.bridge.storyboardAction("read_storyboard_layout",
                                   {"unit_id": unit_id, "shot_id": first}, 4245)
        payload = reply(4245)
        assert "error" not in payload
        assert payload["result"]["count"] == 0

        # 没登记的操作仍然被挡住：白名单不是「什么都放行」。
        ui.bridge.storyboardAction("not_a_storyboard_operation", {"unit_id": unit_id}, 4244)
        payload = reply(4244)
        assert payload["error"] == "未知分镜操作"


def _frame(tmp_path):
    frame = tmp_path / "框图.png"
    if not frame.is_file():
        Image.new("RGB", (16, 9), "gray").save(frame)
    return frame
