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

# 本项目的深色阶梯：按亮度线性插值，灰阶整体带一点蓝。
_RAMP: tuple[tuple[float, tuple[int, int, int]], ...] = (
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

_HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_MAX_SATURATION = 0.18  # 超过这个饱和度视为“有语义的颜色”，不动


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    red, green, blue = (value / 255 for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _lerp_ramp(luminance: float) -> tuple[int, int, int]:
    for index in range(len(_RAMP) - 1):
        low, low_rgb = _RAMP[index]
        high, high_rgb = _RAMP[index + 1]
        if low <= luminance <= high:
            ratio = 0.0 if high == low else (luminance - low) / (high - low)
            return tuple(round(low_rgb[i] + (high_rgb[i] - low_rgb[i]) * ratio) for i in range(3))
    return _RAMP[-1][1] if luminance > _RAMP[-1][0] else _RAMP[0][1]


def map_color(text: str) -> str:
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
    if lightness > 0.94:  # 纯白常用于图标/描边，保留
        return text
    mapped = _lerp_ramp(_relative_luminance(rgb))
    return "#{:02x}{:02x}{:02x}".format(*mapped)


def remap_stylesheet(css: str) -> str:
    return _HEX.sub(lambda match: map_color(match.group(0)), css)


def themed_css(dist: Path) -> str:
    """把产物里所有样式表重映射后拼成一份；找不到就返回空串。"""
    chunks: list[str] = []
    for sheet in sorted((dist / "assets").glob("*.css")):
        try:
            chunks.append(remap_stylesheet(sheet.read_text(encoding="utf-8")))
        except OSError:
            continue
    return "\n".join(chunks)


def inject_script(css: str) -> str:
    """在页面里插一段 <style>；重复注入时替换旧的，不叠加。"""
    return (
        "(function () {"
        "  const old = document.getElementById('narrative-theme');"
        "  if (old) old.remove();"
        "  const style = document.createElement('style');"
        "  style.id = 'narrative-theme';"
        f"  style.textContent = {json.dumps(css)};"
        "  document.head.appendChild(style);"
        "  return 'ok';"
        "})();"
    )
