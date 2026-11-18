"""三维预演台的配色重映射：把它那套中性灰整体挪到本项目主题令牌上。

做法不是"逐条写覆盖"，而是**读它的样式表、按亮度重映射每一个十六进制颜色，再整份注入**。
这样连它写死的面板色、边框色、悬停色都一起被换掉，不会漏掉某一块仍是灰的。

两条边界：
- 3D 视口里的画面**不染色**：颜色来自 canvas 与纹理，样式表管不到，天然安全；
- 有语义的颜色（红/绿/黄等饱和色）**保持原样**，只搬中性灰。
"""

from __future__ import annotations

import colorsys
import json
import re
from pathlib import Path

# 深色阶梯：按亮度线性插值，灰阶整体带一点蓝。
_DARK_RAMP: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.00, (0x0B, 0x0F, 0x16)),
    (0.06, (0x10, 0x16, 0x20)),   # paper
    (0.11, (0x18, 0x21, 0x2C)),   # card
    (0.15, (0x1E, 0x28, 0x33)),   # panelAlt
    (0.19, (0x23, 0x2F, 0x3D)),   # hover
    (0.25, (0x2A, 0x35, 0x42)),   # line
    (0.34, (0x33, 0x45, 0x5C)),
    (0.55, (0x93, 0xA3, 0xB8)),   # muted
    (0.74, (0xC6, 0xD2, 0xE2)),   # body
    (0.92, (0xE8, 0xEE, 0xF7)),   # ink
    (1.00, (0xF4, 0xF7, 0xFB)),
)

# 浅色阶梯：同一个亮度坐标，落点是本项目的浅色令牌。
_LIGHT_RAMP: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.00, (0x0F, 0x1A, 0x2A)),
    (0.18, (0x16, 0x23, 0x3A)),   # ink
    (0.40, (0x33, 0x44, 0x5C)),   # body
    (0.55, (0x6D, 0x7F, 0x96)),   # muted
    (0.72, (0xDF, 0xE6, 0xF0)),   # line
    (0.80, (0xF1, 0xF5, 0xFB)),   # hover
    (0.86, (0xF7, 0xF9, 0xFD)),   # panelAlt
    (0.92, (0xFF, 0xFF, 0xFF)),   # card
    (1.00, (0xF4, 0xF7, 0xFB)),   # paper
)

_HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
# 它的样式表里也有 rgb()/rgba()：只换十六进制会漏掉这些半透明层，
# 浅色皮肤下就会残留一块块深色。
_RGB = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(,\s*[\d.]+\s*)?\)")
_MAX_SATURATION = 0.18  # 超过这个饱和度视为“有语义的颜色”，不动


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    red, green, blue = (value / 255 for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _lerp_ramp(luminance: float, ramp) -> tuple[int, int, int]:
    for index in range(len(ramp) - 1):
        low, low_rgb = ramp[index]
        high, high_rgb = ramp[index + 1]
        if low <= luminance <= high:
            ratio = 0.0 if high == low else (luminance - low) / (high - low)
            return tuple(round(low_rgb[i] + (high_rgb[i] - low_rgb[i]) * ratio) for i in range(3))
    return ramp[-1][1] if luminance > ramp[-1][0] else ramp[0][1]


def map_color(text: str, dark: bool = True) -> str:
    """把一个十六进制颜色搬到本项目的深色阶梯上；饱和色与解析不了的保持原样。"""
    raw = text.lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    try:
        rgb = tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return text
    _, lightness, saturation = colorsys.rgb_to_hls(*(value / 255 for value in rgb))
    if saturation > _MAX_SATURATION:
        return text
    if dark and lightness > 0.94:  # 深色皮肤下纯白常用于图标/描边，保留
        return text
    luminance = _relative_luminance(rgb)
    # 深色皮肤按原亮度落点；浅色皮肤必须**反过来**：它的背景本来就暗，
    # 照原亮度映射会得到一堆深色块，看着还是深色界面。
    mapped = _lerp_ramp(luminance, _DARK_RAMP) if dark else _lerp_ramp(1.0 - luminance, _LIGHT_RAMP)
    return "#{:02x}{:02x}{:02x}".format(*mapped)


def _remap_rgb(match: re.Match, dark: bool) -> str:
    rgb = tuple(int(match.group(index)) for index in (1, 2, 3))
    alpha = match.group(4) or ""
    mapped = map_color("#{:02x}{:02x}{:02x}".format(*rgb), dark)
    red, green, blue = (int(mapped[1:3], 16), int(mapped[3:5], 16), int(mapped[5:7], 16))
    return f"rgba({red}, {green}, {blue}{alpha})"


def remap_stylesheet(css: str, dark: bool = True) -> str:
    css = _HEX.sub(lambda match: map_color(match.group(0), dark), css)
    return _RGB.sub(lambda match: _remap_rgb(match, dark), css)


# 它的顶栏里有品牌 logo 与应用菜单：我们去掉了，把位置让给内容。
_HIDE = """
.topbar .brand, .topbar .version, .topbar .top-separator:first-of-type,
.application-menu { display: none !important; }
/* 摘掉品牌与菜单后，顶栏还留着为两行准备的高度，上面就空出一条。
   这里把它收成一行的高度：内容自己撑开，不再预留。 */
/* 它的整体布局是 grid，第一行（顶栏）写死 62px——这就是顶部那条空白的来源。
   改成 auto，让顶栏按内容撑开；右边给常驻的窗口按钮留 118px。 */
#app { grid-template-rows: auto minmax(320px, 1fr) var(--timeline-height, 233px) 27px !important; }
/* padding 归零去掉顶部留白；右边留 118px 给常驻的窗口按钮。 */
body .topbar { min-height: 0 !important; height: auto !important; padding: 4px 118px 4px 12px !important;
               align-content: center !important; flex-wrap: wrap !important; }
body .topbar > * { flex: 0 0 auto; }
body .topbar .scene-switcher { margin-right: auto; }
"""


def themed_css(dist: Path, dark: bool = True) -> str:
    """把产物里所有样式表重映射后拼成一份；找不到就返回空串。"""
    chunks: list[str] = []
    for sheet in sorted((dist / "assets").glob("*.css")):
        try:
            chunks.append(remap_stylesheet(sheet.read_text(encoding="utf-8"), dark))
        except OSError:
            continue
    chunks.append(_HIDE)
    return "\n".join(chunks)


def inject_script(css: str) -> str:
    """在页面里插一段 <style>，并把顶栏里我们不要的节点直接摘掉。

    只靠 `display:none` 会在顶栏里留下一行空白（它按 flex 换行布局），
    摘掉节点才会真正收起来。
    """
    return (
        "(function () {"
        "  document.querySelectorAll('.topbar .brand, .topbar .version, .application-menu')"
        "    .forEach(function (node) { node.remove(); });"
        "  const old = document.getElementById('narrative-theme');"
        "  if (old) old.remove();"
        "  const style = document.createElement('style');"
        "  style.id = 'narrative-theme';"
        f"  style.textContent = {json.dumps(css)};"
        "  document.head.appendChild(style);"
        "  return 'ok';"
        "})();"
    )
