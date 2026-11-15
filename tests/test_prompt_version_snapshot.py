"""文档里写的提示词版本必须是运行时真值，文档点名的测试必须真的存在。

两处漂移都是实际发生过的：``docs/GLOSSARY.md`` 长期写着「提取提示词（``extraction``
v23）」而代码早已是 v26；SPEC 里也曾点名一个不存在的守卫文件。版本靠人抄、测试名靠人记，
就一定会过期，所以这里按「合同」把它们钉住：

* 文档里凡是 `` `<prompt id>` v<N> `` 的写法，``<N>`` 必须等于
  ``prompt_defaults.DEFAULT_PROMPTS[id]["version"]``（升版而文档不跟，直接红）；
* 文档里凡是 ``tests/test_xxx.py`` 的写法，该文件必须存在（不许用不存在的守卫给自己背书）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from narrative.prompt_defaults import DEFAULT_PROMPTS

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PROSE = [ROOT / "AGENTS.md", ROOT / "README.md", ROOT / "PROJECT_HANDOFF.md"]
ID_VERSION = re.compile(r"`([a-z][a-z_]{2,})`\s*v(\d+)")
# §7 的索引表把版本放在独立单元格里：`| `extraction` | v26 | …`
TABLE_VERSION = re.compile(r"\|\s*`([a-z][a-z_]{2,})`\s*\|\s*v(\d+)\s*\|")
# 历史记录里的旧版本号是「当时的事实」，不是当前声明，不该被当成漂移。
HISTORICAL = re.compile(r"历史|当时|落后|取自")
TEST_REFERENCE = re.compile(r"(tests/[A-Za-z0-9_./-]+\.py)")


def _prose_files():
    yield from sorted(DOCS.rglob("*.md"))
    yield from (path for path in PROSE if path.is_file())


def _mentions():
    """``(文件, 行号, 提示词 id, 文档里的版本)``；只认已知的提示词 id。

    除普通的 `` `extraction` v26 `` 写法，也要认 §7 索引表的单元格形态。
    """
    for path in _prose_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            if HISTORICAL.search(line):
                continue
            for identifier, version in ID_VERSION.findall(line) + TABLE_VERSION.findall(line):
                if identifier in DEFAULT_PROMPTS:
                    yield path, number, identifier, version


def test_documented_prompt_versions_match_the_runtime():
    stale = [
        f"{path.relative_to(ROOT)}:{number} 写的是 `{identifier}` v{version}，"
        f"实际是 v{DEFAULT_PROMPTS[identifier]['version']}"
        for path, number, identifier, version in _mentions()
        if version != str(DEFAULT_PROMPTS[identifier]["version"])
    ]
    assert not stale, "文档里的提示词版本已过期：\n" + "\n".join(stale)


def test_every_prompt_id_is_documented_somewhere_in_the_extraction_spec():
    """提示词索引必须覆盖全部条目：新增一条提示词而不登记，等于没人知道它的规则在哪。"""
    spec = (DOCS / "extraction" / "SPEC.md").read_text(encoding="utf-8")
    missing = sorted(set(DEFAULT_PROMPTS) - {identifier for _p, _n, identifier, _v in _mentions() if _p.name == "SPEC.md"})
    assert not missing, f"SPEC 的提示词索引缺这些条目：{missing}"
    assert "## 7 提示词索引" in spec


def test_documents_only_name_tests_that_exist():
    """文档不许用不存在的测试给自己背书；但写明"已不存在/未落地"的历史说明除外。"""
    retracted = re.compile(r"不存在|已删除|已移除|未落地|不再存在|已不存在|no longer exists|never landed|does not exist")
    missing = []
    for path in _prose_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            if retracted.search(line):
                continue
            for candidate in TEST_REFERENCE.findall(line):
                if not (ROOT / candidate).is_file():
                    missing.append(f"{path.relative_to(ROOT)}:{number} 提到 {candidate}，但文件不存在")
    assert not missing, "文档点名了不存在的测试：\n" + "\n".join(missing)


@pytest.mark.parametrize("identifier", sorted(DEFAULT_PROMPTS))
def test_prompt_entry_has_a_version_string(identifier: str):
    """版本必须是可渲染的字符串，否则文档里的 `vN` 无从对齐。"""
    version = DEFAULT_PROMPTS[identifier]["version"]
    assert isinstance(version, str) and version.strip()
    assert version.isdigit(), f"{identifier} 的 version 不是纯数字：{version!r}"
