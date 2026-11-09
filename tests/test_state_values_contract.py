"""属性查询统一返回「当前有效值列表」——对齐原始契约，`cardinality` 只管录入。"""
import pytest

from narrative.operations import Operations

TEXT = "郭靖先拜江南七怪。后来拜洪七公。又拜哲别。他记得自己的本名。"
QUOTES = ["郭靖先拜江南七怪。", "后来拜洪七公。", "又拜哲别。", "他记得自己的本名。"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    ops = Operations.create_graph_project(tmp_path / "state-values.nproj")
    sid = ops.import_source("值列表", TEXT)
    accepted = ops.submit_extraction(sid, {"nodes": [
        {"type": "person", "name": "郭靖", "quotes": [QUOTES[0]]},
        {"type": "person", "name": "江南七怪", "quotes": [QUOTES[0]]},
        {"type": "person", "name": "洪七公", "quotes": [QUOTES[1]]},
        {"type": "person", "name": "哲别", "quotes": [QUOTES[2]]},
        {"type": "scene", "name": "拜师", "quotes": [TEXT]},
        {"type": "event", "name": "拜江南七怪", "scene": "@5", "participants": ["@1", "@2"],
         "location": "未知", "quotes": [QUOTES[0]]},
        {"type": "event", "name": "拜洪七公", "scene": "@5", "participants": ["@1", "@3"],
         "location": "未知", "quotes": [QUOTES[1]]},
        {"type": "event", "name": "拜哲别", "scene": "@5", "participants": ["@1", "@4"],
         "location": "未知", "quotes": [QUOTES[2]]},
    ], "properties": []})
    assert accepted["status"] == "accepted", accepted
    return ops, sid, {node["name"]: node["id"] for node in ops.graph_snapshot()["nodes"]}


def record(sid, index, op, property_id, value=None, *, identifier, **extra):
    row = {"kind": "property_change", "id": identifier, "node_id": extra.pop("node_id", None),
           "property_id": property_id, "event_id": extra.pop("event_id"), "op": op,
           "disclosure": {"source_id": sid, "start": TEXT.index(QUOTES[index])},
           "evidence": {"source_id": sid, "quote": QUOTES[index]}, **extra}
    if op in {"set", "add", "remove"}:
        row["value"] = value
    return row


def commit(ops, noun, rows):
    return ops.apply_changes([], [noun, *rows], ops.graph_project_info()["revision"], "值列表契约")


def test_single_valued_property_still_returns_a_one_item_list(project):
    ops, sid, ids = project
    definition = {"kind": "property_definition", "id": "称呼", "value_type": "string", "cardinality": "one"}
    rows = [record(sid, 3, "set", "称呼", "郭靖", identifier="c1", node_id=ids["郭靖"], event_id=ids["拜江南七怪"])]
    assert commit(ops, definition, rows)["status"] == "accepted"
    state = ops.property_state(ids["郭靖"], "称呼")["state"]
    assert state == {"kind": "known", "values": ["郭靖"]}
    assert "value" not in state


def test_many_property_returns_every_current_value(project):
    ops, sid, ids = project
    definition = {"kind": "property_definition", "id": "同行者", "value_type": "node", "cardinality": "many"}
    rows = [record(sid, 0, "set", "同行者", [ids["江南七怪"]], identifier="m1", node_id=ids["郭靖"], event_id=ids["拜江南七怪"]),
            record(sid, 1, "add", "同行者", [ids["洪七公"]], identifier="m2", node_id=ids["郭靖"], event_id=ids["拜洪七公"])]
    assert commit(ops, definition, rows)["status"] == "accepted"
    state = ops.property_state(ids["郭靖"], "同行者")["state"]
    assert state["kind"] == "known" and len(state["values"]) == 2
    assert set(state["values"]) == {ids["江南七怪"], ids["洪七公"]}


def test_three_masters_are_one_definition_with_three_current_values(project):
    """郭靖三位师父：三条 one 记录、各指向一个对方，查询得到 3 项——不需要改成 many。"""
    ops, sid, ids = project
    definition = {"kind": "property_definition", "id": "师父", "value_type": "node", "cardinality": "one"}
    rows = [record(sid, 0, "set", "师父", ids["江南七怪"], identifier="t1", node_id=ids["郭靖"], event_id=ids["拜江南七怪"]),
            record(sid, 1, "set", "师父", ids["洪七公"], identifier="t2", node_id=ids["郭靖"], event_id=ids["拜洪七公"]),
            record(sid, 2, "set", "师父", ids["哲别"], identifier="t3", node_id=ids["郭靖"], event_id=ids["拜哲别"])]
    accepted = commit(ops, definition, rows)
    assert accepted["status"] == "accepted", accepted
    state = ops.property_state(ids["郭靖"], "师父")["state"]
    assert state["kind"] == "known"
    assert state["values"] == [ids["江南七怪"], ids["洪七公"], ids["哲别"]]
    # 定义仍是 one：多对方是"按对方分别维护"，不是把基数改成 many。
    assert next(row for row in ops.property_records() if row["id"] == "师父")["cardinality"] == "one"


def test_absent_unrecorded_and_unknown_stay_distinguishable(project):
    ops, sid, ids = project
    definitions = [
        {"kind": "property_definition", "id": name, "value_type": "string", "cardinality": "one"}
        for name in ("已清空", "已未知")
    ]
    rows = [record(sid, 0, "set", "已清空", "有值", identifier="p1", node_id=ids["郭靖"], event_id=ids["拜江南七怪"]),
            record(sid, 1, "clear", "已清空", identifier="p2", node_id=ids["郭靖"], event_id=ids["拜洪七公"]),
            record(sid, 1, "unknown", "已未知", identifier="p3", node_id=ids["郭靖"], event_id=ids["拜洪七公"])]
    assert ops.apply_changes([], [*definitions, *rows], ops.graph_project_info()["revision"], "三态")["status"] == "accepted"
    assert ops.property_state(ids["郭靖"], "已清空")["state"] == {"kind": "absent"}
    assert ops.property_state(ids["郭靖"], "已未知")["state"] == {"kind": "unknown"}
    # 从没记过的属性：未记录，不是空列表、也不是 unknown。
    assert ops.property_state(ids["郭靖"], "称呼")["state"] == {"kind": "unrecorded"}


def test_two_one_values_at_the_same_event_point_are_a_conflict(project):
    """`one` 是录入规则：同一事件点给两个当前值必须被拒，不能按提交顺序挑一个。"""
    ops, sid, ids = project
    definition = {"kind": "property_definition", "id": "称呼", "value_type": "string", "cardinality": "one"}
    rows = [record(sid, 0, "set", "称呼", "郭靖", identifier="s1", node_id=ids["郭靖"], event_id=ids["拜江南七怪"], sequence=7),
            record(sid, 0, "set", "称呼", "郭大侠", identifier="s2", node_id=ids["郭靖"], event_id=ids["拜江南七怪"], sequence=7)]
    before = ops.property_records()
    result = commit(ops, definition, rows)
    assert result["status"] == "staged"
    assert any(problem["code"] == "STATE_CONFLICT" for problem in result["diagnostics"])
    assert ops.property_records() == before
    assert ops.property_state(ids["郭靖"], "称呼")["state"] == {"kind": "unrecorded"}
