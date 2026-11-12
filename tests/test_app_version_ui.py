"""版本号在真实界面上的两处露出：状态栏与启动页底栏。

离屏跑：状态栏在两种模式下都在（它不在页面 StackLayout 里），启动页只有
launcher 模式下可见。断言文本与 ``bridge.appVersion`` 完全一致，而不是只看
「有个标签」——版本串对不上，截图和 bug 报告就指不回具体代码。
"""
from __future__ import annotations

import pytest
import test_theme_ui as themeui

pytestmark = pytest.mark.ui


def _shell(tmp_path, monkeypatch):
    # 离屏时字体目录要显式给，否则测出来的是「没有字」的空标签。
    monkeypatch.setenv("QT_QPA_FONTDIR", "C:\\Windows\\Fonts")
    return themeui.theme_shell(tmp_path, monkeypatch)


def test_status_bar_shows_the_same_version_as_the_bridge(tmp_path, monkeypatch):
    from narrative import theme, version

    with _shell(tmp_path, monkeypatch) as ui:
        ui["settle"]()
        window, bridge = ui["window"], ui["bridge"]
        label = window.findChild(object, "appVersionLabel")
        assert label is not None, "状态栏没有版本标签"
        ui["settle"](lambda: label.property("text") == bridge.appVersion)
        assert label.property("visible") is True
        assert label.property("text") == bridge.appVersion
        # 三处口径一致：bridge → QML → version 模块。
        assert bridge.appVersion == version.display_version()
        assert bridge.appVersionTag == version.tag()
        assert bridge.appVersion.startswith(bridge.appVersionTag + "+")
        # 次要色、小字号，且没被挤掉（内容宽度不超过标签宽度）。
        assert label.property("color").name() == theme.TOKENS["muted"][0]
        assert 10 <= label.property("font").pixelSize() <= 11
        ui["settle"](lambda: label.property("width") >= label.property("contentWidth"))
        # 原来那一行自检文本还在，别被版本号顶替了。
        status = window.findChild(object, "statusLine")
        assert status is not None and status is not label


def test_launcher_footer_shows_version_and_tag(tmp_path, monkeypatch):
    from narrative import version

    with _shell(tmp_path, monkeypatch) as ui:
        ui["settle"]()
        page = ui["window"].findChild(object, "launcherPage")
        assert page is not None
        label = page.findChild(object, "launcherVersion")
        assert label is not None, "启动页没有版本行"
        ui["settle"](lambda: label.property("visible") and label.property("width") > 0)
        text = label.property("text")
        assert version.tag() in text
        assert ui["bridge"].appVersion in text
        assert ui["bridge"].appVersionTag == version.tag()
        ui["settle"](lambda: label.property("width") >= label.property("contentWidth"))
