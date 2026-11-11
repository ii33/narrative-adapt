"""Current world item possession is persisted, explicit, and checked at every write boundary."""

from __future__ import annotations

import copy
import json

import pytest
from agent_fixtures import project as project

TEXT = "一块石头在地上。甲捡起石头。甲把石头交给乙。乙放下石头。"
SENTENCES = TEXT.split("。")[:-1]
# 逐句的参与者（事件必填）：只放原文里真的在场的人。第一句只交代石头落地，
# 没有人物出场，给空数组即"确无参与者"。
PARTICIPANTS = ([], ["甲"], ["甲", "乙"], ["乙"])


def setup(project):
    sid = project.import_source("合成物体流转原文", TEXT)
    nodes = [
        {"type": "scene", "name": "石头流转", "quotes": [TEXT]},
        {"type": "person", "name": "甲", "quotes": [SENTENCES[1] + "。"]},
        {"type": "person", "name": "乙", "quotes": [SENTENCES[2] + "。"]},
    ]
    nodes += [
        {"type": "event", "name": sentence, "scene": "石头流转", "location": "未知",
         "quotes": [sentence + "。"], "participants": list(participants)}
        for sentence, participants in zip(SENTENCES, PARTICIPANTS, strict=True)
    ]
    assert (
        project.submit_extraction(sid, {"nodes": nodes, "properties": []})["status"] == "accepted"
    )
    ids = {n["name"]: n["id"] for n in project.graph_snapshot()["nodes"]}
    item = {
        "kind": "node",
        "id": "I1",
        "node_type": "item",
        "name": "石头",
        "context_id": "canon",
        "evidence": {"source_id": sid, "quote": "石头"},
        "disclosure": {"source_id": sid, "start": 2},
        "evidence_spans": [{"source_id": sid, "start": 2, "end": 4, "quote": "石头"}],
    }
    return sid, ids, item


def record(
    sid,
    ids,
    value="未知",
    *,
    identifier="holder0",
    event_index=0,
    value_type="either",
    scope="world",
):
    sentence = SENTENCES[event_index] + "。"
    result = {
        "kind": "property_change",
        "id": identifier,
        "node_id": "I1",
        "property_id": "持有者",
        "value": value,
        "event_id": ids[SENTENCES[event_index]],
        "op": "set", "sequence": TEXT.index(sentence),
        "scope": scope,
        "category": "归属与持有",
        "evidence": {"source_id": sid, "quote": sentence},
        "disclosure": {"source_id": sid, "start": TEXT.index(sentence)},
    }
    if scope == "cognition":
        result["holder_id"] = ids["甲"]
    definition = {
        "kind": "property_definition",
        "id": "持有者",
        "value_type": value_type,
        "cardinality": "one",
    }
    return [definition, result]


def prepare(project, objects, records, revision):
    return project.prepare_changes(objects, records, revision, snapshot=project.capture_changes())


def install(project, *, value="未知", value_type="either"):
    sid, ids, item = setup(project)
    result = project.apply_changes(
        [item],
        record(sid, ids, value, value_type=value_type),
        project.graph_project_info()["revision"],
        reason="合成原子写入",
    )
    assert result["status"] == "accepted"
    return sid, ids, item


@pytest.mark.parametrize(
    "value,value_type",
    [
        ("未知", "either"),
        ("无", "either"),
        ("未知", "string"),
        ("无", "string"),
        ("named", "node"),
        ("named", "either"),
    ],
)
def test_same_atomic_write_can_establish_real_explicit_possession(project, value, value_type):
    sid, ids, item = setup(project)
    if value == "named":
        value = ids["甲"]
    result = project.apply_changes(
        [item],
        record(sid, ids, value, value_type=value_type),
        project.graph_project_info()["revision"],
        reason="合成合法持有记录",
    )
    assert result["status"] == "accepted"
    assert any(
        r.get("node_id") == "I1" and r.get("property_id") == "持有者" and r.get("value") == value
        for r in project.property_records()
    )


@pytest.mark.parametrize(
    "problem", ["missing", "cognition", "blank", "arbitrary", "any_object", "self"]
)
def test_missing_or_nonworld_or_invalid_holder_does_not_create_item(project, problem):
    sid, ids, item = setup(project)
    rows = record(sid, ids)
    if problem == "missing":
        rows = []
    elif problem == "cognition":
        rows = record(sid, ids, ids["甲"], scope="cognition")
    elif problem == "blank":
        rows = record(sid, ids, "")
    elif problem == "arbitrary":
        rows = record(sid, ids, "随便一个人")
    elif problem == "any_object":
        rows = record(sid, ids, {"holder_id": ids["甲"]}, value_type="any")
    else:
        rows = record(sid, ids, "I1", value_type="node")
    before = copy.deepcopy(project.graph_snapshot())
    issues = prepare(project, [item], rows, project.graph_project_info()["revision"]).diagnostics
    assert any(r["severity"] == "error" for r in issues)
    assert (
        project.apply_changes(
            [item], rows, project.graph_project_info()["revision"], reason="无效状态不可写"
        )["status"]
        == "staged"
    )
    assert project.graph_snapshot() == before


def test_graph_only_item_write_cannot_bypass_possession_requirement(project):
    _sid, _ids, item = setup(project)
    rev = project.graph_project_info()["revision"]
    assert not prepare(project, [item], [], rev).valid
    assert project.apply_changes([item], [], rev, reason="缺持有人")["status"] == "staged"
    assert project.graph_project_info()["revision"] == rev


def test_clear_only_current_holder_cannot_leave_item_uncovered(project):
    sid, ids, _item = install(project)
    before = copy.deepcopy(project.graph_snapshot())
    rows = copy.deepcopy(project.property_records())
    result = project.record_property_change("I1", "持有者", ids[SENTENCES[1]], "clear",
        expected_revision=project.graph_project_info()["revision"], reason="清空不是明确未知")
    assert result['status'] == 'staged'
    assert any(r['code'] == 'ITEM_HOLDER_REQUIRED' for r in result['diagnostics'])
    assert project.graph_snapshot() == before and project.property_records() == rows


def test_atomic_transfer_and_explicit_unowned_keep_reading_cuts_historical(project):
    sid, ids, _item = install(project, value="无")
    for i, value in [(1, ids["甲"]), (2, ids["乙"]), (3, "无")]:
        rows = record(sid, ids, value, identifier=f"holder{i}", event_index=i)
        assert (
            project.apply_changes(
                [], rows[1:], project.graph_project_info()["revision"], reason="原子换手"
            )["status"]
            == "accepted"
        )
    for i, value in [(0, "无"), (1, ids["甲"]), (2, ids["乙"]), (3, "无")]:
        state = project.property_state("I1", "持有者", at=ids[SENTENCES[i]])
        assert state["state"]["values"] == [value]
    assert project.property_state("I1", "持有者")["state"]["values"] == ["无"]


def test_legacy_non_person_participant_reads_but_the_next_write_is_rejected(project):
    """存量资料里遗留的物品参与者：读得出来，改写时才被 participants 取值域拒掉。"""
    sid, ids, item = setup(project)
    assert project.apply_changes(
        [item], record(sid, ids, ids["甲"], value_type="node"),
        project.graph_project_info()["revision"], reason="存量夹具先落物品",
    )["status"] == "accepted"
    event = next(row for row in project.graph_snapshot()["nodes"] if row["node_type"] == "event")
    # Authorized isolated legacy fixture: 旧版本写下的正式资料，物品也在参与者里。
    with project.project.store.connect(write=True) as db:
        (data,) = db.execute("SELECT data FROM objects WHERE id=?", (event["id"],)).fetchone()
        stored = json.loads(data) | {"participants": [ids["甲"], item["id"]]}
        db.execute("UPDATE objects SET data=? WHERE id=?", (json.dumps(stored, ensure_ascii=False), event["id"]))
    # 读：节点快照、当前状态、改编视图都照旧工作。
    assert next(row for row in project.graph_snapshot()["nodes"]
                if row["id"] == event["id"])["participants"] == [ids["甲"], item["id"]]
    assert project.property_state(item["id"], "持有者")["state"] == {"kind": "known", "values": [ids["甲"]]}
    assert any(node["id"] == event["id"] for node in project.sync_adaptation()["nodes"])
    # 检查器只报告，不改写：存量参与者被诊断为 PARTICIPANTS，资料一个字节不动。
    assert any(issue["code"] == "PARTICIPANTS" for issue in project.check_project()["issues"])
    before = copy.deepcopy(project.graph_snapshot())
    replaced = project.apply_changes(
        [stored], [], project.graph_project_info()["revision"], reason="存量参与者改写")
    assert replaced["status"] == "staged"
    assert any(problem["code"] == "PARTICIPANTS" for problem in replaced["problems"])
    assert project.graph_snapshot() == before


def test_legacy_missing_holder_is_diagnosed_without_synthesizing_a_record(project):
    sid, ids, item = setup(project)
    # Authorized isolated legacy fixture: old DB contains item but no holder record.
    with project.project.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO objects(id,kind,data,accepted_revision) VALUES (?,?,?,?)",
            (
                item["id"],
                "node",
                json.dumps(item, ensure_ascii=False),
                project.graph_project_info()["revision"],
            ),
        )
    before = copy.deepcopy(project.property_records())
    assert not any(r.get("property_id") == "持有者" for r in before)
    project.property_node_summary("I1")
    assert project.property_records() == before
    assert any(
        r["code"] == "ITEM_HOLDER_REQUIRED"
        for r in prepare(
            project,
            [next(n for n in project.graph_snapshot()["nodes"] if n["node_type"] == "person")],
            [],
            project.graph_project_info()["revision"],
        ).diagnostics
    )
    result = project.apply_changes(
        [],
        record(sid, ids, "未知"),
        project.graph_project_info()["revision"],
        reason="合成原子写入",
    )
    assert result["status"] == "accepted"
    assert project.property_state("I1", "持有者")["state"]["values"] == ["未知"]


def test_latest_invalid_holder_cannot_be_hidden_by_a_valid_historical_value(project):
    sid, ids, _item = install(project)
    rows = record(sid, ids, "某个未建节点的人", identifier="invalid_current", event_index=2)[1:]
    before = copy.deepcopy(project.property_records())
    assert any(
        issue["severity"] == "error"
        for issue in prepare(
            project, [], rows, project.graph_project_info()["revision"]
        ).diagnostics
    )
    assert (
        project.apply_changes(
            [], rows, project.graph_project_info()["revision"], reason="无效值不可写"
        )["status"]
        == "staged"
    )
    assert project.property_records() == before


def test_fresh_prepare_rejects_legacy_item_gap_without_writing(project):
    _sid, _ids, item = setup(project)
    revision = project.graph_project_info()["revision"]
    with project.project.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO objects(id,kind,data,accepted_revision) VALUES (?,?,?,?)",
            (item["id"], "node", json.dumps(item, ensure_ascii=False), revision),
        )
    prepared = prepare(project, [], [], revision)
    assert not prepared.valid
    assert any(r["code"] == "ITEM_HOLDER_REQUIRED" for r in prepared.diagnostics)
    assert project.commit_changes(prepared, reason="legacy静态校验")["status"] == "rejected"
    assert project.graph_project_info()["revision"] == revision


def test_node_only_holder_definition_can_be_renamed_and_updated_physically(project):
    sid, ids, item = setup(project)
    assert (
        project.apply_changes(
            [item],
            record(sid, ids, ids["甲"], value_type="node"),
            project.graph_project_info()["revision"],
            reason="具名持有",
        )["status"]
        == "accepted"
    )
    project.rename_property(
        "持有者", "物品持有状态", "物理改名不留alias", project.graph_project_info()["revision"]
    )
    definition = {
        "kind": "property_definition",
        "id": "物品持有状态",
        "value_type": "either",
        "cardinality": "one",
        "semantic": "item_holder",
    }
    unowned = record(sid, ids, "无", identifier="unowned", event_index=3)[1] | {
        "property_id": "物品持有状态"
    }
    assert (
        project.apply_changes(
            [],
            [definition, unowned],
            project.graph_project_info()["revision"],
            reason="当前定义支持无/未知",
        )["status"]
        == "accepted"
    )
    assert project.property_state("I1", "物品持有状态")["state"]["values"] == ["无"]
    rows = project.property_records()
    assert not any(r["id"] == "持有者" or r["kind"] == "property_merge" for r in rows)
    assert next(r for r in rows if r["id"] == "物品持有状态")["value_type"] == "either"


def test_item_only_cognition_affiliation_does_not_hide_missing_world_holder(project):
    sid, ids, item = setup(project)
    affiliation = record(sid, ids, "某门派", scope="cognition", value_type="string")
    affiliation[0]["id"] = "门派归属"
    affiliation[1]["property_id"] = "门派归属"
    with project.project.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO objects(id,kind,data,accepted_revision) VALUES (?,?,?,?)",
            (
                item["id"],
                "node",
                json.dumps(item, ensure_ascii=False),
                project.graph_project_info()["revision"],
            ),
        )
        for row in affiliation:
            db.execute(
                "INSERT INTO property_records(id,kind,data,accepted_revision) VALUES (?,?,?,?)",
                (
                    row["id"],
                    row["kind"],
                    json.dumps(row, ensure_ascii=False),
                    project.graph_project_info()["revision"],
                ),
            )
    before = copy.deepcopy(project.property_records())
    values = project.property_node_summary("I1")["properties"]
    assert not any(r['property_id'] == '持有者' and r['scope'] == 'world' for r in values)
    assert project.property_state('I1', '持有者')['state'] == {'kind': 'unrecorded'}
    assert project.property_records() == before
    from narrative.project_package import layout
    from narrative.tool_api import call

    revision = project.graph_project_info()["revision"]
    state = project.read_project_state()
    assert any(
        r["code"] == "ITEM_HOLDER_REQUIRED" and r["object_id"] == "I1" for r in state["diagnostics"]
    )
    for audit in (
        project.property_audit(),
        project.property_audit({"I1"}),
        call(
            "audit_properties",
            {"node_ids": ["I1"]},
            project=layout(project.project_info().path).entry,
        ),
    ):
        assert any(
            r["code"] == "ITEM_HOLDER_REQUIRED" and r["node_id"] == "I1" for r in audit["issues"]
        )
    assert not any(
        r["code"].startswith("ITEM_HOLDER") for r in project.property_audit({ids["甲"]})["issues"]
    )
    assert project.property_records() == before
    assert project.graph_project_info()["revision"] == revision


def test_early_cut_before_real_holder_disclosure_does_not_leak_future_person(project):
    sid, ids, item = setup(project)
    result = project.apply_changes(
        [item],
        record(sid, ids, ids["乙"], event_index=2),
        project.graph_project_info()["revision"],
        reason="合成原子写入",
    )
    assert result["status"] == "accepted"
    summary = project.property_node_summary("I1", cut=ids[SENTENCES[0]])
    assert not any(r['property_id'] == '持有者' and r['value_node'] for r in summary['properties'])
    assert project.property_state('I1', '持有者', ids[SENTENCES[0]])['state'] == {'kind': 'unrecorded'}
    assert '乙' not in json.dumps(summary['properties'], ensure_ascii=False)
    assert project.property_state("I1", "持有者")["state"]["values"] == [ids["乙"]]


def test_atomic_current_definition_update_and_unknown_holder_for_another_item(project):
    sid, ids, item = setup(project)
    assert (
        project.apply_changes(
            [item],
            record(sid, ids, ids["甲"], value_type="node"),
            project.graph_project_info()["revision"],
            reason="初始具名状态",
        )["status"]
        == "accepted"
    )
    old_value = copy.deepcopy(next(r for r in project.property_records() if r["id"] == "holder0"))
    item2 = copy.deepcopy(item) | {"id": "I2", "name": "另一块石头"}
    definition = {
        "kind": "property_definition",
        "id": "持有者",
        "value_type": "either",
        "cardinality": "one",
        "semantic": "item_holder",
    }
    value = record(sid, ids, "未知", identifier="item2_holder")[1] | {"node_id": "I2"}
    assert (
        project.apply_changes(
            [item2],
            [definition, value],
            project.graph_project_info()["revision"],
            reason="同批当前定义及第二物体",
        )["status"]
        == "accepted"
    )
    assert next(r for r in project.property_records() if r["id"] == "holder0") == old_value
    assert project.property_state("I1", "持有者")["state"]["values"] == [ids["甲"]]
    assert project.property_state("I2", "持有者")["state"]["values"] == ["未知"]
    assert not any(
        r["kind"] in {"property_merge", "property_rename", "property_retraction"}
        for r in project.property_records()
    )


@pytest.mark.parametrize("second", ["bad", "other_person"])
@pytest.mark.parametrize("reverse", [False, True])
def test_same_position_conflicting_holders_are_rejected_independent_of_input_order(
    project, second, reverse
):
    sid, ids, item = setup(project)
    rows = record(sid, ids, ids["甲"], identifier="same_position_a", event_index=2)
    alternate = rows[1] | {
        "id": "same_position_b",
        "value": "未建节点的任意人" if second == "bad" else ids["乙"],
    }
    values = [rows[1], alternate]
    if reverse:
        values.reverse()
    records = [rows[0], *values]
    before = copy.deepcopy(project.graph_snapshot())
    diagnostics = prepare(
        project, [item], records, project.graph_project_info()["revision"]
    ).diagnostics
    assert any(row["code"] == "STATE_CONFLICT" and "同sequence" in row["message"] for row in diagnostics), (
        diagnostics
    )
    assert (
        project.apply_changes(
            [item], records, project.graph_project_info()["revision"], reason="无效状态不可写"
        )["status"]
        == "staged"
    )
    assert project.graph_snapshot() == before


def test_prepared_batch_uses_complete_prospective_item_and_holder_without_false_gap(project):
    sid, ids, item = setup(project)
    before = copy.deepcopy(project.graph_snapshot())
    complete = {"objects": [item], "property_records": record(sid, ids, "未知")}
    assert not any(
        row["code"].startswith("ITEM_HOLDER")
        for row in prepare(
            project, complete["objects"], complete["property_records"], before["revision"]
        ).diagnostics
    )
    missing = {"objects": [item], "property_records": []}
    assert any(
        row["code"] == "ITEM_HOLDER_REQUIRED" and row["object_id"] == "I1"
        for row in prepare(
            project, missing["objects"], missing["property_records"], before["revision"]
        ).diagnostics
    )
    assert project.graph_snapshot() == before


def test_prepared_batch_detects_clear_of_only_current_holder(project):
    sid, ids, _item = install(project)
    revision = project.graph_project_info()["revision"]
    ended = {'kind': 'property_change', 'id': 'holder-clear', 'node_id': 'I1',
        'property_id': '持有者', 'event_id': ids[SENTENCES[1]], 'op': 'clear',
        'sequence': TEXT.index(SENTENCES[1]),
        'disclosure': {'source_id': sid, 'start': TEXT.index(SENTENCES[1])},
        'evidence': {'source_id': sid, 'quote': SENTENCES[1] + '。'}}
    issues = prepare(project, [], [ended], revision).diagnostics
    assert any(row["code"] == "ITEM_HOLDER_REQUIRED" and row["object_id"] == "I1" for row in issues)
    assert project.graph_project_info()["revision"] == revision
    assert (
        next(r for r in project.property_records() if r["id"] == "holder0")["op"] == "set"
    )


def _audit_codes(project, code):
    return [row for row in project.property_audit()["issues"] if row["code"] == code]


def test_audit_sees_a_world_holder_record_and_stays_quiet(project):
    """防误报：有 world 持有者记录的物品，不应在属性审计里被报「必须记录持有状态」。

    历史 bug：审计直接拿原始记录跑 item_possession，而它只在"投影行带 derived 标记"时
    才认持有记录，于是每件物品都被判成没记持有者（真实工程的 13 件全中）。
    """
    sid, ids, item = setup(project)
    assert project.apply_changes(
        [item], record(sid, ids, ids["甲"], value_type="node"),
        project.graph_project_info()["revision"], reason="有持有者记录",
    )["status"] == "accepted"
    assert _audit_codes(project, "ITEM_HOLDER_REQUIRED") == []
    assert project.property_state(item["id"], "持有者")["state"] == {"kind": "known", "values": [ids["甲"]]}


def _legacy_item(project, item, records=()):
    """Authorized isolated legacy fixture：直接写库，模拟旧版本留下的真实状态。"""
    revision = project.graph_project_info()["revision"]
    with project.project.store.connect(write=True) as db:
        db.execute("INSERT INTO objects(id,kind,data,accepted_revision) VALUES (?,?,?,?)",
                   (item["id"], "node", json.dumps(item, ensure_ascii=False), revision))
        for row in records:
            db.execute("INSERT INTO property_records(id,kind,data,accepted_revision) VALUES (?,?,?,?)",
                       (row["id"], row["kind"], json.dumps(row, ensure_ascii=False), revision))


def test_audit_still_reports_an_item_without_any_holder_record(project):
    """反向：真没记持有者的物品必须继续报——不能修成"永不报"。"""
    _sid, _ids, item = setup(project)
    _legacy_item(project, item)
    identifiers = [row["node_id"] for row in _audit_codes(project, "ITEM_HOLDER_REQUIRED")]
    assert identifiers == [item["id"]]


def test_audit_still_reports_a_cognition_only_holder(project):
    """反向：只有认知持有者不算 world 持有状态，仍然要报。"""
    sid, ids, item = setup(project)
    cognition = record(sid, ids, ids["甲"], value_type="node", scope="cognition")
    _legacy_item(project, item, cognition)
    assert project.property_records()
    identifiers = [row["node_id"] for row in _audit_codes(project, "ITEM_HOLDER_REQUIRED")]
    assert identifiers == [item["id"]]
