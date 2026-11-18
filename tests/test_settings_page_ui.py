"""独立设置页：分组切换、外观绑定、MCP 面板、返回来源页，以及启动页不再重复设置区。

设置页是「全局设置」从启动页拆出来的那一页：从启动页或工作区都能进，返回时回到
进来的那一页。appearance.json 与 MCP 配置都通过环境变量指到 tmp_path，
测试绝不读写用户真实目录。
"""
from __future__ import annotations

import time

import pytest
import test_theme_ui as themeui
from test_static_check_ui import visual_find

pytestmark = pytest.mark.ui

# 分组键 -> 右侧内容区的 objectName。顺序必须与 SettingsPage 里 StackLayout 的
# 声明顺序一致，否则「点外观看到模型渠道」这种错位会静默通过。
SECTIONS = [
    ("appearance", "settingsAppearanceSection"),
    ("model", "settingsModelSection"),
    ("mcp", "settingsMcpSection"),
    ("about", "settingsAboutSection"),
]


def _shell(tmp_path, monkeypatch):
    # NARRATIVE_APPEARANCE_FILE 由 theme_shell 自己指到 tmp_path；这里补上 MCP 配置文件，
    # 两份配置都落在临时目录里。
    monkeypatch.setenv("NARRATIVE_MCP_FILE", str(tmp_path / "mcp-servers.json"))
    return themeui.theme_shell(tmp_path, monkeypatch)


def _pump(ui, rounds=8):
    """把事件循环多转几圈。

    切换界面（mode）之后布局要重排：只转一圈拿到的还是旧几何，按旧坐标点下去就会
    落到空处——这里等它排完，点击才可复现。
    """
    for _ in range(rounds):
        ui["app"].processEvents()
        time.sleep(0.01)


def _click(ui, item):
    """在控件正中真的点一下。

    theme_shell 给的是 dict（不是带属性的对象），所以这里不用 test_static_check_ui.click；
    但点法一致：换算成窗口坐标后交给 QTest，走的仍是真实的鼠标事件链。
    """
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest

    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    QTest.mouseClick(ui["window"], Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, point.toPoint())
    ui["settle"]()


def _click_for_mode(ui, item, expected, attempts=3):
    """点一下，并等到界面真的切到 expected 所在的模式。

    离屏平台没有真实窗口循环：鼠标事件与「切模式后重排」偶尔擦肩而过，点下去的坐标
    还是旧的。这里把「多等一轮再点」写进重试里——控件真的没接上就会三次全落空、报错。
    """
    window = ui["window"]
    for _ in range(attempts):
        _pump(ui, 4)
        _click(ui, item)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            ui["app"].processEvents()
            if window.property("mode") == expected:
                return
            time.sleep(0.01)
    raise AssertionError("点了 " + item.objectName() + " 之后界面没有切到 " + expected)


def _child(ui, parent, name, what):
    """等控件出现再返回（QObject 树查找）。"""
    try:
        ui["settle"](lambda: parent.findChild(object, name) is not None)
    except AssertionError as exc:
        raise AssertionError(what + "：找不到 " + name) from exc
    return parent.findChild(object, name)


def _visual(ui, root, name, what):
    """在可见树里等控件出现再返回。

    分组导航是 Repeater 生成的：delegate 是视觉子节点、不在 QObject 树上，
    findChild 找不到它（和 ListView 的 delegate 一样），只能按 childItems 走。
    """
    try:
        ui["settle"](lambda: visual_find(root, name) is not None)
    except AssertionError as exc:
        raise AssertionError(what + "：找不到 " + name) from exc
    return visual_find(root, name)


def _evaluate(item, expression):
    """在 item 的 QML 上下文里执行一条表达式。

    用来代替「用户真的操作了那个下拉框」：发 activated(index) 与点选该项走的是同一条
    onActivated 处理函数。离屏环境没有真实窗口几何，弹窗里的选项点不准，这样最稳。
    """
    from PySide6.QtQml import QQmlEngine, QQmlExpression

    context = QQmlEngine.contextForObject(item)
    assert context is not None, "拿不到 QML 上下文"
    expr = QQmlExpression(context, item, expression)
    expr.evaluate()
    assert not expr.hasError(), expr.error().toString()


def _open_settings_page(ui):
    window = ui["window"]
    window.setProperty("mode", "settings")
    _pump(ui)
    page = window.findChild(object, "settingsPage")
    assert page is not None, "Main.qml 里没有设置页"
    ui["settle"](lambda: page.property("visible") is True)
    return page


def test_each_section_switches_and_shows_its_own_content(tmp_path, monkeypatch):
    with _shell(tmp_path, monkeypatch) as ui:
        ui["settle"]()
        page = _open_settings_page(ui)

        for index, (key, section_name) in enumerate(SECTIONS):
            nav = _visual(ui, page, "settingsSectionNav_" + key, "分组导航里没有 " + key)
            _click(ui, nav)
            assert page.property("sectionIndex") == index
            section = _child(ui, page, section_name, "内容区里没有这一组")
            # 用默认参数绑定 section：循环变量晚绑定会让每个 lambda 都看最后一组。
            ui["settle"](lambda group=section: group.property("visible") is True)
            # 一次只该有一组可见：两组同时可见就是控件叠在一起，用户会看到两份设置。
            for other_key, other_name in SECTIONS:
                if other_key == key:
                    continue
                assert page.findChild(object, other_name).property("visible") is False

        # 每组都是真内容而不是空壳：外观两个下拉、模型渠道的保存按钮、MCP 面板、版本串。
        assert page.findChild(object, "appearanceMode") is not None
        assert page.findChild(object, "layoutMode") is not None
        assert page.findChild(object, "saveGlobalSettingsButton") is not None
        assert page.findChild(object, "mcpSettingsPanel") is not None
        version = page.findChild(object, "settingsVersionLabel")
        assert version.property("text") == ui["bridge"].appVersion
        assert not ui["errors"]


def test_appearance_switch_reaches_the_bridge_through_the_moved_handlers(
    tmp_path, monkeypatch
):
    """搬过来的外观/主框构控件必须还连着 bridge：切一下，bridge 与磁盘都跟着变。"""
    from narrative import appearance

    with _shell(tmp_path, monkeypatch) as ui:
        ui["settle"]()
        page = _open_settings_page(ui)
        bridge = ui["bridge"]
        theme_combo = page.findChild(object, "appearanceMode")
        layout_combo = page.findChild(object, "layoutMode")

        # 深色：走设置页里那条 onActivated → bridge.setThemeMode("dark")。
        _evaluate(theme_combo, "activated(2)")
        ui["settle"](lambda: bridge.themeMode == "dark")
        assert bridge.darkMode is True
        assert appearance.load(tmp_path / "appearance.json") == "dark"
        # 下拉框的回读也还连着：换个页面再回来看到的是当前选择。
        ui["settle"](lambda: theme_combo.property("currentIndex") == 2)

        # 浅色：同一控件、同一个处理函数，固定成浅色后不再跟系统。
        _evaluate(theme_combo, "activated(1)")
        ui["settle"](lambda: bridge.themeMode == "light")
        assert bridge.darkMode is False
        assert appearance.load(tmp_path / "appearance.json") == "light"

        # 主框构一样立刻生效并记住。
        _evaluate(layout_combo, "activated(1)")
        ui["settle"](lambda: bridge.layoutMode == "islands")
        assert ui["window"].property("islandsMode") is True
        assert appearance.load_layout(appearance.default_path()) == "islands"
        assert not ui["errors"]


def test_mcp_group_hosts_the_shared_panel(tmp_path, monkeypatch):
    with _shell(tmp_path, monkeypatch) as ui:
        ui["settle"]()
        page = _open_settings_page(ui)
        _click(ui, _visual(ui, page, "settingsSectionNav_mcp", "分组导航里没有 MCP"))
        panel = page.findChild(object, "mcpSettingsPanel")
        assert panel is not None, "MCP 分组里没有复用 McpSettingsPanel"
        ui["settle"](lambda: panel.property("visible") is True)
        # 面板读的是临时目录里的配置：证明 NARRATIVE_MCP_FILE 的隔离生效了，
        # 也顺手证明它真的去读了配置而不是只画了个空框。
        assert str(tmp_path) in str(panel.property("configPath"))
        assert not ui["errors"]


def test_back_returns_to_the_page_settings_was_opened_from(tmp_path, monkeypatch):
    with _shell(tmp_path, monkeypatch) as ui:
        ui["settle"]()
        window = ui["window"]
        launcher = window.findChild(object, "launcherPage")
        assert window.property("mode") == "launcher"

        # ① 启动页进入 → 返回启动页。
        entry = _child(ui, launcher, "launcherSettingsButton", "启动页没有设置入口")
        _click_for_mode(ui, entry, "settings")
        page = window.findChild(object, "settingsPage")
        back = _child(ui, page, "settingsBackButton", "设置页没有返回按钮")
        _click_for_mode(ui, back, "launcher")
        assert not ui["errors"]

        # ② 工作区进入 → 返回工作区：工程还开着，不能被甩回工程列表。
        # 这里没有真的打开工程，各页面会如实报「请先打开图项目」——那是预期内的，
        # 所以这一段只断言模式回位，不拿 errors 当判据。
        window.setProperty("mode", "workspace")
        _pump(ui)
        nav_entry = _child(ui, window, "navigationSettingsButton", "工作区导航底部没有设置入口")
        _click_for_mode(ui, nav_entry, "settings")
        assert window.property("modeBeforeSettings") == "workspace"
        _click_for_mode(ui, _child(ui, page, "settingsBackButton", "设置页没有返回按钮"),
                        "workspace")


def test_launcher_no_longer_carries_a_settings_area(tmp_path, monkeypatch):
    with _shell(tmp_path, monkeypatch) as ui:
        ui["settle"]()
        launcher = ui["window"].findChild(object, "launcherPage")
        # 同一份设置只能有一处：留在启动页就等于两个地方能改同一份值。
        for name in ("appearanceMode", "layoutMode", "globalSettingsTabs",
                     "mcpSettingsPanel", "saveGlobalSettingsButton"):
            assert launcher.findChild(object, name) is None, name + " 还留在启动页里"
        # 工程列表与设置入口都还在——不该把入口和设置区一起删掉。
        assert launcher.findChild(object, "projectList") is not None
        assert launcher.findChild(object, "launcherSettingsButton") is not None
        assert not ui["errors"]
