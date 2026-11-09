"""分类只来自显式值：程序不按属性名或取值猜，未给就是「未分类」。"""
import pytest

from narrative import property_categories
from narrative.operations import Operations

TEXT = "甲约好接乙。"
PERSON, SCENE, EVENT = "甲", "接送", "甲约好接乙"


@pytest.fixture
def ops(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    return Operations.create_graph_project(tmp_path / "categories.nproj")


def draft(*, category="未分类", property_name="身高", node="甲"):
    row = {"node": node, "property": property_name, "value": "一米七", "value_type": "string",
           "scope": "world", "at_event": EVENT, "op": "set", "quotes": [TEXT]}
    if category is not None:
        row["category"] = category
    return {"nodes": [
        {"type": "person", "name": PERSON, "quotes": [TEXT]},
        {"type": "scene", "name": SCENE, "quotes": [TEXT]},
        {"type": "event", "name": EVENT, "scene": SCENE, "participants": [PERSON],
         "location": "未知", "quotes": [TEXT]},
    ], "properties": [row]}


def person_id(ops):
    return next(row["id"] for row in ops.graph_snapshot()["nodes"] if row["name"] == PERSON)


def summary_row(ops, property_name):
    return next(row for row in ops.property_node_summary(person_id(ops))["properties"]
                if row["property_id"] == property_name)


def test_property_row_without_category_is_rejected(ops):
    source = ops.import_source("无分类", TEXT)
    result = ops.submit_extraction(source, draft(category=None))
    assert result["status"] == "staged"
    problem = next(row for row in result["diagnostics"] if row["path"] == ["properties", 0, "category"])
    assert problem["code"] == "DRAFT_SCHEMA"
    assert "未分类" in problem["message"]
    assert not ops.graph_snapshot()["nodes"]


def test_explicit_unclassified_is_accepted_and_marked_explicit(ops):
    source = ops.import_source("显式未分类", TEXT)
    accepted = ops.submit_extraction(source, draft(category="未分类"))
    assert accepted["status"] == "accepted", accepted
    row = summary_row(ops, "身高")
    assert row["category"] == "未分类"
    assert row["category_source"] == "explicit"


def test_category_outside_the_vocabulary_is_diagnosed(ops):
    source = ops.import_source("词表外", TEXT)
    accepted = ops.submit_extraction(source, draft(category="外星分类"))
    assert accepted["status"] == "accepted", accepted
    issue = next(row for row in ops.check_project()["issues"] if row["code"] == "category_unknown")
    assert "外星分类" in issue["message"]
    assert issue["target"]["kind"] == "property_definition"
    assert summary_row(ops, "身高")["category"] == "外星分类"


def test_property_name_no_longer_implies_a_category(ops):
    """以前「身高」会被按名字猜成「外貌」，现在只认显式值。"""
    source = ops.import_source("不猜分类", TEXT)
    accepted = ops.submit_extraction(source, draft(category="未分类", property_name="身高"))
    assert accepted["status"] == "accepted", accepted
    assert summary_row(ops, "身高")["category"] == "未分类"


def test_resolve_has_only_explicit_and_default_unclassified():
    assert property_categories.resolve({"property_id": "height"}, "person") == (
        "未分类", "default_unclassified")
    assert property_categories.resolve({"property_id": "height", "category": "外貌"}, "person") == (
        "外貌", "explicit")
    assert property_categories.resolve({"id": "PV1", "property_id": "height"}, "person",
                                       overrides={"PV1": "关系"}) == ("关系", "explicit")


def test_vocabulary_tail_is_unclassified_and_old_config_is_normalized():
    for node_type, values in property_categories.default_vocabulary().items():
        assert values[-1] == "未分类", node_type
        assert "其他" not in values, node_type
    # 旧工程保存的词表末项还是「其他」：归一化成一个尾巴，不动任何记录。
    assert property_categories.normalize_vocabulary({"person": ["基本信息", "其他"]})["person"] == [
        "基本信息", "未分类"]


@pytest.mark.parametrize("location", ["@2", "未知"])
def test_event_location_shortcut_generates_a_categorized_record(ops, location):
    """程序按 `event.location` 自动生成的地点行也必须带分类：地点的语义分类是「基本信息」。

    防的是「程序生成的行绕过分类必填」——v25 验收里 run2/run3 就是这样缺了 43 条。
    """
    text = "林岚在会议室的会开完了。"
    source = ops.import_source("快捷地点", text)
    accepted = ops.submit_extraction(source, {"nodes": [
        {"type": "person", "name": "林岚", "quotes": ["林岚"]},
        {"type": "location", "name": "会议室", "quotes": ["会议室"]},
        {"type": "scene", "name": "会议", "quotes": [text]},
        {"type": "event", "name": "会议结束", "scene": "@3", "quotes": [text],
         "participants": ["@1"], "location": location},
    ], "properties": []})
    assert accepted["status"] == "accepted", accepted
    records = [row for row in ops.property_records() if row.get("kind") == "property_change"]
    assert [(row["property_id"], row["category"]) for row in records] == [("地点", "基本信息")]
    assert not any(issue["code"] == "category_unassigned" for issue in ops.check_project()["issues"])
