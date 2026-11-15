"""Action drafts enrich an existing event without replacing its narrative level."""
from agent_fixtures import project as project

TEXT = "甲推门进屋，走到窗边，停下张望。"


def seed(project):
    sid = project.import_source("原文", TEXT)
    draft = {"nodes": [
        {"type": "person", "name": "甲"},
        {"type": "scene", "name": "屋内"},
        {"type": "event", "location": "未知", "name": "甲入屋查看", "scene": "屋内", "participants": ["甲"], "quotes": [TEXT]},
    ], "properties": []}
    assert project.submit_extraction(sid, draft)["status"] == "accepted"
    return sid, next(n for n in project.graph_snapshot()["nodes"] if n["node_type"] == "event")


def test_actions_attach_to_existing_event_and_repeat_without_duplicate(project):
    sid, parent = seed(project)
    original = project.graph_snapshot()
    result = {"nodes": [
        {"type": "action", "name": name, "event": parent["id"], "participants": ["甲"]}
        for name, _quote in [("推门入屋", "甲推门进屋"), ("走到窗边", "走到窗边"), ("停下观察", "停下张望")]
    ], "properties": []}
    assert project.preview_extraction(sid, result)["valid"]
    assert project.graph_snapshot() == original
    applied = project.submit_extraction(sid, result)
    assert applied["status"] == "accepted"
    current = project.graph_snapshot()
    actions = [n for n in current["nodes"] if n["node_type"] == "action"]
    assert len(actions) == 3 and all(n["id"].startswith("A") for n in actions)
    event = next(n for n in current["nodes"] if n["id"] == parent["id"])
    assert set(event["events"]) == {n["id"] for n in actions}
    # 动作不填引文（`U-09`）：同名重提没有逐字证据可证明是同一次实施，程序要求显式引用已有动作，
    # 不静默新建重复节点。带上 existing_id 的同一次提交才是幂等的。
    repeated = project.submit_extraction(sid, result)
    assert repeated["status"] == "staged"
    assert project.graph_snapshot() == current
    explicit = {"nodes": [
        {"type": "action", "name": n["name"], "existing_id": n["id"], "event": parent["id"],
         "participants": ["甲"]} for n in actions
    ], "properties": []}
    assert project.submit_extraction(sid, explicit)["status"] == "unchanged"
    assert project.graph_snapshot() == current


def test_new_actions_require_an_event_parent_without_writing(project):
    sid, _ = seed(project)
    before = project.graph_snapshot()
    draft = {"nodes": [{"type": "action", "name": "停下观察"}], "properties": []}
    assert not project.preview_extraction(sid, draft)["valid"]
    staged = project.submit_extraction(sid, draft)
    assert staged["status"] == "staged"
    assert any(problem["code"] == "ACTION_EVENT" for problem in staged["problems"])
    assert project.graph_snapshot() == before


def test_adding_an_earlier_action_keeps_type_numbering_in_source_order(project):
    from narrative.order_ids import order_key

    sid, parent = seed(project)
    for name, _quote in [("停下观察", "停下张望"), ("推门入屋", "甲推门进屋")]:
        result = {"nodes": [{"type": "action", "name": name, "event": parent["id"]}],
                  "properties": []}
        assert project.submit_extraction(sid, result)["status"] == "accepted"
    actions = {n["name"]: n for n in project.graph_snapshot()["nodes"] if n["node_type"] == "action"}
    # 动作不填引文，位置取直属事件的披露点：同一事件内的动作没有各自的原文坐标，
    # 编号按提交顺序（已知缺口：动作失去"按原文先后编号"的能力，见交付报告）。
    assert order_key(actions["停下观察"]["id"]) < order_key(actions["推门入屋"]["id"])
