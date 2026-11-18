"""三维预演台的配色覆盖：把它那套中性灰映射到本项目主题令牌。

为什么只改颜色、不改结构：结构、密度、字体都是它的产品决策，硬改会与上游版本越走越远；
而"底色、线、文字、强调色"是用户一眼看出"像不像同一个软件"的部分。

3D 视口里的画面**一律不染色**——用户要靠它判断光线与材质，任何滤镜都会让判断失真。
"""

from __future__ import annotations

# 覆盖到它用到的那些区域类：面板、对象列表、时间轴、检查器、按钮、输入框、滚动条。
_SELECTORS = """
html, body, .workspace, .center, .viewports, .timeline, .timeline-tracks, .timeline-title,
.stage-panel, .side-tabs, .inspector, .boundary-inspector, .boundary-sidebar, .boundary-split,
.boundary-timeline, .layout-boundary { background: %paper%; color: %ink%; border-color: %line%; }

.panel, .shot-panel, .header-actions, .tools, .transport, .ruler, .monitor-strip,
.asset-card, .info-box, .key-row, .field, .field-pair, .action-grid, .button-row {
  background: %card%; border-color: %line%; color: %ink%;
}

.object-row, .track-label, .timeline-clip, .joint-field, .duration-input {
  border-color: %line%; color: %ink%;
}
.object-row:hover, .track-label:hover, .mini-button:hover, .icon-button:hover { background: %hover%; }
.object-row.active, .side-tabs .active { background: %accentSoft%; }

.timeline-clip { background: %accentSoft%; }
.brand, .logo, .accent, .monitor-label { color: %accent%; }

button { background: %panelAlt%; border-color: %line%; color: %ink%; }
button:hover { background: %hover%; border-color: %accentSoft%; }
button:focus-visible, input:focus-visible, select:focus-visible { outline-color: %accent%; }

input, select, textarea { background: %card%; border-color: %line%; color: %ink%; }
input::placeholder { color: %muted%; }

.dimmed, .empty-state, .inspect-subtitle, .monitor-label { color: %muted%; }

::-webkit-scrollbar { width: 12px; height: 12px; }
::-webkit-scrollbar-track { background: %paper%; }
::-webkit-scrollbar-thumb { background: %line%; border-radius: 6px; }
::-webkit-scrollbar-thumb:hover { background: %panelAlt%; }
"""

_KEYS = ("ink", "paper", "card", "line", "muted", "accent", "accentSoft", "panelAlt", "hover")


def theme_css(tokens: dict[str, str]) -> str:
    """把主题令牌填进覆盖样式；缺哪个令牌就保持原样，不编颜色。"""
    css = _SELECTORS
    for key in _KEYS:
        value = tokens.get(key)
        if value:
            css = css.replace(f"%{key}%", value)
    return " ".join(css.split())


def inject_script(css: str) -> str:
    """在页面里插一段 <style>；重复注入时替换旧的，不叠加。"""
    import json

    return (
        "(function () {"
        "  const old = document.getElementById('narrative-theme');"
        "  if (old) old.remove();"
        f"  const style = document.createElement('style');"
        f"  style.id = 'narrative-theme';"
        f"  style.textContent = {json.dumps(css)};"
        "  document.head.appendChild(style);"
        "  return 'ok';"
        "})();"
    )
