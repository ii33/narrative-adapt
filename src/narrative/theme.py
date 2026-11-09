"""界面配色：浅色与深色两套设计令牌，以及配套的 QPalette。

令牌在这里定义一次：QML 通过 ``bridge.theme`` 读同一份颜色，Qt 控件（Fusion
风格的输入框、菜单、滚动条、提示气泡）用下面派生出的 QPalette。两边分开写迟早
会漂移，所以调色板只从令牌映射出来，不另存一套十六进制值。
"""

from __future__ import annotations

# 令牌名 -> (浅色, 深色)。QML 侧按名字取用，不写死十六进制。
TOKENS: dict[str, tuple[str, str]] = {
    # 文字与分隔
    "ink": ("#16233a", "#e8eef7"),
    "body": ("#33445c", "#c6d2e2"),
    "muted": ("#6d7f96", "#93a3b8"),
    "line": ("#dfe6f0", "#2a3542"),
    # 背景层次
    "paper": ("#f4f7fb", "#101620"),
    "card": ("#ffffff", "#18212c"),
    "panel": ("#ffffff", "#18212c"),
    "panelAlt": ("#f7f9fd", "#1e2833"),
    "hover": ("#f1f5fb", "#232f3d"),
    # 强调与语义色
    "accent": ("#2b5cd9", "#7aa4ff"),
    "accentSoft": ("#eaf0ff", "#22314c"),
    "onAccent": ("#ffffff", "#101a28"),
    "ok": ("#127a5e", "#4fc7a1"),
    "okSoft": ("#e8f6f1", "#14352b"),
    "warn": ("#b06a00", "#e2a94f"),
    "warnSoft": ("#fff7e8", "#3a2b14"),
    "bad": ("#b23b3f", "#f08f93"),
    "badSoft": ("#fdecee", "#3d2023"),
    # 分镜工作台：这些是工具界面，不是画面内容
    "stage": ("#eef0eb", "#151b1a"),
    "stageAlt": ("#e6ebe6", "#1a2220"),
    "stagePanel": ("#e3e8dd", "#1e2725"),
    "stageLine": ("#cad2ca", "#33403b"),
    "stageInk": ("#65726a", "#a8b6ae"),
    "lane": ("#edf0eb", "#1c2421"),
    "clip": ("#e5eae7", "#232d29"),
    "clipActive": ("#cee3db", "#2c4a41"),
    "handle": ("#9a6735", "#c99257"),
    "marker": ("#a15131", "#d4764f"),
    "record": ("#9a4429", "#e08a63"),
    # 剧本正文预览：模拟导出的白纸，深色下也保持纸的白，周围留出对比
    "previewPaper": ("#ffffff", "#ffffff"),
    "previewInk": ("#181818", "#181818"),
    "previewBackdrop": ("#e6e7e9", "#0b1017"),
    # 版式
}

LAYOUT = {"radius": 8, "gap": 12}


def tokens(dark: bool) -> dict:
    """返回给 QML 用的一份令牌（颜色是十六进制字符串，QML 直接当 color 用）。"""
    index = 1 if dark else 0
    values: dict = {name: pair[index] for name, pair in TOKENS.items()}
    values.update(LAYOUT)
    return values


def _palette_colors(dark: bool) -> dict[str, str]:
    values = tokens(dark)
    return {
        "window": values["paper"],
        "windowText": values["ink"],
        "base": values["card"],
        "alternateBase": values["panelAlt"],
        "toolTipBase": values["panelAlt"],
        "toolTipText": values["body"],
        "text": values["ink"],
        "placeholderText": values["muted"],
        "button": values["card"],
        "buttonText": values["ink"],
        "brightText": values["onAccent"],
        "highlight": values["accent"],
        "highlightedText": values["onAccent"],
        "link": values["accent"],
        "light": values["line"],
        "midlight": values["panelAlt"],
        "mid": values["line"],
        "dark": values["line"],
        "shadow": "#000000",
    }


def reset_color_scheme(app) -> None:
    """把窗口框架交回系统，让 ``colorScheme()`` 报出系统真正的值。

    显式选过浅色/深色之后，Qt 会一直回读我们钉进去的那一档；切回「跟随系统」
    前必须先复位，否则读到的还是自己上一次设的值。
    """
    from PySide6.QtCore import Qt

    hints = getattr(app, "styleHints", None)
    if hints is None:
        return
    setter = getattr(hints(), "setColorScheme", None)
    if setter is not None:
        setter(Qt.ColorScheme.Unknown)


def apply_palette(app, dark: bool, *, follow_system: bool = False) -> None:
    """把深浅色应用到 Qt 控件调色板，并让窗口框架（标题栏）跟着切。

    ``follow_system`` 用于「跟随系统」：此时不能把窗口框架钉成某一档，否则
    系统偏好会被自己写进去的值盖住（``colorScheme()`` 之后只回读我们设的值，
    系统换成深色也发现不了）。交回 Unknown 表示继续跟系统走。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPalette

    colors = _palette_colors(dark)
    roles = {
        "window": QPalette.ColorRole.Window,
        "windowText": QPalette.ColorRole.WindowText,
        "base": QPalette.ColorRole.Base,
        "alternateBase": QPalette.ColorRole.AlternateBase,
        "toolTipBase": QPalette.ColorRole.ToolTipBase,
        "toolTipText": QPalette.ColorRole.ToolTipText,
        "text": QPalette.ColorRole.Text,
        "placeholderText": QPalette.ColorRole.PlaceholderText,
        "button": QPalette.ColorRole.Button,
        "buttonText": QPalette.ColorRole.ButtonText,
        "brightText": QPalette.ColorRole.BrightText,
        "highlight": QPalette.ColorRole.Highlight,
        "highlightedText": QPalette.ColorRole.HighlightedText,
        "link": QPalette.ColorRole.Link,
        "light": QPalette.ColorRole.Light,
        "midlight": QPalette.ColorRole.Midlight,
        "mid": QPalette.ColorRole.Mid,
        "dark": QPalette.ColorRole.Dark,
        "shadow": QPalette.ColorRole.Shadow,
    }
    palette = app.palette()
    for name, role in roles.items():
        palette.setColor(role, QColor(colors[name]))
    # Disabled 文字不能靠自动灰化：深色下自动灰化会掉到背景里看不见。
    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, QPalette.ColorRole.Text, QColor(colors["placeholderText"]))
    palette.setColor(disabled, QPalette.ColorRole.WindowText, QColor(colors["placeholderText"]))
    palette.setColor(disabled, QPalette.ColorRole.ButtonText, QColor(colors["placeholderText"]))
    app.setPalette(palette)
    style_hints = getattr(app, "styleHints", None)
    if style_hints is not None:
        hints = style_hints()
        setter = getattr(hints, "setColorScheme", None)
        if setter is not None:
            # 窗口框架（标题栏）的深浅；跟随系统时交回 Unknown，保持可观测、可切换。
            if follow_system:
                setter(Qt.ColorScheme.Unknown)
            else:
                setter(Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light)


def system_dark(app) -> bool:
    """系统当前的深浅色偏好；平台不报告时按浅色处理。"""
    from PySide6.QtCore import Qt

    style_hints = getattr(app, "styleHints", None)
    if style_hints is None:
        return False
    return style_hints().colorScheme() == Qt.ColorScheme.Dark
