"""提取规范页的守卫：编号、锚点、索引与 §0 权威声明。

`docs/extraction/SPEC.md` 是提取任务的唯一规范页（P1 把 INVARIANTS、属性合同、属性分类、
MODEL 第 2、3 节搬了进来；P2 把提示词里可判定的硬约束规范化为 §3 的 `E-xx`、补了 §7 索引）。
这里只查“写进去之后会不会漂”的几件事：

1. 页面引用的诊断码必须真的在 ``src/`` 里抛得出来；
2. 页面里的 ``文件.py::符号`` 锚点必须真的存在（复用 ``test_doc_consistency`` 的解析器）；
3. ``docs/README.md`` 必须列出 ``docs/`` 下的每个 ``.md``——新增文档不能没人知道；
4. §0 的三条权威声明必须还在（有人删掉就等于取消单一真源）；
5. §7 的每个 ``id | version`` 必须等于 ``DEFAULT_PROMPTS`` 的运行时版本，且覆盖
   ``TEMPLATE_PROMPT_KEYS`` 每个模板用到的提示词；
6. §3 的 ``E-xx`` 编号唯一连续、四要素齐全、每条至少一个 ``I-xx``／``M-xx`` 交叉引用，
   且 ``出处`` 指向真实提示词条目（写了版本就必须等于运行时版本）；
7. §5 的「仅提示词约束」表只能是指针：每行都指到某个 ``E-xx``，不允许两处各写一份。

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

from narrative.chat_templates import TEMPLATE_PROMPT_KEYS
from narrative.prompt_defaults import DEFAULT_PROMPTS

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


# --- P2：§3 的 E-xx 与 §7 的提示词索引 ---------------------------------------------

PROMPT_ONLY_HEADING = "### 仅提示词约束（未程序强制，靠验收与复核）"
# 一条 E 条目的首行：`- \`E-01\` 规则句`。
E_ENTRY = re.compile(r"^- `E-(\d{2})` ")
ELEMENT_PREFIXES = ("- 出处：", "- 程序：", "- 反例：")
CROSS_REF = re.compile(r"\b([IM]-\d{2})\b")
BACKTICKED = re.compile(r"`([a-z][a-z0-9_]*)`")
TABLE_VERSION = re.compile(r"^\|\s*`([a-z][a-z0-9_]*)`\s*\|\s*v(\d+)\s*\|")


def _section(text: str, heading: str) -> list[str]:
    """取 ``heading`` 到下一个同级 ``## `` 标题之间的正文行。"""
    lines = text.splitlines()
    assert heading in lines, f"SPEC 缺少章节：{heading}"
    start = lines.index(heading)
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return body


def _e_items() -> list[tuple[str, list[str]]]:
    """(编号, 该条目的所有行)——编号取自 ``- \\`E-xx\\` `` 首行。"""
    items: list[tuple[str, list[str]]] = []
    for line in _section(_spec_text(), SECTION_HEADINGS[3]):
        match = E_ENTRY.match(line)
        if match:
            items.append((match.group(1), [line]))
        elif items:
            items[-1][1].append(line)
    return items


def _prompt_index() -> dict[str, int]:
    """§7 表里的 ``id -> version``。"""
    rows: dict[str, int] = {}
    for line in _section(_spec_text(), SECTION_HEADINGS[7]):
        match = TABLE_VERSION.match(line)
        if match:
            rows[match.group(1)] = int(match.group(2))
    return rows


def test_prompt_index_is_complete():
    """§7 必须列出 `DEFAULT_PROMPTS` 的每一条，不多不少：新增提示词不能没人知道。"""
    rows = _prompt_index()
    missing = sorted(set(DEFAULT_PROMPTS) - set(rows))
    unknown = sorted(set(rows) - set(DEFAULT_PROMPTS))
    assert not missing, f"§7 提示词索引缺这些条目：{missing}"
    assert not unknown, f"§7 索引了代码里不存在的提示词条目：{unknown}"


def test_prompt_index_versions_match_runtime():
    """§7 的版本号必须等于运行时 `DEFAULT_PROMPTS[id]["version"]`（只认代码，不凭印象）。"""
    rows = _prompt_index()
    stale = [
        f"{pid}: 文档 v{rows[pid]} / 代码 v{DEFAULT_PROMPTS[pid]['version']}"
        for pid in sorted(set(rows) & set(DEFAULT_PROMPTS))
        if str(rows[pid]) != str(DEFAULT_PROMPTS[pid]["version"])
    ]
    assert not stale, "§7 的版本号与代码不一致（改提示词要同批改索引）：\n" + "\n".join(stale)


def test_prompt_index_covers_every_template():
    """`TEMPLATE_PROMPT_KEYS` 用到的每一条提示词都必须在 §7 出现（否则模板拼了什么查不到）。"""
    rows = _prompt_index()
    missing = sorted(
        f"{template}.{key}"
        for template, keys in TEMPLATE_PROMPT_KEYS.items()
        for key in keys
        if key not in rows
    )
    assert not missing, "§7 没有覆盖这些模板用到的提示词：\n" + "\n".join(missing)


def test_e_items_are_numbered_and_unique():
    """§3 的 E-xx 编号唯一且从 E-01 连续：编号是引用锚点，跳号会让「引用一律编号」失效。"""
    numbers = [int(number) for number, _ in _e_items()]
    assert numbers, "§3 没有任何 `E-xx` 条目"
    assert len(set(numbers)) == len(numbers), f"§3 出现重复编号：{numbers}"
    assert numbers == list(range(1, len(numbers) + 1)), f"§3 编号不连续：{numbers}"


def test_e_items_have_four_elements_and_cross_reference():
    """每条 E-xx 都要有出处、程序、反例，且「程序」行至少一个 `I-xx`／`M-xx` 交叉引用。"""
    problems = []
    for number, lines in _e_items():
        body = [line.strip() for line in lines[1:]]
        missing = [prefix for prefix in ELEMENT_PREFIXES
                   if not any(line.startswith(prefix) for line in body)]
        if missing:
            problems.append(f"E-{number} 缺要素：{missing}")
            continue
        program = next(line for line in body if line.startswith("- 程序："))
        if not CROSS_REF.search(program):
            problems.append(f"E-{number} 的「程序」行没有 I-xx／M-xx 交叉引用：{program}")
    assert not problems, "\n".join(problems)


def test_e_item_sources_point_to_real_prompts():
    """`出处` 的条目 id 必须存在；写了版本就必须等于运行时版本（正文不许凭印象写版本）。"""
    problems = []
    for number, lines in _e_items():
        source = next((line.strip() for line in lines[1:]
                       if line.strip().startswith("- 出处：")), "")
        match = BACKTICKED.search(source)
        if not match:
            problems.append(f"E-{number} 的出处没有用反引号标出提示词 id：{source}")
            continue
        prompt_id = match.group(1)
        if prompt_id not in DEFAULT_PROMPTS:
            problems.append(f"E-{number} 的出处指向不存在的提示词条目：{prompt_id}")
            continue
        version = re.search(r"v(\d+)", source)
        if version and version.group(1) != str(DEFAULT_PROMPTS[prompt_id]["version"]):
            problems.append(
                f"E-{number} 的出处写 v{version.group(1)}，"
                f"但 DEFAULT_PROMPTS[{prompt_id!r}] 是 v{DEFAULT_PROMPTS[prompt_id]['version']}")
    assert not problems, "\n".join(problems)


def test_prompt_only_table_points_at_e_items():
    """§5「仅提示词约束」表只能是指针：每行都要指到某个 E-xx，条文只在 §3 写一份。"""
    rows = [
        line for line in _section(_spec_text(), PROMPT_ONLY_HEADING)
        if line.startswith("|")
    ]
    assert rows, "§5「仅提示词约束」一节不见了"
    body = [line for line in rows if not re.match(r"^\|\s*[-：: ]+\s*\|", line)
            and "原条目" not in line]
    assert body, "§5「仅提示词约束」表既没有数据行，也没有说明它们去哪了"
    unassigned = [line for line in body if not re.search(r"`E-\d{2}`", line)]
    assert not unassigned, (
        "§5「仅提示词约束」里这些行没有指到 E-xx（允许两处各写一份即失守）：\n"
        + "\n".join(unassigned))
