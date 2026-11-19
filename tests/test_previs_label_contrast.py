"""标签对比度：为什么标签要豁免色值重映射，以及 4.5:1 守卫。

上游 ``.object-label`` 的底色是 8 位十六进制 ``#202020ce``，而 ``_HEX`` 要求 ``\\b``
边界，``#202020ce`` 匹配不上 → 底色留在原处；文字 ``#e7e7e7`` 却能匹配上，浅色皮肤下
被映射成深色``#131f32``。深底配深字，实测 1.02:1（屏幕截图见 .runtime/spike/）。

这几条断言把这个结论钉住：标签规则不再被重映射、注入的样式本身过 4.5:1、
守卫遇到低对比色对时换成黑底白字。
"""

from __future__ import annotations

import re

from narrative import previs_theme

# 上游 dist 里那条标签规则（颜色原样抄自 main-*.css）。
LABEL_RULE = ".object-label{background:#202020ce;color:#e7e7e7;border:1px solid #6e6e6e88}"


def _declarations(css_block: str) -> dict[str, str]:
    body = css_block[css_block.index("{") + 1:css_block.rindex("}")]
    pairs: dict[str, str] = {}
    for piece in body.split(";"):
        if ":" in piece:
            key, value = piece.split(":", 1)
            pairs[key.strip()] = value.strip()
    return pairs


def _label_rule(css: str) -> str:
    match = re.search(r"#object-labels \.object-label, \.object-label\s*\{[^}]*\}", css)
    assert match, "注入的样式里没有标签规则"
    return match.group(0)


def test_label_rules_are_excluded_from_remap():
    """标签底色与文字色都不许被映射：两边一起改等于什么都没保证。"""
    for dark in (True, False):
        assert previs_theme.remap_stylesheet(LABEL_RULE, dark) == LABEL_RULE


def test_neighbouring_rules_are_still_remapped():
    css = ".topline{color:#131313}.object-label{color:#e7e7e7}"
    out = previs_theme.remap_stylesheet(css, False)
    assert ".topline{color:#131313}" not in out
    assert ".object-label{color:#e7e7e7}" in out


def test_label_exclusion_reaches_into_media_queries():
    """上游那条 8px 字号写在 @media 里：递归拆块必须一路管到里面。"""
    css = "@media(max-width:1400px){.object-label{font-size:8px;color:#eeeeee}.panel{color:#131313}}"
    out = previs_theme.remap_stylesheet(css, False)
    assert ".object-label{font-size:8px;color:#eeeeee}" in out
    assert ".panel{color:#131313}" not in out


def test_quoted_braces_do_not_break_the_splitter():
    css = 'a{content:"{";color:#131313}.object-label{color:#e7e7e7}'
    out = previs_theme.remap_stylesheet(css, False)
    assert '.object-label{color:#e7e7e7}' in out
    assert 'content:"{"' in out


def test_upstream_label_pair_is_unreadable_in_light_skin():
    """修之前的实测值：文字被映射成深色，和没被映射的深底色撞在一起。"""
    mapped = previs_theme.map_color("#e7e7e7", False)
    assert previs_theme.label_contrast("#202020ce", mapped) < 2.0


def test_injected_label_colors_pass_aa():
    declarations = _declarations(_label_rule(previs_theme.label_css()))
    worst = previs_theme.label_contrast(declarations["background"], declarations["color"])
    assert worst >= 4.5, f"标签对比度只有 {worst:.2f}:1"


def test_injected_label_style_matches_the_agreed_spec():
    declarations = _declarations(_label_rule(previs_theme.label_css()))
    assert declarations["background"] == "rgba(12, 14, 18, .86)"
    assert declarations["color"] == "#f2f4f6"
    assert declarations["font-weight"] == "600"
    assert int(declarations["font-size"].removesuffix("px")) >= 13
    assert declarations["padding"] == "2px 6px"
    assert declarations["border-radius"] == "4px"
    assert declarations["line-height"] == "1.25"
    assert "0 1px 2px" in declarations["text-shadow"]


def test_themed_css_appends_the_label_style_after_the_remapped_sheets(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.css").write_text(LABEL_RULE, encoding="utf-8")
    css = previs_theme.themed_css(tmp_path, False)
    # 上游那条原样留着（豁免生效），注入的样式排在它后面才能靠顺序赢下来。
    assert LABEL_RULE in css
    assert previs_theme.label_css() in css
    assert css.index(previs_theme.label_css()) > css.index(LABEL_RULE)


def test_guard_falls_back_to_black_on_white():
    background, foreground = previs_theme.readable_label_colors("#202020ce", "#131f32")
    assert (background, foreground) == (previs_theme.LABEL_FALLBACK_BG, previs_theme.LABEL_FALLBACK_FG)
    assert previs_theme.label_contrast(background, foreground) >= 4.5


def test_contrast_ratio_matches_wcag_reference_values():
    assert round(previs_theme.contrast_ratio((0, 0, 0), (255, 255, 255)), 1) == 21.0
    assert round(previs_theme.contrast_ratio((0, 0, 0), (0, 0, 0)), 1) == 1.0
