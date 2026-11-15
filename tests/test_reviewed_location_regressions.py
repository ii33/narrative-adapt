"""Reviewed location regressions, exercised through isolated Operations projects."""
import runpy
from copy import deepcopy
from pathlib import Path

import pytest

from narrative.operations import Operations
from narrative.property_core import validate_records


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    return Operations.create_graph_project(tmp_path / "regressions.nproj")


@pytest.mark.parametrize("cardinality", ["one", "many"])
@pytest.mark.parametrize("location", ["node", "无", "未知"])
def test_adaptation_initial_location_preserves_definition_cardinality(project, cardinality, location):
    text = "甲在大厅结束会议。"
    sid = project.import_source("地点", text)
    result = project.submit_extraction(sid, {"nodes": [
        {"type": "scene", "name": "会议"},
        {"type": "event", "name": "结束会议", "scene": "@1", "quotes": [text],
         "participants": ["@4"]},
        {"type": "location", "name": "大厅"},
        {"type": "person", "name": "甲"},
    ], "properties": [{"node": "@2", "property": "地点"  , "category": "未分类", "at_event": "@2", "op": "set",
        "value_type": "node", "cardinality": cardinality,
        "value": ["@3"] if cardinality == "many" else "@3"}]})
    assert result["status"] == "accepted", result
    state = project.sync_adaptation()
    event = next(row for row in state["nodes"] if row["node_type"] == "event")
    place = next(row for row in state["nodes"] if row["node_type"] == "location")
    value = place["id"] if location == "node" else location
    state = project.edit_adaptation_node(event["id"], {"initial_location": value},
        expected_revision=state["revision"], reason="地点集合回归")
    assert project.check_adaptation()["ok"], state
    definition = next(row for row in state["properties"] if row["kind"] == "property_definition")
    assert definition["cardinality"] == cardinality
    change = max((row for row in state["properties"] if row["kind"] == "property_change"),
        key=lambda row: row["sequence"])
    if value == "未知":
        assert change["op"] == "unknown" and "value" not in change
    else:
        assert change["value"] == ([value] if cardinality == "many" else value)


@pytest.mark.parametrize("occurrence", [0, 1])
@pytest.mark.parametrize("shortcut", [True, False])
def test_slice_disambiguates_repeated_property_evidence(project, occurrence, shortcut):
    quote = "甲说：走吧。"
    text = "😀" + quote + "\n次日。\n" + quote
    start = text.index(quote) if occurrence == 0 else text.rindex(quote)
    sid = project.import_source("重复引文", text)
    event = {"type": "event", "name": "催促出发", "scene": "@1", "quotes": [quote],
        "participants": ["@3"]}
    properties = []
    if shortcut:
        event["location"] = "未知"
    else:
        properties.append({"node": "@2", "property": "地点", "category": "未分类", "at_event": "@2",
            "op": "unknown", "value_type": "either"})
    raw = {"nodes": [{"type": "scene", "name": "出发"}, event,
        {"type": "person", "name": "甲"}], "properties": properties}
    original = deepcopy(raw)
    result = project.submit_extraction(sid, raw, start=start, end=start + len(quote))
    assert result["status"] == "accepted", result
    assert raw == original
    change = next(row for row in project.property_records() if row["kind"] == "property_change")
    assert change["disclosure"] == {"source_id": sid, "start": start}
    assert change["evidence"] == {"source_id": sid, "quote": quote}
    assert not validate_records(project.property_records(), project.graph_snapshot(),
        sources={sid: {"id": sid, "text": text}})


def test_repeated_event_quote_without_slice_is_still_rejected(project):
    quote = "甲说：走吧。"
    sid = project.import_source("未消歧", quote + "\n" + quote)
    result = project.submit_extraction(sid, {"nodes": [
        {"type": "scene", "name": "出发"},
        {"type": "event", "name": "出发", "scene": "@1", "location": "未知", "quotes": [quote],
         "participants": ["@3"]},
        {"type": "person", "name": "甲"},
    ], "properties": []})
    assert result["status"] == "staged", result
    assert not project.graph_snapshot()["nodes"]


@pytest.mark.parametrize("start", [None, -1, 1, True, 999])
def test_repeated_evidence_requires_valid_exact_disclosure(start):
    quote = "甲走了。"
    row = {"id": "PC1", "kind": "property_change", "node_id": "E1", "property_id": "地点",
        "event_id": "E1", "sequence": 0, "op": "unknown",
        "evidence": {"source_id": "source", "quote": quote}}
    if start is not None:
        row["disclosure"] = {"source_id": "source", "start": start}
    records = [{"id": "地点", "kind": "property_definition", "value_type": "either", "cardinality": "one"}, row]
    snapshot = {"nodes": [{"id": "E1", "node_type": "event"}],
        "sources": [{"id": "source", "text": quote + "\n" + quote}]}
    assert {issue["code"] for issue in validate_records(records, snapshot)} & {"EVIDENCE", "DISCLOSURE"}


def test_disclosure_cannot_borrow_matching_quote_from_another_source():
    quote = "甲走了。"
    records = [
        {"id": "地点", "kind": "property_definition", "value_type": "either", "cardinality": "one"},
        {"id": "PC1", "kind": "property_change", "node_id": "E1", "property_id": "地点",
         "event_id": "E1", "sequence": 0, "op": "unknown",
         "evidence": {"source_id": "original", "quote": quote},
         "disclosure": {"source_id": "other", "start": 0}},
    ]
    snapshot = {"nodes": [{"id": "E1", "node_type": "event"}], "sources": [
        {"id": "original", "text": quote}, {"id": "other", "text": quote}]}
    assert "DISCLOSURE" in {issue["code"] for issue in validate_records(records, snapshot)}


@pytest.mark.parametrize("malformed", [[], ["地点"], {}])
def test_malformed_property_id_stages_instead_of_crashing(project, malformed):
    sid = project.import_source("地点", "甲结束会议。")
    saved = project.submit_extraction(sid, {"nodes": [
        {"type": "scene", "name": "会议"},
        {"type": "event", "name": "结束", "scene": "@1", "location": "未知", "quotes": ["甲结束会议。"],
         "participants": ["@3"]},
        {"type": "person", "name": "甲"},
    ], "properties": []})
    assert saved["status"] == "accepted", saved
    event = next(row for row in project.graph_snapshot()["nodes"] if row["node_type"] == "event")
    before = project.property_records()
    revision = project.graph_project_info()["revision"]
    result = project.apply_changes([], [{"id": "malformed-change", "kind": "property_change",
        "node_id": event["id"], "event_id": event["id"], "property_id": malformed,
        "op": "unknown", "sequence": 999}], revision, "畸形属性引用回归")
    assert result["status"] == "staged", result
    assert any(issue["code"] == "TARGET" for issue in result["problems"])
    assert project.property_records() == before
    assert project.graph_project_info()["revision"] == revision


@pytest.mark.xfail(
    reason="scripts/accept_screenplay.py 的 seed 夹具还给人物/地点/场景带 quotes；按 `U-09` 现在会被拒收。"
           "该脚本不在本轮写入范围内，条目已列进交付报告的「仍假设 quotes 必填」清单，脚本修好后此用例自然变绿。",
    strict=False)
def test_screenplay_acceptance_seed_obeys_current_contract(project):
    seed = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "accept_screenplay.py"))["seed"]
    result = seed(project)
    assert result["segment_id"]
    from narrative.event_location import event_location_issues
    snapshot = project.graph_snapshot()
    assert len([row for row in snapshot["nodes"] if row["node_type"] == "event"]) == 3
    assert event_location_issues(snapshot["nodes"], project.property_records()) == []
