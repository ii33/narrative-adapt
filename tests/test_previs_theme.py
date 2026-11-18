"""预演台配色重映射：深色按原亮度落点，浅色必须反过来。"""
from __future__ import annotations

from narrative import previs_theme


def test_dark_mapping_keeps_dark_backgrounds_dark():
    assert previs_theme.map_color("#131313", True).startswith("#1")  # 深色皮肤：仍然是深的
    assert previs_theme.map_color("#e9e9e9", True).startswith("#e")  # 浅色文字仍是亮的


def test_light_mapping_inverts_their_dark_palette():
    background = previs_theme.map_color("#131313", False)
    text = previs_theme.map_color("#e9e9e9", False)
    assert background.startswith("#f"), background  # 它的深背景在浅色皮肤下变亮
    assert text.startswith("#1"), text  # 它的亮文字在浅色皮肤下变深


def test_saturated_colors_are_left_alone():
    assert previs_theme.map_color("#ff3b30") == "#ff3b30"


def test_rgb_and_rgba_are_remapped_too():
    out = previs_theme.remap_stylesheet("a{background:rgba(19,19,19,.8);color:rgb(233,233,233)}", False)
    assert "rgba(254, 254, 255,.8)" in out
    assert "rgba(18, 30, 50)" in out


def test_brand_and_menu_are_hidden():
    assert ".application-menu" in previs_theme._HIDE
    assert ".topbar .brand" in previs_theme._HIDE
