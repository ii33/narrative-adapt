"""Documentation drift guards: retired phrasings, ID closure, link liveness.

Text only: reads ``docs/``, ``AGENTS.md`` and ``src/``; builds no project, touches
no database, writes nothing — safe in the default parallel suite.

Three things are checked:

* retired phrasings — wording that the current 五层 semantics replaced must not
  come back into the docs or the prompt defaults;
* ID closure — ``I-xx`` in ``INVARIANTS.md`` must point at a real ``M-xx``; every
  diagnostic code quoted in a doc must still exist in ``src/``; every glossary
  term must appear in ``MODEL.md``;
* link liveness — relative links between ``docs/*.md`` (and into ``docs/`` from
  ``AGENTS.md``/``README.md``) must resolve.
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
