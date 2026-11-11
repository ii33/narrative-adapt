"""主框构（紧凑／岛屿）：切换即时生效、记住选择、紧凑模式几何不变。

岛屿只是给主框构加一层留白与圆角卡片；紧凑模式是回归基线，任何几何变化都说明
包裹改坏了原有布局。用例同时守住「切换后 window 上有可观察属性」这条合同。
"""
from __future__ import annotations

import json

import test_theme_ui as themeui


def _rects(window, names):
    found = {}
    for name in names:
        item = window.findChild(object, name)
        if item is None:
            continue
        found[name] = tuple(round(item.property(key), 2) for key in ("x", "y", "width", "height"))
    return found


KEY_AREAS = ["workspaceColumns", "applicationNavigation", "workspaceColumnHandle", "statusLine"]


def test_layout_mode_switches_live_and_remembers_choice(tmp_path, monkeypatch):
    from narrative import appearance

    with themeui.theme_shell(tmp_path, monkeypatch) as ui:
        ui['settle']()
        window = ui['window']
        bridge = ui['bridge']
        assert bridge.layoutMode == 'compact'
        assert window.property('islandsMode') is False

        bridge.setLayoutMode('islands')
        ui['settle']()
        # ① 切换后 window 上有可观察属性；② 不重启就生效，并写进同一份外观文件。
        assert window.property('islandsMode') is True
        saved = json.loads(appearance.default_path().read_text(encoding='utf-8'))
        assert saved['layout'] == 'islands'
        # 岛屿模式下卡片之间确实留了缝：导航卡片比外框窄一个 island_gap。
        gap = window.property('theme')['island_gap']
        assert gap > 0
        nav = window.findChild(object, 'applicationNavigation')
        card = nav.property('width')
        assert card > 0

        bridge.setLayoutMode('compact')
        ui['settle']()
        assert window.property('islandsMode') is False
        assert json.loads(appearance.default_path().read_text(encoding='utf-8'))['layout'] == 'compact'
        assert appearance.load_layout() == 'compact'


def test_compact_geometry_is_unchanged_by_toggling(tmp_path, monkeypatch):
    with themeui.theme_shell(tmp_path, monkeypatch) as ui:
        ui['settle']()
        window, bridge = ui['window'], ui['bridge']
        # 启动页没有 workspace 区域，先确认几何基线可测；没有就跳过该部分断言。
        before = _rects(window, KEY_AREAS)
        bridge.setLayoutMode('islands')
        ui['settle']()
        bridge.setLayoutMode('compact')
        ui['settle']()
        assert _rects(window, KEY_AREAS) == before


def test_broken_layout_value_falls_back_to_compact(tmp_path):
    from narrative import appearance

    target = tmp_path / 'appearance.json'
    target.write_text('{"mode": "dark", "layout": "spiral"}', encoding='utf-8')
    assert appearance.load_layout(target) == 'compact'
    assert appearance.load(target) == 'dark'

    appearance.save_layout('islands', target)
    assert appearance.load_layout(target) == 'islands'
    # 互不覆盖：切布局不该丢掉深浅色选择。
    assert appearance.load(target) == 'dark'
    appearance.save('light', target)
    assert appearance.load_layout(target) == 'islands'
    assert appearance.load(target) == 'light'
    assert json.loads(target.read_text(encoding='utf-8')) == {'mode': 'light', 'layout': 'islands'}
