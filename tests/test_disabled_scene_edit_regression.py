"""Disabled scenes remain editable drafts without joining active output."""

import pytest
from test_screenplay_writing import body, table

from narrative.operations import Operations


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    ops = Operations.create_graph_project(tmp_path / "disabled-scenes.nproj")
    quote = "甲在门外等候。"
    source = ops.import_source("合成场次", quote)
    accepted = ops.submit_extraction(source, {"nodes": [
        {"type": "big_event", "name": "等待", "children": ["@2"]},
        {"type": "scene", "name": "门外等候", "children": ["@3"]},
        {"type": "event", "name": "甲等候", "quotes": [quote], "location": "未知"},
    ], "properties": []})
    assert accepted["status"] == "accepted", accepted
    adaptation = ops.sync_adaptation()
    screenplay = ops.create_screenplay("停用场次回归", reason="隔离测试")["screenplay"]
    big_event = next(node for node in adaptation["nodes"] if node["node_type"] == "big_event")
    timeline = ops.import_screenplay_timeline(
        screenplay["id"], [big_event["id"]], expected_timeline_revision=0,
        expected_adaptation_revision=adaptation["revision"], reason="导入合成剧情",
    )
    event = next(node for node in timeline["nodes"] if node["node_type"] == "event")
    span = ops.create_adaptation_span(
        screenplay["id"], "等候范围", selection_ids=[event["id"]],
        expected_timeline_revision=timeline["revision"], reason="合成范围",
    )["span"]
    segment = ops.create_script_segment(
        screenplay["id"], span["id"], "等候段", screenplay["structure_revision"], reason="合成段落",
    )
    return ops, screenplay["id"], segment["id"]


def apply_changes(ops, segment_id, changes):
    segment = ops.read_script_segment(segment_id)
    return ops.apply_scene_table_changes(
        segment_id, changes, segment["outline_revision"], reason="编辑停用场次"
    )


def test_insert_disabled_scene_keeps_editable_draft_out_of_output(project):
    ops, screenplay_id, segment_id = project
    active = table(ops, segment_id, [{"plan": {"title": "启用场", "summary": "保留场次"}}])["units"][0]
    result = apply_changes(ops, segment_id, [{
        "op": "insert", "after_id": active["id"],
        "unit": {"enabled": False, "plan": {"title": "停用草稿"}},
    }])
    assert result["status"] == "accepted"
    assert result["units"][0] == active
    disabled = result["units"][1]
    assert not disabled["enabled"]
    assert disabled["source_reference_origin"] == "explicit"
    assert disabled["plan"]["title"] == "停用草稿"
    assert disabled["plan"]["summary"] == ""
    assert ops.read_script_segment(segment_id)["units"][1] == disabled

    edited = ops.apply_script_block_changes(
        disabled["id"], [{"op": "insert", "block": {
            "type": "action", "payload": {"text": "仅供草稿编辑的正文"},
        }}], [], disabled["body_revision"], reason="保留停用场草稿正文",
    )
    assert edited["status"] == "accepted"
    assert edited["blocks"][0]["payload"]["text"] == "仅供草稿编辑的正文"
    assert ops.read_script_unit_context(disabled["id"])["unit"]["blocks"] == edited["blocks"]
    assert ops.prepare_script_writing_targets(screenplay_id)["unit_ids"] == [active["id"]]
    with pytest.raises(ValueError, match="启用场次"):
        ops.prepare_script_writing_targets(screenplay_id, [disabled["id"]], include_existing=True)
    for output in (ops.export_screenplay(screenplay_id), ops.compile_screenplay(screenplay_id, "text")):
        assert "停用草稿" not in output["text"]
        assert "仅供草稿编辑的正文" not in output["text"]
        assert "启用场" in output["text"]


@pytest.mark.parametrize("disable_with_plan", [False, True])
def test_edit_disabled_scene_plan_preserves_body_and_neighbor(project, disable_with_plan):
    ops, screenplay_id, segment_id = project
    first, neighbor = table(ops, segment_id)["units"]
    written = body(ops, first["id"])
    if not disable_with_plan:
        written = apply_changes(ops, segment_id, [{
            "op": "update", "unit_id": first["id"], "changes": {"enabled": False},
        }])["units"][0]
    plan = written["plan"] | {"title": "修改停用场", "summary": ""}
    changes = {"plan": plan} | ({"enabled": False} if disable_with_plan else {})
    result = apply_changes(ops, segment_id, [{
        "op": "update", "unit_id": first["id"], "changes": changes,
    }])
    assert result["status"] == "accepted"
    edited = result["units"][0]
    assert edited["id"] == first["id"] and not edited["enabled"]
    assert edited["plan"] == plan
    assert edited["plan_revision"] == written["plan_revision"] + 1
    assert edited["blocks"] == written["blocks"]
    assert edited["body_revision"] == written["body_revision"]
    assert edited["body_basis_id"] == written["body_basis_id"]
    assert edited["source_reference_origin"] == "explicit"
    assert result["units"][1] == neighbor
    assert ops.read_script_unit_context(first["id"])["unit"] == edited
    assert ops.prepare_script_writing_targets(screenplay_id)["unit_ids"] == [neighbor["id"]]


@pytest.mark.parametrize(("draft_plan", "problem_code", "problem_path"), [
    ({}, "SCENE_SUMMARY_EMPTY", ["summary"]),
    ({"summary": "有概要的停用草稿", "source_event_ids": ["missing-event"]},
     "SCENE_SOURCE_UNAVAILABLE", ["source_event_ids", 0]),
])
def test_reenabling_disabled_draft_still_validates_active_plan(
    project, draft_plan, problem_code, problem_path,
):
    ops, screenplay_id, segment_id = project
    disabled = table(ops, segment_id, [{"enabled": False, "plan": draft_plan}])["units"][0]
    staged = apply_changes(ops, segment_id, [{
        "op": "update", "unit_id": disabled["id"], "changes": {"enabled": True},
    }])
    assert staged["status"] == "staged"
    assert staged["problems"][0]["code"] == problem_code
    assert staged["problems"][0]["path"] == ["changes", 0, "changes", "plan", *problem_path]
    assert ops.read_script_segment(segment_id)["units"] == [disabled]
    assert ops.prepare_script_writing_targets(screenplay_id)["unit_ids"] == []

    result = apply_changes(ops, segment_id, [{
        "op": "update", "unit_id": disabled["id"],
        "changes": {"enabled": True, "plan": disabled["plan"] | {
            "summary": "已完成的场表概要", "source_event_ids": [],
        }},
    }])
    assert result["status"] == "accepted"
    assert result["units"][0]["id"] == disabled["id"]
    assert result["units"][0]["enabled"]
    assert ops.prepare_script_writing_targets(screenplay_id)["unit_ids"] == [disabled["id"]]
