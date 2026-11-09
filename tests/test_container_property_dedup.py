"""Container property writes yield to the same property on a descendant event.

A scene/big_event/storyline summarizes its events; writing the same property on
the container as well would state one fact at two levels, and both levels reduce
on their own. The container write is dropped with an info notice, the rest of
the batch is stored normally, and an undeterminable hierarchy is never guessed.
"""
from agent_fixtures import project as project

from narrative.container_evidence import descendant_events
from narrative.property_core import container_property_duplicate

TEXT = "甲进城。乙开门。甲离开。"


def draft(container_properties):
    return {
        "nodes": [
            {"type": "person", "name": "甲", "quotes": ["甲进城。"]},
            {"type": "person", "name": "乙", "quotes": ["乙开门。"]},
            {"type": "scene", "name": "进城"},
            {"type": "event", "name": "甲进城", "scene": "进城", "participants": ["甲"], "location": "未知",
             "quotes": ["甲进城。"]},
            {"type": "event", "name": "乙开门", "scene": "进城", "participants": ["乙"], "location": "未知",
             "quotes": ["乙开门。"]},
        ],
        "properties": [
            {"node": "甲", "property": "情绪", "value": "紧张", "value_type": "string",
             "at_event": "甲进城", "op": "set", "quotes": ["甲进城。"]},
        ] + container_properties,
    }


def property_changes(project, property_id):
    return [row for row in project.property_records()
            if row.get("kind") == "property_change" and row.get("property_id") == property_id]


def test_container_write_duplicating_a_descendant_event_is_dropped_with_info(project):
    sid = project.import_source("容器判重", TEXT)
    result = project.submit_extraction(sid, draft([
        # The fact already belongs to the event itself.
        {"node": "甲进城", "property": "气氛", "value": "紧张", "value_type": "string",
         "at_event": "甲进城", "op": "set", "quotes": ["甲进城。"]},
        # Same property on its scene: derived from the event, so it is ignored.
        {"node": "进城", "property": "气氛", "value": "紧张", "value_type": "string",
         "at_event": "乙开门", "op": "set", "quotes": ["乙开门。"]},
    ]))
    assert result["status"] == "accepted" and result["valid"], result
    nodes = {node["name"]: node["id"] for node in project.graph_snapshot()["nodes"]}

    notices = [row for row in result["problems"] if row["severity"] == "info"]
    assert len(notices) == 1, result["problems"]
    notice = notices[0]
    assert notice["code"] == "CONTAINER_PROPERTY_DEDUP"
    assert notice["path"] == ["properties", 2]
    assert notice["property_id"] == "气氛" and notice["node_id"] == nodes["进城"]
    assert notice["event_id"] == nodes["甲进城"] and notice["duplicate_of"].startswith("PC")
    assert "下属事件" in notice["message"] and "已忽略" in notice["message"]
    # The winner is an ordinary properties row, so no node-field origin is named.
    assert "duplicate_of_origin" not in notice
    assert not [row for row in result["problems"] if row["severity"] == "error"]

    # The event record keeps the fact; the container record was never stored.
    assert [row["node_id"] for row in property_changes(project, "气氛")] == [nodes["甲进城"]]
    # Everything else in the batch still lands.
    assert [row["node_id"] for row in property_changes(project, "情绪")] == [nodes["甲"]]


def test_container_property_absent_from_events_is_stored(project):
    sid = project.import_source("容器属性存入", TEXT)
    result = project.submit_extraction(sid, draft([
        {"node": "甲进城", "property": "气氛", "value": "紧张", "value_type": "string",
         "at_event": "甲进城", "op": "set", "quotes": ["甲进城。"]},
        {"node": "进城", "property": "目的", "value": "见乙", "value_type": "string",
         "at_event": "乙开门", "op": "set", "quotes": ["乙开门。"]},
    ]))
    assert result["status"] == "accepted" and result["valid"], result
    nodes = {node["name"]: node["id"] for node in project.graph_snapshot()["nodes"]}
    assert not [row for row in result["problems"] if row["code"] == "CONTAINER_PROPERTY_DEDUP"]
    assert [row["node_id"] for row in property_changes(project, "目的")] == [nodes["进城"]]
    assert [row["node_id"] for row in property_changes(project, "气氛")] == [nodes["甲进城"]]


def test_a_different_scope_is_not_a_duplicate(project):
    sid = project.import_source("容器跨立场", TEXT)
    result = project.submit_extraction(sid, draft([
        # What 甲 believes about the event, anchored on that event.
        {"node": "甲进城", "property": "判断", "value": "安全", "value_type": "string",
         "scope": "cognition", "holder": "甲", "at_event": "甲进城", "op": "set", "quotes": ["甲进城。"]},
        # What the story world states about the scene: a different group.
        {"node": "进城", "property": "判断", "value": "危险", "value_type": "string",
         "at_event": "乙开门", "op": "set", "quotes": ["乙开门。"]},
    ]))
    assert result["status"] == "accepted" and result["valid"], result
    nodes = {node["name"]: node["id"] for node in project.graph_snapshot()["nodes"]}
    assert not [row for row in result["problems"] if row["code"] == "CONTAINER_PROPERTY_DEDUP"]
    stored = property_changes(project, "判断")
    assert sorted((row["node_id"], row["scope"]) for row in stored) == sorted(
        [(nodes["甲进城"], "cognition"), (nodes["进城"], "world")])


def test_undetermined_hierarchy_never_drops_the_container_write():
    nodes = {
        # Child referenced but not stored: the level below is unknown.
        "S1": {"id": "S1", "node_type": "scene", "events": ["E9"]},
        # Containment cycle: it proves nothing about levels.
        "B1": {"id": "B1", "node_type": "big_event", "events": ["S2"]},
        "S2": {"id": "S2", "node_type": "scene", "events": ["B1"]},
        # Two unrelated scenes: the sibling's event is not a descendant.
        "S3": {"id": "S3", "node_type": "scene", "events": ["E1"]},
        "S4": {"id": "S4", "node_type": "scene", "events": ["E2"]},
        "E1": {"id": "E1", "node_type": "event", "events": []},
        "E2": {"id": "E2", "node_type": "event", "events": []},
    }
    assert descendant_events(nodes, "S1") is None
    assert descendant_events(nodes, "B1") is None
    assert descendant_events(nodes, "S3") == ["E1"]
    existing = [{"kind": "property_change", "id": "PC1", "node_id": "E2", "property_id": "气氛",
                 "event_id": "E2", "op": "set"}]

    def record(container):
        return {"kind": "property_change", "id": "PC2", "node_id": container,
                "property_id": "气氛", "event_id": "E1", "op": "set"}

    # S1 and B1 prove nothing about levels; S3's only event is E1, so E2 is a stranger.
    for container in ("S1", "B1", "S3"):
        assert container_property_duplicate(record(container), existing, nodes) is None
    # The same fact under S4's own event is a real duplicate.
    assert container_property_duplicate(record("S4"), existing, nodes) == existing[0]


def test_the_shortcut_write_belongs_to_the_event_so_only_the_container_drops(project):
    """The scene's explicit location write is the one dropped, at its own slot.

    ``location`` is event-only, so the shortcut row can never be the dropped
    container write; what the notice must still say is that the record that won
    came from a node field the model filled in, not from a properties row.
    """
    sid = project.import_source("快捷地点判重", TEXT)
    result = project.submit_extraction(sid, draft([
        {"node": "进城", "property": "地点", "value": "未知", "value_type": "either",
         "at_event": "甲进城", "op": "set", "quotes": ["甲进城。"]},
    ]))
    assert result["status"] == "accepted" and result["valid"], result
    notices = [row for row in result["problems"] if row["code"] == "CONTAINER_PROPERTY_DEDUP"]
    assert len(notices) == 1, result["problems"]
    # properties[0] is 情绪, properties[1] is the explicit scene row; the
    # generated location shortcut rows start at properties[2].
    assert notices[0]["path"] == ["properties", 1]
    assert notices[0]["duplicate_of_origin"] == "location"
    assert notices[0]["duplicate_of_path"] == ["nodes", 3, "location"]
    nodes = {node["name"]: node["id"] for node in project.graph_snapshot()["nodes"]}
    # The winner is the event's own shortcut record, and both events keep theirs.
    assert notices[0]["duplicate_of"] in {row["id"] for row in property_changes(project, "地点")}
    assert sorted(row["node_id"] for row in property_changes(project, "地点")) == sorted(
        [nodes["甲进城"], nodes["乙开门"]])


MANY_TEXT = "甲进城。乙开门。甲离开。乙回头。甲停下。乙说话。"
MANY_QUOTES = ["甲进城。", "乙开门。", "甲离开。", "乙回头。", "甲停下。", "乙说话。"]
MANY_EVENTS = ["甲进城", "乙开门", "甲离开", "乙回头", "甲停下", "乙说话"]


def many_duplicates_draft():
    """Six scene writes that each restate the same property of one event."""
    nodes = [
        {"type": "person", "name": "甲", "quotes": ["甲进城。"]},
        {"type": "person", "name": "乙", "quotes": ["乙开门。"]},
        {"type": "scene", "name": "进城"},
    ] + [{"type": "event", "name": name, "scene": "进城", "participants": ["甲"],
          "location": "未知", "quotes": [quote]}
         for name, quote in zip(MANY_EVENTS, MANY_QUOTES, strict=True)]
    properties = []
    for index, (name, quote) in enumerate(zip(MANY_EVENTS, MANY_QUOTES, strict=True)):
        properties.append({"node": name, "property": "气氛", "value": f"事实{index}", "value_type": "string",
                           "at_event": name, "op": "set", "quotes": [quote]})
        properties.append({"node": "进城", "property": "气氛", "value": f"归纳{index}", "value_type": "string",
                           "at_event": name, "op": "set", "quotes": [quote]})
    return {"nodes": nodes, "properties": properties}


def test_preview_shows_a_bounded_summary_of_info_notices(project):
    """The compact preview carries the notices, not only how many there were."""
    sid = project.import_source("容器判重预览", MANY_TEXT)
    preview = project.preview_extraction(sid, many_duplicates_draft())
    assert preview["valid"], preview
    assert preview["info_count"] == 6
    assert len(preview["info"]) == 5
    assert preview["info_omitted"] == 1
    # No error or warning was reported, so the info summary stands alone.
    assert "diagnostics" not in preview
    first = preview["info"][0]
    assert first["severity"] == "info" and first["code"] == "CONTAINER_PROPERTY_DEDUP"
    assert first["path"] == ["properties", 1]
    assert first["property_id"] == "气氛" and first["duplicate_of"].startswith("PC")
    assert "下属事件" in first["message"]

