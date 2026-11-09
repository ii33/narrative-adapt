"""Static-check issues carry a clickable location: which object family and where."""
import pytest

from narrative.operations import Operations
from narrative.project_check import _locate

TEXT = "甲在屋外等候。"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    ops = Operations.create_graph_project(tmp_path / "check-locations.nproj")
    source = ops.import_source("定位", TEXT)
    accepted = ops.submit_extraction(source, {"nodes": [
        {"type": "person", "name": "甲", "quotes": [TEXT]},
        {"type": "scene", "name": "屋外", "quotes": [TEXT]},
        {"type": "event", "name": "等候", "scene": "@2", "quotes": [TEXT],
         "participants": ["@1"], "location": "未知"},
    ], "properties": []})
    assert accepted["status"] == "accepted", accepted
    return ops, source


def test_static_record_error_has_record_location_and_source_coordinates():
    record = {'id': 'PC1', 'kind': 'property_change', 'node_id': 'E1',
              'evidence': {'source_id': 'SRC1', 'start': 2, 'quote': '发现'}}
    node = {'id': 'E1', 'kind': 'node', 'name': '发现差异'}
    result = _locate({'code': 'VALUE', 'message': '属性值类型不符', 'object_id': 'PC1'},
                     {'E1': node}, {'PC1': record}, {'SRC1': {'title': '样本'}})
    assert result['path'] == ['property_records', 'PC1']
    assert result['reason'] == '属性值类型不符'
    assert result['target']['record_id'] == 'PC1'
    assert result['target']['kind'] == 'property_change'
    assert (result['source_id'], result['start'], result['end']) == ('SRC1', 2, 4)


def test_static_node_error_preserves_producer_field_location():
    issue = {'code': 'REF', 'message': '人物引用不存在', 'object_id': 'E1',
             'path': ['objects', 'E1', 'participants', 0]}
    result = _locate(issue, {'E1': {'kind': 'node', 'id': 'E1', 'name': '会面'}}, {}, {})
    assert result['path'] == issue['path']
    assert result['location_scope'] == 'field'
    assert result['target']['kind'] == 'reference'
    assert result['target']['node_id'] == 'E1'


def test_project_wide_check_does_not_invent_a_repair_field():
    result = _locate({'code': 'ORDER', 'message': '来源顺序整体冲突'}, {}, {}, {})
    assert result['path'] == []
    assert result['target']['kind'] == 'project'
    assert result['location_scope'] == 'operation'


@pytest.mark.parametrize(('code', 'locator', 'expected'), [
    ('PARTICIPANTS', {'object_id': 'E1'}, 'participants'),
    ('EVENT_LOCATION_REQUIRED', {'object_id': 'E1'}, 'event_location'),
    ('EVENT_LOCATION_VALUE', {'object_id': 'E1'}, 'event_location'),
    ('STORYLINE_RANGE', {'object_id': 'T1'}, 'storyline_range'),
    ('BIG_EVENT_STORYLINE', {'object_id': 'B1'}, 'storyline_range'),
    ('ACTION_LEAF', {'object_id': 'E1'}, 'reference'),
    ('definition_unused', {'property_id': '修为境界'}, 'property_definition'),
])
def test_target_kind_names_the_object_family_to_jump_to(code, locator, expected):
    objects = {'E1': {'kind': 'node', 'id': 'E1', 'node_type': 'event', 'name': '等候'},
               'T1': {'kind': 'node', 'id': 'T1', 'node_type': 'storyline', 'name': '屋内篇章'},
               'B1': {'kind': 'node', 'id': 'B1', 'node_type': 'big_event', 'name': '进屋'}}
    result = _locate({'code': code, 'message': 'x'} | locator, objects, {}, {})
    assert result['target']['kind'] == expected


def test_project_wide_aggregate_without_any_location_is_project():
    """「27 条属性归入其他，分布在 14 个节点」是全工程汇总，没有可跳转的对象。"""
    issue = {'code': 'category_unassigned', 'severity': 'info',
             'message': '有 27 条属性暂时归入「其他」，分布在 14 个节点',
             'node_id': None, 'property_id': None, 'record_ids': []}
    result = _locate(issue, {}, {}, {})
    assert result['target']['kind'] == 'project'
    assert all(result['target'][key] is None for key in
               ('property_id', 'node_id', 'record_id', 'source_id', 'start', 'end', 'quote'))


def test_definition_with_a_property_id_still_points_at_the_definition():
    """正向对照：兜底不能把「有定位对象」的条目也压成 project。"""
    result = _locate({'code': 'definition_unused', 'message': 'x', 'property_id': '修为境界'}, {}, {}, {})
    assert result['target']['kind'] == 'property_definition'
    assert result['target']['property_id'] == '修为境界'


def test_unused_definition_points_at_the_definition_not_the_project(project):
    ops, _source = project
    definitions = [{'kind': 'property_definition', 'id': '修为境界',
                    'value_type': 'string', 'cardinality': 'one'}]
    assert ops.apply_changes([], definitions, ops.graph_project_info()['revision'],
                             reason='加一个没人用的定义')['status'] == 'accepted'
    issue = next(row for row in ops.check_project()['issues'] if row['code'] == 'definition_unused')
    assert issue['target']['kind'] == 'property_definition'
    assert issue['target']['property_id'] == '修为境界'
    assert issue['target']['node_id'] is None and issue['target']['source_id'] is None
    assert issue['message'] == '属性定义「修为境界」没有任何取值记录'


def test_uncategorized_aggregate_is_project_level_in_a_real_check(project):
    """真跑一遍：没有分类的记录汇总出来没有定位对象，kind 必须是 project。

    草稿层的 category 现在必填，所以「没有分类」只可能来自手工写入或存量记录。
    """
    ops, _source = project
    nodes = ops.graph_snapshot()["nodes"]
    person = next(row for row in nodes if row["node_type"] == "person")
    event = next(row for row in nodes if row["node_type"] == "event")
    recorded = ops.record_property_change(person["id"], "称呼", event["id"], "set",
        expected_revision=ops.graph_project_info()["revision"], reason="手工补一条没有分类的记录",
        value="阿甲", value_type="string", quote=TEXT)
    assert recorded["status"] == "accepted", recorded
    issue = next(row for row in ops.check_project()["issues"] if row["code"] == "category_unassigned")
    assert issue["target"]["kind"] == "project"
    assert all(issue["target"][key] is None for key in
               ("property_id", "node_id", "record_id", "source_id", "start", "end", "quote"))
    assert issue["property_id"] is None and not issue["record_ids"]
    assert issue["message"].startswith("有 1 条属性未分类")


def test_node_level_issue_carries_node_and_source_coordinates(project):
    ops, source = project
    issue = next(row for row in ops.check_project()['issues'] if row['code'] == 'scene_without_big_event')
    node = next(row for row in ops.graph_snapshot()['nodes'] if row['node_type'] == 'scene')
    assert issue['target']['kind'] == 'node'
    assert issue['target']['node_id'] == issue['node_id'] == node['id']
    # 坐标直接取既有证据，不新算：场景的证据由直属事件派生，就是本文开头那句。
    assert issue['source_id'] == issue['target']['source_id'] == source
    assert issue['start'] == issue['target']['start'] == 0
    assert issue['end'] == issue['target']['end'] == len(TEXT)
    assert issue['quote'] == issue['target']['quote'] == TEXT
