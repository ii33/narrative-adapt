"""窄窗口响应式：协作者停靠/抽屉两种形态、浏览页三栏的宽度契约。

判定的都是可观察事实，不是「跑过了」：

① 宽度——详情列、列表列、分类栏各自守住 ``theme.LAYOUT`` 的下限；分类栏只有
   「窄条(``browse_rail_width``) / 完整(≥``browse_category_min_width``)」两种形态，
   触发条件就是 SplitView 自己比「三栏全展开」窄，没有第三种中间态。
② 形态——窗口宽度跨过 ``assistant_dock_min_width`` 时，协作者从「占 SplitView 一列」
   换成「覆盖在页面区之上的抽屉」，两者互斥：停靠时不画遮罩、抽屉时停靠列宽度为 0。

离屏渲染真实 QML（offscreen + software 后端），几何全部从 objectName 上读。
1000 这一档低于 ``window_min_width``（真实窗口拖不到），是为了看极端情况下三栏
还立不立得住，所以测试里把窗口最小宽临时放开到 900。
"""

from __future__ import annotations

import time

import pytest
import test_theme_ui as themeui

WIDTHS = (1000, 1127, 1360, 1380, 1500)
MODES = ("compact", "islands")
HEIGHT = 780
# 真实窗口最小宽是 window_min_width(1100)；要压到 1000/900 得先放开窗口自己的下限。
MINIMUM_WIDTH_OVERRIDE = 900
COLUMN_HANDLE = 12

# 回归基线：紧凑模式 + 1127 + 协作者收起时三栏的宽度。
#
# 这几个数字曾经是「改动前 HEAD 4eb3843」实测的 885/190/240/431。那时 browse 页根
# 内容边距是 20，页面区宽 925，所以列区 = 925 - 2×20 = 885。之后 327eccc（「右上角
# 控件回到各页标题行，只把位置对齐」）刻意把各页根内容外边距统一成 12（原先 12/16/20
# 混用），目的是让八个页面右上角那组控件落在同一坐标。页面区宽度没动（仍是 925），
# 列区于是变成 925 - 2×12 = 901；多出来的这 16 全部落到 fillWidth 的详情列
# （431 → 447），定宽的分类 190 与列表 240 一动不动。
#
# 所以这 16 是那次「外边距统一」的正当结果，不是走形：12px 仍是真实内缩，内容既没
# 贴边也没被裁切（.runtime/spike 下有四组截图）。不是删说明文本导致滚动条消失——
# 列区的父链里根本没有滚动容器（见 test_islands_columns_are_inset_half_a_gap_from_the_page_card
# 的注释），页内唯一的滚动容器 browseDetailScroll 长在详情栏内部。
# 基线按 327eccc 之后的实测重置；将来再动页面外边距，这几个数字必须跟着给出理由。
COMPACT_1127_BASELINE = {
    "browseColumns": 901.0,
    "browseCategories": 190.0,
    "browseListColumn": 240.0,
    "browseDetail": 447.0,
}

# 页面根内容外边距：327eccc 把各页统一成 12（browse 页原来是 20）。三栏整体从这条
# 边距的内侧起算，岛屿模式再加半条 island_gap——两处断言共用这一个数，改了边距就
# 只有一个地方要动。
PAGE_CONTENT_INSET = 12


def _item(window, name):
    found = window.findChild(object, name)
    assert found is not None, (
        f"找不到 objectName={name}：QML 结构变了，先看 Main.qml / BrowsePage.qml"
    )
    return found


def _find(window, name):
    """可能不存在的对象（懒创建）：与 _item 的区别是找不到时返回 None。"""
    return window.findChild(object, name)


def _width(item):
    return round(item.property("width"), 1)


def _scene_rect(item, window):
    """顶部左角映射到窗口内容坐标系：跨父项比较几何必须先映射。"""
    from PySide6.QtCore import QPointF

    top = item.mapToItem(window.contentItem(), QPointF(0, 0))
    width = item.property("width")
    return {
        "x": top.x(),
        "y": top.y(),
        "width": width,
        "right": top.x() + width,
        "bottom": top.y() + item.property("height"),
    }


def _wait_columns(app, window, seconds=5.0):
    """等布局真正稳定：browseColumns 宽度连续三次采样一致。"""
    deadline = time.monotonic() + seconds
    last = None
    stable = 0
    while time.monotonic() < deadline:
        app.processEvents()
        try:
            width = round(window.findChild(object, "browseColumns").property("width"), 1)
        except AttributeError:
            width = None
        if width is not None and width > 100 and width == last:
            stable += 1
            if stable >= 3:
                return width
        else:
            stable = 0
        last = width
        time.sleep(0.02)
    raise AssertionError(f"三栏宽度没有稳定下来：最后一次采样 {last}")


def _resize(ui, width, assistant):
    """把窗口摆到指定宽度与协作者开合状态，等布局稳定后返回 window。"""
    window = ui["window"]
    window.setProperty("minimumWidth", MINIMUM_WIDTH_OVERRIDE)
    window.setWidth(width)
    window.setHeight(HEIGHT)
    ui["settle"]()
    window.setProperty("assistantOpen", assistant)
    ui["settle"]()
    _wait_columns(ui["app"], window)
    return window


@pytest.fixture(scope="module", params=[(w, m) for w in WIDTHS for m in MODES],
                ids=[f"{w}-{m}" for w in WIDTHS for m in MODES])
def layout_ui(request, tmp_path_factory):
    """一份离屏界面 + 一个带数据的工程，同一 (宽度, 模式) 下的用例共用。

    共用是刻意的：同一窗口里连着改宽度，比较的就是同一个进程里的两次布局，
    不会把「重建窗口」的差异算到响应式头上。每个用例自己把宽度与开合摆好。
    """
    width, mode = request.param
    tmp = tmp_path_factory.mktemp(f"responsive-{width}-{mode}")
    monkeypatch = pytest.MonkeyPatch()
    try:
        with themeui.theme_shell(tmp, monkeypatch) as ui:
            from narrative.operations import Operations

            ui["settle"]()
            entry = tmp / "responsive.nproj"
            ops = Operations.create_graph_project(entry)
            ops.import_source("样例", "甲乙丙沿河走，天开始下雨。\n" * 30)
            ui["bridge"].setLayoutMode(mode)
            ui["bridge"].openProject(str(entry))
            ui["settle"](lambda: ui["window"].findChild(object, "browsePage") is not None)
            ui["param"] = (width, mode)
            yield ui
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize("assistant", [False, True], ids=["closed", "open"])
def test_columns_keep_their_token_minimums(layout_ui, assistant):
    """每一档宽度 × 模式、协作者开合两种状态下，三栏都守得住令牌下限。"""
    from narrative import theme

    layout = theme.LAYOUT
    width, mode = layout_ui["param"]
    window = _resize(layout_ui, width, assistant)
    assert window.width() == width
    assert bool(window.property("islandsMode")) is (mode == "islands")

    columns = _item(window, "browseColumns")
    categories = _item(window, "browseCategories")
    list_column = _item(window, "browseListColumn")
    detail = _item(window, "browseDetail")
    rail = _item(window, "browseCategoryRail")

    full = (layout["browse_category_min_width"] + layout["browse_list_min_width"]
            + layout["browse_detail_min_width"] + 2 * COLUMN_HANDLE)

    # 详情列永远守得住下限：窄窗口里被压扁的正是这一列，容差 1px 给 SplitView 取整。
    assert _width(detail) >= layout["browse_detail_min_width"] - 1, (
        f"{width}/{mode}/assistant={assistant}：详情列 {_width(detail)} 低于"
        f" {layout['browse_detail_min_width']}"
    )
    assert _width(list_column) >= layout["browse_list_min_width"] - 1

    # 分类栏只有两种形态，且形态与 SplitView 自己的宽度一一对应。
    if rail.property("visible"):
        assert _width(categories) == layout["browse_rail_width"]
        assert _width(columns) < full
    else:
        assert _width(categories) >= layout["browse_category_min_width"] - 1
        assert _width(columns) >= full

    # 三栏 + 两条把手正好铺满 SplitView：溢出会把详情列挤到看不见。
    total = (_width(categories) + _width(list_column) + _width(detail) + 2 * COLUMN_HANDLE)
    assert total == pytest.approx(_width(columns), abs=1)


@pytest.mark.parametrize("mode", MODES)
def test_category_rail_takes_over_below_full_columns_width(mode, tmp_path_factory):
    """比「三栏全展开」窄时，分类栏真的收成 44 的窄条，宽度让给列表与详情。

    1000 这一档已经不够窄（三栏全展开只要 724），所以这里单独压到 900——真实窗口
    拖不到，但抽屉/停靠之外的最后一条响应式分支必须有人踩。
    """
    from narrative import theme

    layout = theme.LAYOUT
    tmp = tmp_path_factory.mktemp(f"rail-{mode}")
    monkeypatch = pytest.MonkeyPatch()
    try:
        with themeui.theme_shell(tmp, monkeypatch) as ui:
            from narrative.operations import Operations

            ui["settle"]()
            entry = tmp / "rail.nproj"
            ops = Operations.create_graph_project(entry)
            ops.import_source("样例", "甲乙丙沿河走，天开始下雨。\n" * 30)
            ui["bridge"].setLayoutMode(mode)
            ui["bridge"].openProject(str(entry))
            ui["settle"](lambda: ui["window"].findChild(object, "browsePage") is not None)
            window = _resize(ui, MINIMUM_WIDTH_OVERRIDE, False)

            columns = _item(window, "browseColumns")
            categories = _item(window, "browseCategories")
            rail = _item(window, "browseCategoryRail")
            assert _width(columns) < (layout["browse_category_min_width"]
                                      + layout["browse_list_min_width"]
                                      + layout["browse_detail_min_width"] + 2 * COLUMN_HANDLE)
            assert rail.property("visible") is True
            assert _width(categories) == layout["browse_rail_width"]
            # 腾出来的宽度必须给列表与详情，而不是留成空白。
            assert _width(_item(window, "browseListColumn")) >= layout["browse_list_min_width"] - 1
            assert _width(_item(window, "browseDetail")) >= layout["browse_detail_min_width"] - 1
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize("assistant", [False, True], ids=["closed", "open"])
def test_assistant_dock_and_overlay_are_mutually_exclusive(layout_ui, assistant):
    """协作者形态只由窗口宽度决定：够宽占一列，不够宽覆盖在页面区之上。"""
    from narrative import theme

    layout = theme.LAYOUT
    width, mode = layout_ui["param"]
    window = _resize(layout_ui, width, assistant)

    dock = _item(window, "assistantDock")
    overlay = _item(window, "assistantOverlay")
    scrim = _item(window, "assistantScrim")
    page_area = _item(window, "workspacePageArea")
    # 面板是「第一次打开才建」的：收起态下它本来就不该存在，所以只断言容器不露。
    panel = _find(window, "assistantPanel") if assistant else None
    docked = bool(window.property("assistantDocked"))
    assert docked is (window.width() >= layout["assistant_dock_min_width"])
    area = _scene_rect(page_area, window)
    panel_rect = _scene_rect(panel, window) if panel is not None else None
    gap = layout["island_gap"] if mode == "islands" else 0
    # 岛屿模式整块比页面卡片内缩半条缝，面板自己还留一个 islandInset。
    inset = gap / 2 if mode == "islands" else 0

    if not assistant:
        # 收起时两种形态都不该露出任何东西，但停靠判定本身仍在。
        assert dock.property("visible") is False
        assert overlay.property("visible") is False and scrim.property("visible") is False
        return

    if docked:
        # 停靠：占 SplitView 第三列，宽度是 360（岛屿模式多一圈缝给卡片），不画遮罩。
        assert dock.property("visible") is True
        assert _width(dock) == 360 + gap
        assert _scene_rect(dock, window)["x"] >= area["right"] - 1
        assert overlay.property("visible") is False and scrim.property("visible") is False
        assert panel_rect["right"] <= window.width() + 1
    else:
        # 抽屉：停靠列宽度为 0 且不可见，遮罩盖住页面区，面板压在页面区右侧。
        assert dock.property("visible") is False
        assert _width(dock) == 0
        assert overlay.property("visible") is True and scrim.property("visible") is True
        assert panel_rect["right"] == pytest.approx(area["right"] - inset, abs=1)
        assert area["x"] < panel_rect["x"]
        assert panel_rect["width"] == pytest.approx(360 + gap, abs=1)


@pytest.mark.parametrize("mode", MODES)
def test_1360_drawer_and_1380_dock_share_one_column_form(mode, tmp_path_factory):
    """1360 走抽屉、1380 走停靠：跨过阈值时三栏形态不许跳变。

    这是 assistant_dock_min_width 从 1360 调到 1370 的原因——1360 停靠时三栏只剩
    722，比「三栏全展开」的 724 差 2px，分类栏会在两档之间突然收成窄条。
    """
    from narrative import theme

    layout = theme.LAYOUT
    tmp = tmp_path_factory.mktemp(f"threshold-{mode}")
    monkeypatch = pytest.MonkeyPatch()
    try:
        with themeui.theme_shell(tmp, monkeypatch) as ui:
            from narrative.operations import Operations

            ui["settle"]()
            entry = tmp / "threshold.nproj"
            ops = Operations.create_graph_project(entry)
            ops.import_source("样例", "甲乙丙沿河走，天开始下雨。\n" * 30)
            ui["bridge"].setLayoutMode(mode)
            ui["bridge"].openProject(str(entry))
            ui["settle"](lambda: ui["window"].findChild(object, "browsePage") is not None)

            shape = {}
            for width in (1360, 1380):
                window = _resize(ui, width, True)
                assert bool(window.property("assistantDocked")) is (
                    width >= layout["assistant_dock_min_width"])
                categories = _item(window, "browseCategories")
                shape[width] = {
                    "rail": _item(window, "browseCategoryRail").property("visible"),
                    "categories": _width(categories),
                    "list": _width(_item(window, "browseListColumn")),
                    "detail": _width(_item(window, "browseDetail")),
                }

            # 两档都是「分类展开」这一种形态，不是一档窄条一档完整——这正是把
            # 停靠阈值从 1360 抬到 1370 想要的结果（1360 停靠时三栏只剩 722 < 724）。
            assert shape[1360]["rail"] is False and shape[1380]["rail"] is False
            for width in (1360, 1380):
                assert shape[width]["categories"] >= layout["browse_category_min_width"] - 1
                assert shape[width]["detail"] >= layout["browse_detail_min_width"] - 1
            # 分类栏宽度只掉一个「详情列多吃的零头」，远小于跳成窄条的那 146px。
            assert abs(shape[1360]["categories"] - shape[1380]["categories"]) <= 40
            # 1380 停靠把三栏压回下限；1360 抽屉把整页宽度还给三栏，列表反而更宽。
            # 这就是「窄窗口不再挤压内层三栏」的目的：两个方向都不许低于下限。
            assert shape[1380]["list"] == layout["browse_list_min_width"]
            assert shape[1360]["list"] > shape[1380]["list"]
    finally:
        monkeypatch.undo()


def test_compact_1127_columns_match_the_frozen_baseline(tmp_path_factory):
    """紧凑模式 + 1127 + 协作者收起：三栏宽度与冻结基线逐项一致（见基线处的说明）。"""
    tmp = tmp_path_factory.mktemp("baseline-compact-1127")
    monkeypatch = pytest.MonkeyPatch()
    try:
        with themeui.theme_shell(tmp, monkeypatch) as ui:
            from narrative.operations import Operations

            ui["settle"]()
            entry = tmp / "baseline.nproj"
            ops = Operations.create_graph_project(entry)
            ops.import_source("样例", "甲乙丙沿河走，天开始下雨。\n" * 30)
            ui["bridge"].setLayoutMode("compact")
            ui["bridge"].openProject(str(entry))
            ui["settle"](lambda: ui["window"].findChild(object, "browsePage") is not None)
            window = _resize(ui, 1127, False)

            actual = {name: _width(_item(window, name)) for name in COMPACT_1127_BASELINE}
            assert actual == COMPACT_1127_BASELINE, (
                "紧凑模式 1127 下的三栏宽度偏离了改动前基线："
                f"实测 {actual}，基线 {COMPACT_1127_BASELINE}"
            )
            # 岛屿模式那半条缝在紧凑模式必须为 0，否则卡片边框对不上：列区相对页面
            # 卡片只内缩页面自己的内容边距（PAGE_CONTENT_INSET，不是旧的 20）。
            columns = _item(window, "browseColumns")
            origin = _scene_rect(columns, window)
            page_rect = _scene_rect(_item(window, "browsePage"), window)
            assert origin["x"] - page_rect["x"] == pytest.approx(PAGE_CONTENT_INSET, abs=1)
            assert page_rect["right"] - origin["right"] == pytest.approx(PAGE_CONTENT_INSET, abs=1)
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize("mode", MODES)
def test_islands_columns_are_inset_half_a_gap_from_the_page_card(mode, tmp_path_factory):
    """岛屿模式：三列整体从页面卡片边框内缩「内容边距 + island_gap/2」，不贴边。"""
    from narrative import theme

    gap = theme.LAYOUT["island_gap"]
    tmp = tmp_path_factory.mktemp(f"gutter-{mode}")
    monkeypatch = pytest.MonkeyPatch()
    try:
        with themeui.theme_shell(tmp, monkeypatch) as ui:
            from narrative.operations import Operations

            ui["settle"]()
            entry = tmp / "gutter.nproj"
            ops = Operations.create_graph_project(entry)
            ops.import_source("样例", "甲乙丙沿河走，天开始下雨。\n" * 30)
            ui["bridge"].setLayoutMode(mode)
            ui["bridge"].openProject(str(entry))
            ui["settle"](lambda: ui["window"].findChild(object, "browsePage") is not None)
            window = _resize(ui, 1127, False)

            columns = _scene_rect(_item(window, "browseColumns"), window)
            page_rect = _scene_rect(_item(window, "browsePage"), window)
            # 页面自己的 ColumnLayout 有 PAGE_CONTENT_INSET(12) 的内容边距；岛屿模式
            # 再加半条缝。列区父链上没有任何 ScrollView/Flickable，所以这 12px 不会
            # 因为「页里有没有滚动条」而变——滚动条只出现在详情栏内部的 ScrollView 里。
            inset = PAGE_CONTENT_INSET + (gap / 2 if mode == "islands" else 0)
            assert columns["x"] - page_rect["x"] == pytest.approx(inset, abs=1)
            assert page_rect["right"] - columns["right"] == pytest.approx(inset, abs=1)
            # 列把手的竖线：岛屿模式靠把手宽度形成缝隙，不画线。
            handle = _item(window, "browseColumnHandle")
            lines = [child for child in handle.findChildren(object)
                     if child.property("width") == 2]
            assert lines, "browseColumnHandle 里的竖线不见了，接口名变了？"
            assert lines[0].property("visible") is (mode != "islands")
    finally:
        monkeypatch.undo()
