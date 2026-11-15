"""提取规范页的守卫：编号、锚点、索引与 §0 权威声明。

`docs/extraction/SPEC.md` 是提取任务的唯一规范页（P1 把 INVARIANTS、属性合同、属性分类、
MODEL 第 2、3 节搬了进来）。这里只查“搬进来之后会不会漂”的四件事：

1. 页面引用的诊断码必须真的在 ``src/`` 里抛得出来；
2. 页面里的 ``文件.py::符号`` 锚点必须真的存在（复用 ``test_doc_consistency`` 的解析器）；
3. ``docs/README.md`` 必须列出 ``docs/`` 下的每个 ``.md``——新增文档不能没人知道；
4. §0 的三条权威声明必须还在（有人删掉就等于取消单一真源）。

纯文本，不建工程、不碰数据库，默认套件可并行跑。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_doc_consistency import (
    DOCS,
    SPEC,
    check_diagnostic_codes,
    check_symbol_refs,
    render_text,
)

ROOT = Path(__file__).resolve().parents[1]

# §0 的三条是「唯一真源 + 编号 + 同批更新」的声明，措辞在这里锁前缀，避免整句锁死。
AUTHORITY_STATEMENTS = (
    "本页是提取任务的唯一规范。",
    "编号：`M-xx` 模型判断规则、`E-xx` 提示词规范化出的硬约束、`I-xx` 程序强制的不变量、`G-xx` 术语。",
    "改提取（提示词 / 校验 / 工具 / 诊断码）必须同批更新本页对应条目与 §7 的提示词版本表",
)

SECTION_HEADINGS = (
    "## 0 权威边界与变更纪律",
    "## 1 范围、引用与批次",
    "## 2 流程（读 → 提交 → staged → patch → accepted → 静态检查）",
    "## 3 模型职责（E-xx）",
    "## 4 模型判断规则（M-xx）",
    "## 5 程序强制（I-xx）与属性状态合同",
    "## 6 诊断码总表",
    "## 7 提示词索引",
)


def _spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


def _problems_text(problems) -> str:
    """空列表代表通过；string 断言因此稳定（render_text 对空列表会给“没有发现问题”）。"""
    return render_text(problems) if problems else ""


def test_spec_exists_and_has_eight_sections():
    """八节标题是后续阶段的锚点，改名会让引用集体失效。"""
    assert SPEC.is_file(), f"缺少规范页 {SPEC}"
    text = _spec_text()
    missing = [heading for heading in SECTION_HEADINGS if heading not in text.splitlines()]
    assert not missing, f"SPEC 缺少章节：{missing}"


def test_spec_keeps_authority_statements():
    """§0 的三条权威声明不得被删；删掉就等于取消单一真源。"""
    text = _spec_text()
    missing = [statement for statement in AUTHORITY_STATEMENTS if statement not in text]
    assert not missing, f"SPEC §0 少了权威声明：{missing}"


def test_spec_diagnostic_codes_exist_in_source():
    """SPEC 里引用的每个诊断码都必须在 src/ 里存在（复用总守卫的实现）。"""
    assert not _problems_text(check_diagnostic_codes(ROOT))


def test_spec_symbol_anchors_exist():
    """SPEC 里每个 `文件.py::符号` 都必须文件存在且符号真的定义。"""
    assert not _problems_text(check_symbol_refs(ROOT))


def test_spec_has_no_line_number_references():
    """SPEC 只绑符号，不绑行号：`文件.py:行号` 一律不许出现。"""
    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(_spec_text().splitlines(), 1)
        if re.search(r"[A-Za-z0-9_./\-]+\.py:\d+", line)
    ]
    assert not offenders, "SPEC 里出现行号引用（改成 文件.py::符号）：\n" + "\n".join(offenders)


def _docs_markdown() -> list[str]:
    return sorted(path.relative_to(DOCS).as_posix() for path in DOCS.rglob("*.md"))


def test_docs_index_lists_every_document():
    """docs/README.md 是文档总索引：每个 docs/**/*.md 都要在里面出现。"""
    index = (DOCS / "README.md").read_text(encoding="utf-8")
    unlisted = [relative for relative in _docs_markdown() if relative not in index]
    assert not unlisted, (
        "docs/README.md 没有列出这些文档（新增文档时同步索引）：\n" + "\n".join(unlisted))


@pytest.mark.parametrize(
    "removed",
    [
        "docs/INVARIANTS.md",
        "docs/data/PROPERTY_STATE_CONTRACT.md",
        "docs/data/PROPERTY_CATEGORIES.md",
    ],
)
def test_moved_sources_are_gone(removed):
    """搬运完成后原文件必须删除，避免两套真源同时存在。"""
    assert not (ROOT / removed).exists(), f"{removed} 已并入 docs/extraction/SPEC.md，不应再存在"
