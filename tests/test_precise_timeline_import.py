"""Interval dependencies never become unselected narrative scenes/events."""
import copy

from agent_fixtures import project as project
from test_adaptation_import import setup
from test_screenplay_append import append
from test_screenplay_timeline import create, import_points

from narrative.state_anchors import anchor_nodes


def events(state):
    return {node["id"] for node in state["nodes"] if node["node_type"] in {"event", "action"}}


def test_exact_selection_through_both_domains_and_change_records(project):
    ids = setup(project)
    extracted = copy.deepcopy(project.graph_snapshot())
    adaptation = project.import_adaptation([ids["事件1"]])
    assert events(adaptation) == {ids["事件1"]}
    assert {node["id"] for node in adaptation["state_anchors"]} == {ids["事件0"]}
    assert all(set(row) <= {"id", "node_type", "context_id", "source_position", "offset", "preceding_ids", "following_ids"} for row in adaptation["state_anchors"])
    script = create(project)
    local = import_points(project, script, [ids["事件1"]])
    assert events(local) == {ids["事件1"]}
    assert set(anchor_nodes(local)) == {ids["事件0"]}
    assert project.graph_snapshot() == extracted
    # Explicit import promotes only that endpoint to a normal node.
    adaptation = project.import_adaptation([ids["事件2"]])
    assert ids["事件2"] not in anchor_nodes(adaptation)
    assert events(adaptation) == {ids["事件1"], ids["事件2"]}
    local = append(project, script, [ids["事件2"]])
    assert events(local) == {ids["事件1"], ids["事件2"]}
    assert ids["事件2"] not in anchor_nodes(local)
    coat = [row for row in local["properties"] if row.get("property_id") == "服装"]
    assert {row["value"] for row in coat} == {"青衣", "白衣"}
    assert {row["event_id"] for row in coat} == {ids["事件0"], ids["事件2"]}
    assert all("end_event_id" not in row for row in coat)
    assert len({node["id"] for node in local["nodes"]}) == len(local["nodes"])


def test_append_earlier_event_keeps_existing_change_records_atomically(project):
    ids = setup(project)
    project.sync_adaptation()
    script = create(project)
    import_points(project, script, [ids["事件1"]])
    before = copy.deepcopy(project.read_screenplay_timeline(script))
    result = append(project, script, [ids["事件0"]])
    assert ids["事件0"] in events(result)
    assert [row for row in result["properties"] if row.get("property_id") == "服装"] == [
        row for row in before["properties"] if row.get("property_id") == "服装"]
    assert all("end_event_id" not in row for row in result["properties"])
    assert result["import_summary"]["mode"] == "append"


def test_boundary_events_do_not_introduce_their_own_participants_or_scenes(project):
    ids = setup(project)
    source = next(node for node in project.graph_snapshot()["nodes"] if node["id"] == ids["事件0"])
    source = copy.deepcopy(source)
    source["participants"].append(ids["丙"])
    assert project.apply_changes([source], [], project.graph_project_info()["revision"], reason="边界事件独有人物")["status"] == "accepted"
    selected = project.import_adaptation([ids["事件1"]])
    assert ids["丙"] not in {node["id"] for node in selected["nodes"]}
    assert ids["场景0"] not in {node["id"] for node in selected["nodes"]}
    assert ids["场景2"] not in {node["id"] for node in selected["nodes"]}
    assert events(selected) == {ids["事件1"]}
