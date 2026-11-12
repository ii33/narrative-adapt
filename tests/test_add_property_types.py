"""Tool additions explicitly type new definitions and never infer IDs from text."""
import pytest
from agent_fixtures import project as project
from test_relationship_state import setup_relations

from narrative.project_package import layout
from narrative.tool_api import call


def setup(project):
    ids = setup_relations(project)
    return layout(project.project_info().path).entry, {"node_id": ids["甲"], "value": ids["乙"], "event_id": ids["甲认识朋友乙"], "quote": "甲认识朋友乙。", "category": "关系"}


@pytest.mark.slow
def test_explicit_node_new_role_is_drawn_and_any_same_id_remains_literal(project):
    entry, args = setup(project)
    assert call("record_property_change", args | {"op": "set", "expected_revision": project.graph_project_info()["revision"], "reason": "定向补录"} | {"property_id": "诈骗对象", "value_type": "node", "symmetric": False}, project=entry)["status"] == "accepted"
    assert call("record_property_change", args | {"op": "set", "expected_revision": project.graph_project_info()["revision"], "reason": "定向补录"} | {"property_id": "字面编号", "value_type": "any"}, project=entry)["status"] == "accepted"
    edges = call("relation_map", {}, project=entry)["edges"]
    assert any(e["kind"] == "诈骗对象" and e["from"] == args["node_id"] and e["to"] == args["value"] for e in edges)
    assert not any(e["kind"] == "字面编号" for e in edges)
    assert call("record_property_change", args | {"op": "set", "expected_revision": project.graph_project_info()["revision"], "reason": "定向补录"} | {"property_id": "诈骗对象"}, project=entry)["status"] in {"accepted", "unchanged"}


@pytest.mark.parametrize("changes,path", [({}, "value_type"), ({"value_type": "node", "symmetric": "false"}, "symmetric"), ({"value_type": []}, "value_type")])
def test_new_definition_requires_strict_explicit_type_and_symmetry_without_write(project, changes, path):
    entry, args = setup(project)
    before = project.graph_snapshot(), project.property_records()
    result = call("record_property_change", args | {"op": "set", "expected_revision": project.graph_project_info()["revision"], "reason": "定向补录"} | {"property_id": "新定义"} | changes, project=entry)
    assert result["status"] == "staged", result
    assert any(p["code"] == "CONTENT_VALIDATION" and p["path"] == [path] for p in result["problems"]), result["problems"]
    assert (project.graph_snapshot(), project.property_records()) == before


@pytest.mark.parametrize("changes,path", [({"value_type": "string"}, "value_type"), ({"symmetric": True}, "symmetric")])
def test_existing_definition_conflicts_do_not_change_definition_or_values(project, changes, path):
    entry, args = setup(project)
    before = project.graph_snapshot(), project.property_records()
    result = call("record_property_change", args | {"op": "set", "expected_revision": project.graph_project_info()["revision"], "reason": "定向补录"} | {"property_id": "朋友"} | changes, project=entry)
    assert result["status"] == "staged", result
    problem = next(p for p in result["problems"] if p["path"] == [path])
    assert problem["code"] == "CONTENT_VALIDATION" and "不一致" in problem["message"]
    assert (project.graph_snapshot(), project.property_records()) == before
