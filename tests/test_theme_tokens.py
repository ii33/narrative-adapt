"""界面令牌与弹层配色的静态守卫：QML 只认令牌名，弹层不写死颜色。

两条规则都来自真实缺陷：``LauncherPage.qml`` 的「移除工程」弹层曾写成
``t.color``（令牌表里只有 ``card``），绑定取到 undefined 后弹层退回窗口默认白底，
深色界面里就成了白底浅字。颜色写错容易在浅色下看不出来，令牌名写错更是只在深色
下才现形，所以这两条用静态扫描常驻守卫。

扫描全部是文本读取：不建工程、不加载 QML、不碰数据库，默认套件里跑。
每条规则都配一条「故意写坏」的正向用例，确保守卫真的会红，而不是无声通过。
"""

from __future__ import annotations

import re
from pathlib import Path

from narrative.theme import LAYOUT, TOKENS

QML_DIR = Path(__file__).resolve().parents[1] / "src" / "narrative" / "qml"

# t.<name> / app.theme.<name> / bridge.theme.<name>：QML 侧读令牌的三种写法。
TOKEN_REF = re.compile(
    r"\bt\.([A-Za-z_]\w*)"
    r"|\bapp\.theme\.([A-Za-z_]\w*)"
    r"|\bbridge\.theme\.([A-Za-z_]\w*)"
)
# 弹层块：Popup / Dialog / Menu（含导出控件时的 `Popup {`）。
POPUP_START = re.compile(r"\b(Dialog|Popup|Menu)\s*\{")
HARD_CODED = re.compile(r"#[0-9a-fA-F]{3,8}\b|\"(?:white|black)\"")
STRING_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')
LINE_COMMENT = re.compile(r"//[^\n]*")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)

KNOWN_TOKENS = set(TOKENS) | set(LAYOUT)


def _strip_comments(text: str) -> str:
    """去掉注释，但保留字符串与换行，便于按下标回算行号。"""
    text = BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    return LINE_COMMENT.sub("", text)


def _without_strings(line: str) -> str:
    return STRING_LITERAL.sub('""', line)


def popup_blocks(text: str):
    """逐个产出 (起始行号, 弹层类型, 块文本)。花括号按去注释、去字符串后的文本配平。"""
    lines = _strip_comments(text).splitlines()
    for index, line in enumerate(lines):
        match = POPUP_START.search(line)
        if not match:
            continue
        depth = 0
        started = False
        body = []
        for offset in range(index, len(lines)):
            for char in _without_strings(lines[offset]):
                if char == "{":
                    depth += 1
                    started = True
                elif char == "}":
                    depth -= 1
            body.append(lines[offset])
            if started and depth <= 0:
                break
        yield index + 1, match.group(1), "\n".join(body)


def unknown_token_problems(sources: dict[str, str]) -> list[str]:
    """报告引用了主题表里不存在的令牌。"""
    problems = []
    for name, text in sorted(sources.items()):
        for number, line in enumerate(_strip_comments(text).splitlines(), 1):
            for match in TOKEN_REF.finditer(line):
                token = match.group(1) or match.group(2) or match.group(3)
                if token not in KNOWN_TOKENS:
                    problems.append(f"{name}:{number} → 未知令牌 {token}")
    return problems


def popup_color_problems(sources: dict[str, str]) -> list[str]:
    """报告弹层作用域内写死的颜色；弹层之外（内容画布）不看。"""
    problems = []
    for name, text in sorted(sources.items()):
        for line_no, kind, body in popup_blocks(text):
            for offset, line in enumerate(body.splitlines()):
                # 这里不能先抹掉字符串：写死颜色常常就是 "white" 这种字符串字面量。
                hits = HARD_CODED.findall(line)
                if hits:
                    problems.append(
                        f"{name}:{line_no + offset} → {kind} 内写死颜色 {', '.join(hits)}"
                    )
    return problems


def qml_sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(QML_DIR.glob("*.qml"))}


def test_every_token_reference_exists_in_theme():
    problems = unknown_token_problems(qml_sources())
    assert not problems, "QML 引用了 theme.py 里不存在的令牌（绑定会静默失效，深色下才现形）：\n" + "\n".join(
        problems
    )


def test_no_hard_coded_colors_inside_popups():
    problems = popup_color_problems(qml_sources())
    assert not problems, "弹层里写了固定颜色，深色下不会跟随主题：\n" + "\n".join(problems)


def test_unknown_token_is_reported():
    """正向用例：这条守卫真的会红。"""
    sources = {"Broken.qml": 'Rectangle { color: page.t.color; border.color: t.line }'}
    assert unknown_token_problems(sources) == ["Broken.qml:1 → 未知令牌 color"]


def test_known_tokens_and_layout_values_are_accepted():
    sources = {
        "Fine.qml": (
            "Rectangle { color: app.theme.card }\n"
            "Label { color: page.t.muted; radius: bridge.theme.radius }"
        )
    }
    assert unknown_token_problems(sources) == []


def test_hard_coded_color_inside_popup_is_reported():
    """正向用例：弹层里写死颜色会被抓到，行号指向真正那一行。"""
    sources = {
        "Broken.qml": (
            "Dialog {\n"
            "    id: dialog\n"
            "    background: Rectangle { color: \"#ffffff\" }\n"
            "    contentItem: Label { color: \"white\" }\n"
            "}\n"
        )
    }
    assert popup_color_problems(sources) == [
        'Broken.qml:3 → Dialog 内写死颜色 #ffffff',
        'Broken.qml:4 → Dialog 内写死颜色 "white"',
    ]


def test_hard_coded_color_outside_popup_is_ignored():
    """内容画布（镜头画面、分镜示意）保持自身配色，不算违规。"""
    sources = {
        "Canvas.qml": (
            "Item {\n"
            "    Rectangle { color: \"white\" }\n"
            "    Canvas { onPaint: { ctx.fillStyle = \"#26302c\" } }\n"
            "}\n"
        )
    }
    assert popup_color_problems(sources) == []


def test_nested_popups_and_comments_do_not_confuse_the_scanner():
    sources = {
        "Nested.qml": (
            "Popup {\n"
            "    // 注释里的 #ffffff 与 t.nope 不算\n"
            "    Menu { id: inner\n"
            "        background: Rectangle { color: t.card }\n"
            "    }\n"
            "    Label { text: \"}\" }\n"
            "}\n"
        )
    }
    assert popup_color_problems(sources) == []
    assert unknown_token_problems(sources) == []
