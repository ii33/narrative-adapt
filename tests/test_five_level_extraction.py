"""Completed major-story summaries reuse already extracted phase nodes."""
from agent_fixtures import project as project


def test_finished_story_reuses_existing_phase_without_repeating_children(project):
    text = "甲进屋。甲离开。全篇结束。"
    sid = project.import_source("闭合篇章", text)
    unfinished = {"nodes": [
        {"type": "big_event", "name": "进屋离去"},
        {"type": "scene", "name": "屋内", "big_event": "@1"},
        {"type": "event", "location": "未知", "name": "进屋离去", "scene": "@2", "quotes": ["甲进屋。甲离开。"],
         "participants": ["@4"]},
        {"type": "person", "name": "甲", "quotes": ["甲进屋。"]},
    ], "properties": []}
    first = project.submit_extraction(sid, unfinished)
    assert first["status"] == "accepted", first
    assert not any(n["node_type"] == "storyline" for n in project.graph_snapshot()["nodes"])
    phase = first["node_ids"]["@1"]
    summary = {"nodes": [{"type": "storyline", "name": "屋内篇章", "children": [phase]}], "properties": []}
    result = project.submit_extraction(sid, summary)
    assert result["status"] == "accepted", result
    nodes = project.graph_snapshot()["nodes"]
    major = next(n for n in nodes if n["node_type"] == "storyline")
    assert major["events"] == [phase]
    # Completion derives from the last verified child evidence, never from a
    # model-supplied closing quote.
    assert major["completion"] == {"source_id": sid, "start": 8}
    assert major["disclosure"]["start"] == 0
    assert len([n for n in nodes if n["node_type"] == "big_event"]) == 1


def test_closing_quote_is_rejected_and_storyline_requires_child_evidence(project):
    text = "前文。甲进屋。结束。"
    sid = project.import_source("分段闭合", text)
    payload = {"nodes": [{"type": "storyline", "name": "全篇", "closing_quote": "结束。"}], "properties": []}
    checked = project.preview_extraction(sid, payload, start=3, end=len(text))
    assert not checked["valid"]
    unknown = next(d for d in checked["diagnostics"] if d["path"][-1] == "closing_quote")
    assert unknown["code"] == "DRAFT_SCHEMA"
    assert "closing_quote" in unknown["message"]
    empty = project.preview_extraction(sid, {"nodes": [{"type": "storyline", "name": "全篇"}], "properties": []})
    assert not empty["valid"]
    assert any(d["path"] == ["nodes", 0, "children"] for d in empty["diagnostics"])
    misplaced = project.preview_extraction(sid, {"nodes": [{"type": "scene", "name": "旧直属", "quotes": ["甲进屋。"], "storyline": "T1"}], "properties": []})
    assert not misplaced["valid"]
    assert any(d["path"] == ["nodes", 0, "storyline"] for d in misplaced["diagnostics"])
    repeated = project.import_source("重复闭合", "结束。结束。")
    result = project.preview_extraction(repeated, payload)
    assert not result["valid"]


def test_same_parent_declared_from_both_sides_is_one_edge(project):
    from copy import deepcopy
    text = "甲进屋。甲离开。全篇结束。"
    sid = project.import_source("双向归属声明", text)
    payload = {"nodes": [
        {"type": "storyline", "name": "屋内篇章", "children": ["@2"]},
        {"type": "big_event", "name": "进屋离去", "storyline": "@1", "children": ["@3"]},
        {"type": "scene", "name": "屋内", "big_event": "@2", "children": ["@4"]},
        {"type": "event", "location": "未知", "name": "进屋离去", "scene": "@3", "quotes": ["甲进屋。甲离开。"],
         "participants": ["@5"]},
        {"type": "person", "name": "甲", "quotes": ["甲进屋。"]},
    ], "properties": []}
    original = deepcopy(payload)
    outcome = project.submit_extraction(sid, payload)
    assert outcome["status"] == "accepted", outcome
    assert payload == original
    ids = outcome["node_ids"]
    nodes = {n["id"]: n for n in project.graph_snapshot()["nodes"]}
    for parent, child in [("@1", "@2"), ("@2", "@3"), ("@3", "@4")]:
        assert nodes[ids[parent]]["events"] == [ids[child]]


def test_conflicting_distinct_parents_are_not_deduplicated_away(project):
    text = "甲进屋。甲离开。全篇结束。"
    sid = project.import_source("冲突归属", text)
    payload = {"nodes": [
        {"type": "storyline", "name": "篇章一", "children": ["@3"]},
        {"type": "storyline", "name": "篇章二", "children": ["@3"]},
        {"type": "big_event", "name": "进屋离去", "children": ["@4"]},
        {"type": "scene", "name": "屋内", "big_event": "@3", "children": ["@5"]},
        {"type": "event", "location": "未知", "name": "进屋", "scene": "@4", "quotes": ["甲进屋。"],
         "participants": ["@6"]},
        {"type": "person", "name": "甲", "quotes": ["甲进屋。"]},
    ], "properties": []}
    before = project.graph_snapshot()
    result = project.submit_extraction(sid, payload)
    assert result["status"] == "staged"
    assert any(problem["code"] == "BIG_EVENT_STORYLINE" for problem in result["problems"])
    assert project.graph_snapshot() == before
