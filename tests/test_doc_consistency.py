"""Documentation drift guards: retired phrasings, ID closure, names instead of positions.

Text only: reads ``docs/``, ``AGENTS.md`` and ``src/``; builds no project, touches
no database, writes nothing — safe in the default parallel suite.

What is checked:

* retired phrasings — wording that the current 五层 semantics replaced must not
  come back into the docs or the prompt defaults;
* ID closure — ``I-xx`` in ``INVARIANTS.md`` must point at a real ``M-xx``; every
  diagnostic code quoted in a doc must still exist in ``src/``; every glossary
  term must appear in ``MODEL.md`` or name a schema field that exists;
* link liveness — relative links between ``docs/*.md`` (and into ``docs/`` from
  ``AGENTS.md``/``README.md``) must resolve;
* literal doc paths — ``docs/....md`` written into ``src/``/``tests/``/``scripts/``
  must exist, so moving a document cannot silently break code that reads it;
* no line numbers — docs reference code as ``文件.py::符号``, never ``文件.py:行号``;
* symbol existence — every ``文件.py::符号`` in ``INVARIANTS.md`` must be a real
  ``def``/``class`` in that file.

文档绑「合同」（诊断码、工具名、提示词 key、符号名），不绑「位置」（行号、深层路径）。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SOURCE = ROOT / "src"
INVARIANTS = DOCS / "INVARIANTS.md"
GLOSSARY = DOCS / "GLOSSARY.md"
MODEL = DOCS / "MODEL.md"
PROMPT_DEFAULTS = SOURCE / "narrative" / "prompt_defaults.py"
PROSE_FILES = [ROOT / "AGENTS.md", ROOT / "README.md", PROMPT_DEFAULTS]

# 旧口径：v23 五层语义之后不再成立的说法，禁止在文档与提示词默认值里复现。
RETIRED = {
    "主要故事仅在模型确认完整大篇章真正结束后总结创建": "T 是范围，可在进行中创建并持续修订，不等该部完结",
    "场景是连续时空或叙事语境的容器": "场景是空间接近、时间连续的事件归纳，地点只记在事件上（剧本场次另说）",
}

# 「场景是连续时空或叙事语境的容器」这类表述在讲剧本场次时仍然成立：白名单只放讲场次的上下文。
SCENE_POLICY_MARKERS = ("场次", "剧本", "script_unit")

MODEL_ID = re.compile(r"\bM-\d{2}\b")
INVARIANT_ID = re.compile(r"\bI-\d{2}\b")
CODE_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _doc_files() -> list[Path]:
    return sorted(DOCS.glob("*.md"))


def _scan_files() -> list[Path]:
    return [path for path in [*PROSE_FILES, *_doc_files()] if path.exists()]


@lru_cache(maxsize=1)
def _source_text() -> str:
    return "\n".join(_read(path) for path in sorted(SOURCE.rglob("*.py")))


def _markdown_lines(path: Path) -> list[str]:
    return _read(path).splitlines()


def _heading_chain(lines: list[str], index: int) -> str:
    headings = []
    for line in lines[: index + 1]:
        if line.startswith("#"):
            headings.append(line)
        elif not line.strip():
            headings.clear()
    return " ".join(headings)


def _scene_policy_context(path: Path, lines: list[str], index: int) -> bool:
    """讲「场次」的段落里可以出现「场景是连续时空或叙事语境的容器」式的对比句。"""
    if path == PROMPT_DEFAULTS:
        return True
    window = " ".join(lines[max(0, index - 2) : index + 3])
    if any(marker in window for marker in SCENE_POLICY_MARKERS):
        return True
    return any(marker in _heading_chain(lines, index) for marker in SCENE_POLICY_MARKERS)


def test_no_retired_phrasings():
    """旧口径长句不得在文档或提示词默认值里复现。"""
    hits = []
    for path in _scan_files():
        lines = _markdown_lines(path)
        for index, line in enumerate(lines):
            for phrase, replacement in RETIRED.items():
                if phrase not in line:
                    continue
                if "场景是连续时空" in phrase and _scene_policy_context(path, lines, index):
                    continue
                hits.append(f"{path.relative_to(ROOT)}:{index + 1} 仍是旧口径「{phrase}」，现行写法：{replacement}")
    assert not hits, "停用句式复现：\n" + "\n".join(hits)


def _invariant_rows() -> list[list[str]]:
    lines = _markdown_lines(INVARIANTS)
    rows = []
    for line in lines:
        if not line.startswith("| I-"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells)
    return rows


def test_invariant_table_is_well_formed():
    """每条不变量都要有唯一 ID 和五列（ID / 内容 / 校验点 / 诊断码 / 覆盖测试）。"""
    rows = _invariant_rows()
    assert rows, "docs/INVARIANTS.md 里没有 I-xx 行"
    identifiers = [row[0] for row in rows]
    assert len(identifiers) == len(set(identifiers)), f"不变量 ID 重复：{identifiers}"
    bad = [row[0] for row in rows if len(row) != 5]
    assert not bad, f"这些行的列数不是 5：{bad}"


def _model_ids() -> tuple[set[str], Path]:
    """M-xx 的权威来源；MODEL.md 未定稿前退到 GLOSSARY.md 的占位编号表。"""
    authority = MODEL if MODEL.exists() else GLOSSARY
    return set(MODEL_ID.findall(_read(authority))), authority


def test_invariant_model_refs_exist():
    """INVARIANTS.md 引用的 M-xx 必须在 MODEL.md（暂为 GLOSSARY 占位表）里存在。"""
    identifiers, authority = _model_ids()
    assert identifiers, f"{authority.relative_to(ROOT)} 里没有任何 M-xx 编号"
    missing = sorted(
        {code for row in _invariant_rows() for code in MODEL_ID.findall("|".join(row))} - identifiers
    )
    assert not missing, f"INVARIANTS.md 引用了 {authority.relative_to(ROOT)} 里不存在的编号：{missing}"


def test_glossary_refs_exist():
    """GLOSSARY.md 的 M-xx 编号同样必须可解析。"""
    identifiers, authority = _model_ids()
    missing = sorted(set(MODEL_ID.findall(_read(GLOSSARY))) - identifiers)
    assert not missing, f"GLOSSARY.md 引用了 {authority.relative_to(ROOT)} 里不存在的编号：{missing}"


def _glossary_rows() -> list[tuple[str, str]]:
    """术语表的（词条概念名, 该行原文）。"""
    rows = []
    for line in _read(GLOSSARY).splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in {"概念", "词条", "占位", "---"}:
            continue
        if cells[0].startswith("G-"):
            continue  # 编号对照表只列编号与概念，不重复要求
        rows.append((cells[0], line))
    return rows


def _term_variants(term: str) -> list[str]:
    """「属性（定义）」「判重：容器属性让位」这类词条名要按可检索的前缀比对。"""
    variants = [term]
    if "（" in term:
        variants.append(term.split("（", 1)[0])
    if "：" in term:
        variants.extend(part for part in term.split("：") if part)
    if len(term) >= 3:
        variants.append(term[:2])  # 词条名是归纳式命名（提取层、归纳派生），正文只讲名干
    return [variant.strip() for variant in variants if variant.strip()]


def test_glossary_terms_appear_in_model():
    """术语表的词条必须能落到 MODEL.md，或落到它自己标注的 schema 字段（且该字段在 src/ 里存在）。"""
    model_text = _read(MODEL) if MODEL.exists() else ""
    source = _source_text()
    missing = []
    for term, line in _glossary_rows():
        if model_text and any(variant in model_text for variant in _term_variants(term)):
            continue
        identifiers = [token for token in re.findall(r"`([A-Za-z_][A-Za-z0-9_.]*)`", line)
                       if "_" in token or "." in token]
        if any(identifier.split(".")[-1] in source for identifier in identifiers):
            continue
        missing.append(term)
    assert not missing, f"GLOSSARY.md 的词条既不在 MODEL.md 出现，也没标注可核对的 schema 字段：{missing}"


def _quoted_codes(path: Path) -> set[str]:
    codes = set()
    for token in re.findall(r"`([A-Za-z][A-Za-z0-9_]*)`", _read(path)):
        if CODE_TOKEN.match(token):
            codes.add(token)
    return codes


def test_diagnostic_codes_exist_in_source():
    """文档里引用的诊断码必须能在 src/ 里搜到，防止文档跟着代码一起漂。"""
    codes = _quoted_codes(INVARIANTS)
    if MODEL.exists():
        codes |= _quoted_codes(MODEL)
    assert len(codes) >= 15, f"只提取到 {len(codes)} 个诊断码，正则或表格格式可能被改坏了：{sorted(codes)}"
    source = _source_text()
    missing = sorted(code for code in codes if not re.search(rf"\b{code}\b", source))
    assert not missing, f"这些诊断码在 src/ 里搜不到：{missing}"


def _relative_doc_links(path: Path) -> list[tuple[str, Path]]:
    links = []
    for target in LINK.findall(_read(path)):
        cleaned = target.split("#")[0].strip()
        if not cleaned or cleaned.startswith(("http://", "https://", "mailto:", "#")):
            continue
        resolved = (path.parent / cleaned).resolve()
        if DOCS.resolve() not in resolved.parents and resolved != DOCS.resolve():
            continue  # 指向 data/、artifacts/ 等运行产物，不属于本次范围
        links.append((cleaned, resolved))
    return links


def test_doc_relative_links_alive():
    """docs/ 内部的相对链接必须指向真实文件。"""
    broken = []
    for path in [*_doc_files(), *(doc for doc in (ROOT / "AGENTS.md", ROOT / "README.md") if doc.exists())]:
        for target, resolved in _relative_doc_links(path):
            if not resolved.exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
    assert not broken, "文档相对链接断了：\n" + "\n".join(broken)


# 代码里写死的文档路径（open/read_text/PROJECT_ROOT/"docs/..."）不会被上面的链接检查看到：
# 文档搬家后这些字面量会静默变成 FileNotFoundError。这里把它们也纳入检查。
LITERAL_DOC = re.compile(r"docs/[A-Za-z0-9_\-/]+\.md")
LITERAL_ROOTS = ("src", "tests", "scripts")

# 确实要引用旧路径（例如在注释里说明历史归档位置）的登记在这里，注释写原因。
ALLOWED_STALE_LITERALS: dict[str, str] = {
    # tests/test_prune_artifacts.py 在 tmp_path 下构造的合成目录，不是仓库里的真实路径。
    "docs/archive/old/note.md": "prune 用例的合成受保护目录（tmp_path 下），非仓库路径",
}


def _literal_doc_paths(root: Path, roots: tuple[str, ...] = LITERAL_ROOTS) -> list[tuple[Path, int, str]]:
    """(文件, 行号, 字面量路径) —— 按代码里出现的顺序，构造临时路径的行不算。"""
    found = []
    for name in roots:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if path.resolve() == Path(__file__).resolve():
                continue  # 本文件的字面量是扫描用例自己的夹具
            for number, line in enumerate(_read(path).splitlines(), 1):
                if "tmp_path" in line:
                    continue  # 用例里用 tmp_path 拼出来的假路径，不是仓库路径
                for match in LITERAL_DOC.finditer(line):
                    found.append((path, number, match.group(0)))
    return found


def _doc_candidates(relative: str, root: Path) -> list[str]:
    """同名文档在 docs/ 里的真实位置，用于给出搬家后的候选路径。"""
    name = relative.rsplit("/", 1)[-1]
    return sorted(
        path.relative_to(root).as_posix()
        for path in (root / "docs").rglob(name)
        if path.is_file()
    )


def _stale_literal_problems(root: Path, roots: tuple[str, ...] = LITERAL_ROOTS) -> list[str]:
    problems = []
    for path, number, relative in _literal_doc_paths(root, roots):
        if relative in ALLOWED_STALE_LITERALS:
            continue
        if (root / relative).is_file():
            continue
        candidates = _doc_candidates(relative, root)
        if len(candidates) == 1:
            hint = f"搬到 {candidates[0]}？"
        elif candidates:
            hint = "候选：" + "、".join(candidates[:5])
        else:
            hint = "docs/ 里没有同名文件，可能确实已删除"
        problems.append(f"{path.relative_to(root)}:{number} 引用 {relative} 但该文件不存在（{hint}）")
    return problems


def test_literal_doc_paths_in_code_exist():
    """src/、tests/、scripts/ 里写死的 docs/...md 路径必须存在。"""
    problems = _stale_literal_problems(ROOT)
    assert not problems, "代码里的文档路径失效：\n" + "\n".join(problems)


def test_literal_doc_scanner_flags_missing_path(tmp_path):
    """注入一条失效路径时必须被捕获，并给出同名的真实候选。"""
    (tmp_path / "docs" / "features").mkdir(parents=True)
    (tmp_path / "docs" / "REAL.md").write_text("# 真文档\n", encoding="utf-8")
    (tmp_path / "docs" / "features" / "FEATURES.md").write_text("# 搬过家\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "broken.py").write_text(
        'from pathlib import Path\n\nPath("docs/REAL.md").read_text(encoding="utf-8")\n'
        'Path("docs/GONE.md").read_text(encoding="utf-8")\n',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "moved.py").write_text(
        'Path("docs/FEATURES.md").read_text(encoding="utf-8")\n', encoding="utf-8"
    )

    problems = _stale_literal_problems(tmp_path)

    assert len(problems) == 2, problems
    assert "broken.py:4 引用 docs/GONE.md" in problems[0]
    assert "docs/ 里没有同名文件" in problems[0]
    assert "moved.py:1 引用 docs/FEATURES.md" in problems[1]
    assert "docs/features/FEATURES.md" in problems[1]


def test_literal_doc_scanner_accepts_live_path(tmp_path):
    """路径存在时通过；tmp_path 拼出来的假路径不算仓库路径。"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "MODEL.md").write_text("# 模型\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ok.py").write_text(
        'Path("docs/MODEL.md").read_text(encoding="utf-8")\n'
        '(tmp_path / "docs/not-there.md").write_text("x")\n',
        encoding="utf-8",
    )

    assert _literal_doc_paths(tmp_path) == [(tmp_path / "scripts" / "ok.py", 1, "docs/MODEL.md")]
    assert _stale_literal_problems(tmp_path) == []


# 文档只绑「合同」（码、工具名、提示词 key、符号名），不绑「位置」：行号一改代码就过期。
LINE_REF = re.compile(r"\.py:\d+")
# 引用代码一律写成 文件::符号，符号名由下面的存在性检查兜住。
SYMBOL_REF = re.compile(r"([A-Za-z0-9_./\-]+\.py)::([A-Za-z_][A-Za-z0-9_]*)")


def _line_number_refs(root: Path) -> list[tuple[Path, int, str]]:
    found = []
    for path in sorted((root / "docs").rglob("*.md")):
        for number, line in enumerate(_read(path).splitlines(), 1):
            match = LINE_REF.search(line)
            if match:
                found.append((path, number, match.group(0)))
    return found


def _symbol_refs(path: Path) -> list[tuple[str, str]]:
    return sorted(set(SYMBOL_REF.findall(_read(path))))


def _stale_symbol_problems(root: Path, invariants: Path) -> list[str]:
    problems = []
    for relative, symbol in _symbol_refs(invariants):
        target = root / relative
        if not target.is_file():
            problems.append(f"{relative}::{symbol} 已失效：文件不存在；若函数已改名或搬走，请同步 INVARIANTS")
            continue
        body = _read(target)
        if not re.search(rf"^\s*(def|class)\s+{re.escape(symbol)}\b", body, re.M):
            problems.append(f"{relative}::{symbol} 已失效：文件里找不到该函数或类；"
                            f"若函数已改名，请同步 INVARIANTS")
    return problems


def test_docs_avoid_line_number_references():
    """docs/**/*.md 里不得出现 `文件.py:行号`，改写成 `文件.py::符号`。"""
    hits = _line_number_refs(ROOT)
    detail = "\n".join(f"{path.relative_to(ROOT)}:{number} → {text}"
                       f"（建议改成 文件::符号）" for path, number, text in hits)
    assert not hits, "文档里出现了行号引用：\n" + detail


def test_line_number_guard_flags_injected_reference(tmp_path):
    """注入一行 `src/a.py:42` 时必须被捕获，并提示改成 文件::符号。"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "NOTE.md").write_text(
        "见 src/a.py:42 的校验。\n另见 `docs/x.py:7`。\n", encoding="utf-8")

    hits = _line_number_refs(tmp_path)

    assert [(number, text) for _, number, text in hits] == [(1, ".py:42"), (2, ".py:7")]
    assert "改成 文件::符号" in "（建议改成 文件::符号）"


def test_invariant_symbol_refs_exist():
    """INVARIANTS.md 里的 `文件.py::符号` 必须文件存在且符号真实定义。"""
    problems = _stale_symbol_problems(ROOT, INVARIANTS)
    assert not problems, "INVARIANTS.md 的符号引用失效：\n" + "\n".join(problems)


def test_symbol_guard_flags_injected_reference(tmp_path):
    """注入「符号已改名」与「文件不存在」两种失效引用，都必须被捕获。"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "narrative").mkdir(parents=True)
    (tmp_path / "src" / "narrative" / "guard.py").write_text(
        "def keep_me():\n    pass\n\n\nclass Still_Here:\n    pass\n", encoding="utf-8")
    (tmp_path / "docs" / "INVARIANTS.md").write_text(
        "| I-01 | a | `src/narrative/guard.py::keep_me` | `CODE` | t |\n"
        "| I-02 | b | `src/narrative/guard.py::Still_Here` | `CODE` | t |\n"
        "| I-03 | c | `src/narrative/guard.py::gone_away` | `CODE` | t |\n"
        "| I-04 | d | `src/narrative/missing.py::keep_me` | `CODE` | t |\n",
        encoding="utf-8")

    problems = _stale_symbol_problems(tmp_path, tmp_path / "docs" / "INVARIANTS.md")

    assert len(problems) == 2, problems
    assert "guard.py::gone_away 已失效：文件里找不到该函数或类" in problems[0]
    assert "missing.py::keep_me 已失效：文件不存在" in problems[1]
