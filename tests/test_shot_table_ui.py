"""镜头表（逐镜制作驾驶舱）：桥槽位派生的表格、状态标记、缺口、筛选与详情。

判定全在 Python（read_storyboard / read_asset_needs / 视频回执），界面只负责画。
所以这里两头都验：既点真实界面（行、筛选、详情），也直接核对桥槽位的返回形状——
两边一起才说明「画出来的就是算出来的」。

E3 的两块交互也按这个口径验：标尺拖动直接调桥槽位模拟（松手只提交一次），
四个产图按钮的行为用假 provider 验（绝不联网）。
"""
from __future__ import annotations

import pytest
from PIL import Image
from screenplay_scene_fixture import seed
from test_chat_scroll import transcript_shell
from test_static_check_ui import click, plain, visual_find

from narrative.operations import Operations
from narrative.skills import Skill, save_skills

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


def test_shot_detail_exposes_ruler_drag_metadata(tmp_path, monkeypatch):
    """标尺要拖的东西全部由桥算好：点的路线、能不能拖、为什么不能、段与边界的范围。"""
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        first = board["shots"][0]["id"]
        ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "set_keyframe", "shot_id": first, "offset_ms": 0,
             "sketch_path": str(_frame(tmp_path))},
            {"op": "add_timeline_keyframe", "shot_id": first, "offset_ms": 0, "moment_name": "起手"},
            {"op": "add_timeline_keyframe", "shot_id": first, "offset_ms": 1000,
             "moment_name": "抬眼", "description": "抬眼看向车窗"},
        ])
        detail = ui.bridge.shotDetail(unit_id, first)

        assert detail["ok"] is True
        # 画幅：老工程没写就交空串，界面显示「未写」，不替它猜一个 16:9。
        assert detail["aspect_ratio"] == ""
        # 分段是推导的还是显式存的：界面据此说出「拖一下就变成显式」。
        assert detail["segments_explicit"] is False
        assert detail["max_segment_ms"] == 2000
        assert [[row["start_ms"], row["end_ms"]] for row in detail["segments"]] == [[0, 1000], [1000, 2000]]
        # 内部边界可拖，并给出能落到的区间（两侧段长不能超单段上限）。
        assert [(edge["index"], edge["at_ms"]) for edge in detail["segment_edges"]] == [(1, 1000)]
        assert detail["segment_edges"][0]["draggable"] is True
        assert (detail["segment_edges"][0]["min_ms"], detail["segment_edges"][0]["max_ms"]) == (1, 1999)
        # 这一镜的时长上没有关键帧，所以推导分段钉不成显式表：拖动仍会挪走边界上的
        # 关键帧，分段继续按关键帧推导——提示要说清是哪一种。
        assert detail["segment_edges"][0]["becomes_explicit"] is False
        assert "推导" in detail["segment_edges"][0]["hint"]

        points = {row["offset_ms"]: row for row in detail["keyframes"]}
        assert set(points) == {0, 1000}
        # 0ms 有静帧也有计划行：两条都要挪，否则标尺上那一刻的竖线不动。
        assert points[0]["moves"] == ["move_keyframe", "update_timeline_keyframe"]
        assert points[0]["frame_id"] and points[0]["marker_id"] == ""
        assert points[0]["moment_name"] == "起手" and points[0]["has_layout"] is False
        # 1000ms 只有计划行：只能改计划。
        assert points[1000]["moves"] == ["update_timeline_keyframe"]
        assert points[1000]["frame_id"] == "" and points[1000]["description"] == "抬眼看向车窗"
        # 静帧不能落在时长上（timing_conflicts），只有计划行时可以。
        assert (points[0]["min_offset_ms"], points[0]["max_offset_ms"]) == (0, 1999)
        assert (points[1000]["min_offset_ms"], points[1000]["max_offset_ms"]) == (0, 2000)
        assert all(row["draggable"] is True and row["reason"] == "" for row in points.values())

        # 声音节拍与机位点本轮只显示不可拖，且各自说明为什么。
        assert detail["sound_cues"] == [] and detail["camera_keys"] == []

        # 本场没有预演工程是常态，不是错误：只说清把文件放哪，不报 errorOccurred。
        previs = ui.bridge.readPrevis(unit_id)
        assert previs["ok"] is False and previs["available"] is False
        assert "还没有预演工程" in previs["error"] and "预演" in previs["hint"]
        assert not ui.errors

        # 没绑机位就出框图：ok:false + 一句「下一步做什么」，界面照这句给出指引。
        failed = ui.bridge.capturePrevisLayout(unit_id, first, 1000, False)
        assert failed["ok"] is False and "预演机位" in failed["error"]
        assert "预演页" in failed["hint"]
        assert any(kind == "shot_table" for kind, _ in ui.errors)

        # 定位回执的文字与选择记录同源：预演页读的就是这一份。
        label = ui.bridge.storyboardSelectionLabel({
            "unit_id": unit_id, "shot_id": first, "unit_label": "SC1 · 敲门",
            "shot_index": 1, "shot_title": "敲门", "offset_ms": 1200})
        assert label == "当前定位：SC1 · 敲门 / 第 1 镜（敲门） @ 1200ms"
        assert ui.bridge.storyboardSelectionLabel({}) == ""


def test_dragging_a_keyframe_and_a_segment_edge_writes_once_each(tmp_path, monkeypatch):
    """拖动＝松手提交一次：改时刻、越界被拒、撞点被拒、段边界连带挪关键帧。"""
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        first = board["shots"][0]["id"]
        ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "set_keyframe", "shot_id": first, "offset_ms": 0,
             "sketch_path": str(_frame(tmp_path))},
            {"op": "add_timeline_keyframe", "shot_id": first, "offset_ms": 0},
            {"op": "add_timeline_keyframe", "shot_id": first, "offset_ms": 1000},
            {"op": "add_timeline_keyframe", "shot_id": first, "offset_ms": 2000},
        ])
        plan = lambda: [row["offset_ms"] for row in  # noqa: E731 - 一行取值，不必另开函数
                        ops.read_storyboard(unit_id)["shots"][0]["timeline"]["keyframes"]]

        # 只有计划行的那一点：改的是计划（标尺是按计划画的）。
        moved = ui.bridge.moveShotKeyframe(unit_id, first, 1000, 1200)
        assert moved["ok"] is True, moved
        assert moved["via"] == ["update_timeline_keyframe"] and moved["offset_ms"] == 1200
        assert plan() == [0, 1200, 2000]

        # 越界：静帧不能落在时长上；原因要能直接读给人听。
        rejected = ui.bridge.moveShotKeyframe(unit_id, first, 0, 2000)
        assert rejected["ok"] is False and "0至1999ms" in rejected["error"]
        # 撞点：两个关键帧不能并成一个。
        clash = ui.bridge.moveShotKeyframe(unit_id, first, 0, 1200)
        assert clash["ok"] is False and "已经有另一条关键帧" in clash["error"]
        assert plan() == [0, 1200, 2000]  # 失败没写坏数据
        assert any(kind == "shot_table" for kind, _ in ui.errors)

        # 段边界：拖动推导段会把它钉成显式表，边界上的关键帧一起挪。
        detail = ui.bridge.shotDetail(unit_id, first)
        assert [(edge["at_ms"], edge["draggable"]) for edge in detail["segment_edges"]] == [(1200, True)]
        edge = ui.bridge.moveShotSegmentEdge(unit_id, first, 1, 600)
        assert edge["ok"] is True, edge
        assert edge["became_explicit"] is True and "显式" in edge["notice"]
        shot = ops.read_storyboard(unit_id)["shots"][0]
        assert plan() == [0, 600, 2000]
        assert shot["timeline"]["segments"] == [[0, 600], [600, 2000]]
        assert shot["timeline_derived"]["segments"] is False

        # 显式段再拖：分段表必须跟着改（只挪关键帧会被「端点必须落在关键帧上」拒掉）。
        again = ui.bridge.moveShotSegmentEdge(unit_id, first, 1, 900)
        assert again["ok"] is True, again
        assert again["segments"] == [[0, 900], [900, 2000]]
        shot = ops.read_storyboard(unit_id)["shots"][0]
        assert plan() == [0, 900, 2000] and shot["timeline"]["segments"] == [[0, 900], [900, 2000]]

        # 越界的段边界：两侧段长会超单段上限时不写。
        outside = ui.bridge.moveShotSegmentEdge(unit_id, first, 1, 0)
        assert outside["ok"] is False and "0至" not in outside["error"]
        assert plan() == [0, 900, 2000]


def test_marker_bound_frame_moves_the_named_moment(tmp_path, monkeypatch):
    """绑了命名时刻的帧：改时刻改的是那个标记（move_keyframe 那条路会被拒）。"""
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        second = board["shots"][1]["id"]
        marker = ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "create_marker", "name": "抬手", "time_ms": 700}])["markers"][0]["id"]
        frame_id = ops.apply_storyboard_changes(unit_id, ops.read_storyboard(unit_id)["revision"], [
            {"op": "set_keyframe", "shot_id": second, "offset_ms": 700,
             "sketch_path": str(_frame(tmp_path))}])["shots"][1]["keyframes"][-1]["id"]
        ops.apply_storyboard_changes(unit_id, ops.read_storyboard(unit_id)["revision"], [
            {"op": "bind_keyframe_marker", "shot_id": second, "frame_id": frame_id, "marker_id": marker},
        ])

        point = next(row for row in ui.bridge.shotDetail(unit_id, second)["keyframes"]
                     if row["offset_ms"] == 700)
        assert point["marker_id"] == marker and point["frame_id"] == frame_id
        assert point["moves"] == ["update_marker"]
        assert point["max_offset_ms"] == 1499  # 这一镜 1500ms

        moved = ui.bridge.moveShotKeyframe(unit_id, second, 700, 900)
        assert moved["ok"] is True, moved
        assert moved["via"] == ["update_marker"] and "命名时刻" in moved["notice"]
        current = ops.read_storyboard(unit_id)
        assert current["markers"][0]["time_ms"] == 900
        frames = current["shots"][1]["keyframes"]
        assert [(frame["offset_ms"], frame["marker_id"]) for frame in frames] == [(900, marker)]
        assert not ui.errors


def test_run_generation_task_reports_missing_skill_and_unsupported_warnings(tmp_path, monkeypatch):
    """产图按钮的两种结局：技能缺失说清缺什么；成功把 warnings 原样带回界面。"""
    monkeypatch.setenv("NARRATIVE_SKILLS_FILE", str(tmp_path / "skills.json"))
    # 现在随程序带内置生成技能，所以「技能缺失」这条要把内置也一并摘掉才成立。
    from narrative import skills as skills_module
    monkeypatch.setattr(skills_module, "BUILTIN_SKILLS", ())
    monkeypatch.setattr(skills_module, "BUILTIN_GENERATION_SKILLS", ())
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        first = board["shots"][0]["id"]
        ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "add_timeline_keyframe", "shot_id": first, "offset_ms": 0, "moment_name": "起手"},
        ])

        missing = ui.bridge.runGenerationTask(unit_id, first, 0, "sketch", "")
        assert missing["ok"] is False
        assert "没有可用于sketch生成的技能" in missing["error"]
        # 失败绝不能带着产物路径：界面据此判断「没出图」。
        assert missing["output_path"] == "" and missing["warnings"] == []
        assert any(kind == "shot_table" for kind, _ in ui.errors)

        # 声明了 workflow.params 但没有透传通道：这轮没生效，必须出现在 warnings 里。
        save_skills([Skill(name="本地素描", summary="测试用", instructions="画一帧", enabled=True,
                           created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
                           workflow={"kind": "sketch", "model": "stub-model",
                                     "params": {"steps": 20}})],
                    tmp_path / "skills.json")
        monkeypatch.setattr("narrative.storyboard_style_provider.render_sketch_image", fake_sketch)

        done = ui.bridge.runGenerationTask(unit_id, first, 0, "sketch", "留白多一点")
        assert done["ok"] is True, done
        assert done["bound_to"] == "keyframe_sketch"
        assert [row["code"] for row in done["warnings"]] == ["WORKFLOW_PARAMS_UNSUPPORTED"]
        assert done["warnings"][0]["params"] == {"steps": 20}
        assert done["skill"]["name"] == "本地素描" and done["provider"]["job_id"] == "job-1"
        assert done["output_path"].endswith(".png")
        # 产物在工程目录里，边界已经被绑定到那一帧（路径是工程内的副本，不是临时文件）。
        frame = ops.read_storyboard(unit_id)["shots"][0]["keyframes"][0]
        assert frame["offset_ms"] == 0 and frame["sketch_source"]["provider"] == "fake-comfy"


def test_page_wires_the_ruler_handles_and_the_four_produce_buttons(tmp_path, monkeypatch):
    """页面这一头：标尺把手画出来、四个按钮可用、预演状态与定位记录都接上了。"""
    with transcript_shell(tmp_path, monkeypatch) as ui:
        ops, screenplay_id, unit_id, board = prepare(ui)
        first = board["shots"][0]["id"]
        ops.apply_storyboard_changes(unit_id, board["revision"], [
            {"op": "add_timeline_keyframe", "shot_id": first, "offset_ms": 1000, "moment_name": "抬手"},
        ])
        page = open_page(ui)
        page.reload()
        ui.wait(lambda: page.property("selectedShotId") == first)
        ui.settle()

        # 标尺上的关键帧把手与段边界把手都是真的控件（delegate 不在 QObject 树上）。
        assert visual_find(page, "shotDetailRuler") is not None
        assert visual_find(page, "shotKeyframeHandle") is not None
        assert visual_find(page, "shotSegmentEdgeHandle") is not None
        # 四个产图按钮不再是占位：选中一镜就可用（按钮在 Repeater 里，是视觉子节点）。
        for key in ("layout", "sketch", "style", "video"):
            button = visual_find(page, "shotProduce_" + key)
            assert button is not None and button.property("enabled") is True
        # 预演状态说清本场有没有预演（没有也照常显示，不留空白）。
        state = visual_find(page, "shotDetailPrevisState")
        assert "预演" in state.property("text")
        # 分段状态与「此刻」都来自桥：界面不自己算。
        assert "推导" in visual_find(page, "shotDetailSegmentsState").property("text")
        detail = plain(page.property("detail"))
        assert [row["offset_ms"] for row in detail["keyframes"]] == [1000]

        # 定位记录：预演页的「当前定位」读的就是这一份（含场次名与镜号）。
        selection = plain(page.previsSelection(first))
        assert selection["unit_id"] == unit_id and selection["shot_id"] == first
        assert selection["shot_index"] == 1 and selection["unit_label"]
        assert not ui.errors


def _frame(tmp_path):
    frame = tmp_path / "框图.png"
    if not frame.is_file():
        Image.new("RGB", (16, 9), "gray").save(frame)
    return frame


def fake_sketch(guide, prompt, copies_to, reference_images=()):
    """假 provider：写一张真 PNG 当产物，绝不联网。"""
    Image.new("RGB", (24, 14), "gray").save(copies_to)
    return {"provider": "fake-comfy", "job_id": "job-1"}
