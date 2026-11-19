"""独立设置页：两层分组（全局 / 本工程）、默认分组随来源、工程设置嵌入、返回来源页。

设置页是「全局设置」从启动页拆出来的那一页：从启动页或工作区都能进，返回时回到
进来的那一页。同一页里还有一层「本工程」：工程设置整页嵌进来，入口只剩导航底部
那一个「设置」，工作区导航里不再有同名的页面项。
appearance.json 与 MCP 配置都通过环境变量指到 tmp_path，测试绝不读写用户真实目录。
"""
from __future__ import annotations

import time

import pytest
import test_theme_ui as themeui
from test_static_check_ui import visual_find

pytestmark = pytest.mark.ui

# (分组键, 右侧内容区的 objectName, 所属分组) —— 顺序必须与 SettingsPage 里
# sections 的声明顺序、以及 StackLayout 的声明顺序一致，否则「点外观看到模型渠道」
# 这种错位会静默通过。
SECTIONS = [
    ("appearance", "settingsAppearanceSection", "global"),
    ("model", "settingsModelSection", "global"),
    ("mcp", "settingsMcpSection", "global"),
    ("skill", "settingsSkillSection", "global"),
    ("project_settings", "settingsProjectSection", "project"),
    ("about", "settingsAboutSection", "project"),
]

SECTION_KEYS = [key for key, _name, _scope in SECTIONS]


def _index_of(key):
    return SECTION_KEYS.index(key)


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
    value = expr.evaluate()
    assert not expr.hasError(), expr.error().toString()
    return value


def _open_settings_page(ui, origin="launcher"):
    """打开设置页，并指定「从哪一页进来的」。

    这里直接摆 mode / modeBeforeSettings 两个属性：真实点击入口（启动页按钮、
    导航列表里的「设置」项）另有 test_back_returns_to_the_page_settings_was_opened
    覆盖，这一层只关心「来源决定默认分组」，把来源当参数最省事。
    """
    window = ui["window"]
    window.setProperty("modeBeforeSettings", origin)
    window.setProperty("mode", "settings")
    _pump(ui)
    page = window.findChild(object, "settingsPage")
    assert page is not None, "Main.qml 里没有设置页"
    ui["settle"](lambda: page.property("visible") is True)
    return page


def _open_project(ui, tmp_path):
    """真的打开一个工程。

    「从工作区进来先落在工程设置」和「没有工程时这一节点不可点」是一对：
    只靠摆属性会把 bridge 与界面之间的工程状态摆成不一致，所以这里走真入口。
    """
    from narrative.operations import Operations

    entry = tmp_path / "settings-page.nproj"
    Operations.create_graph_project(entry)
    ui["bridge"].openProject(str(entry))
    ui["settle"](lambda: ui["window"].property("mode") == "workspace")
    ui["settle"](lambda: ui["window"].findChild(object, "browsePage") is not None)
    return entry


def _plain(value):
    """QML 的 JS 值过桥后可能是 QJSValue：要读内容先摊平。"""
    return value.toVariant() if hasattr(value, "toVariant") else value


def _nav_base(ui):
    """导航栏的根：Repeater 生成的导航项是视觉子节点，findChild 找不到，只能从它往下走。"""
    base = ui["window"].findChild(object, "applicationNavigation")
    assert base is not None, "工作区里没有导航栏"
    return base


def _click_nav_settings(ui, expected="settings", attempts=3):
    """点导航列表最后那一项「设置」，并等到界面真的切走。

    这一项在导航滚动区的末尾：内容比视口高时它会被裁在外面，所以点之前先把滚动条
    拉到底（内容装得下就别动，硬拉会把这一项拖出视口，点击落到空处）。
    """
    window = ui["window"]
    for _ in range(attempts):
        scroll = window.findChild(object, "navigationScroll")
        assert scroll is not None, "导航里没有滚动区"
        content = scroll.property("contentItem")
        if content.property("contentHeight") > content.property("height"):
            content.setProperty("contentY", 1e6)
            _pump(ui, 4)
        item = _visual(ui, _nav_base(ui), "navigation_settings", "导航里没有「设置」这一项")
        _click(ui, item)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            ui["app"].processEvents()
            if window.property("mode") == expected:
                return item
            time.sleep(0.01)
    raise AssertionError("点了导航里的「设置」之后界面没有切到 " + expected)


def test_each_section_switches_and_shows_its_own_content(tmp_path, monkeypatch):
    with _shell(tmp_path, monkeypatch) as ui:
        ui["settle"]()
        page = _open_settings_page(ui)

        # 分组清单：全局四项 + 本工程两项。顺序是导航与内容区共用的唯一真相，
        # 所以这里直接读设置页自己那两个清单，再与内容区的 StackLayout 对齐。
        def keys(page_property):
            return [entry["key"] for entry in _plain(page.property(page_property))]

        assert keys("globalSections") == ["appearance", "model", "mcp", "skill"]
        assert keys("projectSections") == ["project_settings", "about"]
        assert keys("sections") == SECTION_KEYS
        # 两组各有标题行：没有它，六项连成一列就又看不出「全局 / 本工程」这层区分。
        assert _visual(ui, page, "settingsGroupLabel_global",
                       "导航里没有「全局」分组标题").property("text") == "全局"
        assert _visual(ui, page, "settingsGroupLabel_project",
                       "导航里没有「本工程」分组标题").property("text") == "本工程"

        # 工程设置这一段要当前工程才连得上，靠 _open_project 的用例覆盖；
        # 这里验证不带工程也能切的那几组，切换规则一样。
        for index, (key, section_name, _scope) in enumerate(SECTIONS):
            if key == "project_settings":
                continue
            nav = _visual(ui, page, "settingsSectionNav_" + key, "分组导航里没有 " + key)
            _click(ui, nav)
            assert page.property("sectionIndex") == index
            section = _child(ui, page, section_name, "内容区里没有这一组")
            # 用默认参数绑定 section：循环变量晚绑定会让每个 lambda 都看最后一组。
            ui["settle"](lambda group=section: group.property("visible") is True)
            # 一次只该有一组可见：两组同时可见就是控件叠在一起，用户会看到两份设置。
            for other_key, other_name, _ in SECTIONS:
                if other_key == key:
                    continue
                assert page.findChild(object, other_name).property("visible") is False

        # 每组都是真内容而不是空壳：外观两个下拉、模型渠道的保存按钮、MCP 面板、
        # 技能面板、版本串，以及本工程那一段就是嵌进来的 ProjectSettingsPage。
        assert page.findChild(object, "appearanceMode") is not None
        assert page.findChild(object, "layoutMode") is not None
        assert page.findChild(object, "saveGlobalSettingsButton") is not None
        assert page.findChild(object, "mcpSettingsPanel") is not None
        assert page.findChild(object, "skillSettingsPanel") is not None
        embedded = page.findChild(object, "projectSettingsPage")
        assert embedded is not None, "本工程这一组不是嵌入的 ProjectSettingsPage"
        assert embedded.property("embedded") is True
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

        # ② 工作区进入（导航里最后那一项「设置」）→ 返回工作区：工程还开着，
        # 不能被甩回工程列表。这里没有真的打开工程，各页面会如实报
        # 「请先打开图项目」——那是预期内的，所以这一段只断言模式回位。
        window.setProperty("mode", "workspace")
        _pump(ui)
        _click_nav_settings(ui)
        assert window.property("modeBeforeSettings") == "workspace"
        _click_for_mode(ui, _child(ui, page, "settingsBackButton", "设置页没有返回按钮"),
                        "workspace")


def test_workspace_navigation_offers_exactly_one_settings_entry(tmp_path, monkeypatch):
    """工程内只剩一个设置入口，且它就是导航列表的最后一项。"""
    with _shell(tmp_path, monkeypatch) as ui:
        ui["settle"]()
        window = ui["window"]

        # 原来并列的两个入口都撤了：页面项「工程设置」与导航底部的「设置」按钮。
        base = _nav_base(ui)
        assert visual_find(base, "navigation_project_settings") is None
        assert window.findChild(object, "navigationSettingsButton") is None

        # 导航顺序：分镜稿 → 镜头表 → 预演 → 导演台 → … → 静态检查 → 设置。
        # 每一项与 StackLayout 的 Loader 同下标，顺序错位就不是「点了另一个页」，
        # 而是整列页面都错一位，所以这里把清单整体钉住。
        keys = [_plain(item)["key"] for item in _plain(window.property("navItems"))]
        assert keys == ["browse", "relations", "source", "adaptation", "assets",
                        "screenplay", "storyboard", "shot_table", "previs", "director",
                        "screenplay_structure", "static_check", "settings"]

        # 唯一入口是最后一项，图标沿用 NavigationIcon 里现成的齿轮画法。
        entry = _visual(ui, base, "navigation_settings", "导航里没有「设置」这一项")
        assert visual_find(entry, "navigationIcon_project_settings") is not None, \
            "设置项的图标不是齿轮那一档"


def test_default_section_depends_on_where_settings_was_opened(tmp_path, monkeypatch):
    """默认分组：工作区 → 本工程 · 工程设置；启动页 → 全局 · 外观。"""
    with _shell(tmp_path, monkeypatch) as ui:
        ui["settle"]()
        window = ui["window"]

        # ① 启动页进来：没有工程，落在全局的「外观」。
        launcher = window.findChild(object, "launcherPage")
        _click_for_mode(ui, _child(ui, launcher, "launcherSettingsButton",
                                   "启动页没有设置入口"), "settings")
        page = _child(ui, window, "settingsPage", "窗口里没有设置页")
        ui["settle"](lambda: page.property("visible") is True)
        # 来源页记在窗口上（设置页据此算默认分组），不是设置页自己的属性。
        assert window.property("modeBeforeSettings") == "launcher"
        assert page.property("sectionIndex") == _index_of("appearance")

        # ② 工作区进来（真开了工程）：落在「本工程 · 工程设置」。
        _open_project(ui, tmp_path)
        _pump(ui)
        _click_nav_settings(ui)
        assert window.property("modeBeforeSettings") == "workspace"
        ui["settle"](lambda: page.property("sectionIndex") == _index_of("project_settings"))
        section = _child(ui, page, "settingsProjectSection", "设置页里没有本工程这一组")
        ui["settle"](lambda: section.property("visible") is True)

        # 嵌入的是 ProjectSettingsPage 本身：它的字段与保存按钮都在，不是抄了一份壳。
        embedded = _child(ui, page, "projectSettingsPage", "设置页里没有嵌入工程设置")
        assert embedded.property("embedded") is True
        for name in ("projectGranularity", "projectProvider", "projectApiKey",
                     "projectPromptType", "projectSettingsSave"):
            assert embedded.findChild(object, name) is not None, name

        # 切到全局的「外观」再切回来：同一份嵌入式页面，不是每次重建一个。
        _click(ui, _visual(ui, page, "settingsSectionNav_appearance", "导航里没有外观"))
        ui["settle"](lambda: section.property("visible") is False)
        _click(ui, _visual(ui, page, "settingsSectionNav_project_settings", "导航里没有工程设置"))
        ui["settle"](lambda: section.property("visible") is True)
        assert page.findChild(object, "projectSettingsPage") is embedded


def test_project_settings_group_is_disabled_without_a_project(tmp_path, monkeypatch):
    """没有工程时「本工程 · 工程设置」不可选，并说明原因。"""
    with _shell(tmp_path, monkeypatch) as ui:
        ui["settle"]()
        page = _open_settings_page(ui)  # 启动页进来，没有工程
        assert page.property("projectSettingsAvailable") is False
        assert page.property("sectionIndex") == _index_of("appearance")

        hit = _visual(ui, page, "settingsSectionHit_project_settings", "工程设置没有命中区")
        assert hit.property("enabled") is False, "无工程时工程设置仍然可点"
        hint = _visual(ui, page, "settingsSectionHint_project_settings", "工程设置没有说明行")
        assert "先打开工程" in hint.property("text")
        # 点了也不会切过去：不是「点了没反应」的错觉，而是真的不可选。
        _click(ui, _visual(ui, page, "settingsSectionNav_project_settings", "导航里没有工程设置"))
        assert page.property("sectionIndex") == _index_of("appearance")


def test_launcher_no_longer_carries_a_settings_area(tmp_path, monkeypatch):
    with _shell(tmp_path, monkeypatch) as ui:
        ui["settle"]()
        launcher = ui["window"].findChild(object, "launcherPage")
        # 同一份设置只能有一处：留在启动页就等于两个地方能改同一份值。
        for name in ("appearanceMode", "layoutMode", "globalSettingsTabs",
                     "mcpSettingsPanel", "skillSettingsPanel", "saveGlobalSettingsButton"):
            assert launcher.findChild(object, name) is None, name + " 还留在启动页里"
        # 工程列表与设置入口都还在——不该把入口和设置区一起删掉。
        assert launcher.findChild(object, "projectList") is not None
        assert launcher.findChild(object, "launcherSettingsButton") is not None
        assert not ui["errors"]
