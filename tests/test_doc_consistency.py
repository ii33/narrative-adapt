"""Documentation drift guards: retired phrasings, ID closure, names instead of positions.

Text only: reads ``docs/``, ``AGENTS.md`` and ``src/``; builds no project, touches no
database, writes nothing — safe in the default parallel suite.

Every finding is a ``Problem`` carrying ``路径:行号``（相对仓库根）, so editors and agent
tooling can jump straight to the line, e.g.
``docs/INVARIANTS.md:42 引用 src/a.py::gone 已失效：文件里找不到该函数或类``。

What is checked:

* retired phrasings — wording that the current 五层 semantics replaced must not come
  back into the docs or the prompt defaults;
* ID closure — ``I-xx`` in ``INVARIANTS.md`` must point at a real ``M-xx``; every
  diagnostic code quoted in a doc must still exist in ``src/``; every glossary term
  must appear in ``MODEL.md`` or name a schema field that exists;
* link liveness — relative links between ``docs/*.md`` (and into ``docs/`` from
  ``AGENTS.md``/``README.md``) must resolve;
* literal doc paths — ``docs/....md`` written into ``src/``/``tests/``/``scripts/``
  must exist, so moving a document cannot silently break code that reads it;
* no line numbers — docs reference code as ``文件.py::符号``, never ``文件.py:行号``;
* symbol existence — every ``文件.py::符号`` in ``INVARIANTS.md`` must be a real
  ``def``/``class`` in that file.

文档绑「合同」（诊断码、工具名、提示词 key、符号名），不绑「位置」（行号、深层路径）。

Standalone report::

    python tests/test_doc_consistency.py             # 文本：按文件分组 + 汇总行
    python tests/test_doc_consistency.py --json      # 结构化，供 agent 读取
    python tests/test_doc_consistency.py --no-hyperlink   # 强制纯文本
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
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
LITERAL_ROOTS = ("src", "tests", "scripts")

# 旧口径：v23 五层语义之后不再成立的说法，禁止在文档与提示词默认值里复现。
RETIRED = {
    "主要故事仅在模型确认完整大篇章真正结束后总结创建": "T 是范围，可在进行中创建并持续修订，不等该部完结",
    "场景是连续时空或叙事语境的容器": "场景是空间接近、时间连续的事件归纳，地点只记在事件上（剧本场次另说）",
}

# 「场景是连续时空或叙事语境的容器」这类表述在讲剧本场次时仍然成立：白名单只放讲场次的上下文。
SCENE_POLICY_MARKERS = ("场次", "剧本", "script_unit")

MODEL_ID = re.compile(r"\bM-\d{2}\b")
CODE_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# 代码里写死的文档路径：文档搬家后这些字面量会静默变成 FileNotFoundError。
LITERAL_DOC = re.compile(r"docs/[A-Za-z0-9_\-/]+\.md")
# 文档只绑「合同」，不绑「位置」：行号一改代码就过期。
LINE_REF = re.compile(r"([A-Za-z0-9_./\-]+\.py:\d+)")
# 引用代码一律写成 文件::符号，符号名由存在性检查兜住。
SYMBOL_REF = re.compile(r"([A-Za-z0-9_./\-]+\.py)::([A-Za-z_][A-Za-z0-9_]*)")

# 确实要引用旧路径（例如在注释里说明历史归档位置）的登记在这里，注释写原因。
ALLOWED_STALE_LITERALS: dict[str, str] = {
    # tests/test_prune_artifacts.py 在 tmp_path 下构造的合成目录，不是仓库里的真实路径。
    "docs/archive/old/note.md": "prune 用例的合成受保护目录（tmp_path 下），非仓库路径",
}


# --- 问题与报告 -------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    """一条问题。``file`` 是相对仓库根的正斜杠路径；``line`` 可缺省。"""

    check: str
    message: str
    file: str
    line: int | None = None
    severity: str = "error"
    fix: str = ""
    candidates: Sequence[str] = field(default_factory=tuple)

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file

    def as_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "fix": self.fix,
            "candidates": list(self.candidates),
        }


def terminal_supports_hyperlink(stream=None) -> bool:
    """OSC 8 只在真终端里有意义；旧 conhost 会原样打印控制序列，必须降级。"""
    if os.environ.get("DOC_CHECK_NO_HYPERLINK") or os.environ.get("TERM") == "dumb":
        return False
    stream = stream if stream is not None else sys.stdout
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.name == "nt":
        # Windows Terminal / VS Code / mintty / ConEmu 会设置其中之一；裸 conhost 不会。
        return any(os.environ.get(name) for name in
                   ("WT_SESSION", "TERM_PROGRAM", "ANSICON", "ConEmuANSI", "TERM"))
    return True


def hyperlink(text: str, path: Path, line: int | None) -> str:
    """OSC 8 可点击链接，指向文件绝对路径（带行号锚点）。"""
    uri = path.resolve().as_posix()
    if line:
        uri = f"{uri}#L{line}"
    return f"\x1b]8;;file:///{uri}\x1b\\{text}\x1b]8;;\x1b\\"


def summary(problems: Sequence[Problem]) -> dict[str, int]:
    return {
        "total": len(problems),
        "error": sum(1 for problem in problems if problem.severity == "error"),
        "warning": sum(1 for problem in problems if problem.severity == "warning"),
        "files": len({problem.file for problem in problems}),
    }


def summary_line(problems: Sequence[Problem]) -> str:
    counts = summary(problems)
    return (f"共 {counts['total']} 个问题（error {counts['error']} / warning {counts['warning']}），"
            f"涉及 {counts['files']} 个文件")


def render_text(problems: Sequence[Problem], *, root: Path | None = None,
                hyperlink_enabled: bool = False) -> str:
    """按文件分组列出，每行以 ``路径:行号`` 开头，末尾给汇总行。"""
    problems = list(problems)
    if not problems:
        return "没有发现问题。\n"
    groups: dict[str, list[Problem]] = {}
    for problem in problems:
        groups.setdefault(problem.file, []).append(problem)
    lines = ["按文件分组："]
    for name, rows in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        lines.append(f"  {name}（{len(rows)} 个）")
        for problem in rows:
            label = problem.location
            if hyperlink_enabled:
                label = hyperlink(label, (root or ROOT) / problem.file, problem.line)
            lines.append(f"    {label} {problem.message}")
            if problem.fix:
                lines.append(f"      建议：{problem.fix}")
            if problem.candidates:
                lines.append("      候选：" + "、".join(list(problem.candidates)[:5]))
    lines += ["", summary_line(problems)]
    return "\n".join(lines) + "\n"


def render_json(problems: Sequence[Problem]) -> str:
    """字段固定为 check / severity / file / line / message / fix / candidates。"""
    problems = list(problems)
    payload = {"summary": summary(problems), "problems": [problem.as_dict() for problem in problems]}
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


# --- 文件与文本读取 ---------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _docs(root: Path) -> Path:
    return root / "docs"


def _invariants(root: Path) -> Path:
    return _docs(root) / "INVARIANTS.md"


def _glossary(root: Path) -> Path:
    return _docs(root) / "GLOSSARY.md"


def _model(root: Path) -> Path:
    return _docs(root) / "MODEL.md"


def _prompt_defaults(root: Path) -> Path:
    return root / "src" / "narrative" / "prompt_defaults.py"


def _doc_files(root: Path) -> list[Path]:
    return sorted(_docs(root).glob("*.md"))


def _prose_files(root: Path) -> list[Path]:
    candidates = [root / "AGENTS.md", root / "README.md", _prompt_defaults(root)]
    return [path for path in candidates if path.exists()]


@lru_cache(maxsize=8)
def _source_text(root: Path) -> str:
    return "\n".join(_read(path) for path in sorted((root / "src").rglob("*.py")))


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
    if path.name == "prompt_defaults.py":
        return True
    window = " ".join(lines[max(0, index - 2) : index + 3])
    if any(marker in window for marker in SCENE_POLICY_MARKERS):
        return True
    return any(marker in _heading_chain(lines, index) for marker in SCENE_POLICY_MARKERS)


# --- 各项检查 ---------------------------------------------------------------------


def check_retired_phrasings(root: Path = ROOT) -> list[Problem]:
    """旧口径长句不得在文档或提示词默认值里复现。"""
    problems = []
    for path in [*_prose_files(root), *_doc_files(root)]:
        lines = _read(path).splitlines()
        for index, line in enumerate(lines):
            for phrase, replacement in RETIRED.items():
                if phrase not in line:
                    continue
                if "场景是连续时空" in phrase and _scene_policy_context(path, lines, index):
                    continue
                problems.append(Problem(
                    check="retired_phrasing", file=_rel(path, root), line=index + 1,
                    message=f"仍是旧口径「{phrase}」",
                    fix=f"改成现行写法：{replacement}",
                ))
    return problems


def _invariant_rows(root: Path) -> list[tuple[int, list[str]]]:
    rows = []
    for number, line in enumerate(_read(_invariants(root)).splitlines(), 1):
        if not line.startswith("| I-"):
            continue
        rows.append((number, [cell.strip() for cell in line.strip().strip("|").split("|")]))
    return rows


def check_invariant_table(root: Path = ROOT) -> list[Problem]:
    """每条不变量都要有唯一 ID 和五列（ID / 内容 / 校验点 / 诊断码 / 覆盖测试）。"""
    rows = _invariant_rows(root)
    file = _rel(_invariants(root), root)
    if not rows:
        return [Problem(check="invariant_table", file=file,
                        message="INVARIANTS.md 里没有 I-xx 行",
                        fix="按 | ID | 不变量 | 校验点 | 诊断码 | 覆盖测试 | 建表")]
    problems = []
    seen: dict[str, int] = {}
    for number, cells in rows:
        identifier = cells[0]
        if identifier in seen:
            problems.append(Problem(check="invariant_table", file=file, line=number,
                                    message=f"不变量 ID {identifier} 与第 {seen[identifier]} 行重复",
                                    fix="改成新的 I-xx 编号"))
        seen.setdefault(identifier, number)
        if len(cells) != 5:
            problems.append(Problem(check="invariant_table", file=file, line=number,
                                    message=f"{identifier} 有 {len(cells)} 列，应为 5 列",
                                    fix="补齐 ID / 不变量 / 校验点 / 诊断码 / 覆盖测试"))
    return problems


def _model_ids(root: Path) -> tuple[set[str], Path]:
    """M-xx 的权威来源；MODEL.md 尚未落地时退到 GLOSSARY.md。"""
    authority = _model(root) if _model(root).exists() else _glossary(root)
    return set(MODEL_ID.findall(_read(authority))), authority


def check_invariant_model_refs(root: Path = ROOT) -> list[Problem]:
    """INVARIANTS.md 引用的 M-xx 必须在 MODEL.md 里存在。"""
    identifiers, authority = _model_ids(root)
    file = _rel(_invariants(root), root)
    if not identifiers:
        return [Problem(check="invariant_model_refs", file=_rel(authority, root),
                        message="权威文档里没有任何 M-xx 编号",
                        fix="在 MODEL.md 第 2 节补上带 M-xx 编号的规则")]
    problems = []
    for number, line in enumerate(_read(_invariants(root)).splitlines(), 1):
        for code in sorted(set(MODEL_ID.findall(line)) - identifiers):
            problems.append(Problem(check="invariant_model_refs", file=file, line=number,
                                    message=f"引用了 {_rel(authority, root)} 里不存在的编号 {code}",
                                    fix="改成 MODEL.md 里现有的 M-xx"))
    return problems


def check_glossary_model_refs(root: Path = ROOT) -> list[Problem]:
    """GLOSSARY.md 的 M-xx 编号同样必须可解析。"""
    identifiers, authority = _model_ids(root)
    problems = []
    for number, line in enumerate(_read(_glossary(root)).splitlines(), 1):
        for code in sorted(set(MODEL_ID.findall(line)) - identifiers):
            problems.append(Problem(check="glossary_model_refs", file=_rel(_glossary(root), root),
                                    line=number,
                                    message=f"引用了 {_rel(authority, root)} 里不存在的编号 {code}",
                                    fix="改成 MODEL.md 里现有的 M-xx"))
    return problems


def _glossary_rows(root: Path) -> list[tuple[int, str, str]]:
    """(行号, 词条概念名, 该行原文)。"""
    rows = []
    for number, line in enumerate(_read(_glossary(root)).splitlines(), 1):
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in {"概念", "词条", "占位", "---"}:
            continue
        if cells[0].startswith("G-"):
            continue  # 编号对照表只列编号与概念，不重复要求
        rows.append((number, cells[0], line))
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


def check_glossary_terms(root: Path = ROOT) -> list[Problem]:
    """术语词条必须落到 MODEL.md，或落到它自己标注的 schema 字段（且字段在 src/ 里存在）。"""
    model_text = _read(_model(root)) if _model(root).exists() else ""
    source = _source_text(root)
    problems = []
    for number, term, line in _glossary_rows(root):
        if model_text and any(variant in model_text for variant in _term_variants(term)):
            continue
        # 词条可以靠「它自己标注的 schema 字段」落地：字段名在 src/ 里搜得到就算有据。
        identifiers = [token for token in re.findall(r"`([^`]+)`", line)
                       if re.fullmatch(r"[a-z][a-z0-9_.]*", token)]
        fields = {token for token in identifiers} | {token.split(".")[-1] for token in identifiers}
        if any(re.search(rf"\b{re.escape(field)}\b", source) for field in fields):
            continue
        problems.append(Problem(check="glossary_terms", file=_rel(_glossary(root), root), line=number,
                                message=f"词条「{term}」既不在 MODEL.md 出现，也没标注可核对的 schema 字段",
                                fix="在 MODEL.md 定义该概念，或在该行写出对应的代码字段名"))
    return problems


def _quoted_codes(path: Path) -> list[tuple[int, str]]:
    codes = []
    for number, line in enumerate(_read(path).splitlines(), 1):
        for token in re.findall(r"`([A-Za-z][A-Za-z0-9_]*)`", line):
            if CODE_TOKEN.match(token):
                codes.append((number, token))
    return codes


def check_diagnostic_codes(root: Path = ROOT) -> list[Problem]:
    """文档里引用的诊断码必须能在 src/ 里搜到，防止文档跟着代码一起漂。"""
    source = _source_text(root)
    problems = []
    total = 0
    for path in (path for path in (_invariants(root), _model(root)) if path.exists()):
        for number, code in _quoted_codes(path):
            total += 1
            if re.search(rf"\b{code}\b", source):
                continue
            problems.append(Problem(check="diagnostic_codes", file=_rel(path, root), line=number,
                                    message=f"诊断码 {code} 在 src/ 里搜不到",
                                    fix="改成代码里实际抛出的码，或从文档删除"))
    if total < 15:
        problems.append(Problem(check="diagnostic_codes", file=_rel(_invariants(root), root),
                                message=f"只提取到 {total} 个诊断码，表格格式或正则可能被改坏了",
                                fix="检查 INVARIANTS.md 的诊断码列是否用反引号包住"))
    return problems


def _relative_doc_links(path: Path, docs: Path) -> list[tuple[int, str, Path]]:
    links = []
    for number, line in enumerate(_read(path).splitlines(), 1):
        for target in LINK.findall(line):
            cleaned = target.split("#")[0].strip()
            if not cleaned or cleaned.startswith(("http://", "https://", "mailto:", "#")):
                continue
            resolved = (path.parent / cleaned).resolve()
            if docs.resolve() not in resolved.parents and resolved != docs.resolve():
                continue  # 指向 data/、artifacts/ 等运行产物，不属于本次范围
            links.append((number, cleaned, resolved))
    return links


def check_doc_links(root: Path = ROOT) -> list[Problem]:
    """docs/ 内部的相对链接必须指向真实文件。"""
    docs = _docs(root)
    candidates = [*_doc_files(root),
                  *(path for path in (root / "AGENTS.md", root / "README.md") if path.exists())]
    problems = []
    for path in candidates:
        for number, target, resolved in _relative_doc_links(path, docs):
            if resolved.exists():
                continue
            problems.append(Problem(check="doc_links", file=_rel(path, root), line=number,
                                    message=f"链接目标不存在：{target}",
                                    fix="改成搬家后的相对路径（docs/ 内）",
                                    candidates=[resolved.relative_to(root).as_posix()]))
    return problems


def _literal_doc_paths(root: Path, roots: tuple[str, ...] = LITERAL_ROOTS) -> list[tuple[Path, int, str]]:
    """(文件, 行号, 字面量路径) —— 本文件自身的夹具与 tmp_path 拼出的假路径不算。"""
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
    return sorted(path.relative_to(root).as_posix()
                  for path in _docs(root).rglob(name) if path.is_file())


def check_literal_doc_paths(root: Path = ROOT) -> list[Problem]:
    """src/、tests/、scripts/ 里写死的 docs/...md 路径必须存在。"""
    problems = []
    for path, number, relative in _literal_doc_paths(root):
        if relative in ALLOWED_STALE_LITERALS:
            continue
        if (root / relative).is_file():
            continue
        candidates = _doc_candidates(relative, root)
        hint = (f"搬到 {candidates[0]}？" if len(candidates) == 1
                else "、".join(candidates[:5]) if candidates
                else "docs/ 里没有同名文件，可能确实已删除")
        problems.append(Problem(check="literal_doc_paths", file=_rel(path, root), line=number,
                                message=f"引用 {relative} 已失效：该文件不存在（{hint}）",
                                fix=f"改成 {candidates[0]}" if len(candidates) == 1
                                else "改成 docs/ 里的实际路径",
                                candidates=candidates))
    return problems


def _line_number_refs(root: Path) -> list[tuple[Path, int, str]]:
    found = []
    for path in sorted(_docs(root).rglob("*.md")):
        for number, line in enumerate(_read(path).splitlines(), 1):
            match = LINE_REF.search(line)
            if match:
                found.append((path, number, match.group(1)))
    return found


def check_line_number_refs(root: Path = ROOT) -> list[Problem]:
    """docs/**/*.md 里不得出现 `文件.py:行号`，改写成 `文件.py::符号`。"""
    return [Problem(check="line_number_refs", file=_rel(path, root), line=number,
                    message=f"文档里出现行号引用 {text}",
                    fix="改成 文件.py::符号（行号会随改动失效，符号名由本测试校验存在）")
            for path, number, text in _line_number_refs(root)]


def _symbol_refs(path: Path) -> list[tuple[int, str, str]]:
    """(行号, 文件, 符号)。"""
    rows = []
    for number, line in enumerate(_read(path).splitlines(), 1):
        for relative, symbol in SYMBOL_REF.findall(line):
            rows.append((number, relative, symbol))
    return rows


def check_symbol_refs(root: Path = ROOT) -> list[Problem]:
    """INVARIANTS.md 里的 `文件.py::符号` 必须文件存在且符号真实定义。"""
    file = _rel(_invariants(root), root)
    problems = []
    for number, relative, symbol in _symbol_refs(_invariants(root)):
        target = root / relative
        if not target.is_file():
            problems.append(Problem(check="symbol_refs", file=file, line=number,
                                    message=f"引用 {relative}::{symbol} 已失效：文件不存在",
                                    fix=f"把引用改成现有文件；{relative} 已搬走或改名"))
            continue
        if not re.search(rf"^\s*(def|class)\s+{re.escape(symbol)}\b", _read(target), re.M):
            problems.append(Problem(check="symbol_refs", file=file, line=number,
                                    message=f"引用 {relative}::{symbol} 已失效：文件里找不到该函数或类",
                                    fix=f"若函数已改名，请同步 INVARIANTS；{relative} 里的 def/class 才是现行符号"))
    return problems


CHECKS = (
    check_retired_phrasings,
    check_invariant_table,
    check_invariant_model_refs,
    check_glossary_model_refs,
    check_glossary_terms,
    check_diagnostic_codes,
    check_doc_links,
    check_literal_doc_paths,
    check_line_number_refs,
    check_symbol_refs,
)


def collect(root: Path = ROOT) -> list[Problem]:
    problems: list[Problem] = []
    for check in CHECKS:
        problems.extend(check(root))
    return problems


def _report(problems: list[Problem]) -> str:
    """空字符串代表通过；失败信息用纯文本渲染，不带超链接，断言因此稳定。"""
    return render_text(problems) if problems else ""


# --- pytest 用例 -------------------------------------------------------------------


def test_no_retired_phrasings():
    """旧口径长句不得在文档或提示词默认值里复现。"""
    assert not _report(check_retired_phrasings())


def test_invariant_table_is_well_formed():
    """每条不变量都要有唯一 ID 和五列（ID / 内容 / 校验点 / 诊断码 / 覆盖测试）。"""
    assert not _report(check_invariant_table())


def test_invariant_model_refs_exist():
    """INVARIANTS.md 引用的 M-xx 必须在 MODEL.md 里存在。"""
    assert not _report(check_invariant_model_refs())


def test_glossary_refs_exist():
    """GLOSSARY.md 的 M-xx 编号同样必须可解析。"""
    assert not _report(check_glossary_model_refs())


def test_glossary_terms_appear_in_model():
    """术语表的词条必须能落到 MODEL.md，或落到它自己标注的 schema 字段。"""
    assert not _report(check_glossary_terms())


def test_diagnostic_codes_exist_in_source():
    """文档里引用的诊断码必须能在 src/ 里搜到。"""
    assert not _report(check_diagnostic_codes())


def test_doc_relative_links_alive():
    """docs/ 内部的相对链接必须指向真实文件。"""
    assert not _report(check_doc_links())


def test_literal_doc_paths_in_code_exist():
    """src/、tests/、scripts/ 里写死的 docs/...md 路径必须存在。"""
    assert not _report(check_literal_doc_paths())


def test_docs_avoid_line_number_references():
    """docs/**/*.md 里不得出现 `文件.py:行号`，改写成 `文件.py::符号`。"""
    assert not _report(check_line_number_refs())


def test_invariant_symbol_refs_exist():
    """INVARIANTS.md 里的 `文件.py::符号` 必须文件存在且符号真实定义。"""
    assert not _report(check_symbol_refs())


# --- 注入失效的用例 ----------------------------------------------------------------


def test_line_number_guard_flags_injected_reference(tmp_path):
    """注入 `src/a.py:42` 时必须报出 文件:行号，并提示改成 文件::符号。"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "NOTE.md").write_text(
        "见 src/a.py:42 的校验。\n另见 `docs/x.py:7`。\n", encoding="utf-8")

    problems = check_line_number_refs(tmp_path)

    assert [problem.location for problem in problems] == ["docs/NOTE.md:1", "docs/NOTE.md:2"]
    assert "src/a.py:42" in problems[0].message
    assert "文件.py::符号" in problems[0].fix


def test_literal_doc_scanner_flags_missing_path(tmp_path):
    """注入一条失效路径时必须被捕获，并给出行号与说明。"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "REAL.md").write_text("# 真文档\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "broken.py").write_text(
        'from pathlib import Path\n\nPath("docs/REAL.md").read_text(encoding="utf-8")\n'
        'Path("docs/GONE.md").read_text(encoding="utf-8")\n',
        encoding="utf-8",
    )

    problems = check_literal_doc_paths(tmp_path)

    assert [problem.location for problem in problems] == ["src/broken.py:4"]
    assert "引用 docs/GONE.md 已失效" in problems[0].message
    assert "docs/ 里没有同名文件" in problems[0].message


def test_literal_doc_scanner_reports_moved_candidate(tmp_path):
    """文档搬家后给出唯一候选路径。"""
    (tmp_path / "docs" / "features").mkdir(parents=True)
    (tmp_path / "docs" / "features" / "API.md").write_text("# 接口\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "read.py").write_text(
        'Path("docs/API.md").read_text(encoding="utf-8")\n', encoding="utf-8")

    problems = check_literal_doc_paths(tmp_path)

    assert len(problems) == 1
    assert problems[0].candidates == ["docs/features/API.md"]
    assert "搬到 docs/features/API.md？" in problems[0].message


def test_literal_doc_scanner_respects_whitelist(tmp_path):
    """白名单里的历史路径不再报；tmp_path 拼出来的假路径从不扫描。"""
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "synthetic.py").write_text(
        'Path("docs/archive/old/note.md").read_text(encoding="utf-8")\n'
        '(tmp_path / "docs/not-there.md").write_text("x")\n',
        encoding="utf-8")

    assert check_literal_doc_paths(tmp_path) == []


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

    problems = check_symbol_refs(tmp_path)

    assert [problem.location for problem in problems] == [
        "docs/INVARIANTS.md:3", "docs/INVARIANTS.md:4"]
    assert "引用 src/narrative/guard.py::gone_away 已失效：文件里找不到该函数或类" == problems[0].message
    assert "引用 src/narrative/missing.py::keep_me 已失效：文件不存在" == problems[1].message


# --- 报告格式 ---------------------------------------------------------------------


def test_text_report_locates_every_problem(tmp_path):
    """文本报告以 路径:行号 开头、按文件分组，末尾给汇总行。"""
    problems = [
        Problem(check="symbol_refs", file="docs/INVARIANTS.md", line=42,
                message="引用 src/a.py::gone 已失效：文件里找不到该函数或类",
                fix="若函数已改名，请同步 INVARIANTS"),
        Problem(check="doc_links", file="docs/INVARIANTS.md", line=7,
                message="链接目标不存在：OLD.md", candidates=["docs/old/OLD.md"]),
        Problem(check="doc_links", file="README.md", line=3, message="链接目标不存在：docs/GONE.md"),
    ]

    text = render_text(problems, root=tmp_path)

    assert "docs/INVARIANTS.md:42 引用 src/a.py::gone 已失效：文件里找不到该函数或类" in text
    assert "docs/INVARIANTS.md（2 个）" in text
    assert "README.md（1 个）" in text
    assert "建议：若函数已改名，请同步 INVARIANTS" in text
    assert "候选：docs/old/OLD.md" in text
    assert text.rstrip().endswith(summary_line(problems))
    assert summary_line(problems) == "共 3 个问题（error 3 / warning 0），涉及 2 个文件"
    assert "\x1b]8;;" not in text  # 纯文本模式不带控制序列


def test_hyperlink_only_when_enabled(tmp_path):
    """超链接由开关控制；关闭时原样输出，开启时带 OSC 8 与行号锚点。"""
    problem = Problem(check="doc_links", file="docs/MODEL.md", line=12,
                      message="链接目标不存在：X")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "MODEL.md").write_text("# 模型\n", encoding="utf-8")

    plain = render_text([problem], root=tmp_path, hyperlink_enabled=False)
    linked = render_text([problem], root=tmp_path, hyperlink_enabled=True)

    assert "\x1b]8;;" not in plain
    assert "\x1b]8;;file:///" in linked
    assert "docs/MODEL.md:12" in linked
    assert "#L12" in linked


def test_json_report_has_fixed_fields():
    """--json 的字段固定，便于 agent 读取。"""
    problem = Problem(check="literal_doc_paths", file="tests/a.py", line=9,
                      message="引用 docs/GONE.md 已失效", fix="改成 docs/x/GONE.md",
                      candidates=["docs/x/GONE.md"])

    payload = json.loads(render_json([problem]))

    assert payload["summary"] == {"total": 1, "error": 1, "warning": 0, "files": 1}
    assert payload["problems"] == [{
        "check": "literal_doc_paths", "severity": "error", "file": "tests/a.py", "line": 9,
        "message": "引用 docs/GONE.md 已失效", "fix": "改成 docs/x/GONE.md",
        "candidates": ["docs/x/GONE.md"],
    }]


def test_terminal_supports_hyperlink_degrades(tmp_path):
    """非 TTY 与环境变量禁用时都不输出 OSC 8。"""
    class FakeStream:
        def __init__(self, tty: bool) -> None:
            self.tty = tty

        def isatty(self) -> bool:
            return self.tty

    assert terminal_supports_hyperlink(FakeStream(False)) is False
    os.environ["DOC_CHECK_NO_HYPERLINK"] = "1"
    try:
        assert terminal_supports_hyperlink(FakeStream(True)) is False
    finally:
        del os.environ["DOC_CHECK_NO_HYPERLINK"]


# --- 独立入口 ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="文档一致性检查（默认文本报告）")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    parser.add_argument("--hyperlink", action="store_true", help="强制输出 OSC 8 链接")
    parser.add_argument("--no-hyperlink", action="store_true", help="强制输出纯文本")
    args = parser.parse_args(argv)

    problems = collect()
    if args.json:
        sys.stdout.write(render_json(problems))
    else:
        enabled = not args.no_hyperlink and (args.hyperlink or terminal_supports_hyperlink())
        sys.stdout.write(render_text(problems, root=ROOT, hyperlink_enabled=enabled))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
