"""slow 标记守卫：防止新增的重用例漏标，把默认门禁拖回几分钟。

判据都是可判定的，不靠人记：

1. `slow` 标记必须注册（`pyproject.toml` 的 `markers`）；
2. 慢模块清单里的文件必须有模块级 `pytestmark = pytest.mark.slow`；
3. `test_storyboard_*.py` 全部算慢模块（真实图像/媒体 IO）；
4. 默认集里任何导入重库（子进程、图像、文档、音频）的模块，必须模块级标慢，
   或在 `EXCEPTIONS` 里登记并写明理由——新文件漏标会在这里变红；
5. `EXCEPTIONS` 不许腐坏：键必须存在、必须真的导入了重库、理由非空、且不该再被标慢。

门禁口径见 `docs/TESTING.md`「快速门禁与全量」。
"""
from __future__ import annotations

import ast
import pathlib
import re

TESTS = pathlib.Path(__file__).parent

MODULE_SLOW_REQUIRED = (
    "test_host_text.py",
    "test_project_copy_boundaries.py",
)
STORYBOARD_PATTERN = "test_storyboard_*.py"
MARK = "pytestmark = pytest.mark.slow"

# 判定为「重」的导入：起子进程、解码图像、生成文档、读写音频。
HEAVY_IMPORTS = frozenset({"subprocess", "PIL", "pypdf", "reportlab", "docx", "mutagen", "imageio",
                           "fontTools", "wave"})

# 默认集里确实导入重库、但用例本身是数据校验、未达 slow 门槛的模块。
# 只登记「已核过」的；新增模块默认必须标慢，不要往这里加。
EXCEPTIONS = {
    "test_business_staging.py": "PIL 只用于尺寸常量断言，主体是暂存合同（单例 <8s）",
    "test_content_domain_atomicity.py": "PIL 只用于生成最小图做原子性反例（单例 <8s）",
    "test_extraction_consistency_summary.py": "subprocess 只读已有验收产物，不启动模型（单例 <8s）",
    "test_screenplay_document_output.py": "docx/pypdf 仅导出格式校验，样例很小（单例 <8s）",
    "test_screenplay_source_outputs.py": "docx/pypdf 仅导出格式校验，样例很小（单例 <8s）",
}


def _source(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _imported_top_modules(path: pathlib.Path) -> set[str]:
    try:
        tree = ast.parse(_source(path))
    except SyntaxError:  # 语法坏的模块由别的测试兜住
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def _is_default_run(path: pathlib.Path) -> bool:
    """与 conftest 的默认集口径一致：ui / native / integration 之外。"""
    from conftest import INTEGRATION_MODULES, NATIVE_HINTS, UI_SUFFIX

    if path.name.endswith(UI_SUFFIX) or path.stem in INTEGRATION_MODULES:
        return False
    if any(hint in path.name for hint in NATIVE_HINTS):
        return False
    # 拼出来是为了让本文件自身不含这个字面量（否则 conftest 会把它当 UI 模块）。
    return ("Py" + "Side6") not in _source(path)


def _module_is_slow(path: pathlib.Path) -> bool:
    return MARK in _source(path)


def test_slow_marker_is_registered():
    """`slow` 必须注册，否则 strict-markers 下任何新用法都会被拒。"""
    text = _source(TESTS.parent / "pyproject.toml")
    block = re.search(r"markers = \[(.*?)\]", text, re.S)
    assert block, "pyproject.toml 没有 markers 段"
    assert '"slow:' in block.group(1), "pyproject.toml 的 markers 里没有注册 slow"


def test_listed_slow_modules_are_marked():
    """登记在清单里的慢模块必须真的带标记，防止名单腐坏。"""
    missing = [name for name in MODULE_SLOW_REQUIRED if not _module_is_slow(TESTS / name)]
    assert not missing, f"这些慢模块缺少模块级 slow 标记：{missing}"


def test_storyboard_modules_are_marked_slow():
    """storyboard 用例涉及真实图像/媒体 IO，一律算慢模块。"""
    unmarked = [p.name for p in sorted(TESTS.glob(STORYBOARD_PATTERN)) if not _module_is_slow(p)]
    assert not unmarked, f"这些 storyboard 模块缺少模块级 slow 标记：{unmarked}"


def test_heavy_importers_are_slow_or_whitelisted():
    """默认集里导入重库的模块必须标慢；否则显式登记并说明理由。"""
    offenders = []
    for path in sorted(TESTS.glob("test_*.py")):
        if not _is_default_run(path):
            continue
        if not (_imported_top_modules(path) & HEAVY_IMPORTS):
            continue
        if _module_is_slow(path) or path.name in EXCEPTIONS:
            continue
        heavy = sorted(_imported_top_modules(path) & HEAVY_IMPORTS)
        offenders.append(f"{path.name}（{', '.join(heavy)}）")
    assert not offenders, (
        "以下模块导入了重库却没标 slow："
        + "、".join(offenders)
        + "。要么加 `pytestmark = pytest.mark.slow`，要么在 EXCEPTIONS 登记理由。")


def test_exceptions_are_honest():
    """豁免名单不许腐坏：文件存在、确实导入重库、有理由、且没有被同时标慢。"""
    problems = []
    for name, reason in EXCEPTIONS.items():
        path = TESTS / name
        if not path.is_file():
            problems.append(f"{name} 不存在")
            continue
        if not (reason or "").strip():
            problems.append(f"{name} 没有写理由")
        if not (_imported_top_modules(path) & HEAVY_IMPORTS):
            problems.append(f"{name} 其实没有导入重库，豁免可以删掉")
        if _module_is_slow(path):
            problems.append(f"{name} 已经标了 slow，豁免可以删掉")
    assert not problems, "EXCEPTIONS 有腐坏条目：" + "；".join(problems)


def test_slow_is_excluded_by_default():
    """默认可执行集不含 slow；`--slow` 才是全量入口。"""
    readme = _source(TESTS.parent / "docs" / "TESTING.md")
    assert "--slow" in readme and "slow" in readme, "TESTING.md 没写全量入口"
    assert "and not slow" in _source(TESTS.parent / "pyproject.toml")
