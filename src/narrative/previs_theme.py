"""三维预演台的配色重映射：把它那套中性灰整体挪到本项目主题令牌上。

做法不是"逐条写覆盖"，而是**读它的样式表、按亮度重映射每一个十六进制颜色，再整份注入**。
这样连它写死的面板色、边框色、悬停色都一起被换掉，不会漏掉某一块仍是灰的。

三条边界：
- 3D 视口里的画面**不染色**：颜色来自 canvas 与纹理，样式表管不到，天然安全；
- 有语义的颜色（红/绿/黄等饱和色）**保持原样**，只搬中性灰；
- **对象名标签（`.object-label` / `#object-labels`）整块不参与重映射**，颜色由
  `label_css()` 单独注入。为什么：它的底色是 8 位十六进制（`#202020ce`），而
  `_HEX` 要求 `\\b` 边界，`#202020ce` 匹配不上 → 底色留在深色；文字 `#e7e7e7`
  却能匹配上 → 浅色皮肤下被映射成深色。一边深一边浅正好撞在一起，标签就成了
  深底深字（实测 1.02:1）。标签是**唯一**必须"看起来一模一样"的东西（DOM 一份、
  抓帧 sprite 一份），让两套颜色规则各管一半必然出错，所以干脆只留一条来源。
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
# 拆规则时只关心这三类起点：块开始、注释、字符串。用正则找下一个而不是逐字符走。
_INTERESTING = re.compile(r"/\*|[\"'{]", re.S)

# 对象名标签：整块豁免重映射，颜色只由 label_css() 给（DOM 与 sprite 共用这一组）。
# 为什么用近黑 86% 而不是更透的底：标签浮在 3D 画面上，透光越多、背后的墙面越亮，
# 底色就被"抬"得越高，文字的对比度越低（实测上游 81% 透光时，字色对比从 13.2:1 掉到 8.3:1）。
_PROTECTED_SELECTORS = ("object-label",)
LABEL_BG = "rgba(12, 14, 18, .86)"
LABEL_FG = "#f2f4f6"
LABEL_FALLBACK_BG = "#000000"
LABEL_FALLBACK_FG = "#ffffff"
LABEL_MIN_FONT_PX = 13  # 再小就不是"看不清"而是"不存在"了：9px 中文在 100% 缩放下一团墨
LABEL_MIN_CONTRAST = 4.5  # WCAG 2.1 AA 正文字号阈值
# 标签是半透明的，背后场景的亮度会渗进来。这里取项目配色里最亮的面（墙地面 #cfc9bd）
# 与最暗的底（paper #0b0f16）：两种极端都过 4.5:1 才算"换到任何场景都读得出名字"。
_LABEL_BACKDROPS: tuple[tuple[int, int, int], ...] = ((0xCF, 0xC9, 0xBD), (0x0B, 0x0F, 0x16))


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


def _remap_declarations(text: str, dark: bool) -> str:
    """把一段声明里的六进制与 rgb()/rgba() 全部换掉（先六进制再 rgb，顺序不能反）。"""
    text = _HEX.sub(lambda match: map_color(match.group(0), dark), text)
    return _RGB.sub(lambda match: _remap_rgb(match, dark), text)


def _skip_comment(css: str, index: int) -> int:
    end = css.find("*/", index + 2)
    return len(css) if end < 0 else end + 2


def _skip_string(css: str, index: int) -> int:
    """跳过 `"…"` / `'…'`：样式表里有 content:"{" 这种写法，不能把它当成块开始。"""
    quote = css[index]
    cursor = index + 1
    while cursor < len(css):
        char = css[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == quote:
            return cursor + 1
        cursor += 1
    return len(css)


def _block_end(css: str, index: int) -> int:
    """返回与 index 处 `{` 配对的 `}` 之后的位置；没闭合就返回末尾。"""
    depth = 0
    cursor = index
    while cursor < len(css):
        char = css[cursor]
        if char == "/" and css.startswith("/*", cursor):
            cursor = _skip_comment(css, cursor)
            continue
        if char in "\"'":
            cursor = _skip_string(css, cursor)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return len(css)


def _has_nested_block(body: str) -> bool:
    """块体里还有块 → 这是 @media / @supports / @keyframes 这类容器。"""
    index = 0
    while True:
        match = _INTERESTING.search(body, index)
        if match is None:
            return False
        token, position = match.group(0), match.start()
        if token == "/*":
            index = _skip_comment(body, position)
            continue
        if token in "\"'":
            index = _skip_string(body, position)
            continue
        return True


def _selector_of(prelude: str) -> str:
    return re.sub(r"/\*.*?\*/", "", prelude, flags=re.S)


def _remap_blocks(css: str, dark: bool, protected: tuple[str, ...]) -> str:
    """逐条规则重映射；选择器命中 protected 的规则**原样保留**，容器块递归往下走。

    为什么要拆规则而不是整份正则替换：豁免是按「选择器」生效的。只看声明分不出
    这一行的颜色属于标签还是属于面板，而整份替换会把标签和面板一起换掉。

    扫描交给正则（`_INTERESTING`）而不是逐个字符：样式表 13 万字符，逐字符的
    Python 循环要 120ms 以上，而换肤脚本是开页面前要跑的。
    """
    out: list[str] = []
    index = 0
    segment = 0  # 当前这块规则的 prelude 起点（含注释与空白）
    while True:
        match = _INTERESTING.search(css, index)
        if match is None:
            break
        token, position = match.group(0), match.start()
        if token == "/*":
            index = _skip_comment(css, position)
            continue
        if token in "\"'":
            index = _skip_string(css, position)
            continue
        end = _block_end(css, position)
        closed = end > position + 1 and css[end - 1] == "}"
        body = css[position + 1:end - 1] if closed else css[position + 1:end]
        prelude = css[segment:position]
        if _has_nested_block(body):
            mapped = _remap_blocks(body, dark, protected)
        elif any(name in _selector_of(prelude) for name in protected):
            mapped = body  # 标签规则整条不重映射
        else:
            mapped = _remap_declarations(body, dark)
        # 统一补上右括号：被截断的样式表（少见）也因此还是平衡的。
        out.append(f"{prelude}{{{mapped}}}")
        index = segment = end
    out.append(css[segment:])
    return "".join(out)


def remap_stylesheet(css: str, dark: bool = True, protect: bool = True) -> str:
    """重映射一份样式表；默认把标签规则排除在外（见模块文档的第三条边界）。"""
    if not protect:
        return _remap_declarations(css, dark)
    return _remap_blocks(css, dark, _PROTECTED_SELECTORS)


# ---------------------------------------------------------------- 标签对比度守卫


def wcag_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.1 的相对亮度：先做 sRGB 反伽马再按系数加权。

    上面的 `_relative_luminance` 是给"按亮度落点"用的经验值（不带反伽马，改它会把整套
    配色的落点挪走），算对比度必须用标准公式，比出来的数才对得上 4.5:1 这条线。
    """

    def channel(value: int) -> float:
        raw = value / 255
        return raw / 12.92 if raw <= 0.03928 else ((raw + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    high, low = sorted((wcag_luminance(first), wcag_luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


_CSS_RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:[,/]\s*([\d.]+)\s*)?\)")


def parse_color(text: str) -> tuple[tuple[int, int, int], float]:
    """只认标签实际会用到的写法：#rrggbb、#rrggbbaa 与 rgb()/rgba()；其余报错，不猜。"""
    raw = text.strip()
    hex_match = re.fullmatch(r"#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?", raw)
    if hex_match:
        digits = hex_match.group(1)
        alpha = int(hex_match.group(2), 16) / 255 if hex_match.group(2) else 1.0
        return tuple(int(digits[index:index + 2], 16) for index in (0, 2, 4)), alpha
    rgb_match = _CSS_RGB.fullmatch(raw)
    if not rgb_match:
        raise ValueError(f"解析不了的颜色：{text!r}")
    rgb = tuple(int(rgb_match.group(index)) for index in (1, 2, 3))
    alpha = float(rgb_match.group(4)) if rgb_match.group(4) is not None else 1.0
    return rgb, alpha


def composite(foreground: tuple[int, int, int], alpha: float,
              backdrop: tuple[int, int, int]) -> tuple[int, int, int]:
    """把半透明前景压到背景上：标签只挡掉 86%，屏幕上真正的底色是混出来的那个。"""
    return tuple(round(foreground[index] * alpha + backdrop[index] * (1 - alpha)) for index in range(3))


def label_contrast(background: str = LABEL_BG, foreground: str = LABEL_FG) -> float:
    """标签文字对它底色的对比度：取所有典型场景底色里**最差**的那个。"""
    backdrop_rgb, alpha = parse_color(background)
    text_rgb, _ = parse_color(foreground)
    return min(contrast_ratio(text_rgb, composite(backdrop_rgb, alpha, scene))
               for scene in _LABEL_BACKDROPS)


def readable_label_colors(background: str = LABEL_BG, foreground: str = LABEL_FG,
                          minimum: float = LABEL_MIN_CONTRAST) -> tuple[str, str]:
    """守卫：这一对颜色低于 minimum 就换成黑底白字，而不是让用户看到糊掉的标签。

    为什么不抛异常：换肤是渲染期的事，页面不能因为某个人改了主题令牌就白屏；
    黑底白字在任何场景下都是 21:1，先保证读得出来，颜色好看是第二位。
    """
    if label_contrast(background, foreground) >= minimum:
        return background, foreground
    return LABEL_FALLBACK_BG, LABEL_FALLBACK_FG


def label_css() -> str:
    """标签的高对比样式：DOM 标签与 readability.ts 的 sprite 共用这一组颜色和字号比例。"""
    background, foreground = readable_label_colors()
    red, green, blue = parse_color(foreground)[0]
    border = f"rgba({red}, {green}, {blue}, .28)"
    return f"""
/* 对象名标签 ────────────────────────────────────────────────────────────
   这一块**不参与**色值重映射（见 _PROTECTED_SELECTORS），并且是 DOM 标签与
   readability.ts 抓帧 sprite 标签的唯一颜色来源：两处一旦不同，抓帧 PNG 和
   屏幕上看到的就不是一个东西。
   底色选 86% 不透光并去掉背景模糊：模糊会把背后的亮墙糊进标签底，实测能把
   文字的对比度从 13.2:1 拖到 8.3:1，字就跟着底色一起糊了。
   font-size 的 13px 只是兜底，真正的距离缩放在 main.ts 里（下限同样 13px）。 */
#object-labels .object-label, .object-label {{
  background: {background};
  color: {foreground};
  font-weight: 600;
  font-size: {LABEL_MIN_FONT_PX}px;
  line-height: 1.25;
  text-shadow: 0 1px 2px rgba(0, 0, 0, .6);
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid {border};
  backdrop-filter: none;
  -webkit-backdrop-filter: none;
}}
/* 左侧那颗"这是谁"的色点在 13px 字号下要跟着放大一点，否则只剩一个像素的灰点。 */
#object-labels .object-label i, .object-label i {{ width: 5px; height: 5px; }}
"""


# 它的顶栏里有品牌 logo 与应用菜单：我们去掉了，把位置让给内容。
_HIDE = """
.topbar .brand, .topbar .version, .topbar .top-separator:first-of-type,
.application-menu { display: none !important; }
/* 摘掉品牌与菜单后，顶栏还留着为两行准备的高度，上面就空出一条。
   这里把它收成一行的高度：内容自己撑开，不再预留。 */
/* 它的顶栏折成两行、样式也与我们不同：整条藏掉，由我们自己的工具条驱动。
   节点仍然留在 DOM 里（display:none 的按钮照样能 click），所以保存/导出照旧可用。 */
/* 不能用 display:none：它是 grid 的子项，删掉会让后面的行整体上移一行。
   压成 0 高、隐藏，格子还在，布局不错位。 */
body .topbar { height: 0 !important; min-height: 0 !important; padding: 0 !important;
               margin: 0 !important; border: none !important; overflow: hidden !important;
               visibility: hidden !important; }
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
    # 标签样式放在最后：它以同样的选择器权重覆盖上游那条 `.object-label`（上游那条
    # 大概率是 remap 之后还剩的同一套灰），顺序靠后就能赢，不需要 !important。
    chunks.append(label_css())
    return "\n".join(chunks)


# 给我们的工具条用的小接口：列场景、切场景、点它自己的按钮。
_API = """
window.__narrativePrevis = {
  scenes: function () {
    const select = document.querySelector('#scene-switch');
    return select ? Array.from(select.options).map(o => ({value: o.value, label: o.textContent})) : [];
  },
  current: function () {
    const select = document.querySelector('#scene-switch');
    return select ? select.value : '';
  },
  select: function (value) {
    const select = document.querySelector('#scene-switch');
    if (!select) return false;
    select.value = value;
    select.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  },
  act: function (name) {
    const button = document.querySelector('[data-act="' + name + '"]');
    if (!button) return false;
    button.click();
    return true;
  }
};
"""


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
        f"  {_API}"
        "  return 'ok';"
        "})();"
    )
