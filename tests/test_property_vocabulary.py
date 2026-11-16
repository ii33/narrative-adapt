"""五层属性白名单（`U-12`）与「编号是身份，名字是属性」（`U-13`）。

两条契约一起测，因为它们落在同一张表上：

* `U-12` 五层（T／B／S／E／A）都是事件，只写表里的属性名；表外的名字丢弃并回一条
  折叠 `info`（列该层允许的名字），分类由表带出；**人物／物品／地点／群体继续自由命名**。
* `U-13` 名字只是一条「名称」属性：改名写新的「名称」，旧名失效、新名生效；界面显示
  当前生效的名字，`node.name` 只是初始值。
"""
from __future__ import annotations

import pytest
from agent_fixtures import project as project

from narrative import property_vocabulary

TEXT = "甲进城。乙开门。甲离开。"


def draft(properties):
    """本地最小批次：一部／一大事件／一场景／两事件，地点用显式「未知」。"""
    return {
        "nodes": [
            {"type": "person", "name": "甲"},
            {"type": "person", "name": "乙"},
            {"type": "storyline", "name": "第一部"},
            {"type": "big_event", "name": "进城风波", "storyline": "第一部"},
            {"type": "scene", "name": "进城", "big_event": "进城风波"},
            {"type": "event", "name": "甲进城", "scene": "进城", "participants": ["甲"],
             "location": "未知", "quotes": ["甲进城。"]},
            {"type": "event", "name": "乙开门", "scene": "进城", "participants": ["乙"],
             "location": "未知", "quotes": ["乙开门。"]},
        ],
        "properties": properties,
    }


def changes(project, property_id):
    return [row for row in project.property_records()
            if row.get("kind") == "property_change" and row.get("property_id") == property_id]


def infos(result, note=None):
    rows = [row for row in result["problems"] if row.get("severity") == "info"]
    return [row for row in rows if note is None or row.get("note") == note]


# --- 表本身（纯函数） ---------------------------------------------------------------

def test_only_the_five_event_layers_are_constrained():
    """受约束的只有 T／B／S／E／A；人物／物品／地点／群体照旧自由命名。"""
    assert property_vocabulary.constrained("event")
    assert property_vocabulary.constrained("storyline")
    for node_type in ("person", "item", "location", "group", "other"):
        assert not property_vocabulary.constrained(node_type)
        assert property_vocabulary.allowed_names(node_type) == []


@pytest.mark.parametrize(
    ("layer", "allowed"),
    [
        ("storyline", ["下属大事件", "名称"]),
        ("big_event", ["下属场景", "名称"]),
        ("scene", ["下属事件", "名称"]),
        ("event", ["地点", "参与者", "锚点", "名称"]),
        ("action", ["所属事件", "名称"]),
    ],
)
def test_default_table_is_the_contract_table(layer, allowed):
    assert property_vocabulary.allowed_names(layer) == allowed


def test_categories_come_from_the_table():
    """分类由表带出：`名称` 归「基本信息」，地点也归「基本信息」。"""
    assert property_vocabulary.category_for("event", "名称") == "基本信息"
    assert property_vocabulary.category_for("scene", "名称") == "基本信息"
    assert property_vocabulary.category_for("event", "地点") == "基本信息"
    assert property_vocabulary.category_for("event", "参与者") == "参与者"
    assert property_vocabulary.category_for("event", "状态") is None


def test_english_and_legacy_spellings_count_as_the_same_entry():
    """同一件事的英文／历史写法照认：地点组的别名与 `event_location` 对齐。"""
    from narrative.event_location import LOCATION_PROPERTIES
    for name in LOCATION_PROPERTIES:
        assert property_vocabulary.match("event", name) is not None, name
    assert property_vocabulary.match("event", "name")["name"] == "名称"
    assert property_vocabulary.match("event", "participants")["name"] == "参与者"


def test_a_saved_config_can_be_changed_but_must_keep_the_name_property():
    config = property_vocabulary.default_vocabulary()
    config["scene"].append({"name": "气氛", "category": "内容"})
    assert property_vocabulary.category_for("scene", "气氛", config) == "内容"
    # 名称是身份属性，删掉就拒绝保存（`U-13`）。
    for layer in property_vocabulary.LAYERS:
        config[layer] = [entry for entry in config[layer] if entry["name"] != "名称"]
    with pytest.raises(ValueError, match="名称"):
        property_vocabulary.validate_vocabulary(config)
    with pytest.raises(ValueError, match="层"):
        property_vocabulary.validate_vocabulary({"person": []})


def test_the_white_list_is_reachable_from_the_tools(project):
    """模型要能拿到白名单：结构合同与分类工具都带着它，不靠提示词枚举（`U-12`）。"""
    from narrative.tool_api import _property_categories

    schema = project.get_schema()
    event_entries = schema["property_vocabulary"]["event"]["properties"]
    assert [entry["name"] for entry in event_entries] == property_vocabulary.allowed_names("event")
    assert schema["property_vocabulary"]["scene"]["label"] == "场景 S"

    result = _property_categories(project, {})
    assert result["property_vocabulary"]["event"]["properties"] == event_entries
    assert result["vocabulary"]["person"], "分类词表照旧随工具返回"


# --- 入库：表内通过、表外丢弃 -------------------------------------------------------

def test_inside_the_table_is_stored_and_the_table_sets_the_category(project):
    """表内的名字照常入库；分类由表带出（写的分类就是表里的那个，所以没有提醒）。"""
    sid = project.import_source("白名单内", TEXT)
    result = project.submit_extraction(sid, draft([
        {"node": "进城风波", "property": "名称", "category": "基本信息", "value": "进城风波（一）",
         "value_type": "string", "at_event": "乙开门", "op": "set"},
    ]))
    assert result["status"] == "accepted" and result["valid"], result
    stored = changes(project, "名称")
    assert len(stored) == 1 and stored[0]["category"] == "基本信息"
    assert not infos(result)


def test_a_category_that_disagrees_with_the_table_follows_the_table_with_one_info(project):
    """模型写的分类与表不符：以表为准，并回一条折叠 `info`（不是错误）。"""
    sid = project.import_source("分类以表为准", TEXT)
    result = project.submit_extraction(sid, draft([
        {"node": "进城风波", "property": "名称", "category": "未分类", "value": "进城风波（一）",
         "value_type": "string", "at_event": "乙开门", "op": "set"},
    ]))
    assert result["status"] == "accepted" and result["valid"], result
    stored = changes(project, "名称")
    assert stored[0]["category"] == "基本信息"
    notes = infos(result, "category")
    assert len(notes) == 1 and notes[0]["count"] == 1 and notes[0]["indexes"] == [0]


def test_a_name_outside_the_table_is_dropped_with_one_folded_info(project):
    """场景上写「状态」：表外名字丢弃，同类折叠成**一条** info，批次照常入库。"""
    sid = project.import_source("表外丢弃", TEXT)
    result = project.submit_extraction(sid, draft([
        {"node": "进城", "property": "状态", "category": "未分类", "value": "紧张",
         "value_type": "string", "at_event": "甲进城", "op": "set"},
        {"node": "进城", "property": "状态", "category": "未分类", "value": "更紧张",
         "value_type": "string", "at_event": "乙开门", "op": "set"},
    ]))
    assert result["status"] == "accepted" and result["valid"], result
    assert changes(project, "状态") == []
    notes = infos(result, "vocabulary")
    assert len(notes) == 1, result["problems"]
    assert notes[0]["count"] == 2 and notes[0]["indexes"] == [0, 1]
    # 消息里必须列出该层允许的名字，模型看一行就知道该写什么。
    for name in property_vocabulary.allowed_names("scene"):
        assert name in notes[0]["message"]
    assert "状态" not in property_vocabulary.allowed_names("scene")


def test_state_on_an_event_is_dropped_because_state_belongs_to_entities(project):
    """事件是一个时间点：模型自写的「载知内容」「持有者」这类状态属性会被丢弃。"""
    sid = project.import_source("事件上的状态", TEXT)
    result = project.submit_extraction(sid, draft([
        {"node": "甲进城", "property": "持有者", "category": "未分类", "value": "甲",
         "value_type": "string", "at_event": "甲进城", "op": "set"},
        {"node": "甲进城", "property": "载知内容", "category": "未分类", "value": "守门人知道口令",
         "value_type": "string", "at_event": "甲进城", "op": "set"},
    ]))
    assert result["status"] == "accepted" and result["valid"], result
    assert changes(project, "持有者") == [] and changes(project, "载知内容") == []
    notes = infos(result, "vocabulary")
    assert len(notes) == 1 and notes[0]["count"] == 2 and notes[0]["node_type"] == "event"


def test_people_keep_naming_their_own_properties(project):
    """人物不受白名单约束：写「身高」照常入库。"""
    sid = project.import_source("人物自由命名", TEXT)
    result = project.submit_extraction(sid, draft([
        {"node": "甲", "property": "身高", "category": "外貌", "value": "一百八十公分",
         "value_type": "string", "at_event": "甲进城", "op": "set"},
    ]))
    assert result["status"] == "accepted" and result["valid"], result
    stored = changes(project, "身高")
    assert len(stored) == 1 and stored[0]["category"] == "外貌"
    assert not infos(result)


@pytest.mark.parametrize("owner", ["第一部", "进城风波"])
def test_containers_may_record_a_name_change(project, owner):
    """主要故事与主要事件都能写「名称」：名字是属性，不是节点字段（`U-13`）。"""
    sid = project.import_source("容器改名", TEXT)
    result = project.submit_extraction(sid, draft([
        {"node": owner, "property": "名称", "category": "基本信息", "value": f"{owner}（改）",
         "value_type": "string", "at_event": "乙开门", "op": "set"},
    ]))
    assert result["status"] == "accepted" and result["valid"], result
    stored = changes(project, "名称")
    assert len(stored) == 1 and stored[0]["value"] == f"{owner}（改）"


def test_the_effective_name_reaches_the_browse_page(project):
    """浏览页读的是当前生效的名字，不是 `node.name`（`U-13`）。"""
    sid = project.import_source("浏览页名字", TEXT)
    result = project.submit_extraction(sid, draft([
        {"node": "进城风波", "property": "名称", "category": "基本信息", "value": "误入天庭",
         "value_type": "string", "at_event": "乙开门", "op": "set"},
    ]))
    assert result["status"] == "accepted" and result["valid"], result
    view = project.browse_view(node_type="big_event")
    shown = {row["id"]: row for row in view["nodes"]}
    big_event = next(row for row in project.graph_snapshot()["nodes"] if row["node_type"] == "big_event")
    assert shown[big_event["id"]]["name"] == "误入天庭"
    # 初始名仍是节点自己的值：名字变了不代表编号或节点换了一个。
    assert big_event["name"] == "进城风波"
