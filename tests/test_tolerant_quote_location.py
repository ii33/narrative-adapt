"""事件引文的宽容定位（`U-11`）：只放宽排版，不放弃逐字。

两件事必须同时成立，少一条就会写进错误证据：

* **能命中**：只差排版——标点（换一个／少一个／多一个）、空白、不可见字符、全半角——
  归一化后在本批范围唯一命中，就用**原文截取到的逐字文本与真实坐标**，绝不把模型那串
  存进 `evidence.quote`；
* **不能命中**：差一个实词（`狠狠地`→`狠地`）、差一个字母或数字，归一化后仍然找不到，
  这时**丢弃这条引文**（事件照常入库、位置取本批范围起点、回一条 `info`），而不是把
  半对的句子当成证据。

归一化后多处命中仍整批拒收，并给出候选起点（`positions_sample`）与另一条消歧路
（`related_paths` 指向该行的 `anchor`）。`anchor` 路径不受影响（本来就精确）。

场景与地点在本文件里是凑齐「一句话可入库的事件」的最小脚手架：事件必须归场景
（`I-22`）、必须有自身 world 地点（`I-01`），容器证据只能从已验证下级派生（`I-10`），
所以每个批次都另带一条**能逐字命中**的同场事件，避免容器缺证据干扰断言。
"""
from __future__ import annotations

import pytest

from narrative.extraction import _find_quote, _search_quote
from narrative.operations import Operations

TEXT = "林岚把一把匕首狠狠地扎在周宁的肩上，众人惊呼：“住手！”"
STABBED = "匕首狠狠地扎在周宁的肩上"      # 原文逐字存在
SHOUT = "众人惊呼：“住手！”"
STUB = "林岚把一把匕首"                   # 同场桩事件：逐字命中，供容器派生证据


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    ops = Operations.create_graph_project(tmp_path / "tolerant.nproj")
    return ops, ops.import_source("宽容定位", TEXT)


def scene_draft(*events):
    """地点 @1、场景 @2，事件从 @3 起依次挂到该场景与地点。"""
    nodes = [{"type": "location", "name": "巷口"}, {"type": "scene", "name": "巷口一场"}]
    for row in events:
        nodes.append({"type": "event", "scene": "@2", "location": "@1", "participants": [], **row})
    return nodes


def submit(ops, source, nodes, start=0):
    return ops.submit_extraction(source, {"nodes": nodes, "properties": []}, start=start)


def nodes_by(ops) -> dict[str, dict]:
    return {node["node_type"] + node["name"]: node for node in ops.graph_snapshot()["nodes"]}


def notes(result, kind):
    return [row for row in result.get("info") or [] if row.get("note") == kind]


# --- 匹配层：能命中的一侧（只差排版） -------------------------------------------------

@pytest.mark.parametrize("quote,expected_quote", [
    ("匕首狠狠地扎在周宁的肩上。众人惊呼", "匕首狠狠地扎在周宁的肩上，众人惊呼"),
    ("匕首狠狠地扎在周宁的肩上众人惊呼", "匕首狠狠地扎在周宁的肩上，众人惊呼"),
    ("匕首狠狠地扎在周宁的肩上！！", "匕首狠狠地扎在周宁的肩上"),
    ("匕首狠狠地扎在周宁的 肩上", STABBED),
    ("匕首狠狠地扎在周宁的\u3000肩上", STABBED),
    ("匕首狠狠地\u200b扎在周宁的肩上", STABBED),
    ("众人惊呼:\"住手!\"", "众人惊呼：“住手"),
    ("住手?", "住手"),
])
def test_typesetting_differences_still_locate(quote, expected_quote):
    hit = _search_quote(TEXT, quote, unique_exact=True)
    assert hit.status == "normalized", (quote, hit)
    assert hit.quote == expected_quote
    # 存进证据的必须是原文：坐标真实、切片逐字相等。
    assert TEXT[hit.start:hit.start + len(hit.quote)] == hit.quote


def test_fullwidth_letters_and_digits_fold_under_nfkc():
    text = "他拿到第12号ABC通行证"
    hit = _search_quote(text, "第１２号ＡＢＣ", unique_exact=True)
    assert hit.status == "normalized"
    assert hit.quote == "第12号ABC"


def test_punctuation_difference_returns_the_original_slice_not_the_model_text():
    model_text = "匕首狠狠地扎在周宁的肩上。众人惊呼"
    hit = _search_quote(TEXT, model_text, unique_exact=True)
    assert hit.quote == "匕首狠狠地扎在周宁的肩上，众人惊呼"
    assert hit.quote != model_text


def test_exact_match_is_untouched_and_still_reported_as_exact():
    assert _find_quote(TEXT, STABBED) == (TEXT.index(STABBED), STABBED)
    hit = _search_quote(TEXT, STABBED, unique_exact=True)
    assert (hit.status, hit.start, hit.quote) == ("exact", TEXT.index(STABBED), STABBED)


def test_wrapping_quotes_are_still_trimmed_before_normalizing():
    # 旧行为保留：整句被再套一层引号时先剥首尾引号，剥完就逐字命中的走 exact。
    assert _find_quote("😀aaaa句😀", "\u201c句😀\u201d") == (5, "句😀")


# --- 匹配层：不能命中的一侧（差的是内容） ---------------------------------------------

@pytest.mark.parametrize("quote", [
    "匕首狠地扎在周宁的肩上",   # 狠狠地 → 狠地（少一个字）
    "短刀狠狠地扎在周宁的肩上",  # 匕首 → 短刀（换一个词）
    "匕首狠狠地扎在周宁的背肩上",  # 肩上 → 背肩上（多一个字）
])
def test_content_differences_never_locate(quote):
    """差一个实词就是另一个词：归一化不许把它抹平，否则等于放弃逐字校验。"""
    assert _search_quote(TEXT, quote, unique_exact=True).status == "missing"
    assert _find_quote(TEXT, quote) == (-1, quote)


@pytest.mark.parametrize("quote", ["第13号abcde", "第12号abce", "第12号abcdf", "第1号abcde"])
def test_digit_and_letter_differences_never_locate(quote):
    """数字与字母是内容不是排版：差一个就是另一个东西，归一化不许抹平。"""
    text = "他拿到第12号abcde通行证"
    assert _search_quote(text, quote, unique_exact=True).status == "missing"
    assert _find_quote(text, quote) == (-1, quote)


def test_punctuation_only_quote_has_no_content_to_match():
    # 只抹掉标点就什么都不剩的引文不参与比对：空串匹配等于「处处命中」。
    assert _search_quote(TEXT, "，。！").status == "missing"
    assert _search_quote(TEXT, "。“”").status == "missing"
    assert _find_quote(TEXT, "，。！") == (-1, "，。！")


def test_multiple_occurrences_are_ambiguous_with_candidate_starts():
    text = "甲说“走”。乙也说“走”。"
    exact = _search_quote(text, "“走”", unique_exact=True)
    assert exact.status == "ambiguous"
    first = text.index("“走”")
    assert exact.candidates == (first, text.index("“走”", first + 1))
    # 归一化后仍然多处命中：不是逐字，也不是唯一，照样拒收。
    repeated = "他走了，他走了"
    normalized = _search_quote(repeated, "他走了。", unique_exact=True)
    assert normalized.status == "ambiguous"
    assert normalized.candidates == (0, repeated.index("他走了", 1))
    # 属性行沿用旧口径（取首处）：宽容不改属性行的语义。
    relaxed = _search_quote(text, "“走”")
    assert (relaxed.status, relaxed.start) == ("exact", text.index("“走”"))


# --- 端到端：唯一命中就用原文坐标 -----------------------------------------------------

def test_event_with_typesetting_difference_stores_the_original_text_and_real_coordinate(project):
    ops, source = project
    model_text = "匕首狠狠地扎在周宁的肩上。众人惊呼"
    result = submit(ops, source, scene_draft(
        {"name": "扎肩", "quotes": [model_text]},
        {"name": "同场桩", "quotes": [STUB]}))
    assert result["status"] == "accepted", result
    stored = nodes_by(ops)["event扎肩"]
    assert stored["evidence"]["quote"] == "匕首狠狠地扎在周宁的肩上，众人惊呼"
    assert stored["evidence"]["quote"] != model_text
    assert stored["disclosure"] == {"source_id": source, "start": TEXT.index("匕首")}
    assert stored["evidence_spans"][0]["quote"] == stored["evidence"]["quote"]
    assert [(row["count"], row["indexes"]) for row in notes(result, "normalized")] == [(1, [2])]


def test_batch_start_is_added_to_the_tolerant_coordinate(project):
    ops, source = project
    offset = TEXT.index("众人")
    result = submit(ops, source, scene_draft(
        {"name": "惊呼", "quotes": ["住手?"]},
        {"name": "同场桩", "quotes": ["惊呼，众人"]}), start=offset)
    assert result["status"] == "accepted", result
    stored = nodes_by(ops)["event惊呼"]
    # 归一化命中的是原文里的「住手」，坐标是它在原文中的真实位置，不是批首。
    assert stored["evidence"]["quote"] == "住手"
    assert stored["disclosure"]["start"] == TEXT.index("住手")
    assert TEXT[stored["disclosure"]["start"]:][:2] == "住手"


# --- 端到端：找不到就丢弃引文、事件入库、回 info ----------------------------------------

def test_unlocatable_quote_is_dropped_while_the_event_is_stored(project):
    ops, source = project
    result = submit(ops, source, scene_draft(
        {"name": "扎肩", "quotes": ["匕首狠地扎在周宁的肩上"]},
        {"name": "同场桩", "quotes": [STABBED]}), start=4)
    assert result["status"] == "accepted", result
    stored = nodes_by(ops)["event扎肩"]
    # 没有证据就不冒称有：不写 evidence、不写 evidence_spans。
    assert "evidence" not in stored and stored["evidence_spans"] == []
    # 位置取**本批范围起点**：这是「近似」，不是模型那句话的坐标。
    assert stored["disclosure"] == {"source_id": source, "start": 4}
    dropped = notes(result, "dropped")
    assert [(row["count"], row["indexes"]) for row in dropped] == [(1, [2])]
    assert dropped[0]["message"].startswith("有 1 处事件引文在提交范围里定位不到原文")


def test_one_located_quote_keeps_the_event_and_the_bad_one_is_dropped(project):
    ops, source = project
    result = submit(ops, source, scene_draft(
        {"name": "扎肩", "quotes": ["匕首狠狠扎在周宁的肩上", STABBED]},
        {"name": "同场桩", "quotes": [STUB]}))
    assert result["status"] == "accepted", result
    stored = nodes_by(ops)["event扎肩"]
    assert stored["evidence"]["quote"] == STABBED
    assert [(row["count"], row["indexes"]) for row in notes(result, "dropped")] == [(1, [2])]


def test_dropped_quotes_are_folded_into_one_info(project):
    ops, source = project
    result = submit(ops, source, scene_draft(
        {"name": "甲", "quotes": ["不存在一"]},
        {"name": "乙", "quotes": ["不存在二"]},
        {"name": "同场桩", "quotes": [STUB]}))
    assert result["status"] == "accepted", result
    dropped = notes(result, "dropped")
    assert len(dropped) == 1, result.get("info")
    assert (dropped[0]["count"], dropped[0]["indexes"]) == (2, [2, 3])


# --- 端到端：多处命中仍然拒收，并给出候选起点 -------------------------------------------

def test_text_with_two_matches_rejects_and_points_at_anchor(project):
    ops, _source = project
    other = ops.import_source("重复句子", "甲说“走”。乙也说“走”。")
    result = submit(ops, other, [
        {"type": "location", "name": "巷口"},
        {"type": "scene", "name": "巷口一场"},
        {"type": "event", "name": "走一次", "scene": "@2", "location": "@1",
         "participants": [], "quotes": ["“走”"]}])
    assert result["status"] == "staged", result
    problem = next(row for row in result["diagnostics"]
                   if row.get("path") == ["nodes", 2, "quotes", 0])
    assert len(problem["positions_sample"]) == 2
    # 候选起点之外还给另一条消歧路：写 anchor 就没有歧义。
    assert problem["related_paths"] == [["nodes", 2, "anchor"]]
    assert ops.graph_snapshot()["nodes"] == []


# --- anchor 路径不变 ------------------------------------------------------------------

def test_anchor_still_wins_and_is_not_tolerated(project):
    ops, source = project
    start = TEXT.index(STABBED)
    result = submit(ops, source, scene_draft(
        {"name": "扎肩", "anchor": {"start": start, "end": start + len(STABBED)}},
        {"name": "同场桩", "quotes": [STUB]}))
    assert result["status"] == "accepted", result
    assert nodes_by(ops)["event扎肩"]["evidence"]["quote"] == STABBED
    bad = submit(ops, source, scene_draft(
        {"name": "坏锚点", "anchor": {"start": start, "end": len(TEXT) + 3}}))
    assert bad["status"] == "staged"
    assert any(row.get("path") == ["nodes", 2, "anchor"] for row in bad["diagnostics"])
