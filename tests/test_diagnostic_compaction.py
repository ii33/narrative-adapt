"""一次提交里的同类问题必须折叠成一行，不能把同一句话重复几十遍。

现场：模型交上来 26 条属性、每条都缺 `op` 和 `value`，工具返回 52 行几乎一样的报错
（码、消息、位置全同，只有索引不同）。这类输出会淹掉模型能读到的上下文，人也看不出
"到底要改哪几条"。折叠只改说法，不减少诊断：`count` 是实际处数，`indexes` 指出涉及的条目。
"""
from __future__ import annotations

import pytest

from narrative.operations import Operations

TEXT = "李强今年二十九岁，个头矮小，皮肤黝黑。"
PROPERTY_COUNT = 26


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATIVE_PROVIDER_DB", str(tmp_path / "providers.sqlite3"))
    return Operations.create_graph_project(tmp_path / "compact.nproj")


def broken_draft():
    """属性缺 op/value：每条都会触发两条 DRAFT_SCHEMA，正好复现"重复几十遍"。"""
    return {
        "nodes": [
            {"type": "person", "name": "李强"},
            {"type": "scene", "name": "出场"},
            {"type": "event", "name": "李强出场", "scene": "出场", "participants": ["李强"],
             "location": "未知", "quotes": [TEXT]},
        ],
        "properties": [
            {"node": "李强", "property": f"属性{i}", "value": "某个值", "value_type": "string",
             "scope": "world", "at_event": "李强出场", "category": "未分类"}
            for i in range(PROPERTY_COUNT)
        ],
    }


def test_repeated_schema_problems_collapse_into_one_row_per_field(project):
    source = project.import_source("折叠", TEXT)
    result = project.submit_extraction(source, broken_draft())
    assert result["status"] == "staged"
    problems = result["problems"]
    assert len(problems) <= 4, [row["message"] for row in problems]
    by_field = {row.get("field"): row for row in problems if row.get("field")}
    assert set(by_field) == {"op", "value"}
    for field, row in by_field.items():
        assert row["count"] == PROPERTY_COUNT, (field, row["count"])
        assert len(row["indexes"]) <= 12
        assert row["indexes"][:3] == [0, 1, 2]


def test_compaction_keeps_the_problem_actionable(project):
    """折叠后仍要能定位：有字段、有索引、有可读的一句话。"""
    source = project.import_source("可定位", TEXT)
    result = project.submit_extraction(source, broken_draft())
    row = next(item for item in result["problems"] if item.get("field") == "op")
    assert row["path"][0] == "properties"
    assert row["location_scope"] == "field"
    assert "26 处" in row["message"]
    assert row["reason"] != row["message"] if "reason" in row else True


def test_a_single_problem_is_not_wrapped(project):
    """只有一条问题时不该出现 count／索引这类折叠痕迹。"""
    draft = broken_draft()
    draft["properties"] = draft["properties"][:1]
    source = project.import_source("单条", TEXT)
    result = project.submit_extraction(source, draft)
    rows = [row for row in result["problems"] if row.get("field")]
    assert len(rows) == 2
    for row in rows:
        assert row["indexes"] == [0]
        assert "共 1 处" not in row["message"]
