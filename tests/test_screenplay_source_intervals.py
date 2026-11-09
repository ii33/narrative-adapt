"""Source coexistence is preserved; new local interval conflicts remain atomic."""
import copy

import pytest
from agent_fixtures import project as project
from test_screenplay_append import append
from test_screenplay_timeline import create, import_points


def seed_relations(ops):
    sid = ops.import_source("并列关系", "甲让乙办事。甲让丙办事。")
    result = ops.submit_extraction(sid, {"nodes": [
        {"type": "person", "name": name, "quotes": ["甲让乙办事。" if name != "丙" else "甲让丙办事。"]} for name in ("甲", "乙", "丙")
    ] + [{"type": "scene", "name": "办事", "quotes": ["甲让乙办事。甲让丙办事。"]},
         {"type": "event", "location": "未知", "name": "派乙", "scene": "办事", "participants": ["甲", "乙"], "quotes": ["甲让乙办事。"]},
         {"type": "event", "location": "未知", "name": "派丙", "scene": "办事", "participants": ["甲", "丙"], "quotes": ["甲让丙办事。"]}],
        "properties": [{"node": "甲", "property": "下属"  , "category": "未分类", "value": name, "value_type": "node", "at_event": event, "op": "set",
                        "quotes": [quote]} for name, event, quote in (("乙", "派乙", "甲让乙办事。"), ("丙", "派丙", "甲让丙办事。"))]})
    assert result["status"] == "accepted", result
    ops.sync_adaptation()
    return {node["name"]: node["id"] for node in ops.adaptation_snapshot()["nodes"]}


def test_first_append_preserves_source_parallel_values(project):
    ids = seed_relations(project)
    source = copy.deepcopy(project.adaptation_snapshot())
    script = create(project)
    candidates = project.import_screenplay_timeline_candidates(script)
    assert ids["办事"] in {row["id"] for row in candidates}
    assert all("diagnostics" not in row for row in candidates)
    result = append(project, script, [ids["办事"]])
    assert [r for r in result["properties"] if r.get("property_id") == "下属"] == [r for r in source["properties"] if r.get("property_id") == "下属"]
    assert result["import_summary"]["diagnostics"] == []
    assert project.adaptation_snapshot() == source


def test_split_import_allows_unchanged_source_coexistence(project):
    ids = seed_relations(project)
    script = create(project)
    import_points(project, script, [ids["派乙"]])
    result = append(project, script, [ids["派丙"]])
    assert len([r for r in result["properties"] if r.get("property_id") == "下属"]) == 2
    assert result["import_summary"]["diagnostics"] == []


@pytest.mark.parametrize("local_record", [False, True])
def test_source_coexistence_does_not_waive_local_edits_or_new_values(project, local_record):
    ids = seed_relations(project)
    script = create(project)
    first = import_points(project, script, [ids["派乙"]])
    row = next(r for r in first["properties"] if r.get("property_id") == "下属")
    project.set_screenplay_timeline_property(script, ids["甲"], "下属", ids["派乙"], "set", ids["甲"],
        record_id=None if local_record else row["id"],
        expected_timeline_revision=first["revision"], reason="本地关系")
    result = append(project, script, [ids["派丙"]])
    values = [r.get("value") for r in result["properties"] if r.get("property_id") == "下属"]
    assert ids["甲"] in values  # local edit kept
    assert ids["丙"] in values  # new source value imported
    assert result["import_summary"]["diagnostics"] == []


def test_initial_import_keeps_same_source_parallel_values(project):
    ids = seed_relations(project)
    result = import_points(project, create(project), [ids["办事"]])
    assert len([r for r in result["properties"] if r.get("property_id") == "下属"]) == 2
    assert result["import_summary"]["diagnostics"] == []
