"""Prompt snapshot guard: text changed ⇒ version changed.

The snapshot (``tests/prompt_snapshot.json``) records ``key -> {version, digest[:16]}``
for every ``DEFAULT_PROMPTS`` entry. This test does not judge wording — 文案是否合理
仍靠要点断言与真实模型对照 — it only catches a silent text edit shipped under an old
version number, and a version bump that changed nothing.

Update with ``python scripts/update_prompt_snapshot.py --write`` after a human confirms.
"""
from __future__ import annotations

from prompt_snapshot import SNAPSHOT, compare, current_entries, digest, load_snapshot, summarise


def test_snapshot_file_exists():
    """快照必须存在，否则「文本变动」没有任何基线。"""
    assert SNAPSHOT.exists(), (
        f"{SNAPSHOT} 缺失；跑 python scripts/update_prompt_snapshot.py --write 生成基线")


def test_current_prompts_match_snapshot():
    """当前 DEFAULT_PROMPTS 与快照完全一致。"""
    snapshot = load_snapshot()
    current = current_entries()
    problems = compare(current, snapshot)
    detail = "\n".join("  " + problem["message"] for problem in problems)
    assert not problems, f"提示词快照与代码不一致（{len(problems)} 条）：\n{detail}"
    assert snapshot.get("prompts") == current


def test_text_change_without_version_change_is_rejected():
    """文本变了版本没变 → 失败，并给出「先升版本再更新快照」的提示。"""
    snapshot = {"prompts": {"extraction": {"version": "23", "digest": digest("旧文本")}},
                "allow_version_only": []}
    current = {"extraction": {"version": "23", "digest": digest("新文本")}}

    problems = compare(current, snapshot)

    assert [problem["kind"] for problem in problems] == ["text_without_version"]
    assert "文本已改但版本仍是 v23" in problems[0]["message"]
    assert "请先升版本再更新快照" in problems[0]["message"]


def test_version_change_without_text_change_is_rejected():
    """版本升了文本没变 → 失败，除非登记在 allow_version_only。"""
    entry = {"version": "23", "digest": digest("同一段文本")}
    snapshot = {"prompts": {"extraction": entry}, "allow_version_only": []}
    current = {"extraction": {"version": "24", "digest": entry["digest"]}}

    problems = compare(current, snapshot)
    assert [problem["kind"] for problem in problems] == ["version_without_text"]
    assert "但文本没变" in problems[0]["message"]

    allowed = {"prompts": {"extraction": entry}, "allow_version_only": ["extraction"]}
    assert compare(current, allowed) == []


def test_version_and_text_both_changed_needs_snapshot_update():
    """正常改法（升版本 + 改文本）仍要求把快照更新到新版本。"""
    snapshot = {"prompts": {"system": {"version": "10", "digest": digest("旧")}},
                "allow_version_only": []}
    current = {"system": {"version": "11", "digest": digest("新")}}

    problems = compare(current, snapshot)

    assert [problem["kind"] for problem in problems] == ["outdated"]
    assert "v10" in problems[0]["message"] and "v11" in problems[0]["message"]


def test_added_and_removed_prompts_are_reported():
    """新增或删除提示词条目同样要让快照跟上。"""
    snapshot = {"prompts": {"old": {"version": "1", "digest": digest("x")}}, "allow_version_only": []}
    current = {"new": {"version": "1", "digest": digest("y")}}

    kinds = {problem["kind"] for problem in compare(current, snapshot)}

    assert kinds == {"added", "removed"}


def test_summarise_prints_version_moves():
    """更新脚本的 diff 摘要要写清 key、旧新版本与哈希前缀。"""
    old = {"extraction": {"version": "23", "digest": "0123456789abcdef"}}
    new = {"extraction": {"version": "24", "digest": "fedcba9876543210"}}

    lines = summarise(old, new)

    assert lines == ["~ extraction  v23 → v24  01234567 → fedcba98"]
